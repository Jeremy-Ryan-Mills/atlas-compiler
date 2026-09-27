#!/usr/bin/env python3
"""Measure an existing Atlas assembly witness using a recorded EE290 simulator.

The reported metric is the program's CSR_DBG1 bracket, not the host's raw cycle
counter or simulator wall time. Source RTL and baremetal inputs remain read-only.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sys

from rtlgraph_s0 import artifact, checked_path, timestamp
from rtlgraph_smoke import classify, compile_commands, copy_inputs, dependencies, run_command, simulator_command
from rtlgraph_mxu1_capture import (VPD2VCD, capture_tcl, closure_mode, conversion_environment,
                                  validate_smoke, validate_trace, verify_artifact, verify_runtime)


MEASUREMENT_SCOPE = ("CSR_DBG1 delta between assembly CSRR 0xC00 snapshots; for the original K64 witness, "
                     "starts before first weight push and ends immediately after final FP8-pop issue, "
                     "excluding setup, final pop drain, and DRAM writeback")


def fixture_info(path: Path) -> dict:
    fixture = json.loads(path.read_text())
    counts = {}
    for category, value_key in (("dram_preloads", "data"), ("dram_checks", "expected")):
        entries = fixture.get(category, [])
        if not isinstance(entries, list) or (category == "dram_checks" and not entries):
            raise ValueError("Performance replay requires nonempty DRAM golden checks")
        for entry in entries:
            if not isinstance(entry.get("word_offset"), int) or entry["word_offset"] < 0:
                raise ValueError("Golden fixture must use nonnegative beat offsets")
            value = int(entry[value_key], 0)
            if not 0 <= value < 1 << 256:
                raise ValueError("Fixture values must fit the assembler's 32-byte beat")
        counts[category + "_beats"] = len(entries)
    return {**counts, "beat_bytes": 32, "expected_check_words": counts["dram_checks_beats"] * 8,
            "preload_words": counts["dram_preloads_beats"] * 8,
            "scope": "Existing fixture reused; golden generator not rerun or independently validated"}


def performance_result(run: dict, log: str, name: str, expected_checks: int) -> dict:
    if run.get("interrupted"):
        return {"status": "INTERRUPTED"}
    marker = f"*** PASSED *** ({name} — all DRAM checks passed)"
    status = classify(run["returncode"], run["timed_out"], log, marker, require_mxu_completion=True)
    if status != "PASS":
        return {"status": status}
    checks = re.findall(r"Verifying DRAM results \(([0-9]+) words\)", log)
    if checks != [str(expected_checks)]:
        return {"status": "CHECK_COUNT_MISMATCH", "reported_counts": checks}
    metrics = {}
    for name, pattern in {
        "dbg1_cycles": r"^\s*dbg1_cycles\s*=\s*([0-9]+)\s*$",
        "raw_mcycles": r"^\s*mcycles\s*=\s*([0-9]+)\s*$",
        "minstret": r"^\s*minstret\s*=\s*([0-9]+)\s*$",
        "dbg0": r"^\s*DBG0\s*=\s*([0-9]+)\s*$",
    }.items():
        values = re.findall(pattern, log, re.MULTILINE)
        if len(values) != 1:
            return {"status": "MISSING_OR_AMBIGUOUS_METRIC", "metric": name}
        metrics[name] = int(values[0])
    if metrics["dbg1_cycles"] == 0:
        return {"status": "ZERO_CYCLE_WINDOW"}
    statuses = re.findall(r"^\s*status\s*=\s*(0x[0-9a-fA-F]+)\s*$", log, re.MULTILINE)
    if len(statuses) != 1:
        return {"status": "MISSING_OR_AMBIGUOUS_METRIC", "metric": "status"}
    metrics["csr_status"] = int(statuses[0], 16)
    util = re.findall(r"^\s*util_mxu1\s*=\s*([0-9]+)%", log, re.MULTILINE)
    if len(util) == 1:
        metrics["reported_integer_util_mxu1_percent"] = int(util[0])
    return {"status": "PASS", "checked_words": expected_checks, "metrics": metrics,
            "measurement_scope": MEASUREMENT_SCOPE,
            "counter_caveat": "CSR counter reads may suppress an increment; report observed bracket values without treating them as waveform edge counts"}


def capture_signal_map(selection, *, banks: bool, mxu0: bool = False) -> dict:
    if mxu0:
        if banks:
            raise ValueError("MXU0 and MXU1 bank capture modes are mutually exclusive")
        return selection.SIGNALS
    signals = selection.BANK_SIGNALS if banks else selection.PERF_SIGNALS
    if banks and any(signals.get(field) != signal for field, signal in selection.PERF_SIGNALS.items()):
        raise ValueError("Bank capture must retain the complete performance signal map")
    return signals


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--smoke-manifest", type=Path, required=True)
    parser.add_argument("--assembly", type=Path, required=True)
    parser.add_argument("--golden-json", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True, help="New isolated output directory")
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--capture", action="store_true", help="Passively capture the expanded MXU1 performance signal map")
    parser.add_argument("--capture-banks", action="store_true", help="Also capture actual MregFile P0/P1 reads; implies --capture")
    parser.add_argument("--capture-mxu0", action="store_true", help="Capture the separate MXU0 overlap map; implies --capture")
    parser.add_argument("--timeout-seconds", type=float, default=900)
    args = parser.parse_args(argv)
    if args.capture_mxu0 and args.capture_banks:
        parser.error("--capture-mxu0 and --capture-banks are mutually exclusive")
    args.capture = args.capture or args.capture_banks or args.capture_mxu0
    if args.timeout_seconds <= 0:
        parser.error("Timeout must be positive")
    smoke_path, assembly, golden, output = map(checked_path, (args.smoke_manifest, args.assembly, args.golden_json, args.output))
    if not re.fullmatch(r"[A-Za-z0-9_]+", assembly.stem):
        parser.error("Assembly filename stem must contain only letters, digits, and underscores")
    if output == smoke_path.parent or smoke_path.parent in output.parents:
        parser.error("Output cannot be inside the source smoke run")
    if output == assembly.parent or assembly.parent in output.parents:
        parser.error("Output cannot be inside the source assembly directory")
    output.mkdir(parents=True, exist_ok=False)
    manifest = output / "manifest.json"
    record = {"schema_version": 1, "kind": "rtlgraph-perf-replay", "config": "EE290SimConfig",
              "status": "preparing", "started_utc": timestamp(), "commands": [],
              "simulator_build_lineage": "UNVERIFIED: recorded cached binary; saved FIRRTL equality is not source-to-binary build proof"}

    def save() -> None:
        manifest.write_text(json.dumps(record, indent=2) + "\n")

    def command(argv: list[str], log_name: str, *, require_success: bool = True) -> dict:
        result = run_command(argv, output, env, output / log_name, args.timeout_seconds)
        record["commands"].append(result)
        save()
        if result["interrupted"]:
            raise KeyboardInterrupt
        if require_success and (result["returncode"] != 0 or result["timed_out"]):
            raise RuntimeError(f"Command failed; see {log_name}")
        return result

    try:
        source = json.loads(smoke_path.read_text())
        validate_smoke(source)
        assembler = verify_artifact(source["inputs"]["assembler"])
        gcc = verify_artifact(source["inputs"]["gcc"])
        for info in source["toolchain_link_inputs"].values():
            verify_artifact(info)
        record["inputs"] = {"driver": artifact(checked_path(__file__)), "smoke_manifest": artifact(smoke_path),
                            "assembly": artifact(assembly), "golden_fixture": artifact(golden),
                            "assembler": artifact(assembler), "gcc": artifact(gcc)}
        record["helper_inputs"] = {name: artifact(checked_path(Path(__file__).parent / name)) for name in
                                  ("rtlgraph_smoke.py", "rtlgraph_mxu1_capture.py", "rtlgraph_s0.py")}
        record["fixture"] = fixture_info(golden)
        record["toolchain_link_inputs"] = source["toolchain_link_inputs"]
        record["saved_fir_matches_fresh_fir"] = source["saved_fir_matches_fresh_fir"]
        record["measurement_scope"] = MEASUREMENT_SCOPE
        converter_home = checked_path(VPD2VCD.parent.parent)
        env = conversion_environment(source["environment"], converter_home)
        record["environment"] = {key: env[key] for key in ("PATH", "LD_LIBRARY_PATH", "VCS_HOME", "VPD_HOME", "VCS_ARCH_OVERRIDE", "VCS_MODE_FLAG")}
        record["license_environment_present"] = bool(env.get("SNPSLMD_LICENSE_FILE") or env.get("LM_LICENSE_FILE"))
        name = assembly.stem
        generated_c, binary = output / f"atlas_{name}.c", output / f"atlas_{name}.riscv"
        command([sys.executable, str(assembler), str(assembly), "--golden-json", str(golden), "--out-c", str(generated_c)], "assemble.log")
        record["generated_c"] = artifact(generated_c)
        for index, argv in enumerate(compile_commands(gcc, generated_c, binary, vector=False)):
            command(argv, f"build-{index}.log")
        record["binary"] = artifact(binary)
        record["compiler_dependencies"] = dependencies(binary.with_suffix(".d"))
        source_runtime = checked_path(smoke_path.parent / "runtime")
        simulator_name = Path(source["inputs"]["simulator"]["path"]).name
        verify_artifact(source["inputs"]["simulator"], path=source_runtime / simulator_name)
        verify_runtime(checked_path(source_runtime / (simulator_name + ".daidir")), source["runtime_files"])
        verify_runtime(checked_path(source_runtime / "coverage-template.vdb"), source["coverage_design_inputs"])
        verify_runtime(checked_path(source_runtime / "dramsim2_ini"), source["dramsim_inputs"])
        for info in source["runtime_libraries"].values():
            verify_artifact(info)
        runtime = output / "runtime"
        record["runtime_files"] = copy_inputs(source_runtime, runtime)
        record["runtime_libraries"] = source["runtime_libraries"]
        simulator = checked_path(runtime / simulator_name)
        record["simulator"] = artifact(simulator)
        coverage = output / "coverage.vdb"
        copy_inputs(runtime / "coverage-template.vdb", coverage)
        argv = simulator_command(simulator, binary, runtime / "dramsim2_ini", coverage, name)
        if args.capture:
            if args.capture_mxu0:
                import rtlgraph_mxu0_vcd as selection
            else:
                import rtlgraph_mxu1_vcd as selection
            signal_map = capture_signal_map(selection, banks=args.capture_banks, mxu0=args.capture_mxu0)
            record["capture_kind"] = "mxu0_overlap" if args.capture_mxu0 else "mxu1_mreg_banks" if args.capture_banks else "mxu1_perf"
            record["signal_selection_driver"] = artifact(checked_path(selection.__file__))
            record["signals"] = {field: {"path": path, "width": width} for field, (path, width) in sorted(signal_map.items())}
            engine = "mxu0" if args.capture_mxu0 else "mxu1"
            tcl, vpd, trace, completed = output / "capture.tcl", output / f"{engine}.vpd", output / f"{engine}.vcd", output / "capture-complete.txt"
            tcl.write_text(capture_tcl(signal_map, vpd, completed))
            record["capture_tcl"] = artifact(tcl)
            argv[argv.index("+permissive-off"):argv.index("+permissive-off")] = ["-ucli", "-i", str(tcl), "-k", "off"]
        record.update(status="prepared", prepared_command={"argv": argv, "cwd": str(output)})
        save()
        if args.run:
            result = command(argv, "simulation.log", require_success=False)
            log = Path(result["log"]["path"]).read_text(errors="replace")
            record["result"] = performance_result(result, log, name, record["fixture"]["expected_check_words"])
            record["status"] = "passed" if record["result"]["status"] == "PASS" else "failed"
            save()
            if args.capture and vpd.is_file():
                record["vpd"] = artifact(vpd)
                record["capture_closure_mode"] = closure_mode(log, completed)
                converter = checked_path(VPD2VCD)
                record["converter"] = {"driver": artifact(converter),
                                       "dispatcher": artifact(checked_path(converter_home / "bin/common.vpd")),
                                       "executable": artifact(checked_path(converter_home / "linux64/bin/vpd2vcd.exe"))}
                conversion = command([str(converter), str(vpd), str(trace)], "conversion.log")
                banner = Path(conversion["log"]["path"]).read_text(errors="replace")
                version = re.search(r"Version\s*:\s*(\S+)", banner)
                record["converter"]["reported_version"] = version.group(1) if version else None
                record["trace"] = artifact(trace)
                record["trace_validation"] = validate_trace(trace, record["signals"])
                if not record["capture_closure_mode"]:
                    raise ValueError("Simulator did not close the capture normally")
            elif args.capture:
                raise ValueError("Requested VPD capture was not produced")
        for role in ("assembly", "golden_fixture", "assembler", "gcc"):
            verify_artifact(record["inputs"][role])
        record["finished_utc"] = timestamp()
        save()
        print(json.dumps({"status": record["status"], "result": record.get("result"), "manifest": str(manifest)}, indent=2))
        return int(record["status"] == "failed")
    except KeyboardInterrupt:
        record.update(status="interrupted", finished_utc=timestamp())
        save()
        return 130
    except Exception as error:
        record.update(status="failed", error=str(error), finished_utc=timestamp())
        save()
        print(f"Performance replay failed: {error}; see {manifest}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

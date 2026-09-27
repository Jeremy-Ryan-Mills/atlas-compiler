#!/usr/bin/env python3
"""Replay the passing MXU1 smoke witness with a narrow UCLI waveform capture.

Capture is a finite execution observation, not a timing proof. The cached
simulator's source-to-binary build linkage remains unverified.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import sys

from rtlgraph_s0 import artifact, checked_path, timestamp
from rtlgraph_smoke import (MXU_TEST, PASS_MARKERS, classify, copy_inputs,
                           run_command, simulator_command)


VPD2VCD = Path("/tools/synopsys/vcs/W-2024.09-1/bin/vpd2vcd")


def verify_artifact(info: dict, *, path: Path | None = None) -> Path:
    if not isinstance(info, dict) or not isinstance(info.get("sha256"), str):
        raise ValueError("Missing recorded artifact SHA-256")
    actual = checked_path(path if path is not None else info["path"])
    if artifact(actual)["sha256"] != info["sha256"]:
        raise ValueError(f"Artifact SHA-256 changed: {actual}")
    return actual


def validate_smoke(record: dict) -> dict:
    if record.get("schema_version") != 1 or record.get("kind") != "rtlgraph-functional-smoke":
        raise ValueError("Expected an rtlgraph-functional-smoke manifest")
    if record.get("config") != "EE290SimConfig":
        raise ValueError("Capture currently supports EE290SimConfig only")
    if record.get("status") != "passed":
        raise ValueError("Source smoke run must have passed")
    test = record.get("tests", {}).get(MXU_TEST, {})
    run = test.get("run", {})
    if test.get("status") != "PASS" or run.get("returncode") != 0 or run.get("timed_out") or run.get("interrupted"):
        raise ValueError("Source MXU1 witness must have completed successfully")
    log = verify_artifact(run["log"])
    if classify(0, False, log.read_text(errors="replace"), PASS_MARKERS[MXU_TEST], require_mxu_completion=True) != "PASS":
        raise ValueError("Source MXU1 log does not establish the expected completion")
    for role in ("driver", "simulator"):
        verify_artifact(record["inputs"][role])
    verify_artifact(test["binary"])
    return test


def relative_runtime_path(value: str) -> Path:
    path = Path(value)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError("Runtime manifest paths must be relative and cannot escape their root")
    return path


def verify_runtime(directory: Path, files: list[dict]) -> None:
    if not files:
        raise ValueError("Runtime manifest must contain recorded files")
    for info in files:
        verify_artifact(info, path=checked_path(directory / relative_runtime_path(info["relative_path"])))


def tcl_word(value: str) -> str:
    if any(character in value for character in "{}\\\n\r"):
        raise ValueError("Capture paths and names cannot contain Tcl grouping or escape characters")
    return "{" + value + "}"


def capture_tcl(signals: dict[str, tuple[str, int]], trace: Path, completed: Path) -> str:
    if not signals:
        raise ValueError("At least one trace signal is required")
    paths = set()
    for path, width in signals.values():
        if not isinstance(width, int) or width <= 0 or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_$.]*", path):
            raise ValueError("Signal selection must contain hierarchical names and positive widths")
        paths.add(path)
    # This VCS UCLI supports VPD/EVCD, not VCD. Convert the closed VPD afterward.
    lines = [f"set fid [dump -file {tcl_word(str(trace))} -type VPD]"]
    lines.extend(f"dump -add {tcl_word(path)} -fid $fid" for path in sorted(paths))
    lines.extend(["run", "dump -close", f"set completed [open {tcl_word(str(completed))} w]",
                  'puts $completed "rtlgraph-capture-complete"', "close $completed", "quit", ""])
    return "\n".join(lines)


def capture_command(simulator: Path, binary: Path, ini: Path, coverage: Path, tcl: Path) -> list[str]:
    command = simulator_command(simulator, binary, ini, coverage, MXU_TEST)
    position = command.index("+permissive-off")
    command[position:position] = ["-ucli", "-i", str(tcl), "-k", "off"]
    return command


def capture_outcome(result: dict, log: str, trace: Path, completed: Path) -> str:
    if result.get("interrupted"):
        return "INTERRUPTED"
    status = classify(result["returncode"], result["timed_out"], log, PASS_MARKERS[MXU_TEST], require_mxu_completion=True)
    if status != "PASS":
        return status
    if closure_mode(log, completed) is None:
        return "CAPTURE_NOT_CLOSED"
    if not trace.is_file() or trace.stat().st_size == 0:
        return "MISSING_TRACE"
    return "CAPTURED"


def closure_mode(log: str, completed: Path) -> str | None:
    if completed.is_file() and completed.read_text().strip() == "rtlgraph-capture-complete":
        return "TCL_DUMP_CLOSE"
    # TestDriver's $finish exits this VCS before Tcl resumes after `run`.
    # The caller separately requires successful process/architectural completion.
    if (re.search(r"\$finish at simulation time\s+[0-9]+", log)
            and "VCSSimulationReport" in re.sub(r"\s+", "", log)
            and "CPU Time:" in log):
        return "NORMAL_VCS_FINISH"
    return None


def conversion_environment(recorded: dict, converter_home: Path) -> dict:
    env = dict(os.environ)
    for key in ("PATH", "LD_LIBRARY_PATH"):
        if not isinstance(recorded.get(key), str) or not recorded[key]:
            raise ValueError(f"Missing recorded {key}")
        env[key] = recorded[key]
    env.update(PYTHONDONTWRITEBYTECODE="1", VCS_HOME=str(converter_home), VPD_HOME=str(converter_home),
               VCS_ARCH_OVERRIDE="linux", VCS_MODE_FLAG="64")
    return env


def validate_trace(trace: Path, signal_map: dict) -> dict:
    import rtlgraph_mxu1_vcd as adapter
    before = artifact(trace)
    signals = {key: (value["path"], value["width"]) for key, value in signal_map.items()}
    with trace.open() as stream:
        selected, timescale = adapter.read_header(stream, signals)
        edges = sum(1 for _ in adapter.edge_samples(stream, selected, signals))
    if edges == 0 or artifact(trace) != before:
        raise ValueError("Trace is empty or changed while parsing")
    return {"status": "PARSED", "rising_edges": edges, "timescale": timescale,
            "parser": artifact(checked_path(adapter.__file__)), "scope": "Structural waveform parsing only; timing obligations checked separately"}


def finalize_existing(path: Path, timeout: float) -> int:
    """Recover a recorded successful simulation without rerunning it."""
    path = checked_path(path)
    original = path.read_bytes()
    record = json.loads(original)
    if record.get("kind") != "rtlgraph-mxu1-capture" or record.get("schema_version") != 1:
        raise ValueError("Expected an rtlgraph-mxu1-capture manifest")
    output = path.parent
    vpd = verify_artifact(record["vpd"])
    simulator, binary = verify_artifact(record["simulator"]), verify_artifact(record["binary"])
    tcl = verify_artifact(record["capture_tcl"])
    simulation = record["commands"][0]
    command = simulation["argv"]
    if (command != record["prepared_command"]["argv"] or checked_path(command[0]) != simulator
            or checked_path(command[-1]) != binary or checked_path(command[command.index("-i") + 1]) != tcl):
        raise ValueError("Recorded simulator command does not match prepared artifacts")
    log = verify_artifact(simulation["log"]).read_text(errors="replace")
    completed = checked_path(output / "capture-complete.txt")
    outcome = capture_outcome(simulation, log, vpd, completed)
    if outcome != "CAPTURED":
        raise ValueError(f"Cannot finalize an unsuccessful/incomplete simulation: {outcome}")
    converter = checked_path(VPD2VCD)
    converter_home = checked_path(VPD2VCD.parent.parent)
    env = conversion_environment(record["environment"], converter_home)
    number = len(record.get("finalizations", [])) + 1
    snapshot = checked_path(output / f"manifest-before-finalize-{number}.json")
    conversion_log = checked_path(output / f"conversion-finalize-{number}.log")
    trace = checked_path(output / "mxu1.vcd")
    if trace.exists():
        trace = checked_path(output / f"mxu1-finalize-{number}.vcd")
    if snapshot.exists() or conversion_log.exists() or trace.exists():
        raise ValueError("Finalization outputs already exist; refusing to overwrite history")
    snapshot.write_bytes(original)
    finalization = {"started_utc": timestamp(), "source_status": record["status"],
                    "source_manifest": artifact(snapshot), "driver": artifact(checked_path(__file__)),
                    "closure_mode": closure_mode(log, completed), "vpd": record["vpd"],
                    "converter": {"driver": artifact(converter),
                                  "dispatcher": artifact(checked_path(converter_home / "bin/common.vpd")),
                                  "executable": artifact(checked_path(converter_home / "linux64/bin/vpd2vcd.exe"))},
                    "environment": {key: env[key] for key in ("PATH", "LD_LIBRARY_PATH", "VCS_HOME", "VPD_HOME", "VCS_ARCH_OVERRIDE", "VCS_MODE_FLAG")}}
    conversion = run_command([str(converter), str(vpd), str(trace)], output, env, conversion_log, timeout)
    finalization["conversion"] = conversion
    record["commands"].append(conversion)
    banner = conversion_log.read_text(errors="replace")
    version = re.search(r"Version\s*:\s*(\S+)", banner)
    finalization["converter"]["reported_version"] = version.group(1) if version else None
    final_outcome = "INTERRUPTED" if conversion.get("interrupted") else "CONVERSION_FAILED"
    if conversion["returncode"] == 0 and not conversion["timed_out"] and not conversion.get("interrupted"):
        try:
            finalization["trace_validation"] = validate_trace(trace, record["signals"])
            if artifact(vpd)["sha256"] != record["vpd"]["sha256"]:
                raise ValueError("VPD changed during finalization")
            record["trace"] = artifact(trace)
            final_outcome = "CAPTURED"
        except Exception as error:
            finalization["error"] = str(error)
            final_outcome = "TRACE_PARSE_FAILED"
    finalization.update(finished_utc=timestamp(), outcome=final_outcome)
    record.setdefault("source_status_before_finalization", record["status"])
    record.setdefault("finalizations", []).append(finalization)
    record["status"] = "captured" if final_outcome == "CAPTURED" else ("interrupted" if final_outcome == "INTERRUPTED" else "failed")
    record["outcome"] = final_outcome
    record["closure_mode"] = finalization["closure_mode"]
    record["finished_utc"] = timestamp()
    if path.read_bytes() != original:
        raise ValueError("Capture manifest changed concurrently; finalization not applied")
    path.write_text(json.dumps(record, indent=2) + "\n")
    print(json.dumps({"status": record["status"], "outcome": final_outcome, "manifest": str(path)}, indent=2))
    return 130 if final_outcome == "INTERRUPTED" else int(final_outcome != "CAPTURED")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--smoke-manifest", type=Path)
    parser.add_argument("--output", type=Path, help="New directory outside the source smoke output")
    parser.add_argument("--finalize-existing", type=Path, help="Convert and validate a recorded capture without rerunning VCS")
    parser.add_argument("--run", action="store_true", help="Run the prepared simulator capture")
    parser.add_argument("--timeout-seconds", type=float, default=300)
    args = parser.parse_args(argv)
    if args.timeout_seconds <= 0:
        parser.error("--timeout-seconds must be positive")
    if args.finalize_existing:
        if args.smoke_manifest or args.output or args.run:
            parser.error("--finalize-existing cannot be combined with capture arguments")
        try:
            return finalize_existing(args.finalize_existing, args.timeout_seconds)
        except Exception as error:
            print(f"Capture finalization failed: {error}", file=sys.stderr)
            return 1
    if not args.smoke_manifest or not args.output:
        parser.error("Capture requires --smoke-manifest and --output")
    source_manifest = checked_path(args.smoke_manifest)
    source_output = source_manifest.parent
    output = checked_path(args.output)
    if output == source_output or source_output in output.parents:
        parser.error("Output must be outside the source smoke output")
    output.mkdir(parents=True, exist_ok=False)
    manifest = output / "manifest.json"
    record = {"schema_version": 1, "kind": "rtlgraph-mxu1-capture", "status": "preparing",
              "started_utc": timestamp(), "config": "EE290SimConfig", "commands": [],
              "scope": "One existing matrix-multiply witness with passive selected-signal capture; temporal analysis is separate",
              "simulator_build_lineage": "UNVERIFIED: cached binary with recorded provenance, not a source-to-binary build proof"}

    def save() -> None:
        manifest.write_text(json.dumps(record, indent=2) + "\n")

    try:
        import rtlgraph_mxu1_vcd as selection
        record["driver"] = artifact(checked_path(__file__))
        record["signal_selection_driver"] = artifact(checked_path(selection.__file__))
        converter = checked_path(VPD2VCD)
        converter_home = checked_path(VPD2VCD.parent.parent)
        record["converter"] = {"driver": artifact(converter),
                               "dispatcher": artifact(checked_path(converter_home / "bin/common.vpd")),
                               "executable": artifact(checked_path(converter_home / "linux64/bin/vpd2vcd.exe"))}
        record["source_smoke_manifest"] = artifact(source_manifest)
        source = json.loads(source_manifest.read_text())
        test = validate_smoke(source)
        record["source_smoke_status"] = source["status"]
        record["source_witness"] = {"name": MXU_TEST, "binary": test["binary"], "run_log": test["run"]["log"]}
        record["saved_fir_matches_fresh_fir"] = source.get("saved_fir_matches_fresh_fir")
        record["source_provenance"] = {role: source["inputs"][role] for role in ("driver", "simulator", "saved_fir", "fresh_fir")}
        source_runtime = checked_path(source_output / "runtime")
        simulator_name = Path(source["inputs"]["simulator"]["path"]).name
        verify_artifact(source["inputs"]["simulator"], path=source_runtime / simulator_name)
        verify_runtime(checked_path(source_runtime / (simulator_name + ".daidir")), source["runtime_files"])
        verify_runtime(checked_path(source_runtime / "coverage-template.vdb"), source["coverage_design_inputs"])
        verify_runtime(checked_path(source_runtime / "dramsim2_ini"), source["dramsim_inputs"])
        for info in source["runtime_libraries"].values():
            verify_artifact(info)
        runtime = output / "runtime"
        record["runtime_files"] = copy_inputs(source_runtime, runtime)
        binary = output / (MXU_TEST + ".riscv")
        shutil.copy2(verify_artifact(test["binary"]), binary)
        verify_artifact(test["binary"], path=binary)
        record["binary"] = artifact(binary)
        simulator = checked_path(runtime / simulator_name)
        record["simulator"] = artifact(simulator)
        record["runtime_libraries"] = source["runtime_libraries"]
        coverage = output / "coverage.vdb"
        copy_inputs(runtime / "coverage-template.vdb", coverage)
        env = conversion_environment(source.get("environment", {}), converter_home)
        record["environment"] = {key: env[key] for key in ("PATH", "LD_LIBRARY_PATH", "PYTHONDONTWRITEBYTECODE", "VCS_HOME", "VPD_HOME", "VCS_ARCH_OVERRIDE", "VCS_MODE_FLAG")}
        record["license_environment_present"] = bool(env.get("SNPSLMD_LICENSE_FILE") or env.get("LM_LICENSE_FILE"))
        record["signals"] = {field: {"path": path, "width": width} for field, (path, width) in sorted(selection.SIGNALS.items())}
        trace, vpd, completed, tcl = output / "mxu1.vcd", output / "mxu1.vpd", output / "capture-complete.txt", output / "capture.tcl"
        tcl.write_text(capture_tcl(selection.SIGNALS, vpd, completed))
        record["capture_tcl"] = artifact(tcl)
        command = capture_command(simulator, binary, runtime / "dramsim2_ini", coverage, tcl)
        record.update(status="prepared", prepared_command={"argv": command, "cwd": str(output)})
        save()
        if args.run:
            result = run_command(command, output, env, output / "simulation.log", args.timeout_seconds)
            record["commands"].append(result)
            save()
            conversion = None
            if vpd.is_file():
                record["vpd"] = artifact(vpd)
                if not result.get("interrupted"):
                    conversion = run_command([str(converter), str(vpd), str(trace)], output, env,
                                             output / "conversion.log", args.timeout_seconds)
                    record["commands"].append(conversion)
                    banner = Path(conversion["log"]["path"]).read_text(errors="replace")
                    version = re.search(r"Version\s*:\s*(\S+)", banner)
                    record["converter"]["reported_version"] = version.group(1) if version else None
                    record["converter"]["run"] = conversion
                    save()
            outcome = capture_outcome(result, Path(result["log"]["path"]).read_text(errors="replace"), trace, completed)
            if conversion and conversion.get("interrupted"):
                outcome = "INTERRUPTED"
            elif conversion and (conversion["returncode"] != 0 or conversion["timed_out"]):
                outcome = "CONVERSION_FAILED"
            if outcome == "CAPTURED":
                record["trace_validation"] = validate_trace(trace, record["signals"])
                record["closure_mode"] = closure_mode(Path(result["log"]["path"]).read_text(errors="replace"), completed)
            record["outcome"] = outcome
            record["status"] = "captured" if outcome == "CAPTURED" else ("interrupted" if outcome == "INTERRUPTED" else "failed")
            if trace.is_file():
                record["trace"] = artifact(trace)
            if completed.is_file():
                record["capture_completed"] = artifact(completed)
            record["temporal_analysis"] = "PENDING: trace presence and functional completion do not validate timing"
        record["finished_utc"] = timestamp()
        save()
        print(json.dumps({"status": record["status"], "outcome": record.get("outcome"), "manifest": str(manifest)}, indent=2))
        return 130 if record["status"] == "interrupted" else int(record["status"] == "failed")
    except KeyboardInterrupt:
        record.update(status="interrupted", finished_utc=timestamp())
        save()
        return 130
    except Exception as error:
        record.update(status="failed", error=str(error), finished_utc=timestamp())
        save()
        print(f"MXU1 capture failed: {error}; see {manifest}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

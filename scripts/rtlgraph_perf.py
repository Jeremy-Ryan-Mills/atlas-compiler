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


def capture_signal_map(selection, *, banks: bool, mxu0: bool = False, vpu: bool = False, lsu: bool = False, dma: bool = False, mixed: bool = False) -> dict:
    if sum((banks, mxu0, vpu, lsu, dma, mixed)) > 1:
        raise ValueError("Engine-specific and bank capture modes are mutually exclusive")
    if vpu or lsu or dma or mixed:
        from rtlgraph_mxu1_vcd import PERF_SIGNALS
        signals = selection.SIGNALS
        for field, signal in PERF_SIGNALS.items():
            if field.startswith(('scalar.', 'csr.')) and signals.get(field) != signal:
                raise ValueError("VPU capture must retain complete scalar and CSR signals")
        if any(field not in signals or signals[field][1] != 1 for field in ('clock', 'reset')):
            raise ValueError("VPU capture requires one-bit clock and reset")
        return signals
    if mxu0:
        return selection.SIGNALS
    signals = selection.BANK_SIGNALS if banks else selection.PERF_SIGNALS
    if banks and any(signals.get(field) != signal for field, signal in selection.PERF_SIGNALS.items()):
        raise ValueError("Bank capture must retain the complete performance signal map")
    return signals


def probe_host_result(run: dict, log: str, name: str) -> dict:
    """Host completion alone cannot distinguish a probe's aliased failure code."""
    if run.get('interrupted'):
        return {'status': 'INTERRUPTED'}
    status = classify(run['returncode'], run['timed_out'], log,
                      f'*** PASSED *** ({name})', require_mxu_completion=True)
    if status != 'PASS':
        return {'status': status}
    dbg0 = re.findall(r'^\s*DBG0\s*=\s*([0-9]+)\s*$', log, re.MULTILINE)
    statuses = re.findall(r'^\s*status\s*=\s*(0x[0-9a-fA-F]+)\s*$', log, re.MULTILINE)
    if dbg0 != ['1'] or len(statuses) != 1 or int(statuses[0], 16) & 7 != 5:
        return {'status': 'MISSING_OR_AMBIGUOUS_COMPLETION'}
    cycles = re.findall(r'^\s*dbg1_cycles\s*=\s*([0-9]+)\s*$', log, re.MULTILINE)
    if len(cycles) != 1 or int(cycles[0]) <= 0:
        return {'status': 'MISSING_OR_AMBIGUOUS_METRIC'}
    return {'status': 'HOST_COMPLETION_ONLY', 'metrics': {'dbg1_cycles': int(cycles[0])},
            'scope': 'Requires independent waveform binding of every numerical check and success PC.'}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--smoke-manifest", type=Path, required=True)
    parser.add_argument("--assembly", type=Path, required=True)
    validation = parser.add_mutually_exclusive_group(required=True)
    validation.add_argument("--golden-json", type=Path)
    validation.add_argument("--vpu-probe", action="store_true", help="Validate the exact binary/reduction probe success path and scalar numerical spot checks")
    parser.add_argument("--output", type=Path, required=True, help="New isolated output directory")
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--capture", action="store_true", help="Passively capture the expanded MXU1 performance signal map")
    parser.add_argument("--capture-banks", action="store_true", help="Also capture actual MregFile P0/P1 reads; implies --capture")
    parser.add_argument("--capture-mxu0", action="store_true", help="Capture the separate MXU0 overlap map; implies --capture")
    parser.add_argument("--capture-vpu", action="store_true", help="Capture VPU row events and scalar/CSR timing; implies --capture")
    parser.add_argument("--capture-lsu", action="store_true", help="Capture LSU/VMEM events together with VPU/scalar timing; implies --capture")
    parser.add_argument("--capture-dma", action="store_true", help="Capture DMA commands/completions and LSU/VPU/scalar timing; implies --capture")
    parser.add_argument("--capture-mixed", action="store_true", help="Capture DMA/LSU/VPU and both MXU row streams together; implies --capture")
    controls = parser.add_mutually_exclusive_group()
    controls.add_argument("--control-program-words", type=int, help="Build a fixed-capacity full-golden host control; padding follows terminal ECALL")
    controls.add_argument("--control-manifest", type=Path, help="Patch only program bytes of a prepared/passed fixed-capacity control ELF")
    parser.add_argument("--timeout-seconds", type=float, default=900)
    args = parser.parse_args(argv)
    if args.vpu_probe and not args.capture_vpu:
        parser.error("--vpu-probe requires --capture-vpu for independent success-path checking")
    if sum((args.capture_mxu0, args.capture_banks, args.capture_vpu, args.capture_lsu, args.capture_dma, args.capture_mixed)) > 1:
        parser.error("Engine-specific capture modes are mutually exclusive")
    if args.vpu_probe and (args.control_program_words is not None or args.control_manifest):
        parser.error("Fixed-host control currently requires full-golden validation")
    if args.control_program_words is not None and not 1 <= args.control_program_words <= 1024:
        parser.error("--control-program-words must be between 1 and 1024")
    args.capture = args.capture or args.capture_banks or args.capture_mxu0 or args.capture_vpu or args.capture_lsu or args.capture_dma or args.capture_mixed
    if args.timeout_seconds <= 0:
        parser.error("Timeout must be positive")
    smoke_path, assembly, output = map(checked_path, (args.smoke_manifest, args.assembly, args.output))
    golden = checked_path(args.golden_json) if args.golden_json else None
    if not re.fullmatch(r"[A-Za-z0-9_]+", assembly.stem):
        parser.error("Assembly filename stem must contain only letters, digits, and underscores")
    if output == smoke_path.parent or smoke_path.parent in output.parents:
        parser.error("Output cannot be inside the source smoke run")
    if output == assembly.parent or assembly.parent in output.parents:
        parser.error("Output cannot be inside the source assembly directory")
    output.mkdir(parents=True, exist_ok=False)
    manifest = output / "manifest.json"
    record = {"schema_version": 1, "kind": "rtlgraph-vpu-probe-replay" if args.vpu_probe else "rtlgraph-perf-replay", "config": "EE290SimConfig",
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
                            "assembly": artifact(assembly),
                            "assembler": artifact(assembler), "gcc": artifact(gcc)}
        record["helper_inputs"] = {name: artifact(checked_path(Path(__file__).parent / name)) for name in
                                  ("rtlgraph_smoke.py", "rtlgraph_mxu1_capture.py", "rtlgraph_s0.py")}
        if golden:
            record['inputs']['golden_fixture'] = artifact(golden)
            record['fixture'] = fixture_info(golden)
        else:
            from rtlgraph_kernel import load_assembler
            import rtlgraph_vpu_probes as probe
            contract = probe.probe_contract(assembly.read_text(), load_assembler(assembler))
            contract_path = output / 'probe-contract.json'
            contract_path.write_text(json.dumps(contract, indent=2) + '\n')
            record['probe_contract'] = artifact(contract_path)
            record['helper_inputs']['rtlgraph_vpu_probes.py'] = artifact(checked_path(probe.__file__))
            record['helper_inputs']['rtlgraph_kernel.py'] = artifact(checked_path(Path(__file__).with_name('rtlgraph_kernel.py')))
        record["toolchain_link_inputs"] = source["toolchain_link_inputs"]
        record["saved_fir_matches_fresh_fir"] = source["saved_fir_matches_fresh_fir"]
        record["measurement_scope"] = ('Sum of the original singleton VPU CSR windows; numerical checks cover one BF16 value per operation.'
                                       if args.vpu_probe else MEASUREMENT_SCOPE)
        converter_home = checked_path(VPD2VCD.parent.parent)
        env = conversion_environment(source["environment"], converter_home)
        record["environment"] = {key: env[key] for key in ("PATH", "LD_LIBRARY_PATH", "VCS_HOME", "VPD_HOME", "VCS_ARCH_OVERRIDE", "VCS_MODE_FLAG")}
        record["license_environment_present"] = bool(env.get("SNPSLMD_LICENSE_FILE") or env.get("LM_LICENSE_FILE"))
        name = assembly.stem
        generated_c, binary = output / f"atlas_{name}.c", output / f"atlas_{name}.riscv"
        if args.control_program_words is not None or args.control_manifest:
            generated_c = output / 'assembler-output.c'
        assemble_command = [sys.executable, str(assembler), str(assembly), '--out-c', str(generated_c)]
        if golden:
            assemble_command += ['--golden-json', str(golden)]
        command(assemble_command, "assemble.log")
        base = None
        if args.control_program_words is not None or args.control_manifest:
            import rtlgraph_replay_control as control
            record['helper_inputs']['rtlgraph_replay_control.py'] = artifact(checked_path(control.__file__))
            record['assembler_generated_c'] = artifact(generated_c)
            capacity = args.control_program_words
            if args.control_manifest:
                base_path = checked_path(args.control_manifest)
                base = json.loads(base_path.read_text())
                if (base.get('kind') != 'rtlgraph-perf-replay' or base.get('status') not in ('prepared', 'passed')
                        or base.get('replay_control', {}).get('mode') != 'fixed_capacity_template'):
                    raise ValueError('Control manifest must be a prepared/passed fixed-capacity template')
                for role in ('smoke_manifest', 'golden_fixture', 'assembler', 'gcc'):
                    verify_artifact(base['inputs'][role])
                    if base['inputs'][role]['sha256'] != record['inputs'][role]['sha256']:
                        raise ValueError(f'Control input differs: {role}')
                record['inputs']['control_manifest'] = artifact(base_path)
                capacity = base['replay_control']['capacity_words']
            controlled_text, info = control.controlled_source(generated_c.read_text(), name, capacity)
            name = control.HOST_NAME
            generated_c, binary = output / f'atlas_{name}.c', output / f'atlas_{name}.riscv'
            generated_c.write_text(controlled_text)
            record['replay_control'] = {'mode': 'fixed_capacity_patch' if base else 'fixed_capacity_template', **info}
            record['replay_control']['scope'] = ('Same host code, ELF layout, fixture, and fixed-capacity IMEM write/readback work. '
                                                 'This does not force identical DRAM state or normalize real Atlas DMA/compute work.')
        record["generated_c"] = artifact(generated_c)
        if base:
            base_c = verify_artifact(base['generated_c'])
            base_binary = verify_artifact(base['binary'])
            if info['host_source_without_program_sha256'] != base['replay_control']['host_source_without_program_sha256']:
                raise ValueError('Generated host outside the program differs from control template')
            if control.PROGRAM.sub('ATLAS_PROGRAM_CONTENTS', controlled_text) != control.PROGRAM.sub('ATLAS_PROGRAM_CONTENTS', base_c.read_text()):
                raise ValueError('Control source equality check failed')
            patched, region = control.patch_binary(base_binary.read_bytes(), control.source_program(base_c.read_text()),
                                                    control.source_program(controlled_text), capacity)
            binary.write_bytes(patched)
            record['replay_control']['template_binary'] = artifact(base_binary)
            record['compiler_dependencies'] = base['compiler_dependencies']
            for dependency in record['compiler_dependencies']:
                verify_artifact(dependency)
        else:
            for index, argv in enumerate(compile_commands(gcc, generated_c, binary, vector=False)):
                command(argv, f"build-{index}.log")
            record['compiler_dependencies'] = dependencies(binary.with_suffix('.d'))
            if args.control_program_words is not None:
                region = control.inspect_binary(binary.read_bytes(), control.source_program(controlled_text), capacity)
        if args.control_program_words is not None or base:
            record['replay_control']['elf_region'] = region
        record["binary"] = artifact(binary)
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
            if args.capture_mixed:
                import rtlgraph_mixed_vcd as selection
            elif args.capture_dma:
                import rtlgraph_dma_vcd as selection
            elif args.capture_lsu:
                import rtlgraph_lsu_vcd as selection
            elif args.capture_vpu:
                import rtlgraph_vpu_vcd as selection
            elif args.capture_mxu0:
                import rtlgraph_mxu0_vcd as selection
            else:
                import rtlgraph_mxu1_vcd as selection
            signal_map = capture_signal_map(selection, banks=args.capture_banks, mxu0=args.capture_mxu0, vpu=args.capture_vpu, lsu=args.capture_lsu, dma=args.capture_dma, mixed=args.capture_mixed)
            record["capture_kind"] = "dma_lsu_vpu_mxu_rows" if args.capture_mixed else "dma_lsu_vpu_rows" if args.capture_dma else "lsu_vpu_rows" if args.capture_lsu else "vpu_rows" if args.capture_vpu else "mxu0_overlap" if args.capture_mxu0 else "mxu1_mreg_banks" if args.capture_banks else "mxu1_perf"
            record["signal_selection_driver"] = artifact(checked_path(selection.__file__))
            record["signals"] = {field: {"path": path, "width": width} for field, (path, width) in sorted(signal_map.items())}
            engine = "mixed" if args.capture_mixed else "dma" if args.capture_dma else "lsu" if args.capture_lsu else "vpu" if args.capture_vpu else "mxu0" if args.capture_mxu0 else "mxu1"
            tcl, vpd, trace, completed = output / "capture.tcl", output / f"{engine}.vpd", output / f"{engine}.vcd", output / "capture-complete.txt"
            tcl.write_text(capture_tcl(signal_map, vpd, completed))
            record["capture_tcl"] = artifact(tcl)
            argv[argv.index("+permissive-off"):argv.index("+permissive-off")] = ["-ucli", "-i", str(tcl), "-k", "off"]
        record.update(status="prepared", prepared_command={"argv": argv, "cwd": str(output)})
        save()
        if args.run:
            result = command(argv, "simulation.log", require_success=False)
            log = Path(result["log"]["path"]).read_text(errors="replace")
            record["result"] = (probe_host_result(result, log, name) if args.vpu_probe else
                                performance_result(result, log, name, record["fixture"]["expected_check_words"]))
            record["status"] = ("awaiting_probe_checks" if record['result']['status'] == 'HOST_COMPLETION_ONLY' else
                                "passed" if record["result"]["status"] == "PASS" else "failed")
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
            if args.vpu_probe and record['result']['status'] == 'HOST_COMPLETION_ONLY':
                from rtlgraph_mxu1_vcd import edge_samples, read_header
                with trace.open() as stream:
                    selected, timescale = read_header(stream, signal_map)
                    observation = probe.analyze_samples(edge_samples(stream, selected, signal_map), contract)
                verify_artifact(record['trace'])
                verify_artifact(record['probe_contract'])
                if observation['metrics']['csr_counter_delta'] != record['result']['metrics']['dbg1_cycles']:
                    raise ValueError('Probe waveform cycle sum differs from host DBG1')
                observation_path = output / 'probe-observation.json'
                observation_path.write_text(json.dumps({**observation, 'timescale': timescale}, indent=2) + '\n')
                record['probe_observation'] = artifact(observation_path)
                record['result']['status'] = 'SPOT_CHECKS_PASS'
                record['result']['scope'] = 'Original numerical spot checks and complete success path observed; remaining tensor elements and other inputs are not validated.'
                record['status'] = 'passed'
        for role in ("assembly", "assembler", "gcc") + (("golden_fixture",) if golden else ()):
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

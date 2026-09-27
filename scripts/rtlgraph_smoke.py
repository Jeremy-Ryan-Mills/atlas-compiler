#!/usr/bin/env python3
"""Compile two existing witnesses and optionally replay a recorded VCS simulator.

This records functional observations; it does not prove timing or establish the
cached simulator's build lineage. No Chipyard Make invocation is needed.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time

from rtlgraph_s0 import FORBIDDEN, artifact, checked_path, timestamp


TARGET = "chipyard.harness.TestHarness.EE290SimConfig"
MXU_TEST = "atlas_mxu1_single_input_tile_bf16"
HOST_TEST = "saturn_atlas_vmem_smoke"
PASS_MARKERS = {
    MXU_TEST: "*** PASSED *** (mxu1_single_input_tile_bf16",
    HOST_TEST: "PASS: VMEM direct-access smoke",
}


def classify(returncode: int, timed_out: bool, log: str, marker: str, *, require_mxu_completion: bool = False) -> str:
    if timed_out:
        return "TIMEOUT"
    if returncode != 0:
        return "PROCESS_FAILED"
    if re.search(r"\*\*\* FAILED \*\*\*|\bFAIL:|Assertion failed|Error-\[|Fatal-\[", log):
        return "CHECK_FAILED"
    if marker not in log:
        return "MISSING_PASS_MARKER"
    if require_mxu_completion:
        dbg0 = re.findall(r"\bDBG0\s*=\s*(0x[0-9a-fA-F]+|[0-9]+)", log)
        status = re.findall(r"\bstatus\s*=\s*(0x[0-9a-fA-F]+|[0-9]+)", log)
        if not dbg0 or not status or int(dbg0[-1], 0) != 1 or int(status[-1], 0) & 7 != 5:
            return "COMPLETION_CHECK_FAILED"
    return "PASS"


def run_command(argv: list[str], cwd: Path, env: dict, log: Path, timeout: float) -> dict:
    started = time.monotonic()
    record = {"argv": argv, "cwd": str(cwd), "started_utc": timestamp(), "timed_out": False, "interrupted": False}
    with log.open("w") as stream:
        process = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                                   stdout=stream, stderr=subprocess.STDOUT, start_new_session=True)
        try:
            process.wait(timeout=timeout)
        except (subprocess.TimeoutExpired, KeyboardInterrupt) as error:
            record["timed_out"] = isinstance(error, subprocess.TimeoutExpired)
            record["interrupted"] = isinstance(error, KeyboardInterrupt)
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
    record.update(returncode=process.returncode, elapsed_seconds=round(time.monotonic() - started, 3),
                  log=artifact(log))
    return record


def copy_inputs(source: Path, destination: Path, *, skip_testdata: bool = False) -> list[dict]:
    """Stage only audited files, rejecting symlinks and pruning before descent."""
    records = []
    source = checked_path(source)
    if not source.is_dir():
        raise ValueError("Runtime input must be a directory")
    pending = [source]
    while pending:
        directory = pending.pop()
        relative = Path(directory).relative_to(source)
        target = checked_path(destination / relative)
        target.mkdir(parents=True, exist_ok=True)
        with os.scandir(directory) as entries:
            for entry in sorted(entries, key=lambda item: item.name):
                name = entry.name
                if any(word in name.lower() for word in FORBIDDEN):
                    raise ValueError("Runtime inputs contain a prohibited name")
                if skip_testdata and relative / name == Path("snps/coverage/db/testdata"):
                    continue
                if entry.is_symlink():
                    raise ValueError("Runtime symlinks require separate review")
                path = checked_path(directory / name)
                if entry.is_dir(follow_symlinks=False):
                    pending.append(path)
                    continue
                if not entry.is_file(follow_symlinks=False):
                    raise ValueError("Runtime inputs must be regular files or directories")
                output = checked_path(target / name)
                shutil.copy2(path, output)
                info = artifact(path)
                if artifact(output)["sha256"] != info["sha256"]:
                    raise ValueError(f"Runtime copy changed: {path.name}")
                records.append({"relative_path": str(relative / name), **info})
    return records


def compile_commands(gcc: Path, source: Path, output: Path, *, vector: bool) -> list[list[str]]:
    obj = output.with_suffix(".o")
    arch = ["-march=rv64gcv_zfh_zvfh" if vector else "-march=rv64imafd", "-mabi=lp64d", "-mcmodel=medany"]
    return [
        [str(gcc), "-g", "-std=gnu99", "-O2", "-Wall", "-Wextra", "-fno-common",
         "-fno-builtin-printf", *arch, "-specs=htif_nano.specs", "-MD", "-MF",
         str(output.with_suffix(".d")), "-c", str(source), "-o", str(obj)],
        [str(gcc), "-g", "-static", "-specs=htif_nano.specs", "-T", "htif.ld", *arch,
         str(obj), f"-Wl,-Map={output.with_suffix('.map')}", "-o", str(output)],
    ]


def simulator_command(simulator: Path, binary: Path, ini: Path, coverage: Path, name: str) -> list[str]:
    # FESVR receives simulator flags too; surround every VCS-only flag with
    # +permissive, including -no_save (htif.cc rejects unknown options otherwise).
    return [str(simulator), "+permissive", "-no_save", "+dramsim", f"+dramsim_ini_dir={ini}",
            "+max-cycles=70000000", "+ntb_random_seed=1", f"+loadmem={binary}",
            "-cm", "line+cond+fsm+branch+tgl+assert", "-cm_dir", str(coverage), "-cm_name", name,
            "+permissive-off", str(binary)]


def dependencies(path: Path) -> list[dict]:
    text = path.read_text().replace("\\\n", " ")
    _, separator, body = text.partition(":")
    if not separator:
        raise ValueError("Malformed compiler dependency file")
    return [artifact(checked_path(value)) for value in sorted(set(shlex.split(body)))]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--chipyard-root", type=Path, required=True, help="Existing initialized Chipyard checkout, read-only")
    parser.add_argument("--fresh-fir", type=Path, required=True, help="Fresh S0 FIRRTL for comparison with saved simulator FIRRTL")
    parser.add_argument("--output", type=Path, required=True, help="New output directory; must not exist")
    parser.add_argument("--run", action="store_true", help="Run both witnesses after staging and compilation")
    parser.add_argument("--timeout-seconds", type=float, default=300)
    args = parser.parse_args(argv)
    root, output, fresh_fir = map(checked_path, (args.chipyard_root, args.output, args.fresh_fir))
    if args.timeout_seconds <= 0:
        parser.error("--timeout-seconds must be positive")
    if output == root or root in output.parents:
        parser.error("Output must be outside the read-only Chipyard source checkout")
    output.mkdir(parents=True, exist_ok=False)
    manifest = output / "manifest.json"
    record = {"schema_version": 1, "kind": "rtlgraph-functional-smoke", "status": "preparing",
              "config": "EE290SimConfig", "started_utc": timestamp(), "commands": [], "tests": {},
              "scope": "Two finite functional observations; no extracted timing validation or universal safety claim",
              "simulator_build_lineage": "UNVERIFIED: cached binary and adjacent build metadata; no recorded source-to-binary build"}

    def save() -> None:
        manifest.write_text(json.dumps(record, indent=2) + "\n")

    def command(argv: list[str], log: Path, *, cwd: Path = output) -> dict:
        result = run_command(argv, cwd, env, log, args.timeout_seconds)
        record["commands"].append(result)
        save()
        if result["interrupted"]:
            raise KeyboardInterrupt
        return result

    try:
        baremetal = checked_path(root / "generators/sp26-atlas-acc/baremetal")
        tools = checked_path(root / ".conda-env/riscv-tools")
        gcc = checked_path(tools / "bin/riscv64-unknown-elf-gcc")
        simulator = checked_path(root / "sims/vcs/simv-chipyard.harness-EE290SimConfig")
        generated = checked_path(root / "sims/vcs/generated-src" / TARGET)
        saved_fir = checked_path(generated / f"{TARGET}.fir")
        assembly = checked_path(baremetal / "assembly/mxu1_single_input_tile_bf16.S")
        golden = checked_path(baremetal / "generators/mxu1_single_input_tile_bf16.json")
        assembler = checked_path(baremetal / "assembler.py")
        host_source = checked_path(root / "tests/saturn_atlas_vmem_smoke.c")
        record["inputs"] = {name: artifact(path) for name, path in {
            "driver": checked_path(__file__),
            "assembly": assembly, "golden_fixture": golden, "assembler": assembler,
            "host_source": host_source, "host_header": checked_path(root / "tests/atlas_mmio.h"),
            "gcc": gcc, "simulator": simulator, "saved_fir": saved_fir, "fresh_fir": fresh_fir,
            "vcs_build_command": checked_path(Path(str(simulator) + ".daidir/build_db")),
        }.items()}
        record["saved_fir_matches_fresh_fir"] = record["inputs"]["saved_fir"]["sha256"] == record["inputs"]["fresh_fir"]["sha256"]
        record["golden_fixture_scope"] = "Existing offset-based fixture reused; its generator was not rerun or independently validated"
        env = dict(os.environ)
        env["PATH"] = str(tools / "bin") + os.pathsep + env.get("PATH", "")
        env["LD_LIBRARY_PATH"] = os.pathsep.join([str(tools / "lib"), str(checked_path(root / ".conda-env/lib")), env.get("LD_LIBRARY_PATH", "")]).rstrip(os.pathsep)
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        record["environment"] = {key: env[key] for key in ("PATH", "LD_LIBRARY_PATH", "PYTHONDONTWRITEBYTECODE")}
        record["license_environment_present"] = bool(env.get("SNPSLMD_LICENSE_FILE") or env.get("LM_LICENSE_FILE"))
        record["toolchain_link_inputs"] = {}
        for name in ("htif_nano.specs", "htif.ld", "libgloss_htif.a", "libc_nano.a", "libgcc.a"):
            result = command([str(gcc), f"-print-file-name={name}"], output / f"gcc-{name}.log")
            if result["returncode"] != 0 or result["timed_out"]:
                raise RuntimeError(f"Could not resolve toolchain input {name}")
            path = checked_path(Path(result["log"]["path"]).read_text().strip())
            record["toolchain_link_inputs"][name] = artifact(path)
        command([str(gcc), "--version"], output / "gcc-version.log")
        generated_c = output / f"{MXU_TEST}.c"
        result = command([sys.executable, str(assembler), str(assembly), "--golden-json", str(golden),
                          "--out-c", str(generated_c)], output / "assemble.log")
        if result["returncode"] != 0 or result["timed_out"]:
            raise RuntimeError("Assembly failed; see assemble.log")
        record["generated_c"] = artifact(generated_c)
        for name, source, vector in ((MXU_TEST, generated_c, False), (HOST_TEST, host_source, True)):
            binary = output / f"{name}.riscv"
            for index, argv in enumerate(compile_commands(gcc, source, binary, vector=vector)):
                result = command(argv, output / f"{name}-build-{index}.log")
                if result["returncode"] != 0 or result["timed_out"]:
                    raise RuntimeError(f"Compilation/link failed for {name}")
            record["tests"][name] = {"status": "PREPARED", "binary": artifact(binary),
                                     "compiler_dependencies": dependencies(binary.with_suffix(".d")),
                                     "required_pass_marker": PASS_MARKERS[name]}
        runtime = output / "runtime"
        runtime.mkdir()
        staged_sim = runtime / simulator.name
        shutil.copy2(simulator, staged_sim)
        if artifact(staged_sim)["sha256"] != record["inputs"]["simulator"]["sha256"]:
            raise RuntimeError("Simulator changed during copying")
        record["runtime_files"] = copy_inputs(checked_path(Path(str(simulator) + ".daidir")), Path(str(staged_sim) + ".daidir"))
        coverage_template = checked_path(generated / "coverage/saturn_atlas_vmem_smoke.vdb")
        record["coverage_design_inputs"] = copy_inputs(coverage_template, runtime / "coverage-template.vdb", skip_testdata=True)
        ini = checked_path(root / "generators/testchipip/src/main/resources/dramsim2_ini")
        record["dramsim_inputs"] = copy_inputs(ini, runtime / "dramsim2_ini")
        record["runtime_libraries"] = {name: artifact(checked_path(tools / "lib" / name)) for name in ("libriscv.so", "libdramsim.so")}
        record["status"] = "prepared"
        save()
        if args.run:
            for name, test in record["tests"].items():
                cwd = output / name
                cwd.mkdir()
                coverage = cwd / "coverage.vdb"
                copy_inputs(runtime / "coverage-template.vdb", coverage)
                argv = simulator_command(staged_sim, Path(test["binary"]["path"]), runtime / "dramsim2_ini", coverage, name)
                result = command(argv, cwd / "simulation.log", cwd=cwd)
                test["run"] = result
                test["status"] = classify(result["returncode"], result["timed_out"], Path(result["log"]["path"]).read_text(errors="replace"), PASS_MARKERS[name], require_mxu_completion=name == MXU_TEST)
                save()
            record["status"] = "passed" if all(t["status"] == "PASS" for t in record["tests"].values()) else "failed"
        record["finished_utc"] = timestamp()
        save()
        print(json.dumps({"status": record["status"], "manifest": str(manifest),
                          "saved_fir_matches_fresh_fir": record["saved_fir_matches_fresh_fir"],
                          "tests": {name: test["status"] for name, test in record["tests"].items()}}, indent=2))
        return 1 if record["status"] == "failed" else 0
    except KeyboardInterrupt:
        record.update(status="interrupted", finished_utc=timestamp())
        save()
        print(f"Smoke preparation/replay interrupted; see {manifest}", file=sys.stderr)
        return 130
    except Exception as error:
        record.update(status="failed", error=str(error), finished_utc=timestamp())
        save()
        print(f"Smoke preparation/replay failed: {error}; see {manifest}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

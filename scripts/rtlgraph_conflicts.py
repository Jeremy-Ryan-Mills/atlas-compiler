#!/usr/bin/env python3
"""Generate narrowly controlled K64 physical-MREG-bank witnesses and a repair.

The positive/negative expectations are hypotheses to test against actual RTL
requests and functional outputs, not assertions of a scheduling-distance LUT.
No simulator is launched. Original assembly, golden data and hardware are inputs.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

from rtlgraph_kernel import (KernelError, assemble, integer, load_assembler,
                             model_issue_summary, read_text, split_kernel,
                             splice, tokens, translate, verified_source)
from rtlgraph_s0 import artifact, checked_path


# Only this existing K64 instruction subset is admitted. Scalar x6 operands must
# never be mistaken for the logical matrix register m6 being renamed.
MREG_OPERANDS = {"VLOAD": 0, "VSTORE": 0, "VMATPUSH.W.MXU1": 1,
                 "VMATMUL.MXU1": 1, "VMATMUL.ACC.MXU1": 1, "VMATPOP.FP8.MXU1": 0}
SCALAR_OPS = {"ADDI", "CSRR", "CSRW", "DELAY", "DMA.CONFIG", "DMA.LOAD",
              "DMA.STORE", "DMA.WAIT", "ECALL", "LI", "SELI", "SUB"}
CASES = (("alias_safe", 32, 32, False), ("alias_invalid", 32, 31, True),
         ("bank_control", 33, 31, False))


def require(condition, message):
    if not condition:
        raise KernelError(message)


def rename_operand(line: str, old: int, new: int) -> tuple[str, bool]:
    parts = tokens(line)
    if not parts:
        return line, False
    op = parts[0].upper()
    require(op in MREG_OPERANDS or op in SCALAR_OPS, f"unsupported full-kernel instruction: {op}")
    index = MREG_OPERANDS.get(op)
    if index is None:
        return line, False
    require(len(parts) > index + 1, f"missing MREG operand: {line.strip()}")
    if integer(parts[index + 1], 63) != old:
        return line, False
    parts[index + 1] = str(new)
    indent = line[:len(line) - len(line.lstrip())]
    comment = (" #" + line.split("#", 1)[1].rstrip("\r\n")) if "#" in line else ""
    return indent + parts[0] + " " + ", ".join(parts[1:]) + comment + "\n", True


def witness_source(source: str, destination: int, issue_gap: int, assembler) -> tuple[str, dict]:
    require(destination in (32, 33), "witness supports only m32 alias and m33 bank control")
    require(issue_gap in (31, 32), "witness supports only the 31/32-cycle boundary pair")
    sliced, _ = verified_source(source, assembler)
    original_body = model_issue_summary(sliced.body)["operations"]
    require(len(original_body) == 16, "expected the 16-operation K64 measured body")
    lines = source.splitlines(keepends=True)
    refs = []
    renamed = []
    changed = []
    for number, line in enumerate(lines):
        parts = tokens(line)
        if parts and parts[0].upper() in MREG_OPERANDS:
            index = MREG_OPERANDS[parts[0].upper()] + 1
            reg = integer(parts[index], 63)
            require(reg != destination, f"destination m{destination} is already used")
            if reg == 6:
                refs.append((number, parts[0].upper()))
        replaced, did_change = rename_operand(line, 6, destination)
        renamed.append(replaced)
        if did_change:
            changed.append(number + 1)
    require([op for _, op in refs] == ["VLOAD", "VMATPUSH.W.MXU1"],
            "m6 must have exactly one setup load and one measured weight-push use")
    require(refs[0][0] < sliced.begin_line - 1 < refs[1][0] < sliced.end_line - 1,
            "expected setup load before timing and weight push inside timing")
    body_instructions = [(i, tokens(lines[i])) for i in range(sliced.begin_line, sliced.end_line - 1)
                         if tokens(lines[i])]
    computes = [(i, p) for i, p in body_instructions if p[0].upper() == "VMATMUL.MXU1"]
    require(computes and [p.upper() for p in computes[0][1]] == ["VMATMUL.MXU1", "0", "0", "0"],
            "first compute must read m0 with accumulator0/weight0")
    compute_line = computes[0][0]
    following = next((p for i, p in body_instructions if i > compute_line), None)
    require(following and [p.upper() for p in following] == ["VMATPUSH.W.MXU1", "1", "6"],
            "first compute must be immediately followed by the B10 weight1 push")

    # First prove renaming can be reversed without changing any instruction
    # word. Then check that only the two intended register bitfields changed.
    renamed_text = "".join(renamed)
    reversed_text = "".join(rename_operand(line, destination, 6)[0] for line in renamed)
    original_words = assemble(assembler, source)
    require(assemble(assembler, reversed_text) == original_words, "reverse-rename encoding mismatch")
    renamed_words = assemble(assembler, renamed_text)
    require(len(renamed_words) == len(original_words), "renaming changed instruction count")
    differences = [(i, a, b) for i, (a, b) in enumerate(zip(original_words, renamed_words)) if a != b]
    require(len(differences) == 2, "renaming must alter exactly two instruction words")
    for (_, before, after), mask in zip(differences, (63 << 7, 63 << 13)):
        require((before ^ after) & ~mask == 0, "renaming altered bits outside a MREG operand")

    renamed.insert(compute_line + 1, f"    DELAY {issue_gap - 2} # controlled first-compute to B10-push gap\n")
    banner = (f"# Generated bank witness: B10 moved from m6 to m{destination}; first compute-to-push gap {issue_gap}.\n"
              "# Original register-map and timing comments below are historical.\n")
    result = banner + "".join(renamed)
    new_slice, _ = verified_source(result, assembler)
    ops = model_issue_summary(new_slice.body)["operations"]
    first = next(op for op in ops if tokens(op["instruction"])[0].upper() == "VMATMUL.MXU1")
    push = next(op for op in ops if tokens(op["instruction"]) == ["VMATPUSH.W.MXU1", "1", str(destination)])
    require(push["body_issue_cycle"] - first["body_issue_cycle"] == issue_gap, "generated issue gap mismatch")
    require(len(assemble(assembler, result)) == len(original_words) + 1, "expected exactly one inserted DELAY")
    report = {"renamed_register": {"from": 6, "to": destination, "changed_source_lines": changed},
              "reverse_rename_words_equal": True,
              "only_mreg_operand_bits_changed": True,
              "first_compute_body_issue": first["body_issue_cycle"], "push_body_issue": push["body_issue_cycle"],
              "first_compute_to_push_gap": issue_gap,
              "static_issue_summary": model_issue_summary(new_slice.body),
              "encoding_word_count": len(assemble(assembler, result)),
              "expected_bank_conflict": destination == 32 and issue_gap == 31,
              "target_pair": {"compute_mreg": 0, "push_mreg": destination,
                              "compute_bank": 0, "push_bank": destination & 31},
              "expectation_scope": "Source-derived hypothesis. Validate actual simultaneous read requests and independently check functional outputs."}
    if report["expected_bank_conflict"]:
        report["hypothesized_collision"] = {"body_cycle": push["body_issue_cycle"], "bank": 0,
                                             "compute_logical_row": 31, "push_logical_row": 0,
                                             "compute_physical_row": 31, "push_physical_row": 32}
    return result, report


def model_run(executable: Path, source: Path, profile: Path, log: Path, *, output: Path | None = None) -> dict:
    command = [str(executable), str(source), "--experimental-mxu1-profile", str(profile)]
    command += (["--passes", "strip-artifacts,schedule", "--schedule-priority", "input", "-o", str(output)]
                if output else ["--check"])
    result = subprocess.run(command, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)
    log.write_text(result.stdout + result.stderr)
    return {"argv": command, "returncode": result.returncode, "log": artifact(log),
            "stdout": result.stdout, "stderr": result.stderr}


def generate(args) -> dict:
    source_path, assembler_path, golden_path, executable, profile, output = map(checked_path,
        (args.source, args.assembler, args.golden_json, args.atlas_opt, args.profile, args.output))
    require(not output.exists(), "output must be a new directory")
    for path in (source_path, assembler_path, golden_path, executable, profile):
        require(output not in path.parents, "output cannot contain an input")
    source = read_text(source_path)
    assembler = load_assembler(assembler_path)
    golden = json.loads(golden_path.read_text())
    require(isinstance(golden.get("dram_checks"), list) and len(golden["dram_checks"]) == 128,
            "expected existing K64 golden with 128 32-byte output beats")
    output.mkdir(parents=True)
    manifest = {"schema": "atlas.rtlgraph.bank-witnesses.v1", "status": "generating",
                "inputs": {name: artifact(path) for name, path in
                           (("source", source_path), ("assembler", assembler_path), ("golden_fixture", golden_path),
                            ("compiler", executable), ("profile", profile), ("driver", checked_path(__file__)))},
                "runtime_library_path": os.environ.get("LD_LIBRARY_PATH", ""),
                "golden_scope": "Original data/output fixture reused unchanged, not regenerated or independently validated.",
                "independent_obligation": "Compare actual MXU1 P0/P1 MREG request valid/address signals. Distinct architectural registers sharing a bank must not request its single read port simultaneously.",
                "expected_negative_assertion": "MregFile bank conflict: multiple read ports targeting physical bank 0 (m0 or m32)",
                "source_contracts": ["src/main/scala/atlas/common/MregParams.scala#L72-L78",
                                     "src/main/scala/atlas/mreg/MregFile.scala#L200-L217",
                                     "src/main/scala/atlas/mxu/ipt/InnerProductTreesSequencer.scala#L303-L316"],
                "rtl_status": "not_run", "cases": {}}
    manifest_path = output / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
    for name, destination, gap, negative in CASES:
        assembly, report = witness_source(source, destination, gap, assembler)
        path = output / (name + ".S")
        path.write_text(assembly)
        compiler_path = output / (name + ".compiler.S")
        compiler_path.write_text(translate(split_kernel(assembly).body, to_compiler=True))
        check = model_run(executable, compiler_path, profile, output / (name + ".check.log"))
        require(check["returncode"] == int(negative), f"unexpected compiler preflight result for {name}")
        if negative:
            errors = [line for line in check["stderr"].splitlines() if "input:" in line]
            require(errors and all("MREG bank 0 port busy" in line for line in errors),
                    "negative case must fail only for the intended physical-bank read conflict")
        manifest["cases"][name] = {**report, "assembly": artifact(path), "compiler_body": artifact(compiler_path),
                                     "model_check": check, "expected_rtl": "bank_read_conflict" if negative else "functional_pass"}
        manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
    invalid = output / "alias_invalid.compiler.S"
    repaired_body = output / "alias_repaired.compiler.S"
    repair = model_run(executable, invalid, profile, output / "alias_repaired.schedule.log", output=repaired_body)
    require(repair["returncode"] == 0, "compiler repair failed")
    checked = model_run(executable, repaired_body, profile, output / "alias_repaired.check.log")
    require(checked["returncode"] == 0, "repaired body fails its selected model")
    repaired, report = splice(read_text(output / "alias_invalid.S"), read_text(repaired_body), assembler, 33)
    repaired_path = output / "alias_repaired.S"
    repaired_path.write_text(repaired)
    manifest["cases"]["alias_repaired"] = {**report, "assembly": artifact(repaired_path),
        "compiler_body": artifact(repaired_body), "schedule_command": repair, "model_check": checked,
        "expected_rtl": "functional_pass", "expected_bank_conflict": False,
        "target_pair": manifest["cases"]["alias_invalid"]["target_pair"]}
    safe_ops = model_issue_summary(read_text(output / "alias_safe.compiler.S"))
    repaired_ops = model_issue_summary(read_text(repaired_body))
    manifest["safe_and_repaired_have_identical_body_schedule"] = safe_ops == repaired_ops
    for name, before in manifest["inputs"].items():
        require(artifact(Path(before["path"])) == before, f"input changed while generating witnesses: {name}")
    manifest["status"] = "fixtures_and_model_preflight_complete"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
    return manifest


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--assembler", type=Path, required=True)
    parser.add_argument("--golden-json", type=Path, required=True)
    parser.add_argument("--atlas-opt", type=Path, required=True)
    parser.add_argument("--profile", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        report = generate(args)
        print(json.dumps({"status": report["status"], "rtl_status": report["rtl_status"],
                          "manifest": str(args.output / "manifest.json"),
                          "same_safe_repaired_schedule": report["safe_and_repaired_have_identical_body_schedule"]}))
        return 0
    except (OSError, ValueError, subprocess.TimeoutExpired) as error:
        print(f"rtlgraph_conflicts: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

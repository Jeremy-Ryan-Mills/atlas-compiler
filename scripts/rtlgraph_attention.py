#!/usr/bin/env python3
"""Prepare four fixed-operation scheduling experiments for MXU1 fused attention.

This is a narrow baremetal adapter, not a new VPU timing model. A compiler-only
ECALL sentinel forces modeled completion before the unchanged ending cycle CSR.
Every generated schedule still requires independent functional RTL validation.
"""
from __future__ import annotations

import argparse
from collections import Counter
import json
import os
from pathlib import Path
import subprocess
import sys

from rtlgraph_kernel import (KernelError, KernelSlice, assemble, integer,
                            instruction_words, load_assembler, model_issue_summary,
                            read_text, sha, tokens, translate_line)
from rtlgraph_s0 import artifact


EXTRA = {
    "VMATPUSH.ACC.BF16.MXU1": ("vmatpush.acc.bf16.mxu1", ("acc", "m")),
    "VMATPOP.BF16.MXU1": ("vmatpop.bf16.acc.mxu1", ("m", "acc")),
    "VMUL.BF16": ("vmul.bf16", ("m", "m", "m")),
    "VADD.BF16": ("vadd.bf16", ("m", "m", "m")),
    "VSUB.BF16": ("vsub.bf16", ("m", "m", "m")),
    "VMAX.BF16": ("vmaximum.bf16", ("m", "m", "m")),
    "VREDMAX.ROW.BF16": ("vredmax.row.bf16", ("m", "m")),
    "VREDSUM.ROW.BF16": ("vredsum.row.bf16", ("m", "m")),
    "VEXP": ("vexp.bf16", ("m", "m")),
    "VMOV": ("vmov", ("m", "m")),
    "VRECIP.BF16": ("vrecip.bf16", ("m", "m")),
}


def translate(text, *, to_compiler):
    result = []
    for line in text.splitlines():
        parts = tokens(line)
        if not parts:
            continue
        match = next(((bare, native, specs) for bare, (native, specs) in EXTRA.items()
                      if parts[0].upper() == (bare if to_compiler else native.upper())), None)
        if match is None:
            result.append(translate_line(line, to_compiler=to_compiler))
            continue
        bare, native, specs = match
        if len(parts) != len(specs) + 1:
            raise KernelError(f"wrong operand count: {line}")
        values = [integer(value, 1 if kind == "acc" else 63, "" if to_compiler else kind)
                  for value, kind in zip(parts[1:], specs)]
        result.append((native if to_compiler else bare) + " " + ", ".join(
            (kind if to_compiler else "") + str(value) for kind, value in zip(specs, values)))
    if not result:
        raise KernelError("empty attention body")
    return "\n".join(result) + "\n"


def split(source):
    lines, markers = source.splitlines(keepends=True), []
    for i, line in enumerate(lines):
        p = tokens(line)
        if not p:
            continue
        if p[0].upper() in {"AUIPC", "JAL", "JALR", "BEQ", "BNE", "BLT", "BGE", "BLTU", "BGEU"}:
            raise KernelError("PC-relative instructions and control flow are unsupported")
        if p[0].upper() == "CSRRS" and len(p) == 4 and p[2].lower() == "0xc00":
            integer(p[1], 31, "x")
            if p[3].lower() != "x0":
                raise KernelError("cycle marker must be a read-only CSRRS")
            markers.append(i)
    if len(markers) != 2:
        raise KernelError("expected exactly two CSRRS cycle markers")
    a, b = markers
    return KernelSlice("".join(lines[:a+1]), "".join(lines[a+1:b]), lines[b],
                       "".join(lines[b+1:]), a+1, b+1)


def strip_sentinel(scheduled):
    lines = [line for line in scheduled.splitlines() if tokens(line)]
    if not lines or tokens(lines[-1]) != ["ecall"] or sum(tokens(s)[0].lower() == "ecall" for s in lines) != 1:
        raise KernelError("scheduled body must end in its single completion sentinel")
    return "\n".join(lines[:-1]) + "\n"


def splice(source, scheduled, assembler):
    sliced = split(source)
    compiler = translate(sliced.body, to_compiler=True)
    roundtrip = translate(compiler, to_compiler=False)
    if assemble(assembler, source) != assemble(assembler, sliced.prefix + roundtrip + sliced.end_marker + sliced.suffix):
        raise KernelError("original assembler roundtrip mismatch")
    body = translate(strip_sentinel(scheduled), to_compiler=False)
    if Counter(instruction_words(assembler, sliced.body, include_idle=False)) != Counter(instruction_words(assembler, body, include_idle=False)):
        raise KernelError("scheduled body changes encoded non-idle instruction multiset")
    result = sliced.prefix + body + sliced.end_marker + sliced.suffix
    expected = assemble(assembler, sliced.prefix) + assemble(assembler, body) + assemble(assembler, sliced.end_marker + sliced.suffix)
    if assemble(assembler, result) != expected:
        raise KernelError("replacement altered instructions outside the measured body")
    return result, body


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for flag in ("source", "assembler", "atlas-opt", "profile", "output"):
        parser.add_argument("--" + flag, type=Path, required=True)
    args = parser.parse_args(argv)
    source_path, asm_path, opt, profile, output = [p.resolve() for p in
        (args.source, args.assembler, args.atlas_opt, args.profile, args.output)]
    output.mkdir(parents=True, exist_ok=False)
    source = read_text(source_path)
    sliced, assembler = split(source), load_assembler(asm_path)
    compiler = translate(sliced.body, to_compiler=True) + "ecall\n"
    # Check the roundtrip before calling the optimizer.
    splice(source, compiler, assembler)
    prepared = output / "original.compiler.S"
    prepared.write_text(compiler)
    report = {"schema": "atlas.rtlgraph.attention-experiment.v1", "status": "preparing",
              "inputs": {"source": artifact(source_path), "assembler": artifact(asm_path),
                         "compiler": artifact(opt), "profile": artifact(profile), "driver": artifact(Path(__file__).resolve())},
              "original_word_count": len(assemble(assembler, source)),
              "cycle_marker_lines": [sliced.begin_line, sliced.end_line],
              "prefix_sha256": sha(sliced.prefix.encode()), "suffix_sha256": sha(sliced.suffix.encode()),
              "original_issue_summary": model_issue_summary(sliced.body), "cases": {},
              "scope": "Fixed operations/operands and original setup/suffix; no register renaming or algorithm change. VPU and BF16 transfer rules inherited from built-in model.",
              "completion": "Compiler ECALL sentinel is replaced by ending CSR; modeled drain remains inside measured window. No extra unmeasured drain.",
              "validation": "Assembly equivalence and compiler model checks only; RTL execution required."}
    for use_profile in (False, True):
        for priority in ("critical", "input"):
            name = ("profile_" if use_profile else "builtin_") + priority
            scheduled, candidate = output / (name + ".compiler.S"), output / (name + ".S")
            command = [str(opt), str(prepared), "--passes", "strip-artifacts,schedule",
                       "--schedule-priority", priority, "-o", str(scheduled)]
            if use_profile:
                command += ["--experimental-mxu1-profile", str(profile)]
            run = subprocess.run(command, capture_output=True, text=True, env=os.environ.copy())
            log = output / (name + ".log")
            log.write_text(run.stdout + run.stderr)
            if run.returncode:
                raise KernelError(f"compiler rejected {name}; see {log}")
            result, body = splice(source, read_text(scheduled), assembler)
            candidate.write_text(result)
            report["cases"][name] = {"assembly": artifact(candidate), "scheduled": artifact(scheduled),
                                    "command": command, "log": artifact(log), "issue_summary": model_issue_summary(body),
                                    "unchanged_non_idle_words": True, "full_assembly_parts_equal": True}
    report["status"] = "model_candidates_ready"
    (output / "manifest.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({name: case["issue_summary"]["cycle_csr_dispatch_window"] for name, case in report["cases"].items()}))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ValueError) as error:
        raise SystemExit(f"rtlgraph_attention: {error}")

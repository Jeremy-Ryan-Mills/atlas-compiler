#!/usr/bin/env python3
"""Adapt the MXU1 body of a CSRR-bracketed baremetal performance kernel.

This is intentionally not a general Atlas assembler translator. Setup, cycle
CSRs, perf directives, and writeback stay in the source dialect. The assembler
argument names the trusted original baremetal assembler used for encoding
checks. Encoding equivalence and an instruction multiset check do not establish
schedule legality; run each generated variant against its functional golden.
"""

from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
import sys
import types


class KernelError(ValueError):
    pass


def tokens(line: str) -> list[str]:
    return re.split(r"[\s,]+", line.split("#", 1)[0].strip()) if line.split("#", 1)[0].strip() else []


def integer(value: str, maximum: int, prefix: str = "") -> int:
    if prefix:
        if not re.fullmatch(re.escape(prefix) + r"\d+", value.lower()):
            raise KernelError(f"expected {prefix} register, got {value!r}")
        value = value[len(prefix):]
    try:
        result = int(value, 16 if value.lower().startswith("0x") else 10)
    except ValueError as exc:
        raise KernelError(f"invalid integer {value!r}") from exc
    if not 0 <= result <= maximum:
        raise KernelError(f"operand {value!r} outside 0..{maximum}")
    return result


# Source operands are (name, maximum); mapping gives compiler operand order.
# In particular baremetal FP8 POP is destination, scale, accumulator, whereas
# atlas-opt is destination, accumulator, scale.
OPS = {
    "VMATPUSH.W.MXU1": ("vmatpush.weight.mxu1", (("w", 1), ("m", 63)), (0, 1)),
    "VMATMUL.MXU1": ("vmatmul.mxu1", (("acc", 1), ("m", 63), ("w", 1)), (0, 1, 2)),
    "VMATMUL.ACC.MXU1": ("vmatmul.acc.mxu1", (("acc", 1), ("m", 63), ("w", 1)), (0, 1, 2)),
    "VMATPOP.FP8.MXU1": ("vmatpop.fp8.acc.mxu1", (("m", 63), ("e", 31), ("acc", 1)), (0, 2, 1)),
}


def translate_line(line: str, *, to_compiler: bool) -> str | None:
    parts = tokens(line)
    if not parts:
        return None
    name, operands = parts[0].upper(), parts[1:]
    if name == "DELAY":
        if len(operands) != 1:
            raise KernelError("DELAY requires exactly one operand")
        return f"{'delay' if to_compiler else 'DELAY'} {integer(operands[0], 4095)}"
    if name == "NOP" or (not to_compiler and [p.lower() for p in parts] == ["addi", "x0", "x0", "0"]):
        if name == "NOP" and operands:
            raise KernelError("NOP takes no operands")
        return "nop" if to_compiler else "NOP"
    for source_name, (compiler_name, specs, order) in OPS.items():
        if name != (source_name if to_compiler else compiler_name.upper()):
            continue
        if len(operands) != len(specs):
            raise KernelError(f"{name} requires {len(specs)} operands")
        if to_compiler:
            vals = [integer(value, limit) for value, (_, limit) in zip(operands, specs)]
            return compiler_name + " " + ", ".join(specs[i][0] + str(vals[i]) for i in order)
        vals = [0] * len(specs)
        for value, i in zip(operands, order):
            prefix, limit = specs[i]
            vals[i] = integer(value, limit, prefix)
        return source_name + " " + ", ".join(map(str, vals))
    raise KernelError(f"unsupported instruction or label in measured body: {parts[0]}")


def translate(text: str, *, to_compiler: bool) -> str:
    result = []
    for number, line in enumerate(text.splitlines(), 1):
        try:
            converted = translate_line(line, to_compiler=to_compiler)
        except KernelError as exc:
            raise KernelError(f"body line {number}: {exc}") from exc
        if converted:
            result.append(converted)
    if not result:
        raise KernelError("empty measured body")
    return "\n".join(result) + "\n"


@dataclass(frozen=True)
class KernelSlice:
    prefix: str  # Includes starting CSRR line.
    body: str
    end_marker: str
    suffix: str
    begin_line: int
    end_line: int


def split_kernel(source: str) -> KernelSlice:
    lines = source.splitlines(keepends=True)
    markers = []
    for i, line in enumerate(lines):
        parts = tokens(line)
        if not parts:
            continue
        op = parts[0].upper()
        # Changing body length must not silently change address-relative code.
        if op in {"AUIPC", "JAL", "JALR", "BEQ", "BNE", "BLT", "BGE", "BLTU", "BGEU"} or ":" in parts[0]:
            raise KernelError("control flow, labels, and PC-relative instructions are outside this adapter's scope")
        if op == "CSRR" and len(parts) == 3 and integer(parts[2], 4095) == 0xC00:
            integer(parts[1], 31, "x")
            markers.append(i)
    if len(markers) != 2:
        raise KernelError(f"expected exactly two CSRR cycle markers, found {len(markers)}")
    begin, end = markers
    return KernelSlice("".join(lines[:begin + 1]), "".join(lines[begin + 1:end]),
                       lines[end], "".join(lines[end + 1:]), begin + 1, end + 1)


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def read_text(path: Path) -> str:
    # Preserve line endings and every byte of the prefix/suffix.
    return path.read_bytes().decode("utf-8")


def load_assembler(path: Path):
    module = types.ModuleType("rtlgraph_original_baremetal_assembler")
    module.__file__ = str(path)
    exec(compile(path.read_bytes(), str(path), "exec"), module.__dict__)
    if not callable(getattr(module, "assemble", None)):
        raise KernelError("assembler does not expose assemble(source)")
    return module


def assemble(assembler, source: str) -> list[int]:
    words = assembler.assemble(source)
    if not isinstance(words, list) or any(type(word) is not int or not 0 <= word <= 0xFFFFFFFF for word in words):
        raise KernelError("assembler did not return a list of 32-bit instruction words")
    return words


def instruction_words(assembler, body: str, *, include_idle: bool = True) -> list[int]:
    lines = [line for line in body.splitlines() if tokens(line)]
    if not include_idle:
        lines = [line for line in lines if tokens(line)[0].upper() not in {"DELAY", "NOP"}]
    return assemble(assembler, "\n".join(lines))


def verified_source(source: str, assembler) -> tuple[KernelSlice, str]:
    sliced = split_kernel(source)
    compiler = translate(sliced.body, to_compiler=True)
    roundtrip = translate(compiler, to_compiler=False)
    rebuilt = sliced.prefix + roundtrip + sliced.end_marker + sliced.suffix
    if assemble(assembler, source) != assemble(assembler, rebuilt):
        raise KernelError("original assembler roundtrip encoding mismatch")
    return sliced, compiler


def model_issue_summary(body: str) -> dict:
    cycle, operations = 0, []
    for line in body.splitlines():
        parts = tokens(line)
        if not parts:
            continue
        if parts[0].upper() == "DELAY":
            cycle += 1 + integer(parts[1], 4095)
        else:
            if parts[0].upper() != "NOP":
                operations.append({"body_issue_cycle": cycle, "instruction": line.strip()})
            cycle += 1
    return {"basis": "static scalar issue count; DELAY N occupies N+1 cycles; not an RTL measurement",
            "cycle_csr_dispatch_window": cycle + 1, "operations": operations}


def serialized_body(compiler: str, gap: int) -> str:
    if not 1 <= gap <= 4097:
        raise KernelError("serialized issue gap must be in 1..4097")
    lines = [line for line in compiler.splitlines() if tokens(line)[0].upper() not in {"DELAY", "NOP"}]
    separator = "\n" if gap == 1 else f"\ndelay {gap - 2}\n"
    return separator.join(lines) + "\n"


def splice(source: str, scheduled: str, assembler, drain_cycles: int) -> tuple[str, dict]:
    if not 0 <= drain_cycles <= 4096:
        raise KernelError("unmeasured drain must be in 0..4096 cycles")
    sliced, _ = verified_source(source, assembler)
    body = translate(scheduled, to_compiler=False)
    original_ops = instruction_words(assembler, sliced.body, include_idle=False)
    proposed_ops = instruction_words(assembler, body, include_idle=False)
    if Counter(original_ops) != Counter(proposed_ops):
        raise KernelError("replacement changes the non-idle encoded instruction multiset")
    note = "# Generated measured body; original header timing commentary is historical.\n"
    drain = (f"# Unmeasured candidate drain: {drain_cycles} idle issue cycles; validate on RTL.\n"
             f"DELAY {drain_cycles - 1}\n") if drain_cycles else ""
    result = sliced.prefix + note + body + sliced.end_marker + drain + sliced.suffix
    expected = (assemble(assembler, sliced.prefix) + instruction_words(assembler, body) +
                assemble(assembler, sliced.end_marker + drain + sliced.suffix))
    if assemble(assembler, result) != expected:
        raise KernelError("full-kernel assembly differs from verified pieces")
    report = {"unchanged_non_idle_instruction_multiset": True,
              "original_issue_summary": model_issue_summary(sliced.body),
              "replacement_issue_summary": model_issue_summary(body),
              "unmeasured_drain_cycles": drain_cycles,
              "unmeasured_drain_encoding": f"DELAY {drain_cycles - 1}" if drain_cycles else None,
              "schedule_legality": "unverified by adapter; requires functional RTL execution and timing checks",
              "boundary_assumptions": "entry inputs ready; added drain is a candidate, not a proof of completion"}
    return result, report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("prepare", "splice", "serialize"))
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--assembler", type=Path, required=True, help="trusted original baremetal/assembler.py")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--scheduled", type=Path, help="atlas-opt output, required by splice")
    parser.add_argument("--gap", type=int, help="serialized issue gap, required by serialize")
    parser.add_argument("--drain-cycles", type=int, default=33,
                        help="idle issue cycles after ending CSRR for splice/serialize (default: 33)")
    parser.add_argument("--manifest", type=Path, help="default: OUTPUT.json")
    args = parser.parse_args(argv)
    try:
        source_path, assembler_path = args.source.resolve(), args.assembler.resolve()
        output_path = args.output.resolve()
        manifest_path = (args.manifest or Path(str(args.output) + ".json")).resolve()
        inputs = {source_path, assembler_path}
        if args.scheduled:
            inputs.add(args.scheduled.resolve())
        if output_path in inputs or manifest_path in inputs or output_path == manifest_path:
            raise KernelError("outputs must not overwrite an input or each other")
        source = read_text(source_path)
        assembler = load_assembler(assembler_path)
        sliced, compiler = verified_source(source, assembler)
        report = {
            "schema": "atlas.rtlgraph.kernel-adapter.v1", "mode": args.mode,
            "source": {"path": str(source_path), "sha256": sha(source.encode())},
            "assembler": {"path": str(assembler_path), "sha256": sha(assembler_path.read_bytes())},
            "cycle_marker_lines": [sliced.begin_line, sliced.end_line],
            "prefix_sha256": sha(sliced.prefix.encode()), "suffix_sha256": sha(sliced.suffix.encode()),
            "original_assembler_roundtrip_words_equal": True,
            "original_word_count": len(assemble(assembler, source)),
            "scope": "MXU1 weight push, matmul, accumulating matmul, FP8 pop, delays and no-ops between cycle CSRs",
        }
        if args.mode == "prepare":
            if args.scheduled or args.gap is not None:
                raise KernelError("prepare does not accept --scheduled or --gap")
            result = compiler
            report["issue_summary"] = model_issue_summary(sliced.body)
        else:
            if args.mode == "splice":
                if args.scheduled is None or args.gap is not None:
                    raise KernelError("splice requires --scheduled and does not accept --gap")
                scheduled = read_text(args.scheduled)
                report["scheduled"] = {"path": str(args.scheduled.resolve()), "sha256": sha(scheduled.encode())}
            else:
                if args.gap is None or args.scheduled:
                    raise KernelError("serialize requires --gap and does not accept --scheduled")
                scheduled = serialized_body(compiler, args.gap)
                report["serialized_issue_gap"] = args.gap
            result, details = splice(source, scheduled, assembler, args.drain_cycles)
            report.update(details)
            report["output_word_count"] = len(assemble(assembler, result))
        report["output"] = {"path": str(output_path), "sha256": sha(result.encode())}
        output_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(result.encode())
        manifest_path.write_text(json.dumps(report, indent=2) + "\n")
        print(f"wrote {output_path}; encoding/provenance report: {manifest_path}")
        return 0
    except (OSError, ValueError, TypeError) as exc:
        print(f"rtlgraph_kernel: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

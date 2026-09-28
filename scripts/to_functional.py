#!/usr/bin/env python3
"""Turns hand-scheduled (executable) Atlas assembly into functional assembly.

Functional assembly has no `delay`s, no `dma.wait`s and no branch delay slots;
atlas-opt adds all three. Use this to turn a hand-scheduled kernel into atlas-opt
input:

    scripts/to_functional.py kernel.S -o kernel.fs.S

Branch delay slots must hold a nop (or a delay), as in npu_model's kernels. A real
instruction in a slot runs on both paths in executable assembly but only on the
fall-through path in functional assembly, so such files are rejected.
"""

import argparse
import re
import sys

HEADER = "# atlas-fs 0\n"

# A `delay` or `dma.wait` instruction, optionally after a label on the same line.
DELAY_LINE = re.compile(r"^(\s*[A-Za-z_][\w.]*:)?\s*delay\b.*$", re.IGNORECASE)
WAIT_LINE = re.compile(r"^(\s*[A-Za-z_][\w.]*:)?\s*dma\.wait\.ch[0-7]\b.*$", re.IGNORECASE)
BRANCH_LINE = re.compile(r"^\s*(beq|bne|blt|bge|bltu|bgeu|jal|jalr)\b", re.IGNORECASE)
NOP_LINE = re.compile(r"^\s*(nop|addi\s+x0\s*,\s*x0\s*,\s*0)\s*(#.*)?$", re.IGNORECASE)
HEADER_LINE = re.compile(r"^\s*#\s*atlas-(fs|es)\s+\d+\s*$", re.IGNORECASE)


def check_delay_slots(text: str) -> None:
    """Raises ValueError if a branch's delay slot holds something other than a nop or delay."""
    branch_line = None
    for number, line in enumerate(text.splitlines(), 1):
        code = line.split("#")[0].strip()
        if not code or code.endswith(":"):  # blank line, comment or label
            continue
        if branch_line is not None and not (NOP_LINE.match(line) or DELAY_LINE.match(line)):
            raise ValueError(
                f"line {number}: `{code}` is in the delay slot of the branch on line {branch_line}; "
                "functional assembly would run it only when the branch is not taken. "
                "Move it before the branch and put a nop in the slot."
            )
        branch_line = number if BRANCH_LINE.match(line) else None


def _drop(text: str, pattern: re.Pattern) -> str:
    """Removes lines matching `pattern`, keeping a label that shared the line."""
    out = []
    for line in text.splitlines(keepends=True):
        match = pattern.match(line)
        if match is None:
            out.append(line)
        elif match.group(1):
            out.append(match.group(1).strip() + "\n")
    return "".join(out)


def strip_delays(text: str) -> str:
    """Returns `text` without its delays. Labels and all other lines are kept."""
    check_delay_slots(text)
    return _drop(text, DELAY_LINE)


def to_functional(text: str) -> str:
    """Returns `text` as functional assembly: no delays or waits, with the version header."""
    body = _drop(strip_delays(text), WAIT_LINE)
    lines = body.splitlines(keepends=True)
    if lines and HEADER_LINE.match(lines[0]):
        lines = lines[1:]
    return HEADER + "".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("input", help="assembly file")
    parser.add_argument("-o", "--output", help="where to write the result (default: stdout)")
    args = parser.parse_args()
    with open(args.input) as f:
        try:
            text = to_functional(f.read())
        except ValueError as error:
            sys.exit(f"{args.input}: {error}")
    if args.output:
        with open(args.output, "w") as f:
            f.write(text)
    else:
        sys.stdout.write(text)


if __name__ == "__main__":
    main()

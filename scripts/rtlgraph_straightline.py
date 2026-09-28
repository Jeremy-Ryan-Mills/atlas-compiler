#!/usr/bin/env python3
"""Preserve a branch-free executed prefix while removing unreachable halt tails."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import struct

from rtlgraph_kernel import KernelError, assemble, load_assembler, tokens
from rtlgraph_s0 import artifact


def require(condition, message):
    if not condition:
        raise KernelError(message)


def word_hash(words):
    return hashlib.sha256(b''.join(struct.pack('<I', word) for word in words)).hexdigest()


def normalize(source, assembler, *, add_perf_report=True):
    """Return source and evidence for exactly the original prefix through ECALL.

    Labels may only occupy their own line. No reachable branch, jump or
    PC-relative instruction is admitted, so no label can redirect execution.
    DELAY's custom JALR-shaped encoding is explicitly distinguished from JALR.
    """
    original = assemble(assembler, source)
    require(0x73 in original, 'source has no ECALL')
    halt = original.index(0x73)
    reachable = original[:halt + 1]
    for word in reachable:
        opcode = word & 0x7f
        require(opcode not in (0x17, 0x63, 0x6f) and
                (opcode != 0x67 or word & 0xfffff == 0x1067),
                'reachable branch, jump, or PC-relative instruction is unsupported')
    lines, removed_labels, halt_line = [], [], None
    for index, line in enumerate(source.splitlines(), 1):
        code = line.split('#', 1)[0].strip()
        if re.fullmatch(r'[A-Za-z_][A-Za-z_0-9.]*:', code):
            require('atlas.release' not in line.split('#', 1)[-1].split(),
                    'atlas.release cannot annotate a removed label')
            removed_labels.append(index)
            continue
        require(':' not in code, 'only label-only lines may be removed')
        lines.append(line)
        if tokens(line) and tokens(line)[0].upper() == 'ECALL':
            require(len(tokens(line)) == 1, 'ECALL must have no operands')
            halt_line = index
            break
    require(halt_line is not None, 'encoded halt has no source ECALL boundary')
    perf_present = any(re.fullmatch(r'\s*#\s*@PERF_REPORT\s*', line) for line in lines)
    if add_perf_report and not perf_present:
        lines.insert(0, '# @PERF_REPORT')
    result = '\n'.join(lines) + '\n'
    normalized = assemble(assembler, result)
    require(normalized == reachable, 'normalization changed reachable encoded instruction words')
    return result, dict(original_word_count=len(original), reachable_word_count=len(reachable),
                        unreachable_word_count=len(original) - len(reachable),
                        first_ecall_source_line=halt_line, removed_label_source_lines=removed_labels,
                        perf_report_added=bool(add_perf_report and not perf_present),
                        original_words_sha256=word_hash(original), reachable_words_sha256=word_hash(reachable),
                        normalized_words_sha256=word_hash(normalized),
                        proof='Exact encoded prefix equality through first ECALL; reachable branch/jump/PC-relative encodings rejected.',
                        assumptions=['Execution starts at the first instruction and ECALL halts the Atlas core.',
                                     'Instruction memory is not modified during execution.',
                                     'PERF_REPORT changes host diagnostics, not Atlas instruction words.'])


def prepare(source_path, assembler_path, output):
    source, assembler = source_path.read_text(), load_assembler(assembler_path)
    normalized, facts = normalize(source, assembler)
    output.mkdir(parents=True, exist_ok=False)
    result = output / source_path.name
    result.write_text(normalized)
    report = dict(schema='atlas.rtlgraph.straightline-normalization.v1', status='encoded_prefix_preserved',
                  inputs=dict(source=artifact(source_path), assembler=artifact(assembler_path),
                              driver=artifact(Path(__file__).resolve())),
                  assembly=artifact(result), normalization=facts)
    (output / 'manifest.json').write_text(json.dumps(report, indent=2) + '\n')
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for field in ('source', 'assembler', 'output'):
        parser.add_argument('--' + field, type=Path, required=True)
    args = parser.parse_args()
    report = prepare(args.source.resolve(), args.assembler.resolve(), args.output.resolve())
    print(json.dumps(dict(status=report['status'], assembly=report['assembly']['path'],
                         reachable_word_count=report['normalization']['reachable_word_count'])))


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, KeyError) as error:
        raise SystemExit(f'rtlgraph_straightline: {error}')

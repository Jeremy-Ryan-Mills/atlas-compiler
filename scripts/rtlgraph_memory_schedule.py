#!/usr/bin/env python3
"""Schedule an explicit VMEM load/VPU/store window with checked entry addresses.

This experimental adapter removes the former immutable memory wrapper. It uses
the existing compiler model, retains the outer DMA waits and original counters,
and requires independent RTL validation; no new numerical latency is asserted.
"""
from __future__ import annotations

import argparse
from collections import Counter
import json
import os
from pathlib import Path
import re
import subprocess

from rtlgraph_attention import strip_sentinel
from rtlgraph_kernel import (KernelError, assemble, instruction_words, integer,
                             load_assembler, read_text, tokens)
from rtlgraph_s0 import artifact
from rtlgraph_schedule import compute_slice, issue_summary, split, translate as compute_translate

BODY_LABEL = 'rtlgraph_memory_body'


def translate(text, *, to_compiler):
    result = []
    for line in text.splitlines():
        p = tokens(line)
        if not p:
            continue
        op = p[0].upper()
        if op in {'VLOAD', 'VSTORE'}:
            if to_compiler:
                if len(p) != 4:
                    raise KernelError('memory instruction needs three operands')
                m, x, imm = integer(p[1], 63), integer(p[2], 31, 'x'), integer(p[3], 4095)
            else:
                if len(p) != 3 or not (match := re.fullmatch(r'(\d+)\(x(\d+)\)', p[2])):
                    raise KernelError('unsupported compiler memory operand')
                m, x, imm = integer(p[1], 63, 'm'), integer(match[2], 31), integer(match[1], 4095)
            result.append(f'{op.lower()} m{m}, {imm}(x{x})' if to_compiler else f'{op} {m}, x{x}, {imm}')
        elif op == 'LI':
            if len(p) != 3 or integer(p[1], 31, 'x') == 0:
                raise KernelError('LI must write a nonzero scalar register')
            result.append(f'{"li" if to_compiler else "LI"} {p[1].lower()}, {integer(p[2], 0xffffffff)}')
        elif op in {'LUI', 'ADDI'} and not to_compiler:
            count = 3 if op == 'LUI' else 4
            if len(p) != count:
                raise KernelError('unsupported compiler scalar instruction')
            rd = integer(p[1], 31, 'x')
            if op == 'ADDI':
                rs = integer(p[2], 31, 'x')
                imm = integer(p[3], 4095)
                if rd == 0:
                    if rs != 0 or imm != 0:
                        raise KernelError('unexpected discarded scalar operation')
                    result.append('NOP')
                else:
                    result.append(f'ADDI x{rd}, x{rs}, {imm}')
            else:
                result.append(f'LUI x{rd}, {integer(p[2], 0xfffff)}')
        else:
            # The slice is VPU-only. No MXU/scale-register entry assumptions.
            converted = compute_translate(line, to_compiler=to_compiler).strip()
            if '.MXU' in converted.upper() or op.startswith('VMAT'):
                raise KernelError('only VPU compute is supported in a memory window')
            result.append(converted)
    if not result:
        raise KernelError('empty memory window')
    return '\n'.join(result) + '\n'


def entry_values(prefix, body):
    """Interpret only LI-defined scalar setup and completed DMA commands.

    Reject unknown setup instead of assuming zero registers or ready DMA data.
    The preserved beginning CSR can write a register, but cannot seed an address.
    """
    values, pending = {0: 0}, set()
    for line in prefix.splitlines():
        p = tokens(line)
        if not p:
            continue
        op = p[0].upper()
        if op == 'LI' and len(p) == 3:
            rd = integer(p[1], 31, 'x')
            if rd:
                values[rd] = integer(p[2], 0xffffffff)
        elif op in {'DMA.LOAD', 'DMA.STORE'} and len(p) == 5:
            channel = integer(p[4], 7)
            if channel in pending:
                raise KernelError('reused DMA channel before wait in setup')
            pending.add(channel)
        elif op == 'DMA.WAIT' and len(p) == 2:
            pending.discard(integer(p[1], 7))
        elif op == 'DMA.CONFIG' and len(p) == 3:
            if values.get(integer(p[1], 31, 'x')) != 0 or integer(p[2], 7) != 0:
                raise KernelError('only the known zero DMA base/configuration setup is supported')
        elif op in {'CSRR', 'CSRRS'} and len(p) in (3, 4) and integer(p[2], 4095) == 0xc00:
            rd = integer(p[1], 31, 'x')
            if rd:
                values.pop(rd, None)
        elif op == 'NOP' or (op == 'DELAY' and len(p) == 2):
            pass
        else:
            raise KernelError(f'unsupported entry-state operation: {op}')
    if pending:
        raise KernelError('all setup DMA commands must complete before the timed window')
    defined, needed = set(), set()
    for line in body.splitlines():
        p = tokens(line)
        if not p:
            continue
        if p[0].upper() in {'VLOAD', 'VSTORE'}:
            reg = integer(p[2], 31, 'x')
            if reg not in defined:
                needed.add(reg)
        elif p[0].upper() == 'LI':
            defined.add(integer(p[1], 31, 'x'))
    if not needed <= values.keys():
        raise KernelError('memory address register is unknown at counter entry')
    return {reg: values[reg] for reg in sorted(needed) if reg != 0}


def splice(source, scheduled, assembler):
    parts = split(source)
    body = translate(strip_sentinel(scheduled), to_compiler=False)
    if Counter(instruction_words(assembler, parts.body, include_idle=False)) != Counter(instruction_words(assembler, body, include_idle=False)):
        raise KernelError('scheduled memory window changed encoded non-idle operations')
    candidate = parts.prefix + body + parts.end_marker + parts.suffix
    if assemble(assembler, candidate) != assemble(assembler, parts.prefix) + assemble(assembler, body) + assemble(assembler, parts.end_marker + parts.suffix):
        raise KernelError('changed encoded instructions outside the measured window')
    return candidate, body


def prepare(source_path, assembler_path, compiler_path, output):
    source, assembler = read_text(source_path), load_assembler(assembler_path)
    parts = split(source)
    # Start with the same admitted source family as the frozen-wrapper adapter.
    compute_slice(parts.body, preserve_memory_wrapper=True)
    marker_regs = {integer(tokens(line)[1], 31, 'x') for line in
                   (parts.prefix.splitlines()[-1], parts.end_marker)}
    if any(p and p[0].upper() == 'LI' and integer(p[1], 31, 'x') in marker_regs
           for p in map(tokens, parts.body.splitlines())):
        raise KernelError('timed address setup must not overwrite a counter register')
    translated = translate(parts.body, to_compiler=True)
    if assemble(assembler, parts.body) != assemble(assembler, translate(translated, to_compiler=False)):
        raise KernelError('memory-window assembler roundtrip mismatch')
    values = entry_values(parts.prefix, parts.body)
    seed = ''.join(f'li x{reg}, {value}\n' for reg, value in values.items())
    # An explicit label separates the seed from scheduling; seed code is never
    # spliced into the real program. The actual preserved setup supplies values.
    prepared_text = seed + BODY_LABEL + ':\n' + translated + 'nop\necall\n'
    output.mkdir(parents=True, exist_ok=False)
    prepared = output / 'original.compiler.S'
    prepared.write_text(prepared_text)
    report = {'schema': 'atlas.rtlgraph.schedule-experiment.v1', 'status': 'preparing',
              'inputs': {'source': artifact(source_path), 'assembler': artifact(assembler_path),
                         'compiler': artifact(compiler_path), 'driver': artifact(Path(__file__).resolve())},
              'memory_scheduling': {'entry_scalar_values': {f'x{k}': v for k, v in values.items()},
                                    'entry': 'Preserved LI setup and completed DMA waits; fixed scalar addresses; idle VPU/LSU on entry.',
                                    'timing': 'Existing compiler model; independent CIRCT/RTL evidence recorded separately.'},
              'original_issue_summary': issue_summary(parts.body, assembler, preserve_memory_wrapper=True),
              'scope': 'Reorder timed VLOAD/VPU/LI/VSTORE operations, preserving encoded operations, outer setup/suffix, and original counters.',
              'completion': 'Model drains all scheduled work before the original ending counter.', 'cases': {}}
    for priority in ('critical', 'input'):
        name = 'memory_' + priority
        scheduled, candidate = output / (name + '.compiler.S'), output / (name + '.S')
        command = [str(compiler_path), str(prepared), '--passes', 'strip-artifacts,schedule',
                   '--schedule-priority', priority, '-o', str(scheduled)]
        run = subprocess.run(command, capture_output=True, text=True, env=os.environ.copy())
        log = output / (name + '.log')
        log.write_text(run.stdout + run.stderr)
        if run.returncode:
            raise KernelError(f'compiler rejected {name}; see {log}')
        pieces = scheduled.read_text().split(BODY_LABEL + ':\n')
        if len(pieces) != 2:
            raise KernelError('compiler did not retain the seed/window boundary')
        if Counter(assemble(assembler, pieces[0])) != Counter(assemble(assembler, seed)):
            raise KernelError('compiler moved instructions across the entry-state boundary')
        result, body = splice(source, pieces[1], assembler)
        candidate.write_text(result)
        report['cases'][name] = {'assembly': artifact(candidate), 'scheduled': artifact(scheduled),
                                'command': command, 'log': artifact(log),
                                'issue_summary': issue_summary(body, assembler, preserve_memory_wrapper=True),
                                'unchanged_non_idle_words': True, 'full_assembly_parts_equal': True}
    report['status'] = 'model_candidates_ready'
    (output / 'manifest.json').write_text(json.dumps(report, indent=2) + '\n')
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('source', 'assembler', 'atlas-opt', 'output'):
        parser.add_argument('--' + name, type=Path, required=True)
    args = parser.parse_args()
    report = prepare(*(getattr(args, name).resolve() for name in ('source', 'assembler', 'atlas_opt', 'output')))
    print(json.dumps({name: item['issue_summary']['cycle_csr_dispatch_window'] for name, item in report['cases'].items()}))


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError) as error:
        raise SystemExit(f'rtlgraph_memory_schedule: {error}')

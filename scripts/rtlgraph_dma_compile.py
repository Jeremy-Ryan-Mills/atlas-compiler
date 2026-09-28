#!/usr/bin/env python3
"""Schedule a straight-line baremetal kernel through native atlas-opt DMA rules.

This adapter translates assembly dialects and optionally relocates private
benchmark counters. It supplies no scheduling order, DMA addresses, or timing
constants: every functional instruction is scheduled and checked by atlas-opt.
"""
from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import subprocess

from rtlgraph_kernel import KernelError, assemble, instruction_words, integer, load_assembler, read_text, tokens
from rtlgraph_memory_schedule import translate as memory_translate
from rtlgraph_schedule import translate as compute_translate
from rtlgraph_s0 import artifact


REG_ALU = {'ADD', 'SUB', 'SLL', 'SLT', 'SLTU', 'XOR', 'SRL', 'SRA', 'OR', 'AND'}
IMM_ALU = {'ADDI', 'SLTI', 'SLTIU', 'XORI', 'ORI', 'ANDI', 'SLLI', 'SRLI', 'SRAI'}
CSR = {'CSRRW', 'CSRRS', 'CSRRC'}


def require(condition, message):
    if not condition:
        raise KernelError(message)


def active(source):
    return [line.split('#', 1)[0].strip() for line in source.splitlines() if tokens(line)]


def scalar_reg(value):
    return f'x{integer(value, 31, "x")}'


def immediate(value, bits):
    try:
        number = int(value, 0) if value.lower().startswith(('0x', '-0x')) else int(value)
    except ValueError as error:
        raise KernelError('invalid scalar immediate') from error
    require(-(1 << (bits - 1)) <= number < (1 << bits), 'scalar immediate out of range')
    return str(number & ((1 << bits) - 1))


def translate(source, *, to_compiler):
    out = []
    for line in source.splitlines():
        p = tokens(line)
        if not p:
            require('atlas.release' not in line.split('#', 1)[-1].split(),
                    'atlas.release must annotate a CSR instruction')
            continue
        op = p[0].upper()
        release = '#' in line and 'atlas.release' in line.split('#', 1)[1].split()
        csr_address = None
        require(not release or op in CSR | {'CSRR', 'CSRW'}, 'atlas.release must annotate a CSR instruction')
        require(':' not in op and op not in {'AUIPC', 'JAL', 'JALR', 'BEQ', 'BNE', 'BLT', 'BGE', 'BLTU', 'BGEU'},
                'only straight-line instructions without labels or PC-relative operands are supported')
        if op.startswith('DMA.'):
            if to_compiler:
                require(op in {'DMA.LOAD', 'DMA.STORE', 'DMA.CONFIG', 'DMA.WAIT'}, 'unsupported DMA instruction')
                count = {'DMA.LOAD': 5, 'DMA.STORE': 5, 'DMA.CONFIG': 3, 'DMA.WAIT': 2}[op]
                require(len(p) == count, 'wrong DMA operand count')
                channel = integer(p[-1], 7)
                args = [scalar_reg(x) for x in p[1:-1]]
                out.append(f'{op.lower()}.ch{channel}' + (' ' + ', '.join(args) if args else ''))
            else:
                match = re.fullmatch(r'(DMA\.(?:LOAD|STORE|CONFIG|WAIT))\.CH([0-7])', op)
                require(match is not None, 'unsupported native DMA instruction')
                op, channel = match.groups()
                require(len(p) == {'DMA.LOAD': 4, 'DMA.STORE': 4, 'DMA.CONFIG': 2, 'DMA.WAIT': 1}[op],
                        'wrong native DMA operand count')
                out.append(op + ' ' + ', '.join([*(scalar_reg(x) for x in p[1:]), channel]))
        elif op in CSR or (to_compiler and op in {'CSRR', 'CSRW'}):
            if to_compiler:
                if op == 'CSRR':
                    require(len(p) == 3, 'wrong CSRR operand count')
                    p, op = ['CSRRS', p[1], p[2], 'x0'], 'CSRRS'
                elif op == 'CSRW':
                    require(len(p) == 3, 'wrong CSRW operand count')
                    p, op = ['CSRRW', 'x0', p[2], p[1]], 'CSRRW'
                require(len(p) == 4, 'wrong CSR operand count')
                csr_address = integer(p[2], 4095)
                out.append(f'{op.lower()} {scalar_reg(p[1])}, {scalar_reg(p[3])}, {csr_address}')
            else:
                require(len(p) == 4, 'wrong native CSR operand count')
                csr_address = integer(p[3], 4095)
                out.append(f'{op} {scalar_reg(p[1])}, {csr_address}, {scalar_reg(p[2])}')
        elif op in REG_ALU | IMM_ALU | {'LI', 'LUI'}:
            count = 3 if op in {'LI', 'LUI'} else 4
            require(len(p) == count, 'wrong scalar operand count')
            args = [scalar_reg(p[1])]
            if op in REG_ALU | IMM_ALU:
                args.append(scalar_reg(p[2]))
            args.append(scalar_reg(p[-1]) if op in REG_ALU else immediate(p[-1], 32 if op == 'LI' else 20 if op == 'LUI' else 12))
            if not to_compiler and op == 'ADDI' and args == ['x0', 'x0', '0']:
                out.append('NOP')
            else:
                out.append((op.lower() if to_compiler else op) + ' ' + ', '.join(args))
        elif op in {'ECALL', 'FENCE'}:
            require(len(p) == 1, 'unexpected operand on ' + op)
            out.append(op.lower() if to_compiler else op)
        elif op == 'SELI':
            require(len(p) == 3, 'SELI requires a scale register and a 12-bit immediate')
            scale = integer(p[1], 31, '' if to_compiler else 'e')
            value = integer(p[2], 4095)
            out.append(f'seli e{scale}, {value}' if to_compiler else f'SELI {scale}, {value}')
        elif op.endswith(('.MXU0', '.MXU1')):
            # Reuse the checked MXU operand mappings. The VPU memory-window
            # adapter deliberately rejects MXUs, while this full stream has
            # their real loads, scale setup, and completion guards available.
            out.append(compute_translate(line, to_compiler=to_compiler).strip())
        else:
            out.append(memory_translate(line, to_compiler=to_compiler).strip())
        # DBG0 publishes completion to the replay host. Even a source without
        # compiler metadata must drain fixed-latency work before publication.
        if release or csr_address == 0xc10:
            out[-1] += ' # atlas.release'
        # Match the native parser's case-sensitive keep annotation semantics.
        if op == 'DELAY' and '#' in line and 'keep' in line.split('#', 1)[1]:
            out[-1] += ' # keep'
    require(out, 'empty kernel')
    return '\n'.join(out) + '\n'


@dataclass(frozen=True)
class Markers:
    start: str
    ending: tuple[str, str, str]
    private_registers: tuple[int, int, int]


def extract_markers(source):
    """Remove only an isolated cycle-read/subtract/DBG1 benchmark expression."""
    # Validate metadata before filtering comment-only lines or removing markers.
    translate(source, to_compiler=True)
    lines = [line.strip() for line in source.splitlines() if tokens(line)]
    positions = []
    for index, line in enumerate(lines):
        p = tokens(line)
        op = p[0].upper()
        if op in {'CSRR', 'CSRRS'} and len(p) >= 3 and integer(p[2], 4095) == 0xc00:
            require((op == 'CSRR' and len(p) == 3) or
                    (op == 'CSRRS' and len(p) == 4 and scalar_reg(p[3]) == 'x0'),
                    'cycle benchmark marker must be a read-only CSR read')
            positions.append(index)
    require(len(positions) == 2, 'marker relocation requires exactly two cycle reads')
    begin, end = positions
    require(end + 2 < len(lines), 'truncated benchmark expression')
    a, b = [integer(tokens(lines[pos])[1], 31, 'x') for pos in positions]
    sub, write = tokens(lines[end + 1]), tokens(lines[end + 2])
    require(len(sub) == 4 and sub[0].upper() == 'SUB' and
            [scalar_reg(x) for x in sub[2:]] == [f'x{b}', f'x{a}'],
            'ending cycle read must feed the isolated elapsed-cycle subtraction')
    elapsed = integer(sub[1], 31, 'x')
    require(len({a, b, elapsed}) == 3 and 0 not in {a, b, elapsed}, 'counter registers must be distinct and nonzero')
    normalized_write = translate(lines[end + 2], to_compiler=True).strip()
    require(normalized_write == f'csrrw x0, x{elapsed}, 3089', 'elapsed result must write only DBG1')
    selected = {begin, end, end + 1, end + 2}
    require(not any('#' in lines[i] and 'atlas.release' in lines[i].split('#', 1)[1].split()
                    for i in selected), 'cannot relocate a benchmark marker with atlas.release')
    body = [line for i, line in enumerate(lines) if i not in selected]
    private = {a, b, elapsed}
    for line in body:
        canonical = translate(line, to_compiler=True).split('#', 1)[0]
        references = {int(x) for x in re.findall(r'\bx(\d+)\b', canonical)}
        require(not references & private, 'benchmark registers also appear in functional instructions')
        p = tokens(line)
        if p[0].upper() in CSR | {'CSRR', 'CSRW'} and len(p) >= 3:
            require(integer(p[2], 4095) not in {0xc00, 0xc11}, 'another instruction accesses a benchmark CSR')
    require(any(tokens(line)[0].upper() == 'DMA.WAIT' for line in body), 'benchmark kernel has no final DMA wait')
    return '\n'.join(body) + '\n', Markers(lines[begin], tuple(lines[end:end + 3]), (a, b, elapsed))


def insert_markers(body, markers):
    lines = [line.strip() for line in body.splitlines() if tokens(line)]
    waits = [i for i, line in enumerate(lines) if tokens(line)[0].upper() == 'DMA.WAIT']
    require(waits, 'scheduled kernel lost its final explicit DMA wait')
    index = waits[-1]
    return '\n'.join([markers.start, *lines[:index], *markers.ending, *lines[index:]]) + '\n'


def same_operations(source, candidate, assembler):
    require(Counter(instruction_words(assembler, source, include_idle=False)) ==
            Counter(instruction_words(assembler, candidate, include_idle=False)),
            'native schedule changed encoded non-idle operations or operands')


def prepare(source_path, assembler_path, compiler_path, profile_path, output, *, relocate_perf_markers=False,
            mxu0_profile=None, mxu1_profile=None):
    source, assembler = read_text(source_path), load_assembler(assembler_path)
    require(active(source)[-1].upper() == 'ECALL' and
            sum(tokens(line)[0].upper() == 'ECALL' for line in active(source)) == 1,
            'kernel must terminate in exactly one ECALL')
    markers = None
    functional = source
    if relocate_perf_markers:
        functional, markers = extract_markers(source)
    native = translate(functional, to_compiler=True)
    require(assemble(assembler, functional) == assemble(assembler, translate(native, to_compiler=False)),
            'assembly dialect translation changed original encoded instructions')
    output.mkdir(parents=True, exist_ok=False)
    prepared = output / 'original.compiler.S'
    prepared.write_text(native)
    profiles = [(f'mxu{engine}_profile', f'--experimental-mxu{engine}-profile', path)
                for engine, path in ((0, mxu0_profile), (1, mxu1_profile)) if path is not None]
    model_flags = ['--rtl-dma-profile', str(profile_path)]
    for _, flag, path in profiles:
        model_flags += [flag, str(path)]
    report = dict(schema='atlas.rtlgraph.dma-native-schedule.v1', status='preparing',
                  inputs={name: artifact(path) for name, path in
                          (('source', source_path), ('assembler', assembler_path), ('compiler', compiler_path),
                           ('profile', profile_path), ('driver', Path(__file__).resolve()))},
                  native_input=artifact(prepared),
                  counter_scope_comparable=not relocate_perf_markers,
                  instrumentation=dict(relocated=relocate_perf_markers,
                                       private_registers=list(markers.private_registers) if markers else [],
                                       policy='Starting cycle read before the scheduled kernel; ending read/SUB/DBG1 before the last existing DMA.WAIT. Only diagnostic counter scope changes.' if markers else 'Original CSR instructions remain functional scheduling barriers.'),
                  scope='Native atlas-opt dependency and resource scheduling of the complete supported straight-line scalar/DMA/LSU/VPU/MXU stream; no handwritten ordering or unary-specific transformation.',
                  publication='All DBG0 CSR accesses conservatively receive atlas.release; explicit source release metadata on other CSRs is preserved.',
                  limitations=['Explicit waits are preserved; no automatic wait insertion.',
                               'Local LSU/VPU and non-overridden MXU timing remain inherited; no fixed DMA completion bound.',
                               'Native model legality and encoded operation preservation require independent RTL/golden validation.'],
                  cases={})
    report['inputs'].update({name: artifact(path) for name, _, path in profiles})
    directives = '\n'.join(line for line in source.splitlines() if re.match(r'^\s*#\s*@', line)) + '\n'
    duplicates = {}
    for priority in ('critical', 'input'):
        name = 'native_' + priority
        scheduled, candidate = output / f'{name}.compiler.S', output / f'{name}.S'
        command = [str(compiler_path), str(prepared), '--passes', 'strip-artifacts,schedule',
                   '--schedule-priority', priority, *model_flags, '-o', str(scheduled)]
        run = subprocess.run(command, capture_output=True, text=True, env=os.environ.copy())
        log = output / f'{name}.log'
        log.write_text(run.stdout + run.stderr)
        require(run.returncode == 0, f'native compiler rejected {name}; see {log}')
        body = translate(read_text(scheduled), to_compiler=False)
        require(assemble(assembler, translate(translate(body, to_compiler=True), to_compiler=False)) == assemble(assembler, body),
                'native output roundtrip changed instruction encoding')
        if markers:
            body = insert_markers(body, markers)
        result = directives + '# Native compiler DMA scheduling; completion is checked after explicit waits.\n' + body
        same_operations(source, result, assembler)
        candidate.write_text(result)
        # Reinserting counter instructions changes issue gaps. Resource conflicts
        # need not be monotone in those gaps, so check the exact final stream.
        checked = output / f'{name}.final-check.compiler.S'
        checked.write_text(translate(result, to_compiler=True))
        check_command = [str(compiler_path), '--check', str(checked), *model_flags]
        check = subprocess.run(check_command, capture_output=True, text=True, env=os.environ.copy())
        check_log = output / f'{name}.final-check.log'
        check_log.write_text(check.stdout + check.stderr)
        require(check.returncode == 0, f'exact final native schedule rejected; see {check_log}')
        words = tuple(assemble(assembler, result))
        duplicate = duplicates.get(words)
        duplicates.setdefault(words, name)
        report['cases'][name] = dict(assembly=artifact(candidate), scheduled=artifact(scheduled), command=command,
                                   log=artifact(log), final_check_input=artifact(checked), final_check_command=check_command,
                                   final_check_log=artifact(check_log), unchanged_non_idle_words=True,
                                   scheduler_returncode=run.returncode, final_check_returncode=check.returncode,
                                   word_count=len(words), identical_encoded_stream_to=duplicate)
    report['status'] = 'native_model_candidates_ready'
    (output / 'manifest.json').write_text(json.dumps(report, indent=2) + '\n')
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for flag in ('source', 'assembler', 'atlas-opt', 'profile', 'output'):
        parser.add_argument('--' + flag, type=Path, required=True)
    parser.add_argument('--relocate-perf-markers', action='store_true',
                        help='Explicitly expand private benchmark counter scope; original CSR deltas become incomparable')
    for engine in (0, 1):
        parser.add_argument(f'--mxu{engine}-profile', type=Path,
                            help=f'Compose an existing partial MXU{engine} profile with the DMA model')
    args = parser.parse_args()
    report = prepare(*(getattr(args, flag).resolve() for flag in ('source', 'assembler', 'atlas_opt', 'profile', 'output')),
                     relocate_perf_markers=args.relocate_perf_markers,
                     mxu0_profile=args.mxu0_profile.resolve() if args.mxu0_profile else None,
                     mxu1_profile=args.mxu1_profile.resolve() if args.mxu1_profile else None)
    print(json.dumps({name: item['word_count'] for name, item in report['cases'].items()}))


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, KeyError) as error:
        raise SystemExit(f'rtlgraph_dma_compile: {error}')

#!/usr/bin/env python3
"""Prepare fixed-operation schedules for straight-line, register-only perf kernels.

Supports both MXUs and the VPU operations used by the golden-backed corpus.
Memory/scalar instructions are rejected unless an explicitly selected, checked
load/compute/store wrapper preserves them. Setup and writeback stay byte-for-byte.
Model completion is kept inside the counter window; RTL validation is separate.
"""
from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import dataclass
import json
import os
from pathlib import Path
import subprocess

from rtlgraph_attention import strip_sentinel, translate as translate_attention
from rtlgraph_kernel import (KernelError, KernelSlice, assemble, integer,
                             instruction_words, load_assembler, model_issue_summary,
                             read_text, sha, tokens)
from rtlgraph_s0 import artifact

UNARY = {"VSQUARE.BF16": "vsquare.bf16", "VSQRT": "vsqrt.bf16",
         "VTANH": "vtanh.bf16", "VLOG2": "vlog2.bf16"}


@dataclass(frozen=True)
class ComputeSlice:
    prefix: str
    body: str
    suffix: str


def compute_slice(body, *, preserve_memory_wrapper=False):
    """Select one compute span without moving timed memory/scalar instructions.

    The optional form is deliberately narrow: VLOAD+DELAY setup, register-only
    compute, then LI/VSTORE+DELAY writeback. Every transfer retains an immediate
    DELAY of at least 33. This preserves the inherited transfer-ready assumption;
    it is not an extracted or universally proven memory timing contract.
    """
    if not preserve_memory_wrapper:
        translate(body, to_compiler=True)
        return ComputeSlice('', body, '')
    lines = body.splitlines(keepends=True)
    active = [(i, tokens(line)) for i, line in enumerate(lines) if tokens(line)]
    compute = []
    for i, parts in active:
        if parts[0].upper() not in {'VLOAD', 'VSTORE', 'LI', 'DELAY', 'NOP'}:
            translate(lines[i], to_compiler=True)
            compute.append(i)
    if not compute:
        raise KernelError('memory wrapper needs a register-only compute block')
    begin = compute[0]
    end = next((i for i, parts in active if i > compute[-1] and parts[0].upper() not in {'DELAY', 'NOP'}), len(lines))
    result = ComputeSlice(''.join(lines[:begin]), ''.join(lines[begin:end]), ''.join(lines[end:]))
    translate(result.body, to_compiler=True)  # Reject interleaved transfers/setup.
    for region, transfer, allowed in ((result.prefix, 'VLOAD', {'VLOAD', 'DELAY', 'NOP'}),
                                     (result.suffix, 'VSTORE', {'LI', 'VSTORE', 'DELAY', 'NOP'})):
        instructions = [tokens(line) for line in region.splitlines() if tokens(line)]
        if not any(parts[0].upper() == transfer for parts in instructions):
            raise KernelError(f'memory wrapper requires {transfer}')
        for index, parts in enumerate(instructions):
            op = parts[0].upper()
            if op not in allowed:
                raise KernelError(f'unsupported {op} in immutable memory wrapper')
            if op == transfer:
                if len(parts) != 4:
                    raise KernelError(f'{transfer} requires three operands')
                integer(parts[1], 63)
                integer(parts[2], 31, 'x')
                integer(parts[3], 4095)
                following = instructions[index + 1] if index + 1 < len(instructions) else []
                if len(following) != 2 or following[0].upper() != 'DELAY' or integer(following[1], 4095) < 33:
                    raise KernelError(f'each immutable {transfer} must retain DELAY >= 33 immediately afterward')
            elif op == 'LI':
                if len(parts) != 3:
                    raise KernelError('immutable LI requires two operands')
                integer(parts[1], 31, 'x')
                integer(parts[2], 0xffffffff)
            else:
                translate(' '.join(parts), to_compiler=True)
    return result


def issue_summary(body, assembler, *, preserve_memory_wrapper=False):
    if not preserve_memory_wrapper:
        return model_issue_summary(body)
    summary = model_issue_summary(body)
    extra, index = 0, 0
    for line in body.splitlines():
        parts = tokens(line)
        if not parts or parts[0].upper() in {'DELAY', 'NOP'}:
            continue
        summary['operations'][index]['body_issue_cycle'] += extra
        extra += len(assemble(assembler, line)) - 1
        index += 1
    summary['cycle_csr_dispatch_window'] += extra
    summary['basis'] = ('static encoded scalar issue count (including LI expansion); DELAY N occupies N+1 cycles; '
                        'memory stalls excluded; not an RTL measurement')
    return summary


def translate(text, *, to_compiler):
    result = []
    for line in text.splitlines():
        parts = tokens(line)
        if not parts:
            continue
        op = parts[0].upper()
        if op == 'VLI.ALL':
            if len(parts) != 3:
                raise KernelError('VLI.ALL requires a destination and a 16-bit immediate')
            reg = integer(parts[1], 63, '' if to_compiler else 'm')
            immediate = integer(parts[2], 65535)
            result.append(f'vli.all m{reg}, {immediate}' if to_compiler else f'VLI.ALL {reg}, {immediate}')
        elif op in (UNARY if to_compiler else {value.upper() for value in UNARY.values()}):
            if len(parts) != 3:
                raise KernelError(f'{op} requires two registers')
            regs = [integer(value, 63, '' if to_compiler else 'm') for value in parts[1:]]
            name = UNARY[op] if to_compiler else next(key for key, value in UNARY.items() if value.upper() == op)
            result.append(name + ' ' + ', '.join(('m' if to_compiler else '') + str(reg) for reg in regs))
        elif op.endswith('.MXU0'):
            # Same operand encodings; only known MXU1 mappings are admitted.
            equivalent = ' '.join([parts[0][:-1] + '1', *parts[1:]])
            converted = translate_attention(equivalent, to_compiler=to_compiler).strip()
            name, rest = converted.split(' ', 1)
            if not name.upper().endswith('.MXU1'):
                raise KernelError('MXU engine translation mismatch')
            result.append(name[:-1] + '0 ' + rest)
        else:
            result.append(translate_attention(line, to_compiler=to_compiler).strip())
    if not result:
        raise KernelError('empty measured body')
    return '\n'.join(result) + '\n'


def split(source):
    lines, markers = source.splitlines(keepends=True), []
    for index, line in enumerate(lines):
        p = tokens(line)
        if not p:
            continue
        op = p[0].upper()
        if op in {'AUIPC', 'JAL', 'JALR', 'BEQ', 'BNE', 'BLT', 'BGE', 'BLTU', 'BGEU'}:
            raise KernelError('PC-relative instructions and control flow are unsupported')
        if op in {'CSRR', 'CSRRS'} and len(p) >= 3 and integer(p[2], 4095) == 0xc00:
            integer(p[1], 31, 'x')
            if not ((op == 'CSRR' and len(p) == 3) or (op == 'CSRRS' and len(p) == 4 and p[3].lower() == 'x0')):
                raise KernelError('cycle marker must be a read-only CSR read')
            markers.append(index)
    if len(markers) != 2:
        raise KernelError('expected exactly two cycle CSR reads')
    a, b = markers
    return KernelSlice(''.join(lines[:a+1]), ''.join(lines[a+1:b]), lines[b], ''.join(lines[b+1:]), a+1, b+1)


def splice(source, scheduled, assembler, *, preserve_memory_wrapper=False):
    sliced = split(source)
    compute = compute_slice(sliced.body, preserve_memory_wrapper=preserve_memory_wrapper)
    original = translate(compute.body, to_compiler=True)
    roundtrip = compute.prefix + translate(original, to_compiler=False) + compute.suffix
    if assemble(assembler, source) != assemble(assembler, sliced.prefix + roundtrip + sliced.end_marker + sliced.suffix):
        raise KernelError('original assembler roundtrip mismatch')
    scheduled_body = translate(strip_sentinel(scheduled), to_compiler=False)
    if Counter(instruction_words(assembler, compute.body, include_idle=False)) != Counter(instruction_words(assembler, scheduled_body, include_idle=False)):
        raise KernelError('scheduled compute changes encoded non-idle instruction multiset')
    body = compute.prefix + scheduled_body + compute.suffix
    if Counter(instruction_words(assembler, sliced.body, include_idle=False)) != Counter(instruction_words(assembler, body, include_idle=False)):
        raise KernelError('scheduled body changes encoded non-idle instruction multiset')
    candidate = sliced.prefix + body + sliced.end_marker + sliced.suffix
    if assemble(assembler, candidate) != assemble(assembler, sliced.prefix) + assemble(assembler, body) + assemble(assembler, sliced.end_marker + sliced.suffix):
        raise KernelError('replacement changed instructions outside the measured body')
    return candidate, body


def prepare(source_path, assembler_path, compiler_path, output, profiles=(), *, preserve_memory_wrapper=False):
    output.mkdir(parents=True, exist_ok=False)
    source = read_text(source_path)
    sliced, assembler = split(source), load_assembler(assembler_path)
    compute = compute_slice(sliced.body, preserve_memory_wrapper=preserve_memory_wrapper)
    compiled = translate(compute.body, to_compiler=True) + 'ecall\n'
    splice(source, compiled, assembler, preserve_memory_wrapper=preserve_memory_wrapper)
    prepared = output / 'original.compiler.S'
    prepared.write_text(compiled)
    report = {'schema': 'atlas.rtlgraph.schedule-experiment.v1', 'status': 'preparing',
              'inputs': {'source': artifact(source_path), 'assembler': artifact(assembler_path),
                         'compiler': artifact(compiler_path), 'driver': artifact(Path(__file__).resolve()),
                         'profiles': {flag: artifact(path) for flag, path in profiles}},
              'cycle_marker_lines': [sliced.begin_line, sliced.end_line],
              'original_word_count': len(assemble(assembler, source)),
              'prefix_sha256': sha(sliced.prefix.encode()), 'suffix_sha256': sha(sliced.suffix.encode()),
              'original_issue_summary': issue_summary(sliced.body, assembler, preserve_memory_wrapper=preserve_memory_wrapper), 'cases': {},
              'scope': 'Fixed operations and operands, unchanged setup/counters/writeback. Assumes ready entry operands; no memory/scalar operations inside body.',
              'completion': 'ECALL sentinel replaced by original ending CSR; modeled completion drain stays inside measured window. Original window may end earlier than modeled completion.',
              'validation': 'Assembler equivalence and compiler model checks only; each distinct candidate needs RTL execution.'}
    if preserve_memory_wrapper:
        report['memory_wrapper'] = {'prefix_sha256': sha(compute.prefix.encode()),
                                    'suffix_sha256': sha(compute.suffix.encode()),
                                    'prefix_words': assemble(assembler, compute.prefix),
                                    'suffix_words': assemble(assembler, compute.suffix),
                                    'entry_assumption': 'Every original VLOAD retains immediate DELAY >= 33; no timing extraction or universal proof.',
                                    'completion': 'Modeled compute completion drain precedes immutable VSTORE writeback inside original counters.'}
        report['scope'] = 'Fixed compute operations/operands; original counters, timed VLOAD/DELAY setup, LI/VSTORE/DELAY writeback, and outer setup/suffix unchanged.'
    encodings = {}
    for use_profile in ((False, True) if profiles else (False,)):
        for priority in ('critical', 'input'):
            name = ('profile_' if use_profile else 'builtin_') + priority
            scheduled = output / (name + '.compiler.S')
            candidate = output / (name + '.S')
            command = [str(compiler_path), str(prepared), '--passes', 'strip-artifacts,schedule',
                       '--schedule-priority', priority, '-o', str(scheduled)]
            if use_profile:
                for flag, path in profiles:
                    command += [flag, str(path)]
            run = subprocess.run(command, capture_output=True, text=True, env=os.environ.copy())
            log = output / (name + '.log')
            log.write_text(run.stdout + run.stderr)
            if run.returncode:
                raise KernelError(f'compiler rejected {name}; see {log}')
            result, body = splice(source, read_text(scheduled), assembler, preserve_memory_wrapper=preserve_memory_wrapper)
            candidate.write_text(result)
            words = tuple(assemble(assembler, result))
            duplicate = encodings.get(words)
            encodings.setdefault(words, name)
            report['cases'][name] = {'assembly': artifact(candidate), 'scheduled': artifact(scheduled),
                                     'command': command, 'log': artifact(log), 'issue_summary': issue_summary(body, assembler, preserve_memory_wrapper=preserve_memory_wrapper),
                                     'identical_encoded_stream_to': duplicate,
                                     'unchanged_non_idle_words': True, 'full_assembly_parts_equal': True}
    report['status'] = 'model_candidates_ready'
    (output / 'manifest.json').write_text(json.dumps(report, indent=2) + '\n')
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('source', 'assembler', 'atlas-opt', 'output'):
        parser.add_argument('--' + name, type=Path, required=True)
    for mxu in (0, 1):
        parser.add_argument(f'--mxu{mxu}-profile', type=Path)
    parser.add_argument('--preserve-memory-wrapper', action='store_true',
                        help='schedule only compute between checked, immutable timed VLOAD and LI/VSTORE wrappers')
    args = parser.parse_args()
    profiles = [(f'--experimental-mxu{mxu}-profile', path.resolve()) for mxu in (0, 1)
                if (path := getattr(args, f'mxu{mxu}_profile'))]
    report = prepare(args.source.resolve(), args.assembler.resolve(), args.atlas_opt.resolve(), args.output.resolve(), profiles,
                     preserve_memory_wrapper=args.preserve_memory_wrapper)
    print(json.dumps({name: {'static_window': case['issue_summary']['cycle_csr_dispatch_window'],
                             'duplicate': case['identical_encoded_stream_to']} for name, case in report['cases'].items()}))


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError) as error:
        raise SystemExit(f'rtlgraph_schedule: {error}')

#!/usr/bin/env python3
"""Overlap explicit DMA commands with the checked perf_unary memory schedule.

This is a narrow experiment, not automatic wait insertion or the compiler's DMA
model. DMA completion has no numerical bound. The range checker treats each DMA
as pending until its existing wait; compiler checks cover DMA-elided LSU/VPU
projections. Independent RTL replay remains required.
"""
from __future__ import annotations

import argparse
from collections import Counter
import json
import os
from pathlib import Path
import subprocess

from rtlgraph_kernel import KernelError, assemble, instruction_words, integer, load_assembler, read_text, tokens
from rtlgraph_memory_schedule import translate
from rtlgraph_s0 import artifact
from rtlgraph_schedule import split

UNARY = {'VSQUARE.BF16', 'VTANH', 'VEXP', 'VSQRT', 'VLOG2', 'VRECIP.BF16'}
LAST_LSU_WRITE = 34  # inherited model, corroborated by recorded LSU traces
LAST_UNARY_WRITE = 65  # inherited model, corroborated by recorded VPU traces


def require(condition, message):
    if not condition:
        raise KernelError(message)


def active(text):
    return [line.split('#', 1)[0].strip() for line in text.splitlines() if tokens(line)]


def timeline(text, assembler):
    cycle, events = 0, []
    for line in active(text):
        p = tokens(line)
        if p[0].upper() == 'DELAY':
            cycle += 1 + integer(p[1], 4095)
        else:
            width = len(assemble(assembler, line))
            require(width > 0, 'empty encoding')
            if p[0].upper() != 'NOP':
                events.append((cycle, line, width))
            cycle += width
    return events, cycle


def emit(events, end):
    result, cycle = [], 0
    for when, line, width in sorted(events):
        require(when >= cycle, 'inserted instructions collide with scalar issue')
        gap = when - cycle
        if gap:
            require(gap <= 4096, 'unsupported long idle interval')
            result.append('NOP' if gap == 1 else f'DELAY {gap - 1}')
        result.append(line)
        cycle = when + width
    require(end >= cycle, 'body ends before inserted instruction')
    if end > cycle:
        gap = end - cycle
        require(gap <= 4096, 'unsupported long final interval')
        result.append('NOP' if gap == 1 else f'DELAY {gap - 1}')
    return '\n'.join(result) + '\n'


def transfer_key(transfer):
    return tuple(transfer[name] for name in ('kind', 'channel', 'vmem_line', 'line_count', 'dram_address', 'bytes'))


def check_dma_ranges(text, assembler):
    """Check concrete launch-captured ranges, data readiness and explicit waits.

    Issue cycles are lower bounds, omitting unknown DMA stalls. Delaying a wait
    only advances completion of older local work. Compiler projection checks
    separately cover the only admitted wait with outstanding LSU/VPU work.
    """
    regs, pending, available, produced = {0: 0}, {}, set(), {}
    transfers, waits, local = [], [], []
    base = None
    cycle = wait_index = 0

    def reg(name):
        key = integer(name, 31, 'x')
        require(key in regs, 'unknown launch/address scalar register ' + name)
        return regs[key]

    def lines(address, count):
        require(address % 8 == 0, 'VMEM word address must be line aligned')
        start = (address & ((1 << 19) - 1)) >> 3
        require(count > 0 and start + count <= 6 * 8192, 'VMEM range outside configured banks')
        return set(range(start, start + count))

    for line in active(text):
        p = tokens(line)
        op = p[0].upper()
        width = len(assemble(assembler, line))
        if op in {'LI', 'LUI', 'ADDI'}:
            rd = integer(p[1], 31, 'x')
            if op == 'LI':
                value = integer(p[2], 0xffffffff)
            elif op == 'LUI':
                value = integer(p[2], 0xfffff) << 12
            else:
                immediate = integer(p[3], 4095)
                value = (reg(p[2]) + (immediate if immediate < 2048 else immediate - 4096)) & 0xffffffff
            if rd:
                regs[rd] = value
        elif op == 'DMA.CONFIG':
            require(reg(p[1]) == 0 and integer(p[2], 7) == 0 and not pending,
                    'requires the original zero DMA base configuration')
            base = 0
        elif op in {'DMA.LOAD', 'DMA.STORE'}:
            require(base == 0 and len(p) == 5, 'missing supported DMA configuration')
            channel = integer(p[4], 7)
            require(channel not in pending, 'DMA channel reused before explicit wait')
            require(len(pending) < 8, 'too many outstanding DMA commands')
            load = op == 'DMA.LOAD'
            address, dram, size = reg(p[1] if load else p[2]), reg(p[2] if load else p[1]), reg(p[3])
            require(size == 2048 and dram % 32 == 0, 'only aligned 2048-byte transfers are admitted')
            region = lines(address, size // 32)
            for old in pending.values():
                require(not (region & old['region']) or not (load or old['kind'] == 'load'),
                        'overlapping pending DMA ranges')
                dram_overlap = max(dram, old['dram_address']) < min(dram + size, old['dram_address'] + old['bytes'])
                require(not dram_overlap or load and old['kind'] == 'load', 'overlapping DMA DRAM destinations')
            for event in local:
                require(not (region & event['region']) or cycle > event['last']
                        or not (load or event['kind'] == 'store'), 'DMA races unfinished local VMEM access')
            if not load:
                require(all(row in produced and produced[row] < cycle for row in region),
                        'DMA store launched before complete local output')
            record = dict(kind='load' if load else 'store', channel=channel, vmem_line=min(region),
                          line_count=len(region), dram_address=dram, bytes=size, issue_lower_bound=cycle)
            transfers.append(record)
            pending[channel] = dict(record, region=region)
        elif op == 'DMA.WAIT':
            channel = integer(p[1], 7)
            require(channel in pending, 'wait has no unmatched DMA command')
            complete = pending.pop(channel)
            if complete['kind'] == 'load':
                available.update(complete['region'])
            remaining = max([0] + [event['last'] - cycle for event in local])
            waits.append(dict(index=wait_index, channel=channel, issue_lower_bound=cycle,
                              prior_local_last_age=max(0, remaining)))
            wait_index += 1
        elif op in {'VLOAD', 'VSTORE'}:
            imm = integer(p[3], 4095)
            imm = imm if imm < 2048 else imm - 4096
            region = lines((reg(p[2]) + imm * 32) & 0xffffffff, 32)
            require(min(region) % 32 == 0 and min(region) // 8192 == max(region) // 8192,
                    'LSU vector transfer must be aligned and contained in one bank')
            load = op == 'VLOAD'
            for old in pending.values():
                require(not (region & old['region']) or load and old['kind'] == 'store',
                        'local VMEM access crosses an unfinished DMA')
            if load:
                require(region <= available, 'VLOAD data lacks a completed DMA load')
            else:
                for row in region:
                    produced[row] = cycle + LAST_LSU_WRITE
            local.append(dict(kind='load' if load else 'store', region=region,
                              last=cycle + LAST_LSU_WRITE))
        elif op in UNARY:
            local.append(dict(kind='compute', region=set(), last=cycle + LAST_UNARY_WRITE))
        elif op in {'CSRRS', 'CSRRW', 'CSRW', 'SUB'}:
            # These exact counter/debug instructions are preserved and do not
            # feed addresses. Mark their outputs unknown rather than inventing
            # cycle-counter values.
            if op != 'CSRW':
                rd = integer(p[1], 31, 'x')
                if rd:
                    regs.pop(rd, None)
        elif op == 'DELAY':
            cycle += integer(p[1], 4095)
        elif op == 'ECALL':
            require(not pending and all(event['last'] < cycle for event in local), 'halt precedes completion')
        else:
            require(op == 'NOP', 'unsupported operation in DMA experiment: ' + op)
        cycle += width
    require(not pending and len(transfers) == 5 and len(waits) == 5, 'requires all five DMA commands and waits')
    require(sum(t['kind'] == 'load' for t in transfers) == 2, 'requires two input and three output transfers')
    return dict(transfers=transfers, waits=waits, minimum_dispatch_cycles=cycle,
                scope='Explicit wait/range/channel checks with RTL address units; local completion ages are inherited assumptions, not a universal RTL proof.')


def candidate(source, assembler, move_input):
    parts = split(source)
    prefix, suffix = active(parts.prefix), active(parts.suffix)
    require([tokens(s) for s in suffix[:2]] == [['SUB', 'x22', 'x21', 'x20'], ['CSRRW', 'x0', '0xC11', 'x22']],
            'unexpected counter suffix')
    groups = []
    for i, (address, base) in enumerate(((0x90001000, 'x6'), (0x90001800, 'x7'), (0x90002000, 'x8'))):
        group = suffix[2 + 3 * i:5 + 3 * i]
        require(len(group) == 3 and tokens(group[0])[:2] == ['LI', 'x3']
                and integer(tokens(group[0])[2], 0xffffffff) == address
                and tokens(group[1]) == ['DMA.STORE', 'x3', base, 'x12', '1']
                and tokens(group[2]) == ['DMA.WAIT', '1'], 'unexpected explicit output DMA group')
        groups.append(group)
    require([tokens(line) for line in suffix[11:]] == [['LI', 'x30', '1'], ['CSRW', 'x30', '0xC10'], ['ECALL']],
            'unexpected final completion sequence')
    events, end = timeline(parts.body, assembler)
    require(all(tokens(line)[0].upper() in UNARY | {'VLOAD', 'VSTORE', 'LUI', 'ADDI', 'LI'} for _, line, _ in events),
            'unsupported memory baseline body')
    c_stores = [when for when, line, _ in events if tokens(line) in
                (['VSTORE', '8', 'x8', '0'], ['VSTORE', '9', 'x8', '8'])]
    require(len(c_stores) == 2, 'missing C output stores')
    start = max(c_stores) + LAST_LSU_WRITE
    width = len(assemble(assembler, groups[2][0]))
    require(width == 1 and all(not (start <= when < start + 2) for when, _, _ in events),
            'C DMA launch needs two free scalar issue cycles')
    events.extend(((start, groups[2][0], 1), (start + 1, groups[2][1], 1)))
    if move_input:
        waits = [i for i, line in enumerate(prefix) if tokens(line) == ['DMA.WAIT', '1']]
        require(len(waits) == 1, 'requires one input channel-one wait')
        wait = prefix.pop(waits[0])
        targets = [when for when, line, _ in events if tokens(line) == ['VLOAD', '4', 'x9', '0']]
        require(len(targets) == 1, 'requires first B load')
        when = targets[0]
        events = [(age + int(age >= when), line, width) for age, line, width in events]
        events.append((when, wait, 1))
        end += 1
    banner = ('# @TIMEOUT 25000\n# @DRAM_BASE 0x90000000\n# @PYTHON_GEN gen_perf_unary.py\n# @PERF_REPORT\n'
              '# Explicit-wait DMA overlap experiment; counter scope differs from the handwritten kernel.\n')
    after = suffix[:2] + [groups[2][2]] + groups[1] + groups[0] + suffix[11:]
    result = banner + '\n'.join(prefix) + '\n' + emit(events, end) + parts.end_marker + '\n'.join(after) + '\n'
    require(Counter(instruction_words(assembler, source, include_idle=False)) ==
            Counter(instruction_words(assembler, result, include_idle=False)), 'changed encoded non-idle operation multiset')
    before, after_checks = check_dma_ranges(source, assembler), check_dma_ranges(result, assembler)
    require(Counter(map(transfer_key, before['transfers'])) == Counter(map(transfer_key, after_checks['transfers'])),
            'changed effective DMA transfer semantics')
    return result, after_checks


def projection(text, extra_wait=0):
    """Replace DMA/control events by equal-width inert dispatch placeholders."""
    out, wait_index = [], 0
    for line in active(text):
        p, op = tokens(line), tokens(line)[0].upper()
        if op == 'DMA.WAIT':
            out.append(f'delay {extra_wait}' if wait_index == 1 and extra_wait else 'nop')
            wait_index += 1
        elif op.startswith('DMA.') or op.startswith('CSR') or op == 'SUB':
            out.append('nop')
        elif op in {'LI', 'LUI', 'ADDI', 'ECALL'}:
            out.append(line.lower())
        else:
            out.append(translate(line, to_compiler=True).strip())
    return '\n'.join(out) + '\n'


def prepare(source_path, baseline_path, assembler_path, compiler_path, output):
    assembler = load_assembler(assembler_path)
    source, baseline = read_text(source_path), read_text(baseline_path)
    require(Counter(instruction_words(assembler, source, include_idle=False)) ==
            Counter(instruction_words(assembler, baseline, include_idle=False)), 'memory baseline changed encoded operations')
    original = check_dma_ranges(source, assembler)
    output.mkdir(parents=True, exist_ok=False)
    report = dict(schema='atlas.rtlgraph.dma-schedule-experiment.v1', status='preparing',
                  inputs={name: artifact(path) for name, path in (('source', source_path), ('memory_baseline', baseline_path),
                          ('assembler', assembler_path), ('compiler', compiler_path), ('driver', Path(__file__).resolve()))},
                  original=original, cases={}, counter_scope_comparable=False,
                  scope='Move existing explicit DMA commands/waits only. Reorder output C/B/A; launch C while independent compute/stores finish. Optional B wait movement hides independent A work.',
                  limitations=['No fixed DMA latency bound; pending commands remain live until an explicit wait.',
                               'Compiler checks cover DMA-elided local LSU/VPU projections, not the native compiler DMA model.',
                               'Local timing constants are inherited; typed CIRCT checks and independent finite RTL replay evidence are separate.',
                               'Only the admitted perf_unary layout and straight-line commands are supported; no automatic wait insertion.'])
    for name, move in (('output_overlap', False), ('input_output_overlap', True)):
        text, checks = candidate(baseline, assembler, move)
        path = output / (name + '.S')
        path.write_text(text)
        active_waits = [w for w in checks['waits'] if w['prior_local_last_age']]
        require(not active_waits or move and len(active_waits) == 1 and active_waits[0]['index'] == 1,
                'unsupported wait with outstanding local work')
        bound = active_waits[0]['prior_local_last_age'] + 1 if active_waits else 0
        logs = []
        for stall in range(bound + 1):
            projected = output / f'{name}.stall-{stall}.compiler.S'
            projected.write_text(projection(text, stall))
            command = [str(compiler_path), '--check', str(projected)]
            run = subprocess.run(command, text=True, capture_output=True, env=os.environ.copy())
            logs.append(dict(stall=stall, projection=artifact(projected), command=command, stdout=run.stdout, stderr=run.stderr))
            require(run.returncode == 0, f'local projection rejected at stall {stall}: {run.stdout}{run.stderr}')
        validation = output / f'{name}.local-checks.json'
        validation.write_text(json.dumps(logs, indent=2) + '\n')
        report['cases'][name] = dict(assembly=artifact(path), dma_checks=checks, unchanged_non_idle_words=True,
                                    unchanged_transfer_semantics=True, local_projection_checks=artifact(validation),
                                    exhaustive_extra_wait_stalls=list(range(bound + 1)),
                                    stall_scope='Every relevant integer stall through prior local work completion; larger stalls cannot overlap that prior work. This is a model argument, not RTL liveness proof.')
    report['status'] = 'candidates_ready'
    (output / 'manifest.json').write_text(json.dumps(report, indent=2) + '\n')
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('source', 'memory-baseline', 'assembler', 'atlas-opt', 'output'):
        parser.add_argument('--' + name, type=Path, required=True)
    args = parser.parse_args()
    result = prepare(*(getattr(args, name).resolve() for name in ('source', 'memory_baseline', 'assembler', 'atlas_opt', 'output')))
    print(json.dumps({'status': result['status'], 'cases': list(result['cases'])}))


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError) as error:
        raise SystemExit('rtlgraph_dma_schedule: ' + str(error))

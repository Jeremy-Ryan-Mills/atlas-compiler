#!/usr/bin/env python3
"""Bind the original VPU binary/reduction probes to their numerical success path.

These probes check one BF16 element per operation with LHU/BNE. They have no
full-tensor goldens, and some failure DBG0 codes equal success. Accepting their
trace therefore requires the complete expected success path, not DBG0 alone.
This module does not generate schedules or change their checks/timing windows.
"""
from __future__ import annotations

from rtlgraph_kernel import assemble


CASES = {
    'binary': [('VADD.BF16', 64, 0x40c0), ('VSUB.BF16', 64, 0x4000),
               ('VMUL.BF16', 64, 0x4100), ('VMIN.BF16', 64, 0x4000),
               ('VMAX.BF16', 64, 0x4080)],
    'reduction': [('VREDSUM.BF16', 128, 0x4380), ('VREDMIN.BF16', 128, 0x4080),
                  ('VREDMAX.BF16', 128, 0x4080), ('VREDSUM.ROW.BF16', 37, 0x4300),
                  ('VREDMIN.ROW.BF16', 32, 0x4080), ('VREDMAX.ROW.BF16', 32, 0x4080)],
}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def expected_source(kind):
    """Explicit original instruction/operand/check contract; comments are ignored."""
    require(kind in CASES, 'unknown VPU probe kind')
    result = [f'ADDI x28, x0, {1 if kind == "binary" else 0}']
    for index, (reg, immediate) in enumerate(((0, 0x4080), (2, 0x4000))):
        result.extend(('CSRRS x24, 0xC00, x0', f'VLI.ALL {reg}, {immediate}', 'DELAY 63',
                       'CSRRS x25, 0xC00, x0', 'SUB x9, x25, x24',
                       f'ADD x26, {"x0" if index == 0 else "x26"}, x9'))
    for op, delay, expected in CASES[kind]:
        operands = '8, 0, 2' if kind == 'binary' else '8, 0'
        result.extend(('CSRRS x24, 0xC00, x0', f'{op} {operands}', f'DELAY {delay}',
                       'CSRRS x25, 0xC00, x0', 'SUB x9, x25, x24', 'ADD x26, x26, x9',
                       'LI x8, 0x20000000', 'VSTORE 8, x8, 0', 'DELAY 33',
                       'LHU x10, x8, 0', 'DELAY 2', f'LI x11, {expected}',
                       'BNE x10, x11, fail', 'ADDI x28, x28, 1'))
    result.extend(('pass:', 'CSRRW x0, 0xC11, x26', 'ADDI x1, x0, 1',
                   'CSRRW x0, 0xC10, x1', 'ECALL', 'fail:', 'CSRRW x0, 0xC10, x28', 'ECALL'))
    return '\n'.join(result) + '\n'


def branch_target(pc, word):
    immediate = ((word >> 31) << 12 | ((word >> 7) & 1) << 11 |
                 ((word >> 25) & 63) << 5 | ((word >> 8) & 15) << 1)
    if immediate & 0x1000:
        immediate -= 0x2000
    # Atlas PCs count words; its B encoder stores twice the word displacement
    # and the hardware shifts the decoded B immediate right by one.
    return pc + immediate // 2


def probe_contract(source, assembler):
    words = assemble(assembler, source)
    matches = [kind for kind in CASES if words == assemble(assembler, expected_source(kind))]
    require(len(matches) == 1, 'probe must match the original binary or reduction instruction/check contract exactly')
    kind = matches[0]
    start = assemble(assembler, 'CSRRS x24, 0xC00, x0')[0]
    end = assemble(assembler, 'CSRRS x25, 0xC00, x0')[0]
    starts = [pc for pc, word in enumerate(words) if word == start]
    ends = [pc for pc, word in enumerate(words) if word == end]
    require(len(starts) == len(ends) == len(CASES[kind]) + 2, 'probe window count mismatch')
    windows = []
    names = ['VLI.ALL 0', 'VLI.ALL 2', *[case[0] for case in CASES[kind]]]
    for index, (first, last) in enumerate(zip(starts, ends)):
        require(last == first + 3, 'probe windows must contain exactly one operation and DELAY')
        windows.append({'start_pc': first, 'operation_pc': first + 1, 'end_pc': last, 'operation': names[index]})
    success_word = assemble(assembler, 'CSRRW x0, 0xC10, x1')[0]
    fail_word = assemble(assembler, 'CSRRW x0, 0xC10, x28')[0]
    success_pc, fail_pc = words.index(success_word), words.index(fail_word)
    branches = [pc for pc, word in enumerate(words) if word & 0x707f == 0x1063]
    require(len(branches) == len(CASES[kind]), 'probe numerical-check count mismatch')
    require(all(branch_target(pc, words[pc]) == fail_pc for pc in branches), 'numerical checks must branch to failure')
    checks = [{'branch_pc': pc, 'operation': case[0], 'expected_bf16': case[2],
               'scope': 'First BF16 element of result m8, stored to VMEM and loaded by LHU'}
              for pc, case in zip(branches, CASES[kind])]
    return {'kind': kind, 'words': words, 'windows': windows, 'checks': checks,
            'success_pc': success_pc, 'failure_pc': fail_pc,
            'validation_scope': 'Original assembly numerical spot checks; no full-tensor golden or universal arithmetic proof'}


def analyze_samples(samples, contract):
    words, issues, csrs = contract['words'], [], []
    for cycle, _time, sample in samples:
        if sample['reset'] != 0:
            require(not issues, 'reset or unknown reset after probe execution began')
            continue
        fire, valid = sample['scalar.fire'], sample['csr.valid']
        require(fire in (0, 1) and valid in (0, 1), 'unknown scalar/CSR validity')
        word = None
        if fire:
            pc, word = sample['scalar.pc'], sample['scalar.instr']
            require(type(pc) is int and pc == len(issues), 'probe must follow the complete consecutive success path; a check failed or execution was skipped')
            require(pc <= contract['success_pc'] and pc < len(words) and word == words[pc],
                    'probe executed a word outside the expected successful instruction stream')
            issues.append({'cycle': cycle, 'pc': pc, 'instruction': word})
        csr_word = bool(fire and word & 0x7f == 0x73 and (word >> 12) & 7 in (1, 2, 3, 5, 6, 7))
        require(bool(valid) == csr_word, 'CSR event does not match an executed CSR instruction')
        if valid:
            values = {name: sample['csr.' + name] for name in ('addr', 'cmd', 'wdata', 'rdata')}
            require(all(type(value) is int and value >= 0 for value in values.values()), 'unknown CSR event value')
            require(values['addr'] == word >> 20, 'CSR address differs from the executed instruction')
            csrs.append({**issues[-1], **values})
    require(len(issues) == contract['success_pc'] + 1, 'incomplete probe success path')
    reads = [event for event in csrs if event['addr'] == 0xc00]
    dbg1 = [event for event in csrs if event['addr'] == 0xc11]
    dbg0 = [event for event in csrs if event['addr'] == 0xc10]
    require(len(csrs) == 2 * len(contract['windows']) + 2, 'unexpected CSR event count')
    require(len(reads) == 2 * len(contract['windows']), 'missing or extra timing windows')
    require(all(event['cmd'] == 2 and event['wdata'] == 0 for event in reads), 'cycle markers must be read-only CSRRS')
    require(len(dbg1) == 1 and dbg1[0]['cmd'] == 1, 'expected one cumulative DBG1 write')
    require(len(dbg0) == 1 and dbg0[0]['cmd'] == 1 and dbg0[0]['wdata'] == 1 and dbg0[0]['pc'] == contract['success_pc'],
            'expected the exact successful DBG0 write after every numerical check')
    require(dbg1[0]['cycle'] < dbg0[0]['cycle'], 'completion markers out of order')
    require(words[contract['success_pc'] + 1] == 0x73, 'successful marker must be followed by ECALL')
    windows, cumulative = [], 0
    for index, region in enumerate(contract['windows']):
        start, end = reads[2 * index:2 * index + 2]
        require(start['pc'] == region['start_pc'] and end['pc'] == region['end_pc'], 'timing window PC mismatch')
        require(start['cycle'] < end['cycle'] < dbg1[0]['cycle'], 'timing window events out of order')
        delta = (end['rdata'] - start['rdata']) & 0xffffffff
        require(delta > 0, 'zero probe timing window')
        cumulative = (cumulative + delta) & 0xffffffff
        windows.append({**region, 'cycle_start': start, 'cycle_end': end,
                        'csr_counter_delta': delta, 'clock_edges': end['cycle'] - start['cycle']})
    require(dbg1[0]['wdata'] == cumulative, 'DBG1 differs from cumulative timing windows')
    checks = [{**check, 'branch_cycle': issues[check['branch_pc']]['cycle'],
               'fallthrough_cycle': issues[check['branch_pc'] + 1]['cycle'], 'observed_untaken': True}
              for check in contract['checks']]
    return {'status': 'observed_numerical_spot_checks_passed', 'kind': contract['kind'],
            'executed_instruction_count': len(issues), 'windows': windows, 'checks': checks,
            'numerical_spot_check_count': len(checks), 'dbg1_write': dbg1[0], 'dbg0_write': dbg0[0],
            'metrics': {'csr_counter_delta': cumulative,
                        'first_issue_to_dbg0_edges': dbg0[0]['cycle'] - issues[0]['cycle']},
            'limitations': ['Only the first BF16 result element of each operation is checked; other rows/elements are unvalidated.',
                            'Observed BNE fallthrough validates the existing hardware-executed comparison, not an independent full-tensor arithmetic oracle.',
                            'Cumulative CSR timing omits stores, loads, comparisons, and other work between the original windows.',
                            'ECALL suppresses scalar fire; host halted/ECALL status must be checked separately.',
                            'No universal VPU timing or state-space proof; no optimized schedule was generated.']}

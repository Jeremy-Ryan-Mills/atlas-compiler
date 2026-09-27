#!/usr/bin/env python3
"""Extract a narrow experimental MXU1 profile from typed CIRCT and a checked run.

The core valid pipeline is derived structurally. The accumulator-read Boolean
function is checked exhaustively over named cutpoints. Row timing and scalar
alignment are corroborated by a finite, functionally checked execution. The
projection inherits all other atlas-opt rules; it is not a complete machine
description or a universal timing proof.
"""

import argparse
import itertools
import json
from pathlib import Path
import re
import subprocess
import sys

from rtlgraph_mxu1 import Graph, wrapper_wiring
from rtlgraph_mxu1_capture import verify_artifact
from rtlgraph_query import atlas_launch_wiring
from rtlgraph_s0 import artifact, checked_path


def require(condition, message):
    if not condition:
        raise ValueError(message)


def constant(op):
    require(op['kind'] == 'hw.constant', 'Expected constant')
    value = op['attributes']['value']
    if value in ('true', 'false'):
        return int(value == 'true')
    match = re.fullmatch(r'(-?\d+) : i(\d+)', value)
    require(match is not None, 'Unsupported integer constant')
    return int(match[1]) % (1 << int(match[2]))


def valid_pipeline(module):
    """Only unconditional i1 seq.firreg chains with a shared sync reset/clock.

    Operand roles are the pinned CIRCT 1.75 seq.firreg definition: next, clk,
    optional reset/resetValue. Enables represented by feedback muxes are rejected.
    """
    graph = Graph(module)
    start, cursor = graph.named('io_compute_valid'), graph.named('io_out_valid')
    clock, reset = graph.named('clock'), graph.named('reset')
    registers, visited = [], set()
    while cursor != start:
        require(cursor not in visited, 'Feedback in valid pipeline')
        visited.add(cursor)
        op = graph.definitions.get(cursor)
        require(op is not None and op['result_types'] == ['i1'], 'Unsupported valid pipeline value')
        if op['identity_wire'] and len(op['operands']) == 1:
            cursor = op['operands'][0]
            continue
        require(op['kind'] == 'seq.firreg' and len(op['operands']) == 4 and not op['has_regions'],
                'Valid pipeline must contain only unconditional sync-reset firregs and wires')
        require('isAsync' not in op['attributes'] and 'preset' not in op['attributes'], 'Unsupported register reset/preset')
        data, clk, rst, zero = op['operands']
        require(clk == clock and rst == reset, 'Pipeline clock/reset differs from module ports')
        require(constant(graph.definitions[zero]) == 0, 'Valid pipeline reset must be zero')
        registers.append(op)
        cursor = data
    require(1 <= len(registers) < 32, 'Unsupported pipeline depth')
    return {'status': 'unconditional_valid_shift_derived', 'latency': len(registers),
            'registers_output_to_input': registers,
            'assumptions': 'Rising edges of module clock; reset deasserted throughout the transaction. Result-valid timing only, not arithmetic equivalence.'}


def acc_read_function(module):
    graph = Graph(module)
    names = ('p0Boundary', 'acceptCompute', 'p0CmdValid', 'p0Cmd_op', 'io_cmd_bits_op')
    values = [graph.named(name) for name in names]
    require(len(set(values)) == len(names), 'Accumulator read cutpoints must have distinct SSA identities')
    types = {p['value']: p['type'] for p in module['ports'] if p['direction'] == 'input'}
    types.update((v, t) for op in module['operations'] for v, t in zip(op['results'], op['result_types']))
    require([types.get(v) for v in values] == ['i1', 'i1', 'i1', 'i3', 'i3'],
            'Unsupported accumulator read cutpoint types')
    require(types.get(graph.named('io_accComputeReadEn')) == 'i1', 'Accumulator read output must be i1')
    cuts = dict(zip(values, names))
    cone = graph.cone(graph.named('io_accComputeReadEn'), cuts)
    require(not cone['unsupported'], 'Unsupported accumulator read cone')

    def evaluate(value, assignments, active):
        if value in cuts:
            return assignments[cuts[value]]
        require(value not in active, 'Feedback in accumulator read cone')
        active = active | {value}
        op = graph.definitions.get(value)
        require(op is not None, 'Unresolved accumulator read input')
        require(len(op['results']) == 1 and not op['has_regions'], 'Unsupported accumulator read operation')
        kind = op['kind']
        if kind == 'hw.constant':
            return constant(op)
        args = [evaluate(v, assignments, active) for v in op['operands']]
        if op['identity_wire'] and len(args) == 1:
            return args[0]
        if kind == 'comb.icmp' and op['attributes'].get('predicate') == '0 : i64' and len(args) == 2:
            return int(args[0] == args[1])
        require(op['result_types'] == ['i1'], 'Unsupported Boolean result type')
        if kind in ('comb.and', 'comb.or', 'comb.xor'):
            require(all(x in (0, 1) for x in args), 'Non-Boolean operand')
            return int(all(args)) if kind == 'comb.and' else int(any(args)) if kind == 'comb.or' else sum(args) % 2
        if kind == 'comb.mux' and len(args) == 3:
            require(args[0] in (0, 1), 'Non-Boolean selector')
            return args[1] if args[0] else args[2]
        raise ValueError(f'Unsupported Boolean operation: {kind}')

    checked = 0
    for values in itertools.product(range(2), range(2), range(2), range(8), range(8)):
        boundary, accept, valid, old_op, new_op = values
        expected = int(new_op == 6) if boundary and accept else int(valid and not boundary and old_op == 6)
        actual = evaluate(graph.named('io_accComputeReadEn'), dict(zip(names, values)), set())
        require(actual == expected, f'Accumulator read guard mismatch at {values}')
        checked += 1
    return {'status': 'opcode_guard_exhaustively_matched', 'assignments_checked': checked,
            'cutpoints': names, 'cone': cone, 'overwrite_reads_accumulator': False,
            'scope': 'Combinational function over independent cutpoints; not a reachability or temporal proof. Matmul=5, MatmulAcc=6 in this pinned ISA.'}


def timing_agreement(timing, latency):
    require(timing.get('status') == 'trace_obligations_passed', 'Timing monitor did not pass')
    require(timing.get('perf', {}).get('status') == 'scalar_issue_and_engine_events_matched', 'Missing scalar issue alignment')
    transactions = timing['transactions']
    require({t['command']['op'] for t in transactions} == {'Matmul', 'MatmulAcc'}, 'Need overwrite and accumulation witnesses')
    for transaction in transactions:
        require(transaction.get('issue_to_accept_gap') == 0, 'Issue and acceptance differ')
        for field, first in (('requests', 0), ('feeds', 1), ('writes', latency + 1)):
            require(transaction[field]['ages'] == list(range(first, first + 32)), 'Observed row timing differs from profile')
        require(transaction['feed_to_write_gaps'] == [latency] * 32, 'Observed core latency differs from CIRCT')
        require(transaction['retire_age'] == latency + 32 and transaction['feed_boundary_age'] == 32,
                'Unsupported compute retirement or feed boundary')
    require(any(b['accepted_cycle'] - a['accepted_cycle'] == 32 for a, b in zip(transactions, transactions[1:])),
            'Need back-to-back compute witness')
    return {'transaction_count': len(transactions), 'issue_to_accept_gap': 0,
            'first_write_age': latency + 1, 'overwrite_acc_read_hold': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--s0-manifest', type=Path, required=True)
    parser.add_argument('--perf-manifest', type=Path, required=True)
    parser.add_argument('--timing-report', type=Path, required=True)
    parser.add_argument('--exporter', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True, help='New directory for canonical JSON and compiler projection')
    args = parser.parse_args()
    s0_path, perf_path, timing_path, exporter, output = map(checked_path,
        (args.s0_manifest, args.perf_manifest, args.timing_report, args.exporter, args.output))
    s0, perf, timing = [json.loads(p.read_text()) for p in (s0_path, perf_path, timing_path)]
    require(s0.get('status') == 'complete' and s0.get('config') == 'EE290SimConfig', 'Wrong or incomplete S0 configuration')
    require(perf.get('kind') == 'rtlgraph-perf-replay' and perf.get('status') == 'passed' and perf.get('config') == 'EE290SimConfig',
            'A passing EE290 performance replay is required')
    require(perf.get('result', {}).get('status') == 'PASS', 'Missing functional success')
    smoke = json.loads(verify_artifact(perf['inputs']['smoke_manifest']).read_text())
    require(perf['saved_fir_matches_fresh_fir'] and smoke['inputs']['fresh_fir']['sha256'] == s0['artifacts']['firrtl']['sha256'],
            'Simulator FIRRTL and selected CIRCT lineage differ')
    hw = verify_artifact(s0['artifacts']['hardware_ir'])
    verify_artifact(s0['artifacts']['firrtl'])
    verify_artifact(timing['input'])
    verify_artifact(timing['trace_header']['vcd'])
    require(timing['trace_header']['vcd']['sha256'] == perf['trace']['sha256'], 'Timing report came from another capture')
    output.mkdir(parents=True, exist_ok=False)
    command = [str(exporter), str(hw), 'ScalarCore', 'AtlasCore', 'InnerProductTreesTop', 'InnerProductTreesSequencer', 'InnerProductTrees']
    typed_path = output / 'typed.json'
    with typed_path.open('w') as stream:
        subprocess.run(command, stdout=stream, check=True)
    typed = json.loads(typed_path.read_text())
    require(not typed['missing_modules'], 'Missing hardware modules')
    modules = {m['name']: m for m in typed['modules']}
    wrapper = wrapper_wiring(modules['InnerProductTreesTop'])
    launch = atlas_launch_wiring(modules)
    require(wrapper['status'] == launch['status'] == 'direct_wiring_confirmed', 'Unsupported command/core wiring')
    pipeline = valid_pipeline(modules['InnerProductTrees'])
    read_guard = acc_read_function(modules['InnerProductTreesSequencer'])
    agreement = timing_agreement(timing, pipeline['latency'])
    report = {'schema_version': 1, 'kind': 'atlas-partial-mxu1-profile', 'config': 'EE290SimConfig',
              'status': 'structural_facts_and_finite_trace_agree',
              'scope': 'Experimental partial profile. Unmentioned access ages, logical reservations, shared resources and instructions inherit the built-in npu_model model.',
              'inputs': {name: artifact(path) for name, path in (('s0', s0_path), ('perf', perf_path), ('timing', timing_path),
                        ('exporter', exporter), ('hardware_ir', hw), ('driver', checked_path(__file__)))},
              'export_command': command, 'typed': artifact(typed_path), 'valid_pipeline': pipeline,
              'accumulator_read_guard': read_guard, 'wrapper_wiring': wrapper, 'launch_wiring': launch,
              'trace_agreement': agreement,
              'compiler_overrides': {'first_write_age': agreement['first_write_age'], 'overwrite_acc_read_hold': 0},
              'limitations': ['No bounded or unbounded temporal safety proof.',
                              'Register-valid recurrence establishes result-valid timing, not arithmetic equivalence.',
                              'Request/feed ages and scalar alignment validated in the recorded execution only.',
                              'The cached simulator source-to-binary build linkage remains unverified.',
                              'Other profiles and resources remain inherited; each candidate schedule requires RTL validation.']}
    report_path = output / 'profile.json'
    report_path.write_text(json.dumps(report, indent=2) + '\n')
    projection = output / 'atlas-mxu1.profile'
    projection.write_text('schema=atlas-mxu1-profile-v1\nconfig=EE290SimConfig\n'
        f'source_ir_sha256={artifact(hw)["sha256"]}\nevidence_sha256={artifact(report_path)["sha256"]}\n'
        f'first_write_age={agreement["first_write_age"]}\noverwrite_acc_read_hold=0\n')
    print(json.dumps({'status': report['status'], 'profile': str(report_path), 'compiler_projection': str(projection),
                      'first_write_age': agreement['first_write_age'], 'guard_assignments': read_guard['assignments_checked']}))


if __name__ == '__main__':
    try:
        main()
    except (ValueError, KeyError, subprocess.CalledProcessError) as error:
        print(f'MXU1 profile extraction failed: {error}', file=sys.stderr)
        raise SystemExit(1)

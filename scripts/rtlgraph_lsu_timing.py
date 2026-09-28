#!/usr/bin/env python3
"""Derive conditional LSU row timing from checked typed control recurrences.

The LSU request trajectory is internal. Destination-write and release ages are
conditional on a stated one-cycle external response contract; this query does
not prove SRAM arbitration, data, or latency, nor change the compiler model.
"""
from __future__ import annotations

import argparse
import functools
import itertools
import json
import operator
from pathlib import Path
import re

from rtlgraph_dma import direct, operation, register, slice_fact
from rtlgraph_lsu import analyze as analyze_local
from rtlgraph_mxu1 import Graph
from rtlgraph_mxu1_capture import verify_artifact
from rtlgraph_mxu1_profile import constant, require
from rtlgraph_s0 import artifact


class TypedCone:
    """Fail-closed finite-width combinational evaluator, including LUT arrays.

Arrays here are explicit hw.array_create values, not memories or state. Their
operand ordering is reversed by hw.array_get's index convention.
"""
    def __init__(self, module, root, cuts):
        self.graph = Graph(module)
        self.types = {p['value']: p['type'] for p in module['ports'] if p['direction'] == 'input'}
        self.types.update((v, t) for op in module['operations'] for v, t in zip(op['results'], op['result_types']))
        self.cuts = dict(cuts)
        require(len(set(cuts.values())) == len(cuts), 'Aliased LSU timing cutpoints')
        self.cut_widths = {name: self.width(value) for name, value in cuts.items()}
        self.steps = []
        positions = {value: i for i, value in enumerate(cuts.values())}
        active = set()

        def visit(value):
            if value in positions: return positions[value]
            require(value not in active, 'Feedback in LSU timing cone')
            active.add(value)
            op = self.graph.definitions.get(value)
            require(op is not None and Graph.traversable(op), 'Unresolved LSU timing input/state')
            kind = op['kind']
            args = [visit(v) for v in op['operands']]
            extra = None
            if kind == 'hw.array_create':
                match = re.fullmatch(r'!hw.array<([1-9][0-9]*)xi([1-9][0-9]*)>', self.types.get(value, ''))
                require(match is not None, 'Unsupported timing array type')
                count, width = map(int, match.groups())
                require(len(args) == count and count & (count - 1) == 0 and
                        all(self.width(v) == width for v in op['operands']), 'Unsupported timing array shape')
                size, extra = None, (count, width)
            elif kind == 'hw.array_get':
                require(len(args) == 2, 'Unsupported timing array index')
                match = re.fullmatch(r'!hw.array<([1-9][0-9]*)xi([1-9][0-9]*)>', self.types.get(op['operands'][0], ''))
                require(match is not None, 'Unsupported timing array source')
                count, width = map(int, match.groups())
                size = self.width(value)
                require(size == width and (1 << self.width(op['operands'][1])) == count,
                        'Timing array index must cover exactly its entries')
            else:
                size = self.width(value)
                widths = [self.width(v) for v in op['operands']]
                if kind == 'hw.constant':
                    require(not args, 'Timing constant has operands')
                    extra = constant(op)
                elif op['identity_wire'] and widths == [size]: kind = 'wire'
                elif kind in ('comb.and', 'comb.or', 'comb.xor', 'comb.add'):
                    require(args and all(w == size for w in widths), 'Unsupported timing arithmetic widths')
                elif kind == 'comb.shl':
                    require(widths == [size, size], 'Unsupported timing shift widths')
                elif kind == 'comb.replicate':
                    require(len(widths) == 1 and size % widths[0] == 0, 'Unsupported timing replicate widths')
                    extra = widths[0]
                elif kind == 'comb.mux': require(widths == [1, size, size], 'Unsupported timing mux widths')
                elif kind == 'comb.icmp':
                    require(len(widths) == 2 and widths[0] == widths[1] and size == 1 and
                            op['attributes'].get('predicate') in ('0 : i64', '1 : i64'), 'Unsupported timing comparison')
                    extra = int(op['attributes']['predicate'] == '1 : i64')
                elif kind == 'comb.concat':
                    require(args and sum(widths) == size, 'Unsupported timing concat')
                    extra = widths
                elif kind == 'comb.extract':
                    match = re.fullmatch(r'([0-9]+) : i32', op['attributes'].get('lowBit', ''))
                    require(len(args) == 1 and match is not None and
                            int(match[1]) + size <= widths[0], 'Unsupported timing slice')
                    extra = int(match[1])
                else: raise ValueError('Unsupported LSU timing operation: ' + kind)
            positions[value] = len(cuts) + len(self.steps)
            self.steps.append((kind, args, size, extra))
            active.remove(value)
            return positions[value]
        self.root = visit(root)
        self.evidence = self.graph.cone(root, {value: name for name, value in cuts.items()})

    def width(self, value):
        match = re.fullmatch(r'i([1-9][0-9]*)', self.types.get(value, ''))
        require(match is not None and int(match[1]) <= 64, 'Unsupported LSU timing bit-vector type')
        return int(match[1])

    def __call__(self, assignments):
        values = [assignments[name] for name in self.cuts]
        require(all(type(value) is int and 0 <= value < 1 << self.cut_widths[name]
                    for name, value in zip(self.cuts, values)), 'LSU timing input out of range')
        for kind, args, width, extra in self.steps:
            data = [values[i] for i in args]
            if kind == 'hw.constant': value = extra
            elif kind == 'wire': value = data[0]
            elif kind == 'hw.array_create': value = tuple(reversed(data))
            elif kind == 'hw.array_get': value = data[0][data[1]]
            elif kind == 'comb.mux': value = data[1] if data[0] else data[2]
            elif kind == 'comb.icmp': value = int(data[0] == data[1]) ^ extra
            elif kind == 'comb.extract': value = data[0] >> extra
            elif kind == 'comb.shl': value = 0 if data[1] >= width else data[0] << data[1]
            elif kind == 'comb.replicate':
                value = sum(data[0] << offset for offset in range(0, width, extra))
            elif kind == 'comb.concat':
                value = 0
                for part, size in zip(data, extra): value = (value << size) | part
            else:
                value = functools.reduce({'comb.and': operator.and_, 'comb.or': operator.or_,
                                          'comb.xor': operator.xor, 'comb.add': operator.add}[kind], data)
            values.append(value if width is None else value & ((1 << width) - 1))
        return values[self.root]


def cone(module, root, names):
    graph = Graph(module)
    return TypedCone(module, root, {name: graph.named(name) for name in names})


def state_machine(module, kind):
    graph = Graph(module)
    state, count, issue = kind + 'State', kind + 'Counter', 'issue' + kind.title() + 'Cmd'
    state_reg, count_reg = register(module, state, 2), register(module, count, 6)
    names = (state, count, issue)
    next_state = cone(module, state_reg['operands'][0], names)
    next_count = cone(module, count_reg['operands'][0], names)
    request_name = 'io_vmemVecRead_valid' if kind == 'vload' else 'io_mregReadReq_valid'
    request = cone(module, graph.named(request_name), (state,))
    checked = 0
    for s, c, accepted in itertools.product(range(4), range(64), range(2)):
        inputs = dict(zip(names, (s, c, accepted)))
        expected_state = (1 if accepted else 0) if s == 0 else (2 if c == 31 else 1) if s == 1 else 0 if s == 2 else 3
        expected_count = 0 if s == 0 and accepted else (c + 1) % 64 if s == 1 else c
        require(next_state(inputs) == expected_state and next_count(inputs) == expected_count,
                'LSU state/counter recurrence mismatch: ' + kind)
        require(request({state: s}) == int(s == 1), 'LSU request depends on unsupported stall or state')
        checked += 1
    return dict(kind=kind, assignments_checked=checked, state_register=state_reg, counter_register=count_reg,
                state_cone=next_state.evidence, counter_cone=next_count.evidence, request_cone=request.evidence,
                laws=['IDLE with command enters RUN and resets counter to zero',
                      'RUN requests one row per edge and increments counter modulo64',
                      'RUN at counter31 enters DRAIN; DRAIN enters IDLE on the next edge',
                      'No response/backpressure input is present in these next-state or request cones'])


def row_pipeline(module):
    """Check row identity recurrences structurally, including hold enables."""
    graph = Graph(module)
    facts = []
    for kind in ('vload', 'vstore'):
        line = operation(graph, graph.named(kind + 'LineAddr'), 'comb.add')
        require(line['result_types'] == ['i16'] and len(line['operands']) == 2, 'Unsupported LSU line sum')
        base = direct(module, graph.named(kind + 'VmemBase'), line['operands'][0], 'captured base in ' + kind + ' line sum')
        extended = operation(graph, line['operands'][1], 'comb.concat')
        require(extended['result_types'] == ['i16'] and len(extended['operands']) == 2,
                'Unsupported LSU counter extension')
        zero = operation(graph, extended['operands'][0], 'hw.constant')
        require(zero['result_types'] == ['i10'] and constant(zero) == 0, 'LSU counter must zero extend')
        counter = direct(module, graph.named(kind + 'Counter'), extended['operands'][1], 'counter in line sum')
        facts.append(dict(kind=kind, line_add=line, extension=extended, zero=zero, paths=[base, counter]))
    facts.append(slice_fact(module, graph.named('vloadIssuedRow'), graph.named('vloadCounter'), 0, 5))
    facts.append(slice_fact(module, graph.named('io_mregReadReq_bits_row'), graph.named('vstoreCounter'), 0, 5))
    for name, width, enable, source in (
            ('vloadRespRow', 5, 'vloadIssueRead', 'vloadIssuedRow'),
            ('vloadWriteRow', 5, None, 'vloadRespRow'),
            ('vstoreRespLineAddr_d', 16, 'vstoreIssueRead', 'vstoreLineAddr'),
            ('vstoreRespLineAddr_q', 16, 'vstoreRespPending_d', 'vstoreRespLineAddr_d')):
        saved = register(module, name, width)
        mux = operation(graph, saved['operands'][0], 'comb.mux')
        require(mux['result_types'] == [f'i{width}'] and len(mux['operands']) == 3, 'Unsupported LSU row capture')
        paths = [direct(module, graph.named(source), mux['operands'][1], name + ' incoming row'),
                 direct(module, graph.named(name), mux['operands'][2], name + ' held row')]
        if enable:
            paths.append(direct(module, graph.named(enable), mux['operands'][0], name + ' capture enable'))
        else:
            guard = cone(module, mux['operands'][0], ('io_vmemVecReadData_valid', 'vloadRespPending'))
            for response, pending in itertools.product(range(2), repeat=2):
                require(guard(dict(io_vmemVecReadData_valid=response, vloadRespPending=pending)) == response * pending,
                        'LSU write-row capture guard mismatch')
            paths.append(dict(guard=guard.evidence, assignments_checked=4))
        facts.append(dict(register=saved, mux=mux, paths=paths))
    facts.append(direct(module, graph.named('vloadWriteRow'), graph.named('io_mregWriteReq_bits_row'), 'VLOAD write row output'))
    return facts


CONTROL_REGS = dict(vloadState=2, vloadCounter=6, vloadRespPending=1, vloadRespRow=5,
                    vloadWritePending=1, vloadWriteRow=5, vstoreState=2, vstoreCounter=6,
                    vstoreRespPending_d=1, vstoreRespPending_q=1, mregReadRespValid_q=1,
                    vstoreRespLineAddr_d=16, vstoreRespLineAddr_q=16)
INPUTS = ('issueVloadCmd', 'issueVstoreCmd', 'io_vmemVecReadData_valid', 'io_mregReadResp_valid',
          'vloadVmemBase', 'vstoreVmemBase')
OUTPUTS = ('io_vmemVecRead_valid', 'vloadIssuedRow', 'io_mregWriteReq_valid', 'io_mregWriteReq_bits_row',
           'io_mregReadReq_valid', 'io_mregReadReq_bits_row', 'io_vmemVecWrite_valid',
           'io_vloadBusy', 'io_vstoreBusy', 'io_activeMregWrite_valid', 'io_activeMregRead_valid')


def temporal_model(module):
    graph = Graph(module)
    cuts = tuple(CONTROL_REGS) + INPUTS
    registers = {name: register(module, name, width) for name, width in CONTROL_REGS.items()}
    next_values = {name: cone(module, op['operands'][0], cuts) for name, op in registers.items()}
    outputs = {name: cone(module, graph.named(name), cuts) for name in OUTPUTS}
    return next_values, outputs


def trajectory(model, initial_counter=0, response_latency=1, bound=37):
    """Evaluate actual typed recurrence; assumptions enter at named interfaces.

No timing constants are asserted here. The response contract is checked
explicitly before a run can be used to derive write/release timing.
"""
    require(response_latency == 1, 'Conditional LSU timing requires the one-cycle response contract')
    require(0 <= initial_counter < 64 and bound >= 36, 'Unsupported LSU timing bound or initial counter')
    next_values, outputs = model
    current = {name: 0 for name in CONTROL_REGS}
    current['vloadCounter'] = current['vstoreCounter'] = initial_counter
    history = []
    for age in range(bound):
        inputs = dict(issueVloadCmd=int(age == 0), issueVstoreCmd=int(age == 0),
                      io_vmemVecReadData_valid=history[-1]['io_vmemVecRead_valid'] if history else 0,
                      io_mregReadResp_valid=history[-1]['io_mregReadReq_valid'] if history else 0,
                      vloadVmemBase=0, vstoreVmemBase=0)
        environment = current | inputs
        events = {name: evaluate(environment) for name, evaluate in outputs.items()}
        history.append(dict(age=age, **current, **inputs, **events))
        current = {name: evaluate(environment) for name, evaluate in next_values.items()}
    return history


def summarize(history, kind):
    request = 'io_vmemVecRead_valid' if kind == 'vload' else 'io_mregReadReq_valid'
    response = 'io_vmemVecReadData_valid' if kind == 'vload' else 'io_mregReadResp_valid'
    write = 'io_mregWriteReq_valid' if kind == 'vload' else 'io_vmemVecWrite_valid'
    row = 'vloadIssuedRow' if kind == 'vload' else 'io_mregReadReq_bits_row'
    destination = 'io_mregWriteReq_bits_row' if kind == 'vload' else 'vstoreRespLineAddr_q'
    requests = [dict(age=edge['age'], row=edge[row]) for edge in history if edge[request]]
    writes = [dict(age=edge['age'], row=edge[destination]) for edge in history if edge[write]]
    require([e['row'] for e in requests] == list(range(32)) and [e['row'] for e in writes] == list(range(32)),
            'Conditional LSU trajectory does not transfer exactly one ordered tensor')
    busy = 'io_' + kind + 'Busy'
    occupied = [edge['age'] for edge in history if edge[busy]]
    require(occupied and not history[-1][busy], 'LSU timing trajectory did not drain within its bound')
    first_idle = next(edge['age'] for edge in history if edge['age'] > occupied[0] and not edge[busy])
    return dict(requests=requests, response_ages=[edge['age'] for edge in history if edge[response]],
                writes=writes, first_not_busy_age=first_idle,
                age_zero='LSU decoded command-valid edge; existing AtlasCore identity wiring links scalar LSU command',
                note='Busy is zero just before the issue edge; the command itself is still a launch reservation.')


def analyze(module):
    machines = [state_machine(module, kind) for kind in ('vload', 'vstore')]
    rows = row_pipeline(module)
    model = temporal_model(module)
    history = trajectory(model)
    baseline = {kind: summarize(history, kind) for kind in ('vload', 'vstore')}
    # All 6-bit stale counter states are possible while idle; accepted command
    # resets them. This check does not assume a fresh reset for every transfer.
    for old_counter in range(64):
        trace = trajectory(model, initial_counter=old_counter)
        require({kind: summarize(trace, kind) for kind in ('vload', 'vstore')} == baseline,
                'Conditional LSU timing depends on stale idle counter')
    return dict(status='typed_conditional_lsu_timing_derived', state_machines=machines,
                row_pipeline=rows, temporal=dict(bound_edges=37, idle_counter_cases_checked=64,
                                                events=baseline, canonical_trajectory=history),
                assumptions=['Age zero has IDLE path state and no pending response/write stages; one admitted decoded command is asserted at that edge.',
                             'Reset remains low during the bounded transfer; clock follows the checked common seq.firreg clock.',
                             'No second command is issued on that path during the transfer.',
                             'Each asserted VMEM/MREG source-row request receives exactly one valid response at the next sampled edge; no stalls, drops, spurious responses, or reordering.',
                             'VMEM bases are aligned, in range and nonwrapping; no conflicting physical bank/port traffic prevents access.',
                             'The simultaneous load/store traces are independent control compositions, not permission to issue two scalar commands together or alias their physical banks.'],
                evidence_scope=dict(internal='Exhaustive finite-width state/counter transitions and request predicates; structural row capture and pending-stage recurrences.',
                                    conditional='Bounded composition of extracted recurrences with an assumed one-cycle source-response contract derives write and release ages.',
                                    unproved='SRAM response latency and arbitration, data payloads, complete address/bank routing, physical collision freedom, scalar decoding/issue legality, and whole-kernel scheduling safety.'),
                compiler_change='No model override or shorter numerical latency; these results corroborate the existing LSU profile under explicit conditions.')


def prepare(evidence_path, output):
    local = json.loads(evidence_path.read_text())
    require(local['schema'] == 'atlas.rtlgraph.lsu-local-functions.v1' and local['config'] == 'EE290SimConfig',
            'Wrong LSU evidence schema/configuration')
    for record in local['inputs'].values(): verify_artifact(record)
    typed_path = verify_artifact(local['typed'])
    typed = json.loads(typed_path.read_text())
    modules = {m['name']: m for m in typed['modules']}
    require(not typed['missing_modules'] and len(typed['modules']) == 2 and set(modules) == {'LSU', 'AtlasCore'},
            'Missing or ambiguous LSU timing modules')
    fresh = analyze_local(modules)
    require(all(local.get(key) == value for key, value in fresh.items()), 'LSU local evidence differs from fresh analysis')
    report = analyze(modules['LSU'])
    inputs = dict(evidence=artifact(evidence_path), typed=artifact(typed_path),
                  hardware_ir=local['inputs']['hardware_ir'], driver=artifact(Path(__file__).resolve()))
    for name in ('rtlgraph_dma.py', 'rtlgraph_lsu.py', 'rtlgraph_mxu1.py', 'rtlgraph_mxu1_capture.py',
                 'rtlgraph_mxu1_profile.py', 'rtlgraph_query.py', 'rtlgraph_s0.py', 'rtlgraph_vpu.py'):
        inputs[name] = artifact(Path(__file__).with_name(name).resolve())
    report.update(schema='atlas.rtlgraph.lsu-conditional-timing.v1', config='EE290SimConfig', inputs=inputs)
    output.mkdir(parents=True, exist_ok=False)
    path = output / 'lsu-timing.json'
    path.write_text(json.dumps(report, indent=2) + '\n')
    return dict(status=report['status'], output=str(path),
                timings={kind: dict(first_request=facts['requests'][0]['age'], last_request=facts['requests'][-1]['age'],
                                    first_write=facts['writes'][0]['age'], last_write=facts['writes'][-1]['age'],
                                    first_not_busy=facts['first_not_busy_age'])
                         for kind, facts in report['temporal']['events'].items()})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--lsu-evidence', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(prepare(args.lsu_evidence.resolve(), args.output.resolve())))


if __name__ == '__main__':
    try: main()
    except (OSError, ValueError, KeyError) as error:
        raise SystemExit(f'rtlgraph_lsu_timing: {error}')

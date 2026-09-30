#!/usr/bin/env python3
"""Check the actual AtlasCore -> XLU -> MREG timing boundary in typed CIRCT."""
import argparse
import copy
import hashlib
import json
from pathlib import Path
import subprocess

from rtlgraph_xlu import Bits, Circuit, integer, require, sha
from rtlgraph_instruction_timing import ControlCircuit


class Graph:
    def __init__(self, module):
        self.module = module
        self.ops = {r: op for op in module['operations'] for r in op['results']}
        self.ports = {p['name']: p['value'] for p in module['ports']}
        self.instances = {o['instance']['name']: o for o in module['operations'] if o['kind'] == 'hw.instance'}

    def strip(self, value):
        while value in self.ops and self.ops[value]['kind'] == 'hw.wire':
            value = self.ops[value]['operands'][0]
        return value

    def pin(self, instance, name, direction):
        op = self.instances[instance]
        names = op['instance'][direction + '_names']
        return self.strip(op['operands' if direction == 'input' else 'results'][names.index(name)])

    def cone(self, value):
        pending, seen = [value], set()
        while pending:
            v = pending.pop()
            if v in seen:
                continue
            seen.add(v)
            if v in self.ops:
                pending.extend(self.ops[v]['operands'])
        return seen


def routing(atlas, scalar):
    g = Graph(atlas)
    require(g.instances['xlu']['instance']['module'] == 'XluEngine', 'Wrong XLU instance')
    require(g.instances['mreg']['instance']['module'] == 'MregFile', 'Wrong MREG instance')
    links = []

    def link(source_instance, source, dest_instance, dest):
        require(g.pin(source_instance, source, 'output') == g.pin(dest_instance, dest, 'input'),
                f'Non-direct connection: {source_instance}.{source} -> {dest_instance}.{dest}')
        links.append(f'{source_instance}.{source} -> {dest_instance}.{dest}')

    for s, d in [('valid', 'valid'), ('bits_srcBank', 'bits_srcMregId'), ('bits_dstBank', 'bits_dstMregId')]:
        link('scalar', 'io_xluCmd_' + s, 'xlu', 'io_cmd_' + d)
    for direction, fields in [('ReadReq', ['valid', 'bits_mregId', 'bits_row']),
                              ('WriteReq', ['valid', 'bits_mregId', 'bits_row', 'bits_data'])]:
        for field in fields:
            link('xlu', 'io_mreg' + direction + '_' + field, 'mreg', 'io_xlu' + direction + '_' + field)
    for field in ['valid', 'bits']:
        link('mreg', 'io_xluReadResp_' + field, 'xlu', 'io_mregReadResp_' + field)
    for direction in ['Read', 'Write']:
        for field in ['valid', 'bits']:
            link('xlu', 'io_activeMreg' + direction + '_' + field,
                 'mregTracker', 'io_' + ('readers' if direction == 'Read' else 'writers') + '_4_' + field)
        link('mregTracker', 'io_' + direction.lower() + 'Busy', 'scalar', 'io_mreg' + direction + 'Busy')
    scalar_graph = Graph(scalar)
    cone = scalar_graph.cone(scalar_graph.ports['io_xluCmd_valid'])
    for name in ['io_mregReadBusy', 'io_mregWriteBusy']:
        require(scalar_graph.ports[name] not in cone, f'Launch depends on {name}')
    require(g.pin('xlu', 'io_busy', 'output') not in set(g.instances['scalar']['operands']),
            'XLU busy feeds frontend')
    return {'direct_links': links, 'mreg_busy_interlocks_xlu_launch': False,
            'xlu_busy_interlocks_frontend': False}


def tracker_mapping(module):
    inputs = {p['name']: 0 for p in module['ports'] if p['direction'] == 'input'}
    circuit = ControlCircuit({'modules': [module]}, 'MregBankTracker', ['io_readBusy', 'io_writeBusy'], set(inputs))
    require(not circuit.registers, 'Tracker adds unexpected response stage')
    for bank in range(64):
        values = dict(inputs, io_readers_4_valid=1, io_readers_4_bits=bank, io_writers_4_valid=1, io_writers_4_bits=bank)
        require(circuit.cycle(values) == {'io_readBusy': 1 << bank, 'io_writeBusy': 1 << bank},
                'XLU tracker mapping differs from architectural register ID')
    return {'checks': 64, 'latency': 0, 'mapping': 'Each active XLU register ID sets the corresponding architectural busy bit.'}


def frontend_assertions(scalar):
    """Evaluate actual hazard predicates, with instruction fire/decode as cuts.

    No-stall signals are not permission to violate software-scheduling assertions.
    The selected predicates remain actual ScalarCore logic, including reset gating.
    """
    module = copy.deepcopy(scalar)
    graph = Graph(module)
    inputs = {'clock', 'reset', 'io_mregReadBusy', 'io_mregWriteBusy'}
    cuts = {('', value): name for name, value in zip(graph.instances['decoder']['instance']['output_names'],
                                                    graph.instances['decoder']['results'])}
    fire = [o['results'][0] for o in module['operations'] if o['kind'] == 'hw.wire' and o['attributes'].get('name') == 's1_fire']
    require(len(fire) == 1, 'Missing unique instruction fire signal')
    cuts['', fire[0]] = 'instruction_fire'
    names = []
    for name, message in [('raw_assertion', 'MREG RAW hazard'), ('war_waw_assertion', 'MREG WAR/WAW hazard')]:
        errors = [o for o in module['operations'] if o['kind'] == 'sv.error' and message in o['attributes']['message']]
        require(len(errors) == 1, f'Missing {message} assertion')
        candidates = [o['operands'][0] for o in module['operations'] if o['kind'] == 'sv.if' and o['location'] == errors[0]['location']
                      and graph.ports['io_mregWriteBusy'] in graph.cone(o['operands'][0])]
        require(len(candidates) == 1, f'Ambiguous {message} predicate')
        module['ports'].append({'name': name, 'value': candidates[0], 'direction': 'output', 'type': 'i1'})
        names.append(name)
    circuit = ControlCircuit({'modules': [module]}, 'ScalarCore', names, inputs, cuts=cuts)
    require(not circuit.registers, 'Hazard assertion acquired temporal state')
    base = {name: 0 for name in inputs | set(cuts.values())}
    base['instruction_fire'] = 1
    # Every architectural bank, with and without the relevant pending activity.
    for bank in range(64):
        store = dict(base, io_decoded_is_lsu=1, io_decoded_lsu_cmd=2, io_decoded_vd=bank)
        for busy in (0, 1 << bank, 1 << ((bank + 1) % 64)):
            result = circuit.cycle(dict(store, io_mregWriteBusy=busy))
            require(result['raw_assertion'] == int(bool(busy & (1 << bank))), 'VSTORE RAW assertion changed')
        overwrite = dict(base, io_decoded_xlu_cmd=1, io_decoded_vd=bank, io_decoded_vs1=(bank + 1) % 64)
        require(circuit.cycle(dict(overwrite, io_mregReadBusy=1 << bank))['war_waw_assertion'] == 1,
                'XLU source-overwrite reservation changed')
        load = dict(base, io_decoded_is_lsu=1, io_decoded_lsu_cmd=1, io_decoded_vd=bank)
        require(circuit.cycle(dict(load, io_mregReadBusy=1 << bank))['war_waw_assertion'] == 0,
                'VLOAD source-reuse exception changed')
        require(circuit.cycle(dict(load, io_mregWriteBusy=1 << bank))['war_waw_assertion'] == 1,
                'VLOAD WAW assertion changed')
    return {'checks': 384, 'vstore_requires_no_pending_destination_write': True,
            'xlu_destination_requires_no_pending_read_or_write': True,
            'vload_destination_checks_writes_only': True,
            'implication': 'XLU to VSTORE must wait until age 66 despite numerically valid row overlap at gap 34.',
            'scope': 'Exact combinational assertion predicates; decoded fields and instruction-fire are explicit cut inputs.'}


class Mreg:
    """Execute actual arbitration/response cones; model only seq.firmem semantics.

    Same-address read-under-write produces symbolic undefined data, never old/new data.
    Diagnostic SV assertions are not executed; port collisions are explicit test inputs.
    """
    def __init__(self, module):
        lowered = copy.deepcopy(module)
        self.reads, self.writes, self.memories = [], [], {}
        self.memory, self.read_data = {}, {}
        graph = Graph(module)
        clock = graph.ports['clock']
        memories = {o['results'][0]: o for o in module['operations'] if o['kind'] == 'seq.firmem'}
        require(len(memories) == 32, 'Expected 32 physical MREG banks')
        for value, op in memories.items():
            attrs = op['attributes']
            require(op['result_types'] == ['!seq.firmem<64 x 256>'], 'Unsupported MREG dimensions')
            require(all(integer(attrs[k]) == expected for k, expected in
                        [('readLatency', 1), ('writeLatency', 1), ('ruw', 0), ('wuw', 1)]),
                    'Unsupported SRAM timing or collision semantics')
            self.memories[value] = attrs['name']
        kept = []
        for op in lowered['operations']:
            kind = op['kind']
            if kind == 'seq.firmem':
                continue
            if kind in ('seq.firmem.read_port', 'seq.firmem.write_port'):
                memory, address, clk, enable, *data = op['operands']
                require(memory in memories and clk == clock, 'Unsupported memory/clock')
                prefix = op['id']
                for suffix, value in [('address', address), ('enable', enable)] + ([('data', data[0])] if data else []):
                    lowered['ports'].append({'name': prefix + '_' + suffix, 'value': value, 'direction': 'output'})
                if kind.endswith('read_port'):
                    require(not data and op['result_types'] == ['i256'], 'Unsupported read port')
                    value = op['results'][0]
                    lowered['arguments'].append({'id': value, 'type': 'i256'})
                    lowered['ports'].append({'name': prefix, 'value': value, 'direction': 'input'})
                    self.reads.append((prefix, memory))
                    self.read_data[prefix] = 0
                else:
                    require(len(data) == 1 and op['attributes']['operandSegmentSizes'] == 'array<i32: 1, 1, 1, 1, 1, 0>',
                            'Unsupported write mask')
                    self.writes.append((prefix, memory))
                continue
            if kind in ('sv.always', 'sv.if', 'sv.ifdef', 'sv.error', 'sv.fatal', 'sv.macro.ref', 'seq.from_clock'):
                continue
            kept.append(op)
        require(len(self.reads) == len(self.writes) == 32, 'Expected 1R1W per physical bank')
        require({m for _, m in self.reads} == {m for _, m in self.writes} == set(memories), 'Missing memory port')
        lowered['operations'] = kept
        self.circuit = Circuit(lowered)
        self.inputs = {p['name']: 0 for p in module['ports'] if p['direction'] == 'input'}
        # An idle reset edge also makes the unreset return-port tags concrete;
        # SRAM contents remain arbitrary and response valid stays deasserted.
        _, reset_advance = self.cycle({'reset': 1})
        reset_advance()

    def cell(self, memory, row):
        key = memory, row
        if key not in self.memory:
            self.memory[key] = Bits(tuple(('memory', self.memories[memory], row, bit) for bit in range(256)))
        return self.memory[key]

    def cycle(self, inputs):
        output, registers_advance = self.circuit.cycle({**self.inputs, **inputs, **self.read_data})

        def advance():
            writes = {m: (output(p + '_address'), output(p + '_data')) for p, m in self.writes if output(p + '_enable')}
            reads = {}
            for p, m in self.reads:
                if output(p + '_enable'):
                    row = output(p + '_address')
                    reads[p] = (Bits(tuple(('undefined-ruw', self.memories[m], row, bit) for bit in range(256)))
                                if m in writes and writes[m][0] == row else self.cell(m, row))
                else:
                    reads[p] = 0
            registers_advance()
            self.read_data = reads
            for m, (row, data) in writes.items():
                self.memory[m, row] = data
        return output, advance


def connected_case(xlu_module, mreg_module, src=0, dst=1, second_age=None, competing_age=None, overwrite_age=None):
    xlu, mreg = Circuit(xlu_module), Mreg(mreg_module)
    reads, responses, writes, active_read, active_write, accepted = [], [], [], [], [], []
    resp_valid, resp_bits = 0, 0
    source_rows = [Bits(tuple(('input', row, bit) for bit in range(256))) for row in range(32)]
    memory_id = next(v for v, name in mreg.memories.items() if name == f'banks_{src % 32}')
    for row, data in enumerate(source_rows):
        mreg.memory[memory_id, (src // 32) * 32 + row] = data
    limit = 140 if second_age is not None else 70
    for age in range(limit):
        launch = age == 0 or age == second_age
        output, xadvance = xlu.cycle({'clock': 0, 'reset': 0, 'io_cmd_valid': int(launch), 'io_cmd_bits_op': 0,
                                    'io_cmd_bits_srcMregId': src, 'io_cmd_bits_dstMregId': dst,
                                    'io_mregReadResp_valid': resp_valid, 'io_mregReadResp_bits': resp_bits})
        if launch and not output('io_busy'):
            accepted.append(age)
        if output('io_activeMregRead_valid'):
            require(output('io_activeMregRead_bits') == src, 'Wrong tracked source')
            active_read.append(age)
        if output('io_activeMregWrite_valid'):
            require(output('io_activeMregWrite_bits') == dst, 'Wrong tracked destination')
            active_write.append(age)
        inputs = {}
        for direction, fields in [('ReadReq', ['valid', 'bits_mregId', 'bits_row']),
                                  ('WriteReq', ['valid', 'bits_mregId', 'bits_row', 'bits_data'])]:
            for field in fields:
                inputs['io_xlu' + direction + '_' + field] = output('io_mreg' + direction + '_' + field)
        if output('io_mregReadReq_valid'):
            reads.append(age)
        if output('io_mregWriteReq_valid'):
            row = output('io_mregWriteReq_bits_row')
            expected = Bits(tuple(('input', col, row * 8 + bit) for col in range(32) for bit in range(8)))
            require(output('io_mregWriteReq_bits_data') == expected, 'Connected transpose payload mismatch')
            writes.append(age)
        if age == competing_age:
            inputs.update(io_lsuReadReq_valid=1, io_lsuReadReq_bits_mregId=src ^ 32, io_lsuReadReq_bits_row=0)
        if age == overwrite_age:
            inputs.update(io_lsuWriteReq_valid=1, io_lsuWriteReq_bits_mregId=src, io_lsuWriteReq_bits_row=31,
                          io_lsuWriteReq_bits_data=0)
        mout, madvance = mreg.cycle(inputs)
        require(mout('io_xluReadResp_valid') == resp_valid, 'Unexpected combinational response dependency')
        if resp_valid:
            responses.append(age)
        xadvance()
        madvance()
        next_output, _ = mreg.cycle({})
        resp_valid, resp_bits = next_output('io_xluReadResp_valid'), next_output('io_xluReadResp_bits')
    return {'accepted_ages': accepted, 'read_ages': reads, 'response_ages': responses, 'write_ages': writes,
            'active_read_ages': active_read, 'active_write_ages': active_write}


def derive(document):
    modules = {m['name']: m for m in document['modules']}
    connectivity = routing(modules['AtlasCore'], modules['ScalarCore'])
    assertions = frontend_assertions(modules['ScalarCore'])
    tracker = tracker_mapping(modules['MregBankTracker'])
    base = connected_case(modules['XluEngine'], modules['MregFile'])
    require(base['read_ages'] == list(range(1, 33)) and base['response_ages'] == list(range(2, 34)) and
            base['write_ages'] == list(range(34, 66)), 'Unexpected connected timing')
    require(base['active_read_ages'] == list(range(1, 34)) and base['active_write_ages'] == list(range(1, 66)),
            'Unexpected logical reservation signals')
    for src, dst in [(31, 63), (32, 32), (63, 0)]:
        require(connected_case(modules['XluEngine'], modules['MregFile'], src, dst) == base, 'Operand-dependent timing')
    consecutive = connected_case(modules['XluEngine'], modules['MregFile'], second_age=66)
    require(consecutive['accepted_ages'] == [0, 66] and consecutive['write_ages'] == list(range(34, 66)) + list(range(100, 132)),
            'Earliest consecutive launch failed')
    busy = connected_case(modules['XluEngine'], modules['MregFile'], second_age=65)
    require(busy == base, 'Busy launch was not ignored')
    conflict = connected_case(modules['XluEngine'], modules['MregFile'], competing_age=1)
    require(conflict['write_ages'] == [] and len(conflict['response_ages']) == 31,
            'Expected conflicting higher-priority read to lose an XLU response')
    overwrite = connected_case(modules['XluEngine'], modules['MregFile'], overwrite_age=33)
    require(overwrite == base, 'Overwrite after last request changed captured input')
    try:
        connected_case(modules['XluEngine'], modules['MregFile'], overwrite_age=32)
    except ValueError as exc:
        require('payload mismatch' in str(exc), 'Unexpected boundary failure')
    else:
        raise ValueError('Same-address simultaneous read/write must not claim defined data')
    return {'connectivity': connectivity, 'frontend_assertions': assertions, 'tracker': tracker,
            'timing': {'read_age': 1, 'response_age': 2, 'write_age': 34, 'first_free_age': 66},
            'logical_signal_last_ages': {'read': 33, 'write': 65},
            'memory': {'physical_banks': 32, 'rows_per_bank': 64, 'row_bits': 256, 'read_latency': 1,
                       'write_latency': 1, 'read_under_write': 'undefined', 'ports_per_bank': '1R1W'},
            'checks': ['Symbolic payload routing through actual MREG response arbitration.',
                       'Low/high register mapping, in-place transpose, and distinct destinations.',
                       'Consecutive launch at age 66 succeeds; age 65 is ignored.',
                       'A same-bank competing LSU read drops an XLU response; no retry or automatic stall.',
                       'Datapath-only check: source row 31 overwrite at age 33 preserves captured input; age 32 read/write is undefined.']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--query', type=Path, required=True)
    parser.add_argument('--hardware-ir', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    names = ['XluEngine', 'MregFile', 'AtlasCore', 'ScalarCore', 'MregBankTracker']
    typed = subprocess.check_output([str(args.query.resolve()), str(args.hardware_ir.resolve()), *names])
    facts = derive(json.loads(typed))
    report = {'schema': 'atlas.rtlgraph.xlu-connected.v1', 'config': 'EE290SimConfig',
              'status': 'connected_typed_execution_and_structural_checks',
              'inputs': {'hardware_ir': {'sha256': sha(args.hardware_ir)}, 'typed_query': {'sha256': sha(args.query)},
                         'typed_output': {'sha256': hashlib.sha256(typed).hexdigest()},
                         'extractor': {'sha256': sha(__file__)}, 'evaluator': {'sha256': sha(Path(__file__).with_name('rtlgraph_xlu.py'))},
                         'control_evaluator': {'sha256': sha(Path(__file__).with_name('rtlgraph_instruction_timing.py'))}},
              **facts,
              'assumptions': ['Reset initialized control; no reset during execution.',
                              'Compiler prevents conflicting physical-bank ports and same-address read/write.',
                              'XLU launched only while idle; surrounding commands satisfy ScalarCore scheduling assertions.'],
              'limitations': ['Finite concrete control cases with arbitrary symbolic payload bits, not unbounded formal proof.',
                              'Scalar instruction decoding is outside this check; command routing starts at ScalarCore output.',
                              'Numerical row overlap does not permit violating frontend logical reservation assertions.']}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(facts['timing'], sort_keys=True))


if __name__ == '__main__':
    main()

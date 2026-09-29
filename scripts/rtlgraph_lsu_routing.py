#!/usr/bin/env python3
"""Check conditional LSU address, payload routing and SRAM write acceptance."""
import argparse
import copy
import itertools
import json
from pathlib import Path
import re
import subprocess

from rtlgraph_dma import direct, operation, register, slice_fact
from rtlgraph_lsu_timing import TypedCone, summarize
from rtlgraph_lsu_response import READERS, analyze as analyze_response, check, memories
from rtlgraph_mxu1 import Graph
from rtlgraph_mxu1_capture import verify_artifact
from rtlgraph_mxu1_profile import constant, require
from rtlgraph_query import find_instance, instance_value
from rtlgraph_s0 import artifact


class BitRoute:
    """Exact bit provenance after fixing control inputs; no payload sampling."""
    def __init__(self, module):
        self.graph = Graph(module)
        self.types = {p['value']: p['type'] for p in module['ports'] if p['direction'] == 'input'}
        self.types.update((v, t) for op in module['operations'] for v, t in zip(op['results'], op['result_types']))
        self.visited = set()

    def width(self, value):
        match = re.fullmatch(r'i([1-9][0-9]*)', self.types.get(value, ''))
        require(match is not None and int(match[1]) <= 256, 'Unsupported LSU routing type')
        return int(match[1])

    def symbolic(self, value):
        return tuple((value, bit) for bit in range(self.width(value)))

    def evaluate(self, root, sources, controls):
        require(not (set(sources) & set(controls)), 'Overlapping LSU routing cutpoints')
        cache = {v: self.symbolic(v) for v in sources}
        for value, number in controls.items():
            width = self.width(value)
            require(type(number) is int and 0 <= number < 1 << width, 'Invalid routing control')
            cache[value] = tuple((number >> bit) & 1 for bit in range(width))
        active = set()

        def visit(value):
            if value in cache: return cache[value]
            require(value not in active, 'Cyclic LSU routing cone')
            active.add(value)
            op = self.graph.definitions.get(value)
            if op is None or not Graph.traversable(op):
                require(value in self.types, 'Unknown LSU routing source')
                cache[value] = self.symbolic(value)
                active.remove(value)
                return cache[value]
            self.visited.add(op['id'])
            args, kind, width = op['operands'], op['kind'], self.width(value)
            widths = [self.width(v) for v in args]
            if op['identity_wire']:
                require(widths == [width], 'Malformed routing wire')
                result = visit(args[0])
            elif kind == 'hw.constant':
                require(not args, 'Malformed routing constant')
                result = tuple((constant(op) >> bit) & 1 for bit in range(width))
            elif kind == 'comb.mux':
                require(widths == [1, width, width], 'Malformed routing mux')
                guard = visit(args[0])
                require(guard in ((0,), (1,)), 'Payload-dependent routing guard')
                result = visit(args[1] if guard == (1,) else args[2])
            elif kind == 'comb.concat':
                require(args and sum(widths) == width, 'Malformed routing concat')
                result = tuple(bit for arg in reversed(args) for bit in visit(arg))
            elif kind == 'comb.extract':
                match = re.fullmatch(r'([0-9]+) : i32', op['attributes'].get('lowBit', ''))
                require(len(args) == 1 and match is not None and int(match[1]) + width <= widths[0],
                        'Malformed routing extract')
                start = int(match[1])
                result = visit(args[0])[start:start + width]
            elif kind in ('comb.and', 'comb.or', 'comb.xor'):
                require(args and all(w == width for w in widths), 'Malformed routing Boolean gate')
                operands = [visit(arg) for arg in args]
                def combine(bits):
                    if kind == 'comb.and' and 0 in bits: return 0
                    if kind == 'comb.or' and 1 in bits: return 1
                    if all(bit in (0, 1) for bit in bits):
                        return sum(bits) % 2 if kind == 'comb.xor' else int((all if kind == 'comb.and' else any)(bits))
                    remaining = tuple(bit for bit in bits if bit != (1 if kind == 'comb.and' else 0))
                    return remaining[0] if len(remaining) == 1 else (kind, remaining)
                result = tuple(combine(bits) for bits in zip(*operands))
            else:
                raise ValueError('Unsupported LSU routing operation: ' + kind)
            require(len(result) == width, 'LSU routing width mismatch')
            cache[value] = result
            active.remove(value)
            return result
        return visit(root)

    def prove(self, root, source, assignments, law, expected=None):
        self.visited.clear()
        controls = list(assignments)
        require(controls, 'Empty LSU routing control domain')
        expected = self.symbolic(source) if expected is None else expected
        for assignment in controls:
            require(self.evaluate(root, [source], assignment) == expected, 'LSU route mismatch: ' + law)
        return dict(law=law, root=root, source=source, width=len(expected),
                    control_assignments_checked=len(controls), operations=sorted(self.visited))


def one_hot_controls(graph, names, selected):
    return {graph.named(name): int(i == selected) for i, name in enumerate(names)}


def captured(module, name, width, source, controls, expected, reset=True):
    graph = Graph(module)
    saved = register(module, name, width, reset=reset)
    mux = operation(graph, saved['operands'][0], 'comb.mux')
    require(mux['result_types'] == [f'i{width}'] and len(mux['operands']) == 3, 'Unsupported LSU payload capture')
    return dict(register=saved, guard=check(module, mux['operands'][0], controls,
                [range(1 << (2 if n.endswith('State') else 1)) for n in controls], expected, name + ' capture guard'),
                source=direct(module, graph.named(source), mux['operands'][1], name + ' captured value'),
                held=direct(module, graph.named(name), mux['operands'][2], name + ' held value'))


def lsu_routes(module):
    graph = Graph(module)
    facts = []
    for kind in ('vload', 'vstore'):
        for suffix, width, source in (('MregId', 6, 'io_cmd_bits_mregBank'), ('VmemBase', 16, 'io_cmd_bits_vmemLineAddr')):
            facts.append(captured(module, kind + suffix, width, source, (kind + 'State', 'issue' + kind.title() + 'Cmd'),
                                  lambda state, issue: state == 0 and issue, reset=False))
    for name, source, controls, expected in (
            ('vloadWriteData', 'io_vmemVecReadData_bits', ('io_vmemVecReadData_valid', 'vloadRespPending'), lambda valid, pending: valid and pending),
            ('mregReadRespBits_q', 'io_mregReadResp_bits', ('io_mregReadResp_valid',), lambda valid: valid)):
        facts.append(captured(module, name, 256, source, controls, expected))
    for source, output in (('vloadMregId', 'io_mregWriteReq_bits_mregId'),
                           ('vstoreMregId', 'io_mregReadReq_bits_mregId'),
                           ('vloadWriteData', 'io_mregWriteReq_bits_data'),
                           ('mregReadRespBits_q', 'io_vmemVecWrite_bits_data'),
                           ('vloadMregId', 'io_activeMregWrite_bits'),
                           ('vstoreMregId', 'io_activeMregRead_bits')):
        facts.append(direct(module, graph.named(source), graph.named(output), 'LSU selected ' + output))
    for source, output in (('vloadLineAddr', 'io_vmemVecRead'), ('vstoreRespLineAddr_q', 'io_vmemVecWrite')):
        facts.extend([slice_fact(module, graph.named(output + '_bits_bankIdx'), graph.named(source), 13, 3),
                      slice_fact(module, graph.named(output + '_bits_bankAddr'), graph.named(source), 0, 13)])
    return facts


def mreg_routes(module):
    graph = Graph(module)
    banks = memories(module, 32, '!seq.firmem<64 x 256>')
    decode = [check(module, graph.named(f'writeBankOHs_{i}'),
                    ('io_' + reader.replace('Read', 'Write') + '_valid',
                     'io_' + reader.replace('Read', 'Write') + '_bits_mregId'),
                    (range(2), range(64)), lambda valid, reg: (1 << (reg % 32)) if valid else 0,
                    reader.replace('Read', 'Write') + ': valid write selects physical bank mregId[4:0]')
              for i, reader in enumerate(READERS)]
    rows = []
    for direction in ('read', 'write'):
        rows.append(check(module, graph.named(direction + 'PhysRows_6'),
                          (f'io_lsu{direction.title()}Req_bits_mregId', f'io_lsu{direction.title()}Req_bits_row'),
                          (range(64), range(32)), lambda reg, row: (reg // 32) * 32 + row,
                          direction + ' physical row = mregId[5] concatenated with row[4:0]'))
    result, route = [], BitRoute(module)
    for bank, memory in enumerate(banks):
        facts = []
        for direction in ('read', 'write'):
            names = [f'{direction}Hits_{bank}_{i}' if bank else f'{direction}Hits_{i}' for i in range(8)]
            if direction == 'write':
                facts.extend(slice_fact(module, graph.named(name), graph.named(f'writeBankOHs_{i}'), bank, 1)
                             for i, name in enumerate(names))
            controls = one_hot_controls(graph, names, 6)
            port = memory['read_port'] if direction == 'read' else next(o for o, _ in graph.uses[memory['memory']['results'][0]] if o['kind'] == 'seq.firmem.write_port')
            if direction == 'write':
                require(not port['has_regions'] and not port['results'] and not port['result_types'] and
                        len(port['operands']) == 5 and port['operands'][2] == graph.named('clock') and
                        port['attributes'] == {'operandSegmentSizes': 'array<i32: 1, 1, 1, 1, 1, 0>'},
                        'Unsupported MREG SRAM write port')
                facts.append(route.prove(port['operands'][3], graph.named('io_lsuWriteReq_bits_data'),
                                                   [controls], 'MREG sole LSU write enables SRAM', expected=(1,)))
                facts.append(route.prove(port['operands'][4], graph.named('io_lsuWriteReq_bits_data'),
                                                   [controls], 'MREG sole LSU write preserves all payload bits'))
            facts.append(route.prove(port['operands'][1], graph.named(direction + 'PhysRows_6'),
                                               [controls], 'MREG sole LSU ' + direction + ' selects physical row'))
        result.append(dict(bank=bank, facts=facts))
    response_names = [f'respHits_6_{i}' for i in range(32)]
    responses = [route.prove(graph.named('io_lsuReadResp_bits'), memory['read_port']['results'][0],
                  [one_hot_controls(graph, response_names, bank)], 'MREG response preserves selected SRAM payload')
                 for bank, memory in enumerate(banks)]
    return dict(write_decode=decode, physical_rows=rows, banks=result, responses=responses)


def vmem_routes(module):
    graph = Graph(module)
    banks = memories(module, 6, '!seq.firmem<8192 x 256, mask 32>', True)
    result, route = [], BitRoute(module)
    for bank, memory in enumerate(banks):
        names = ['accessSel_leaf' + (f'_{bank * 8 + i}' if bank * 8 + i else '') + '_valid' for i in range(8)]
        port, facts = memory['read_port'], []
        for direction, selected in (('Read', 3), ('Write', 1)):
            controls = []
            for lower in itertools.product(range(2), repeat=7 - selected):
                controls.append(dict(zip((graph.named(n) for n in names), [0] * selected + [1] + list(lower))))
            source = graph.named('io_lsuVec' + direction + '_bits_bankAddr')
            facts.append(route.prove(port['operands'][1], source, controls, 'VMEM selected vector address reaches SRAM'))
            if direction == 'Write':
                source = graph.named('io_lsuVecWrite_bits_data')
                facts.append(route.prove(port['operands'][4], source, controls, 'VMEM vector write preserves all payload bits'))
                facts.append(route.prove(port['operands'][3], source, controls, 'VMEM selected vector write enables SRAM', expected=(1,)))
                facts.append(route.prove(port['operands'][5], source, controls, 'VMEM selected vector write enables write mode', expected=(1,)))
                facts.append(route.prove(port['operands'][6], source, controls, 'VMEM vector write enables every byte', expected=(1,) * 32))
        result.append(dict(bank=bank, facts=facts))
    response_names = [f'lsuVecRespSel_leaves_{i}_valid' for i in range(6)]
    responses = [route.prove(graph.named('io_lsuVecReadData_bits'), memory['read_port']['results'][0],
                  [one_hot_controls(graph, response_names, bank)], 'VMEM response preserves selected SRAM byte order')
                 for bank, memory in enumerate(banks)]
    return dict(banks=result, responses=responses)


def wrapper(module):
    instances = {name: find_instance(module, name) for name in ('lsu', 'mreg', 'vmem')}
    wires = []
    for target, lsu_port, memory_port, fields in (
            ('mreg', 'io_mregReadReq', 'io_lsuReadReq', ('bits_row', 'bits_mregId')),
            ('mreg', 'io_mregWriteReq', 'io_lsuWriteReq', ('bits_row', 'bits_mregId', 'bits_data')),
            ('vmem', 'io_vmemVecRead', 'io_lsuVecRead', ('bits_bankIdx', 'bits_bankAddr')),
            ('vmem', 'io_vmemVecWrite', 'io_lsuVecWrite', ('bits_bankIdx', 'bits_bankAddr', 'bits_data'))):
        for field in fields:
            wires.append(direct(module, instance_value(instances['lsu'], lsu_port + '_' + field, 'output'),
                                instance_value(instances[target], memory_port + '_' + field, 'input'), 'LSU request -> ' + target + ' ' + field))
    for target, source, sink in (('mreg', 'io_lsuReadResp_bits', 'io_mregReadResp_bits'),
                                ('vmem', 'io_lsuVecReadData_bits', 'io_vmemVecReadData_bits')):
        wires.append(direct(module, instance_value(instances[target], source, 'output'),
                            instance_value(instances['lsu'], sink, 'input'), target + ' response payload -> LSU'))
    return wires


def busy_extensions(module):
    """Recognize an optional synchronous tail without altering request geometry."""
    normalized = copy.deepcopy(module)
    graph, clean = Graph(module), Graph(normalized)
    tails = {}
    for kind, active in (('vload', 'Write'), ('vstore', 'Read')):
        busy = graph.definitions[graph.named(kind + 'Busy')]
        require(busy['identity_wire'] and len(busy['operands']) == 1, 'Unsupported LSU busy alias')
        joined = graph.definitions[busy['operands'][0]]
        if joined['kind'] != 'comb.or' or len(joined['operands']) != 2:
            continue
        candidates = [value for value in joined['operands']
                      if graph.definitions.get(value, {}).get('kind') == 'seq.firreg']
        if len(candidates) != 1:
            continue
        delayed = candidates[0]
        tail = graph.definitions[delayed]
        base = next(value for value in joined['operands'] if value != delayed)
        name = tail['attributes'].get('name')
        require(name is not None, 'Unnamed LSU busy tail')
        saved = register(module, name, 1)
        path = direct(module, base, saved['operands'][0], 'LSU busy tail captures base busy')
        owners = [direct(module, graph.named(kind + 'Busy'), graph.named(output), 'LSU extended busy ownership')
                  for output in ('io_' + kind + 'Busy', 'io_activeMreg' + active + '_valid')]
        evaluate = TypedCone(module, graph.named(kind + 'Busy'), dict(base=base, previous=delayed))
        for first, second in itertools.product(range(2), repeat=2):
            require(evaluate(dict(base=first, previous=second)) == (first | second), 'LSU busy tail must preserve current busy')
        clean.definitions[clean.named(kind + 'Busy')]['operands'][0] = base
        tails[kind] = dict(register=saved, base_value=base, capture=path, owner_aliases=owners, output_cone=evaluate.evidence)
    return normalized, tails


def compose_busy_tails(timing, module, tails):
    history = timing['temporal']['canonical_trajectory']
    graph = Graph(module)
    for kind, tail in tails.items():
        previous = 0
        evaluate = TypedCone(module, graph.named(kind + 'Busy'),
                             dict(base=tail['base_value'], previous=tail['register']['results'][0]))
        for edge in history:
            base = edge['io_' + kind + 'Busy']
            value = evaluate(dict(base=base, previous=previous))
            edge['io_' + kind + 'Busy'] = value
            edge['io_activeMreg' + ('Write' if kind == 'vload' else 'Read') + '_valid'] = value
            previous = base
        timing['temporal']['events'][kind] = summarize(history, kind)
    timing['busy_tails'] = tails
    if tails:
        timing['assumptions'].append('Optional synchronous busy-tail registers start clear; no reset during transfer.')
        timing['evidence_scope']['busy_tail'] = 'Checked baseBusy OR previous(baseBusy), with common synchronous clock/reset and matching active-MREG ownership; bounded composition retains the unchanged 32-row request/write trajectory.'


def analyze(modules):
    normalized, tails = busy_extensions(modules['LSU'])
    response = analyze_response(modules | {'LSU': normalized})
    compose_busy_tails(response['lsu_timing'], modules['LSU'], tails)
    response.update(status='typed_conditional_lsu_routing_derived', routing=dict(
        lsu=lsu_routes(modules['LSU']), MregFile=mreg_routes(modules['MregFile']),
        Vmem=vmem_routes(modules['Vmem']), wrapper=wrapper(modules['AtlasCore'])),
        operand_capture='issue', routing_contract=dict(
            payload='Exact bit provenance through selected mux/concat/extract routes, not sampled payload values; includes LSU capture/hold registers.',
            mreg='Bank=mregId[4:0], physical row={mregId[5],row[4:0]}; sole LSU reader/writer selects requested row and payload.',
            vmem='Bank=line[15:13], address=line[12:0]; aligned 32-line transfers stay within one of six 8192-line banks.',
            writes='MREG writes are accepted with no other writer to that physical bank. VMEM vector writes enable all 32 bytes when no LSU scalar write targets that bank; all lower-priority arbitration combinations are checked.',
            composition='One-cycle SRAM response and LSU row/data capture associate each requested row with its age3..34 destination write. SRAM storage semantics are those of the checked seq.firmem ports.'),
        remaining_assumptions=[
            'Transfer starts idle with empty pending, response-valid and optional busy-tail stages; reset stays low and no second command enters the same path.',
            'The decoded command is admitted at age zero; scalar decoding, instruction-to-command delay and frontend issue legality are not proved here.',
            'Software satisfies physical bank/port exclusions throughout each transfer; priority selection alone does not permit conflicting requests or hardware assertion violations.',
            'VMEM base is 32-line aligned, in range and nonwrapping; buffers use bank=line[15:13]. Unknown compiler addresses require these runtime obligations.',
            'MREG same-row simultaneous read/write visibility, complete kernel safety and source-to-cached-simulator lineage remain outside this proof.',
            'Evidence is local finite-domain checks, exact bit routing and bounded recurrence composition, not an unbounded proof or circt-bmc run.'])
    return response


def prepare(evidence_path, output):
    local = json.loads(evidence_path.read_text())
    require(local['schema'] == 'atlas.rtlgraph.lsu-local-functions.v1' and local['config'] == 'EE290SimConfig',
            'Wrong LSU routing evidence schema/configuration')
    require(not output.exists(), 'Output must be a new evidence directory')
    for record in local['inputs'].values(): verify_artifact(record)
    hardware, exporter = (verify_artifact(local['inputs'][name]) for name in ('hardware_ir', 'exporter'))
    inputs = dict(evidence=artifact(evidence_path), hardware_ir=artifact(hardware), exporter=artifact(exporter), driver=artifact(Path(__file__).resolve()))
    for name in ('rtlgraph_lsu_response.py', 'rtlgraph_lsu_timing.py', 'rtlgraph_lsu.py', 'rtlgraph_dma.py',
                 'rtlgraph_mxu1.py', 'rtlgraph_mxu1_capture.py', 'rtlgraph_mxu1_profile.py', 'rtlgraph_query.py', 'rtlgraph_s0.py', 'rtlgraph_vpu.py'):
        inputs[name] = artifact(Path(__file__).with_name(name).resolve())
    output.mkdir(parents=True)
    typed_path = output / 'typed.json'
    command = [str(exporter), str(hardware), 'LSU', 'AtlasCore', 'MregFile', 'Vmem']
    with typed_path.open('w') as stream: subprocess.run(command, stdout=stream, check=True)
    typed = json.loads(typed_path.read_text())
    modules = {m['name']: m for m in typed['modules']}
    require(not typed['missing_modules'] and len(typed['modules']) == 4 and set(modules) == {'LSU', 'AtlasCore', 'MregFile', 'Vmem'},
            'Missing or ambiguous LSU routing modules')
    report = analyze(modules)
    require(all(local.get(key) == value for key, value in report['lsu_local'].items()), 'LSU local evidence differs from fresh analysis')
    report.update(schema='atlas.rtlgraph.lsu-routing.v1', config='EE290SimConfig', inputs=inputs, typed=artifact(typed_path), export_command=command)
    for record in inputs.values(): verify_artifact(record)
    path = output / 'lsu-routing.json'
    path.write_text(json.dumps(report, indent=2) + '\n')
    return dict(status=report['status'], output=str(path), operand_capture=report['operand_capture'])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--lsu-evidence', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(prepare(args.lsu_evidence.resolve(), args.output.resolve())))


if __name__ == '__main__':
    try: main()
    except (OSError, ValueError, KeyError) as error:
        raise SystemExit(f'rtlgraph_lsu_routing: {error}')

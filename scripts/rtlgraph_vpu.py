#!/usr/bin/env python3
"""Check VPU issue/resource cutpoints in pinned typed CIRCT, without cycle ages."""
from __future__ import annotations

import argparse
import itertools
import json
from pathlib import Path
import re
import subprocess

from rtlgraph_mxu1 import Graph
from rtlgraph_mxu1_capture import verify_artifact
from rtlgraph_mxu1_profile import constant, require
from rtlgraph_query import find_instance, identity_chain, instance_value
from rtlgraph_s0 import artifact, checked_path


# Source-correlated VectorIO.scala enum labels. Numeric cutpoints and the mask
# are independently checked below; labels are not recovered from an opcode name.
OPS = ('add', 'sub', 'mul', 'rcp', 'sqrt', 'sin', 'cos', 'tanh', 'log', 'exp', 'exp2',
       'square', 'cube', 'rsum', 'csum', 'fp8', 'fp8pack', 'fp8unpack', 'relu',
       'rmax', 'rmin', 'cmax', 'cmin', 'pairmax', 'pairmin', 'mov',
       'vliOne', 'vliCol', 'vliRow', 'vliAll')
DOUBLE = frozenset((0, 1, 2, 13, 19, 20, 23, 24))
ROW = frozenset((13, 19, 20))
GROUPS = ({0, 1, 13}, {9, 10}, {5, 6}, {11, 12}, {23, 21}, {24, 22}, {26, 27, 28, 29})
ALL_BUSY = ((1 << 30) - 1) << 1


class Cone:
    """Compile a small checked SSA cone once; evaluate finite-width assignments."""
    def __init__(self, module, root, cuts, widths, result_width):
        self.graph = Graph(module)
        types = {p['value']: p['type'] for p in module['ports'] if p['direction'] == 'input'}
        types.update((v, t) for op in module['operations'] for v, t in zip(op['results'], op['result_types']))

        def width(value):
            match = re.fullmatch(r'i([1-9][0-9]*)', types.get(value, ''))
            require(match is not None and int(match[1]) <= 256, 'Unsupported bit-vector type')
            return int(match[1])

        require(len(set(cuts)) == len(cuts) and [width(v) for v in cuts] == widths,
                'Unsupported cutpoint identities or widths')
        require(width(root) == result_width, 'Unsupported output width')
        self.widths, self.steps = widths, []
        positions = {v: i for i, v in enumerate(cuts)}
        active = set()

        def visit(value):
            if value in positions:
                return positions[value]
            require(value not in active, 'Feedback in combinational cone')
            active.add(value)
            op = self.graph.definitions.get(value)
            require(op is not None and Graph.traversable(op), 'Unsupported operation or unresolved input')
            size = width(value)
            arguments = [visit(v) for v in op['operands']]
            sizes = [width(v) for v in op['operands']]
            kind, extra = op['kind'], None
            if kind == 'hw.constant':
                require(not arguments, 'Constant has operands')
                extra = constant(op)
            elif op['identity_wire'] and sizes == [size]:
                kind = 'wire'
            elif kind in ('comb.and', 'comb.or', 'comb.xor'):
                require(arguments and all(w == size for w in sizes), 'Unsupported logical widths')
            elif kind == 'comb.mux':
                require(sizes == [1, size, size], 'Unsupported mux widths')
            elif kind == 'comb.icmp':
                require(len(sizes) == 2 and sizes[0] == sizes[1] and size == 1
                        and op['attributes'].get('predicate') in ('0 : i64', '1 : i64'), 'Unsupported comparison')
                extra = int(op['attributes']['predicate'] == '1 : i64')
            elif kind == 'comb.concat':
                require(arguments and sum(sizes) == size, 'Unsupported concat widths')
                extra = sizes
            else:
                raise ValueError('Unsupported combinational operation: ' + kind)
            positions[value] = len(cuts) + len(self.steps)
            self.steps.append((kind, arguments, (1 << size) - 1, extra))
            active.remove(value)
            return positions[value]
        self.root = visit(root)
        self.evidence = self.graph.cone(root, {v: f'cut{i}' for i, v in enumerate(cuts)})

    def __call__(self, values):
        require(len(values) == len(self.widths) and all(type(x) is int and 0 <= x < (1 << w)
                for x, w in zip(values, self.widths)), 'Cutpoint assignment out of range')
        data = list(values)
        for kind, args, mask, extra in self.steps:
            if kind == 'hw.constant':
                out = extra
            elif kind == 'wire':
                out = data[args[0]]
            elif kind == 'comb.mux':
                out = data[args[1] if data[args[0]] else args[2]]
            elif kind == 'comb.icmp':
                out = int(data[args[0]] == data[args[1]]) ^ extra
            elif kind == 'comb.concat':
                out = 0
                for index, size in zip(args, extra): out = (out << size) | data[index]
            else:
                out = data[args[0]]
                for index in args[1:]:
                    if kind == 'comb.and': out &= data[index]
                    elif kind == 'comb.or': out |= data[index]
                    else: out ^= data[index]
            data.append(out & mask)
        return data[self.root]


def named_cone(module, root, names, widths, result_width):
    graph = Graph(module)
    return Cone(module, graph.named(root), [graph.named(n) for n in names], widths, result_width)


def shared(a, b):
    return a == b or any(a in group and b in group for group in GROUPS)


def single_busy(active):
    return sum(1 << (op + 1) for op in range(30) if op in DOUBLE or shared(op, active))


def expected_busy(state, done1, done2, op1, op2):
    if state == 0: return 0
    if state == 1:
        if done1 and done2: return 0
        if done1: return single_busy(op2)
        if done2: return single_busy(op1)
        return ALL_BUSY
    if state == 2: return 0 if done1 and (done2 or op1 not in ROW) else ALL_BUSY
    return ALL_BUSY


def issue_guard(module):
    names = ('state', 'done1', 'done2', 'inst1', 'inst2')
    cone = named_cone(module, 'io_out_issueBusy', names, [2, 1, 1, 5, 5], 31)
    checked = 0
    for values in itertools.product(range(4), range(2), range(2), range(32), range(32)):
        require(cone(values) == expected_busy(*values), 'VPU issue-busy function mismatch')
        checked += 1
    matrix = []
    for active, name in enumerate(OPS):
        state = 2 if active in DOUBLE else 1
        mask = cone((state, 0, 1, active, 0))
        matrix.append({'active_opcode': active, 'active_name': name, 'state_cutpoint': state,
                       'allowed_incoming_opcodes': [op for op in range(30) if not (mask >> (op + 1) & 1)]})
    return {'assignments_checked': checked, 'cutpoints': list(names), 'cone': cone.evidence,
            'encoding': [{'opcode': i, 'issue_busy_bit': i + 1, 'source_label': op} for i, op in enumerate(OPS)],
            'both_read_slots': sorted(DOUBLE), 'shared_logic_groups': [sorted(g) for g in GROUPS],
            'one_active_instruction_overlap': matrix,
            'scope': 'Exact local issueBusy function for every i2/i1/i1/i5/i5 assignment, including unreachable/invalid resident encodings. Pair matrix assumes the stated state cutpoint and one unfinished instruction; it does not prove that state invariant or a slot lifetime.'}


def final_write_release(module, slot):
    names = (f'writeDone{slot}', f'io_in_dataOutFire{slot}', f'writeCounter{slot}', f'writeLim{slot}')
    cone = named_cone(module, f'done{slot}', names, [1, 1, 7, 7], 1)
    checked = 0
    for done, fire, counter, limit in itertools.product(range(2), range(2), range(128), range(128)):
        require(cone((done, fire, counter, limit)) == int(done or (fire and counter == limit)),
                'VPU slot-release predicate mismatch')
        checked += 1
    return {'slot': slot, 'assignments_checked': checked, 'cutpoints': list(names),
            'function': 'writeDone || (dataOutFire && writeCounter == writeLimit)', 'cone': cone.evidence,
            'scope': 'A final result pulse can make done true in the same combinational cycle. No fixed issue age, counter progression, result latency, or reachable write-limit value is established.'}


def wire(module, source, target, description, width):
    types = {p['value']: p['type'] for p in module['ports'] if p['direction'] == 'input'}
    types.update((v, t) for op in module['operations'] for v, t in zip(op['results'], op['result_types']))
    require(types.get(source) == types.get(target) == f'i{width}', 'Unsupported VPU wire width: ' + description)
    path = identity_chain(module, source, target)
    require(path is not None, 'VPU direct wiring mismatch: ' + description)
    return {'connection': description, 'path': path}


def wrapper_facts(modules):
    top, engine = modules['VectorEngineTop'], modules['VectorEngine']
    tg, eg = Graph(top), Graph(engine)
    core, fsm = find_instance(top, 'core'), find_instance(engine, 'fsm')
    require(core['instance']['module'] == 'VectorEngine' and fsm['instance']['module'] == 'VectorFSM',
            'Unsupported VPU module binding')
    wires = [wire(top, tg.named('io_cmd_valid'), instance_value(core, 'io_inst_valid', 'input'), 'cmd.valid -> engine.inst.valid', 1),
             wire(engine, eg.named('io_inst_valid'), instance_value(fsm, 'io_in_instFire', 'input'), 'engine.inst.valid -> fsm.instFire', 1),
             wire(engine, instance_value(fsm, 'io_out_issueBusy', 'output'), eg.named('io_issueBusy'), 'fsm.issueBusy -> engine.issueBusy', 31),
             wire(top, instance_value(core, 'io_issueBusy', 'output'), tg.named('io_issueBusy'), 'engine.issueBusy -> top.issueBusy', 31)]
    read = {f'{port}.{field}': instance_value(core, f'io_read{port}_' + ('valid' if field == 'valid' else 'bits_' + field), 'output')
            for port in (1, 2) for field in ('valid', 'bank', 'row')}
    same = Cone(top, tg.named('sameReadBank'), [read['1.valid'], read['2.valid'], read['1.bank'], read['2.bank']], [1, 1, 6, 6], 1)
    for a, b, x, y in itertools.product(range(2), range(2), range(64), range(64)):
        require(same((a, b, x, y)) == int(a and b and x == y), 'VPU same-register predicate mismatch')
    mirrored = Cone(top, tg.named('mirroredReadReq'), [tg.named('sameReadBank'), read['1.row'], read['2.row']], [1, 5, 5], 1)
    for same_bank, a, b in itertools.product(range(2), range(32), range(32)):
        require(mirrored((same_bank, a, b)) == int(same_bank and a == b), 'VPU mirrored-row predicate mismatch')
    suppress = Cone(top, tg.named('io_mregReadReq1_valid'), [read['2.valid'], tg.named('mirroredReadReq')], [1, 1], 1)
    for valid, mirror in itertools.product(range(2), repeat=2):
        require(suppress((valid, mirror)) == int(valid and not mirror), 'VPU duplicate request suppression mismatch')
    for field, target, width in (('valid', 'valid', 1), ('bank', 'bits_mregId', 6), ('row', 'bits_row', 5)):
        wires.append(wire(top, read['1.' + field], tg.named('io_mregReadReq0_' + target), 'first read ' + field, width))
    delayed = tg.definitions[tg.named('mirroredReadReq_d')]
    require(delayed['kind'] == 'seq.firreg' and delayed['result_types'] == ['i1'] and
            delayed['operands'][:3] == [tg.named('mirroredReadReq'), tg.named('clock'), tg.named('reset')]
            and len(delayed['operands']) == 4 and constant(tg.definitions[delayed['operands'][3]]) == 0,
            'Unsupported mirrored-read flag register')
    response = Cone(top, instance_value(core, 'io_data2_valid', 'input'),
                    [tg.named('mirroredReadReq_d'), tg.named('io_mregReadResp0_valid'), tg.named('io_mregReadResp1_valid')], [1, 1, 1], 1)
    for mirror, a, b in itertools.product(range(2), repeat=3):
        require(response((mirror, a, b)) == (a if mirror else b), 'VPU response-valid selection mismatch')
    return {'direct_wires': wires,
            'command_contract': 'Command valid is forwarded directly to FSM instFire. issueBusy is a legality indicator; these wires do not stall an illegal command.',
            'mirrored_reads': {'same_register_assignments': 16384, 'same_row_assignments': 2048,
                               'suppression_assignments': 4, 'response_valid_assignments': 8,
                               'flag_register': delayed,
                               'cones': [same.evidence, mirrored.evidence, suppress.evidence, response.evidence],
                               'scope': 'Two valid reads of the exact same logical register and row suppress port1 and select port0 response-valid through one reset-to-zero register. Does not prove payload equality, memory latency, physical-bank aliases, or a complete instruction access stream.'}}


def analyze(modules):
    return {'status': 'typed_vpu_local_functions_checked', 'issue_guard': issue_guard(modules['VectorFSM']),
            'slot_release': [final_write_release(modules['VectorFSM'], i) for i in (1, 2)],
            'wrapper': wrapper_facts(modules),
            'limitations': ['No inferred instruction latency, row age, complete access profile, or universal scheduling proof.',
                            'No state reachability, arithmetic, same-cycle SRAM visibility, or source-to-cached-simulator build proof.',
                            'The opcode labels are source-correlated; all finite cutpoint functions are evaluated from typed CIRCT.']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--s0-manifest', type=Path, required=True)
    parser.add_argument('--exporter', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    s0_path, exporter, output = map(checked_path, (args.s0_manifest, args.exporter, args.output))
    s0 = json.loads(s0_path.read_text())
    require(s0.get('status') == 'complete' and s0.get('config') == 'EE290SimConfig', 'Unsupported S0 input')
    hardware = verify_artifact(s0['artifacts']['hardware_ir'])
    output.mkdir(parents=True, exist_ok=False)
    inputs = {name: artifact(path) for name, path in (('s0', s0_path), ('hardware_ir', hardware),
                                                    ('exporter', exporter), ('driver', checked_path(__file__)))}
    for name in ('rtlgraph_mxu1.py', 'rtlgraph_mxu1_capture.py', 'rtlgraph_mxu1_profile.py', 'rtlgraph_query.py', 'rtlgraph_s0.py'):
        inputs[name] = artifact(checked_path(Path(__file__).with_name(name)))
    typed_path = output / 'typed.json'
    command = [str(exporter), str(hardware), 'VectorFSM', 'VectorEngine', 'VectorEngineTop']
    with typed_path.open('w') as stream: subprocess.run(command, stdout=stream, check=True)
    typed = json.loads(typed_path.read_text())
    require(not typed['missing_modules'] and len(typed['modules']) == 3 and
            {m['name'] for m in typed['modules']} == {'VectorFSM', 'VectorEngine', 'VectorEngineTop'}, 'Missing or ambiguous VPU modules')
    report = {'schema_version': 1, 'kind': 'atlas-vpu-local-facts', 'config': 'EE290SimConfig',
              **analyze({m['name']: m for m in typed['modules']}), 'inputs': inputs,
              'typed': artifact(typed_path), 'export_command': command}
    source_root = checked_path(s0['chipyard_root']) / 'generators/sp26-atlas-acc/src/main/scala/atlas/vector'
    report['source_references'] = {name: artifact(checked_path(source_root / name)) for name in
                                   ('VectorFSM.scala', 'VectorIO.scala', 'VectorEngine.scala', 'VectorEngineTop.scala')}
    report['source_reference_scope'] = 'Current read-only source hashes for names/context; facts come from recorded CIRCT, not a new elaboration or source-to-binary proof.'
    for saved in inputs.values(): require(artifact(checked_path(saved['path'])) == saved, 'Input changed during VPU extraction')
    (output / 'vpu.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps({'status': report['status'], 'report': str(output / 'vpu.json')}))


if __name__ == '__main__':
    try: main()
    except (OSError, ValueError, KeyError, subprocess.CalledProcessError) as error:
        raise SystemExit('VPU extraction failed: ' + str(error))

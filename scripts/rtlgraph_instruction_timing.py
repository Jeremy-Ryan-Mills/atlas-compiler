#!/usr/bin/env python3
"""Extract payload-independent instruction timing from connected typed CIRCT cones."""
import argparse
import hashlib
from functools import cache, reduce
import json
import operator
from pathlib import Path
import subprocess

def require(condition, message):
    if not condition:
        raise ValueError(message)


def integer(text):
    return int(text == 'true') if text in ('true', 'false') else int(text.split(' : ')[0])


def width(typ):
    require(typ.startswith('i') and typ[1:].isdigit(), f'Unsupported integer type: {typ}')
    return int(typ[1:])


def sha(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as source:
        for block in iter(lambda: source.read(1 << 20), b''):
            digest.update(block)
    return digest.hexdigest()


class ControlCircuit:
    """Slice hierarchy at register boundaries, then execute exact finite-width control.

    Only requested outputs and their transitive state updates are included. A
    dependency on a payload input or unsupported operation fails construction.
    Unreset control must be flushed by the caller before age zero.
    """
    allowed = {'hw.constant', 'hw.wire', 'seq.firreg', 'comb.mux', 'comb.and',
               'comb.or', 'comb.xor', 'comb.add', 'comb.sub', 'comb.icmp',
               'comb.extract', 'comb.concat', 'comb.shru', 'comb.shl',
               'comb.replicate', 'hw.array_create', 'hw.array_get'}

    def __init__(self, document, top, outputs, inputs, cuts=None):
        self.modules = {m['name']: m for m in document['modules']}
        require(top in self.modules, f'Missing module: {top}')
        self.contexts, self.nodes, self.registers, self.types = {}, {}, {}, {}
        self.cuts = cuts or {}
        self.context('', top, {})
        self.inputs = {p['value']: p['name'] for p in self.modules[top]['ports'] if p['direction'] == 'input'}
        self.allowed_inputs = set(inputs)
        ports = self.contexts[''][2]
        self.outputs = {name: self.visit(('', ports[name]['value'])) for name in outputs}
        self.state = {node: None for node in self.registers}

    def context(self, path, name, bindings):
        require(name in self.modules, f'Missing module: {name}')
        module = self.modules[name]
        definitions = {v: op for op in module['operations'] for v in op['results']}
        ports = {p['name']: p for p in module['ports']}
        self.contexts[path] = (definitions, bindings, ports)
        self.types.update(((path, a['id']), a['type']) for a in module['arguments'])
        self.types.update(((path, v), t) for op in module['operations'] for v, t in zip(op['results'], op['result_types']))

    def clock_root(self, key):
        path, value = key
        definitions, bindings, _ = self.contexts[path]
        if value in bindings:
            return self.clock_root(bindings[value])
        if value in definitions and definitions[value]['kind'] == 'hw.wire':
            return self.clock_root((path, definitions[value]['operands'][0]))
        return key

    def visit(self, key):
        if key in self.nodes:
            return key
        path, value = key
        if key in self.cuts:
            self.nodes[key] = ('input', (), self.cuts[key])
            return key
        definitions, bindings, ports = self.contexts[path]
        if value in bindings:
            return self.visit(bindings[value])
        if not path and value in self.inputs:
            name = self.inputs[value]
            require(name in self.allowed_inputs, f'Timing depends on unapproved input: {name}')
            self.nodes[key] = ('input', (), name)
            return key
        require(value in definitions, f'Unresolved value: {key}')
        op = definitions[value]
        if op['kind'] == 'hw.instance':
            attrs = op['attributes']
            name = attrs['moduleName'].lstrip('@')
            child = path + '/' + attrs['instanceName']
            if child not in self.contexts:
                module = self.modules.get(name)
                require(module is not None, f'Missing module: {name}')
                child_ports = {p['name']: p for p in module['ports']}
                args = json.loads(attrs['argNames'])
                self.context(child, name, {child_ports[n]['value']: (path, v) for n, v in zip(args, op['operands'])})
            result_name = json.loads(attrs['resultNames'])[op['results'].index(value)]
            return self.visit((child, self.contexts[child][2][result_name]['value']))
        kind = op['kind']
        require(kind in self.allowed and not op.get('has_regions'), f'Unsupported timing operation: {kind}')
        require(len(op['results']) == 1, 'Multiple operation results are unsupported')
        self.nodes[key] = (kind, (), op['attributes'])
        if kind == 'seq.firreg':
            require(len(op['operands']) in (2, 4), 'Enabled register is unsupported')
            require(op['operands'][1] == ports['clock']['value'] and
                    self.clock_root((path, op['operands'][1])) == ('', self.contexts[''][2]['clock']['value']), 'Changed register clock')
            require(set(op['attributes']) <= {'firrtl.random_init_start', 'inner_sym', 'name', 'sv.namehint'}, 'Unsupported register attribute')
            if len(op['operands']) == 4:
                require(op['operands'][2] == ports['reset']['value'], 'Changed register reset')
            args = tuple(self.visit((path, v)) for i, v in enumerate(op['operands']) if i != 1)
            self.registers[key] = args
        else:
            args = tuple(self.visit((path, v)) for v in op['operands'])
        self.nodes[key] = (kind, args, op['attributes'])
        return key

    def cycle(self, inputs):
        @cache
        def get(key):
            kind, operands, attrs = self.nodes[key]
            if kind == 'input':
                require(attrs in inputs, f'Missing input {attrs}')
                return inputs[attrs]
            if kind == 'seq.firreg':
                return self.state[key]
            if kind == 'comb.mux':
                select = get(operands[0])
                if select is None:
                    yes, no = get(operands[1]), get(operands[2])
                    return yes if yes == no else None
                return get(operands[1 if select else 2])
            args = [get(v) for v in operands]
            if kind == 'hw.array_create':
                return tuple(reversed(args))
            if kind == 'hw.array_get':
                if args[0] is None or args[1] is None:
                    return None
                require(0 <= args[1] < len(args[0]), 'Out-of-range array access')
                return args[0][args[1]]
            n = width(self.types[key])
            mask = (1 << n) - 1
            if any(arg is None for arg in args):
                if kind == 'comb.and' and 0 in args:
                    return 0
                if kind == 'comb.or' and mask in args:
                    return mask
                return None
            if kind == 'hw.constant':
                raw = attrs['value']
                result = int(raw == 'true') if raw in ('true', 'false') else integer(raw)
            elif kind == 'hw.wire':
                result = args[0]
            elif kind == 'comb.extract':
                result = args[0] >> integer(attrs['lowBit'])
            elif kind == 'comb.concat':
                result = 0
                for operand, arg in zip(operands, args):
                    result = (result << width(self.types[operand])) | arg
            elif kind == 'comb.replicate':
                step = width(self.types[operands[0]])
                require(n % step == 0, 'Non-integral replicate')
                result = sum(args[0] << i for i in range(0, n, step))
            elif kind == 'comb.icmp':
                pred = integer(attrs['predicate'])
                require(0 <= pred <= 9, 'Unsupported comparison predicate')
                if 2 <= pred <= 5:
                    w = width(self.types[operands[0]])
                    args = [v - (1 << w) if v >> (w - 1) else v for v in args]
                index = pred if pred <= 5 else pred - 4
                result = int((operator.eq, operator.ne, operator.lt, operator.le, operator.gt, operator.ge)[index](*args))
            else:
                operations = {'comb.and': operator.and_, 'comb.or': operator.or_, 'comb.xor': operator.xor,
                              'comb.add': operator.add, 'comb.sub': operator.sub,
                              'comb.shru': operator.rshift, 'comb.shl': operator.lshift}
                require(kind in operations, f'Unsupported operation {kind}')
                result = reduce(operations[kind], args)
            return result & mask
        outputs = {name: get(key) for name, key in self.outputs.items()}
        self.state = {key: get(args[2] if len(args) == 3 and get(args[1]) else args[0]) for key, args in self.registers.items()}
        return outputs


VPU_OPS = ('add', 'sub', 'mul', 'rcp', 'sqrt', 'sin', 'cos', 'tanh', 'log', 'exp', 'exp2',
           'square', 'cube', 'rsum', 'csum', 'fp8', 'fp8pack', 'fp8unpack', 'relu',
           'rmax', 'rmin', 'cmax', 'cmin', 'pairmax', 'pairmin', 'mov',
           'vliOne', 'vliCol', 'vliRow', 'vliAll')


def vpu_circuit(document):
    outputs = ['io_busy', 'io_issueBusy']
    for direction in ('Read', 'Write'):
        for port in range(2):
            outputs += [f'io_mreg{direction}Req{port}_{suffix}' for suffix in ('valid', 'bits_mregId', 'bits_row')]
    for direction in ('Reads', 'Writes'):
        for port in range(4):
            outputs += [f'io_active{direction}_{port}_{suffix}' for suffix in ('valid', 'bits')]
    inputs = {'clock', 'reset', 'io_cmd_valid', 'io_cmd_bits_op', 'io_cmd_bits_vs1', 'io_cmd_bits_vs2',
              'io_cmd_bits_vd', 'io_mregReadResp0_valid', 'io_mregReadResp1_valid'}
    return ControlCircuit(document, 'VectorEngineTop', outputs, inputs)


def run_vpu(document, operations, response_latency=1, limit=320, circuit=None):
    circuit = circuit or vpu_circuit(document)
    circuit.state = {node: None for node in circuit.registers}
    inputs = {name: 0 for name in circuit.allowed_inputs}
    for _ in range(16):
        circuit.cycle(dict(inputs, reset=1))
    for _ in range(16):
        circuit.cycle(inputs)
    require(all(v is not None for v in circuit.state.values()), 'Control state remains dependent on initialization after reset/flush')
    pending, trace = {}, []
    for age in range(limit):
        incoming = operations.get(age)
        inputs.update(io_cmd_valid=int(incoming is not None), io_cmd_bits_op=VPU_OPS.index(incoming) + 1 if incoming else 1,
                      io_cmd_bits_vs1=2 if age == 0 else 18, io_cmd_bits_vs2=6 if age == 0 else 22,
                      io_cmd_bits_vd=10 if age == 0 else 26)
        for port in range(2):
            inputs[f'io_mregReadResp{port}_valid'] = int((age, port) in pending)
        out = circuit.cycle(inputs)
        if incoming:
            require(not (out['io_issueBusy'] >> (VPU_OPS.index(incoming) + 1)) & 1, f'Illegal launch at age {age}: {incoming}')
        reads, writes = [], []
        for port in range(2):
            if out[f'io_mregReadReq{port}_valid']:
                reads.append((port, out[f'io_mregReadReq{port}_bits_mregId'], out[f'io_mregReadReq{port}_bits_row']))
                pending[age + response_latency, port] = True
            if out[f'io_mregWriteReq{port}_valid']:
                writes.append((port, out[f'io_mregWriteReq{port}_bits_mregId'], out[f'io_mregWriteReq{port}_bits_row']))
        active = {}
        for direction in ('Reads', 'Writes'):
            active[direction.lower()] = sorted({out[f'io_active{direction}_{i}_bits'] for i in range(4) if out[f'io_active{direction}_{i}_valid']})
        trace.append({'age': age, 'reads': reads, 'writes': writes, 'active': active, 'busy': out['io_busy'], 'issue_busy': out['io_issueBusy']})
        if age > max(operations) + 2 and not out['io_busy']:
            break
    require(not trace[-1]['busy'], 'VPU did not release within bound')
    return trace



def streams(trace, direction):
    grouped = {}
    for event in trace:
        for _, bank, row in event[direction]:
            grouped.setdefault(bank, []).append((event['age'], row))
    result = []
    for bank, accesses in sorted(grouped.items()):
        for start in range(0, len(accesses), 32):
            block = accesses[start:start+32]
            require(len(block) == 32 and [r for _, r in block] == list(range(32)), 'Unexpected VPU row stream')
            step = block[1][0] - block[0][0]
            require(step > 0 and [a for a, _ in block] == list(range(block[0][0], block[0][0]+32*step, step)), 'Non-affine row stream')
            result.append({'bank': bank, 'age': block[0][0], 'step': step, 'count': 32})
    return result


def derive_vpu(document, overlap=True):
    circuit = vpu_circuit(document)
    traces = {op: run_vpu(document, {0: op}, circuit=circuit) for op in VPU_OPS if op != 'fp8'}
    profiles = {}
    for op, trace in traces.items():
        profiles[op] = {'reads': streams(trace, 'reads'), 'writes': streams(trace, 'writes'),
                        'read_release': max((e['age'] for e in trace if e['active']['reads']), default=-1),
                        'write_release': max(e['age'] for e in trace if e['active']['writes']),
                        'same_op_next_issue': next(e['age'] for e in trace[1:] if not(e['issue_busy'] >> (VPU_OPS.index(op)+1)) & 1)}
    def first(op):
        return min(s['age'] for s in profiles[op]['writes'])
    fields = {'read_age': min(s['age'] for s in profiles['add']['reads']),
              'simple_write_age': first('add'), 'row_sum_write_age': first('rsum'),
              'column_write_age': first('csum'), 'pack_write_age': first('fp8pack'),
              'pack_write_step': profiles['fp8pack']['writes'][0]['step'],
              'unpack_write_age': first('fp8unpack'), 'immediate_write_age': first('vliAll')}
    simple = set(traces) - {'rsum', 'rmax', 'rmin', 'csum', 'cmax', 'cmin', 'fp8pack', 'fp8unpack', 'vliAll', 'vliRow', 'vliCol', 'vliOne'}
    require(all(first(op) == fields['simple_write_age'] for op in simple | {'rmax', 'rmin'}), 'Simple-family timing diverged')
    require(all(first(op) == fields['column_write_age'] for op in ('csum', 'cmax', 'cmin')), 'Column-family timing diverged')
    require(all(first(op) == fields['immediate_write_age'] for op in ('vliAll', 'vliRow', 'vliCol', 'vliOne')), 'Immediate-family timing diverged')
    def stream(bank, age, step=1):
        return {'bank': bank, 'age': age, 'step': step, 'count': 32}
    for op, profile in profiles.items():
        read = fields['read_age']
        if op.startswith('vli'):
            expected_reads = []
        elif op in ('rsum', 'rmax', 'rmin'):
            expected_reads = [stream(2, read), stream(3, read)]
        elif op in ('csum', 'cmax', 'cmin'):
            expected_reads = [stream(2, read), stream(2, read+64), stream(3, read+32), stream(3, read+96)]
        elif op == 'fp8unpack':
            expected_reads = [stream(6, read)]
        else:
            sources = [6] if op == 'fp8pack' else [2, 6] if op in ('add', 'sub', 'mul', 'pairmax', 'pairmin') else [2]
            expected_reads = [s for bank in sources for s in (stream(bank, read), stream(bank+1, read+32))]
        write = first(op)
        if op == 'fp8pack':
            expected_writes = [stream(10, write, fields['pack_write_step'])]
        elif op in ('vliOne', 'vliCol'):
            expected_writes = [stream(10, write)]
        elif op in ('rsum', 'rmax', 'rmin'):
            expected_writes = [stream(10, write), stream(11, write)]
        else:
            expected_writes = [stream(10, write), stream(11, write+32)]
        require(profile['reads'] == expected_reads and profile['writes'] == expected_writes,
                f'Unsupported row geometry or family timing: {op}')
        last_read = max((v['age']+31*v['step'] for v in expected_reads), default=-1)
        last_write = max(v['age']+31*v['step'] for v in expected_writes)
        require(profile['read_release'] == last_read and profile['write_release'] == last_write,
                f'Logical reservations do not match profile stream boundaries: {op}')
        require(profile['same_op_next_issue'] == last_write, f'Slot lifetime mismatch: {op}')
    pairs = []
    if overlap:
        for a, baseline in traces.items():
            for b, second in traces.items():
                gap = next(e['age'] for e in baseline[1:] if not(e['issue_busy'] >> (VPU_OPS.index(b)+1)) & 1)
                mixed = run_vpu(document, {0: a, gap: b}, circuit=circuit)
                for direction in ('reads', 'writes'):
                    expected = sorted((e['age'], bank, row) for e in baseline for _, bank, row in e[direction])
                    expected += sorted((e['age']+gap, bank+16, row) for e in second for _, bank, row in e[direction])
                    actual = sorted((e['age'], bank, row) for e in mixed for _, bank, row in e[direction])
                    require(sorted(expected) == actual, f'Overlap changed {direction}: {a}/{b} at gap {gap}')
                pairs.append({'first': a, 'second': b, 'gap': gap})
    return fields, profiles, pairs


def export_typed(query, hardware, output):
    names = ['VectorEngineTop', 'VectorEngine', 'VectorFSM', 'LSU', 'ScalarCore',
             'SystolicArrayTop', 'SystolicArraySequencer', 'SystolicArray',
             'InnerProductTreesTop', 'InnerProductTreesSequencer', 'InnerProductTrees']
    document = None
    while True:
        raw = subprocess.check_output([str(query.resolve()), str(hardware.resolve()), *sorted(set(names))])
        document = json.loads(raw)
        lanes = {o['attributes']['moduleName'].lstrip('@') for m in document['modules'] if m['name'] == 'VectorEngine'
                 for o in m['operations'] if o['kind'] == 'hw.instance'}
        if lanes - set(names):
            names.extend(sorted(lanes - set(names)))
            continue
        require(not document['missing_modules'], 'Requested modules absent from hardware IR')
        try:
            vpu_circuit(document)
            break
        except ValueError as error:
            require(str(error).startswith('Missing module:'), str(error))
            names.append(str(error).split(': ', 1)[1])
    output.write_bytes(raw)
    return document




def mxu_circuit(document, top):
    import copy
    document = copy.deepcopy(document)
    module = next(m for m in document['modules'] if m['name'] == top)
    seq = next(o for o in module['operations'] if o['kind'] == 'hw.instance' and o['attributes']['instanceName'] == 'seq')
    outputs = ['io_dataBusy', 'io_computeBusy']
    for direction in ('Read', 'Write'):
        for port in range(2):
            outputs += [f'io_mreg{direction}Req{port}_{suffix}' for suffix in ('valid', 'bits_mregId', 'bits_row')]
    for direction in ('Reads', 'Writes'):
        for port in range(2):
            outputs += [f'io_active{direction}_{port}_{suffix}' for suffix in ('valid', 'bits')]
    for name in ('io_compute_valid', 'io_weightWriteReq_valid', 'io_accComputeWrite_valid', 'io_accComputeWrite_bits_rowIdx',
                 'io_accComputeReadEn', 'io_accComputeReadAddr_rowIdx', 'io_accLoadReq_valid', 'io_accLoadReq_bits_rowIdx',
                 'io_accStoreReadEn', 'io_accStoreAddr_rowIdx'):
        index = json.loads(seq['attributes']['resultNames']).index(name)
        module['ports'].append({'direction': 'output', 'name': name, 'type': seq['result_types'][index], 'value': seq['results'][index]})
        outputs.append(name)
    inputs = {'clock', 'reset', 'io_cmd_valid', 'io_cmd_bits_op', 'io_cmd_bits_mregId', 'io_cmd_bits_accSel',
              'io_cmd_bits_weightSlot', 'io_mregReadResp0_valid', 'io_mregReadResp1_valid'}
    return ControlCircuit(document, top, outputs, inputs)


def run_mxu(document, top, opcode, launches=None):
    circuit = mxu_circuit(document, top)
    inputs = {name: 0 for name in circuit.allowed_inputs}
    for _ in range(100):
        circuit.cycle(dict(inputs, reset=1))
    for _ in range(100):
        circuit.cycle(inputs)
    launches = launches or {0: (opcode, 2, 0)}
    trace, pending = [], {}
    for age in range(max(launches)+160):
        command = launches.get(age, (0, 22, 1))
        inputs.update(io_cmd_valid=int(age in launches), io_cmd_bits_op=command[0],
                      io_cmd_bits_mregId=command[1], io_cmd_bits_accSel=command[2],
                      io_cmd_bits_weightSlot=0)
        for port in range(2):
            inputs[f'io_mregReadResp{port}_valid'] = int((age, port) in pending)
        out = circuit.cycle(inputs)
        require(all(out[k] is not None for k in out if 'valid' in k or k.endswith(('Busy', 'ReadEn'))),
                'MXU timing output depends on uninitialized state')
        for port in range(2):
            if out[f'io_mregReadReq{port}_valid']:
                pending[age+1, port] = True
        trace.append({'age': age, **out})
    return trace



def derive_mxu(document):
    results = {}
    for top in ('SystolicArrayTop', 'InnerProductTreesTop'):
        singles = {op: run_mxu(document, top, op) for op in range(7)}
        summary = {}
        for op, trace in singles.items():
            summary[str(op)] = {key: {'first': ages[0], 'last': ages[-1], 'count': len(ages)}
                               for key in trace[0] if ('valid' in key or key.endswith('ReadEn')) and
                               (ages := [e['age'] for e in trace if e[key]])}
        first_write = summary['5']['io_accComputeWrite_valid']['first']
        require(summary['6']['io_accComputeWrite_valid']['first'] == first_write, 'Accumulate timing differs')
        require('io_accComputeReadEn' not in summary['5'] and summary['6']['io_accComputeReadEn'] == {'first': 0, 'last': 31, 'count': 32}, 'Unexpected accumulator read occupancy')
        cases = [(5, 5, 32, 1), (0, 5, 32, 0), (1, 6, 33, 0), (2, 6, 33, 0),
                 (5, 3, first_write+1, 0), (6, 4, first_write+1, 0),
                 (3, 5, 32, 0), (4, 6, 32, 0), (0, 1, 1, 0)]
        checked = []
        for a, b, gap, acc in cases:
            mixed = run_mxu(document, top, a, {0: (a, 2, 0), gap: (b, 22, acc)})
            for direction in ('Read', 'Write'):
                def events(trace, shift=0, bank_shift=0):
                    return [(e['age']+shift, e[f'io_mreg{direction}Req{p}_bits_mregId']+bank_shift,
                             e[f'io_mreg{direction}Req{p}_bits_row']) for e in trace for p in range(2)
                            if e[f'io_mreg{direction}Req{p}_valid']]
                require(sorted(events(mixed)) == sorted(events(singles[a])+events(singles[b], gap, 20)),
                        f'MXU overlap changes MREG {direction} stream: {top}/{a}/{b}/{gap}')
            for key in ('io_weightWriteReq_valid', 'io_accLoadReq_valid', 'io_accComputeWrite_valid', 'io_accComputeReadEn', 'io_accStoreReadEn'):
                expected = sorted([e['age'] for e in singles[a] if e[key]] + [e['age']+gap for e in singles[b] if e[key]])
                require([e['age'] for e in mixed if e[key]] == expected, f'MXU overlap changes {key}: {top}/{a}/{b}/{gap}')
            checked.append({'first_opcode': a, 'second_opcode': b, 'gap': gap, 'second_accumulator': acc})
        rejected = []
        if top == 'InnerProductTreesTop':
            boundary = run_mxu(document, top, 1, {0: (1, 2, 0), 32: (6, 22, 0)})
            require(not any(e['io_accComputeWrite_valid'] for e in boundary), 'Expected negative launch boundary changed')
            rejected.append({'first_opcode': 1, 'second_opcode': 6, 'gap': 32, 'result': 'second compute produces no accumulator writes'})
        results[top] = {'commands': summary, 'overlap_cases': checked, 'rejected_boundaries': rejected, 'first_write_age': first_write}
    return results


def scalar_lsu(document):
    """Compose scalar control at explicit issue and decoded-op cutpoints."""
    import copy
    doc = copy.deepcopy(document)
    module = next(m for m in doc['modules'] if m['name'] == 'ScalarCore')
    named = {op['attributes'].get('name'): op['results'][0] for op in module['operations'] if len(op['results']) == 1}
    decoder = next(op for op in module['operations'] if op['kind'] == 'hw.instance' and op['attributes']['instanceName'] == 'decoder')
    memcmd = decoder['results'][json.loads(decoder['attributes']['resultNames']).index('io_decoded_mem_cmd')]
    cuts = {('', named[n]): n for n in ('issueScalarLoad', 'issueScalarStore', 'hostStart')}
    cuts['', memcmd] = 'mem_cmd'
    for name in ('memLoadPending', 'memLoadRespValid', 'isMemLoadWB', 'seldWriteEn'):
        module['ports'].append({'direction': 'output', 'name': name, 'type': 'i1', 'value': named[name]})
    scalar = ControlCircuit(doc, 'ScalarCore', ['io_scalarMemCmd_valid', 'io_scalarMemCmd_bits_isStore',
                            'memLoadPending', 'memLoadRespValid', 'isMemLoadWB', 'seldWriteEn'],
                            {'reset', 'io_scalarMemResp_valid'}, cuts)
    lsu = ControlCircuit(doc, 'LSU', ['io_vmemScalarRead_valid', 'io_vmemScalarWrite_valid', 'io_scalarResp_valid', 'io_scalarBusy'],
                         {'reset', 'io_scalarCmd_valid', 'io_scalarCmd_bits_isStore', 'io_vmemScalarReadData_valid'})
    profiles = {}
    for op, code in [('load', 3), ('scale_load', 9), ('store', 0)]:
        scalar.state = {k: None for k in scalar.registers}
        lsu.state = {k: None for k in lsu.registers}
        response, trace = {}, []
        for age in range(-3, 8):
            # LSU response is the previous request valid qualified by its pending
            # register; query without advancing, then clock both circuits together.
            li = {'reset': int(age < 0), 'io_scalarCmd_valid': 0, 'io_scalarCmd_bits_isStore': 0,
                  'io_vmemScalarReadData_valid': int(age in response)}
            state = dict(lsu.state)
            ls = lsu.cycle(li)
            lsu.state = state
            sc = scalar.cycle({'reset': int(age < 0), 'io_scalarMemResp_valid': ls['io_scalarResp_valid'],
                               'issueScalarLoad': int(age == 0 and op != 'store'), 'issueScalarStore': int(age == 0 and op == 'store'),
                               'hostStart': 0, 'mem_cmd': code})
            li.update(io_scalarCmd_valid=sc['io_scalarMemCmd_valid'], io_scalarCmd_bits_isStore=sc['io_scalarMemCmd_bits_isStore'])
            ls = lsu.cycle(li)
            if ls['io_vmemScalarRead_valid']:
                response[age+1] = True
            if age >= 0:
                trace.append({'age': age, **sc, **ls})
        profiles[op] = trace
    return profiles




def mutation_checks(document):
    import copy
    baseline = run_vpu(document, {0: 'relu'})
    changed = copy.deepcopy(document)
    module = next(m for m in changed['modules'] if m['name'] == 'Relu')
    ports = {p['name']: p for p in module['ports']}
    ports['io_resp_valid']['value'] = ports['io_req_valid']['value']
    mutated = run_vpu(changed, {0: 'relu'})
    require(next(e['age'] for e in mutated if e['writes']) < next(e['age'] for e in baseline if e['writes']),
            'Bypassing valid pipeline did not change extracted timing')

    changed = copy.deepcopy(document)
    module = next(m for m in changed['modules'] if m['name'] == 'VectorEngineTop')
    ports = {p['name']: p for p in module['ports']}
    ports['io_busy']['value'] = ports['io_mregReadResp0_bits']['value']
    try:
        vpu_circuit(changed)
    except ValueError as error:
        require('unapproved input' in str(error), 'Payload mutation failed for unrelated reason')
    else:
        raise ValueError('Payload-dependent timing mutation was accepted')

    changed = copy.deepcopy(document)
    module = next(m for m in changed['modules'] if m['name'] == 'InnerProductTrees')
    ports = {p['name']: p for p in module['ports']}
    register = {'id': 'mutation_stage', 'kind': 'seq.firreg', 'operands': [ports['io_out_valid']['value'], ports['clock']['value']],
                'results': ['mutation_stage.r0'], 'result_types': ['i1'], 'attributes': {'name': 'mutation_stage'}, 'has_regions': False}
    module['operations'].append(register)
    ports['io_out_valid']['value'] = 'mutation_stage.r0'
    before = run_mxu(document, 'InnerProductTreesTop', 5)
    after = run_mxu(changed, 'InnerProductTreesTop', 5)
    require(next(e['age'] for e in after if e['io_accComputeWrite_valid']) ==
            next(e['age'] for e in before if e['io_accComputeWrite_valid'])+1,
            'Extra MXU valid stage did not change writeback timing')
    return ['Bypassing Relu valid pipeline advances extracted writes.',
            'Payload-dependent VPU control is rejected.',
            'Adding an MXU1 valid pipeline stage delays accumulator writes by one.']


def emit_profile(output, role, schema, report):
    output.mkdir(parents=True, exist_ok=True)
    evidence = output / 'profile.json'
    evidence.write_text(json.dumps(report, indent=2)+'\n')
    fields = {'schema': schema, 'config': 'EE290SimConfig',
              'source_ir_sha256': report['inputs']['hardware_ir']['sha256'], 'evidence_sha256': sha(evidence),
              **report['compiler_overrides']}
    (output / f'atlas-{role}.profile').write_text(''.join(f'{k}={v}\n' for k,v in fields.items()))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--typed', type=Path, help='Cached export; rechecked against the supplied IR and exporter')
    parser.add_argument('--query', type=Path, required=True)
    parser.add_argument('--hardware-ir', type=Path, required=True)
    parser.add_argument('--lsu-base', type=Path, required=True, help='Existing vector LSU evidence to preserve in v2')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--skip-overlap', action='store_true')
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    typed = args.output / 'typed.json'
    if args.typed:
        cached = json.loads(args.typed.read_text())
        names = sorted({m['name'] for m in cached['modules']})
        raw = subprocess.check_output([str(args.query.resolve()), str(args.hardware_ir.resolve()), *names])
        doc = json.loads(raw)
        require({m['name']: m for m in doc['modules']} == {m['name']: m for m in cached['modules']}, 'Cached typed export differs from hardware IR')
        typed.write_bytes(raw)
    else:
        doc = export_typed(args.query, args.hardware_ir, typed)
    source_hash = sha(args.hardware_ir)
    inputs = {'hardware_ir': {'sha256': source_hash}, 'typed_output': {'sha256': sha(typed)},
              'extractor': {'sha256': sha(__file__)}, 'typed_query': {'sha256': sha(args.query)}}
    assumptions = ['Age zero is an accepted engine command.',
                   'Synchronous reset and idle flush precede launch; unknown-state propagation must establish known timing outputs. Unreset operand metadata is only inspected when valid.',
                   'Each MREG/VMEM request receives its corresponding valid response one cycle later.',
                   'No conflicting external port access or reset during transactions.']
    limits = ['Finite control execution, not numerical arithmetic validation or unbounded proof.',
              'Whole-system memory arbitration and frontend acceptance are separate obligations.',
              'SV assertion regions are outside these output cones; engine overlap does not establish ScalarCore assertion legality. Existing compiler logical reservations remain enforced.']
    mutations = mutation_checks(doc)
    fields, profiles, pairs = derive_vpu(doc, not args.skip_overlap)
    report = {'schema': 'atlas.rtlgraph.vpu-profile.v1', 'config': 'EE290SimConfig',
              'status': 'connected_typed_control_execution', 'inputs': inputs, 'mutation_checks': mutations,
              'compiler_overrides': {'rows': 32, 'row_step': 1, 'operand_capture': 'issue', **fields},
              'instructions': profiles,
              'overlap_checks': {'ordered_pairs': len(pairs), 'method': 'Issue second instruction at earliest per-op issueBusy-clear age; compare both streams to independent isolated executions with distinct operands.'},
              'assumptions': assumptions,
              'checks': ['Every timing output and transitive state-update cone is independent of payload inputs.',
                         'Physical read/write row streams, active register reservations and issue-busy masks execute from typed hierarchy.',
                         'All 29 implemented commands are checked; reserved fp8 command is excluded.'],
              'limitations': limits}
    emit_profile(args.output / 'vpu', 'vpu', 'atlas-vpu-profile-v1', report)
    if pairs:
        (args.output / 'vpu-overlap.json').write_text(json.dumps(pairs, indent=2)+'\n')

    scalar = scalar_lsu(doc)
    def active(trace, name):
        return [e['age'] for e in trace if e[name]]
    load, scale, store = (scalar[k] for k in ('load', 'scale_load', 'store'))
    read = active(load, 'io_vmemScalarRead_valid')
    write = active(load, 'isMemLoadWB')
    require(len(read) == len(write) == 1 and active(scale, 'seldWriteEn') == write and
            active(store, 'io_vmemScalarWrite_valid') == read, 'Scalar LSU streams diverged')
    pending = sorted(set(active(load, 'memLoadPending') + active(load, 'io_scalarBusy')))
    scalar_fields = {'scalar_memory_age': read[0], 'scalar_load_write_age': write[0], 'scalar_load_first_free_age': max(pending)+1}
    base = json.loads(args.lsu_base.read_text())
    vector = base.get('vector_evidence', base)
    require(vector['schema'] == 'atlas.rtlgraph.lsu-profile.v1' and vector['inputs']['hardware_ir']['sha256'] == source_hash,
            'Vector LSU evidence does not match current hardware')
    report = {'schema': 'atlas.rtlgraph.lsu-profile.v2', 'config': 'EE290SimConfig',
              'status': 'scalar_control_composed_with_preserved_vector_evidence', 'inputs': inputs,
              'compiler_overrides': {**vector['compiler_overrides'], **scalar_fields},
              'vector_evidence': vector,
              'scalar_events': {name: {key: active(trace, key) for key in trace[0] if key != 'age' and active(trace, key)} for name, trace in scalar.items()},
              'assumptions': assumptions + ['ScalarCore issueScalarLoad/issueScalarStore and decoded mem_cmd are explicit accepted-command cutpoints; hostStart stays low.',
                                             'Scalar command/response connections follow the AtlasCore direct wiring; whole-frontend control is outside these scalar cones.'],
              'limitations': limits + ['Scalar load/store address translation, byte masks and numerical payloads are outside valid-only extraction.']}
    emit_profile(args.output / 'lsu', 'lsu', 'atlas-lsu-profile-v2', report)

    mxu = derive_mxu(doc)
    for index, top in enumerate(('SystolicArrayTop', 'InnerProductTreesTop')):
        role = f'mxu{index}'
        report = {'schema_version': 2, 'kind': f'atlas-partial-{role}-profile', 'config': 'EE290SimConfig',
                  'status': 'connected_typed_control_execution', 'inputs': inputs,
                  'compiler_overrides': {'first_write_age': mxu[top]['first_write_age'], 'overwrite_acc_read_hold': 0},
                  'commands': mxu[top]['commands'], 'overlap_checks': mxu[top]['overlap_cases'], 'rejected_boundaries': mxu[top]['rejected_boundaries'],
                  'assumptions': assumptions, 'limitations': limits + ['Resource capacities and all-state acceptance remain separately established scheduling constraints.']}
        emit_profile(args.output / role, role, f'atlas-{role}-profile-v{2 if index == 0 else 1}', report)
    print(json.dumps({'vpu_instructions': len(profiles), 'vpu_overlap_pairs': len(pairs),
                      'scalar_lsu': scalar_fields, 'mxu_commands': 14, 'mxu_overlap_cases': sum(len(v['overlap_cases']) for v in mxu.values())}))


if __name__ == '__main__':
    main()

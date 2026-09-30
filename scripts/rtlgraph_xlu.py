#!/usr/bin/env python3
"""Derive conditional XLU timing and symbolic payload routing from typed CIRCT.

The typed exporter is retained at dd7342c:scripts/rtlgraph_query.cpp.
"""
import argparse
from dataclasses import dataclass
from functools import cache, reduce
import hashlib
import json
import operator
from pathlib import Path
import subprocess


def require(condition, message):
    if not condition:
        raise ValueError(message)


def integer(text):
    return int(text.split(' : ')[0])


def width(typ):
    require(typ.startswith('i') and typ[1:].isdigit(), f'Unsupported integer type: {typ}')
    return int(typ[1:])


@dataclass(frozen=True)
class Bits:
    values: tuple


def bits(value, count):
    return value.values if isinstance(value, Bits) else tuple((value >> i) & 1 for i in range(count))


class Circuit:
    """Small two-state evaluator; unsupported operations fail closed."""
    allowed = {'hw.constant', 'hw.wire', 'hw.output', 'seq.firreg', 'comb.mux',
               'comb.and', 'comb.or', 'comb.xor', 'comb.add', 'comb.icmp',
               'comb.extract', 'comb.concat', 'hw.array_create', 'hw.array_get'}

    def __init__(self, module):
        self.ops = {}
        self.types = {a['id']: a['type'] for a in module['arguments']}
        self.ports = {p['name']: p for p in module['ports']}
        self.registers = []
        self.state = {}
        for op in module['operations']:
            require(op['kind'] in self.allowed and not op['has_regions'], f"Unsupported operation: {op['kind']}")
            if op['kind'] == 'hw.output':
                continue
            require(len(op['results']) == 1, 'Expected one operation result')
            result = op['results'][0]
            self.ops[result] = op
            self.types[result] = op['result_types'][0]
            if op['kind'] == 'seq.firreg':
                require(len(op['operands']) in (2, 4) and op['operands'][1] == self.ports['clock']['value'],
                        'Unsupported register clock or reset')
                require(len(op['operands']) == 2 or op['operands'][2] == self.ports['reset']['value'],
                        'Unsupported register reset signal')
                require('isAsync' not in op['attributes'], 'Asynchronous reset is unsupported')
                self.registers.append(op)
                self.state[result] = Bits(tuple(('initial', result, b) for b in range(width(self.types[result]))))
        for op in self.registers:
            if len(op['operands']) == 4:
                reset = self.ops[op['operands'][3]]
                require(reset['kind'] == 'hw.constant', 'Unsupported reset value')
                self.state[op['results'][0]] = integer(reset['attributes']['value']) & ((1 << width(op['result_types'][0])) - 1)

    def cycle(self, inputs):
        values = {self.ports[k]['value']: v for k, v in inputs.items()}
        values.update(self.state)

        @cache
        def get(value):
            if value in values:
                return values[value]
            op = self.ops[value]
            kind, operands, attrs = op['kind'], op['operands'], op['attributes']
            if kind == 'hw.constant':
                raw = attrs['value']
                return (int(raw == 'true') if raw in ('true', 'false') else integer(raw)) & ((1 << width(self.types[value])) - 1)
            if kind == 'hw.wire':
                return get(operands[0])
            if kind == 'comb.mux':
                select = get(operands[0])
                require(isinstance(select, int), 'Payload-dependent control is unsupported')
                return get(operands[1 if select else 2])
            args = [get(v) for v in operands]
            if kind == 'hw.array_create':
                return list(reversed(args))
            if kind == 'hw.array_get':
                require(isinstance(args[1], int) and 0 <= args[1] < len(args[0]), 'Unsupported array index')
                return args[0][args[1]]
            n = width(self.types[value])
            mask = (1 << n) - 1
            if kind == 'comb.extract':
                low = integer(attrs['lowBit'])
                return Bits(args[0].values[low:low+n]) if isinstance(args[0], Bits) else (args[0] >> low) & mask
            if kind == 'comb.concat':
                if any(isinstance(a, Bits) for a in args):
                    return Bits(tuple(b for v, a in reversed(list(zip(operands, args))) for b in bits(a, width(self.types[v]))))
                result = 0
                for v, a in zip(operands, args):
                    result = (result << width(self.types[v])) | a
                return result
            require(all(isinstance(a, int) for a in args), f'Unsupported symbolic operation: {kind}')
            if kind == 'comb.icmp':
                pred = integer(attrs['predicate'])
                require(pred in (0, 1), 'Only equality predicates are supported')
                return int((args[0] == args[1]) if pred == 0 else (args[0] != args[1]))
            functions = {'comb.and': operator.and_, 'comb.or': operator.or_, 'comb.xor': operator.xor, 'comb.add': operator.add}
            require(kind in functions, f'Unsupported evaluation: {kind}')
            return reduce(functions[kind], args) & mask

        def output(name):
            return get(self.ports[name]['value'])

        def advance():
            state = {}
            for op in self.registers:
                operands = op['operands']
                selected = operands[3] if len(operands) == 4 and get(operands[2]) else operands[0]
                state[op['results'][0]] = get(selected)
            self.state = state

        return output, advance


def run(module, response_latency=1, busy_command=False, limit=160):
    circuit = Circuit(module)
    source = Bits(tuple(('source', b) for b in range(6)))
    destination = Bits(tuple(('destination', b) for b in range(6)))
    responses, reads, writes, busy, first_free = {}, [], [], [], None
    for age in range(limit):
        response = responses.get(age)
        inputs = {'clock': 0, 'reset': 0,
                  'io_cmd_valid': int(age == 0 or busy_command and len(writes) < 32),
                  'io_cmd_bits_srcMregId': source if age == 0 else 13,
                  'io_cmd_bits_dstMregId': destination if age == 0 else 55,
                  'io_cmd_bits_op': 0 if age == 0 else 3,
                  'io_mregReadResp_valid': int(response is not None),
                  'io_mregReadResp_bits': response if response is not None else 0}
        output, advance = circuit.cycle(inputs)
        busy.append(output('io_busy'))
        if output('io_mregReadReq_valid'):
            row = output('io_mregReadReq_bits_row')
            require(output('io_mregReadReq_bits_mregId') == source, 'Source operand was not captured at issue')
            reads.append((age, row))
            responses[age + response_latency] = Bits(tuple(('input', row, b) for b in range(256)))
        if output('io_mregWriteReq_valid'):
            row = output('io_mregWriteReq_bits_row')
            require(output('io_mregWriteReq_bits_mregId') == destination, 'Destination operand was not captured at issue')
            expected = Bits(tuple(('input', column, row * 8 + b) for column in range(32) for b in range(8)))
            require(output('io_mregWriteReq_bits_data') == expected, f'Incorrect transpose routing at row {row}')
            writes.append((age, row))
        advance()
        if age and not busy[-1] and first_free is None:
            first_free = age
        if first_free is not None and age == first_free + 2:
            break
    require(len(reads) == len(writes) == 32, 'Expected exactly 32 reads and writes before release')
    require(all(row == i and age == reads[0][0] + i for i, (age, row) in enumerate(reads)), 'Noncontiguous input row stream')
    require(all(row == i and age == writes[0][0] + i for i, (age, row) in enumerate(writes)), 'Noncontiguous output row stream')
    require(first_free is not None and busy == [0] + [1] * (first_free - 1) + [0] * 3, 'Unexpected busy interval')
    require(writes[-1][0] + 1 == first_free, 'Resource release differs from final write plus one')
    return {'read_age': reads[0][0], 'write_age': writes[0][0], 'first_free_age': first_free}


def derive(document):
    modules = [m for m in document['modules'] if m['name'] == 'XluEngine']
    require(len(modules) == 1, 'Expected exactly one XluEngine module')
    module = modules[0]
    profile = run(module)
    require(run(module, busy_command=True) == profile, 'Busy command changed an in-flight transpose')
    delayed = run(module, response_latency=2)
    require(delayed['write_age'] == profile['write_age'] + 1 and delayed['first_free_age'] == profile['first_free_age'] + 1,
            'Final response does not control write start and release')
    return profile


def sha(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as source:
        for block in iter(lambda: source.read(1 << 20), b''):
            h.update(block)
    return h.hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--query', type=Path, required=True)
    parser.add_argument('--hardware-ir', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    typed = subprocess.check_output([str(args.query.resolve()), str(args.hardware_ir.resolve()), 'XluEngine'])
    document = json.loads(typed)
    timing = derive(document)
    report = {
        'schema': 'atlas.rtlgraph.xlu-profile.v1', 'config': 'EE290SimConfig',
        'status': 'conditional_typed_execution_and_symbolic_routing',
        'inputs': {'hardware_ir': {'sha256': sha(args.hardware_ir)},
                   'extractor': {'sha256': sha(__file__)},
                   'typed_query': {'sha256': sha(args.query)},
                   'typed_output': {'sha256': hashlib.sha256(typed).hexdigest()}},
        'compiler_overrides': {'rows': 32, 'row_step': 1, 'operand_capture': 'issue', **timing},
        'scope': 'VTRPOSE.XLU physical MREG row accesses and XLU occupancy; symbolic 32x32-byte transpose routing.',
        'assumptions': ['Age zero is command acceptance while XLU is idle.',
                        'Resettable registers start at their post-reset values; unreset payload and command registers remain symbolic.',
                        'Each MREG request receives its corresponding response exactly one cycle later.',
                        'No conflicting MREG port access or reset occurs during the transaction.',
                        'The frontend routes VTRPOSE.XLU directly to this instance in the pinned configuration.'],
        'checks': ['All 8192 payload bits route to their transpose positions.',
                   'Changing command operands after launch does not change source or destination.',
                   'Asserting a second command throughout busy does not affect the active transpose.',
                   'An extra response cycle shifts write start and release by one.'],
        'limitations': ['Conditional finite typed execution, not a whole-design or unbounded proof.',
                        'Logical MREG reservations and same-cycle visibility remain inherited.',
                        'Frontend routing and MREG response timing require independent RTL validation.']}
    args.output.mkdir(parents=True, exist_ok=True)
    evidence = args.output / 'profile.json'
    evidence.write_text(json.dumps(report, indent=2) + '\n')
    fields = {'schema': 'atlas-xlu-profile-v1', 'config': report['config'],
              'source_ir_sha256': report['inputs']['hardware_ir']['sha256'],
              'evidence_sha256': sha(evidence), **report['compiler_overrides']}
    (args.output / 'atlas-xlu.profile').write_text(''.join(f'{k}={v}\n' for k, v in fields.items()))
    print(json.dumps(timing, sort_keys=True))


if __name__ == '__main__':
    main()

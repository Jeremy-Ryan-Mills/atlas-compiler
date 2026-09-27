#!/usr/bin/env python3
"""Check MREG address predecode and physical memory ports in typed CIRCT.

This is a configuration-specific combinational/structural check, not a proof of
arbitration, response routing, memory contents, or legal instruction schedules.
"""
import argparse
import functools
import json
import operator
from pathlib import Path
import re
import subprocess
import sys

from rtlgraph_mxu1 import Graph
from rtlgraph_mxu1_capture import verify_artifact
from rtlgraph_mxu1_profile import constant, require
from rtlgraph_query import identity_chain
from rtlgraph_s0 import artifact, checked_path

PORTS = ('mxu0ReadReq0', 'mxu0ReadReq1', 'mxu1ReadReq0', 'mxu1ReadReq1',
         'vpuReadReq0', 'vpuReadReq1', 'lsuReadReq', 'xluReadReq')


class Bits:
    """Small, fail-closed interpreter for the exported address cones only."""
    def __init__(self, module):
        self.graph = Graph(module)
        self.types = {p['value']: p['type'] for p in module['ports'] if p['direction'] == 'input'}
        self.types.update((v, t) for op in module['operations']
                          for v, t in zip(op['results'], op['result_types']))

    def width(self, value):
        match = re.fullmatch(r'i([1-9][0-9]*)', self.types.get(value, ''))
        require(match is not None and int(match[1]) <= 256, 'Unsupported bit-vector type')
        return int(match[1])

    def evaluate(self, root, inputs):
        active, memo = set(), dict(inputs)
        for value, bits in inputs.items():
            require(0 <= bits < 1 << self.width(value), 'Input out of range')

        def visit(value):
            if value in memo:
                return memo[value]
            require(value not in active, 'Feedback in address cone')
            active.add(value)
            op = self.graph.definitions.get(value)
            require(op is not None and Graph.traversable(op), 'Unsupported address operation/input')
            width = self.width(value)
            args = [visit(v) for v in op['operands']]
            widths = [self.width(v) for v in op['operands']]
            kind = op['kind']
            if kind == 'hw.constant':
                out = constant(op)
            elif op['identity_wire'] and len(args) == 1 and widths[0] == width:
                out = args[0]
            elif kind == 'comb.extract' and len(args) == 1:
                match = re.fullmatch(r'(\d+) : i32', op['attributes'].get('lowBit', ''))
                require(match is not None and int(match[1]) + width <= widths[0], 'Invalid extract range')
                out = args[0] >> int(match[1])
            elif kind == 'comb.concat' and sum(widths) == width:
                out = 0
                for part, size in zip(args, widths):
                    out = (out << size) | part
            elif kind == 'comb.replicate' and len(args) == 1 and width % widths[0] == 0:
                out = sum(args[0] << offset for offset in range(0, width, widths[0]))
            elif kind == 'comb.shl' and len(args) == 2 and widths == [width, width]:
                out = 0 if args[1] >= width else args[0] << args[1]
            elif kind == 'comb.and' and args and all(w == width for w in widths):
                out = functools.reduce(operator.and_, args)
            else:
                raise ValueError('Unsupported address operation: ' + kind)
            active.remove(value)
            memo[value] = out & ((1 << width) - 1)
            return memo[value]
        return visit(root)


def mapping(module, direction, index, stem):
    bits = Bits(module)
    graph = bits.graph
    inputs = [graph.named('io_' + stem + suffix) for suffix in ('_valid', '_bits_mregId', '_bits_row')]
    require(len(set(inputs)) == 3 and [bits.width(v) for v in inputs] == [1, 6, 5],
            'Unsupported request identities or widths')
    valid, reg, row = inputs
    bank_root = graph.named(f'{direction}BankOHs_{index}')
    row_root = graph.named(f'{direction}PhysRows_{index}')
    require(bits.width(bank_root) == 32 and bits.width(row_root) == 6, 'Unsupported address geometry')
    cones = [graph.cone(v) for v in (bank_root, row_root)]
    for cone, allowed in zip(cones, ({valid, reg}, {reg, row})):
        require(not cone['unsupported'] and all(t['reason'] == 'input_port' and t['value'] in allowed
                for t in cone['terminals']), 'Unexpected address input/state')
    for enable in range(2):
        for register in range(64):
            actual = bits.evaluate(bank_root, {valid: enable, reg: register})
            require(actual == (1 << (register % 32) if enable else 0), 'Physical bank mapping mismatch')
    for register in range(64):
        for logical_row in range(32):
            actual = bits.evaluate(row_root, {reg: register, row: logical_row})
            require(actual == (register // 32) * 32 + logical_row, 'Physical row mapping mismatch')
    return {'port': stem, 'bank_assignments_checked': 128, 'row_assignments_checked': 2048,
            'bank_cone': cones[0], 'row_cone': cones[1]}


def memory_bank(module, index):
    graph = Graph(module)
    value = graph.named(f'banks_{index}')
    memory = graph.definitions[value]
    require(memory['kind'] == 'seq.firmem' and memory['result_types'] == ['!seq.firmem<64 x 256>'],
            'Unsupported physical memory type')
    require(memory['attributes'].get('readLatency') == '1 : i32' and
            memory['attributes'].get('writeLatency') == '1 : i32', 'Unsupported memory port latency')
    uses = graph.uses[value]
    require(len(uses) == 2 and {op['kind'] for op, _ in uses} ==
            {'seq.firmem.read_port', 'seq.firmem.write_port'} and all(i == 0 for _, i in uses),
            'Physical bank must have exactly one read and one write port')
    for op, _ in uses:
        read = op['kind'] == 'seq.firmem.read_port'
        kind = 'Read' if read else 'Write'
        require(len(op['operands']) == (4 if read else 5), 'Unsupported optional memory operands')
        for operand, name in ((1, f'bank{kind}Row_{index}'), (2, 'clock'), (3, f'bank{kind}Valid_{index}')):
            require(identity_chain(module, graph.named(name), op['operands'][operand]) is not None,
                    'Memory address/enable/clock connection mismatch')
        if not read:
            require(identity_chain(module, graph.named(f'bankWriteData_{index}'), op['operands'][4]) is not None,
                    'Memory write-data connection mismatch')
    return {'bank': index, 'memory': memory, 'ports': [op for op, _ in uses]}


def analyze(module):
    mappings = [mapping(module, direction, index, port if direction == 'read' else port.replace('Read', 'Write'))
                for direction in ('read', 'write') for index, port in enumerate(PORTS)]
    banks = [memory_bank(module, i) for i in range(32)]
    require(sum(op['kind'] == 'seq.firmem' for op in module['operations']) == 32, 'Unexpected memory count')
    return {'status': 'address_functions_and_1r1w_structure_checked', 'mappings': mappings, 'banks': banks,
            'geometry': {'logical_registers': 64, 'logical_rows': 32, 'physical_banks': 32,
                         'physical_rows': 64, 'row_bits': 256},
            'address_rule': {'bank': 'mreg_id % 32', 'row': '(mreg_id // 32) * 32 + logical_row'},
            'scope': 'Exhaustive combinational input-domain checks of named predecode cones, plus physical memory port/connectivity census. Does not prove arbitration, response routing, contents, same-cycle visibility, or temporal safety.'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--s0-manifest', type=Path, required=True)
    parser.add_argument('--exporter', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True, help='New evidence directory')
    args = parser.parse_args()
    manifest, exporter, output = map(checked_path, (args.s0_manifest, args.exporter, args.output))
    s0 = json.loads(manifest.read_text())
    require(s0.get('status') == 'complete' and s0.get('config') == 'EE290SimConfig', 'Unsupported S0 input')
    hw = verify_artifact(s0['artifacts']['hardware_ir'])
    output.mkdir(parents=True, exist_ok=False)
    typed_path = output / 'typed.json'
    command = [str(exporter), str(hw), 'MregFile']
    with typed_path.open('w') as stream:
        subprocess.run(command, stdout=stream, check=True)
    typed = json.loads(typed_path.read_text())
    require(not typed['missing_modules'] and len(typed['modules']) == 1, 'MregFile missing or ambiguous')
    report = {'schema_version': 1, 'config': 'EE290SimConfig', **analyze(typed['modules'][0]),
              'inputs': {key: artifact(path) for key, path in (('s0', manifest), ('hardware_ir', hw),
                         ('exporter', exporter), ('driver', checked_path(__file__)))},
              'export_command': command, 'typed': artifact(typed_path)}
    (output / 'mreg.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps({'status': report['status'], 'request_ports': len(report['mappings']),
                      'physical_banks': len(report['banks']), 'report': str(output / 'mreg.json')}))


if __name__ == '__main__':
    try:
        main()
    except (ValueError, KeyError, subprocess.CalledProcessError) as error:
        print('MREG extraction failed: ' + str(error), file=sys.stderr)
        raise SystemExit(1)

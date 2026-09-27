#!/usr/bin/env python3
"""Extract narrow MXU1 resource facts from the pinned typed CIRCT artifact.

Checks accumulator 1R1W structure and read muxes, port boundary predicates, and
the shared weight-write request. Exhaustive checks cover independent cutpoints,
not state reachability or instruction timing. These facts support a separate
conditional scheduling argument; this tool does not prove an RTL cycle bound.
"""

import argparse
import functools
import itertools
import json
import operator
from pathlib import Path
import re
import subprocess
import sys

from rtlgraph_mxu1 import Graph
from rtlgraph_mxu1_capture import verify_artifact
from rtlgraph_mxu1_profile import constant, require
from rtlgraph_query import find_instance, identity_chain, instance_value
from rtlgraph_s0 import artifact, checked_path


class Evaluator:
    """Finite-width combinational subset used by these three local queries."""
    def __init__(self, module):
        self.graph = Graph(module)
        self.types = {p['value']: p['type'] for p in module['ports'] if p['direction'] == 'input'}
        self.types.update((v, t) for op in module['operations'] for v, t in zip(op['results'], op['result_types']))

    def width(self, value):
        match = re.fullmatch(r'i([1-9][0-9]*)', self.types.get(value, ''))
        require(match is not None and int(match[1]) <= 64, 'Unsupported bit-vector type')
        return int(match[1])

    def cuts(self, names, widths):
        values = [self.graph.named(name) for name in names]
        require(len(set(values)) == len(values) and [self.width(v) for v in values] == widths,
                'Unsupported cutpoint identities or widths')
        return values

    def evaluate(self, root, inputs):
        active, memo = set(), dict(inputs)
        for value, bits in inputs.items():
            require(type(bits) is int and 0 <= bits < 1 << self.width(value), 'Input out of range')

        def visit(value):
            if value in memo:
                return memo[value]
            require(value not in active, 'Feedback in combinational cone')
            active.add(value)
            op = self.graph.definitions.get(value)
            require(op is not None and Graph.traversable(op), 'Unsupported operation or unresolved input')
            width = self.width(value)
            args = [visit(v) for v in op['operands']]
            widths = [self.width(v) for v in op['operands']]
            kind = op['kind']
            if kind == 'hw.constant':
                result = constant(op)
            elif op['identity_wire'] and len(args) == 1 and widths == [width]:
                result = args[0]
            elif kind in ('comb.and', 'comb.or', 'comb.xor', 'comb.add') and args and all(w == width for w in widths):
                result = functools.reduce({'comb.and': operator.and_, 'comb.or': operator.or_,
                                           'comb.xor': operator.xor, 'comb.add': operator.add}[kind], args)
            elif kind == 'comb.mux' and len(args) == 3 and widths == [1, width, width]:
                result = args[1] if args[0] else args[2]
            elif kind == 'comb.extract' and len(args) == 1:
                match = re.fullmatch(r'(\d+) : i32', op['attributes'].get('lowBit', ''))
                require(match is not None and int(match[1]) + width <= widths[0], 'Unsupported extract range')
                result = args[0] >> int(match[1])
            elif (kind == 'comb.icmp' and len(args) == 2 and widths[0] == widths[1]
                  and width == 1 and op['attributes'].get('predicate') == '0 : i64'):
                result = int(args[0] == args[1])
            else:
                raise ValueError('Unsupported combinational operation: ' + kind)
            active.remove(value)
            memo[value] = result & ((1 << width) - 1)
            return memo[value]
        return visit(root)


def boundary_function(module, prefix):
    evaluator = Evaluator(module)
    names = (prefix + 'CmdValid', prefix + 'Row')
    inputs = evaluator.cuts(names, [1, 6])
    root = evaluator.graph.named(prefix + 'Boundary')
    require(evaluator.width(root) == 1, 'Unsupported boundary output width')
    for valid, row in itertools.product(range(2), range(64)):
        actual = evaluator.evaluate(root, dict(zip(inputs, (valid, row))))
        require(actual == int(not valid or (((row + 1) & 63) >> 5)), 'Port boundary function mismatch')
    return {'port': prefix, 'assignments_checked': 128,
            'function': '!cmdValid || (((row + 1) mod 64) >= 32)',
            'cone': evaluator.graph.cone(root, dict(zip(inputs, names))),
            'scope': 'Exact i1/i6 cutpoint function. Does not prove reachable row values, initialization, or the 32-cycle instruction interval.'}


def accumulator_bank(module, bank):
    evaluator = Evaluator(module)
    graph = evaluator.graph
    names = ('io_computeReadEn', 'io_computeReadAddr_accSel', 'io_computeReadAddr_rowIdx',
             'io_storeReadEn', 'io_storeAddr_accSel', 'io_storeAddr_rowIdx')
    inputs = evaluator.cuts(names, [1, 1, 5, 1, 1, 5])
    memory = graph.definitions[graph.named('buffer' + str(bank))]
    require(memory['kind'] == 'seq.firmem' and memory['result_types'] == ['!seq.firmem<32 x 512>'],
            'Unsupported accumulator memory geometry')
    require(memory['attributes'].get('readLatency') == '1 : i32'
            and memory['attributes'].get('writeLatency') == '1 : i32', 'Unsupported memory latency')
    uses = graph.uses[memory['results'][0]]
    require(len(uses) == 2 and {op['kind'] for op, _ in uses}
            == {'seq.firmem.read_port', 'seq.firmem.write_port'} and all(i == 0 for _, i in uses),
            'Accumulator must have exactly one read and one write port')
    read = next(op for op, _ in uses if op['kind'] == 'seq.firmem.read_port')
    enable, address = (graph.named('read' + str(bank) + suffix) for suffix in ('En', 'Addr'))
    require(evaluator.width(enable) == 1 and evaluator.width(address) == 5, 'Unsupported read output widths')
    require(read['operands'][1:] == [address, graph.named('clock'), enable], 'Accumulator read wiring mismatch')
    checked = 0
    for ce, cs, cr, se, ss, sr in itertools.product(range(2), range(2), range(32), range(2), range(2), range(32)):
        assignment = dict(zip(inputs, (ce, cs, cr, se, ss, sr)))
        compute = ce and cs == bank
        require(evaluator.evaluate(enable, assignment) == int(compute or (se and ss == bank)),
                'Accumulator read enable mismatch')
        require(evaluator.evaluate(address, assignment) == (cr if compute else sr),
                'Accumulator read address selection mismatch')
        checked += 1
    return {'bank': bank, 'memory': memory, 'ports': [op for op, _ in uses],
            'assignments_checked': checked,
            'read_enable': '(computeEn && computeAcc==bank) || (storeEn && storeAcc==bank)',
            'read_address': 'computeEn && computeAcc==bank ? computeRow : storeRow',
            'scope': 'One physical read port with compute priority; no two-row or same-row broadcast path in this local mux.'}


def weight_write_function(module):
    evaluator = Evaluator(module)
    names = ('p0CmdValid', 'p1CmdValid', 'p0Cmd_op', 'p1Cmd_op', 'p0Cmd_weightSlot', 'p1Cmd_weightSlot')
    inputs = evaluator.cuts(names, [1, 1, 3, 3, 1, 1])
    valid, slot = (evaluator.graph.named('io_weightWriteReq_' + suffix) for suffix in ('valid', 'bits_weightSlot'))
    require(evaluator.width(valid) == evaluator.width(slot) == 1, 'Unsupported weight output widths')
    checked = 0
    for v0, v1, op0, op1, s0, s1 in itertools.product(range(2), range(2), range(8), range(8), range(2), range(2)):
        assignment = dict(zip(inputs, (v0, v1, op0, op1, s0, s1)))
        p0, p1 = v0 and op0 == 0, v1 and op1 == 0
        require(evaluator.evaluate(valid, assignment) == int(p0 or p1), 'Weight write enable mismatch')
        if p0 or p1:
            require(evaluator.evaluate(slot, assignment) == (s1 if p1 else s0), 'Weight write priority mismatch')
        checked += 1
    return {'assignments_checked': checked, 'valid': 'p0Weight || p1Weight',
            'slot_when_valid': 'p1Weight ? p1Slot : p0Slot',
            'scope': 'Single output request with P1 priority; simultaneous P0/P1 weight writers do not produce two distinct write requests.'}


def accumulator_wrapper_wires(module):
    sequencer, accumulator = find_instance(module, 'seq'), find_instance(module, 'accBuf')
    require(sequencer['instance']['module'] == 'InnerProductTreesSequencer'
            and accumulator['instance']['module'] == 'AccumulationBuffers', 'Unsupported wrapper module binding')
    wires = []
    for source, target in (('io_accComputeReadEn', 'io_computeReadEn'),
                           ('io_accComputeReadAddr_accSel', 'io_computeReadAddr_accSel'),
                           ('io_accComputeReadAddr_rowIdx', 'io_computeReadAddr_rowIdx'),
                           ('io_accStoreReadEn', 'io_storeReadEn'),
                           ('io_accStoreAddr_accSel', 'io_storeAddr_accSel'),
                           ('io_accStoreAddr_rowIdx', 'io_storeAddr_rowIdx')):
        src, dst = instance_value(sequencer, source, 'output'), instance_value(accumulator, target, 'input')
        path = identity_chain(module, src, dst)
        require(path is not None, 'Accumulator wrapper read connection mismatch')
        wires.append({'source': source, 'target': target, 'path': path})
    return wires


def analyze(modules):
    seq, acc = modules['InnerProductTreesSequencer'], modules['AccumulationBuffers']
    require(sum(op['kind'] == 'seq.firmem' for op in acc['operations']) == 2, 'Unexpected accumulator memory count')
    return {'status': 'typed_local_functions_checked',
            'boundary_functions': [boundary_function(seq, port) for port in ('p0', 'p1', 'w0')],
            'accumulator_banks': [accumulator_bank(acc, bank) for bank in (0, 1)],
            'weight_write_function': weight_write_function(seq),
            'accumulator_wrapper_wires': accumulator_wrapper_wires(modules['InnerProductTreesTop']),
            'scope': 'Structural memory topology, exact wrapper wires, and exhaustive local combinational functions only.',
            'limitations': ['No reachable-state, instruction-interval, arithmetic, or universal scheduling proof.',
                            'Does not establish a kernel lower bound; workload and temporal assumptions require separate justification.',
                            'The accumulator mux chooses one read; separate RTL assertions specify that simultaneous compute/store reads of one buffer are illegal.']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--s0-manifest', type=Path, required=True)
    parser.add_argument('--exporter', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True, help='New evidence directory')
    args = parser.parse_args()
    manifest, exporter, output = map(checked_path, (args.s0_manifest, args.exporter, args.output))
    s0 = json.loads(manifest.read_text())
    require(s0.get('status') == 'complete' and s0.get('config') == 'EE290SimConfig', 'Unsupported S0 input')
    hardware = verify_artifact(s0['artifacts']['hardware_ir'])
    output.mkdir(parents=True, exist_ok=False)
    typed_path = output / 'typed.json'
    command = [str(exporter), str(hardware), 'AccumulationBuffers', 'InnerProductTreesSequencer', 'InnerProductTreesTop']
    with typed_path.open('w') as stream:
        subprocess.run(command, stdout=stream, check=True)
    typed = json.loads(typed_path.read_text())
    require(not typed['missing_modules'] and len(typed['modules']) == 3, 'Missing or ambiguous required modules')
    report = {'schema_version': 1, 'kind': 'atlas-mxu1-resource-facts', 'config': 'EE290SimConfig',
              **analyze({module['name']: module for module in typed['modules']}),
              'inputs': {key: artifact(path) for key, path in (('s0', manifest), ('hardware_ir', hardware),
                         ('exporter', exporter), ('driver', checked_path(__file__)))},
              'export_command': command, 'typed': artifact(typed_path)}
    (output / 'bottleneck.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps({'status': report['status'], 'output': str(output / 'bottleneck.json')}))


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, KeyError, subprocess.CalledProcessError) as error:
        raise SystemExit('MXU1 bottleneck extraction failed: ' + str(error))

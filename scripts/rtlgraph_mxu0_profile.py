#!/usr/bin/env python3
"""Extract MXU0 accumulator-read occupancy from the pinned typed CIRCT IR.

This deliberately emits one experimental override, not systolic timing ages.
It exhausts the sequencer's local read-enable function, verifies its wrapper
connections, and checks the physical accumulator read muxes. It neither proves
instruction acceptance nor validates a resulting schedule against execution.
"""

import argparse
import itertools
import json
from pathlib import Path
import re
import subprocess
import sys

from rtlgraph_mxu1 import Graph
from rtlgraph_mxu1_bottleneck import Evaluator, accumulator_bank
from rtlgraph_mxu1_capture import verify_artifact
from rtlgraph_mxu1_profile import require
from rtlgraph_query import find_instance, identity_chain, instance_value
from rtlgraph_s0 import artifact, checked_path


MODULES = ('AtlasCore', 'SystolicArrayTop', 'SystolicArraySequencer', 'AccumulationBuffers')
CUTPOINTS = ('p0Boundary', 'acceptCompute', 'p0CmdValid', 'p0Cmd_op', 'io_cmd_bits_op')


def acc_read_function(module):
    """Check all 512 assignments, including combinations unreachable in RTL."""
    bits = Evaluator(module)
    graph = bits.graph
    values = bits.cuts(CUTPOINTS, [1, 1, 1, 3, 3])
    root = graph.named('io_accComputeReadEn')
    require(bits.width(root) == 1, 'Accumulator read output must be i1')
    cuts = dict(zip(values, CUTPOINTS))
    cone = graph.cone(root, cuts)
    require(not cone['unsupported'] and all(t['reason'] == 'named_cutpoint' for t in cone['terminals']),
            'Unsupported or unresolved accumulator read cone')
    # Restrict this query further than the shared finite-bit-vector evaluator.
    # Type checks apply before enumeration, including constants and wire inputs.
    for op in cone['operations']:
        kind, operands = op['kind'], op['operands']
        result_type = op['result_types']
        operand_types = [bits.types.get(value) for value in operands]
        if kind == 'hw.constant':
            value = op['attributes'].get('value', '')
            match = re.fullmatch(r'-?\d+ : (i[1-9][0-9]*)', value)
            declared = 'i1' if value in ('true', 'false') else match[1] if match else None
            require(not operands and result_type == [declared] and declared in ('i1', 'i3'),
                    'Unsupported accumulator guard constant type')
        elif op['identity_wire']:
            require(len(operands) == 1 and result_type == operand_types and result_type[0] in ('i1', 'i3'),
                    'Unsupported accumulator guard wire type')
        elif kind == 'comb.icmp':
            require(result_type == ['i1'] and operand_types == ['i3', 'i3']
                    and op['attributes'].get('predicate') == '0 : i64', 'Unsupported opcode comparison')
        elif kind in ('comb.and', 'comb.or', 'comb.xor'):
            require(operands and result_type == ['i1'] and all(t == 'i1' for t in operand_types),
                    'Unsupported Boolean operand/result type')
        elif kind == 'comb.mux':
            require(result_type == ['i1'] and operand_types == ['i1', 'i1', 'i1'],
                    'Unsupported Boolean mux types')
        else:
            raise ValueError('Unsupported accumulator guard operation: ' + kind)

    checked = 0
    for assignment in itertools.product(range(2), range(2), range(2), range(8), range(8)):
        boundary, accepted, valid, old_op, new_op = assignment
        expected = int(new_op == 6) if boundary and accepted else int(valid and not boundary and old_op == 6)
        actual = bits.evaluate(root, dict(zip(values, assignment)))
        require(actual == expected, 'MXU0 accumulator read guard mismatch at ' + str(assignment))
        checked += 1
    return {'status': 'opcode_guard_exhaustively_matched', 'assignments_checked': checked,
            'cutpoints': [{'name': name, 'value': value, 'type': bits.types[value]}
                          for name, value in zip(CUTPOINTS, values)],
            'expected_function': '(p0Boundary && acceptCompute) ? (newOp == 6) : (p0CmdValid && !p0Boundary && oldOp == 6)',
            'opcode_interpretation': {'5': 'Matmul', '6': 'MatmulAcc'},
            'overwrite_reads_accumulator': False, 'cone': cone,
            'scope': 'Two-valued combinational equivalence over exact independent i1/i1/i1/i3/i3 cutpoints. No state reachability, acceptance, row timing, or arithmetic claim.'}


def binding(module, instance, expected_module):
    op = find_instance(module, instance)
    require(op['instance']['module'] == expected_module, 'Unexpected module binding for ' + instance)
    return op


def wire(module, source, sink, width, label):
    bits = Evaluator(module)
    require(bits.width(source) == bits.width(sink) == width, 'Unexpected connection width: ' + label)
    path = identity_chain(module, source, sink)
    require(path is not None, 'Unresolved wrapper wire: ' + label)
    # Every intermediate identity preserves the exact width as well.
    for step in path:
        require(bits.width(step['from_value']) == bits.width(step['to_value']) == width,
                'Identity wire changes width: ' + label)
    return {'module': module['name'], 'connection': label, 'source_value': source,
            'sink_value': sink, 'width': width, 'identity_path': path}


def wrapper_wiring(modules):
    top, wrapper = modules['AtlasCore'], modules['SystolicArrayTop']
    mxu = binding(top, 'mxu0', 'SystolicArrayTop')
    seq = binding(wrapper, 'seq', 'SystolicArraySequencer')
    acc = binding(wrapper, 'accBuf', 'AccumulationBuffers')
    checks = []
    for name, width in (('valid', 1), ('bits_op', 3)):
        checks.append(wire(wrapper, Graph(wrapper).named('io_cmd_' + name),
                           instance_value(seq, 'io_cmd_' + name, 'input'), width,
                           'io_cmd_' + name + ' -> seq.io_cmd_' + name))
        # Check AtlasCore has an actual, correctly typed input for this instance.
        # Opcode arithmetic upstream is not interpreted by this local extractor.
        require(Evaluator(top).width(instance_value(mxu, 'io_cmd_' + name, 'input')) == width,
                'Unexpected AtlasCore MXU0 command width')
    for source, target, width in (
        ('io_accComputeReadEn', 'io_computeReadEn', 1),
        ('io_accComputeReadAddr_accSel', 'io_computeReadAddr_accSel', 1),
        ('io_accComputeReadAddr_rowIdx', 'io_computeReadAddr_rowIdx', 5),
        ('io_accStoreReadEn', 'io_storeReadEn', 1),
        ('io_accStoreAddr_accSel', 'io_storeAddr_accSel', 1),
        ('io_accStoreAddr_rowIdx', 'io_storeAddr_rowIdx', 5)):
        checks.append(wire(wrapper, instance_value(seq, source, 'output'),
                           instance_value(acc, target, 'input'), width,
                           'seq.' + source + ' -> accBuf.' + target))
    return {'status': 'direct_wiring_confirmed', 'mxu0_instance': mxu,
            'sequencer_instance': seq, 'accumulator_instance': acc, 'checks': checks,
            'scope': 'MXU0 module binding and exact command/read wires only; no scalar decoding, issue/acceptance equality, or upstream opcode-conversion proof.'}


def analyze(modules):
    require(set(modules) == set(MODULES), 'Missing or unexpected required modules')
    acc = modules['AccumulationBuffers']
    require(sum(op['kind'] == 'seq.firmem' for op in acc['operations']) == 2, 'Unexpected accumulator memory count')
    return {'accumulator_read_guard': acc_read_function(modules['SystolicArraySequencer']),
            'wrapper_wiring': wrapper_wiring(modules),
            'accumulator_banks': [accumulator_bank(acc, bank) for bank in (0, 1)]}


def projection(source_hash, evidence_hash):
    require(all(re.fullmatch('[0-9a-f]{64}', value) for value in (source_hash, evidence_hash)),
            'Projection requires lowercase SHA-256 hashes')
    return ('schema=atlas-mxu0-profile-v1\nconfig=EE290SimConfig\n'
            f'source_ir_sha256={source_hash}\nevidence_sha256={evidence_hash}\n'
            'overwrite_acc_read_hold=0\n')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--s0-manifest', type=Path, required=True)
    parser.add_argument('--exporter', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True, help='New evidence directory')
    args = parser.parse_args()
    manifest, exporter, output = map(checked_path, (args.s0_manifest, args.exporter, args.output))
    s0 = json.loads(manifest.read_text())
    require(s0.get('status') == 'complete' and s0.get('config') == 'EE290SimConfig', 'Wrong or incomplete S0 configuration')
    hardware = verify_artifact(s0['artifacts']['hardware_ir'])
    firrtl = verify_artifact(s0['artifacts']['firrtl'])
    output.mkdir(parents=True, exist_ok=False)
    typed_path = output / 'typed.json'
    command = [str(exporter), str(hardware), *MODULES]
    with typed_path.open('w') as stream:
        subprocess.run(command, stdout=stream, check=True)
    typed = json.loads(typed_path.read_text())
    require(not typed['missing_modules'] and len(typed['modules']) == len(MODULES), 'Missing or duplicate required modules')
    facts = analyze({module['name']: module for module in typed['modules']})
    scripts = checked_path(__file__).parent
    report = {'schema_version': 1, 'kind': 'atlas-partial-mxu0-profile', 'config': 'EE290SimConfig',
              'status': 'typed_accumulator_read_occupancy_derived', **facts,
              'scope': 'Experimental one-field profile from local structural and combinational evidence. No simulation or temporal proof is required or claimed by this extractor.',
              'inputs': {name: artifact(path) for name, path in (('s0', manifest), ('hardware_ir', hardware),
                         ('firrtl', firrtl), ('exporter', exporter), ('driver', checked_path(__file__)))},
              'analysis_dependencies': {name: artifact(scripts / name) for name in (
                  'rtlgraph_mxu1.py', 'rtlgraph_mxu1_bottleneck.py', 'rtlgraph_mxu1_capture.py',
                  'rtlgraph_mxu1_profile.py', 'rtlgraph_query.py', 'rtlgraph_s0.py')},
              'export_command': command, 'typed': artifact(typed_path),
              'compiler_overrides': {'overwrite_acc_read_hold': 0},
              'inherited_not_extracted': {'first_accumulator_write_age': 63, 'same_accumulator_reuse_gap': 64,
                                          'same_weight_slot_compute_to_push_gap': 63},
              'limitations': ['This is not a complete scheduling model or a universal schedule-safety proof.',
                              'Opcode names use pinned MxuOp encodings: Matmul=5 and MatmulAcc=6.',
                              'Acceptance, initialization, row sequencing, release, and scalar alignment remain unchecked.',
                              'Physical read mux evidence does not authorize simultaneous accumulating-compute and pop reads.',
                              'Systolic ages, other resources, and instruction profiles remain inherited.',
                              'Each emitted candidate needs independent functional RTL validation; temporal event validation remains open.']}
    report_path = output / 'profile.json'
    report_path.write_text(json.dumps(report, indent=2) + '\n')
    projected = output / 'atlas-mxu0.profile'
    projected.write_text(projection(artifact(hardware)['sha256'], artifact(report_path)['sha256']))
    print(json.dumps({'status': report['status'], 'profile': str(report_path), 'compiler_projection': str(projected),
                      'guard_assignments': facts['accumulator_read_guard']['assignments_checked']}))


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, KeyError, subprocess.CalledProcessError) as error:
        raise SystemExit('MXU0 profile extraction failed: ' + str(error))

#!/usr/bin/env python3
"""Extract local LSU control recurrences and wiring from typed CIRCT.

This proves selected local Boolean functions and synchronous register recurrences.
It intentionally does not infer a complete FSM trajectory or fixed issue age.
"""
import argparse
import itertools
import json
from pathlib import Path
import subprocess

from rtlgraph_mxu1 import Graph
from rtlgraph_mxu1_capture import verify_artifact
from rtlgraph_mxu1_profile import constant, require
from rtlgraph_query import find_instance, identity_chain, instance_value
from rtlgraph_s0 import artifact, checked_path
from rtlgraph_vpu import Cone


def truth(module, root, names, widths, expected, law):
    graph = Graph(module)
    cone = Cone(module, root, [graph.named(n) for n in names], list(widths), 1)
    checked = 0
    for assignment in itertools.product(*(range(1 << width) for width in widths)):
        require(cone(assignment) == int(expected(*assignment)), 'Local LSU function mismatch: ' + law)
        checked += 1
    return {'law': law, 'cutpoints': list(names), 'assignments_checked': checked, 'cone': cone.evidence}


def register(module, name):
    graph = Graph(module)
    op = graph.definitions[graph.named(name)]
    require(op['kind'] == 'seq.firreg' and op['result_types'] == ['i1'] and not op['has_regions']
            and len(op['operands']) == 4, 'Unsupported LSU control register')
    require(set(op['attributes']) <= {'firrtl.random_init_start', 'inner_sym', 'name', 'sv.namehint'},
            'Unsupported LSU control register attributes')
    data, clock, reset, initial = op['operands']
    require(clock == graph.named('clock') and reset == graph.named('reset')
            and constant(graph.definitions[initial]) == 0, 'Unsupported LSU clock/reset')
    return op, data


def analyze_lsu(module):
    graph = Graph(module)
    facts = []
    registers = []
    for kind, opcode, request in (('vload', 1, 'io_vmemVecRead_valid'), ('vstore', 2, 'io_mregReadReq_valid')):
        facts.append(truth(module, graph.named('issue' + kind[0].upper() + kind[1:] + 'Cmd'),
                           ('io_cmd_valid', 'io_cmd_bits_op'), (1, 2),
                           lambda valid, op, expected=opcode: valid and op == expected,
                           f'{kind} issue = command.valid && command.op == {opcode}'))
        facts.append(truth(module, graph.named(request), (kind + 'State',), (2,), lambda state: state == 1,
                           f'{request} = {kind}State == RUN(1)'))
        pending = ('vloadRespPending', 'vloadWritePending') if kind == 'vload' else ('vstoreRespPending_d', 'vstoreRespPending_q')
        facts.append(truth(module, graph.named('io_' + kind + 'Busy'), (kind + 'State', *pending), (2, 1, 1),
                           lambda state, first, second: state != 0 or first or second,
                           f'{kind} busy = non-IDLE state || pending first stage || pending second stage'))
    for name, source in (('vloadRespPending', 'vloadIssueRead'),
                         ('vstoreRespPending_d', 'vstoreIssueRead'),
                         ('vstoreRespPending_q', 'vstoreRespPending_d'),
                         ('mregReadRespValid_q', 'io_mregReadResp_valid')):
        op, data = register(module, name)
        facts.append(truth(module, data, (source,), (1,), lambda valid: valid,
                           f'next({name}) = {source} when reset is low; synchronous reset clears it'))
        registers.append(op)
    op, data = register(module, 'vloadWritePending')
    facts.append(truth(module, data, ('io_vmemVecReadData_valid', 'vloadRespPending'), (1, 1),
                       lambda response, pending: response and pending,
                       'next(vloadWritePending) = memory response valid && pending request when reset is low'))
    registers.append(op)
    facts.append(truth(module, graph.named('io_mregWriteReq_valid'), ('vloadWritePending',), (1,),
                       lambda pending: pending, 'MREG write valid = vloadWritePending'))
    facts.append(truth(module, graph.named('io_vmemVecWrite_valid'),
                       ('mregReadRespValid_q', 'vstoreRespPending_q'), (1, 1),
                       lambda response, pending: response and pending,
                       'VMEM write valid = registered MREG response valid && second pending stage'))
    for output, source in (('io_activeMregWrite_valid', 'io_vloadBusy'), ('io_activeMregRead_valid', 'io_vstoreBusy')):
        # Output aliases can point to a shared internal value rather than each other.
        internal = 'vloadBusy' if 'Write' in output else 'vstoreBusy'
        facts.append(truth(module, graph.named(output), (internal,), (1,), lambda busy: busy,
                           f'{output} = {source}'))
    return {'functions': facts, 'registers': registers,
            'assignments_checked': sum(f['assignments_checked'] for f in facts),
            'independence_scope': 'Each extracted load/store request and busy cone references only its own state/pending cutpoints. VPU activity does not gate these functions. Memory/register conflicts remain software obligations; independence is not permission to violate those obligations.'}


def wrapper(module):
    checks = []
    for name, expected in (('lsu', 'LSU'), ('scalar', 'ScalarCore'), ('mreg', 'MregFile'), ('vmem', 'Vmem')):
        require(find_instance(module, name)['instance']['module'] == expected, 'Unsupported LSU wrapper module binding')
    for source_instance, source_port, sink_instance, sink_port in (
        ('scalar', 'io_lsuCmd_valid', 'lsu', 'io_cmd_valid'),
        ('scalar', 'io_lsuCmd_bits_op', 'lsu', 'io_cmd_bits_op'),
        ('scalar', 'io_lsuCmd_bits_mregBank', 'lsu', 'io_cmd_bits_mregBank'),
        ('scalar', 'io_lsuCmd_bits_vmemLineAddr', 'lsu', 'io_cmd_bits_vmemLineAddr'),
        ('lsu', 'io_mregReadReq_valid', 'mreg', 'io_lsuReadReq_valid'),
        ('mreg', 'io_lsuReadResp_valid', 'lsu', 'io_mregReadResp_valid'),
        ('lsu', 'io_mregWriteReq_valid', 'mreg', 'io_lsuWriteReq_valid'),
        ('lsu', 'io_vmemVecRead_valid', 'vmem', 'io_lsuVecRead_valid'),
        ('vmem', 'io_lsuVecReadData_valid', 'lsu', 'io_vmemVecReadData_valid'),
        ('lsu', 'io_vmemVecWrite_valid', 'vmem', 'io_lsuVecWrite_valid'),
        ('lsu', 'io_vloadBusy', 'scalar', 'io_lsu_vload_busy'),
        ('lsu', 'io_vstoreBusy', 'scalar', 'io_lsu_vstore_busy'),
    ):
        source_op, sink_op = find_instance(module, source_instance), find_instance(module, sink_instance)
        source = instance_value(source_op, source_port, 'output')
        sink = instance_value(sink_op, sink_port, 'input')
        path = identity_chain(module, source, sink)
        require(path is not None, 'Unsupported LSU wrapper connection')
        checks.append({'source': source_instance + '.' + source_port, 'sink': sink_instance + '.' + sink_port,
                       'source_location': source_op['location'], 'sink_location': sink_op['location'], 'path': path})
    return checks


def analyze(modules):
    return {'status': 'typed_local_lsu_functions_checked', 'lsu': analyze_lsu(modules['LSU']),
            'wrapper_wires': wrapper(modules['AtlasCore']),
            'limitations': ['No complete FSM reachability, counter progression, row-address/payload, or fixed command-age proof.',
                            'No proof of same-cycle SRAM visibility, cross-engine bank safety, scalar issue legality, or memory response latency.',
                            'The explicit cutpoint functions and resettable recurrence hold locally; composition with external memory behavior requires separate assumptions/evidence.',
                            'Command opcodes and state labels are source-correlated; all functions and register structures are checked from typed CIRCT.',
                            'Cached simulator source-to-binary lineage remains unverified.']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--s0-manifest', type=Path, required=True)
    parser.add_argument('--exporter', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    s0_path, exporter, output = (checked_path(p) for p in (args.s0_manifest, args.exporter, args.output))
    require(not output.exists(), 'Output must be a new evidence directory')
    s0 = json.loads(s0_path.read_text())
    require(s0['config'] == 'EE290SimConfig' and s0['status'] == 'complete', 'Requires complete EE290SimConfig S0')
    hardware = verify_artifact(s0['artifacts']['hardware_ir'])
    inputs = {'s0': artifact(s0_path), 'exporter': artifact(exporter), 'hardware_ir': artifact(hardware),
              'driver': artifact(checked_path(__file__))}
    for name in ('rtlgraph_mxu1.py', 'rtlgraph_mxu1_profile.py', 'rtlgraph_mxu1_capture.py',
                 'rtlgraph_vpu.py', 'rtlgraph_query.py', 'rtlgraph_s0.py'):
        inputs[name] = artifact(checked_path(Path(__file__).with_name(name)))
    output.mkdir(parents=True)
    typed_path = output / 'typed.json'
    command = [str(exporter), str(hardware), 'LSU', 'AtlasCore']
    with typed_path.open('w') as stream: subprocess.run(command, stdout=stream, check=True)
    typed = json.loads(typed_path.read_text())
    require(not typed['missing_modules'] and len(typed['modules']) == 2, 'Missing/ambiguous LSU modules')
    report = analyze({m['name']: m for m in typed['modules']})
    report.update(schema='atlas.rtlgraph.lsu-local-functions.v1', config=s0['config'], inputs=inputs,
                  typed=artifact(typed_path), export_command=command)
    for record in inputs.values(): verify_artifact(record)
    (output / 'lsu.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps({'status': report['status'], 'assignments': report['lsu']['assignments_checked'],
                      'output': str(output / 'lsu.json')}))


if __name__ == '__main__': main()

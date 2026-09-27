#!/usr/bin/env python3
"""Derive exact local VPU valid-pipeline laws from typed CIRCT register chains.

This rejects enables, muxes, hidden inputs, clock changes and unsupported state.
It proves a local control recurrence, not complete instruction access timing or
arithmetic correctness. Instruction ages still require the wrapper/FSM/MREG
composition checked separately by a temporal capture.
"""

import argparse
import json
from pathlib import Path
import subprocess

from rtlgraph_mxu1 import Graph
from rtlgraph_mxu1_capture import verify_artifact
from rtlgraph_mxu1_profile import constant, require
from rtlgraph_s0 import artifact, checked_path


MODULES = ('AddSubSumVec', 'MulRec', 'Rcp', 'Sqrt', 'Exp', 'SquareCubeVec',
           'RowMax', 'ReduSumRec', 'VectorLoadImm', 'TanhRec', 'Log',
           'PairWiseMax', 'PairWiseMin', 'RowMin', 'ColAddVec', 'Mov', 'Relu', 'SinCosVec')
BENIGN_REG_ATTRS = frozenset(('firrtl.random_init_start', 'inner_sym', 'name', 'sv.namehint'))


def analyze(module):
    graph = Graph(module)
    ports = {p['name']: p for p in module['ports']}
    require(len(ports) == len(module['ports']), 'Ambiguous module ports')
    request, response = ports['io_req_valid'], ports['io_resp_valid']
    require(request['direction'] == 'input' and response['direction'] == 'output'
            and request['type'] == response['type'] == 'i1', 'Unsupported valid port direction or width')
    types = {p['value']: p['type'] for p in module['ports'] if p['direction'] == 'input'}
    types.update((v, t) for op in module['operations'] for v, t in zip(op['results'], op['result_types']))
    visited, path, registers = set(), [], []
    value = response['value']
    while value != request['value']:
        require(value not in visited, 'Feedback in valid chain')
        visited.add(value)
        op = graph.definitions.get(value)
        require(op is not None and op['result_types'] == ['i1'] and len(op['results']) == 1
                and not op.get('has_regions'), 'Unsupported or unresolved valid-chain operation')
        path.append({'id': op['id'], 'kind': op['kind'], 'location': op['location'], 'attributes': op['attributes']})
        if op['kind'] == 'hw.wire' and op['identity_wire']:
            require(len(op['operands']) == 1 and types.get(op['operands'][0]) == 'i1', 'Invalid identity wire')
        elif op['kind'] == 'seq.firreg':
            require(set(op['attributes']) <= BENIGN_REG_ATTRS, 'Unsupported register attributes')
            require(len(op['operands']) in (2, 4), 'Enabled or unsupported valid register')
            data, clock = op['operands'][:2]
            require(types.get(data) == 'i1' and ports.get('clock', {}).get('value') == clock
                    and types.get(clock) == '!seq.clock', 'Changed clock or register data width')
            reset = None
            if len(op['operands']) == 4:
                reset_value, initial = op['operands'][2:]
                require(ports.get('reset', {}).get('value') == reset_value and types.get(reset_value) == 'i1',
                        'Unsupported reset source')
                definition = graph.definitions.get(initial)
                require(definition is not None and definition['kind'] == 'hw.constant'
                        and definition['result_types'] == ['i1'] and constant(definition) == 0,
                        'Valid register reset is not constant zero')
                reset = reset_value
            registers.append({'id': op['id'], 'name': op['attributes'].get('name'), 'input': data,
                              'clock': clock, 'reset': reset, 'location': op['location']})
        else:
            raise ValueError('Unsupported valid-chain operation: ' + op['kind'])
        value = op['operands'][0]
    latency = len(registers)
    return {'module': module['name'], 'status': 'exact_local_valid_shift_chain',
            'pipeline_cycles': latency, 'registers_output_to_input': registers, 'path': path,
            'request_value': request['value'], 'response_value': response['value'],
            'has_unreset_stages': any(r['reset'] is None for r in registers),
            'law': f'With reset deasserted across the interval, resp.valid at edge t equals req.valid at edge t-{latency}, after {latency} clock edges of history.',
            'initialization_scope': 'Initial output before the recorded history is not constrained. Resettable stages synchronously clear to zero; unreset stages require history to flush unknown initial state.',
            'proof_scope': 'Structural recurrence of one-bit valid only; every intermediate operation is an identity wire or unconditional register on the module clock.'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--s0-manifest', type=Path, required=True)
    parser.add_argument('--exporter', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    s0_path, exporter, output = (checked_path(p) for p in (args.s0_manifest, args.exporter, args.output))
    require(not output.exists(), 'Output must be a new evidence directory')
    s0 = json.loads(s0_path.read_text())
    require(s0['config'] == 'EE290SimConfig' and s0['status'] == 'complete', 'Requires successful EE290SimConfig S0')
    hardware = verify_artifact(s0['artifacts']['hardware_ir'])
    inputs = {'s0': artifact(s0_path), 'exporter': artifact(exporter), 'hardware_ir': artifact(hardware),
              'driver': artifact(checked_path(__file__))}
    for name in ('rtlgraph_mxu1.py', 'rtlgraph_mxu1_profile.py', 'rtlgraph_mxu1_capture.py', 'rtlgraph_s0.py'):
        inputs[name] = artifact(checked_path(Path(__file__).with_name(name)))
    output.mkdir(parents=True)
    typed_path = output / 'typed.json'
    command = [str(exporter), str(hardware), *MODULES]
    with typed_path.open('w') as stream: subprocess.run(command, stdout=stream, check=True)
    typed = json.loads(typed_path.read_text())
    require(not typed['missing_modules'] and len(typed['modules']) == len(MODULES)
            and {m['name'] for m in typed['modules']} == set(MODULES), 'Missing or ambiguous functional units')
    report = {'schema': 'atlas.rtlgraph.vpu-valid-pipelines.v1', 'config': s0['config'],
              'status': 'exact_local_valid_shift_chains', 'modules': [analyze(m) for m in typed['modules']],
              'inputs': inputs, 'typed': artifact(typed_path), 'export_command': command,
              'limitations': ['No instruction-acceptance, operand-read, write-bank, slot-lifetime or MREG visibility proof is implied by local functional-unit valid timing.',
                              'Data arithmetic and data/valid alignment are outside these one-bit recurrences.',
                              'Cached simulator source-to-binary linkage remains unverified; static facts describe the recorded hardware IR.']}
    for saved in inputs.values(): verify_artifact(saved)
    (output / 'pipelines.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps({'status': report['status'], 'pipelines': {m['module']: m['pipeline_cycles'] for m in report['modules']},
                      'output': str(output / 'pipelines.json')}))


if __name__ == '__main__': main()

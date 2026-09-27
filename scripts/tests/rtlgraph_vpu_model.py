#!/usr/bin/env python3
"""Corroborate inherited compiler overlap policy against retained typed VPU facts.

This builds a tiny probe against an existing libatlas.a. It does not generate
instruction timing, establish state reachability, or change the compiler model.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from rtlgraph_mxu1_capture import verify_artifact
from rtlgraph_mxu1_profile import require
from rtlgraph_s0 import artifact, checked_path
from rtlgraph_vpu import OPS, issue_guard


# Explicit source-enum correlation, not recovered assembler encoding or a name
# heuristic. VectorIO.scala declares inner values 0..29; outer issue bits add 1.
NAMES = ('vadd.bf16', 'vsub.bf16', 'vmul.bf16', 'vrecip.bf16', 'vsqrt.bf16',
         'vsin.bf16', 'vcos.bf16', 'vtanh.bf16', 'vlog2.bf16', 'vexp.bf16',
         'vexp2.bf16', 'vsquare.bf16', 'vcube.bf16', 'vredsum.row.bf16',
         'vredsum.bf16', None, 'vpack.bf16.fp8', 'vunpack.fp8.bf16', 'vrelu.bf16',
         'vredmax.row.bf16', 'vredmin.row.bf16', 'vredmax.bf16', 'vredmin.bf16',
         'vmaximum.bf16', 'vminimum.bf16', 'vmov', 'vli.one', 'vli.col', 'vli.row', 'vli.all')
CLASSES = {13: 'row_reduce', 19: 'row_reduce', 20: 'row_reduce',
           14: 'column_reduce', 21: 'column_reduce', 22: 'column_reduce',
           16: 'pack', 17: 'unpack', 26: 'immediate_single', 27: 'immediate_single',
           28: 'immediate_pair', 29: 'immediate_pair'}
SUPPORTED = [i for i, name in enumerate(NAMES) if name is not None]
EXCLUDED = [
    {'inner_opcode': 15, 'source_label': 'fp8',
     'reason': 'Source enum entry has no generic compiler opcode. Pack and unpack have distinct inner opcodes 16 and 17; neither is substituted.'},
    {'inner_opcodes': [30, 31],
     'reason': 'Outside the declared VPUOp enum; included as resident cutpoint bit patterns in extraction, not supported compiler instructions.'},
    {'issue_busy_bit': 0, 'reason': 'Reserved scalar VPU_NONE bit, not an instruction.'},
]


def compare(guard, probe):
    require(guard['encoding'] == [{'opcode': i, 'issue_busy_bit': i + 1, 'source_label': name}
                                  for i, name in enumerate(OPS)], 'Unsupported source-correlated opcode encoding')
    require(guard['assignments_checked'] == 16384, 'Incomplete issue-guard domain')
    rows = guard['one_active_instruction_overlap']
    require(len(rows) == 30 and all(row['active_opcode'] == i and row['active_name'] == OPS[i]
            and type(row['state_cutpoint']) is int and row['state_cutpoint'] in (1, 2)
            and isinstance(row['allowed_incoming_opcodes'], list)
            and all(type(x) is int and 0 <= x < 30 for x in row['allowed_incoming_opcodes'])
            and len(set(row['allowed_incoming_opcodes'])) == len(row['allowed_incoming_opcodes'])
            for i, row in enumerate(rows)), 'Incomplete or malformed typed overlap matrix')
    both = guard['both_read_slots']
    require(isinstance(both, list) and all(type(x) is int and 0 <= x < 30 for x in both)
            and len(set(both)) == len(both)
            and all((row['state_cutpoint'] == 2) == (i in both) for i, row in enumerate(rows)),
            'Invalid typed slot classes')
    require(probe.get('schema_version') == 1 and probe.get('kind') == 'atlas-vpu-compiler-probe',
            'Unsupported compiler probe')
    ops = probe['operations']
    require(len(ops) == len(SUPPORTED) and [op['name'] for op in ops] == [NAMES[i] for i in SUPPORTED],
            'Missing, reordered, or unsupported compiler operation')
    matrix = probe['can_overlap']
    require(len(matrix) == len(SUPPORTED) and all(len(row) == len(SUPPORTED)
            and all(type(x) is bool for x in row) for row in matrix), 'Malformed compiler overlap matrix')
    mapping = []
    for index, opcode in enumerate(SUPPORTED):
        op = ops[index]
        require(op['class'] == CLASSES.get(opcode, 'elementwise'), 'Compiler class mismatch: ' + op['name'])
        require(type(op['both_slots']) is bool and op['both_slots'] == (opcode in both),
                'Compiler slot class mismatch: ' + op['name'])
        for other_index, other_opcode in enumerate(SUPPORTED):
            require(matrix[index][other_index] == (other_opcode in rows[opcode]['allowed_incoming_opcodes']),
                    'Compiler overlap mismatch: ' + op['name'] + ' -> ' + NAMES[other_opcode])
        mapping.append({'inner_opcode': opcode, 'source_label': OPS[opcode], 'issue_busy_bit': opcode + 1, **op})
    return {'status': 'inherited_vpu_overlap_compatible', 'operations_checked': len(SUPPORTED),
            'slot_class_checks': len(SUPPORTED), 'ordered_pair_checks': len(SUPPORTED) ** 2,
            'mapping': mapping, 'excluded': EXCLUDED,
            'scope': 'Source-correlated operations agree with the actual linked compiler functions for the extracted one-unfinished-instruction cutpoint matrix. No new latency, slot lifetime, state invariant, complete ISA decoder mapping, or speedup is established.'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--vpu-report', type=Path, required=True)
    parser.add_argument('--compiler', type=Path, required=True)
    parser.add_argument('--library', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    source = checked_path(Path(__file__).with_suffix('.cpp'))
    root = source.parents[2]
    report_path, compiler, library, output = map(checked_path, (args.vpu_report, args.compiler, args.library, args.output))
    inputs = {name: artifact(path) for name, path in (('vpu_report', report_path),
              ('compiler', compiler), ('library', library), ('probe_source', source), ('driver', checked_path(__file__)))}
    for relative in ('src/core/asm.cpp', 'src/core/asm.h', 'src/core/machine.cpp', 'src/core/machine.h',
                     'scripts/rtlgraph_vpu.py', 'scripts/rtlgraph_mxu1.py', 'scripts/rtlgraph_query.py',
                     'scripts/rtlgraph_mxu1_profile.py', 'scripts/rtlgraph_mxu1_capture.py', 'scripts/rtlgraph_s0.py'):
        inputs[relative] = artifact(checked_path(root / relative))
    report = json.loads(report_path.read_text())
    require(report.get('schema_version') == 1 and report.get('kind') == 'atlas-vpu-local-facts'
            and report.get('config') == 'EE290SimConfig' and report.get('status') == 'typed_vpu_local_functions_checked',
            'Expected completed pinned VPU extraction')
    typed_path = verify_artifact(report['typed'])
    inputs['typed'] = artifact(typed_path)
    require(inputs['typed']['sha256'] == report['typed']['sha256'], 'Retained typed input changed')
    # Re-evaluate the recorded typed module instead of trusting an edited matrix.
    typed = json.loads(typed_path.read_text())
    fsms = [m for m in typed['modules'] if m['name'] == 'VectorFSM']
    require(not typed['missing_modules'] and len(fsms) == 1, 'Missing or ambiguous retained VectorFSM')
    extracted = issue_guard(fsms[0])
    require(report['issue_guard'] == extracted, 'Retained issue guard differs from typed re-evaluation')
    output.mkdir(parents=True, exist_ok=False)
    executable = output / 'rtlgraph-vpu-model'
    build = [str(compiler), '-std=c++20', '-O2', '-Wall', '-Wextra', '-Wpedantic', '-I' + str(root / 'src'),
             str(source), str(library), '-o', str(executable)]
    with (output / 'build.log').open('w') as log:
        subprocess.run(build, stdout=log, stderr=subprocess.STDOUT, check=True)
    command = [str(executable), *(NAMES[i] for i in SUPPORTED)]
    probe_path = output / 'probe.json'
    probe_path.write_text(subprocess.check_output(command, text=True))
    joined = compare(extracted, json.loads(probe_path.read_text()))
    for saved in inputs.values():
        require(artifact(checked_path(saved['path'])) == saved, 'Input changed during compatibility check')
    result = {'schema_version': 1, 'kind': 'atlas-vpu-model-compatibility', 'config': 'EE290SimConfig', **joined,
              'inputs': inputs, 'build_command': build, 'probe_command': command,
              'probe': artifact(probe_path), 'executable': artifact(executable), 'build_log': artifact(output / 'build.log'),
              'provenance_scope': 'Probe was compiled in this check and linked to the hashed pre-existing library. Current compiler source hashes are context; this check does not rebuild or prove the source lineage of libatlas.a.'}
    (output / 'compatibility.json').write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps({key: result[key] for key in ('status', 'operations_checked', 'slot_class_checks', 'ordered_pair_checks')}))


if __name__ == '__main__':
    try: main()
    except (OSError, ValueError, KeyError, TypeError, subprocess.CalledProcessError) as error:
        raise SystemExit('VPU model compatibility check failed: ' + str(error))

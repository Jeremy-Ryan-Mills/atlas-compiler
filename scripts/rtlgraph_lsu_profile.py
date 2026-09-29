#!/usr/bin/env python3
"""Project rechecked conditional LSU row and occupancy timing into atlas-opt."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from rtlgraph_lsu_routing import analyze
from rtlgraph_mxu1_capture import verify_artifact
from rtlgraph_mxu1_profile import require
from rtlgraph_s0 import artifact


def settings(facts):
    require(facts['status'] == 'typed_conditional_lsu_routing_derived', 'Unchecked LSU routing facts')
    require(facts['operand_capture'] == 'issue', 'Unsupported LSU operand capture')
    result = dict(rows=32, row_step=1, operand_capture=facts['operand_capture'])
    for kind in ('vload', 'vstore'):
        events = facts['lsu_timing']['temporal']['events'][kind]
        for event, access in (('requests', 'read'), ('writes', 'write')):
            rows = events[event]
            require(len(rows) == 32 and [row['row'] for row in rows] == list(range(32)),
                    'Unsupported LSU row geometry or ordering')
            age = rows[0]['age']
            require(type(age) is int and 1 <= age <= 64 and
                    [row['age'] for row in rows] == list(range(age, age + 32)),
                    'Unsupported LSU row timing or stride')
            result[f'{kind}_{access}_age'] = age
        free = events['first_not_busy_age']
        require(type(free) is int and 1 <= free <= 128 and
                result[f'{kind}_write_age'] > result[f'{kind}_read_age'] and
                free > events['writes'][-1]['age'], 'Unsupported LSU drain timing')
        result[f'{kind}_first_free_age'] = free
    return result


def checked_facts(report, typed):
    require(report.get('schema') == 'atlas.rtlgraph.lsu-routing.v1' and
            report.get('config') == 'EE290SimConfig', 'Wrong LSU evidence schema or hardware configuration')
    modules = {module['name']: module for module in typed['modules']}
    require(not typed['missing_modules'] and len(modules) == len(typed['modules']) == 4 and
            set(modules) == {'LSU', 'AtlasCore', 'MregFile', 'Vmem'},
            'Missing or ambiguous typed LSU modules')
    fresh = analyze(modules)
    require(all(report.get(key) == value for key, value in fresh.items()),
            'Recorded LSU evidence differs from fresh typed analysis')
    return fresh


def prepare(evidence_path, output):
    report = json.loads(evidence_path.read_text())
    for record in report['inputs'].values():
        verify_artifact(record)
    typed_path = verify_artifact(report['typed'])
    local = json.loads(verify_artifact(report['inputs']['evidence']).read_text())
    require(local['schema'] == 'atlas.rtlgraph.lsu-local-functions.v1' and
            local['config'] == report['config'] == 'EE290SimConfig' and
            local['inputs']['hardware_ir']['sha256'] == report['inputs']['hardware_ir']['sha256'],
            'LSU routing and local evidence lineage differ')
    for record in local['inputs'].values():
        verify_artifact(record)
    s0 = json.loads(verify_artifact(local['inputs']['s0']).read_text())
    require(s0['status'] == 'complete' and s0['config'] == 'EE290SimConfig' and
            s0['artifacts']['hardware_ir']['sha256'] == report['inputs']['hardware_ir']['sha256'],
            'LSU evidence and elaboration lineage differ')
    facts = checked_facts(report, json.loads(typed_path.read_text()))
    overrides = settings(facts)
    inputs = dict(evidence=artifact(evidence_path), typed=artifact(typed_path),
                  hardware_ir=report['inputs']['hardware_ir'], driver=artifact(Path(__file__).resolve()))
    dependencies = {name: artifact(Path(__file__).with_name(name).resolve()) for name in
                    ('rtlgraph_lsu_routing.py', 'rtlgraph_lsu_response.py', 'rtlgraph_lsu_timing.py',
                     'rtlgraph_lsu.py', 'rtlgraph_dma.py', 'rtlgraph_mxu1.py', 'rtlgraph_mxu1_profile.py',
                     'rtlgraph_mxu1_capture.py', 'rtlgraph_query.py', 'rtlgraph_s0.py', 'rtlgraph_vpu.py')}
    canonical = dict(schema='atlas.rtlgraph.lsu-profile.v1', config='EE290SimConfig',
                     status='typed_conditional_facts_rechecked', inputs=inputs,
                     analysis_dependencies=dependencies, compiler_overrides=overrides,
                     age_zero='Accepted decoded LSU command; operand registers captured on that edge.',
                     hold_interval='Path reserved from issue through first_free_age - 1 inclusive; VMEM bank held at its row access ages.',
                     assumptions=facts['remaining_assumptions'],
                     scope='Partial VLOAD/VSTORE operand-access and physical resource timing under the checked routing/arbitration conditions.',
                     evidence_categories=dict(
                         structural=['Command operand capture, row/address and payload routing, memory enables and write acceptance'],
                         local_function=['Memory arbitration and one-cycle source responses under physical-bank exclusions'],
                         conditional_temporal=['Ordered 32-row request/write ages and first idle edge under stated assumptions'],
                         inherited=['Logical MREG reservation policy and same-cycle visibility; reservation release follows the selected path lifetime',
                                    'Operand-resolved resource identities, physical-port exclusion rules and all unselected instruction timings'],
                         unproved=['Whole-kernel collision freedom and numerical correctness for arbitrary programs',
                                   'Universal DMA liveness, scalar decoder legality and cached simulator build lineage']),
                     limitations=['Local finite-domain and bounded recurrence checks are not an unbounded hardware proof.',
                                  'Known illegal VMEM addresses are rejected; unresolved bases require aligned, in-range, nonwrapping runtime inputs.',
                                  'Each newly emitted schedule requires independent functional RTL replay.'])
    output.mkdir(parents=True, exist_ok=False)
    path = output / 'profile.json'
    path.write_text(json.dumps(canonical, indent=2) + '\n')
    projection = dict(schema='atlas-lsu-profile-v1', config='EE290SimConfig',
                      source_ir_sha256=report['inputs']['hardware_ir']['sha256'],
                      evidence_sha256=artifact(path)['sha256'], **overrides)
    projected = output / 'atlas-lsu.profile'
    projected.write_text(''.join(f'{key}={value}\n' for key, value in projection.items()))
    for record in [*inputs.values(), *dependencies.values()]:
        verify_artifact(record)
    return dict(status=canonical['status'], profile=str(path), compiler_projection=str(projected), timings=overrides)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--lsu-evidence', type=Path, required=True, help='Checked lsu-routing.json')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(prepare(args.lsu_evidence.resolve(), args.output.resolve())))


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise SystemExit(f'rtlgraph_lsu_profile: {error}')

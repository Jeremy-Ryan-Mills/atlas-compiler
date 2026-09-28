#!/usr/bin/env python3
"""Project checked local CIRCT DMA facts into the native compiler model.

Re-evaluates the typed graph rather than trusting an evidence status string.
The projection has no DMA latency; completion remains an explicit wait event.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from rtlgraph_dma import analyze
from rtlgraph_mxu1_capture import verify_artifact
from rtlgraph_mxu1_profile import require
from rtlgraph_s0 import artifact


def settings(facts):
    require(facts['status'] == 'typed_local_dma_functions_checked', 'Unchecked DMA facts')
    geometry = facts['geometry']
    address = facts['wrapper']['word_address_to_line']
    size = facts['wrapper']['transfer_size_low_bits']
    require(geometry == dict(command_slots=8, channels=8, vmem_banks=6,
                            bank_line_address_bits=13, line_bytes=32,
                            dma_vmem_word_address_mask='0x7ffff', dma_transfer_size_bits=13),
            'Unsupported DMA geometry')
    require(address['low_bit'] == 3 and address['width'] == 16 and
            size['low_bit'] == 0 and size['width'] == 13, 'Unsupported DMA address or size projection')
    return dict(operand_capture='issue', config_update='issue',
                vmem_word_address_low_bit=address['low_bit'], vmem_line_address_bits=address['width'],
                transfer_size_bits=size['width'], vmem_line_bytes=geometry['line_bytes'],
                vmem_lines=geometry['vmem_banks'] * (1 << geometry['bank_line_address_bits']),
                channels=geometry['channels'], command_slots=geometry['command_slots'],
                supported_max_transfer_bytes=4096, completion='explicit-wait', lsu_priority_over_dma=1)


def checked_facts(report, typed):
    require(report.get('schema') == 'atlas.rtlgraph.dma-local-functions.v1' and
            report.get('config') == 'EE290SimConfig', 'Wrong DMA evidence schema or hardware configuration')
    modules = {module['name']: module for module in typed['modules']}
    require(not typed['missing_modules'] and len(modules) == len(typed['modules']) == 4 and
            set(modules) == {'ScalarCore', 'DmaEngine', 'Vmem', 'AtlasCore'},
            'Missing or ambiguous typed DMA modules')
    fresh = analyze(modules)
    require(all(report.get(key) == value for key, value in fresh.items()),
            'Recorded DMA evidence differs from fresh typed analysis')
    return fresh


def prepare(evidence_path, output):
    report = json.loads(evidence_path.read_text())
    for record in report['inputs'].values():
        verify_artifact(record)
    typed_path = verify_artifact(report['typed'])
    s0 = json.loads(verify_artifact(report['inputs']['s0']).read_text())
    require(s0['status'] == 'complete' and s0['config'] == 'EE290SimConfig' and
            s0['artifacts']['hardware_ir']['sha256'] == report['inputs']['hardware_ir']['sha256'],
            'DMA evidence and elaboration lineage differ')
    facts = checked_facts(report, json.loads(typed_path.read_text()))
    overrides = settings(facts)
    output.mkdir(parents=True, exist_ok=False)
    canonical = dict(schema='atlas.rtlgraph.dma-profile.v1', config='EE290SimConfig',
                     status='typed_local_facts_rechecked',
                     inputs=dict(evidence=artifact(evidence_path), typed=artifact(typed_path),
                                 hardware_ir=report['inputs']['hardware_ir'],
                                 driver=artifact(Path(__file__).resolve())),
                     compiler_overrides=overrides,
                     scope='Partial DMA semantics for the existing atlas-opt machine model; other units and numerical local timings remain inherited.',
                     evidence_categories=dict(
                         structural=['Command payload captured into slot registers on launch',
                                     'DMA.CONFIG updates scalar base state; LOAD/STORE launch DMA',
                                     'VMEM word-to-line slice, transfer-size slice, banks and slot geometry'],
                         local_function=['Selected-channel DMA.WAIT busy guard',
                                         'Slot completion requires dispatched requests and zero outstanding responses',
                                         'Per-bank LSU requests take priority over DMA'],
                         compiler_admission=['At most 4096 bytes per transfer; positive whole aligned lines, explicit waits and no live channel/slot reuse',
                                             'Known address/size values are checked; unknown values conservatively alias and require legal runtime inputs'],
                         inherited=['All non-DMA timing profiles and numeric scheduler cost estimates'],
                         unproved=['Universal DMA liveness or latency bound', 'Full queue/channel recurrence and TileLink source matching',
                                   'Raw instruction decoder correctness and end-to-end scalar register values']),
                     limitations=[
                         'The 4096-byte supported maximum is an imposed compiler admission limit, not a fact derived from the 13-bit transfer-size slice.',
                         'Explicit waits guard completion; estimated DMA duration may guide priority but cannot authorize dependent access.',
                         'Local Boolean cutpoint checks are not FSM reachability or an unbounded temporal proof.',
                         'Each newly emitted schedule still requires independent functional RTL replay.'])
    path = output / 'profile.json'
    path.write_text(json.dumps(canonical, indent=2) + '\n')
    projection = dict(schema='atlas-dma-profile-v1', config='EE290SimConfig',
                      source_ir_sha256=report['inputs']['hardware_ir']['sha256'],
                      evidence_sha256=artifact(path)['sha256'], **overrides)
    projected = output / 'atlas-dma.profile'
    projected.write_text(''.join(f'{key}={value}\n' for key, value in projection.items()))
    return dict(status=canonical['status'], profile=str(path), compiler_projection=str(projected))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dma-evidence', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(prepare(args.dma_evidence.resolve(), args.output.resolve())))


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, KeyError) as error:
        raise SystemExit(f'rtlgraph_dma_profile: {error}')

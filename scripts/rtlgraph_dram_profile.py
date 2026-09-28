#!/usr/bin/env python3
"""Extend the native DMA profile with checked physical DRAM address ranges."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from rtlgraph_dma_profile import checked_facts, settings
from rtlgraph_dram import checked_address_facts
from rtlgraph_mxu1_capture import verify_artifact
from rtlgraph_mxu1_profile import require
from rtlgraph_s0 import artifact


def address_settings(facts):
    require(facts['status'] == 'typed_local_dram_address_functions_checked', 'Unchecked DRAM facts')
    require(facts['geometry'] == dict(base_bits=32, low_address_bits=32, command_address_bits=64,
                                    tilelink_address_bits=37, alignment_bytes=32, beat_count_bits=9),
            'Unsupported DRAM geometry')
    return dict(dram_address='base32-concat-low32', dram_address_bits=37,
                dram_alignment_bytes=32, dram_base_reset=0, dram_wrap='conservative-alias')


def prepare(evidence_path, output):
    report = json.loads(evidence_path.read_text())
    for item in report['inputs'].values(): verify_artifact(item)
    adapter_path = verify_artifact(report['adapter_typed'])
    dma_path = verify_artifact(report['inputs']['dma_evidence'])
    dma = json.loads(dma_path.read_text())
    for item in dma['inputs'].values(): verify_artifact(item)
    typed_path = verify_artifact(dma['typed'])
    require(report['inputs']['typed']['sha256'] == dma['typed']['sha256'] and
            report['inputs']['hardware_ir']['sha256'] == dma['inputs']['hardware_ir']['sha256'],
            'DRAM and DMA evidence lineage differ')
    s0 = json.loads(verify_artifact(report['inputs']['s0']).read_text())
    require(s0['status'] == 'complete' and s0['config'] == 'EE290SimConfig' and
            s0['artifacts']['hardware_ir']['sha256'] == report['inputs']['hardware_ir']['sha256'],
            'DRAM evidence and elaboration lineage differ')
    typed = json.loads(typed_path.read_text())
    dma_facts = checked_facts(dma, typed)
    facts = checked_address_facts(report, typed, json.loads(adapter_path.read_text()))
    overrides = settings(dma_facts) | address_settings(facts)
    output.mkdir(parents=True, exist_ok=False)
    inputs = dict(evidence=artifact(evidence_path), dma_evidence=artifact(dma_path),
                  typed=artifact(typed_path), adapter_typed=artifact(adapter_path),
                  hardware_ir=report['inputs']['hardware_ir'], driver=artifact(Path(__file__).resolve()))
    for name in ('rtlgraph_dram.py', 'rtlgraph_dma_profile.py'):
        inputs[name] = artifact(Path(__file__).with_name(name).resolve())
    canonical = dict(schema='atlas.rtlgraph.dma-profile.v2', config='EE290SimConfig',
                     status='typed_local_facts_rechecked', inputs=inputs, compiler_overrides=overrides,
                     scope='Extends the existing native DMA model with configured external-memory ranges; all non-DMA timing remains inherited.',
                     evidence_categories=dict(
                         structural=['Existing DMA issue capture, CONFIG update, geometry and arbitration facts',
                                     'Command address concatenates base32 with LOAD rs1 or STORE rd low32',
                                     'Scalar rs2 supplies the command byte count',
                                     'Captured command plus beatCount*32 forms the 64-bit request address',
                                     'Adapter clears low five bits and truncates address to 37 bits',
                                     'TileLink size=5 denotes 32-byte transactions through AtlasCore output',
                                     'Requests require an active undispatched selected slot; adapter and TileLink request acceptance agree',
                                     'Request count reset, increment, last-beat test and synchronized slot advance'],
                         conditional_invariant=['Admitted stable commands emit only beats 0..size/32-1, starting at count zero'],
                         compiler_admission=['Positive aligned transfers up to 4096 bytes; explicit waits and no live slot or channel reuse',
                                             'Unknown base/low address/size or ranges wrapping the 37-bit boundary conservatively alias',
                                             'Known range disjointness uses the masked physical bus address, including low32 carry'],
                         inherited=['Non-DMA timing and numerical scheduling cost estimates',
                                    'Legal external-memory region, raw opcode decoding, scalar register semantics and complete queue/response behavior'],
                         unproved=['Universal DMA liveness, latency bound or whole-DMA correctness']),
                     limitations=['The new local recurrences are not an unbounded whole-DMA proof; they assume reset and no live command overwrite.',
                                  'Address truncation can identify aliases but does not establish that an external address maps to legal DRAM.',
                                  'CONFIG base state is captured in each command; later CONFIG or scalar updates do not change that command.',
                                  'No missing DMA.WAIT is inserted. Completion is an explicit event, never an estimated delay.',
                                  'Each emitted schedule still requires independent functional RTL replay.'])
    path = output / 'profile.json'
    path.write_text(json.dumps(canonical, indent=2) + '\n')
    projection = dict(schema='atlas-dma-profile-v2', config='EE290SimConfig',
                      source_ir_sha256=report['inputs']['hardware_ir']['sha256'],
                      evidence_sha256=artifact(path)['sha256'], **overrides)
    projected = output / 'atlas-dma.profile'
    projected.write_text(''.join(f'{key}={value}\n' for key, value in projection.items()))
    return dict(status=canonical['status'], profile=str(path), compiler_projection=str(projected))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dram-evidence', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(prepare(args.dram_evidence.resolve(), args.output.resolve())))


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, KeyError) as error:
        raise SystemExit(f'rtlgraph_dram_profile: {error}')

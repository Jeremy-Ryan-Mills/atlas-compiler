#!/usr/bin/env python3
"""Bind a straight-line Atlas replay to observed cycle and completion markers.

DBG0 follows the program's final DMA.WAIT. ECALL inhibits scalar s1_fire, so
this capture establishes the DBG0 event and eventual host ECALL status, not
the clock edge on which ECALL halts execution.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import struct

from rtlgraph_kernel import assemble, load_assembler
from rtlgraph_mxu1_capture import verify_artifact
from rtlgraph_mxu1_vcd import edge_samples, read_header
from rtlgraph_perf import fixture_info, performance_result
from rtlgraph_s0 import artifact, checked_path


FIELDS = ('clock', 'reset', 'scalar.fire', 'scalar.pc', 'scalar.instr',
          'csr.valid', 'csr.addr', 'csr.cmd', 'csr.wdata', 'csr.rdata')


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def analyze_samples(samples, words: list[int], dma_wait_words: set[int]) -> dict:
    issues, csrs = [], []
    for cycle, _time, sample in samples:
        if sample['reset'] != 0:
            require(not issues, 'reset or unknown reset after program execution began')
            continue
        fire, valid = sample['scalar.fire'], sample['csr.valid']
        require(fire in (0, 1) and valid in (0, 1), 'unknown scalar/CSR validity')
        if fire:
            pc, word = sample['scalar.pc'], sample['scalar.instr']
            require(type(pc) is int and pc == len(issues), 'execution must start at PC0 and follow consecutive instruction words')
            require(pc < len(words) and word == words[pc], f'executed instruction disagrees with assembly at PC{pc}')
            issues.append({'cycle': cycle, 'pc': pc, 'instruction': word})
        csr_word = bool(fire and word & 0x7f == 0x73 and (word >> 12) & 7 in (1, 2, 3, 5, 6, 7))
        require(bool(valid) == csr_word, 'CSR event is not matched to an executed CSR instruction')
        if valid:
            values = {name: sample['csr.' + name] for name in ('addr', 'cmd', 'wdata', 'rdata')}
            require(all(type(value) is int and value >= 0 for value in values.values()), 'unknown CSR event value')
            require(values['addr'] == word >> 20, 'CSR address disagrees with instruction')
            csrs.append({**issues[-1], **values})
    require(bool(issues), 'no executed instructions')
    reads = [event for event in csrs if event['addr'] == 0xc00]
    dbg1 = [event for event in csrs if event['addr'] == 0xc11]
    dbg0 = [event for event in csrs if event['addr'] == 0xc10]
    require(len(reads) == 2, 'expected exactly two cycle CSR reads')
    require(all(event['cmd'] == 2 and event['wdata'] == 0 for event in reads), 'cycle markers must be CSRRS with zero write data')
    require(len(dbg1) == 1 and dbg1[0]['cmd'] == 1, 'expected exactly one DBG1 write')
    require(len(dbg0) == 1 and dbg0[0]['cmd'] == 1 and dbg0[0]['wdata'] == 1, 'expected exactly one successful DBG0 write')
    start, end, result, done = reads[0], reads[1], dbg1[0], dbg0[0]
    require(start['cycle'] < end['cycle'] < result['cycle'] < done['cycle'], 'markers are out of order')
    delta = (end['rdata'] - start['rdata']) & 0xffffffff
    require(delta > 0 and result['wdata'] == delta, 'DBG1 does not match the nonzero cycle CSR difference')
    require(issues[-1]['pc'] == done['pc'], 'instructions execute after the successful completion marker')
    require(done['pc'] + 1 < len(words) and words[done['pc'] + 1] == 0x00000073,
            'successful completion marker must be immediately followed by ECALL in assembly')
    waits = [event for event in issues if event['instruction'] in dma_wait_words and event['cycle'] > end['cycle']]
    require(bool(waits) and waits[-1]['cycle'] < done['cycle'], 'no final DMA.WAIT before completion')
    window = [event for event in issues if start['pc'] < event['pc'] < end['pc']]
    return {'status': 'observed_completion_markers_passed', 'executed_instruction_count': len(issues),
            'execution_binding': 'All fired instruction words and consecutive PCs match the supplied assembler output through DBG0',
            'first_instruction': issues[0], 'cycle_start': start, 'cycle_end': end,
            'dbg1_write': result, 'dbg0_write': done, 'last_dma_wait': waits[-1],
            'csr_window_instructions': window,
            'metrics': {'csr_counter_delta': delta, 'csr_window_clock_edges': end['cycle'] - start['cycle'],
                        'first_issue_to_dbg0_edges': done['cycle'] - issues[0]['cycle'],
                        'csr_start_to_dbg0_edges': done['cycle'] - start['cycle'],
                        'csr_end_to_dbg0_edges': done['cycle'] - end['cycle'],
                        'last_dma_wait_to_dbg0_edges': done['cycle'] - waits[-1]['cycle']},
            'ecall_cycle': None,
            'limitations': ['ECALL suppresses s1_fire and is not separately captured; only the host log establishes eventual halted/ECALL status.',
                            'DBG0 timing includes actual suffix/DMA waits; one observed memory-environment execution is not a universal latency bound.',
                            'This checks scalar execution and completion markers, not VPU row-level timing or arithmetic equivalence.']}


def summarize(manifest_path: Path) -> dict:
    manifest_path = checked_path(manifest_path)
    manifest = json.loads(manifest_path.read_text())
    require(manifest.get('kind') == 'rtlgraph-perf-replay' and manifest.get('config') == 'EE290SimConfig', 'unsupported replay manifest')
    require(manifest.get('status') == 'passed' and bool(manifest.get('finished_utc')), 'replay must have completed successfully')
    require(manifest.get('trace_validation', {}).get('status') == 'PARSED', 'replay requires a completely parsed capture')
    assembly = verify_artifact(manifest['inputs']['assembly'])
    assembler_path = verify_artifact(manifest['inputs']['assembler'])
    golden = verify_artifact(manifest['inputs']['golden_fixture'])
    verify_artifact(manifest['binary'])
    trace = verify_artifact(manifest['trace'])
    simulations = [command for command in manifest['commands'] if command['argv'] == manifest['prepared_command']['argv']]
    require(len(simulations) == 1, 'expected one recorded prepared simulator command')
    simulation = simulations[0]
    log = verify_artifact(simulation['log']).read_text(errors='replace')
    assembler = load_assembler(assembler_path)
    words = assemble(assembler, assembly.read_text())
    from rtlgraph_replay_control import audit_control
    control = audit_control(manifest, words)
    host_name = control['host_name'] if control else assembly.stem
    functional = performance_result(simulation, log, host_name, fixture_info(golden)['expected_check_words'])
    require(functional['status'] == 'PASS' and functional == manifest['result'], 'functional result does not match the recorded successful replay')
    waits = {assemble(assembler, f'DMA.WAIT {channel}')[0] for channel in range(8)}
    signals = {key: (manifest['signals'][key]['path'], manifest['signals'][key]['width']) for key in FIELDS}
    with trace.open() as stream:
        selected, timescale = read_header(stream, signals)
        report = analyze_samples(edge_samples(stream, selected, signals), words, waits)
    require(report['metrics']['csr_counter_delta'] == functional['metrics']['dbg1_cycles'], 'trace CSR delta disagrees with the host log')
    verify_artifact(manifest['trace'])
    report.update(schema='atlas.rtlgraph.completion.v1', driver=artifact(checked_path(__file__)),
                  replay_manifest=artifact(manifest_path), assembly=artifact(assembly),
                  assembler=artifact(assembler_path), golden_fixture=artifact(golden),
                  trace=manifest['trace'], timescale=timescale, functional_result=functional,
                  assembled_word_count=len(words),
                  assembled_words_sha256=hashlib.sha256(b''.join(struct.pack('<I', word) for word in words)).hexdigest())
    if control:
        report['replay_control'] = control
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)
    output = checked_path(args.output)
    require(not output.exists(), 'output must be a new file')
    report = summarize(args.manifest)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps({'status': report['status'], 'metrics': report['metrics'], 'output': str(output)}, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

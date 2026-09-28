#!/usr/bin/env python3
"""Compare explicit-wait DMA schedules through independently checked completion.

DMA work may cross the old CSR markers, so their deltas are observations only.
This comparison requires one audited fixed host and matching executed transfers.
"""
from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path

from rtlgraph_compare import require, runtime_options
from rtlgraph_kernel import instruction_words, load_assembler
from rtlgraph_mxu1_capture import verify_artifact, verify_runtime
from rtlgraph_s0 import artifact, timestamp


def observation(path):
    from rtlgraph_dma_trace import summarize
    return summarize(path)


def transfer_signature(report):
    return Counter((command['op'], command['channel'], command['line'],
                    command['dram'], command['size']) for command in report['commands'])


def compare(original, memory_baseline, candidates, candidate_manifest):
    static = json.loads(candidate_manifest.read_text())
    require(static.get('schema') == 'atlas.rtlgraph.dma-schedule-experiment.v1'
            and static.get('status') == 'candidates_ready', 'Invalid DMA candidate manifest')
    require(candidates and set(candidates) <= set(static['cases']), 'Unknown or missing DMA candidate')
    require(not set(candidates) & {'original', 'memory_baseline'}, 'Reserved candidate name')
    results, baseline = {}, None
    for name, path in [('original', original), ('memory_baseline', memory_baseline), *candidates.items()]:
        run = json.loads(path.read_text())
        report = observation(path)  # Recheck command ownership, waits, row accesses, and goldens.
        completion = report['completion']
        expected = (static['inputs']['source'] if name == 'original' else
                    static['inputs']['memory_baseline'] if name == 'memory_baseline' else
                    static['cases'][name]['assembly'])
        verify_artifact(expected)
        source = verify_artifact(completion['assembly'])
        require(completion['assembly']['sha256'] == expected['sha256'], 'Replayed source differs from prepared experiment')
        assembler_path = verify_artifact(completion['assembler'])
        verify_artifact(static['inputs']['assembler'])
        require(completion['assembler']['sha256'] == static['inputs']['assembler']['sha256'], 'Assembler differs')
        control = completion.get('replay_control')
        require(bool(control), 'DMA comparison requires an audited fixed-host control')
        simulator = verify_artifact(run['simulator'])
        verify_runtime(simulator.parent, run['runtime_files'])
        for library in run['runtime_libraries'].values():
            verify_artifact(library)
        signature = {
            'operations': Counter(instruction_words(load_assembler(assembler_path), source.read_text(), include_idle=False)),
            'transfers': transfer_signature(report),
            'control': control,
            'runtime': (run['simulator']['sha256'],
                        sorted((entry['relative_path'], entry['sha256']) for entry in run['runtime_files']),
                        sorted((key, entry['sha256']) for key, entry in run['runtime_libraries'].items()),
                        runtime_options(run)),
            'golden': completion['golden_fixture']['sha256'],
            'first_issue': completion['first_instruction']['cycle'],
        }
        if baseline is None:
            baseline = signature
        for key, value in signature.items():
            require(value == baseline[key], 'Comparison mismatch: ' + key)
        report_path = path.parent / 'dma-events-comparison.json'
        if report_path.exists():
            old = json.loads(report_path.read_text())
            require(old == report, 'Existing DMA observation differs; use a fresh report location')
        else:
            report_path.write_text(json.dumps(report, indent=2) + '\n')
        results[name] = {'manifest': artifact(path), 'observation': artifact(report_path),
                         'assembly': completion['assembly'], 'metrics': completion['metrics'],
                         'first_issue_cycle': signature['first_issue'],
                         'checked_words': completion['functional_result']['checked_words'],
                         'dma_commands': len(report['commands'])}
    comparisons = {}
    for name in ['memory_baseline', *candidates]:
        refs = ['original'] if name == 'memory_baseline' else ['original', 'memory_baseline']
        comparisons[name] = {}
        for reference in refs:
            before = results[reference]['metrics']['first_issue_to_dbg0_edges']
            after = results[name]['metrics']['first_issue_to_dbg0_edges']
            comparisons[name][reference] = {'first_issue_to_dbg0_edges': {
                'before': before, 'after': after, 'edges_saved': before - after,
                'reduction_percent': 100 * (before - after) / before}}
    return {'schema': 'atlas.rtlgraph.dma-comparison.v1', 'created_utc': timestamp(),
            'driver': artifact(Path(__file__).resolve()), 'candidate_manifest': artifact(candidate_manifest),
            'cases': results, 'comparisons': comparisons, 'fixed_host_control': baseline['control'],
            'identical_global_non_idle_words': True, 'identical_observed_dma_transfers': True,
            'identical_runtime_golden_and_launch_edge': True,
            'scope': {'completion': 'First Atlas issue through successful DBG0 after checked DMA completion; excludes host setup and golden comparison.',
                      'csr': 'Recorded for diagnosis only. Moving DMA commands/waits across the markers changes the work in the interval; no CSR speedup is reported.',
                      'correctness': 'Full fixture goldens plus finite DMA/LSU/VPU trace checks, including explicit waits and actual memory requests/responses.'},
            'limitations': ['One execution per variant and launch condition; no universal memory-latency or performance bound.',
                            'Instruction-word and transfer multisets do not alone prove semantic equivalence.',
                            'Local typed CIRCT facts and finite traces do not constitute universal scheduling-safety proof.',
                            'Cached simulator source-to-binary build linkage remains unverified.']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('original', 'memory-baseline', 'candidate-manifest', 'output'):
        parser.add_argument('--' + name, required=True, type=Path)
    parser.add_argument('--candidate', action='append', default=[], metavar='NAME=MANIFEST')
    args = parser.parse_args()
    require(not args.output.exists(), 'Output must be new')
    candidates = {}
    for value in args.candidate:
        name, sep, path = value.partition('=')
        require(sep and name not in candidates, 'Invalid or duplicate candidate')
        candidates[name] = Path(path).resolve()
    report = compare(args.original.resolve(), args.memory_baseline.resolve(), candidates,
                     args.candidate_manifest.resolve())
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report['comparisons'], indent=2))


if __name__ == '__main__':
    main()

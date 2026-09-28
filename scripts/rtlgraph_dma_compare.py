#!/usr/bin/env python3
"""Compare explicit-wait DMA schedules through independently checked completion.

DMA work may cross the old CSR markers, so their deltas are observations only.
This comparison requires one audited fixed host and matching executed transfers.
"""
from __future__ import annotations

import argparse
from collections import Counter
import json
import os
from pathlib import Path
import subprocess

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


def audit_native(static, selected):
    """Bind native schedules and their exact final checks to source and model."""
    from rtlgraph_dma_compile import extract_markers, insert_markers, translate
    for name in ('source', 'assembler', 'compiler', 'profile', 'driver'):
        verify_artifact(static['inputs'][name])
    compiler = verify_artifact(static['inputs']['compiler'])
    profile = verify_artifact(static['inputs']['profile'])
    pairs = [line.split('=', 1) for line in profile.read_text().splitlines() if line]
    require(all(len(pair) == 2 for pair in pairs) and len(dict(pairs)) == len(pairs), 'Malformed native DMA profile')
    settings = dict(pairs)
    canonical_path = profile.parent / 'profile.json'
    canonical = json.loads(canonical_path.read_text())
    schemas = {'atlas-dma-profile-v1': 'atlas.rtlgraph.dma-profile.v1',
               'atlas-dma-profile-v2': 'atlas.rtlgraph.dma-profile.v2'}
    require(settings.get('schema') in schemas and
            canonical.get('schema') == schemas[settings['schema']] and
            settings.get('config') == canonical.get('config') == 'EE290SimConfig' and
            settings.get('evidence_sha256') == artifact(canonical_path)['sha256'], 'Native DMA profile evidence differs')
    require(settings == dict(schema=settings['schema'], config='EE290SimConfig',
                             source_ir_sha256=canonical['inputs']['hardware_ir']['sha256'],
                             evidence_sha256=artifact(canonical_path)['sha256'],
                             **{key: str(value) for key, value in canonical['compiler_overrides'].items()}),
            'Native DMA projection differs from canonical model')
    for record in canonical['inputs'].values():
        verify_artifact(record)
    native_input = verify_artifact(static['native_input'])
    source = verify_artifact(static['inputs']['source']).read_text()
    markers = None
    if static['instrumentation']['relocated']:
        source, markers = extract_markers(source)
        require(static['instrumentation']['private_registers'] == list(markers.private_registers),
                'Native benchmark register proof differs')
    require(native_input.read_text() == translate(source, to_compiler=True), 'Native compiler input differs from source')
    for name in selected:
        case = static['cases'][name]
        for field in ('assembly', 'scheduled', 'log', 'final_check_input', 'final_check_log'):
            verify_artifact(case[field])
        require(case.get('scheduler_returncode') == case.get('final_check_returncode') == 0,
                'Native compiler/check did not succeed')
        command = case['command']
        require(name in ('native_critical', 'native_input'), 'Unknown native schedule policy')
        expected = [str(compiler), str(native_input), '--passes', 'strip-artifacts,schedule',
                    '--schedule-priority', name.removeprefix('native_'), '--rtl-dma-profile', str(profile),
                    '-o', case['scheduled']['path']]
        require(command == expected, 'Native scheduler command differs from recorded inputs/model')
        checked = verify_artifact(case['final_check_input'])
        emitted = translate(verify_artifact(case['scheduled']).read_text(), to_compiler=False)
        if markers:
            emitted = insert_markers(emitted, markers)
        require(translate(emitted, to_compiler=True) ==
                translate(verify_artifact(case['assembly']).read_text(), to_compiler=True),
                'Replayed candidate differs from native compiler output and marker policy')
        require(checked.read_text() == translate(verify_artifact(case['assembly']).read_text(), to_compiler=True),
                'Final native check did not cover the replayed assembly')
        check_command = [str(compiler), '--check', str(checked), '--rtl-dma-profile', str(profile)]
        require(case['final_check_command'] == check_command, 'Native check command differs from model/input')
        check = subprocess.run(check_command, capture_output=True, text=True, env=os.environ.copy())
        require(check.returncode == 0, 'Exact final native schedule no longer passes its model: ' + check.stdout + check.stderr)
    return dict(profile=static['inputs']['profile'], canonical=artifact(canonical_path),
                compiler=static['inputs']['compiler'], exact_final_native_checks_repeated=True)


def compare(original, memory_baseline, candidates, candidate_manifest):
    static = json.loads(candidate_manifest.read_text())
    native = static.get('schema') == 'atlas.rtlgraph.dma-native-schedule.v1'
    require((native and static.get('status') == 'native_model_candidates_ready') or
            (static.get('schema') == 'atlas.rtlgraph.dma-schedule-experiment.v1'
             and static.get('status') == 'candidates_ready'), 'Invalid DMA candidate manifest')
    require(candidates and set(candidates) <= set(static['cases']), 'Unknown or missing DMA candidate')
    require(not set(candidates) & {'original', 'memory_baseline'}, 'Reserved candidate name')
    require(native or memory_baseline is not None, 'Legacy DMA experiments require a memory baseline')
    native_audit = audit_native(static, candidates) if native else None
    results, baseline = {}, None
    references = [('original', original)]
    if memory_baseline is not None:
        references.append(('memory_baseline', memory_baseline))
    for name, path in [*references, *candidates.items()]:
        run = json.loads(path.read_text())
        report = observation(path)  # Recheck command ownership, waits, row accesses, and goldens.
        completion = report['completion']
        expected = (static['inputs']['source'] if name == 'original' else
                    (completion['assembly'] if native else static['inputs']['memory_baseline']) if name == 'memory_baseline' else
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
    for name in [*(name for name, _ in references if name != 'original'), *candidates]:
        refs = ['original'] if name == 'memory_baseline' else [key for key, _ in references]
        comparisons[name] = {}
        for reference in refs:
            before = results[reference]['metrics']['first_issue_to_dbg0_edges']
            after = results[name]['metrics']['first_issue_to_dbg0_edges']
            comparisons[name][reference] = {'first_issue_to_dbg0_edges': {
                'before': before, 'after': after, 'edges_saved': before - after,
                'reduction_percent': 100 * (before - after) / before}}
    result = {'schema': 'atlas.rtlgraph.dma-comparison.v1', 'created_utc': timestamp(),
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
    if native_audit:
        result['native_compiler_audit'] = native_audit
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('original', 'candidate-manifest', 'output'):
        parser.add_argument('--' + name, required=True, type=Path)
    parser.add_argument('--memory-baseline', type=Path,
                        help='Optional additional baseline for native schedules; required for the legacy experiment')
    parser.add_argument('--candidate', action='append', default=[], metavar='NAME=MANIFEST')
    args = parser.parse_args()
    require(not args.output.exists(), 'Output must be new')
    candidates = {}
    for value in args.candidate:
        name, sep, path = value.partition('=')
        require(sep and name not in candidates, 'Invalid or duplicate candidate')
        candidates[name] = Path(path).resolve()
    report = compare(args.original.resolve(), args.memory_baseline.resolve() if args.memory_baseline else None, candidates,
                     args.candidate_manifest.resolve())
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report['comparisons'], indent=2))


if __name__ == '__main__':
    main()

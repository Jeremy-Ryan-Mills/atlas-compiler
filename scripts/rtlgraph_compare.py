#!/usr/bin/env python3
"""Compare fully captured golden-checked replays with unchanged setup/writeback."""
from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path

from rtlgraph_completion import summarize
from rtlgraph_kernel import assemble, instruction_words, load_assembler
from rtlgraph_mxu1_capture import verify_artifact
from rtlgraph_s0 import artifact, timestamp
from rtlgraph_schedule import split


def require(condition, message):
    if not condition:
        raise ValueError(message)


def runtime_options(run):
    """Retain simulator settings; normalize only per-run artifact locations."""
    args = run['prepared_command']['argv']
    require(args[0] == run['simulator']['path'], 'simulator command identity mismatch')
    binary = run['binary']['path']
    result, index = ['<simulator>'], 1
    while index < len(args):
        arg = args[index]
        if arg in ('-cm_dir', '-cm_name', '-i'):
            require(index + 1 < len(args), 'missing simulator option value')
            if arg == '-i':
                require(args[index + 1] == run['capture_tcl']['path'], 'capture script identity mismatch')
                verify_artifact(run['capture_tcl'])
            result.extend((arg, '<per-run artifact>'))
            index += 2
            continue
        if arg.startswith('+dramsim_ini_dir='):
            require(arg == '+dramsim_ini_dir=' + str(Path(run['simulator']['path']).parent / 'dramsim2_ini'),
                    'DRAM settings directory differs from the recorded runtime')
            arg = '+dramsim_ini_dir=<runtime files compared by hash>'
        elif arg.startswith('+loadmem='):
            require(arg == '+loadmem=' + binary, 'loaded binary differs from recorded binary')
            arg = '+loadmem=<recorded binary>'
        elif arg == binary:
            arg = '<recorded binary>'
        result.append(arg)
        index += 1
    return result


def compare(original, candidates, candidate_manifest):
    static = json.loads(candidate_manifest.read_text())
    require(static['schema'] == 'atlas.rtlgraph.schedule-experiment.v1' and static['status'] == 'model_candidates_ready', 'invalid candidate manifest')
    require(set(candidates) <= set(static['cases']), 'unknown candidate name')
    results, baseline_parts, baseline_runtime, baseline_golden = {}, None, None, None
    for name, path in [('original', original), *candidates.items()]:
        run = json.loads(path.read_text())
        completion = summarize(path)  # Recheck full execution, goldens, and marker binding.
        source = verify_artifact(completion['assembly'])
        expected = static['inputs']['source'] if name == 'original' else static['cases'][name]['assembly']
        require(artifact(source)['sha256'] == expected['sha256'], 'replayed source differs from prepared experiment')
        assembler = load_assembler(verify_artifact(completion['assembler']))
        parts = split(source.read_text())
        encoded = (assemble(assembler, parts.prefix), assemble(assembler, parts.end_marker + parts.suffix),
                   Counter(instruction_words(assembler, parts.body, include_idle=False)))
        runtime = (run['simulator']['sha256'],
                   sorted((entry['relative_path'], entry['sha256']) for entry in run['runtime_files']),
                   sorted((key, entry['sha256']) for key, entry in run['runtime_libraries'].items()),
                   runtime_options(run))
        golden = completion['golden_fixture']['sha256']
        if name == 'original':
            baseline_parts, baseline_runtime, baseline_golden = encoded, runtime, golden
        require(encoded == baseline_parts, 'setup, suffix, or non-idle operations changed')
        require(runtime == baseline_runtime, 'simulator runtime differs between replays')
        require(golden == baseline_golden, 'golden fixtures differ between replays')
        completion_path = path.parent / 'completion.json'
        if completion_path.exists():
            # Historical reports retain their own driver identity.
            old = json.loads(completion_path.read_text())
            require({key: value for key, value in old.items() if key != 'driver'} ==
                    {key: value for key, value in completion.items() if key != 'driver'},
                    'existing completion report differs')
        else:
            completion_path.write_text(json.dumps(completion, indent=2) + '\n')
        results[name] = {'manifest': artifact(path), 'completion': artifact(completion_path),
                         'assembly': completion['assembly'], 'metrics': completion['metrics'],
                         'checked_words': completion['functional_result']['checked_words']}
    comparisons = {}
    for name in candidates:
        refs = ['original']
        if name.startswith('profile_') and ('builtin_' + name[len('profile_'):]) in results:
            refs.append('builtin_' + name[len('profile_'):])
        comparisons[name] = {}
        for reference in refs:
            comparisons[name][reference] = {}
            for metric in ('csr_counter_delta', 'first_issue_to_dbg0_edges', 'csr_start_to_dbg0_edges'):
                before, after = results[reference]['metrics'][metric], results[name]['metrics'][metric]
                comparisons[name][reference][metric] = {'before': before, 'after': after, 'cycles_saved': before-after,
                                                         'reduction_percent': 100*(before-after)/before}
    return {'schema': 'atlas.rtlgraph.corpus-comparison.v1', 'created_utc': timestamp(),
            'driver': artifact(Path(__file__).resolve()), 'candidate_manifest': artifact(candidate_manifest),
            'cases': results, 'comparisons': comparisons,
            'identical_setup_suffix_and_non_idle_operations': True, 'identical_runtime_and_golden': True,
            'scope': {'csr': 'Original counter locations, with candidate modeled completion drain before ending counter. Original matmul windows can end at final-pop issue, before completion.',
                      'completion': 'First instruction or first counter through DBG0 following final DMA.WAIT; ECALL edge is not captured.',
                      'profile_attribution': 'Compare same-priority built-in/profile CSR windows; differing priority is a separate scheduling choice.'},
            'limitations': ['Single execution per variant; whole-kernel differences include memory-environment timing.',
                            'Finite golden and scalar-trace checks do not prove temporal safety for all inputs/states.',
                            'VPU row timing and other unprojected rules remain inherited; static VPU guards can separately corroborate resource constraints.',
                            'Cached simulator source-to-binary build lineage remains unverified.']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('original', 'candidate-manifest', 'output'):
        parser.add_argument('--' + name, required=True, type=Path)
    parser.add_argument('--candidate', action='append', default=[], metavar='NAME=MANIFEST')
    args = parser.parse_args()
    require(not args.output.exists(), 'output must be new')
    candidates = {}
    for value in args.candidate:
        name, sep, path = value.partition('=')
        require(sep and name not in candidates and name != 'original', 'invalid or duplicate candidate assignment')
        candidates[name] = Path(path).resolve()
    require(bool(candidates), 'at least one candidate required')
    report = compare(args.original.resolve(), candidates, args.candidate_manifest.resolve())
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report['comparisons'], indent=2))


if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError, KeyError) as error:
        raise SystemExit(f'rtlgraph_compare: {error}')

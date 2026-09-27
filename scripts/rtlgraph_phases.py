#!/usr/bin/env python3
"""Attribute scalar issue-gap differences outside an Atlas performance window.

Requires successful golden-backed, straight-line replays accepted by the
completion monitor. The prefix and suffix must contain identical encoded
instructions; the timed window may change. A gap ending at DMA.WAIT localizes
the delay to that issue interval, without proving the underlying bus cause.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from rtlgraph_completion import require, summarize
from rtlgraph_kernel import assemble, load_assembler
from rtlgraph_mxu1_capture import verify_artifact
from rtlgraph_mxu1_vcd import edge_samples, read_header
from rtlgraph_s0 import artifact, checked_path


def compare_issues(baseline: list[dict], candidate: list[dict],
                   baseline_markers: dict, candidate_markers: dict,
                   wait_channels: dict[int, int]) -> dict:
    """Compare inclusive phase endpoints; sum the intervening edge gaps."""
    for issues, markers in ((baseline, baseline_markers), (candidate, candidate_markers)):
        require(bool(issues), 'empty issue stream')
        require(all(event['pc'] == index for index, event in enumerate(issues)),
                'issues must have consecutive PCs from zero')
        require(all(a['cycle'] < b['cycle'] for a, b in zip(issues, issues[1:])),
                'issue cycles must increase strictly')
        require(0 <= markers['start'] < markers['end'] < markers['done'] == len(issues) - 1,
                'invalid performance/completion marker PCs')

    phases = {}
    for name, start, end in (('setup', None, 'start'), ('writeback', 'end', 'done')):
        a_start = baseline_markers[start] if start else 0
        b_start = candidate_markers[start] if start else 0
        a = baseline[a_start:baseline_markers[end] + 1]
        b = candidate[b_start:candidate_markers[end] + 1]
        require([event['instruction'] for event in a] == [event['instruction'] for event in b],
                f'{name} encoded instructions differ')
        gaps = []
        for previous_a, current_a, previous_b, current_b in zip(a, a[1:], b, b[1:]):
            old_gap = current_a['cycle'] - previous_a['cycle']
            new_gap = current_b['cycle'] - previous_b['cycle']
            word = current_a['instruction']
            gaps.append({'baseline_pc': current_a['pc'], 'candidate_pc': current_b['pc'],
                         'instruction': word, 'dma_wait_channel': wait_channels.get(word),
                         'baseline_edges': old_gap, 'candidate_edges': new_gap,
                         'delta_edges': new_gap - old_gap})
        old_edges = a[-1]['cycle'] - a[0]['cycle']
        new_edges = b[-1]['cycle'] - b[0]['cycle']
        phases[name] = {'baseline_edges': old_edges, 'candidate_edges': new_edges,
                        'delta_edges': new_edges - old_edges, 'intervals': gaps,
                        'changed_intervals': [gap for gap in gaps if gap['delta_edges']],
                        'dma_wait_delta_edges': sum(gap['delta_edges'] for gap in gaps
                                                    if gap['dma_wait_channel'] is not None),
                        'other_delta_edges': sum(gap['delta_edges'] for gap in gaps
                                                 if gap['dma_wait_channel'] is None)}
    old_window = baseline[baseline_markers['end']]['cycle'] - baseline[baseline_markers['start']]['cycle']
    new_window = candidate[candidate_markers['end']]['cycle'] - candidate[candidate_markers['start']]['cycle']
    old_total = baseline[-1]['cycle'] - baseline[0]['cycle']
    new_total = candidate[-1]['cycle'] - candidate[0]['cycle']
    phases['timed_window'] = {'baseline_edges': old_window, 'candidate_edges': new_window,
                             'delta_edges': new_window - old_window}
    phases['first_issue_to_dbg0'] = {'baseline_edges': old_total, 'candidate_edges': new_total,
                                    'delta_edges': new_total - old_total}
    require(sum(phases[name]['delta_edges'] for name in ('setup', 'timed_window', 'writeback'))
            == new_total - old_total, 'phase deltas do not add up')
    return phases


def load_issues(manifest_path: Path) -> tuple[list[dict], dict, dict]:
    report = summarize(manifest_path)
    manifest = json.loads(manifest_path.read_text())
    signals = {key: (manifest['signals'][key]['path'], manifest['signals'][key]['width'])
               for key in ('clock', 'reset', 'scalar.fire', 'scalar.pc', 'scalar.instr')}
    trace = verify_artifact(manifest['trace'])
    issues = []
    with trace.open() as stream:
        selected, _ = read_header(stream, signals)
        for cycle, _, sample in edge_samples(stream, selected, signals):
            if sample['reset'] == 0 and sample['scalar.fire'] == 1:
                issues.append({'cycle': cycle, 'pc': sample['scalar.pc'],
                               'instruction': sample['scalar.instr']})
    verify_artifact(manifest['trace'])
    require(len(issues) == report['executed_instruction_count'], 'issue stream changed after validation')
    markers = {name: report[key]['pc'] for name, key in
               (('start', 'cycle_start'), ('end', 'cycle_end'), ('done', 'dbg0_write'))}
    return issues, markers, report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline', type=Path, required=True, help='Baseline replay manifest')
    parser.add_argument('--candidate', type=Path, required=True, help='Candidate replay manifest')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)
    output = checked_path(args.output)
    require(not output.exists(), 'output must be a new file')
    baseline_path, candidate_path = checked_path(args.baseline), checked_path(args.candidate)
    baseline, baseline_markers, baseline_report = load_issues(baseline_path)
    candidate, candidate_markers, candidate_report = load_issues(candidate_path)
    require(baseline_report['assembler']['sha256'] == candidate_report['assembler']['sha256'],
            'replays use different assembler versions')
    require(baseline_report['golden_fixture']['sha256'] == candidate_report['golden_fixture']['sha256'],
            'replays use different golden fixtures')
    assembler = load_assembler(verify_artifact(baseline_report['assembler']))
    waits = {assemble(assembler, f'DMA.WAIT {channel}')[0]: channel for channel in range(8)}
    phases = compare_issues(baseline, candidate, baseline_markers, candidate_markers, waits)
    result = {'schema': 'atlas.rtlgraph.scalar-phases.v1', 'status': 'matched_outer_phases',
              'driver': artifact(checked_path(__file__)),
              'baseline': {'manifest': artifact(baseline_path), 'trace': baseline_report['trace'],
                           'binary': json.loads(baseline_path.read_text())['binary'],
                           'first_issue_cycle': baseline[0]['cycle']},
              'candidate': {'manifest': artifact(candidate_path), 'trace': candidate_report['trace'],
                            'binary': json.loads(candidate_path.read_text())['binary'],
                            'first_issue_cycle': candidate[0]['cycle']},
              'phases': phases,
              'limitations': [
                  'A gap ending at DMA.WAIT includes the interval since the preceding issue; no DMA/bus events are captured by this analysis.',
                  'Matching instruction words and fixtures does not establish matching initial memory state, host ELF layout, or launch phase.',
                  'Waveform clock-edge counts are distinct from CSR counter differences.',
                  'These measurements characterize the recorded executions; they are not universal latency bounds.']}
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps({name: {key: phase[key] for key in ('baseline_edges', 'candidate_edges', 'delta_edges')}
                      for name, phase in phases.items()}, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

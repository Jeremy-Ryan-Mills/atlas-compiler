#!/usr/bin/env python3
"""Stage unchanged perf originals, dispatch isolated replays, and report coverage.

Each `run` occupies one simulator slot. The caller must coordinate a maximum
of three VCS jobs across baseline and candidate workers.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import struct
import sys

from rtlgraph_kernel import assemble, load_assembler
from rtlgraph_mxu1_capture import verify_artifact
from rtlgraph_perf import fixture_info, performance_result
from rtlgraph_s0 import artifact, checked_path, timestamp


def require(condition, message):
    if not condition:
        raise ValueError(message)


def encoded_digest(words):
    return hashlib.sha256(b''.join(struct.pack('<I', word) for word in words)).hexdigest()


def stage_text(source):
    return source if re.search(r'^\s*#\s*@PERF_REPORT\s*$', source, re.MULTILINE) else '# @PERF_REPORT\n' + source


def classify_source(source, fixture):
    marker_count = sum(bool(re.match(r'^\s*(?:CSRR|CSRRS)\b.*(?:0x[Cc]00|3072)\b', line.split('#', 1)[0]))
                       for line in source.splitlines())
    checks = len(fixture.get('dram_checks', [])) * 8 if fixture is not None else 0
    if checks:
        status = 'golden_checked_baseline'
    elif fixture is not None:
        status = 'no_output_golden'
    elif marker_count > 2:
        status = 'self_check_multiple_windows'
    else:
        status = 'missing_fixture'
    return {'readiness': status, 'cycle_read_count': marker_count, 'expected_check_words': checks,
            'has_control_flow': bool(re.search(r'^\s*(?:BNE|BEQ|BLT|BGE|JAL|JALR)\b', source, re.MULTILINE)),
            'utilization_threshold': re.findall(r'^\s*#\s*@PERF_UTIL_THRESHOLD\s+(\d+)\s*$', source, re.MULTILINE)}


def assignments(values):
    result = {}
    for value in values:
        name, separator, path = value.partition('=')
        require(separator and re.fullmatch(r'perf_[A-Za-z0-9_]+', name) and path, 'use NAME=MANIFEST assignments')
        require(name not in result, 'duplicate kernel assignment')
        result[name] = checked_path(path)
    return result


def verify_replay(case, path, assembler):
    replay = json.loads(path.read_text())
    require(replay.get('kind') == 'rtlgraph-perf-replay' and replay.get('config') == 'EE290SimConfig', 'wrong replay type/config')
    require(replay.get('status') == 'passed' and replay.get('finished_utc'), 'replay is incomplete or unsuccessful')
    require(replay.get('trace_validation', {}).get('status') == 'PARSED', 'capture is not completely parsed')
    assembly = verify_artifact(replay['inputs']['assembly'])
    require(encoded_digest(assemble(assembler, assembly.read_text())) == case['encoded_words_sha256'], 'replay changes original instruction words')
    require(replay['inputs']['golden_fixture']['sha256'] == case['golden_fixture']['sha256'], 'replay golden fixture differs')
    verify_artifact(replay['inputs']['golden_fixture'])
    verify_artifact(replay['binary'])
    verify_artifact(replay['trace'])
    commands = [command for command in replay['commands'] if command['argv'] == replay['prepared_command']['argv']]
    require(len(commands) == 1, 'expected one matching simulator command')
    log = verify_artifact(commands[0]['log']).read_text(errors='replace')
    result = performance_result(commands[0], log, assembly.stem, case['expected_check_words'])
    require(result.get('status') == 'PASS', 'recorded completion does not independently classify PASS')
    require(all(result.get(field) == replay['result'].get(field) for field in ('status', 'checked_words', 'metrics')), 'recorded measurements differ')
    return {'status': 'passed', 'manifest': artifact(path), 'metrics': result['metrics'],
            'checked_words': result['checked_words'], 'capture_status': 'PARSED',
            'instruction_words_match_original': True}


def prepare(baremetal, output, reused):
    require(not output.exists(), 'inventory output must be new')
    assembler_path = checked_path(baremetal / 'assembler.py')
    assembler = load_assembler(assembler_path)
    output.mkdir(parents=True)
    record = {'schema': 'atlas.rtlgraph.perf-corpus.v1', 'created_utc': timestamp(),
              'driver': artifact(checked_path(__file__)), 'assembler': artifact(assembler_path), 'cases': {},
              'scope': 'Comment-only local copies of existing perf originals; fixtures reused unchanged; no hardware-source writes',
              'parallelism': 'At most three VCS processes across all workers; run dispatches exactly one'}
    sources = sorted((baremetal / 'assembly').glob('perf_*.S'))
    require(bool(sources), 'no perf assembly sources found')
    for source in sources:
        source = checked_path(source)
        text = source.read_text()
        staged = output / source.name
        staged.write_text(stage_text(text))
        original_words = assemble(assembler, text)
        require(original_words == assemble(assembler, staged.read_text()), 'staging changed assembled words')
        golden = checked_path(baremetal / 'generators' / (source.stem + '.json'))
        fixture = json.loads(golden.read_text()) if golden.is_file() else None
        case = {**classify_source(text, fixture), 'source': artifact(source), 'assembly': artifact(staged),
                'report_directive_added': staged.read_text() != text,
                'instruction_word_count': len(original_words), 'encoded_words_sha256': encoded_digest(original_words),
                'encoded_words_unchanged': True}
        if fixture is not None:
            case['golden_fixture'] = artifact(golden)
            if case['expected_check_words']:
                case['fixture_info'] = fixture_info(golden)
        hints = re.findall(r'^\s*#\s*@PYTHON_GEN\s+(\S+)', text, re.MULTILINE)
        candidates = [checked_path(baremetal / 'generators' / hint) for hint in hints]
        candidates.append(checked_path(baremetal / 'generators' / ('gen_' + source.stem + '.py')))
        generators = list(dict.fromkeys(path for path in candidates if path.is_file()))
        case['declared_generators'] = hints
        case['generator_sources'] = [artifact(path) for path in generators]
        case['generator_rerun'] = False
        if source.stem in reused:
            require(case['readiness'] == 'golden_checked_baseline', 'cannot reuse special case as golden-backed PASS')
            case['reused_baseline'] = verify_replay(case, reused[source.stem], assembler)
        record['cases'][source.stem] = case
    require(set(reused) <= set(record['cases']), 'reuse assignment names an unknown kernel')
    target = output / 'inventory.json'
    target.write_text(json.dumps(record, indent=2) + '\n')
    print(json.dumps({'inventory': str(target), 'source_count': len(record['cases']),
                      'golden_ready': sum(case['readiness'] == 'golden_checked_baseline' for case in record['cases'].values()),
                      'reused': len(reused)}, indent=2))


def dispatch(inventory, name, smoke, output, timeout):
    record = json.loads(inventory.read_text())
    case = record['cases'][name]
    require(case['readiness'] == 'golden_checked_baseline', 'special cases need dedicated validation; refusing generic replay')
    require(not output.exists(), 'run output must be new')
    verify_artifact(record['assembler'])
    assembly, golden = verify_artifact(case['assembly']), verify_artifact(case['golden_fixture'])
    runner = checked_path(Path(__file__).parent / 'rtlgraph_perf.py')
    command = [sys.executable, '-B', str(runner), '--smoke-manifest', str(smoke),
               '--assembly', str(assembly), '--golden-json', str(golden), '--output', str(output),
               '--run', '--capture', '--timeout-seconds', str(timeout)]
    dispatch_path = output.with_name(output.name + '.dispatch.json')
    require(not dispatch_path.exists(), 'dispatch record already exists')
    dispatch_path.parent.mkdir(parents=True, exist_ok=True)
    dispatch_path.write_text(json.dumps({'schema': 'atlas.rtlgraph.corpus-dispatch.v1', 'created_utc': timestamp(),
        'driver': artifact(checked_path(__file__)), 'inventory': artifact(inventory), 'case': name,
        'runner': artifact(runner), 'argv': command, 'expected_check_words': case['expected_check_words'],
        'status': 'dispatch_prepared'}, indent=2) + '\n')
    print(json.dumps({'case': name, 'output': str(output), 'status': 'dispatching_one_replay'}), flush=True)
    os.execv(sys.executable, command)


def report(inventory, runs, output):
    record = json.loads(inventory.read_text())
    assembler = load_assembler(verify_artifact(record['assembler']))
    result = {'schema': 'atlas.rtlgraph.corpus-results.v1', 'created_utc': timestamp(),
              'driver': artifact(checked_path(__file__)), 'inventory': artifact(inventory), 'cases': {}}
    require(set(runs) <= set(record['cases']), 'run assignment names an unknown kernel')
    for name, case in record['cases'].items():
        reused = case.get('reused_baseline')
        path = runs.get(name)
        if path is None and reused:
            path = checked_path(reused['manifest']['path'])
        if case['readiness'] != 'golden_checked_baseline':
            entry = {'status': 'requires_dedicated_validation', 'reason': case['readiness']}
        elif path is None:
            entry = {'status': 'not_run'}
        else:
            try:
                if name not in runs:
                    verify_artifact(reused['manifest'])
                entry = verify_replay(case, path, assembler)
            except (ValueError, KeyError, OSError) as error:
                entry = {'status': 'not_verified_pass', 'reason': str(error), 'manifest_path': str(path)}
                if path.is_file():
                    entry['manifest'] = artifact(path)
                    try:
                        entry['reported_status'] = json.loads(path.read_text()).get('status')
                    except (ValueError, AttributeError):
                        entry['reported_status'] = 'unreadable_manifest'
        result['cases'][name] = entry
    output.parent.mkdir(parents=True, exist_ok=True)
    require(not output.exists(), 'report output must be new')
    output.write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps({name: entry['status'] for name, entry in result['cases'].items()}, indent=2))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='mode', required=True)
    p = commands.add_parser('prepare')
    p.add_argument('--baremetal-root', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--reuse', action='append', default=[])
    p = commands.add_parser('run')
    p.add_argument('--inventory', type=Path, required=True)
    p.add_argument('--case', required=True)
    p.add_argument('--smoke-manifest', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--timeout-seconds', type=float, default=1200)
    p = commands.add_parser('report')
    p.add_argument('--inventory', type=Path, required=True)
    p.add_argument('--run', action='append', default=[])
    p.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)
    if args.mode == 'prepare':
        prepare(checked_path(args.baremetal_root), checked_path(args.output), assignments(args.reuse))
    elif args.mode == 'run':
        require(args.timeout_seconds > 0, 'timeout must be positive')
        dispatch(checked_path(args.inventory), args.case, checked_path(args.smoke_manifest), checked_path(args.output), args.timeout_seconds)
    else:
        report(checked_path(args.inventory), assignments(args.run), checked_path(args.output))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

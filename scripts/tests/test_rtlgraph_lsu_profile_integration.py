#!/usr/bin/env python3
"""Typed LSU busy-tail mutation -> profile -> native compiler sensitivity.

Usage: python3 -B scripts/tests/test_rtlgraph_lsu_profile_integration.py TYPED_JSON ATLAS_OPT [--output DIR]
The mutation is a typed test fixture, not an RTL rebuild or performance result.
"""
import argparse
import copy
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


SCRIPTS = Path(__file__).resolve().parents[1]
SOURCES = [SCRIPTS / name for name in (
    'rtlgraph_lsu_profile.py', 'rtlgraph_lsu_routing.py', 'rtlgraph_lsu_response.py',
    'rtlgraph_lsu_timing.py', 'rtlgraph_lsu.py', 'rtlgraph_dma.py', 'rtlgraph_mxu1.py',
    'rtlgraph_mxu1_profile.py', 'rtlgraph_mxu1_capture.py', 'rtlgraph_query.py',
    'rtlgraph_s0.py', 'rtlgraph_vpu.py', 'tests/test_rtlgraph_lsu_routing.py')]
SOURCES.append(Path(__file__).resolve())
SOURCE_HASHES = {str(path): digest(path) for path in SOURCES}
sys.path.insert(0, str(SCRIPTS))
from rtlgraph_lsu_profile import settings
from rtlgraph_lsu_routing import analyze
from test_rtlgraph_lsu_routing import add_busy_tail


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + '\n')


def load_issues(path):
    cycle, issues = 0, []
    for line in path.read_text().splitlines():
        instruction = line.split('#', 1)[0].strip()
        if not instruction or instruction.endswith(':'):
            continue
        if instruction.startswith('vload '):
            issues.append(cycle)
        cycle += 1 + int(instruction.split()[1], 0) if instruction.startswith('delay ') else 1
    return issues


class LsuProfileIntegrationTest(unittest.TestCase):
    def test_typed_busy_tail_changes_profile_and_native_schedule(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        output = OUTPUT if OUTPUT else Path(temporary.name) / 'result'
        output.mkdir(parents=True, exist_ok=False)
        inputs = {str(path): digest(path) for path in (TYPED, COMPILER)}
        write_json(output / 'analysis-sources.json', SOURCE_HASHES)
        typed = json.loads(TYPED.read_text())
        self.assertFalse(typed['missing_modules'])
        modules = {module['name']: module for module in typed['modules']}
        self.assertEqual(set(modules), {'LSU', 'AtlasCore', 'MregFile', 'Vmem'})
        self.assertEqual(len(modules), len(typed['modules']))
        changed = copy.deepcopy(modules)
        add_busy_tail(changed['LSU'])
        source = output / 'before.S'
        source.write_text('vload m0, 0(x0)\nvload m1, 8(x0)\necall\n')
        results = {}

        def execute(command, log, *, success=True):
            run = subprocess.run([str(arg) for arg in command], capture_output=True, text=True)
            log.write_text(run.stdout + run.stderr)
            if success:
                self.assertEqual(run.returncode, 0, run.stderr)
            else:
                self.assertNotEqual(run.returncode, 0, 'Changed model accepted insufficient VLOAD path spacing')

        for label, fixture, free in (('baseline', modules, 35), ('synthetic_busy_tail', changed, 36)):
            directory = output / label
            directory.mkdir()
            fixture_path = directory / 'typed-fixture.json'
            write_json(fixture_path, fixture)
            facts = analyze(fixture)
            overrides = settings(facts)
            self.assertEqual(overrides['vload_first_free_age'], free)
            self.assertEqual(overrides['vstore_first_free_age'], 35)
            self.assertEqual((overrides['vload_read_age'], overrides['vload_write_age']), (1, 3))
            evidence = directory / 'test-derivation.json'
            write_json(evidence, dict(test_only=True, status=facts['status'], compiler_overrides=overrides,
                                     scope='Synthetic typed-fixture identity; not a production hardware profile or RTL replay.',
                                     typed_fixture_sha256=digest(fixture_path),
                                     timing=facts['lsu_timing']['temporal']['events']))
            profile = directory / 'test-lsu.profile'
            fields = dict(schema='atlas-lsu-profile-v1', config='EE290SimConfig',
                          source_ir_sha256=digest(fixture_path), evidence_sha256=digest(evidence), **overrides)
            profile.write_text('# Test-only: source_ir_sha256 identifies the typed fixture, not rebuilt hardware.\n' +
                               ''.join(f'{key}={value}\n' for key, value in fields.items()))
            dump = directory / 'footprints.json'
            flags = ['--rtl-lsu-profile', profile]
            execute([COMPILER, source, '--dump-footprints', dump, *flags], directory / 'query.log')
            footprint = json.loads(dump.read_text())['blocks'][0]['instructions'][0]['footprint']
            path_hold = next(hold for hold in footprint['holds'] if hold['unit'] == 'VLOAD path')
            self.assertEqual((path_hold['from'], path_hold['to'], footprint['done_age']), (0, free - 1, free - 1))
            assembly = directory / 'after.S'
            execute([COMPILER, source, '--passes', 'strip-artifacts,schedule', '--schedule-priority', 'input',
                     '-o', assembly, *flags], directory / 'schedule.log')
            issues = load_issues(assembly)
            self.assertEqual(issues, [0, free])
            execute([COMPILER, assembly, '--check', *flags], directory / 'check.log')
            results[label] = dict(first_free_age=free, path_hold_to=path_hold['to'],
                                  issue_cycles=issues, profile_sha256=digest(profile),
                                  assembly_sha256=digest(assembly), typed_fixture_sha256=digest(fixture_path))
        self.assertNotEqual(results['baseline']['typed_fixture_sha256'], results['synthetic_busy_tail']['typed_fixture_sha256'])
        execute([COMPILER, output / 'baseline' / 'after.S', '--check', '--rtl-lsu-profile',
                 output / 'synthetic_busy_tail' / 'test-lsu.profile'], output / 'old-schedule-rejected.log', success=False)
        for path, expected in (SOURCE_HASHES | inputs).items():
            self.assertEqual(digest(Path(path)), expected, f'Input changed during sensitivity check: {path}')
        write_json(output / 'results.json', dict(status='pass', typed_input_sha256=inputs[str(TYPED)],
                   compiler_sha256=inputs[str(COMPILER)], analysis_sources=SOURCE_HASHES, results=results,
                   scope='Typed analysis and compiler sensitivity only; no modified-RTL build, simulation or speedup claim.'))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('typed', type=Path)
    parser.add_argument('compiler', type=Path)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    TYPED, COMPILER = args.typed.resolve(), args.compiler.resolve()
    OUTPUT = args.output.resolve() if args.output else None
    unittest.main(argv=[sys.argv[0]])

#!/usr/bin/env python3
"""Exercise profile provenance and the native assembly handoff."""
import copy
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from rtlgraph_contract import (checked_file, checked_profile, export, identity, load_contract,
                               read_json, schedule, write_json)

COMPILER = Path(sys.argv.pop(1)).resolve() if len(sys.argv) > 1 and not sys.argv[1].startswith('-') else None


def mxu_profile(directory, *, role='mxu1', ir='a' * 64):
    directory.mkdir(parents=True)
    overrides = {'overwrite_acc_read_hold': 0}
    if role == 'mxu1':
        overrides['first_write_age'] = 3
    report = dict(schema_version=1, kind=f'atlas-partial-{role}-profile', config='EE290SimConfig',
                  inputs={'hardware_ir': {'path': '/external/hardware.mlir', 'sha256': ir}},
                  compiler_overrides=overrides, limitations=['Synthetic test fixture, not RTL evidence.'])
    evidence = directory / 'profile.json'
    write_json(evidence, report)
    profile = directory / f'atlas-{role}.profile'
    fields = dict(schema=f'atlas-{role}-profile-v1', config='EE290SimConfig', source_ir_sha256=ir,
                  evidence_sha256=identity(evidence)['sha256'], **overrides)
    profile.write_text(''.join(f'{key}={value}\n' for key, value in fields.items()))
    return profile


class ContractTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.profile = mxu_profile(self.root / 'profile')

    def tearDown(self):
        self.temp.cleanup()

    def test_projection_must_agree_with_exact_evidence_bytes(self):
        report = self.profile.with_name('profile.json')
        self.assertEqual(checked_profile('mxu1', self.profile, report)['first_write_age'], '3')
        self.profile.write_text(self.profile.read_text().replace('first_write_age=3', 'first_write_age=4'))
        with self.assertRaisesRegex(ValueError, 'settings differ'):
            checked_profile('mxu1', self.profile, report)

    def test_replaced_evidence_is_rejected(self):
        report = self.profile.with_name('profile.json')
        report.write_text(report.read_text() + '\n')
        with self.assertRaisesRegex(ValueError, 'evidence identity'):
            checked_profile('mxu1', self.profile, report)

    def test_mixed_ir_cannot_create_bundle(self):
        other = mxu_profile(self.root / 'other', role='mxu0', ir='b' * 64)
        with self.assertRaisesRegex(ValueError, 'different hardware IR'):
            export(self.root / 'absent.S', self.root / 'absent-compiler',
                   {'mxu1': self.profile, 'mxu0': other}, self.root / 'bundle')
        self.assertFalse((self.root / 'bundle').exists())

    def test_duplicate_profile_and_json_fields_are_rejected(self):
        self.profile.write_text(self.profile.read_text() + 'first_write_age=3\n')
        with self.assertRaisesRegex(ValueError, 'Duplicate field'):
            checked_profile('mxu1', self.profile, self.profile.with_name('profile.json'))
        malformed = self.root / 'duplicate.json'
        malformed.write_text('{"schema": 1, "schema": 2}')
        with self.assertRaisesRegex(ValueError, 'Duplicate field'):
            read_json(malformed)

    def test_paths_cannot_escape_bundle(self):
        record = identity(self.profile)
        for path in (str(self.profile), '../profile/atlas-mxu1.profile'):
            with self.assertRaisesRegex(ValueError, 'escapes'):
                checked_file(dict(record, path=path), self.root)
        (self.root / 'link').symlink_to('/tmp')
        with self.assertRaisesRegex(ValueError, 'symlink escapes'):
            checked_file(dict(record, path='link/not-needed'), self.root)

    @unittest.skipUnless(COMPILER, 'pass atlas-opt path for native integration')
    def test_native_export_schedule_and_changed_artifact_rejection(self):
        before = self.root / 'source.S'
        before.write_text('addi x1, x0, 5\naddi x2, x1, 7\necall\n')
        other = mxu_profile(self.root / 'other', role='mxu0')
        bundle = self.root / 'bundle'
        export(before, COMPILER, {'mxu0': other, 'mxu1': self.profile}, bundle)
        path = bundle / 'contract.json'
        contract, _, _, _ = load_contract(path)
        self.assertFalse(contract['semantics']['dma_cost_estimate_is_completion_bound'])
        self.assertEqual(contract['merlin']['adapter_status'], 'no-lossless-gap-projection')
        self.assertFalse(read_json(bundle / 'footprints.json')['schedule_validated'])
        moved = self.root / 'moved'
        shutil.copytree(bundle, moved)
        for priority in ('critical', 'input'):
            result = schedule(moved / 'contract.json', self.root / priority, priority)
            self.assertEqual(result['status'], 'native-model-checked')
            self.assertEqual(result['rtl_validation'], 'required-separately')
            self.assertIn('--experimental-mxu0-profile', result['commands']['schedule'])
        mutations = [('source', bundle / 'before.S'), ('footprints', bundle / 'footprints.json'),
                     ('profile', bundle / 'mxu1' / 'atlas-mxu1.profile')]
        for _, artifact in mutations:
            original = artifact.read_bytes()
            artifact.write_bytes(original + b'\n')
            with self.assertRaisesRegex(ValueError, 'Artifact changed'):
                load_contract(path)
            artifact.write_bytes(original)
        changed = copy.deepcopy(contract)
        changed['semantics']['publication_annotation'] = 'atlas.complete'
        write_json(path, changed)
        with self.assertRaisesRegex(ValueError, 'assembly semantics'):
            load_contract(path)
        write_json(path, contract)
        stale = read_json(bundle / 'footprints.json')
        stale['blocks'][0]['instructions'][0]['footprint']['done_age'] += 1
        write_json(bundle / 'footprints.json', stale)
        contract['footprints'] = identity(bundle / 'footprints.json', relative_to=bundle)
        write_json(path, contract)
        with self.assertRaisesRegex(ValueError, 'fresh native query'):
            schedule(path, self.root / 'stale-query')
        replacement = self.root / 'changed-compiler'
        replacement.write_text('changed')
        with self.assertRaisesRegex(ValueError, 'Artifact changed'):
            load_contract(path, replacement)


if __name__ == '__main__':
    unittest.main()

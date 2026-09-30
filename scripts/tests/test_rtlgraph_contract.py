#!/usr/bin/env python3
"""Regression coverage for the production RTL-model handoff."""
import hashlib
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from rtlgraph_contract import export, identity, load, read_json, write_json

COMPILER = Path(sys.argv.pop(1)).resolve()
REPOSITORY = Path(__file__).resolve().parents[2]
PROFILE_ROOT = REPOSITORY / 'profiles/EE290SimConfig'
PROFILES = {role: PROFILE_ROOT / role / f'atlas-{role}.profile'
            for role in ('mxu0', 'mxu1', 'dma', 'lsu', 'xlu')}


class ContractTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.before = self.root / 'before.S'
        self.before.write_text('li x6, 536870912\nvload m0, 0(x6)\nvtrpose.xlu m1, m0\n'
                               'vstore m1, 128(x6)\necall\n')

    def tearDown(self):
        self.temp.cleanup()

    def bundle(self, name='bundle'):
        output = self.root / name
        export(self.before, COMPILER, PROFILES, output)
        return output

    def test_checked_in_profiles_export_and_verify_native_footprints(self):
        bundle = self.bundle()
        contract = load(bundle / 'contract.json')
        self.assertEqual({entry['role'] for entry in contract['profiles']}, set(PROFILES))
        dump = read_json(bundle / 'footprints.json')
        load_insn = next(i for b in dump['blocks'] for i in b['instructions'] if i['opcode'] == 'vload')
        self.assertEqual({a['age'] for a in load_insn['footprint']['accesses'] if a['write']}, {3})
        self.assertTrue(dump['model']['rtl_xlu'])
        self.assertEqual(contract['semantics']['dma_completion'], 'explicit-wait')
        self.assertEqual(contract['merlin']['adapter_status'], 'no-lossless-gap-projection')
        moved = self.root / 'moved'
        shutil.copytree(bundle, moved)
        load(moved / 'contract.json')

    def test_profiles_without_dma_retain_modeled_completion(self):
        for role in ('lsu', 'mxu1'):
            with self.subTest(role=role):
                bundle = self.root / role
                export(self.before, COMPILER, {role: PROFILES[role]}, bundle)
                contract = load(bundle / 'contract.json')
                self.assertEqual(contract['semantics']['dma_completion'], 'modeled-completion')
                self.assertFalse(read_json(bundle / 'footprints.json')['model']['rtl_dma'])

    def test_contract_semantics_cannot_be_changed_or_omitted(self):
        bundle = self.bundle()
        path = bundle / 'contract.json'
        contract = read_json(path)
        semantics = contract['semantics']
        cases = [None, {}, {**semantics, 'unknown': 'value'}]
        for key in semantics:
            cases.extend(({**semantics, key: 'changed'},
                          {k: v for k, v in semantics.items() if k != key}))
        for altered in cases:
            with self.subTest(semantics=altered):
                document = {k: v for k, v in contract.items() if k != 'semantics'}
                if altered is not None:
                    document['semantics'] = altered
                write_json(path, document)
                with self.assertRaisesRegex(ValueError, 'Contract semantics'):
                    load(path)

    def test_native_semantics_must_agree_with_selected_profiles(self):
        bundle = self.bundle()
        path = bundle / 'contract.json'
        contract = read_json(path)
        footprints = read_json(bundle / 'footprints.json')
        cases = [('semantics', 'age_origin', 'reset'), ('semantics', 'hold_end', 'exclusive'),
                 ('semantics', 'at_completion', 'access at modeled DMA completion'),
                 ('model', 'rtl_dma', False)]
        for section, key, value in cases:
            with self.subTest(field=key):
                altered = {**footprints, section: {**footprints[section], key: value}}
                write_json(bundle / 'footprints.json', altered)
                contract['footprints'] = identity(bundle / 'footprints.json', bundle)
                write_json(path, contract)
                with self.assertRaisesRegex(ValueError, 'Unsupported native|DMA model differs'):
                    load(path)

    def test_profile_evidence_and_common_hardware_are_checked(self):
        source = self.root / 'changed-lsu'
        shutil.copytree(PROFILE_ROOT / 'lsu', source)
        evidence = source / 'profile.json'
        report = read_json(evidence)
        report['inputs']['hardware_ir']['sha256'] = 'b' * 64
        write_json(evidence, report)
        projection = source / 'atlas-lsu.profile'
        text = projection.read_text().replace(
            'source_ir_sha256=' + 'd2fd900eadda35788ca85a4c0f3ad8058d7ca7c1856af4351b6bd6be6cf1fbe2',
            'source_ir_sha256=' + 'b' * 64)
        old_hash = next(line.split('=', 1)[1] for line in text.splitlines() if line.startswith('evidence_sha256='))
        new_hash = hashlib.sha256(evidence.read_bytes()).hexdigest()
        projection.write_text(text.replace('evidence_sha256=' + old_hash, 'evidence_sha256=' + new_hash))
        with self.assertRaisesRegex(ValueError, 'different hardware IR'):
            export(self.before, COMPILER, {'mxu1': PROFILES['mxu1'], 'lsu': projection}, self.root / 'mixed')
        evidence.write_text(evidence.read_text() + '\n')
        with self.assertRaisesRegex(ValueError, 'profile/evidence identity'):
            export(self.before, COMPILER, {'lsu': projection}, self.root / 'stale')

    def test_bundle_paths_hashes_and_fresh_query_are_checked(self):
        bundle = self.bundle()
        contract_path = bundle / 'contract.json'
        original = (bundle / 'before.S').read_bytes()
        (bundle / 'before.S').write_bytes(original + b'\n')
        with self.assertRaisesRegex(ValueError, 'Artifact changed'):
            load(contract_path)
        (bundle / 'before.S').write_bytes(original)
        contract = read_json(contract_path)
        contract['source']['path'] = '../before.S'
        write_json(contract_path, contract)
        with self.assertRaisesRegex(ValueError, 'escapes'):
            load(contract_path)
        contract['source'] = identity(bundle / 'before.S', bundle)
        write_json(contract_path, contract)
        footprints = read_json(bundle / 'footprints.json')
        footprints['blocks'][0]['instructions'][0]['footprint']['done_age'] += 1
        write_json(bundle / 'footprints.json', footprints)
        contract['footprints'] = identity(bundle / 'footprints.json', bundle)
        write_json(contract_path, contract)
        with self.assertRaisesRegex(ValueError, 'fresh atlas-opt query'):
            load(contract_path)


if __name__ == '__main__':
    unittest.main()

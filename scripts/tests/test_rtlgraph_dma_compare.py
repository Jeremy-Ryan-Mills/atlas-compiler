"""Check fair-comparison joins separately from DMA waveform obligations."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from rtlgraph_dma_compare import compare
from rtlgraph_dma_compile import translate
from rtlgraph_s0 import artifact


SOURCE = '''LI x6, 0x20000000
DMA.LOAD x6, x1, x12, 0
DMA.WAIT 0
CSRR x20, 0xC00
VLOAD 0, x6, 0
DELAY 35
CSRR x21, 0xC00
DMA.STORE x3, x6, x12, 1
DMA.WAIT 1
CSRW x30, 0xC10
ECALL
'''


class DmaCompareTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.assembler = self.root / 'assembler.py'
        self.assembler.write_text('import hashlib\ndef assemble(source):\n'
            ' return [int.from_bytes(hashlib.sha256(s.strip().encode()).digest()[:4], "little") for s in source.splitlines() if s.strip()]\n')
        golden = self.root / 'golden.json'
        golden.write_text('{}\n')
        self.runs, self.sources, self.reports = {}, {}, {}
        for name, count in [('original', 1000), ('memory_baseline', 900), ('output_overlap', 850)]:
            directory = self.root / name
            directory.mkdir()
            source = directory / 'kernel.S'
            text = SOURCE
            if name == 'output_overlap':
                text = text.replace('CSRR x21, 0xC00\nDMA.STORE x3, x6, x12, 1',
                                    'DMA.STORE x3, x6, x12, 1\nCSRR x21, 0xC00')
            source.write_text(text)
            capture = directory / 'capture.tcl'
            capture.write_text('run\nquit\n')
            simulator, binary = directory / 'runtime/simulator', directory / 'kernel.riscv'
            simulator.parent.mkdir()
            simulator.write_text('simulator\n')
            runtime_data, library = simulator.parent / 'data', directory / 'library'
            runtime_data.write_text('runtime data\n')
            library.write_text('library\n')
            manifest = directory / 'manifest.json'
            manifest.write_text(json.dumps({'simulator': artifact(simulator),
                'binary': {'path': str(binary)}, 'capture_tcl': artifact(capture),
                'prepared_command': {'argv': [str(simulator), '-i', str(capture), '+ntb_random_seed=1',
                                              '+loadmem=' + str(binary), str(binary)]},
                'runtime_files': [{'relative_path': 'data', **artifact(runtime_data)}],
                'runtime_libraries': {'lib': artifact(library)}}))
            self.runs[name], self.sources[name] = manifest, source
            self.reports[manifest] = {
                'commands': [{'op': 0, 'channel': 0, 'line': 0, 'dram': 0x90000000, 'size': 2048},
                             {'op': 1, 'channel': 1, 'line': 0, 'dram': 0x90001000, 'size': 2048}],
                'completion': {'assembly': artifact(source), 'assembler': artifact(self.assembler),
                    'golden_fixture': artifact(golden), 'first_instruction': {'cycle': 100},
                    'replay_control': {'capacity_words': 128, 'elf_without_program_sha256': 'same'},
                    'functional_result': {'checked_words': 1536},
                    'metrics': {'first_issue_to_dbg0_edges': count, 'csr_counter_delta': 800}}}
        self.static = self.root / 'candidates.json'
        self.record = {'schema': 'atlas.rtlgraph.dma-schedule-experiment.v1', 'status': 'candidates_ready',
            'inputs': {'source': artifact(self.sources['original']),
                       'memory_baseline': artifact(self.sources['memory_baseline']),
                       'assembler': artifact(self.assembler)},
            'cases': {'output_overlap': {'assembly': artifact(self.sources['output_overlap'])}}}
        self.save_static()
        mock = patch('rtlgraph_dma_compare.observation', side_effect=lambda p: copy.deepcopy(self.reports[p]))
        mock.start()
        self.addCleanup(mock.stop)

    def save_static(self):
        self.static.write_text(json.dumps(self.record))

    def run_compare(self):
        return compare(self.runs['original'], self.runs['memory_baseline'],
                       {'output_overlap': self.runs['output_overlap']}, self.static)

    def test_cross_marker_schedule_uses_completion_only(self):
        result = self.run_compare()
        gains = result['comparisons']['output_overlap']
        self.assertEqual(gains['original']['first_issue_to_dbg0_edges']['edges_saved'], 150)
        self.assertEqual(gains['memory_baseline']['first_issue_to_dbg0_edges']['edges_saved'], 50)
        self.assertEqual(set(gains['original']), {'first_issue_to_dbg0_edges'})
        self.assertIn('no CSR speedup', result['scope']['csr'])

    def test_changed_transfer_address_rejected(self):
        self.reports[self.runs['output_overlap']]['commands'][1]['dram'] += 2048
        with self.assertRaisesRegex(ValueError, 'transfers'):
            self.run_compare()

    def test_operation_or_wait_removal_rejected(self):
        source = self.sources['output_overlap']
        source.write_text(source.read_text().replace('DMA.WAIT 1\n', ''))
        self.record['cases']['output_overlap']['assembly'] = artifact(source)
        self.reports[self.runs['output_overlap']]['completion']['assembly'] = artifact(source)
        self.save_static()
        with self.assertRaisesRegex(ValueError, 'operations'):
            self.run_compare()

    def test_missing_control_rejected(self):
        del self.reports[self.runs['output_overlap']]['completion']['replay_control']
        with self.assertRaisesRegex(ValueError, 'fixed-host'):
            self.run_compare()

    def test_changed_launch_edge_rejected(self):
        self.reports[self.runs['output_overlap']]['completion']['first_instruction']['cycle'] += 1
        with self.assertRaisesRegex(ValueError, 'first_issue'):
            self.run_compare()

    def test_changed_runtime_seed_rejected(self):
        path = self.runs['output_overlap']
        record = json.loads(path.read_text())
        record['prepared_command']['argv'] = [s.replace('seed=1', 'seed=2') for s in record['prepared_command']['argv']]
        path.write_text(json.dumps(record))
        with self.assertRaisesRegex(ValueError, 'runtime'):
            self.run_compare()

    def test_replay_must_be_from_prepared_candidate(self):
        self.record['cases']['output_overlap']['assembly'] = artifact(self.sources['original'])
        self.save_static()
        with self.assertRaisesRegex(ValueError, 'source differs'):
            self.run_compare()

    def test_mutated_runtime_bytes_rejected(self):
        run = json.loads(self.runs['output_overlap'].read_text())
        files = [Path(run['simulator']['path']), Path(run['runtime_files'][0]['path']),
                 Path(run['runtime_libraries']['lib']['path'])]
        for path in files:
            original = path.read_bytes()
            path.write_bytes(original + b'tampered')
            try:
                with self.subTest(path=path), self.assertRaisesRegex(ValueError, 'SHA-256 changed'):
                    self.run_compare()
            finally:
                path.write_bytes(original)

    def native_fixture(self, version=1):
        compiler, driver, hardware = [self.root / name for name in ('atlas-opt', 'adapter.py', 'hardware.mlir')]
        for path in (compiler, driver, hardware):
            path.write_text(path.name + '\n')
        canonical = self.root / 'profile.json'
        overrides = dict(completion='explicit-wait', operand_capture='issue')
        canonical.write_text(json.dumps(dict(schema=f'atlas.rtlgraph.dma-profile.v{version}', config='EE290SimConfig', compiler_overrides=overrides,
                                             inputs=dict(hardware_ir=artifact(hardware)))))
        profile = self.root / 'atlas-dma.profile'
        profile.write_text(f'schema=atlas-dma-profile-v{version}\nconfig=EE290SimConfig\n'
                           f'source_ir_sha256={artifact(hardware)["sha256"]}\n'
                           f'evidence_sha256={artifact(canonical)["sha256"]}\n'
                           + ''.join(f'{key}={value}\n' for key, value in overrides.items()))
        native_input = self.root / 'native.compiler.S'
        native_input.write_text(translate(SOURCE, to_compiler=True))
        checked = self.root / 'final-check.compiler.S'
        checked.write_text(translate(self.sources['output_overlap'].read_text(), to_compiler=True))
        scheduled, log = self.root / 'scheduled.compiler.S', self.root / 'compile.log'
        scheduled.write_text(checked.read_text())
        log.write_text('Passed\n')
        case = dict(self.record['cases']['output_overlap'], scheduled=artifact(scheduled), log=artifact(log),
                    final_check_input=artifact(checked), final_check_log=artifact(log),
                    scheduler_returncode=0, final_check_returncode=0,
                    command=[str(compiler), str(native_input), '--passes', 'strip-artifacts,schedule',
                             '--schedule-priority', 'critical', '--rtl-dma-profile', str(profile), '-o', str(scheduled)],
                    final_check_command=[str(compiler), '--check', str(checked), '--rtl-dma-profile', str(profile)])
        self.record.update(schema='atlas.rtlgraph.dma-native-schedule.v1', status='native_model_candidates_ready',
                           native_input=artifact(native_input), instrumentation=dict(relocated=False, private_registers=[]),
                           cases=dict(native_critical=case))
        self.record['inputs'].pop('memory_baseline')
        self.record['inputs'].update(compiler=artifact(compiler), profile=artifact(profile), driver=artifact(driver))
        self.save_static()
        return case

    def compare_native(self):
        return compare(self.runs['original'], self.runs['memory_baseline'],
                       {'native_critical': self.runs['output_overlap']}, self.static)

    def test_native_manifest_uses_independently_observed_baseline_and_rechecks_final_stream(self):
        self.native_fixture()
        with patch('rtlgraph_dma_compare.subprocess.run') as check:
            check.return_value.returncode = 0
            result = self.compare_native()
        check.assert_called_once()
        self.assertTrue(result['native_compiler_audit']['exact_final_native_checks_repeated'])
        self.assertEqual(result['comparisons']['native_critical']['memory_baseline']['first_issue_to_dbg0_edges']['edges_saved'], 50)

    def test_native_v2_can_compare_directly_with_original(self):
        self.native_fixture(version=2)
        with patch('rtlgraph_dma_compare.subprocess.run') as check:
            check.return_value.returncode = 0
            result = compare(self.runs['original'], None,
                             {'native_critical': self.runs['output_overlap']}, self.static)
        self.assertEqual(set(result['cases']), {'original', 'native_critical'})
        self.assertEqual(set(result['comparisons']['native_critical']), {'original'})
        self.assertEqual(result['comparisons']['native_critical']['original']['first_issue_to_dbg0_edges']['edges_saved'], 150)

    def test_lsu_profile_is_bound_to_native_check_and_common_hardware(self):
        case = self.native_fixture(version=2)
        directory = self.root / 'lsu'
        directory.mkdir()
        hardware = artifact(self.root / 'hardware.mlir')
        evidence = directory / 'profile.json'
        evidence.write_text(json.dumps(dict(schema='atlas.rtlgraph.lsu-profile.v1', config='EE290SimConfig',
            inputs=dict(hardware_ir=hardware), compiler_overrides=dict(vload_read_age=1))))
        profile = directory / 'atlas-lsu.profile'
        profile.write_text('schema=atlas-lsu-profile-v1\nconfig=EE290SimConfig\n'
                           f'source_ir_sha256={hardware["sha256"]}\nevidence_sha256={artifact(evidence)["sha256"]}\n'
                           'vload_read_age=1\n')
        self.record['inputs']['lsu_profile'] = artifact(profile)
        case['command'][-2:-2] = ['--rtl-lsu-profile', str(profile)]
        case['final_check_command'] += ['--rtl-lsu-profile', str(profile)]
        self.save_static()
        with patch('rtlgraph_dma_compare.subprocess.run') as check:
            check.return_value.returncode = 0
            result = self.compare_native()
        self.assertIn('--rtl-lsu-profile', check.call_args.args[0])
        self.assertEqual(result['native_compiler_audit']['lsu_profile']['projection'], artifact(profile))
        case['final_check_command'] = case['final_check_command'][:-2]
        self.save_static()
        with self.assertRaisesRegex(ValueError, 'check command differs'):
            self.compare_native()
        profile.write_text(profile.read_text().replace('vload_read_age=1', 'vload_read_age=2'))
        self.record['inputs']['lsu_profile'] = artifact(profile)
        self.save_static()
        with self.assertRaisesRegex(ValueError, 'settings differ'):
            self.compare_native()

    def test_native_profile_and_canonical_versions_must_match(self):
        self.native_fixture(version=2)
        canonical = self.root / 'profile.json'
        record = json.loads(canonical.read_text())
        record['schema'] = 'atlas.rtlgraph.dma-profile.v1'
        old_hash = artifact(canonical)['sha256']
        canonical.write_text(json.dumps(record))
        profile = self.root / 'atlas-dma.profile'
        profile.write_text(profile.read_text().replace(old_hash, artifact(canonical)['sha256']))
        self.record['inputs']['profile'] = artifact(profile)
        self.save_static()
        with self.assertRaisesRegex(ValueError, 'profile evidence differs'):
            self.compare_native()

    def test_legacy_experiment_still_requires_its_memory_baseline(self):
        with self.assertRaisesRegex(ValueError, 'require a memory baseline'):
            compare(self.runs['original'], None,
                    {'output_overlap': self.runs['output_overlap']}, self.static)

    def test_native_final_check_must_bind_replayed_stream_and_selected_model(self):
        case = self.native_fixture()
        case['final_check_command'][-1] = 'another.profile'
        self.save_static()
        with self.assertRaisesRegex(ValueError, 'check command differs'):
            self.compare_native()
        case['final_check_command'][-1] = self.record['inputs']['profile']['path']
        path = Path(case['final_check_input']['path'])
        path.write_text(path.read_text().replace('dma.wait.ch1', 'nop'))
        case['final_check_input'] = artifact(path)
        self.save_static()
        with self.assertRaisesRegex(ValueError, 'did not cover'):
            self.compare_native()

    def test_native_profile_canonical_provenance_and_binary_are_required(self):
        self.native_fixture()
        canonical = self.root / 'profile.json'
        canonical.write_text(canonical.read_text() + ' ')
        with self.assertRaisesRegex(ValueError, 'profile evidence differs'):
            self.compare_native()

    def test_native_candidate_must_come_from_recorded_compiler_output(self):
        case = self.native_fixture()
        scheduled = Path(case['scheduled']['path'])
        scheduled.write_text(scheduled.read_text().replace('dma.wait.ch1', 'nop'))
        case['scheduled'] = artifact(scheduled)
        self.save_static()
        with self.assertRaisesRegex(ValueError, 'differs from native compiler output'):
            self.compare_native()

    def test_native_checker_failure_rejected_even_with_passing_record(self):
        self.native_fixture()
        with patch('rtlgraph_dma_compare.subprocess.run') as check:
            check.return_value.returncode = 1
            check.return_value.stdout, check.return_value.stderr = '', 'resource conflict'
            with self.assertRaisesRegex(ValueError, 'no longer passes'):
                self.compare_native()

    def add_mxu_profile(self, engine):
        directory = self.root / f'mxu{engine}'
        directory.mkdir()
        dma = json.loads((self.root / 'profile.json').read_text())
        typed = directory / 'typed.json'
        typed.write_text('{}\n')
        canonical = directory / 'profile.json'
        overrides = dict(overwrite_acc_read_hold=0)
        if engine == 1:
            overrides['first_write_age'] = 3
        canonical.write_text(json.dumps(dict(schema_version=1, kind=f'atlas-partial-mxu{engine}-profile',
            config='EE290SimConfig', inputs=dict(hardware_ir=dma['inputs']['hardware_ir']),
            typed=artifact(typed), compiler_overrides=overrides)))
        profile = directory / f'atlas-mxu{engine}.profile'
        profile.write_text(f'schema=atlas-mxu{engine}-profile-v1\nconfig=EE290SimConfig\n'
                          f'source_ir_sha256={dma["inputs"]["hardware_ir"]["sha256"]}\n'
                          f'evidence_sha256={artifact(canonical)["sha256"]}\n' +
                          ''.join(f'{key}={value}\n' for key, value in overrides.items()))
        self.record['inputs'][f'mxu{engine}_profile'] = artifact(profile)
        case = self.record['cases']['native_critical']
        flags = [f'--experimental-mxu{engine}-profile', str(profile)]
        case['command'][-2:-2] = flags
        case['final_check_command'].extend(flags)
        self.save_static()
        return profile

    def test_native_combined_profiles_bind_both_scheduler_and_checker(self):
        self.native_fixture(version=2)
        for engine in (0, 1):
            self.add_mxu_profile(engine)
        with patch('rtlgraph_dma_compare.subprocess.run') as check:
            check.return_value.returncode = 0
            result = self.compare_native()
        self.assertEqual(set(result['native_compiler_audit']['mxu_profiles']), {'mxu0_profile', 'mxu1_profile'})
        self.assertIn('--experimental-mxu1-profile', check.call_args.args[0])

    def test_native_mxu_profile_cannot_be_omitted_from_final_check(self):
        case = self.native_fixture(version=2)
        self.add_mxu_profile(1)
        del case['final_check_command'][-2:]
        self.save_static()
        with self.assertRaisesRegex(ValueError, 'check command differs'):
            self.compare_native()

    def test_native_mxu_projection_and_ir_identity_are_verified(self):
        self.native_fixture(version=2)
        profile = self.add_mxu_profile(1)
        text = profile.read_text()
        profile.write_text(text.replace('first_write_age=3', 'first_write_age=4'))
        self.record['inputs']['mxu1_profile'] = artifact(profile)
        self.save_static()
        with self.assertRaisesRegex(ValueError, 'projection differs'):
            self.compare_native()
        evidence_path = profile.parent / 'profile.json'
        evidence = json.loads(evidence_path.read_text())
        evidence['inputs']['hardware_ir']['sha256'] = '0' * 64
        evidence_path.write_text(json.dumps(evidence))
        with self.assertRaisesRegex(ValueError, 'hardware IR differ'):
            self.compare_native()

    def test_native_mxu_typed_evidence_mutation_is_rejected(self):
        self.native_fixture(version=2)
        profile = self.add_mxu_profile(0)
        (profile.parent / 'typed.json').write_text('changed\n')
        with self.assertRaisesRegex(ValueError, 'SHA-256 changed'):
            self.compare_native()

    def test_mxu_program_cannot_use_only_a_dma_observation(self):
        with patch('rtlgraph_dma_compare.instruction_words', return_value=[0x77]):
            with self.assertRaisesRegex(ValueError, 'require a mixed-engine observation'):
                self.run_compare()

    def test_strengthened_mixed_observation_preserves_prior_report(self):
        for report in self.reports.values():
            report['mxu'] = {'mxu1': {'command_count': 2}}
            report['driver'] = {'sha256': '0' * 64}
        first = self.run_compare()
        path = Path(first['cases']['original']['observation']['path'])
        before = path.read_bytes()
        for report in self.reports.values():
            report['driver']['sha256'] = '1' * 64
        second = self.run_compare()
        self.assertNotEqual(second['cases']['original']['observation']['path'], str(path))
        self.assertEqual(path.read_bytes(), before)


if __name__ == '__main__':
    unittest.main()

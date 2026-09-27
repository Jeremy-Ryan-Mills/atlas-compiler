"""Exercise comparison joins; scalar waveform validation has separate tests."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from rtlgraph_compare import compare
from rtlgraph_s0 import artifact


SOURCE = '''ADDI x1, x0, 0
CSRR x20, 0xC00
VMATMUL.MXU1 0, 2, 0
VMATPOP.FP8.MXU1 4, 0, 0
DELAY 31
CSRR x21, 0xC00
DMA.WAIT 0
ADDI x30, x0, 1
CSRW x30, 0xC10
ECALL
'''


class CompareTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        # A deterministic token encoder is sufficient for join invariants. The
        # hardware assembler and completion binding are exercised elsewhere.
        self.assembler = self.root / 'token_assembler.py'
        self.assembler.write_text('import hashlib\n'
            'def assemble(source):\n'
            '    lines = [line.split("#", 1)[0].strip() for line in source.splitlines()]\n'
            '    return [int.from_bytes(hashlib.sha256(line.encode()).digest()[:4], "little") for line in lines if line]\n')
        self.golden = self.root / 'golden.json'
        self.golden.write_text('{"dram_checks": [1]}\n')
        self.runs, self.sources, self.completions = {}, {}, {}
        for name, count in [('original', 100), ('builtin_critical', 90), ('profile_critical', 80), ('profile_input', 85)]:
            directory = self.root / name
            directory.mkdir()
            source = directory / (name + '.S')
            source.write_text(SOURCE if name == 'original' else SOURCE.replace('DELAY 31', 'DELAY 30'))
            capture = directory / 'capture.tcl'
            capture.write_text('run\nquit\n')
            simulator, binary = directory / 'runtime' / 'simulator', directory / 'kernel.riscv'
            manifest = directory / 'manifest.json'
            manifest.write_text(json.dumps({'simulator': {'path': str(simulator), 'sha256': 'a' * 64},
                'binary': {'path': str(binary)}, 'capture_tcl': artifact(capture),
                'prepared_command': {'argv': [str(simulator), '+permissive', '-no_save',
                    '-cm_dir', str(directory / 'coverage'), '-cm_name', name,
                    '-ucli', '-i', str(capture), '+ntb_random_seed=1',
                    '+dramsim_ini_dir=' + str(simulator.parent / 'dramsim2_ini'),
                    '+loadmem=' + str(binary), '+permissive-off', str(binary)]},
                'runtime_files': [{'relative_path': 'runtime.so', 'sha256': 'b' * 64}],
                'runtime_libraries': {'libstdc++': {'sha256': 'c' * 64}}}))
            self.runs[name], self.sources[name] = manifest, source
            self.completions[manifest] = {'status': 'observed_completion_markers_passed',
                'assembly': artifact(source), 'assembler': artifact(self.assembler),
                'golden_fixture': artifact(self.golden), 'replay_manifest': artifact(manifest),
                'functional_result': {'checked_words': 8},
                'metrics': {'csr_counter_delta': count, 'first_issue_to_dbg0_edges': count + 100,
                            'csr_start_to_dbg0_edges': count + 20}}
        self.static = self.root / 'candidates.json'
        self.static_record = {'schema': 'atlas.rtlgraph.schedule-experiment.v1', 'status': 'model_candidates_ready',
            'inputs': {'source': artifact(self.sources['original'])},
            'cases': {name: {'assembly': artifact(source)} for name, source in self.sources.items() if name != 'original'}}
        self.save_static()
        self.mock = patch('rtlgraph_compare.summarize', side_effect=lambda path: copy.deepcopy(self.completions[path]))
        self.mock.start()
        self.addCleanup(self.mock.stop)

    def save_static(self):
        self.static.write_text(json.dumps(self.static_record))

    def run_compare(self, names=('builtin_critical', 'profile_critical')):
        return compare(self.runs['original'], {name: self.runs[name] for name in names}, self.static)

    def change_source(self, old, new):
        name = 'builtin_critical'
        source = self.sources[name]
        source.write_text(source.read_text().replace(old, new))
        self.completions[self.runs[name]]['assembly'] = artifact(source)
        self.static_record['cases'][name]['assembly'] = artifact(source)
        self.save_static()

    def test_same_priority_profile_attribution(self):
        report = self.run_compare()
        self.assertTrue(report['identical_setup_suffix_and_non_idle_operations'])
        self.assertTrue(report['identical_runtime_and_golden'])
        refs = report['comparisons']['profile_critical']
        self.assertEqual(set(refs), {'original', 'builtin_critical'})
        self.assertEqual(refs['builtin_critical']['csr_counter_delta']['cycles_saved'], 10)
        self.assertEqual(refs['original']['csr_counter_delta']['cycles_saved'], 20)

    def test_different_priority_is_not_attributed_to_profile(self):
        report = self.run_compare(('builtin_critical', 'profile_input'))
        self.assertEqual(set(report['comparisons']['profile_input']), {'original'})

    def test_changed_setup_rejected(self):
        self.change_source('ADDI x1, x0, 0', 'ADDI x1, x0, 1')
        with self.assertRaisesRegex(ValueError, 'setup, suffix, or non-idle'):
            self.run_compare()

    def test_changed_suffix_rejected(self):
        self.change_source('DMA.WAIT 0', 'DMA.WAIT 1')
        with self.assertRaisesRegex(ValueError, 'setup, suffix, or non-idle'):
            self.run_compare()

    def test_changed_nonidle_multiset_rejected(self):
        self.change_source('VMATMUL.MXU1 0, 2, 0', 'VMATMUL.MXU1 0, 3, 0')
        with self.assertRaisesRegex(ValueError, 'setup, suffix, or non-idle'):
            self.run_compare()

    def test_changed_golden_rejected(self):
        self.completions[self.runs['builtin_critical']]['golden_fixture']['sha256'] = 'd' * 64
        with self.assertRaisesRegex(ValueError, 'golden fixtures differ'):
            self.run_compare()

    def test_changed_runtime_components_rejected(self):
        path = self.runs['builtin_critical']
        original = json.loads(path.read_text())
        for component in ('simulator', 'runtime_files', 'runtime_libraries'):
            with self.subTest(component=component):
                changed = copy.deepcopy(original)
                if component == 'simulator':
                    changed[component]['sha256'] = 'd' * 64
                elif component == 'runtime_files':
                    changed[component][0]['sha256'] = 'd' * 64
                else:
                    changed[component]['libstdc++']['sha256'] = 'd' * 64
                path.write_text(json.dumps(changed))
                with self.assertRaisesRegex(ValueError, 'runtime differs'):
                    self.run_compare()

    def test_incomplete_replay_failure_propagates(self):
        with patch('rtlgraph_compare.summarize', side_effect=ValueError('replay is incomplete')):
            with self.assertRaisesRegex(ValueError, 'incomplete'):
                self.run_compare()

    def test_changed_random_seed_rejected(self):
        path = self.runs['builtin_critical']
        run = json.loads(path.read_text())
        run['prepared_command']['argv'] = [arg.replace('+ntb_random_seed=1', '+ntb_random_seed=2')
                                           for arg in run['prepared_command']['argv']]
        path.write_text(json.dumps(run))
        with self.assertRaisesRegex(ValueError, 'runtime differs'):
            self.run_compare()

    def test_loaded_binary_must_match_recorded_binary(self):
        path = self.runs['builtin_critical']
        run = json.loads(path.read_text())
        run['prepared_command']['argv'] = ['+loadmem=/unrecorded.riscv' if arg.startswith('+loadmem=') else arg
                                           for arg in run['prepared_command']['argv']]
        path.write_text(json.dumps(run))
        with self.assertRaisesRegex(ValueError, 'loaded binary differs'):
            self.run_compare()

    def test_changed_capture_artifact_rejected(self):
        run = json.loads(self.runs['builtin_critical'].read_text())
        Path(run['capture_tcl']['path']).write_text('quit\n')
        with self.assertRaisesRegex(ValueError, 'hash|changed|mismatch'):
            self.run_compare()

    def test_source_must_match_prepared_experiment(self):
        self.static_record['cases']['builtin_critical']['assembly']['sha256'] = 'd' * 64
        self.save_static()
        with self.assertRaisesRegex(ValueError, 'prepared experiment'):
            self.run_compare()

    def test_stale_completion_metrics_rejected(self):
        previous = copy.deepcopy(self.completions[self.runs['original']])
        previous['metrics']['csr_counter_delta'] = 1
        (self.runs['original'].parent / 'completion.json').write_text(json.dumps(previous))
        with self.assertRaisesRegex(ValueError, 'existing completion report differs'):
            self.run_compare()

    def test_stale_completion_status_rejected(self):
        previous = copy.deepcopy(self.completions[self.runs['original']])
        previous['status'] = 'failed'
        (self.runs['original'].parent / 'completion.json').write_text(json.dumps(previous))
        with self.assertRaisesRegex(ValueError, 'existing completion report differs'):
            self.run_compare()

    def test_historical_completion_driver_preserved(self):
        previous = copy.deepcopy(self.completions[self.runs['original']])
        previous['driver'] = {'path': '/historical/driver.py', 'sha256': 'd' * 64}
        target = self.runs['original'].parent / 'completion.json'
        target.write_text(json.dumps(previous))
        self.run_compare()
        self.assertEqual(json.loads(target.read_text()), previous)


if __name__ == '__main__':
    unittest.main()

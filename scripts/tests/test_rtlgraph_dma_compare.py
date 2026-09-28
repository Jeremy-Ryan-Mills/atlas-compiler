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


if __name__ == '__main__':
    unittest.main()

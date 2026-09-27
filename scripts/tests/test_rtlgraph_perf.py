#!/usr/bin/env python3
"""Check that performance measurements require correctness and a real window."""

import json
from pathlib import Path
import sys
import tempfile
import types
import unittest

sys.path.insert(0, str(Path(__file__).absolute().parents[1]))
from rtlgraph_perf import capture_signal_map, fixture_info, performance_result
import rtlgraph_mxu1_vcd as selection


class PerfTests(unittest.TestCase):
    def setUp(self):
        self.run = {'returncode': 0, 'timed_out': False}
        self.log = ('DBG0 = 1\nmcycles = 99999\nminstret = 128\nstatus = 0x00000005\n'
                    'dbg1_cycles = 290\nutil_mxu1 = 88%\nVerifying DRAM results (1024 words) ...\n'
                    '*** PASSED *** (example — all DRAM checks passed)\n')

    def test_records_bracket_separately_from_raw_counter(self):
        result = performance_result(self.run, self.log, 'example', 1024)
        self.assertEqual(result['status'], 'PASS')
        self.assertEqual(result['metrics']['dbg1_cycles'], 290)
        self.assertEqual(result['metrics']['raw_mcycles'], 99999)

    def test_bad_completion_and_short_check_count_fail(self):
        for log in (self.log.replace('DBG0 = 1', 'DBG0 = 2'), self.log.replace('0x00000005', '0x00000003'), self.log.replace('(1024 words)', '(512 words)')):
            self.assertNotEqual(performance_result(self.run, log, 'example', 1024)['status'], 'PASS')

    def test_zero_missing_and_ambiguous_cycle_windows_fail(self):
        for log in (self.log.replace('dbg1_cycles = 290', 'dbg1_cycles = 0'), self.log.replace('dbg1_cycles = 290', ''), self.log + 'dbg1_cycles = 291\n'):
            self.assertNotEqual(performance_result(self.run, log, 'example', 1024)['status'], 'PASS')

    def test_failed_process_cannot_pass_via_marker(self):
        for run in ({**self.run, 'returncode': 1}, {**self.run, 'timed_out': True}, {**self.run, 'interrupted': True}):
            self.assertNotEqual(performance_result(run, self.log, 'example', 1024)['status'], 'PASS')
        self.assertNotEqual(performance_result(self.run, self.log + '*** FAILED ***', 'example', 1024)['status'], 'PASS')

    def test_requires_nonempty_golden_checks(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'golden.json'
            path.write_text(json.dumps({'dram_preloads': [], 'dram_checks': []}))
            with self.assertRaises(ValueError):
                fixture_info(path)
            path.write_text(json.dumps({'dram_preloads': [], 'dram_checks': [{'word_offset': 0, 'expected': '0x1'}]}))
            self.assertEqual(fixture_info(path)['expected_check_words'], 8)

    def test_bank_capture_preserves_timing_and_adds_actual_port_requests(self):
        self.assertEqual(capture_signal_map(selection, banks=False), selection.PERF_SIGNALS)
        signals = capture_signal_map(selection, banks=True)
        for field, signal in selection.PERF_SIGNALS.items():
            self.assertEqual(signals[field], signal)
        for port in (0, 1):
            for field in ('valid', 'mreg', 'row'):
                self.assertIn(f'bank_read.p{port}.{field}', signals)

    def test_bank_capture_rejects_lost_timing_signals(self):
        broken = dict(selection.BANK_SIGNALS)
        del broken[next(iter(selection.PERF_SIGNALS))]
        with self.assertRaisesRegex(ValueError, 'complete performance signal map'):
            capture_signal_map(types.SimpleNamespace(PERF_SIGNALS=selection.PERF_SIGNALS,
                                                    BANK_SIGNALS=broken), banks=True)

    def test_hardware_bank_assertion_cannot_be_functional_pass(self):
        log = self.log + 'Assertion failed: MregFile bank conflict: multiple read ports targeting physical bank 0 (m0 or m32)\n'
        self.assertEqual(performance_result(self.run, log, 'example', 1024)['status'], 'CHECK_FAILED')


if __name__ == '__main__':
    unittest.main()

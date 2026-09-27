#!/usr/bin/env python3
"""Test edge sampling independently of VCD change order and response latency."""

import io
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).absolute().parents[1]))
from rtlgraph_mxu1_vcd import (BANK_SIGNALS, PERF_SIGNALS, SIGNALS, convert, edge_samples, read_header,
                                sample_record, value_changes)


SMALL = {"clock": ("dut.clk", 1), "valid": ("dut.valid", 1)}
HEADER = """$timescale 1 ps $end
$scope module dut $end
$var wire 1 ! clk $end
$var wire 1 \" valid $end
$upscope $end
$enddefinitions $end
"""


class VcdTests(unittest.TestCase):
    def samples(self, changes):
        stream = io.StringIO(HEADER + changes)
        selected, timescale = read_header(stream, SMALL)
        self.assertEqual(timescale, "1ps")
        return list(edge_samples(stream, selected, SMALL))

    def test_samples_left_limit_for_both_same_timestamp_record_orders(self):
        for update in ('1!\n1"\n', '1"\n1!\n'):
            samples = self.samples('#0\n$dumpvars\n0!\n0"\n$end\n#10\n' + update
                                   + '#15\n0!\n#20\n1!\n0"\n#25\n0!\n')
            self.assertEqual([s[2]["valid"] for s in samples], [0, 1])
            self.assertEqual([s[:2] for s in samples], [(0, 10), (1, 20)])

    def test_repeated_timestamp_is_one_batch(self):
        samples = self.samples('#0\n0!\n0"\n#10\n1"\n#10\n1!\n')
        self.assertEqual(len(samples), 1)
        self.assertEqual(samples[0][2]["valid"], 0)

    def test_preserves_unknown_payloads(self):
        samples = self.samples('#0\n0!\nx"\n#10\n1!\n')
        self.assertIsNone(samples[0][2]["valid"])

    def test_missing_or_wrong_width_signal_rejected(self):
        for header in (HEADER.replace('$var wire 1 " valid $end\n', ''),
                       HEADER.replace('$var wire 1 " valid', '$var wire 8 " valid')):
            with self.assertRaises(ValueError):
                read_header(io.StringIO(header), SMALL)

    def test_clock_unknown_or_nonuniform_rejected(self):
        for changes in ('#0\n0!\n#10\n1!\n#15\nx!\n',
                        '#0\n0!\n#10\n1!\n#15\n0!\n#20\n1!\n#25\n0!\n#31\n1!\n'):
            with self.assertRaises(ValueError):
                self.samples(changes)

    def test_conflicting_delta_changes_rejected(self):
        with self.assertRaisesRegex(ValueError, "Multiple"):
            list(value_changes(io.StringIO('#0\n0!\n1!\n'), {'!'}))

    def test_response_uses_actual_bank_tags_and_preserves_conflicts(self):
        flat = {key: 0 for key in SIGNALS}
        flat['mreg_req.valid'] = 1
        self.assertFalse(sample_record(0, 0, flat)['mreg_resp_valid'])
        flat['bank.9.valid'], flat['bank.9.port'] = 1, 2
        flat['bank.4.valid'], flat['bank.4.port'] = 1, 3
        first = sample_record(1, 10, flat)
        self.assertEqual(first['mreg_resp_banks'], [9])
        flat['bank.4.port'] = 2
        self.assertEqual(sample_record(2, 20, flat)['mreg_resp_count'], 2)

    def test_invalid_bank_tag_ignored_but_valid_unknown_tag_propagates(self):
        flat = {key: 0 for key in SIGNALS}
        flat['bank.9.port'] = None
        self.assertEqual(sample_record(0, 0, flat)['mreg_resp_count'], 0)
        flat['bank.9.valid'] = 1
        self.assertIsNone(sample_record(0, 0, flat)['mreg_resp_count'])

    def test_perf_map_preserves_original_and_emits_scalar_word(self):
        self.assertTrue(all(PERF_SIGNALS[key] == value for key, value in SIGNALS.items()))
        flat = {key: 0 for key in PERF_SIGNALS}
        flat.update({'scalar.fire': 1, 'scalar.pc': 42, 'scalar.instr': 0x16000077,
                     'accept.push_p1': 1, 'weight_write.row': 9})
        sample = sample_record(0, 1000, flat, perf=True)
        self.assertEqual(sample['scalar'], {'fire': 1, 'pc': 42, 'instr': 0x16000077})
        self.assertEqual(sample['accept']['push_p1'], 1)
        self.assertEqual(sample['weight_write']['row'], 9)
        self.assertNotIn('scalar', sample_record(0, 1000, flat))

    def test_failed_conversion_does_not_publish_partial_trace(self):
        with tempfile.TemporaryDirectory() as directory:
            source, output = Path(directory)/'input.vcd', Path(directory)/'trace.jsonl'
            source.write_text(HEADER + '#0\n0!\n0"\n')
            with patch('rtlgraph_mxu1_vcd.SIGNALS', SMALL):
                with self.assertRaisesRegex(ValueError, "No sequencer rising"):
                    convert(source, output)
            self.assertFalse(output.exists())

    def test_bank_mode_preserves_perf_and_samples_actual_requests_and_routes(self):
        self.assertTrue(all(BANK_SIGNALS[key] == value for key, value in PERF_SIGNALS.items()))
        self.assertEqual(len(BANK_SIGNALS) - len(PERF_SIGNALS), 6)
        flat = {key: 0 for key in BANK_SIGNALS}
        flat.update({'bank_read.p0.valid': 1, 'bank_read.p0.mreg': 0, 'bank_read.p0.row': 31,
                     'bank_read.p1.valid': 1, 'bank_read.p1.mreg': 32, 'bank_read.p1.row': 0,
                     'bank.9.valid': 1, 'bank.9.port': 3})
        sample = sample_record(0, 1000, flat, banks=True)
        self.assertEqual(sample['bank_reads']['p1'], {'valid': 1, 'mreg': 32, 'row': 0})
        self.assertEqual(sample['p1_resp_banks'], [9])
        self.assertEqual(sample['mreg_resp_banks'], [])
        self.assertIn('scalar', sample)
        self.assertNotIn('bank_reads', sample_record(0, 1000, flat, perf=True))
        flat['bank.9.port'] = None
        self.assertIsNone(sample_record(0, 1000, flat, banks=True)['p1_resp_banks'])


if __name__ == '__main__':
    unittest.main()

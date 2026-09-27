#!/usr/bin/env python3
"""Adversarial independent queue checks; no simulator or fixed SA LUT needed."""
import copy
import io
from pathlib import Path
import sys
import types
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import rtlgraph_mxu0_vcd as selection
from rtlgraph_mxu0_trace import check_samples, decode_mxu0
from rtlgraph_mxu1_vcd import PERF_SIGNALS, edge_samples
from rtlgraph_perf import capture_signal_map


def encoding(op, mreg, accsel, wslot=0):
    # Independent instruction-field fixture. Low funct7 bit selects MXU0.
    vd, vs1, vs2 = (mreg, 0, accsel) if op in (3, 4) else (accsel, mreg, wslot)
    return (op * 2 << 25) | (vs2 << 19) | (vs1 << 13) | (vd << 7) | 0x77


def witness(first_write=63, accumulating=False, bf16=False):
    pop, compute = 2, 3
    accsel, op = (1, 6) if accumulating else (0, 5)
    samples = [{key: 0 for key in selection.SIGNALS} for _ in range(compute + first_write + 34)]
    for cycle, operation, mreg, acc, slot, route, pc in (
        (pop, 4 if bf16 else 3, 8, 0, 0, 'pop_bf16' if bf16 else 'pop_fp8', 0),
        (compute, op, 2, accsel, 1, 'compute', 1)):
        samples[cycle].update({'scalar.fire': 1, 'scalar.instr': encoding(operation, mreg, acc, slot), 'scalar.pc': pc,
                              'cmd.valid': 1, 'cmd.op': operation, 'cmd.mreg': mreg, 'cmd.accsel': acc,
                              'cmd.wslot': slot, 'accept.' + route: 1})
    for row in range(32):
        samples[pop + row].update({'acc_store.valid': 1, 'acc_store.accsel': 0, 'acc_store.row': row})
        for port in range(2 if bf16 else 1):
            samples[pop + row + 1].update({f'mreg_write{port}.valid': 1,
                                         f'mreg_write{port}.mreg': 8 + port, f'mreg_write{port}.row': row})
        samples[compute + row].update({'mreg_read.valid': 1, 'mreg_read.mreg': 2, 'mreg_read.row': row})
        samples[compute + row + 1]['compute.valid'] = 1
        if accumulating:
            samples[compute + row].update({'acc_read.valid': 1, 'acc_read.accsel': accsel, 'acc_read.row': row})
        samples[compute + first_write + row].update({'acc_write.valid': 1, 'core_out.valid': 1,
            'acc_write.accsel': accsel, 'acc_write.row': row, 'retire': int(row == 31)})
    return samples


def check(samples):
    return check_samples((cycle, cycle * 2000, sample) for cycle, sample in enumerate(samples))


class Mxu0TraceTests(unittest.TestCase):
    def test_observes_same_accumulator_pop_overwrite_overlap(self):
        result = check(witness())
        self.assertEqual(result['status'], 'mxu0_observed_row_ownership_passed')
        self.assertEqual(result['computes'][0]['writes']['ages'], list(range(63, 95)))
        self.assertEqual(result['computes'][0]['reads']['ages'], [])
        overlap, = result['pop_overwrite_overlaps']
        self.assertEqual(overlap['pop_to_compute_issue_gap'], 1)
        self.assertEqual(len(overlap['cycles']), 31)
        self.assertEqual(overlap['pop_rows'], list(range(1, 32)))

    def test_write_age_is_measured_not_hardcoded(self):
        for first in (47, 63, 85):
            with self.subTest(first=first):
                self.assertEqual(check(witness(first))['computes'][0]['writes']['first_age'], first)

    def test_accumulation_on_other_accumulator_and_bf16_pop(self):
        result = check(witness(accumulating=True, bf16=True))
        self.assertEqual(result['computes'][0]['reads']['ages'], list(range(32)))
        self.assertEqual(result['pop_overwrite_overlaps'], [])

    def test_wrong_scalar_metadata_or_acceptance_rejected(self):
        for mutation, diagnostic in (('word', 'operands'), ('accept', 'rejected'), ('route', 'route')):
            samples = witness()
            if mutation == 'word': samples[3]['scalar.instr'] ^= 1 << 7
            if mutation == 'accept': samples[3]['accept.compute'] = 0
            if mutation == 'route':
                samples[3]['accept.compute'] = 0
                samples[3]['accept.push_p0'] = 1
            with self.subTest(mutation=mutation), self.assertRaisesRegex(ValueError, diagnostic):
                check(samples)

    def test_overwrite_accumulator_read_rejected(self):
        samples = witness()
        samples[8]['acc_read.valid'] = 1
        with self.assertRaisesRegex(ValueError, 'read ownership'):
            check(samples)

    def test_wrong_read_or_result_rows_rejected(self):
        for cycle, key, diagnostic in ((8, 'mreg_read.row', 'request'), (8, 'acc_store.row', 'pop read'),
                                      (9, 'mreg_write0.mreg', 'Pop destination'), (70, 'acc_write.row', 'result')):
            samples = witness()
            samples[cycle][key] += 1
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, diagnostic):
                check(samples)

    def test_feed_result_drop_retirement_and_pop_write_alignment(self):
        for cycle, key, diagnostic in ((8, 'compute.valid', 'feed'), (70, 'core_out.valid', 'dropped'),
                                      (70, 'retire', 'retirement'), (9, 'mreg_write0.valid', 'prior observed')):
            samples = witness()
            samples[cycle][key] ^= 1
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, diagnostic):
                check(samples)

    def test_unknown_missing_and_truncated_samples_rejected(self):
        for mutation in ('unknown', 'missing', 'truncate', 'reset'):
            samples = witness()
            if mutation == 'unknown': samples[8]['acc_store.row'] = None
            if mutation == 'missing': del samples[8]['acc_store.row']
            if mutation == 'truncate': samples = samples[:20]
            if mutation == 'reset': samples[8]['reset'] = 1
            with self.subTest(mutation=mutation), self.assertRaisesRegex(ValueError, 'Unknown|Truncated|Reset'):
                check(samples)

    def test_unknown_inactive_payload_is_allowed(self):
        samples = witness()
        for sample in samples:
            sample['acc_read.row'] = None
        check(samples)

    def test_engine_selector_and_invalid_opcode(self):
        word = encoding(5, 2, 0, 1)
        self.assertEqual(decode_mxu0(word)['op'], 'Matmul')
        self.assertIsNone(decode_mxu0(word | (1 << 25)))
        with self.assertRaisesRegex(ValueError, 'unsupported issued'):
            decode_mxu0((14 << 25) | 0x77)

    def test_map_and_optional_selection_preserve_common_signals(self):
        self.assertEqual(len(selection.SIGNALS), 42)
        for key, signal in PERF_SIGNALS.items():
            if key.startswith(('scalar.', 'csr.')):
                self.assertEqual(selection.SIGNALS[key], signal)
        self.assertIs(capture_signal_map(selection, banks=False, mxu0=True), selection.SIGNALS)
        with self.assertRaisesRegex(ValueError, 'mutually exclusive'):
            capture_signal_map(selection, banks=True, mxu0=True)
        old = types.SimpleNamespace(PERF_SIGNALS={'old': ('scope.old', 1)})
        self.assertIs(capture_signal_map(old, banks=False), old.PERF_SIGNALS)

    def test_pre_edge_sampling_ignores_same_timestamp_record_order(self):
        # The event changes at the rising timestamp and belongs to the next
        # settled cycle. Both encodings must observe its previous value.
        selected = {'clock': 'c', 'event': 'e'}
        signals = {'clock': ('scope.clock', 1), 'event': ('scope.event', 1)}
        for changes in ('1c\n1e', '1e\n1c'):
            data = '#0\n0c\n0e\n#10\n' + changes + '\n#20\n0c\n#30\n1c\n'
            result = list(edge_samples(io.StringIO(data), selected, signals))
            self.assertEqual([sample['event'] for _, _, sample in result], [0, 1])


if __name__ == '__main__':
    unittest.main()

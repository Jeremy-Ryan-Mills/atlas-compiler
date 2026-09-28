#!/usr/bin/env python3
"""Mixed-engine ownership and physical conflict checks without timing LUTs."""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from rtlgraph_dma_vcd import SIGNALS as DMA_SIGNALS
from rtlgraph_mixed_trace import check_samples, physical_accesses
from rtlgraph_mixed_vcd import SIGNALS, MXU_MAPS, engine_sample
from rtlgraph_mxu0_trace import Events as MxuEvents
from rtlgraph_perf import capture_signal_map
import rtlgraph_mixed_vcd as selection
from test_rtlgraph_dma_trace import capture as dma_capture
from test_rtlgraph_lsu_trace import capture as lsu_capture
from test_rtlgraph_mxu0_trace import encoding, witness as mxu_witness
from test_rtlgraph_vpu_trace import witness as vpu_witness


def blank():
    sample = {key: 0 for key in SIGNALS}
    sample['scalar.instr'] = 0x13
    return sample


def append_component(samples, component):
    for sample in component:
        result = blank()
        result.update(sample)
        samples.append(result)


def overlay_mxu(samples, engine, component, offset=0):
    while len(samples) < offset + len(component):
        samples.append(blank())
    for index, source in enumerate(component):
        sample = samples[offset + index]
        for key, result in source.items():
            if not key.startswith(('scalar.', 'csr.')):
                sample[f'mxu{engine}.' + key] = result
        if source['scalar.fire']:
            sample.update({key: result for key, result in source.items() if key.startswith('scalar.')})
            sample['scalar.instr'] |= engine << 25
            sample['scalar.valid'], sample['scalar.stall'] = 1, 0
        for port in (0, 1):
            for field in ('valid', 'mreg', 'row'):
                sample[f'mxu{engine}.port.write{port}.' + field] = source[f'mreg_write{port}.' + field]
                if port == 0:
                    sample[f'mxu{engine}.port.read0.' + field] = source['mreg_read.' + field]


def witness(first_write=3, both=False):
    # Delayed external memory keeps DMA live while independent MXU rows stream.
    samples = []
    append_component(samples, dma_capture(gap=120))
    overlay_mxu(samples, 1, mxu_witness(first_write=first_write, bf16=True))
    if both:
        offset = len(samples)
        overlay_mxu(samples, 0, mxu_witness(first_write=63), offset)
    append_component(samples, lsu_capture())
    append_component(samples, vpu_witness())
    return samples


def check(samples):
    return check_samples((cycle, cycle * 2000, sample) for cycle, sample in enumerate(samples))


class MixedTraceTests(unittest.TestCase):
    def test_mixed_map_preserves_existing_maps_and_selection(self):
        self.assertTrue(all(SIGNALS[key] == record for key, record in DMA_SIGNALS.items()))
        for engine in (0, 1):
            sample = blank()
            self.assertEqual(set(engine_sample(sample, engine)), set(MXU_MAPS[engine]))
            for key, record in MXU_MAPS[engine].items():
                shared = key if key.startswith(('scalar.', 'csr.')) else f'mxu{engine}.' + key
                self.assertEqual(SIGNALS[shared], record)
        self.assertIs(capture_signal_map(selection, banks=False, mixed=True), SIGNALS)
        for name in ('banks', 'mxu0', 'vpu', 'lsu', 'dma'):
            arguments = dict(banks=False, mixed=True)
            arguments[name] = True
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, 'mutually exclusive'):
                capture_signal_map(selection, **arguments)

    def test_real_queues_check_both_engines_and_bf16_pop(self):
        result = check(witness(both=True))
        self.assertEqual(result['status'], 'mixed_dma_lsu_vpu_mxu_observed_ownership_passed')
        self.assertEqual(result['command_count'], 1)
        self.assertEqual(result['lsu_vpu']['command_count'], 1)
        self.assertEqual(result['lsu_vpu']['vpu']['command_count'], 1)
        self.assertEqual(result['mxu']['mxu1']['scalar_decoded_command_counts'], {'PopAccBF16': 1, 'Matmul': 1})
        self.assertEqual(result['mxu']['mxu0']['scalar_decoded_command_counts'], {'PopAccFP8': 1, 'Matmul': 1})
        self.assertEqual(result['mxu']['mxu1']['computes'][0]['writes']['first_age'], 3)
        self.assertEqual(result['mxu']['mxu0']['computes'][0]['writes']['first_age'], 63)
        self.assertEqual(result['dma_busy_with_mxu_row_edges'], {'mxu0': 0, 'mxu1': 32})
        self.assertEqual(result['dma_busy_with_mxu_compute_edges'], {'mxu0': 0, 'mxu1': 35})

    def test_write_ages_are_measured_and_empty_engine_is_explicit(self):
        for age in (3, 7, 47):
            result = check(witness(first_write=age))
            self.assertEqual(result['mxu']['mxu1']['computes'][0]['writes']['first_age'], age)
            self.assertEqual(result['mxu']['mxu0']['status'], 'mxu0_no_issued_commands')
            self.assertEqual(result['mxu']['mxu0']['command_count'], 0)

    def test_missing_engine_bit_acceptance_or_command_is_rejected(self):
        for key, change, diagnostic in (('scalar.instr', 1 << 25, 'unmatched'),
                                        ('mxu1.accept.compute', 1, 'rejected'),
                                        ('mxu1.cmd.valid', 1, 'unmatched')):
            samples = witness()
            samples[3][key] ^= change
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, diagnostic):
                check(samples)

    def test_wrong_bf16_destination_or_result_ownership_is_rejected(self):
        for key, diagnostic in (('mxu1.mreg_write1.mreg', 'Pop destination'),
                                ('mxu1.acc_write.row', 'result destination')):
            samples = witness()
            samples[9][key] += 1
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, diagnostic):
                check(samples)

    def test_empty_engine_cannot_hide_unowned_reads_or_writes(self):
        for direction in ('read', 'write'):
            samples = witness()
            samples[0][f'mxu0.port.{direction}1.valid'] = 1
            with self.subTest(direction=direction), self.assertRaisesRegex(ValueError, 'no accepted command'):
                check(samples)

    def test_push_only_monitor_scope_is_explicit_and_legacy_finish_is_unchanged(self):
        events = MxuEvents(engine=1)
        sample = engine_sample(blank(), 1)
        sample.update({'scalar.fire': 1, 'scalar.instr': encoding(2, 2, 1) | 1 << 25,
                       'cmd.valid': 1, 'cmd.op': 2, 'cmd.mreg': 2, 'cmd.accsel': 1, 'cmd.wslot': 1,
                       'accept.push_bf16': 1})
        events.step(0, sample)
        with self.assertRaisesRegex(ValueError, 'both compute and pop'):
            events.finish()
        result = events.finish(require_compute_pop=False)
        self.assertEqual(result['command_count'], 1)
        self.assertEqual(result['computes'], [])
        self.assertTrue(any('push commands' in text for text in result['limitations']))

    def test_cross_engine_physical_read_and_write_bank_conflicts(self):
        for direction in ('read', 'write'):
            for a, b in ((f'lsu.mreg.{direction}', f'mxu1.port.{direction}0'),
                         (f'physical.{direction}0', f'mxu0.port.{direction}1'),
                         (f'mxu0.port.{direction}0', f'mxu1.port.{direction}1')):
                sample = blank()
                for port, register in ((a, 2), (b, 34)):
                    sample.update({port + '.valid': 1, port + '.mreg': register, port + '.row': 0})
                with self.subTest(a=a, b=b), self.assertRaisesRegex(ValueError, 'bank conflict'):
                    physical_accesses(sample)

    def test_cross_engine_same_row_visibility_and_different_rows(self):
        sample = blank()
        sample.update({'mxu1.port.read0.valid': 1, 'mxu1.port.read0.mreg': 34, 'mxu1.port.read0.row': 7,
                       'physical.write1.valid': 1, 'physical.write1.mreg': 34, 'physical.write1.row': 7})
        with self.assertRaisesRegex(ValueError, 'visibility contract'):
            physical_accesses(sample)
        sample['physical.write1.row'] = 8
        accesses = physical_accesses(sample)
        self.assertEqual(accesses['read'][0]['row'], 39)
        self.assertEqual(accesses['write'][0]['row'], 40)

    def test_physical_unknown_payload_is_only_allowed_when_inactive(self):
        sample = blank()
        sample['mxu1.port.read1.row'] = None
        physical_accesses(sample)
        sample['mxu1.port.read1.valid'] = 1
        with self.assertRaisesRegex(ValueError, 'Unknown'):
            physical_accesses(sample)

    def test_pending_compute_and_pop_cannot_disappear(self):
        samples = witness()
        with self.assertRaisesRegex(ValueError, 'pending|unfinished'):
            check(samples[:20])
        samples[8]['mxu1.reset'] = 1
        with self.assertRaisesRegex(ValueError, 'reset|Reset'):
            check(samples)

    def test_private_reset_cannot_hide_every_issued_engine_command(self):
        samples = witness()
        for sample in samples:
            sample['mxu1.reset'] = 1
        with self.assertRaisesRegex(ValueError, 'MXU1 reset.*common capture'):
            check(samples)

    def test_clock_domain_mismatch_and_unknown_domain_flags_fail_closed(self):
        samples = witness()
        samples[3]['mxu1.clock'] = 1
        with self.assertRaisesRegex(ValueError, 'MXU1 clock.*common capture'):
            check(samples)
        for key in ('clock', 'reset', 'mxu0.clock', 'mxu1.reset'):
            samples = witness()
            samples[0][key] = None
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, 'Unknown'):
                check(samples)


if __name__ == '__main__':
    unittest.main()

#!/usr/bin/env python3
"""Adversarial checks for LSU transaction ownership and overlap monitoring."""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from rtlgraph_lsu_trace import Events, decode_lsu
from rtlgraph_lsu_vcd import SIGNALS


def capture(kind='load', request_age=1, write_age=3):
    samples = []
    for cycle in range(max(request_age + 34, write_age + 34)):
        sample = {key: 0 for key in SIGNALS}
        sample['scalar.instr'] = 0x13
        if cycle == 0:
            sample.update({'scalar.fire': 1, 'scalar.instr': 0x07 | (2 << 7) | ((kind == 'store') << 13),
                           'lsu.cmd.valid': 1, 'lsu.cmd.op': 1 if kind == 'load' else 2, 'lsu.cmd.mreg': 2})
        busy = 1 <= cycle < write_age + 32
        sample[f'lsu.{kind}.busy'] = int(busy)
        direction = 'write' if kind == 'load' else 'read'
        sample[f'lsu.active.{direction}.valid'] = int(busy)
        sample[f'lsu.active.{direction}.mreg'] = 2
        sample[f'scalar.mreg_{direction}_busy'] = (1 << 2) if busy else 0
        if request_age <= cycle < request_age + 32:
            port = 'lsu.vmem.load' if kind == 'load' else 'lsu.mreg.read'
            sample[port + '.valid'] = 1
            sample[port + '.row'] = cycle - request_age
            if kind == 'store': sample[port + '.mreg'] = 2
        if request_age + 1 <= cycle < request_age + 33:
            sample['lsu.vmem.response' if kind == 'load' else 'lsu.mreg.response'] = 1
        if write_age <= cycle < write_age + 32:
            port = 'lsu.mreg.write' if kind == 'load' else 'lsu.vmem.store'
            sample[port + '.valid'] = 1
            sample[port + '.row'] = cycle - write_age
            if kind == 'load': sample[port + '.mreg'] = 2
        samples.append(sample)
    return samples


def check(samples):
    events = Events()
    for cycle, sample in enumerate(samples): events.step(cycle, sample)
    return events.finish()


class LsuTraceTests(unittest.TestCase):
    def test_decode(self):
        self.assertEqual(decode_lsu(0x07 | 63 << 7 | 1 << 13), {'op': 2, 'kind': 'store', 'mreg': 63})
        self.assertIsNone(decode_lsu(0x13))
        with self.assertRaisesRegex(ValueError, 'encoding'): decode_lsu(0x07 | 2 << 13)

    def test_complete_load_store_and_measured_age(self):
        for kind in ('load', 'store'):
            result = check(capture(kind))
            self.assertEqual(result['commands'][0]['reads']['ages'], list(range(1, 33)))
            self.assertEqual(result['commands'][0]['writes']['ages'], list(range(3, 35)))
            self.assertEqual(result['commands'][0]['first_not_busy_age'], 35)
        # The monitor measures delayed streams; it does not assert model ages.
        result = check(capture('load', 2, 4))
        self.assertEqual(result['commands'][0]['writes']['ages'], list(range(4, 36)))

    def test_bad_scalar_binding(self):
        samples = capture(); samples[0]['lsu.cmd.mreg'] = 3
        with self.assertRaisesRegex(ValueError, 'opcode or register'): check(samples)

    def test_row_corruption(self):
        samples = capture(); samples[9]['lsu.mreg.write.row'] = 7
        with self.assertRaisesRegex(ValueError, 'destination row'): check(samples)

    def test_response_corruption(self):
        samples = capture(); samples[9]['lsu.vmem.response'] = 0
        with self.assertRaisesRegex(ValueError, 'response does not match'): check(samples)

    def test_truncated(self):
        with self.assertRaisesRegex(ValueError, 'truncated'): check(capture()[:20])

    def test_early_release(self):
        samples = capture(); samples[20]['lsu.load.busy'] = samples[20]['lsu.active.write.valid'] = 0
        with self.assertRaisesRegex(ValueError, 'released before'): check(samples)

    def test_physical_alias_conflict(self):
        samples = capture()
        samples[9].update({'physical.write0.valid': 1, 'physical.write0.mreg': 34, 'physical.write0.row': 0})
        with self.assertRaisesRegex(ValueError, 'physical-bank write'): check(samples)

    def test_independent_bank_overlap(self):
        samples = capture()
        samples[9].update({'physical.write0.valid': 1, 'physical.write0.mreg': 3, 'physical.write0.row': 0})
        self.assertEqual(check(samples)['lsu_active_with_vpu_row_edges'], 1)

    def test_vmem_conflict(self):
        samples = capture(); samples[9]['lsu.vmem.scalar_read.valid'] = 1
        with self.assertRaisesRegex(ValueError, 'VMEM bank'): check(samples)

    def test_dma_request_is_not_grant(self):
        samples = capture(); samples[9]['vmem.dma_read.valid'] = 1
        self.assertEqual(check(samples)['dma_request_not_granted_edges'], 1)
        samples[9]['vmem.dma_read.grant'] = 1
        with self.assertRaisesRegex(ValueError, 'Granted DMA'): check(samples)

    def test_command_busy_hazard(self):
        samples = capture(); samples[0]['scalar.mreg_write_busy'] = 1 << 2
        with self.assertRaisesRegex(ValueError, 'logical MREG writer'): check(samples)

    def test_vpu_read_during_load(self):
        samples = capture()
        samples[9].update({'scalar.fire': 1, 'scalar.instr': 0x46 << 25 | 2 << 13 | 4 << 7 | 0x57})
        with self.assertRaisesRegex(ValueError, 'VPU issue overlaps a logical MREG writer'): check(samples)

    def test_vpu_write_during_store(self):
        samples = capture('store')
        samples[9].update({'scalar.fire': 1, 'scalar.instr': 0x46 << 25 | 4 << 13 | 2 << 7 | 0x57})
        with self.assertRaisesRegex(ValueError, 'logical MREG reader/writer'): check(samples)

    def test_same_cycle_visibility_not_assumed(self):
        samples = capture()
        samples[9].update({'physical.read0.valid': 1, 'physical.read0.mreg': 2, 'physical.read0.row': 6})
        with self.assertRaisesRegex(ValueError, 'visibility contract'): check(samples)


if __name__ == '__main__': unittest.main()

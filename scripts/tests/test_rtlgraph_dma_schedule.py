#!/usr/bin/env python3
"""Adversarial checks for explicit DMA completion and launch-captured ranges."""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from rtlgraph_dma_schedule import (candidate, check_dma_ranges, emit, projection,
                                  timeline, transfer_key)
from rtlgraph_kernel import KernelError, tokens
from test_rtlgraph_attention import TokenAssembler


PREFIX = '''LI x5, 0
DMA.CONFIG x5, 0
LI x12, 2048
LI x6, 0x20000000
LI x9, 0x20000400
LI x1, 0x90000000
DMA.LOAD x6, x1, x12, 0
LI x1, 0x90000800
DMA.LOAD x9, x1, x12, 1
DMA.WAIT 0
DMA.WAIT 1
CSRRS x20, 0xC00, x0
'''
BODY = '''VLOAD 0, x6, 0
DELAY 34
VLOAD 1, x6, 8
DELAY 34
VSQUARE.BF16 2, 0
VLOAD 4, x9, 0
DELAY 34
VLOAD 5, x9, 8
DELAY 34
LUI x7, 131073
LUI x8, 131074
ADDI x8, x8, 2048
VSTORE 8, x8, 0
DELAY 34
VSTORE 9, x8, 8
DELAY 39
VSTORE 6, x7, 0
DELAY 34
VSTORE 7, x7, 8
DELAY 34
VSTORE 2, x6, 0
DELAY 34
VSTORE 3, x6, 8
DELAY 34
'''
SUFFIX = '''CSRRS x21, 0xC00, x0
SUB x22, x21, x20
CSRRW x0, 0xC11, x22
LI x3, 0x90001000
DMA.STORE x3, x6, x12, 1
DMA.WAIT 1
LI x3, 0x90001800
DMA.STORE x3, x7, x12, 1
DMA.WAIT 1
LI x3, 0x90002000
DMA.STORE x3, x8, x12, 1
DMA.WAIT 1
LI x30, 1
CSRW x30, 0xC10
ECALL
'''
SOURCE = PREFIX + BODY + SUFFIX


class DmaScheduleTests(unittest.TestCase):
    def check(self, source):
        return check_dma_ranges(source, TokenAssembler())

    def test_launch_captures_scalar_operands_and_signed_address_setup(self):
        report = self.check(SOURCE)
        self.assertEqual([(t['vmem_line'], t['dram_address']) for t in report['transfers']],
                         [(0, 0x90000000), (128, 0x90000800), (0, 0x90001000),
                          (512, 0x90001800), (768, 0x90002000)])
        self.assertTrue(all(t['line_count'] == 64 for t in report['transfers']))
        self.assertEqual(len(report['waits']), 5)

    def test_input_wait_cannot_move_after_its_consumer(self):
        changed = SOURCE.replace('DMA.WAIT 0\n', '', 1).replace('VLOAD 0, x6, 0', 'VLOAD 0, x6, 0\nDMA.WAIT 0')
        with self.assertRaisesRegex(KernelError, 'unfinished DMA'):
            self.check(changed)

    def test_input_range_changes_cannot_masquerade_as_ready(self):
        for changed in (SOURCE.replace('VLOAD 1, x6, 8', 'VLOAD 1, x6, 16'),
                        SOURCE.replace('LI x9, 0x20000400', 'LI x9, 0x20000000')):
            with self.subTest(source=changed), self.assertRaises(KernelError):
                self.check(changed)

    def test_channels_cannot_be_reused_before_wait(self):
        changed = SOURCE.replace('DMA.LOAD x9, x1, x12, 1', 'DMA.LOAD x9, x1, x12, 0')
        with self.assertRaisesRegex(KernelError, 'channel reused'):
            self.check(changed)

    def test_output_requires_every_row_and_final_write_before_launch(self):
        for changed in (SOURCE.replace('VSTORE 3, x6, 8', 'NOP'),
                        SOURCE.replace('VSTORE 3, x6, 8\nDELAY 34', 'VSTORE 3, x6, 8')):
            with self.subTest(source=changed), self.assertRaises(KernelError):
                self.check(changed)

    def test_pending_output_cannot_be_overwritten(self):
        changed = SOURCE.replace('DMA.STORE x3, x6, x12, 1',
                                 'DMA.STORE x3, x6, x12, 1\nVSTORE 2, x6, 0')
        with self.assertRaisesRegex(KernelError, 'unfinished DMA'):
            self.check(changed)

    def test_bad_config_size_unknown_address_and_unmatched_wait_rejected(self):
        for changed in (SOURCE.replace('LI x5, 0', 'LI x5, 1'),
                        SOURCE.replace('LI x12, 2048', 'LI x12, 1024'),
                        SOURCE.replace('LI x6, 0x20000000\n', ''),
                        SOURCE.replace('DMA.WAIT 0', 'DMA.WAIT 2'),
                        SOURCE.replace('CSRW x30, 0xC10', 'DMA.WAIT 2\nCSRW x30, 0xC10')):
            with self.subTest(source=changed), self.assertRaises(KernelError):
                self.check(changed)

    def test_candidates_preserve_transfer_semantics_and_explicit_waits(self):
        original = sorted(map(transfer_key, self.check(SOURCE)['transfers']))
        for move in (False, True):
            text, report = candidate(SOURCE, TokenAssembler(), move)
            self.assertEqual(sorted(map(transfer_key, report['transfers'])), original)
            self.assertEqual([t['vmem_line'] for t in report['transfers'][2:]], [768, 512, 0])
            self.assertEqual(sum(bool(tokens(line)) and tokens(line)[0] == 'DMA.WAIT' for line in text.splitlines()), 5)
            self.assertEqual(len(report['waits']), 5)
            active_waits = [w for w in report['waits'] if w['prior_local_last_age']]
            self.assertEqual(len(active_waits), int(move))
            if move:
                self.assertEqual(active_waits[0]['index'], 1)
            self.assertLess(text.rindex('DMA.WAIT'), text.index('CSRW x30'))

    def test_changed_output_group_and_occupied_launch_gap_rejected(self):
        for changed in (SOURCE.replace('DMA.STORE x3, x8, x12, 1', 'DMA.STORE x3, x9, x12, 1'),
                        SOURCE.replace('VSTORE 9, x8, 8\nDELAY 39', 'VSTORE 9, x8, 8\nDELAY 33\nVSQUARE.BF16 2, 0\nDELAY 4')):
            with self.subTest(source=changed), self.assertRaises(KernelError):
                candidate(changed, TokenAssembler(), False)

    def test_projection_removes_native_dma_semantics_and_varies_only_second_wait(self):
        text, _ = candidate(SOURCE, TokenAssembler(), True)
        a, b = projection(text), projection(text, 65)
        self.assertNotIn('dma.', a.lower())
        self.assertEqual(len(a.splitlines()), len(b.splitlines()))
        differences = [(x, y) for x, y in zip(a.splitlines(), b.splitlines()) if x != y]
        self.assertEqual(differences, [('nop', 'delay 65')])
        self.assertIn('vload m1, 8(x6)', a)
        self.assertIn('vsquare.bf16 m2, m0', a)

    def test_timeline_roundtrip_preserves_issue_cycles_and_final_drain(self):
        before, end = timeline(BODY, TokenAssembler())
        after, final = timeline(emit(before, end), TokenAssembler())
        self.assertEqual((after, final), (before, end))
        with self.assertRaises(KernelError):
            emit([(0, 'NOP', 1), (0, 'NOP', 1)], 2)


if __name__ == '__main__':
    unittest.main()

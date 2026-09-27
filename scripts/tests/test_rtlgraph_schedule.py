#!/usr/bin/env python3
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from rtlgraph_kernel import KernelError, load_assembler, assemble
from rtlgraph_schedule import translate, split, splice, compute_slice, issue_summary
from test_rtlgraph_attention import TokenAssembler


class ScheduleAdapterTests(unittest.TestCase):
    MEMORY_BODY = ('# inputs stay timed\r\nVLOAD 0, x6, 0\r\nDELAY 33\r\n'
                   'VLOAD 1, x6, 8\r\nDELAY 33\r\n'
                   'VSQUARE.BF16 2, 0\r\nVTANH 2, 2\r\nDELAY 64\r\n'
                   'LI x7, 0x20001800 # writeback address\r\n'
                   'VSTORE 2, x7, 0\r\nDELAY 33\r\n'
                   'VSTORE 3, x7, 8\r\nDELAY 33\r\n')

    def test_mxu0_order_and_engine_are_preserved(self):
        self.assertEqual(translate('VMATPOP.FP8.MXU0 30, 7, 1', to_compiler=True),
                         'vmatpop.fp8.acc.mxu0 m30, acc1, e7\n')
        source = 'VMATPUSH.W.MXU0 1, 2\nVMATMUL.ACC.MXU0 0, 4, 1\nVMATPOP.BF16.MXU0 6, 0\n'
        self.assertEqual(translate(translate(source, to_compiler=True), to_compiler=False), source)

    def test_immediate_and_unary_mapping(self):
        source = 'VLI.ALL 12, 12288\nVSQUARE.BF16 2, 0\nVSQRT 2, 2\n'
        self.assertEqual(translate(translate(source, to_compiler=True), to_compiler=False), source)
        self.assertEqual(translate('VLI.ALL 12, 0x3000', to_compiler=True), 'vli.all m12, 12288\n')
        for text in ('VLI.ALL 12, 65536', 'VLI.ALL 12, -1', 'VLI.ALL 64, 1', 'VSQRT 2', 'VSQRT 2, x0'):
            with self.subTest(text=text), self.assertRaises(KernelError):
                translate(text, to_compiler=True)

    def test_both_counter_spellings_and_no_control_flow(self):
        for start, end in [('CSRR x20, 0xC00', 'CSRR x21, 3072'),
                           ('CSRRS x20, 0xC00, x0', 'CSRRS x21, 0xC00, x0')]:
            source = f'{start}\nVMATMUL.MXU0 0, 2, 0\n{end}\nECALL\n'
            sliced = split(source)
            compiled = translate(sliced.body, to_compiler=True) + 'delay 94\necall\n'
            output, body = splice(source, compiled, TokenAssembler())
            self.assertTrue(output.endswith('DELAY 94\n' + end + '\nECALL\n'))
            with self.assertRaises(KernelError):
                split(source + 'BEQ x1, x2, done\n')
            with self.assertRaises(KernelError):
                splice(source, compiled.replace('m2', 'm3'), TokenAssembler())

    def test_entry_state_dependent_operations_fail_closed(self):
        for text in ('VLOAD 0, x6, 0', 'LI x6, 0x20000000', 'VSTORE 2, x6, 0', 'body:'):
            with self.subTest(text=text), self.assertRaises(KernelError):
                translate(text, to_compiler=True)

    def test_memory_wrapper_requires_explicit_selection_and_preserves_bytes(self):
        source = 'CSRRS x20, 0xC00, x0\r\n' + self.MEMORY_BODY + 'CSRRS x21, 0xC00, x0\r\nECALL\r\n'
        with self.assertRaises(KernelError):
            compute_slice(self.MEMORY_BODY)
        compute = compute_slice(self.MEMORY_BODY, preserve_memory_wrapper=True)
        scheduled = 'vsquare.bf16 m2, m0\ndelay 64\nvtanh.bf16 m2, m2\ndelay 64\necall\n'
        result, body = splice(source, scheduled, TokenAssembler(), preserve_memory_wrapper=True)
        self.assertTrue(result.startswith(split(source).prefix + compute.prefix))
        self.assertTrue(result.endswith(compute.suffix + split(source).end_marker + split(source).suffix))
        self.assertEqual(body, compute.prefix + translate(scheduled.rsplit('ecall', 1)[0], to_compiler=False) + compute.suffix)
        self.assertEqual(result.count('VLOAD'), 2)
        self.assertEqual(result.count('VSTORE'), 2)
        with self.assertRaisesRegex(KernelError, 'multiset'):
            splice(source, scheduled.replace('m2, m0', 'm2, m4'), TokenAssembler(), preserve_memory_wrapper=True)

    def test_memory_wrapper_rejects_missing_waits_and_interleaved_transfers(self):
        malformed = [
            self.MEMORY_BODY.replace('DELAY 33', 'DELAY 32', 1),
            self.MEMORY_BODY.replace('DELAY 33\r\n', '', 1),
            self.MEMORY_BODY.replace('DELAY 33\r\n', 'NOP\r\nDELAY 33\r\n', 1),
            self.MEMORY_BODY.replace('VTANH 2, 2', 'VLOAD 4, x6, 0\r\nDELAY 33\r\nVTANH 2, 2'),
            self.MEMORY_BODY.replace('LI x7, 0x20001800', 'ADDI x7, x6, 0'),
            self.MEMORY_BODY.replace('VSTORE 3, x7, 8\r\nDELAY 33', 'VSTORE 3, x7, 8'),
            self.MEMORY_BODY.replace('VLOAD 0', 'VLOAD 64'),
            'VSQUARE.BF16 2, 0\nDELAY 64\n',
        ]
        for body in malformed:
            with self.subTest(body=body), self.assertRaises(KernelError):
                compute_slice(body, preserve_memory_wrapper=True)

    def test_memory_issue_summary_counts_scalar_pseudo_expansion(self):
        class ExpandingAssembler(TokenAssembler):
            @staticmethod
            def assemble(source):
                if source.startswith('LI '):
                    return [1, 2]
                return TokenAssembler.assemble(source)

        summary = issue_summary('LI x7, 0x20001800\nVSTORE 2, x7, 0\nDELAY 33\n',
                                ExpandingAssembler(), preserve_memory_wrapper=True)
        self.assertEqual(summary['operations'][1]['body_issue_cycle'], 2)
        self.assertEqual(summary['cycle_csr_dispatch_window'], 38)


if __name__ == '__main__':
    unittest.main()

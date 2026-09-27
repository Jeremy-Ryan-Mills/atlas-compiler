#!/usr/bin/env python3
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from rtlgraph_kernel import KernelError, load_assembler, assemble
from rtlgraph_schedule import translate, split, splice
from test_rtlgraph_attention import TokenAssembler


class ScheduleAdapterTests(unittest.TestCase):
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


if __name__ == '__main__':
    unittest.main()

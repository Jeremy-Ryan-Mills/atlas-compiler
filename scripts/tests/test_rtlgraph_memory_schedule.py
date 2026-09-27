#!/usr/bin/env python3
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from rtlgraph_kernel import KernelError
from rtlgraph_memory_schedule import entry_values, splice, translate
from test_rtlgraph_attention import TokenAssembler


class MemoryScheduleTests(unittest.TestCase):
    PREFIX = ('LI x6, 536870912\nLI x1, 2415919104\nLI x12, 2048\n'
              'DMA.LOAD x6, x1, x12, 0\nDMA.WAIT 0\nCSRRS x20, 0xC00, x0\n')
    BODY = ('VLOAD 0, x6, 0\nDELAY 33\nVLOAD 1, x6, 8\nDELAY 33\n'
            'VSQUARE.BF16 2, 0\nDELAY 64\nLI x7, 536875008\n'
            'VSTORE 2, x7, 0\nDELAY 33\nVSTORE 3, x7, 8\nDELAY 33\n')

    def test_requires_known_entry_and_completed_dma(self):
        self.assertEqual(entry_values(self.PREFIX, self.BODY), {6: 536870912})
        for prefix in (self.PREFIX.replace('DMA.WAIT 0\n', ''),
                       self.PREFIX.replace('LI x6, 536870912\n', ''),
                       self.PREFIX.replace('LI x6, 536870912', 'LW x6, x2, 0'),
                       self.PREFIX.replace('CSRRS x20', 'CSRRS x6'),
                       self.PREFIX.replace('DMA.WAIT 0', 'DMA.LOAD x6, x1, x12, 0\nDMA.WAIT 0')):
            with self.subTest(prefix=prefix), self.assertRaises(KernelError):
                entry_values(prefix, self.BODY)

    def test_scalar_definitions_inside_window_do_not_become_entry_assumptions(self):
        self.assertEqual(entry_values('', 'LI x6, 536870912\nVLOAD 0, x6, 0\n'), {})
        with self.assertRaises(KernelError):
            entry_values('', 'VLOAD 0, x6, 0\nLI x6, 536870912\n')

    def test_memory_address_units_and_scalar_expansion_preserved(self):
        self.assertEqual(translate('VLOAD 1, x6, 8\nVSTORE 3, x7, 0', to_compiler=True),
                         'vload m1, 8(x6)\nvstore m3, 0(x7)\n')
        self.assertEqual(translate(translate(self.BODY, to_compiler=True), to_compiler=False), self.BODY)
        self.assertEqual(translate('lui x8, 131074\naddi x8, x8, 2048\naddi x0, x0, 0', to_compiler=False),
                         'LUI x8, 131074\nADDI x8, x8, 2048\nNOP\n')

    def test_rejects_control_flow_other_engines_and_unsupported_operands(self):
        for text in ('VMATMUL.MXU0 0, 2, 0', 'VLOAD 64, x6, 0', 'VSTORE 2, x32, 0',
                     'VLOAD 2, x6, 4096', 'DMA.WAIT 0', 'LI x0, 1', 'JAL x1, 0', 'label:'):
            with self.subTest(text=text), self.assertRaises(KernelError):
                translate(text, to_compiler=True)
        for text in ('vload m1, 8(x32)', 'vstore m1, -1(x6)', 'addi x0, x6, 0'):
            with self.subTest(text=text), self.assertRaises(KernelError):
                translate(text, to_compiler=False)

    def test_splice_preserves_outer_program_and_all_nonidle_operations(self):
        source = self.PREFIX + self.BODY + 'CSRRS x21, 0xC00, x0\nECALL\n'
        scheduled = translate(self.BODY, to_compiler=True) + 'nop\necall\n'
        result, body = splice(source, scheduled, TokenAssembler())
        self.assertTrue(result.startswith(self.PREFIX))
        self.assertTrue(result.endswith('CSRRS x21, 0xC00, x0\nECALL\n'))
        self.assertTrue(body.endswith('NOP\n'))
        for changed in (scheduled.replace('vstore m3', 'vstore m5'),
                        scheduled.replace('8(x6)', '16(x6)'),
                        scheduled.replace('li x7, 536875008\n', '')):
            with self.subTest(changed=changed), self.assertRaisesRegex(KernelError, 'non-idle'):
                splice(source, changed, TokenAssembler())


if __name__ == '__main__':
    unittest.main()

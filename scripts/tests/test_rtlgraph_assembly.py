#!/usr/bin/env python3
import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from rtlgraph_assembly import KernelError, functional_words, relocate_markers, straight_line_prefix, translate


class AssemblyTests(unittest.TestCase):
    def test_dma_capture_and_publication_spelling(self):
        native = translate('DMA.LOAD x4, x1, x2, 3\nCSRRW x0, 0xC10, x8\n', to_compiler=True)
        self.assertEqual(native, 'dma.load.ch3 x4, x1, x2\ncsrrw x0, x8, 3088 # atlas.release\n')
        self.assertIn('DMA.LOAD x4, x1, x2, 3', translate(native, to_compiler=False))

    def test_preserves_branch_slot_and_delay_age(self):
        source = 'loop:\nDELAY 7 # keep\nBNE x2, x0, loop\nADDI x9, x0, 1\n'
        native = translate(source, to_compiler=True)
        self.assertEqual(native, 'loop:\ndelay 7 # keep\nbne x2, x0, loop\naddi x9, x0, 1\n')
        self.assertEqual(translate(native, to_compiler=False), source)

    def test_pop_operand_order_and_register_bounds(self):
        source = 'VMATPOP.FP8.MXU1 63, 31, 1\n'
        native = translate(source, to_compiler=True)
        self.assertEqual(native, 'vmatpop.fp8.acc.mxu1 m63, acc1, e31\n')
        self.assertEqual(translate(native, to_compiler=False), source)
        with self.assertRaises(KernelError):
            translate(source.replace('63', '64'), to_compiler=True)

    def test_column_and_row_reduction_names(self):
        self.assertEqual(translate("VREDSUM.BF16 2, 0\nVREDMAX.ROW.BF16 4, 0\n", to_compiler=True),
                         "vredsum.bf16 m2, m0\nvredmax.row.bf16 m4, m0\n")

    def test_scalar_and_vector_memory(self):
        source = 'VSTORE 2, x8, 24\nLHU x10, x8, 0\n'
        native = translate(source, to_compiler=True)
        self.assertEqual(native, 'vstore m2, 24(x8)\nlhu x10, 0(x8)\n')
        self.assertEqual(translate(native, to_compiler=False), source)

    def test_relocates_only_private_read_only_counter(self):
        source = 'CSRR x20, 0xC00\nDMA.WAIT 0\nCSRR x21, 0xC00\nSUB x22, x21, x20\nCSRRW x0, 0xC11, x22\nCSRRW x0, 0xC10, x1\nECALL\n'
        body, markers = relocate_markers(source)
        self.assertEqual(len(markers), 4)
        self.assertIn('DMA.WAIT 0', body)
        for changed in (
            source.replace('DMA.WAIT 0', 'ADDI x20, x0, 1'),
            source.replace('CSRR x20, 0xC00', 'CSRRS x20, 0xC00, x1'),
            source.replace('DMA.WAIT 0', 'CSRRW x0, 0xC11, x1'),
            source.replace('DMA.WAIT 0', 'BNE x1, x0, done\nNOP\ndone:'),
            source.replace('SUB x22, x21, x20', 'SUB x22, x21, x20 # atlas.release'),
        ):
            with self.assertRaises(KernelError):
                relocate_markers(changed)

    def test_preserves_branch_targets_in_operation_comparison(self):
        class Assembler:
            def assemble(self, source):
                return [0x73] if 'ECALL' in source else []
        asm = Assembler()
        first = functional_words(asm, 'BNE x1, x0, done\nDELAY 2\ndone:\nECALL')
        moved = functional_words(asm, 'DELAY 5\nBNE x1, x0, done\ndone:\nECALL')
        changed = functional_words(asm, 'BNE x1, x0, other\nother:\nECALL')
        self.assertEqual(first, moved)
        self.assertNotEqual(first, changed)

    def test_control_prefix_requires_straight_line_encoding(self):
        class Assembler:
            def assemble(self, source):
                return [0x63 if line.startswith('BNE ') else 0x73 for line in source.splitlines()
                        if line.startswith(('BNE ', 'ECALL'))]
        asm = Assembler()
        self.assertEqual(straight_line_prefix('pass:\nECALL\nfail:\nECALL\n', asm), 'ECALL\n')
        with self.assertRaises(KernelError):
            straight_line_prefix('BNE x1, x0, fail\nECALL\nfail:\nECALL', asm)

    def test_unknown_syntax_and_misplaced_metadata_fail_closed(self):
        for source in ('AUIPC x1, 2', 'DMA.WAIT 8', '# atlas.release', 'ADDI x1, x0, 1 # atlas.release', 'BNE x1, x0, 16'):
            with self.assertRaises(KernelError):
                translate(source, to_compiler=True)


if __name__ == '__main__':
    unittest.main()

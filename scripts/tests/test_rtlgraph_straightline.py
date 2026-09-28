#!/usr/bin/env python3
"""Boundary and encoding checks for replay-only straight-line normalization."""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from rtlgraph_straightline import normalize
from rtlgraph_kernel import KernelError


class Assembler:
    @staticmethod
    def assemble(source):
        words = []
        encoding = {'NOP': 0x13, 'ECALL': 0x73, 'JAL': 0x6f, 'JALR': 0x67,
                    'BEQ': 0x63, 'AUIPC': 0x17, 'DELAY': 0x00301067,
                    'CSRRS': 0xc0002a73}
        for line in source.splitlines():
            line = line.split('#', 1)[0].strip()
            if not line or line.endswith(':'):
                continue
            words.append(encoding[line.split()[0].upper()])
        return words


class StraightlineTests(unittest.TestCase):
    def test_removes_only_unreachable_words_and_labels(self):
        text, facts = normalize('entry:\nNOP\nDELAY 3 # keep\npass:\nECALL\nfail:\nNOP\nECALL\n', Assembler)
        self.assertEqual(text, '# @PERF_REPORT\nNOP\nDELAY 3 # keep\nECALL\n')
        self.assertEqual(facts['reachable_word_count'], 3)
        self.assertEqual(facts['unreachable_word_count'], 2)
        self.assertEqual(facts['reachable_words_sha256'], facts['normalized_words_sha256'])
        self.assertEqual(facts['removed_label_source_lines'], [1, 4])

    def test_rejects_reachable_control_flow_and_pc_relative_ops(self):
        for instruction in ('JAL', 'JALR', 'BEQ', 'AUIPC'):
            with self.subTest(instruction=instruction), self.assertRaisesRegex(KernelError, 'reachable'):
                normalize(instruction + '\nECALL\n', Assembler)

    def test_unreachable_control_flow_does_not_change_prefix(self):
        text, facts = normalize('NOP\nECALL\nJAL\n', Assembler)
        self.assertEqual(facts['unreachable_word_count'], 1)
        self.assertNotIn('JAL', text)

    def test_existing_report_and_csr_metadata_preserved(self):
        source = '# @PERF_REPORT\nCSRRS x20, 0xc00, x0 # atlas.release\nECALL\n'
        text, facts = normalize(source, Assembler)
        self.assertEqual(text, source)
        self.assertFalse(facts['perf_report_added'])

    def test_missing_halt_and_release_on_removed_label_rejected(self):
        with self.assertRaisesRegex(KernelError, 'no ECALL'):
            normalize('NOP\n', Assembler)
        with self.assertRaisesRegex(KernelError, 'release'):
            normalize('start: # atlas.release\nECALL\n', Assembler)

    def test_encoding_mismatch_rejected(self):
        class BrokenAssembler(Assembler):
            @staticmethod
            def assemble(source):
                result = Assembler.assemble(source)
                if '@PERF_REPORT' in source:
                    result[0] ^= 1
                return result
        with self.assertRaisesRegex(KernelError, 'changed reachable'):
            normalize('NOP\nECALL\n', BrokenAssembler)


if __name__ == '__main__':
    unittest.main()

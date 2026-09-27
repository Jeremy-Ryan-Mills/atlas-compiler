#!/usr/bin/env python3
"""Controlled register-renaming and single-cycle bank-boundary fixtures."""

from pathlib import Path
import sys
import unittest
import zlib

sys.path.insert(0, str(Path(__file__).absolute().parents[1]))
from rtlgraph_conflicts import KernelError, rename_operand, witness_source
from rtlgraph_kernel import tokens


SOURCE = """# @PERF_REPORT
# @PERF_MACS_MXU1 262144
LI x6, 1024
VLOAD 0, x6, 0
VLOAD 1, x6, 0
VLOAD 6, x6, 0
CSRR x20, 0xC00
VMATPUSH.W.MXU1 0, 4
DELAY 30
VMATMUL.MXU1 0, 0, 0
VMATPUSH.W.MXU1 1, 6
DELAY 29
VMATMUL.MXU1 1, 2, 0
DELAY 30
VMATMUL.ACC.MXU1 0, 1, 1
VMATPUSH.W.MXU1 0, 5
DELAY 29
VMATMUL.ACC.MXU1 1, 3, 1
VMATPOP.FP8.MXU1 8, 0, 0
DELAY 29
VMATMUL.MXU1 0, 0, 0
VMATPUSH.W.MXU1 1, 7
VMATPOP.FP8.MXU1 10, 0, 1
DELAY 28
VMATMUL.MXU1 1, 2, 0
DELAY 30
VMATMUL.ACC.MXU1 0, 1, 1
DELAY 30
VMATMUL.ACC.MXU1 1, 3, 1
VMATPOP.FP8.MXU1 9, 0, 0
DELAY 30
VMATPOP.FP8.MXU1 11, 0, 1
CSRR x21, 0xC00
VSTORE 8, x6, 0
ECALL
"""


class OperandWords:
    """Opaque words except the two MREG fields being tested, not an ISA oracle.

    Real assembler roundtrip/bit checks also run in the generator integration.
    """
    @staticmethod
    def assemble(source):
        words = []
        for line in source.splitlines():
            p = [part.upper() for part in tokens(line)]
            if not p:
                continue
            if p[0] == "VLOAD":
                words.append(0x07 | (int(p[1]) << 7))
            elif p[0] == "VMATPUSH.W.MXU1":
                words.append(0x02000077 | (int(p[1]) << 7) | (int(p[2]) << 13))
            else:
                words.append(zlib.crc32(" ".join(p).encode()))
        return words


class ConflictFixtures(unittest.TestCase):
    def test_rename_changes_only_matrix_operand(self):
        result, changed = rename_operand("  VLOAD 6, x6, 0 # load source\n", 6, 32)
        self.assertTrue(changed)
        self.assertEqual(result, "  VLOAD 32, x6, 0 # load source\n")
        self.assertEqual(rename_operand("LI x6, 6\n", 6, 32), ("LI x6, 6\n", False))

    def test_safe_alias_retains_high_register_data_and_known_gap(self):
        result, info = witness_source(SOURCE, 32, 32, OperandWords())
        self.assertIn("VLOAD 32, x6, 0", result)
        self.assertIn("VMATPUSH.W.MXU1 1, 32", result)
        self.assertIn("VMATMUL.MXU1 0, 0, 0\n    DELAY 30", result)
        self.assertIn("VLOAD 0, x6, 0", result)
        self.assertIn("# @PERF_MACS_MXU1 262144", result)
        self.assertFalse(info["expected_bank_conflict"])
        self.assertEqual((info["first_compute_body_issue"], info["push_body_issue"]), (32, 64))
        self.assertEqual(info["static_issue_summary"]["cycle_csr_dispatch_window"], 322)
        self.assertTrue(info["reverse_rename_words_equal"])
        self.assertTrue(info["only_mreg_operand_bits_changed"])

    def test_invalid_gap_is_one_cycle_earlier_and_rows_are_distinct(self):
        _, info = witness_source(SOURCE, 32, 31, OperandWords())
        self.assertTrue(info["expected_bank_conflict"])
        self.assertEqual(info["hypothesized_collision"], {
            "body_cycle": 63, "bank": 0, "compute_logical_row": 31,
            "push_logical_row": 0, "compute_physical_row": 31, "push_physical_row": 32})
        self.assertEqual(info["static_issue_summary"]["cycle_csr_dispatch_window"], 321)

    def test_bank_control_keeps_the_invalid_case_spacing(self):
        _, invalid = witness_source(SOURCE, 32, 31, OperandWords())
        result, control = witness_source(SOURCE, 33, 31, OperandWords())
        self.assertIn("VLOAD 33, x6, 0", result)
        self.assertFalse(control["expected_bank_conflict"])
        self.assertEqual(control["target_pair"]["push_bank"], 1)
        self.assertEqual(control["push_body_issue"], invalid["push_body_issue"])

    def test_rejects_preexisting_destination_or_other_m6_uses(self):
        for source in (SOURCE.replace("VLOAD 1,", "VLOAD 32,"),
                       SOURCE.replace("VSTORE 8,", "VSTORE 6,"),
                       SOURCE.replace("VLOAD 6,", "VLOAD 7,")):
            with self.subTest(source=source), self.assertRaises(KernelError):
                witness_source(source, 32, 31, OperandWords())

    def test_rejects_unknown_ops_and_changed_pair_structure(self):
        for source in (SOURCE.replace("LI x6, 1024", "VMOV 6, 0"),
                       SOURCE.replace("VMATPUSH.W.MXU1 1, 6", "DELAY 1\nVMATPUSH.W.MXU1 1, 6"),
                       SOURCE.replace("VMATMUL.MXU1 0, 0, 0", "VMATMUL.MXU1 0, 1, 0", 1)):
            with self.subTest(source=source), self.assertRaises(KernelError):
                witness_source(source, 32, 31, OperandWords())

    def test_rejects_out_of_scope_destinations_and_gaps(self):
        for destination, gap in ((34, 31), (32, 30), (32, 33)):
            with self.subTest(destination=destination, gap=gap), self.assertRaises(KernelError):
                witness_source(SOURCE, destination, gap, OperandWords())

    def test_rejects_encoder_that_changes_other_bits(self):
        class BrokenWords(OperandWords):
            @staticmethod
            def assemble(source):
                words = OperandWords.assemble(source)
                if "VLOAD 32," in source:
                    words[3] ^= 1
                return words
        with self.assertRaisesRegex(KernelError, "outside a MREG operand"):
            witness_source(SOURCE, 32, 31, BrokenWords())


if __name__ == "__main__":
    unittest.main()

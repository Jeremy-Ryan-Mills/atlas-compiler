#!/usr/bin/env python3
"""Adapter boundary/syntax tests; real assembler equivalence is mandatory in CLI."""

from pathlib import Path
import sys
import unittest
import zlib

sys.path.insert(0, str(Path(__file__).absolute().parents[1]))
from rtlgraph_kernel import (KernelError, model_issue_summary, serialized_body,
                             splice, split_kernel, tokens, translate, verified_source)


SOURCE = """# @PERF_REPORT
# @PERF_MACS_MXU1 262144
NOP
CSRR x20, 0xC00 # begin
VMATPUSH.W.MXU1 1, 6
DELAY 30
VMATMUL.MXU1 0, 2, 1
VMATMUL.ACC.MXU1 1, 3, 0
VMATPOP.FP8.MXU1 9, 7, 1
CSRR x21, 0xC00 # end
NOP # original suffix
"""


class TokenAssembler:
    """Opaque words for checking token identity; deliberately not an ISA encoder."""

    @staticmethod
    def assemble(source):
        return [zlib.crc32(" ".join(p.upper() for p in tokens(line)).encode())
                for line in source.splitlines() if tokens(line)]


class KernelTests(unittest.TestCase):
    def test_pop_scale_accumulator_operand_reordering(self):
        compiler = translate("VMATPOP.FP8.MXU1 9, 7, 1", to_compiler=True)
        self.assertEqual(compiler, "vmatpop.fp8.acc.mxu1 m9, acc1, e7\n")
        self.assertEqual(translate(compiler, to_compiler=False), "VMATPOP.FP8.MXU1 9, 7, 1\n")

    def test_roundtrip_all_supported_operations_and_idle(self):
        sliced, compiler = verified_source(SOURCE, TokenAssembler())
        self.assertEqual(TokenAssembler.assemble(sliced.body),
                         TokenAssembler.assemble(translate(compiler, to_compiler=False)))
        self.assertEqual(translate("addi x0, x0, 0\nnop\ndelay 0", to_compiler=False),
                         "NOP\nNOP\nDELAY 0\n")

    def test_rejects_out_of_range_and_wrong_operand_types(self):
        for line in ("VMATMUL.MXU1 2, 0, 0", "VMATPUSH.W.MXU1 0, 64",
                     "VMATPOP.FP8.MXU1 0, 32, 0", "DELAY -1", "DELAY 4096"):
            with self.subTest(line=line), self.assertRaises(KernelError):
                translate(line, to_compiler=True)
        for line in ("vmatmul.mxu1 acc0, x2, w1", "vmatpop.fp8.acc.mxu1 m9, e7, acc1",
                     "vmatmul.mxu1 acc0, m2, w1, m3", "nop x0"):
            with self.subTest(line=line), self.assertRaises(KernelError):
                translate(line, to_compiler=False)

    def test_unknown_operations_and_labels_rejected(self):
        for line in ("vadd.bf16 m0, m1, m2", "other:", "addi x1, x0, 0", "ecall"):
            with self.subTest(line=line), self.assertRaises(KernelError):
                translate(line, to_compiler=False)

    def test_requires_unambiguous_cycle_markers(self):
        for source in (SOURCE.replace("CSRR x21, 0xC00 # end", ""),
                       SOURCE + "CSRR x22, 0xC00\n"):
            with self.assertRaisesRegex(KernelError, "exactly two"):
                split_kernel(source)
        sliced = split_kernel(SOURCE)
        self.assertEqual((sliced.begin_line, sliced.end_line), (4, 10))

    def test_rejects_pc_relative_or_control_flow_outside_slice(self):
        for line in ("JAL x1, 2", "BEQ x0, x0, 4", "AUIPC x1, 2", "label:"):
            with self.subTest(line=line), self.assertRaisesRegex(KernelError, "control flow"):
                split_kernel(SOURCE + line + "\n")

    def test_splice_preserves_prefix_marker_suffix_and_adds_unmeasured_drain(self):
        sliced, compiler = verified_source(SOURCE, TokenAssembler())
        result, report = splice(SOURCE, compiler, TokenAssembler(), 33)
        self.assertTrue(result.startswith(sliced.prefix))
        self.assertTrue(result.endswith(sliced.suffix))
        self.assertIn(sliced.end_marker + "# Unmeasured candidate drain", result)
        self.assertIn("DELAY 32\n" + sliced.suffix, result)
        self.assertTrue(report["unchanged_non_idle_instruction_multiset"])
        self.assertEqual(report["original_issue_summary"], report["replacement_issue_summary"])

    def test_windows_ignore_unmeasured_drain(self):
        _, compiler = verified_source(SOURCE, TokenAssembler())
        _, a = splice(SOURCE, compiler, TokenAssembler(), 0)
        _, b = splice(SOURCE, compiler, TokenAssembler(), 4096)
        self.assertEqual(a["replacement_issue_summary"], b["replacement_issue_summary"])
        self.assertEqual(b["unmeasured_drain_encoding"], "DELAY 4095")

    def test_dropped_duplicated_and_changed_operations_rejected(self):
        _, compiler = verified_source(SOURCE, TokenAssembler())
        for candidate in (compiler.replace("vmatmul.mxu1 acc0, m2, w1\n", ""),
                          compiler + "vmatmul.mxu1 acc0, m2, w1\n",
                          compiler.replace("m2", "m4")):
            with self.assertRaisesRegex(KernelError, "instruction multiset"):
                splice(SOURCE, candidate, TokenAssembler(), 33)

    def test_permutations_are_candidate_schedules_not_legality_proofs(self):
        _, compiler = verified_source(SOURCE, TokenAssembler())
        backwards = "\n".join(reversed(compiler.splitlines()))
        _, report = splice(SOURCE, backwards, TokenAssembler(), 33)
        self.assertIn("unverified", report["schedule_legality"])

    def test_serialized_gap_accounts_for_delay_instruction_itself(self):
        compiler = "vmatmul.mxu1 acc0, m2, w1\ndelay 30\nvmatmul.acc.mxu1 acc1, m3, w0\n"
        serial = serialized_body(compiler, 64)
        self.assertIn("delay 62", serial)
        summary = model_issue_summary(serial)
        self.assertEqual([op["body_issue_cycle"] for op in summary["operations"]], [0, 64])
        self.assertEqual(summary["cycle_csr_dispatch_window"], 66)
        self.assertNotIn("delay", serialized_body(compiler, 1))

    def test_roundtrip_requires_original_assembler_encoding_equality(self):
        class BadAssembler(TokenAssembler):
            def assemble(self, source):
                words = super().assemble(source)
                if "VMATPUSH.W.MXU1 1,6" in source:
                    words[0] ^= 1
                return words
        with self.assertRaisesRegex(KernelError, "roundtrip encoding mismatch"):
            verified_source(SOURCE.replace("1, 6", "1,6"), BadAssembler())


if __name__ == "__main__":
    unittest.main()

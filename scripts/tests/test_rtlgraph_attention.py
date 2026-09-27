#!/usr/bin/env python3
from pathlib import Path
import sys
import unittest
import zlib

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from rtlgraph_attention import EXTRA, KernelError, splice, split, strip_sentinel, translate
from rtlgraph_kernel import tokens


SOURCE = """NOP
CSRRS x20, 0xC00, x0
VMATPUSH.ACC.BF16.MXU1 1, 26
DELAY 31
VMATPOP.FP8.MXU1 30, 0, 1
VMAX.BF16 18, 8, 16
VEXP 24, 24
CSRRS x21, 0xC00, x0
pass:
ECALL
"""


class TokenAssembler:
    @staticmethod
    def assemble(source):
        return [zlib.crc32(" ".join(t.upper() for t in tokens(line)).encode())
                for line in source.splitlines() if tokens(line)]


class AttentionTests(unittest.TestCase):
    def test_new_operand_mappings_and_aliases(self):
        self.assertEqual(translate("VMATPUSH.ACC.BF16.MXU1 1, 26\nVMAX.BF16 18, 8, 16\nVEXP 24, 24", to_compiler=True),
                         "vmatpush.acc.bf16.mxu1 acc1, m26\nvmaximum.bf16 m18, m8, m16\nvexp.bf16 m24, m24\n")
        for name, (_, specs) in EXTRA.items():
            bare = name + " " + ", ".join("1" if s == "acc" else "26" for s in specs)
            self.assertEqual(translate(translate(bare, to_compiler=True), to_compiler=False), bare + "\n")

    def test_retains_fp8_pop_operand_order(self):
        self.assertEqual(translate("VMATPOP.FP8.MXU1 30, 7, 1", to_compiler=True),
                         "vmatpop.fp8.acc.mxu1 m30, acc1, e7\n")

    def test_rejects_invalid_operands_and_body_control_flow(self):
        for line in ("VMATPOP.BF16.MXU1 64, 0", "VMATPUSH.ACC.BF16.MXU1 2, 26",
                     "VEXP 2", "VEXP 2, 3, 4", "body:", "JAL x0, 4"):
            with self.subTest(line=line), self.assertRaises(KernelError):
                translate(line, to_compiler=True)
        with self.assertRaises(KernelError):
            translate("vexp.bf16 x2, m4", to_compiler=False)

    def test_requires_two_read_only_markers_and_no_relocation(self):
        self.assertTrue(split(SOURCE).suffix.startswith("pass:"))
        for changed in (SOURCE.replace("x21, 0xC00, x0", "x21, 0xC00, x1"),
                        SOURCE + "CSRRS x22, 0xC00, x0\n", SOURCE + "AUIPC x3, 8\n"):
            with self.assertRaises(KernelError):
                split(changed)

    def test_requires_single_last_completion_sentinel(self):
        for text in ("vexp.bf16 m2, m2\n", "ecall\necall\n", "ecall\ndelay 31\n"):
            with self.assertRaises(KernelError):
                strip_sentinel(text)
        self.assertEqual(strip_sentinel("delay 64\necall\n"), "delay 64\n")

    def test_drain_stays_before_counter_and_suffix_unchanged(self):
        s = split(SOURCE)
        compiled = translate(s.body, to_compiler=True) + "delay 64\necall\n"
        result, _ = splice(SOURCE, compiled, TokenAssembler())
        self.assertTrue(result.startswith(s.prefix))
        self.assertTrue(result.endswith("DELAY 64\n" + s.end_marker + s.suffix))

    def test_rejects_dropped_or_changed_operations(self):
        compiled = translate(split(SOURCE).body, to_compiler=True) + "ecall\n"
        for changed in (compiled.replace("vexp.bf16 m24, m24\n", ""), compiled.replace("acc1, m26", "acc0, m26")):
            with self.assertRaisesRegex(KernelError, "multiset"):
                splice(SOURCE, changed, TokenAssembler())


if __name__ == "__main__":
    unittest.main()

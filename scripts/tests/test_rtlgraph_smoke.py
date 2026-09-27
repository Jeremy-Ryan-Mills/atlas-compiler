#!/usr/bin/env python3
"""Validate smoke outcomes and isolation without hardware or licensed tools."""

from pathlib import Path
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).absolute().parents[1]))
from rtlgraph_smoke import classify, compile_commands, copy_inputs, run_command, simulator_command


class SmokeTests(unittest.TestCase):
    def test_marker_requires_successful_process_and_no_failure(self):
        self.assertEqual(classify(0, False, "PASS: good", "PASS: good"), "PASS")
        self.assertEqual(classify(1, False, "PASS: good", "PASS: good"), "PROCESS_FAILED")
        self.assertEqual(classify(0, True, "PASS: good", "PASS: good"), "TIMEOUT")
        self.assertEqual(classify(0, False, "nothing", "PASS: good"), "MISSING_PASS_MARKER")
        self.assertEqual(classify(0, False, "FAIL: bad\nPASS: good", "PASS: good"), "CHECK_FAILED")

    def test_runtime_command_uses_explicit_new_outputs(self):
        cmd = simulator_command(Path("/tmp/sim"), Path("/tmp/test.riscv"), Path("/tmp/ini"), Path("/tmp/out/coverage.vdb"), "test")
        self.assertEqual(cmd[cmd.index("-cm_dir") + 1], "/tmp/out/coverage.vdb")
        self.assertIn("-no_save", cmd)
        self.assertEqual(cmd[1], "+permissive")
        for option in ("-no_save", "-cm", "-cm_dir", "-cm_name"):
            self.assertLess(cmd.index("+permissive"), cmd.index(option))
            self.assertLess(cmd.index(option), cmd.index("+permissive-off"))
        self.assertIn("+ntb_random_seed=1", cmd)
        self.assertIn("+loadmem=/tmp/test.riscv", cmd)
        self.assertEqual(cmd[-2:], ["+permissive-off", "/tmp/test.riscv"])

    def test_mxu_requires_successful_architectural_completion(self):
        good = "DBG0 = 1\nstatus = 0x00000005\nPASS"
        self.assertEqual(classify(0, False, good, "PASS", require_mxu_completion=True), "PASS")
        for bad in ("PASS", good.replace("DBG0 = 1", "DBG0 = 2"), good.replace("0x00000005", "0x00000003")):
            self.assertEqual(classify(0, False, bad, "PASS", require_mxu_completion=True), "COMPLETION_CHECK_FAILED")

    def test_vector_arch_is_used_at_compile_and_link(self):
        commands = compile_commands(Path("/tmp/gcc"), Path("/tmp/test.c"), Path("/tmp/test.riscv"), vector=True)
        self.assertTrue(all("-march=rv64gcv_zfh_zvfh" in cmd for cmd in commands))
        self.assertIn("-MD", commands[0])

    def test_coverage_copy_omits_historical_test_runs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            src = root / "source"
            design = src / "snps/coverage/db/design/a.xml"
            old_run = src / "snps/coverage/db/testdata/old/log"
            for path in (design, old_run):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("data")
            records = copy_inputs(src, root / "new", skip_testdata=True)
            self.assertEqual(len(records), 1)
            self.assertFalse((root / "new/snps/coverage/db/testdata").exists())

    def test_copy_rejects_runtime_symlinks(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            src = root / "source"
            src.mkdir()
            (src / "link").symlink_to(root / "missing")
            with self.assertRaisesRegex(ValueError, "symlinks"):
                copy_inputs(src, root / "new")

    def test_timeout_is_recorded_as_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = run_command([sys.executable, "-c", "import time; time.sleep(10)"], root,
                                 dict(os.environ), root / "run.log", 0.1)
            self.assertTrue(result["timed_out"])
            self.assertNotEqual(result["returncode"], 0)

    def test_interrupt_cleans_up_process_group(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch("rtlgraph_smoke.subprocess.Popen") as popen, patch("rtlgraph_smoke.os.killpg") as kill:
                process = popen.return_value
                process.pid = 123
                process.wait.side_effect = [KeyboardInterrupt, -15]
                process.returncode = -15
                result = run_command(["fake"], root, {}, root / "run.log", 1)
                self.assertTrue(result["interrupted"])
                kill.assert_called_once()

    def test_missing_runtime_directory_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaisesRegex(ValueError, "must be a directory"):
                copy_inputs(root / "absent", root / "new")


if __name__ == "__main__":
    unittest.main()

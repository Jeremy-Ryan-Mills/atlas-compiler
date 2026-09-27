#!/usr/bin/env python3
"""CLI boundary tests; pass the compiled rtlgraph-vpu-search executable."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


SOLVER = None


class CliTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if SOLVER is None:
            raise unittest.SkipTest('pass the compiled VPU solver executable')

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = self.root / 'body.S'
        self.source.write_text('vsquare.bf16 m2, m0\nvtanh.bf16 m2, m2\necall\n')
        self.output = self.root / 'output.S'
        self.report = self.root / 'report.json'

    def command(self):
        return [str(SOLVER), '--source', str(self.source), '--output', str(self.output),
                '--report', str(self.report)]

    def run_solver(self, extra=()):
        return subprocess.run(self.command() + list(extra), capture_output=True, text=True)

    def test_nonfinite_and_invalid_budget_rejected(self):
        for value in ('nan', 'inf', '0', '-1', '3601', 'invalid'):
            with self.subTest(value=value):
                self.assertNotEqual(self.run_solver(['--seconds', value]).returncode, 0)
                self.assertFalse(self.output.exists())

    def test_missing_option_and_unknown_option_rejected(self):
        for extra in (['--seconds'], ['--unknown', '1']):
            self.assertNotEqual(self.run_solver(extra).returncode, 0)
        self.assertFalse(self.output.exists())

    def test_scalar_body_and_branch_rejected(self):
        for body in ('addi x1, x0, 1\necall\n', 'vsquare.bf16 m2, m0\njal x0, done\ndone:\necall\n'):
            self.source.write_text(body)
            self.assertNotEqual(self.run_solver().returncode, 0)
            self.assertFalse(self.output.exists())

    def test_timeout_exports_valid_seed_without_optimality_claim(self):
        self.source.write_text('''vsquare.bf16 m2, m0
vtanh.bf16 m6, m4
vtanh.bf16 m2, m2
vexp.bf16 m6, m6
vexp.bf16 m2, m2
vsqrt.bf16 m6, m6
vsqrt.bf16 m2, m2
vlog2.bf16 m6, m6
vlog2.bf16 m2, m2
vrecip.bf16 m6, m6
vrecip.bf16 m2, m2
vsquare.bf16 m8, m10
vsquare.bf16 m2, m2
vtanh.bf16 m8, m8
vtanh.bf16 m2, m2
vexp.bf16 m8, m8
ecall
''')
        run = self.run_solver(['--seconds', '1e-12'])
        self.assertEqual(run.returncode, 0, run.stderr)
        report = json.loads(self.report.read_text())
        self.assertEqual(report['status'], 'search_incomplete')
        self.assertTrue(report['timed_out'])
        self.assertEqual(report['exhausted_infeasible_horizon'], -1)
        self.assertEqual(report['best_objective'], report['initial_upper_bound'])
        self.assertLess(report['lower_bound'], report['best_objective'])
        self.assertTrue(self.output.read_text().rstrip().endswith('ecall'))

    def test_existing_output_rejected_without_overwriting(self):
        self.output.write_text('preserve me\n')
        self.assertNotEqual(self.run_solver().returncode, 0)
        self.assertEqual(self.output.read_text(), 'preserve me\n')
        self.assertFalse(self.report.exists())


if __name__ == '__main__':
    if len(sys.argv) > 1:
        SOLVER = Path(sys.argv.pop(1)).resolve()
    unittest.main()

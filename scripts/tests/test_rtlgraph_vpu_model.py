#!/usr/bin/env python3
"""Run with typed exporter and built model probe as arguments."""
import copy
import json
from pathlib import Path
import subprocess
import sys
import unittest

from rtlgraph_vpu_model import compare, NAMES, SUPPORTED, issue_guard

EXPORTER = PROBE = None


class VpuModelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if EXPORTER is None or PROBE is None:
            raise unittest.SkipTest('Requires typed exporter and linked compiler probe')
        typed = json.loads(subprocess.check_output([str(EXPORTER), str(Path(__file__).with_name('rtlgraph-vpu.mlir')),
                                                    'IssueGuard'], text=True))
        cls.guard = issue_guard(typed['modules'][0])
        cls.probe = json.loads(subprocess.check_output([str(PROBE), *(NAMES[i] for i in SUPPORTED)], text=True))

    def test_real_compiler_policy_and_exact_coverage(self):
        result = compare(self.guard, self.probe)
        self.assertEqual(result['operations_checked'], 29)
        self.assertEqual(result['slot_class_checks'], 29)
        self.assertEqual(result['ordered_pair_checks'], 841)
        self.assertEqual(result['excluded'][0]['inner_opcode'], 15)
        self.assertNotIn(15, [op['inner_opcode'] for op in result['mapping']])

    def test_changed_ordered_pair_rejected(self):
        probe = copy.deepcopy(self.probe)
        a, b = SUPPORTED.index(9), SUPPORTED.index(25)  # exp -> mov
        probe['can_overlap'][a][b] = False
        with self.assertRaisesRegex(ValueError, 'overlap mismatch: vexp.bf16 -> vmov'):
            compare(self.guard, probe)

    def test_changed_slot_or_opcode_class_rejected(self):
        for field, replacement, error in (('both_slots', False, 'slot class'), ('class', 'row_reduce', 'Compiler class')):
            probe = copy.deepcopy(self.probe)
            probe['operations'][0][field] = replacement
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, error):
                compare(self.guard, probe)

    def test_missing_reordered_or_aliased_operation_rejected(self):
        for mutation in ('missing', 'reordered', 'alias'):
            probe = copy.deepcopy(self.probe)
            if mutation == 'missing': probe['operations'].pop()
            elif mutation == 'reordered': probe['operations'][:2] = reversed(probe['operations'][:2])
            else: probe['operations'][15]['name'] = 'vfp8'
            with self.subTest(mutation=mutation), self.assertRaisesRegex(ValueError, 'unsupported compiler operation'):
                compare(self.guard, probe)

    def test_changed_source_encoding_or_matrix_shape_rejected(self):
        for mutation in ('encoding', 'row', 'unknown', 'duplicate'):
            guard = copy.deepcopy(self.guard)
            if mutation == 'encoding': guard['encoding'][15]['issue_busy_bit'] = 15
            elif mutation == 'row': guard['one_active_instruction_overlap'].pop()
            elif mutation == 'unknown': guard['one_active_instruction_overlap'][3]['allowed_incoming_opcodes'].append(30)
            else: guard['one_active_instruction_overlap'][3]['allowed_incoming_opcodes'].append(25)
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                compare(guard, self.probe)

    def test_incomplete_domain_and_unknown_boolean_rejected(self):
        guard = copy.deepcopy(self.guard)
        guard['assignments_checked'] = 8192
        with self.assertRaisesRegex(ValueError, 'Incomplete'):
            compare(guard, self.probe)
        probe = copy.deepcopy(self.probe)
        probe['can_overlap'][0][0] = 0  # JSON integer is not a checked Boolean.
        with self.assertRaisesRegex(ValueError, 'Malformed'):
            compare(self.guard, probe)

    def test_probe_rejects_non_vpu_missing_duplicate_and_unsafe_names(self):
        for names in ([], ['add'], ['vfp8'], ['vmov', 'vmov'], ['vmov"']):
            with self.subTest(names=names):
                result = subprocess.run([str(PROBE), *names], capture_output=True, text=True)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stdout, '')


if __name__ == '__main__':
    EXPORTER, PROBE = (Path(sys.argv.pop(1)).resolve() for _ in range(2))
    unittest.main()

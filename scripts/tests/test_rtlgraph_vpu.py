#!/usr/bin/env python3
"""Run with the existing typed CIRCT exporter as the first argument."""
import copy
import json
from pathlib import Path
import subprocess
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from rtlgraph_vpu import Graph, issue_guard, final_write_release, named_cone, wire


class VpuTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        exported = json.loads(subprocess.check_output([str(EXPORTER), str(Path(__file__).with_name('rtlgraph-vpu.mlir')),
                                                       'IssueGuard', 'Release'], text=True))
        cls.modules = {module['name']: module for module in exported['modules']}

    def test_full_domains_and_selected_overlap_meanings(self):
        guard = issue_guard(self.modules['IssueGuard'])
        self.assertEqual(guard['assignments_checked'], 16384)
        matrix = {row['active_name']: row['allowed_incoming_opcodes'] for row in guard['one_active_instruction_overlap']}
        self.assertNotIn(10, matrix['exp'])  # shared exp/exp2 datapath
        self.assertIn(25, matrix['exp'])    # independent move can use the other slot
        self.assertNotIn(2, matrix['exp'])  # multiply needs both read slots
        self.assertEqual(matrix['rsum'], [])
        for slot in (1, 2):
            self.assertEqual(final_write_release(self.modules['Release'], slot)['assignments_checked'], 65536)

    def test_width_and_duplicate_cutpoints_rejected(self):
        for mutation in ('width', 'identity'):
            module = copy.deepcopy(self.modules['IssueGuard'])
            done1 = next(p for p in module['ports'] if p['name'] == 'done1')
            done2 = next(p for p in module['ports'] if p['name'] == 'done2')
            if mutation == 'width': done1['type'] = 'i2'
            else: done1['value'] = done2['value']
            with self.subTest(mutation=mutation), self.assertRaisesRegex(ValueError, 'identities or widths'):
                issue_guard(module)

    def test_unsupported_state_feedback_and_missing_input_rejected(self):
        for mutation in ('state', 'feedback', 'undefined', 'operation'):
            module = copy.deepcopy(self.modules['IssueGuard'])
            op = next(o for o in module['operations'] if o['kind'] == 'comb.icmp')
            if mutation == 'state': op['kind'] = 'seq.firreg'
            elif mutation == 'feedback': op['operands'][0] = op['results'][0]
            elif mutation == 'undefined': op['operands'][0] = 'not-defined'
            else: op['kind'] = 'comb.mul'
            with self.subTest(mutation=mutation), self.assertRaisesRegex(ValueError, 'Unsupported|Feedback'):
                issue_guard(module)

    def test_unsupported_comparison_rejected(self):
        module = copy.deepcopy(self.modules['IssueGuard'])
        next(o for o in module['operations'] if o['kind'] == 'comb.icmp')['attributes']['predicate'] = '2 : i64'
        with self.assertRaisesRegex(ValueError, 'Unsupported comparison'):
            issue_guard(module)

    def test_changed_issue_guard_rejected(self):
        module = copy.deepcopy(self.modules['IssueGuard'])
        graph = Graph(module)
        root = graph.definitions[graph.named('io_out_issueBusy')]
        self.assertEqual(root['kind'], 'comb.mux')
        root['operands'][1:] = reversed(root['operands'][1:])
        with self.assertRaisesRegex(ValueError, 'issue-busy function mismatch'):
            issue_guard(module)

    def test_last_write_comparison_change_rejected(self):
        module = copy.deepcopy(self.modules['Release'])
        next(o for o in module['operations'] if o['kind'] == 'comb.icmp')['attributes']['predicate'] = '1 : i64'
        with self.assertRaisesRegex(ValueError, 'slot-release predicate mismatch'):
            final_write_release(module, 1)

    def test_release_requires_fire_or_previously_done(self):
        module = self.modules['Release']
        cone = named_cone(module, 'done1', ['writeDone1', 'io_in_dataOutFire1', 'writeCounter1', 'writeLim1'], [1, 1, 7, 7], 1)
        self.assertEqual(cone((0, 0, 63, 63)), 0)
        self.assertEqual(cone((0, 1, 63, 63)), 1)
        self.assertEqual(cone((0, 1, 62, 63)), 0)
        self.assertEqual(cone((1, 0, 0, 63)), 1)
        for values in ((0, 1, 128, 63), (0, 1, -1, 63), (False, 1, 63, 63)):
            with self.assertRaisesRegex(ValueError, 'assignment out of range'):
                cone(values)

    def test_direct_wire_requires_identity_and_pinned_width(self):
        module = self.modules['Release']
        graph = Graph(module)
        value = graph.named('writeDone1')
        self.assertEqual(wire(module, value, value, 'fixture', 1)['path'], [])
        with self.assertRaisesRegex(ValueError, 'wire width'):
            wire(module, value, value, 'fixture', 2)
        with self.assertRaisesRegex(ValueError, 'direct wiring mismatch'):
            wire(module, value, graph.named('io_in_dataOutFire1'), 'fixture', 1)


if __name__ == '__main__':
    EXPORTER = Path(sys.argv.pop(1)).resolve()
    unittest.main()

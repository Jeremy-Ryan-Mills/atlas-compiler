import copy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from rtlgraph_phases import compare_issues


class PhaseTests(unittest.TestCase):
    def setUp(self):
        self.wait = 0x0200107f
        words = [0x13, self.wait, 0xc0002073, 0x13, 0xc0002073, self.wait, 0xc1001073]
        self.baseline = [dict(pc=pc, cycle=cycle, instruction=word)
                         for pc, (cycle, word) in enumerate(zip([10, 20, 21, 25, 30, 45, 46], words))]
        candidate_words = words[:3] + [0x93, 0x13] + words[4:]
        self.candidate = [dict(pc=pc, cycle=cycle, instruction=word)
                          for pc, (cycle, word) in enumerate(zip([110, 123, 124, 126, 128, 130, 150, 151], candidate_words))]
        self.old_markers = {'start': 2, 'end': 4, 'done': 6}
        self.new_markers = {'start': 2, 'end': 5, 'done': 7}

    def compare(self):
        return compare_issues(self.baseline, self.candidate, self.old_markers, self.new_markers,
                              {self.wait: 1})

    def test_separates_dma_waits_window_and_launch_shift(self):
        phases = self.compare()
        self.assertEqual(phases['setup']['delta_edges'], 3)
        self.assertEqual(phases['writeback']['delta_edges'], 5)
        self.assertEqual(phases['timed_window']['delta_edges'], -3)
        self.assertEqual(phases['first_issue_to_dbg0']['delta_edges'], 5)
        self.assertEqual(phases['setup']['dma_wait_delta_edges'], 3)
        self.assertEqual(phases['writeback']['other_delta_edges'], 0)
        changed = phases['writeback']['changed_intervals']
        self.assertEqual([(gap['baseline_pc'], gap['candidate_pc']) for gap in changed], [(5, 6)])

    def test_changed_outer_instruction_rejected(self):
        for index in (0, 6):
            with self.subTest(index=index):
                candidate = copy.deepcopy(self.candidate)
                candidate[index]['instruction'] = 0x33
                with self.assertRaisesRegex(ValueError, 'encoded instructions differ'):
                    compare_issues(self.baseline, candidate, self.old_markers, self.new_markers,
                                   {self.wait: 1})

    def test_nonwait_gap_attributed_separately(self):
        self.candidate[-1]['cycle'] += 2
        phases = self.compare()
        self.assertEqual(phases['writeback']['dma_wait_delta_edges'], 5)
        self.assertEqual(phases['writeback']['other_delta_edges'], 2)

    def test_bad_pc_cycle_or_marker_rejected(self):
        for key, value in [('pc', 17), ('cycle', 130)]:
            candidate = copy.deepcopy(self.candidate)
            candidate[2][key] = value
            with self.assertRaises(ValueError):
                compare_issues(self.baseline, candidate, self.old_markers, self.new_markers, {})
        self.new_markers['done'] -= 1
        with self.assertRaisesRegex(ValueError, 'marker'):
            self.compare()


if __name__ == '__main__':
    unittest.main()

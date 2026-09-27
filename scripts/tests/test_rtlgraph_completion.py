import copy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from rtlgraph_completion import analyze_samples


class CompletionTests(unittest.TestCase):
    def setUp(self):
        self.wait = 0x0200107f
        self.words = [0x13, 0xc0002073, 0x13, 0xc0002073, 0x13, 0xc1101073,
                      self.wait, 0x13, 0xc1001073, 0x73]
        cycles = [2, 10, 12, 36, 37, 38, 60, 61, 62]
        self.samples = []
        for pc, cycle in enumerate(cycles):
            word = self.words[pc]
            csr = pc in (1, 3, 5, 8)
            sample = {'reset': 0, 'scalar.fire': 1, 'scalar.pc': pc, 'scalar.instr': word,
                      'csr.valid': int(csr), 'csr.addr': word >> 20 if csr else 0,
                      'csr.cmd': 2 if pc in (1, 3) else 1,
                      'csr.wdata': 25 if pc == 5 else 1 if pc == 8 else 0,
                      'csr.rdata': 100 if pc == 1 else 125 if pc == 3 else 0}
            self.samples.append((cycle, cycle * 1000, sample))

    def analyze(self, samples=None, words=None):
        return analyze_samples(self.samples if samples is None else samples,
                               self.words if words is None else words, {self.wait})

    def test_distinct_counter_completion_and_clock_metrics(self):
        report = self.analyze()
        self.assertEqual(report['metrics'], {'csr_counter_delta': 25, 'csr_window_clock_edges': 26,
            'first_issue_to_dbg0_edges': 60, 'csr_start_to_dbg0_edges': 52,
            'csr_end_to_dbg0_edges': 26, 'last_dma_wait_to_dbg0_edges': 2})
        self.assertIsNone(report['ecall_cycle'])

    def test_missing_completion_rejected(self):
        with self.assertRaisesRegex(ValueError, 'successful DBG0'):
            self.analyze(self.samples[:-1])

    def test_changed_word_and_skipped_pc_rejected(self):
        for key, value in [('scalar.instr', 0x93), ('scalar.pc', 3)]:
            samples = copy.deepcopy(self.samples)
            samples[2][2][key] = value
            with self.assertRaises(ValueError):
                self.analyze(samples)

    def test_spurious_csr_and_bad_delta_rejected(self):
        for index, key, value in [(2, 'csr.valid', 1), (5, 'csr.wdata', 24), (8, 'csr.wdata', 2)]:
            samples = copy.deepcopy(self.samples)
            samples[index][2][key] = value
            with self.assertRaises(ValueError):
                self.analyze(samples)

    def test_extra_cycle_marker_rejected(self):
        samples = copy.deepcopy(self.samples)
        words = list(self.words)
        words[2] = 0xc0002073
        samples[2][2].update({'scalar.instr': words[2], 'csr.valid': 1, 'csr.addr': 0xc00,
                              'csr.cmd': 2, 'csr.wdata': 0, 'csr.rdata': 102})
        with self.assertRaisesRegex(ValueError, 'exactly two'):
            self.analyze(samples, words)

    def test_missing_final_dma_wait_or_ecall_rejected(self):
        with self.assertRaisesRegex(ValueError, 'DMA.WAIT'):
            analyze_samples(self.samples, self.words, set())
        with self.assertRaisesRegex(ValueError, 'ECALL'):
            self.analyze(words=self.words[:-1] + [0x13])

    def test_reset_during_execution_rejected(self):
        samples = copy.deepcopy(self.samples)
        samples[2][2]['reset'] = 1
        with self.assertRaisesRegex(ValueError, 'reset'):
            self.analyze(samples)


if __name__ == '__main__':
    unittest.main()

import copy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from rtlgraph_vpu_probes import analyze_samples, branch_target, expected_source, probe_contract
from test_rtlgraph_attention import TokenAssembler


class VpuProbeTests(unittest.TestCase):
    def setUp(self):
        self.words = [0x13, 0xc0002c73, 0x00100457, 0x04001067, 0xc0002cf3,
                      0x00045503, 0x00b51663, 0x001e0e13, 0xc11d1073,
                      0x00100093, 0xc1009073, 0x73, 0xc10e1073, 0x73]
        self.contract = {'kind': 'binary', 'words': self.words,
                         'windows': [{'start_pc': 1, 'operation_pc': 2, 'end_pc': 4, 'operation': 'VADD.BF16'}],
                         'checks': [{'branch_pc': 6, 'operation': 'VADD.BF16', 'expected_bf16': 0x40c0}],
                         'success_pc': 10, 'failure_pc': 12}
        self.samples = []
        for pc, word in enumerate(self.words[:11]):
            csr = pc in (1, 4, 8, 10)
            sample = {'reset': 0, 'scalar.fire': 1, 'scalar.pc': pc, 'scalar.instr': word,
                      'csr.valid': int(csr), 'csr.addr': word >> 20 if csr else 0,
                      'csr.cmd': 2 if pc in (1, 4) else 1,
                      'csr.wdata': 37 if pc == 8 else 1 if pc == 10 else 0,
                      'csr.rdata': 100 if pc == 1 else 137 if pc == 4 else 0}
            self.samples.append((pc * 10, pc * 1000, sample))

    def test_success_path_binds_numerical_check_and_counter_sum(self):
        result = analyze_samples(self.samples, self.contract)
        self.assertEqual(result['status'], 'observed_numerical_spot_checks_passed')
        self.assertEqual(result['metrics']['csr_counter_delta'], 37)
        self.assertEqual(result['numerical_spot_check_count'], 1)
        self.assertTrue(result['checks'][0]['observed_untaken'])
        self.assertEqual(result['windows'][0]['clock_edges'], 30)

    def test_failure_code_equal_to_one_cannot_masquerade_as_success(self):
        samples = copy.deepcopy(self.samples[:7])
        failed = copy.deepcopy(self.samples[10])
        failed[2].update({'scalar.pc': 12, 'scalar.instr': self.words[12], 'csr.wdata': 1})
        samples.append(failed)
        with self.assertRaisesRegex(ValueError, 'success path'):
            analyze_samples(samples, self.contract)

    def test_missing_branch_or_changed_instruction_is_rejected(self):
        with self.assertRaisesRegex(ValueError, 'success path'):
            analyze_samples(self.samples[:6] + self.samples[7:], self.contract)
        samples = copy.deepcopy(self.samples)
        samples[2][2]['scalar.instr'] ^= 1 << 7
        with self.assertRaisesRegex(ValueError, 'expected successful instruction'):
            analyze_samples(samples, self.contract)

    def test_wrong_sum_or_read_side_effect_is_rejected(self):
        for index, key, value in ((8, 'csr.wdata', 38), (1, 'csr.wdata', 1),
                                  (4, 'csr.addr', 0xc01), (8, 'csr.valid', 0),
                                  (10, 'csr.wdata', 2)):
            samples = copy.deepcopy(self.samples)
            samples[index][2][key] = value
            with self.subTest(index=index, key=key), self.assertRaises(ValueError):
                analyze_samples(samples, self.contract)

    def test_incomplete_or_reset_execution_is_rejected(self):
        with self.assertRaisesRegex(ValueError, 'incomplete'):
            analyze_samples(self.samples[:-1], self.contract)
        samples = copy.deepcopy(self.samples)
        samples[3][2]['reset'] = 1
        with self.assertRaisesRegex(ValueError, 'reset'):
            analyze_samples(samples, self.contract)

    def test_window_pc_binding_and_ecall_are_required(self):
        contract = copy.deepcopy(self.contract)
        contract['windows'][0]['end_pc'] = 5
        with self.assertRaisesRegex(ValueError, 'window PC'):
            analyze_samples(self.samples, contract)
        contract = copy.deepcopy(self.contract)
        contract['words'][11] = 0x13
        with self.assertRaisesRegex(ValueError, 'ECALL'):
            analyze_samples(self.samples, contract)

    def test_atlas_word_addressed_branch_target(self):
        self.assertEqual(branch_target(26, 0x08b51063), 90)
        self.assertEqual(branch_target(83, 0x00b51663), 89)
        self.assertEqual(branch_target(10, 0xfeb51ee3), 8)

    def test_templates_retain_original_numerical_checks_and_singleton_windows(self):
        for kind, windows, checks in [('binary', 7, 5), ('reduction', 8, 6)]:
            source = expected_source(kind)
            self.assertEqual(source.count('CSRRS x24'), windows)
            self.assertEqual(source.count('BNE x10, x11, fail'), checks)
            self.assertIn('CSRRW x0, 0xC10, x28', source)
        with self.assertRaisesRegex(ValueError, 'exactly'):
            probe_contract('VADD.BF16 8, 0, 2', TokenAssembler())


if __name__ == '__main__':
    unittest.main()

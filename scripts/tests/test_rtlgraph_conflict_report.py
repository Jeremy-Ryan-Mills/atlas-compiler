import copy
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import rtlgraph_conflict_report as report


def records(negative=False):
    digest = {"bytes": 4, "sha256": "a"}
    case = {"assembly": digest.copy(), "model_check": {"returncode": int(negative)},
            "target_pair": {"push_mreg": 32}}
    replay = {"kind": "rtlgraph-perf-replay", "config": "EE290SimConfig", "status": "passed",
              "finished_utc": "2026-09-27T00:00:00+00:00", "trace_validation": {"status": "PARSED"},
              "inputs": {"assembly": digest.copy()}, "trace": digest.copy(),
              "result": {"status": "PASS", "checked_words": 1024,
                         "metrics": {"dbg0": 1, "csr_status": 5, "dbg1_cycles": 321}}}
    banks = {"status": "trace_obligations_passed", "trace_header": {"vcd": digest.copy()},
             "transaction_count": 8, "banks": {"status": "observed_p0_p1_requests_checked",
                                               "p1_pushes": [None] * 4, "different_bank_overlap_cycles": 62},
             "perf": {"pushes": [None] * 4, "pops": [None] * 4,
                      "mxu1_commands": [{"cycle": 100, "command": {"op": "Matmul", "mreg": 0}},
                                        {"cycle": 132, "command": {"op": "PushWeight", "mreg": 32}}]}}
    if negative:
        replay.update(status="failed", result={"status": "FAIL"})
        banks.update(status="bank_read_conflict_observed", collisions=[{
            "cycle": 131, "physical_bank": 0,
            "p0": {"valid": True, "mreg": 0, "row": 31, "physical_bank": 0, "physical_row": 31},
            "p1": {"valid": True, "mreg": 32, "row": 0, "physical_bank": 0, "physical_row": 32}}])
    return case, replay, banks


class ReportTests(unittest.TestCase):
    def test_positive_and_negative_are_different_evidence_categories(self):
        self.assertEqual(report.evaluate_case("alias_safe", *records(), "", "bank assertion")["status"],
                         "finite_positive_witness_passed")
        self.assertEqual(report.evaluate_case("alias_invalid", *records(True), "bank assertion", "bank assertion")["status"],
                         "intended_bank_conflict_observed")

    def test_negative_requires_both_exact_assertion_and_observed_target(self):
        with self.assertRaisesRegex(ValueError, "expected RTL bank assertion"):
            report.evaluate_case("alias_invalid", *records(True), "unrelated failure", "bank assertion")
        case, replay, banks = records(True)
        for key, value in (("mreg", 33), ("row", 1), ("physical_row", 33)):
            changed = copy.deepcopy(banks)
            changed["collisions"][0]["p1"][key] = value
            with self.assertRaisesRegex(ValueError, "different p1 addresses"):
                report.evaluate_case("alias_invalid", case, replay, changed, "bank assertion", "bank assertion")

    def test_hash_mismatch_rejects_wrong_assembly_and_waveform(self):
        case, replay, banks = records()
        replay["inputs"]["assembly"]["sha256"] = "b"
        with self.assertRaisesRegex(ValueError, "different assembly"):
            report.evaluate_case("alias_safe", case, replay, banks, "", "assertion")
        replay["inputs"]["assembly"]["sha256"] = "a"
        banks["trace_header"]["vcd"]["sha256"] = "b"
        with self.assertRaisesRegex(ValueError, "different waveform"):
            report.evaluate_case("alias_safe", case, replay, banks, "", "assertion")

    def test_incomplete_functional_or_bank_checks_rejected(self):
        for changed in ("words", "bank", "transactions"):
            case, replay, banks = records()
            if changed == "words":
                replay["result"]["checked_words"] = 512
            elif changed == "bank":
                banks["banks"]["status"] = "pending"
            else:
                banks["transaction_count"] = 7
            with self.assertRaises(ValueError):
                report.evaluate_case("alias_safe", case, replay, banks, "", "assertion")

    def test_transient_success_before_capture_completion_rejected(self):
        for field in ("finished_utc", "trace_validation"):
            case, replay, banks = records()
            del replay[field]
            with self.assertRaisesRegex(ValueError, "not complete and parsed"):
                report.evaluate_case("alias_safe", case, replay, banks, "", "assertion")
        case, replay, banks = records(True)
        replay["trace_validation"]["status"] = "PENDING"
        with self.assertRaisesRegex(ValueError, "not complete and parsed"):
            report.evaluate_case("alias_invalid", case, replay, banks, "assertion", "assertion")

    def test_control_must_expose_same_31_cycle_gap(self):
        case, replay, banks = records()
        case["target_pair"]["push_mreg"] = 33
        banks["perf"]["mxu1_commands"][1]["command"]["mreg"] = 33
        with self.assertRaisesRegex(ValueError, "wrong tested issue gap"):
            report.evaluate_case("bank_control", case, replay, banks, "", "assertion")
        banks["perf"]["mxu1_commands"][1]["cycle"] = 131
        self.assertEqual(report.evaluate_case("bank_control", case, replay, banks, "", "assertion")["observed_target_issue_gap"], 31)

    def test_fixture_comparison_is_data_driven(self):
        preload = [{"word_offset": i, "data": "0x0"} for i in range(32)]
        preload += [{"word_offset": 192 + i, "data": "0x1" if i == 0 else "0x0"} for i in range(32)]
        data = report.fixture_data({"dram_preloads": preload})
        self.assertEqual(data["different_beats"], 1)
        self.assertEqual(data["different_bytes"], 1)
        self.assertTrue(data["collision_target_rows_differ"])


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
"""Adversarial P0/P1 request observations, independent of schedule gap rules."""
import copy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).absolute().parents[1]))
from rtlgraph_mxu1_banks import BANK_SCHEMA, check_banked_trace, check_expected_conflict
from rtlgraph_mxu1_trace import TraceError
from test_rtlgraph_mxu1_trace import PERF_HEADER, perf_witness, add_scalar_issue


HEADER = {**PERF_HEADER, "bank_capture": BANK_SCHEMA}


def witness(mreg=33, start=34):
    samples = perf_witness(count=2)
    for sample in samples[1:]:
        sample["bank_reads"] = {"p0": copy.deepcopy(sample["mreg_req"]), "p1": {"valid": False}}
        sample["p1_resp_banks"] = []
    samples[start]["cmd"] = {"valid": True, "op": "PushWeight", "mreg": mreg, "accsel": 1, "wslot": 1}
    samples[start]["accept"]["push_p1"] = True
    add_scalar_issue(samples[start])
    for row in range(32):
        samples[start + row]["bank_reads"]["p1"] = {"valid": True, "mreg": mreg, "row": row}
        samples[start + row + 1]["p1_resp_banks"] = [mreg & 31]
        samples[start + row + 1]["weight_write"] = {"valid": True, "wslot": 1, "row": row}
    return samples


class BankTest(unittest.TestCase):
    def test_valid_different_bank_overlap_and_high_half_request(self):
        report = check_banked_trace(HEADER, witness())
        self.assertEqual(report["status"], "trace_obligations_passed")
        self.assertEqual(report["banks"]["different_bank_overlap_cycles"], 32)
        push = report["banks"]["p1_pushes"][0]
        self.assertEqual(push["command"]["mreg"], 33)
        self.assertEqual(push["requests"]["ages"], list(range(32)))
        self.assertEqual(push["responses"]["ages"], list(range(1, 33)))

    def test_same_bank_different_physical_rows_rejected(self):
        samples = witness(mreg=34, start=64)
        with self.assertRaisesRegex(TraceError, "physical-bank read collision"):
            check_banked_trace(HEADER, samples)
        report = check_expected_conflict(HEADER, samples[:65])
        self.assertEqual(report["status"], "bank_read_conflict_observed")
        hit = report["collisions"][0]
        self.assertEqual((hit["cycle"], hit["physical_bank"]), (64, 2))
        self.assertEqual((hit["p0"]["physical_row"], hit["p1"]["physical_row"]), (31, 32))

    def test_expected_invalid_mode_requires_exactly_one_collision(self):
        for samples in (witness(), witness(mreg=34, start=34)):
            with self.assertRaisesRegex(TraceError, "exactly one"):
                check_expected_conflict(HEADER, samples)

    def test_p1_request_follows_scalar_metadata_and_row_order(self):
        for key, value, message in (("mreg", 35, "scalar-specified"), ("row", 1, "request rows")):
            samples = witness()
            samples[34]["bank_reads"]["p1"][key] = value
            with self.subTest(key=key), self.assertRaisesRegex(TraceError, message):
                check_banked_trace(HEADER, samples)

    def test_p1_response_uses_actual_registered_bank_tags(self):
        samples = witness()
        samples[35]["p1_resp_banks"] = [9]
        with self.assertRaisesRegex(TraceError, "P1 response bank routing"):
            check_banked_trace(HEADER, samples)
        samples[35]["p1_resp_banks"] = [1, 2]
        with self.assertRaisesRegex(TraceError, "P1 response bank routing"):
            check_banked_trace(HEADER, samples)

    def test_p1_response_requires_matching_weight_write(self):
        samples = witness()
        samples[35]["weight_write"]["valid"] = False
        with self.assertRaisesRegex(TraceError, "did not produce"):
            check_banked_trace(HEADER, samples)
        samples = witness()
        samples[35]["weight_write"]["wslot"] = 0
        with self.assertRaisesRegex(TraceError, "destination or row"):
            check_banked_trace(HEADER, samples)

    def test_p0_mreg_boundary_must_match_sequencer(self):
        samples = witness()
        samples[34]["bank_reads"]["p0"]["mreg"] = 7
        with self.assertRaisesRegex(TraceError, "differs from sequencer"):
            check_banked_trace(HEADER, samples)

    def test_missing_unknown_or_out_of_range_requests_rejected(self):
        for change in ("missing", "unknown", "wide"):
            samples = witness()
            if change == "missing": del samples[34]["bank_reads"]["p1"]
            if change == "unknown": samples[34]["bank_reads"]["p1"]["mreg"] = None
            if change == "wide": samples[34]["bank_reads"]["p1"]["mreg"] = 65
            with self.subTest(change=change), self.assertRaises(TraceError):
                check_banked_trace(HEADER, samples)

    def test_bank_mode_requires_frozen_schema(self):
        with self.assertRaisesRegex(TraceError, "schema"):
            check_banked_trace(PERF_HEADER, witness())


if __name__ == "__main__":
    unittest.main()

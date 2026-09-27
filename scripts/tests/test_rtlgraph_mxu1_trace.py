#!/usr/bin/env python3
"""Adversarial sampled-interface traces; no simulator or timing LUT required."""

import copy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).absolute().parents[1]))
from rtlgraph_mxu1_trace import TraceError, check_trace, decode_mxu1


HEADER = {"schema_version": 1, "kind": "atlas-mxu1-cycle-trace",
          "sampling": "settled_pre_rising_edge", "tile_rows": 32,
          "response_valid_provenance": "reconstructed_from_registered_bank_tags"}


def idle(cycle):
    return {"cycle": cycle, "reset": False, "cmd": {"valid": False},
            "accept_compute": False, "p0": {"valid": False, "boundary": True},
            "mreg_req": {"valid": False}, "mreg_resp_valid": False, "mreg_resp_count": 0,
            "mreg_resp_banks": [],
            "acc_read": {"valid": False}, "compute_valid": False, "core_out_valid": False,
            "acc_write": {"valid": False}, "retire": False, "comp_busy": False}


def witness(op="Matmul", count=1, latency=2, push=False):
    """Construct interface traces with adjustable arithmetic latency and overlap."""
    starts = [33 + i * 32 if push else 3 + i * 32 for i in range(count)]
    end = starts[-1] + 33 + latency
    samples = [{"cycle": 0, "reset": True}] + [idle(c) for c in range(1, end + 1)]
    if push:
        samples[1]["cmd"] = {"valid": True, "op": "PushWeight"}
        for cycle in range(1, 33):
            samples[cycle]["mreg_req"] = {"valid": True, "mreg": 9, "row": cycle - 1}
        for cycle in range(2, 34):
            samples[cycle]["p0"] = {"valid": True, "op": "PushWeight", "row": cycle - 2,
                                      "boundary": cycle == 33}
    for index, start in enumerate(starts):
        command = {"op": op, "mreg": index + 2, "accsel": index % 2, "wslot": index % 2}
        samples[start]["cmd"] = {"valid": True, **command}
        samples[start]["accept_compute"] = True
        for row in range(32):
            request, feed, write = (samples[start + row], samples[start + row + 1],
                                    samples[start + row + 1 + latency])
            request["mreg_req"] = {"valid": True, "mreg": command["mreg"], "row": row}
            if op == "MatmulAcc":
                request["acc_read"] = {"valid": True, "accsel": command["accsel"], "row": row}
            feed["p0"] = {"valid": True, **command, "row": row, "boundary": row == 31}
            feed["compute_valid"] = True
            write["core_out_valid"] = True
            write["acc_write"] = {"valid": True, "accsel": command["accsel"], "row": row}
            write["retire"] = row == 31
        for cycle in range(start + 1, start + 33 + latency):
            samples[cycle]["comp_busy"] = True
    for cycle in range(1, len(samples)):
        response = samples[cycle - 1].get("mreg_req", {}).get("valid", False)
        samples[cycle]["mreg_resp_valid"] = response
        samples[cycle]["mreg_resp_count"] = int(response)
        samples[cycle]["mreg_resp_banks"] = [samples[cycle - 1]["mreg_req"]["mreg"] & 31] if response else []
    return samples


PERF_HEADER = {**HEADER, "perf_capture": "mxu1_scalar_issue_push_pop_v1"}


def encoded(command):
    kinds = ("PushWeight", "PushAccFP8", "PushAccBF16", "PopAccFP8", "PopAccBF16", "Matmul", "MatmulAcc")
    op = command["op"]
    if op.startswith("Pop"):
        vd, vs1, vs2 = command["mreg"], 0, command["accsel"]
    elif op == "PushWeight":
        vd, vs1, vs2 = command["wslot"], command["mreg"], 0
    else:
        vd, vs1, vs2 = command["accsel"], command["mreg"], command["wslot"]
    return ((2 * kinds.index(op) + 1) << 25) | (vs2 << 19) | (vs1 << 13) | (vd << 7) | 0x77


def add_scalar_issue(sample):
    command = sample["cmd"]
    sample["scalar"] = {"fire": True, "pc": sample["cycle"], "instr": encoded(command)}


def perf_witness(op="Matmul", count=2):
    samples = witness(op=op, count=count, push=True)
    pop_cycle = 33 + (count - 1) * 32 + 4
    while len(samples) <= pop_cycle + 33:
        samples.append(idle(len(samples)))
    for sample in samples[1:]:
        sample.update(scalar={"fire": False}, csr={"valid": False},
                      accept={key: False for key in ("push_p0", "push_p1", "bf16_push", "pop_fp8", "pop_bf16")},
                      weight_write={"valid": False}, acc_store={"valid": False}, mreg_write={"valid": False})
    samples[1]["cmd"].update(mreg=9, accsel=0, wslot=0)
    samples[1]["accept"]["push_p0"] = True
    for row in range(32):
        samples[2 + row]["weight_write"] = {"valid": True, "wslot": 0, "row": row}
        samples[pop_cycle + row]["acc_store"] = {"valid": True, "accsel": (count - 1) % 2, "row": row}
        samples[pop_cycle + row + 1]["mreg_write"] = {"valid": True, "mreg": 10, "row": row}
    samples[pop_cycle]["cmd"] = {"valid": True, "op": "PopAccFP8", "mreg": 10, "accsel": (count - 1) % 2, "wslot": 0}
    samples[pop_cycle]["accept"]["pop_fp8"] = True
    for sample in samples[1:]:
        if sample["cmd"]["valid"]:
            add_scalar_issue(sample)
    return samples


class TraceTest(unittest.TestCase):
    def rejected(self, samples, message):
        with self.assertRaisesRegex(TraceError, message):
            check_trace(HEADER, iter(samples))

    def test_matmul_measures_complete_row_series(self):
        report = check_trace(HEADER, iter(witness()))
        self.assertEqual(report["status"], "trace_obligations_passed")
        transaction = report["transactions"][0]
        self.assertEqual(transaction["requests"]["ages"], list(range(32)))
        self.assertEqual(transaction["feeds"]["first_age"], 1)
        self.assertEqual(transaction["writes"]["first_age"], 3)
        self.assertEqual(transaction["writes"]["uniform_step"], 1)
        self.assertEqual(transaction["retire_age"], 34)

    def test_result_latency_is_measured_instead_of_fixed(self):
        transaction = check_trace(HEADER, witness(latency=7))["transactions"][0]
        self.assertEqual(transaction["writes"]["first_age"], 8)
        self.assertEqual(transaction["feed_to_write_gaps"], [7] * 32)

    def test_back_to_back_compute_on_final_feed_boundary(self):
        report = check_trace(HEADER, witness(count=2))
        self.assertEqual(report["transaction_count"], 2)
        self.assertEqual([t["retire_age"] for t in report["transactions"]], [34, 34])
        self.assertEqual([t["command"]["accsel"] for t in report["transactions"]], [0, 1])

    def test_matmulacc_reads_aligned_accumulator_rows(self):
        report = check_trace(HEADER, witness(op="MatmulAcc"))
        self.assertEqual(report["status"], "trace_obligations_passed")
        samples = witness(op="MatmulAcc")
        samples[4]["acc_read"]["row"] = 0
        self.rejected(samples, "accumulator read address")

    def test_plain_matmul_does_not_read_accumulator(self):
        samples = witness()
        samples[3]["acc_read"] = {"valid": True, "accsel": 0, "row": 0}
        self.rejected(samples, "accumulator read enable")

    def test_unrelated_port_user_does_not_become_compute(self):
        report = check_trace(HEADER, witness(push=True))
        self.assertEqual(report["transaction_count"], 1)
        self.assertEqual(report["unrelated_read_requests"], 32)

    def test_wrong_request_bank_or_repeated_row(self):
        for field, value, error in (("mreg", 7, "wrong mreg"), ("row", 0, "request rows")):
            with self.subTest(field=field):
                samples = witness()
                samples[4]["mreg_req"][field] = value
                self.rejected(samples, error)

    def test_response_must_follow_actual_request(self):
        samples = witness()
        samples[4]["mreg_resp_valid"], samples[4]["mreg_resp_count"] = False, 0
        samples[4]["mreg_resp_banks"] = []
        self.rejected(samples, "response bank routing mismatch")

    def test_response_bank_tags_must_be_one_hot(self):
        samples = witness()
        samples[4]["mreg_resp_count"] = 2
        self.rejected(samples, "not one-hot")

    def test_response_bank_identity_is_checked_for_compute_and_other_reads(self):
        for push, cycle in ((False, 4), (True, 2)):
            with self.subTest(push=push):
                samples = witness(push=push)
                samples[cycle]["mreg_resp_banks"] = [17]
                self.rejected(samples, "response bank routing mismatch")

    def test_response_bank_list_count_and_presence_are_required(self):
        samples = witness()
        samples[4]["mreg_resp_banks"] = []
        self.rejected(samples, "bank list/count mismatch")
        samples = witness()
        del samples[4]["mreg_resp_banks"]
        self.rejected(samples, "missing sampled value mreg_resp_banks")
        samples = witness()
        del samples[4]["mreg_resp_count"]
        self.rejected(samples, "missing sampled value mreg_resp_count")
        samples = witness()
        samples[4]["mreg_resp_banks"] = None
        self.rejected(samples, "unknown/invalid sampled response bank list")

    def test_physical_response_bank_uses_low_five_mreg_bits(self):
        samples = witness()
        for sample in samples:
            for group in ("cmd", "p0", "mreg_req"):
                if "mreg" in sample.get(group, {}):
                    sample[group]["mreg"] += 32
        self.assertEqual(check_trace(HEADER, samples)["status"], "trace_obligations_passed")

    def test_feed_requires_request_and_preserves_metadata(self):
        samples = witness()
        samples[4]["p0"]["wslot"] = 1
        self.rejected(samples, "metadata changed")
        samples = witness()
        samples[4]["p0"]["row"] = 1
        self.rejected(samples, "feed row differs")

    def test_dropped_core_result_is_detected(self):
        samples = witness()
        samples[6]["acc_write"]["valid"] = False
        self.rejected(samples, "core result was dropped")

    def test_result_destination_and_row_order_are_independent(self):
        for field, value, error in (("accsel", 1, "wrong buffer"), ("row", 1, "result rows")):
            with self.subTest(field=field):
                samples = witness()
                samples[6]["acc_write"][field] = value
                self.rejected(samples, error)

    def test_retirement_is_final_write_only(self):
        samples = witness()
        samples[6]["retire"] = True
        self.rejected(samples, "retirement does not coincide")

    def test_early_busy_release_is_detected(self):
        samples = witness()
        samples[4]["comp_busy"] = False
        self.rejected(samples, "compute busy disagrees")

    def test_unknown_or_missing_relevant_samples_are_rejected(self):
        samples = witness()
        samples[4]["mreg_resp_valid"] = "x"
        self.rejected(samples, "unknown/non-Boolean")
        samples = witness()
        del samples[4]["p0"]["row"]
        self.rejected(samples, "missing sampled value")

    def test_unknown_invalid_payload_is_not_used(self):
        samples = witness()
        samples[-1]["p0"]["row"] = "x"
        self.assertEqual(check_trace(HEADER, samples)["status"], "trace_obligations_passed")

    def test_gap_reset_and_truncation_are_rejected(self):
        samples = witness()
        self.rejected(samples[:8] + samples[9:], "missing, repeated or reordered cycle")
        samples[8] = {"cycle": 8, "reset": True}
        self.rejected(samples, "reset interrupted")
        self.rejected(witness()[:-1], "truncated capture")
        self.rejected(witness()[1:], "include reset")

    def test_sampling_convention_and_rows_are_explicit(self):
        for key, value in (("sampling", "post_edge"), ("tile_rows", 16)):
            header = copy.deepcopy(HEADER)
            header[key] = value
            with self.subTest(key=key), self.assertRaises(TraceError):
                check_trace(header, witness())


class PerfTraceTest(unittest.TestCase):
    def rejected(self, samples, message):
        with self.assertRaisesRegex(TraceError, message):
            check_trace(PERF_HEADER, samples)

    def test_scalar_alignment_back_to_back_compute_and_overlapping_pop(self):
        report = check_trace(PERF_HEADER, perf_witness(op="MatmulAcc"))
        self.assertEqual([t["issue_to_accept_gap"] for t in report["transactions"]], [0, 0])
        self.assertEqual(report["transactions"][1]["scalar_relative"]["writes"]["first_age"], 3)
        self.assertEqual(len(report["perf"]["mxu1_commands"]), 4)
        self.assertEqual(report["perf"]["pushes"][0]["writes"]["ages"], list(range(1, 33)))
        self.assertEqual(report["perf"]["pops"][0]["reads"]["ages"], list(range(32)))
        self.assertEqual(report["perf"]["pops"][0]["writes"]["ages"], list(range(1, 33)))

    def test_concurrent_p1_push_does_not_replace_compute_p0_requests(self):
        samples = perf_witness(count=1)
        cycle = 34
        samples[cycle]["cmd"] = {"valid": True, "op": "PushWeight", "mreg": 12, "accsel": 1, "wslot": 1}
        samples[cycle]["accept"]["push_p1"] = True
        add_scalar_issue(samples[cycle])
        for row in range(32):
            samples[cycle + row + 1]["weight_write"] = {"valid": True, "wslot": 1, "row": row}
        report = check_trace(PERF_HEADER, samples)
        self.assertEqual(len(report["perf"]["pushes"]), 2)
        self.assertEqual(report["transactions"][0]["requests"]["ages"], list(range(32)))

    def test_unmatched_scalar_issue_and_changed_operands_fail(self):
        samples = perf_witness()
        samples[33]["scalar"]["fire"] = False
        self.rejected(samples, "instruction and command-valid are unmatched")
        samples = perf_witness()
        samples[33]["scalar"]["instr"] ^= 1 << 13
        self.rejected(samples, "operands disagree")

    def test_unknown_scalar_instruction_fails(self):
        samples = perf_witness()
        samples[33]["scalar"]["instr"] = None
        self.rejected(samples, "unknown/non-integer")

    def test_rejected_push_and_missing_weight_rows_fail(self):
        samples = perf_witness()
        samples[1]["accept"]["push_p0"] = False
        self.rejected(samples, "rejected or multiply accepted")
        samples = perf_witness()
        samples[3]["weight_write"]["row"] = 0
        self.rejected(samples, "weight push rows")

    def test_pop_destination_and_read_address_are_checked(self):
        samples = perf_witness()
        samples[70]["mreg_write"]["mreg"] = 11
        self.rejected(samples, "pop writes wrong mreg")
        samples = perf_witness()
        samples[69]["acc_store"]["row"] = 1
        self.rejected(samples, "pop read row order")

    def test_truncated_pop_does_not_pass_after_compute_drains(self):
        samples = perf_witness()
        self.rejected(samples[:-2], "accepted push/pop rows have not drained")

    def test_csr_events_retain_scalar_identity(self):
        samples = perf_witness()
        sample = samples[-1]
        instruction = (0xC00 << 20) | (2 << 12) | (20 << 7) | 0x73
        sample["scalar"] = {"fire": True, "pc": 123, "instr": instruction}
        sample["csr"] = {"valid": True, "addr": 0xC00, "cmd": 2, "wdata": 0, "rdata": 512}
        report = check_trace(PERF_HEADER, samples)
        self.assertEqual(report["perf"]["csr_events"][0]["pc"], 123)
        self.assertEqual(report["perf"]["csr_events"][0]["rdata"], 512)

    def test_reserved_mxu1_encoding_is_unsupported(self):
        with self.assertRaisesRegex(TraceError, "unsupported issued MXU1"):
            decode_mxu1((15 << 25) | 0x77)


if __name__ == "__main__":
    unittest.main()

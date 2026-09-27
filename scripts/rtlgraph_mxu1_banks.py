#!/usr/bin/env python3
"""Check observed MXU1 P0/P1 reads, independently of proposed issue distances.

The normal mode adds P1 push request/response obligations to the existing full
MXU1 trace checker. --expect-conflict records exactly one observed P0/P1 physical
bank collision from an intentionally invalid, possibly truncated RTL execution;
it does not report successful temporal or functional validation.
"""

import argparse
from collections import deque
import json
from pathlib import Path
import sys

from rtlgraph_mxu1_trace import (TraceError, check_trace, decode_mxu1, flag, field,
                                integer, metadata, require, series)
from rtlgraph_s0 import artifact, checked_path


BANK_SCHEMA = "mxu1_mreg_p0_p1_v1"


def read_requests(sample):
    requests = {}
    for port in ("p0", "p1"):
        prefix = "bank_reads." + port
        valid = flag(sample, prefix + ".valid")
        request = {"valid": valid}
        if valid:
            mreg, row = integer(sample, prefix + ".mreg"), integer(sample, prefix + ".row")
            require(mreg < 64 and row < 32, "bank read address exceeds pinned 64-register/32-row shape")
            request.update(mreg=mreg, row=row, physical_bank=mreg & 31,
                           physical_row=(mreg >> 5) * 32 + row)
        requests[port] = request
    p0 = requests["p0"]
    require(p0["valid"] == flag(sample, "mreg_req.valid"), "MregFile P0 valid differs from sequencer request")
    if p0["valid"]:
        require((p0["mreg"], p0["row"]) == (integer(sample, "mreg_req.mreg"), integer(sample, "mreg_req.row")),
                "MregFile P0 address differs from sequencer request")
    return requests


def collision(requests):
    p0, p1 = requests["p0"], requests["p1"]
    if p0["valid"] and p1["valid"] and p0["physical_bank"] == p1["physical_bank"]:
        return {"physical_bank": p0["physical_bank"], "p0": p0, "p1": p1}
    return None


class BankEvents:
    def __init__(self):
        self.pushes, self.pending = [], deque()
        self.previous = None
        self.overlap_cycles = 0

    def observe(self, sample):
        cycle = integer(sample, "cycle")
        if flag(sample, "reset"):
            require(not self.pending and self.previous is None, "reset interrupted a P1 weight push")
            return
        requests = read_requests(sample)
        hit = collision(requests)
        require(hit is None, f"MXU1 P0/P1 physical-bank read collision: {hit}")
        self.overlap_cycles += int(requests["p0"]["valid"] and requests["p1"]["valid"])

        response_banks = field(sample, "p1_resp_banks")
        require(isinstance(response_banks, list) and all(type(x) is int and 0 <= x < 32 for x in response_banks),
                "unknown/invalid P1 response bank list")
        expected = [self.previous[0]["command"]["mreg"] & 31] if self.previous else []
        require(response_banks == expected, f"P1 response bank routing mismatch: expected {expected}, observed {response_banks}")
        if self.previous:
            push, row = self.previous
            require(flag(sample, "weight_write.valid"), "P1 response did not produce a weight write")
            require(integer(sample, "weight_write.wslot") == push["command"]["wslot"]
                    and integer(sample, "weight_write.row") == row,
                    "P1 response/weight-write destination or row mismatch")
            push["response_cycles"].append(cycle)
        self.previous = None

        if flag(sample, "accept.push_p1"):
            require(flag(sample, "scalar.fire") and flag(sample, "cmd.valid"), "P1 push accepted without scalar issue")
            decoded = decode_mxu1(integer(sample, "scalar.instr"))
            require(decoded is not None and decoded["op"] == "PushWeight", "bank mode requires scalar-issued P1 weight push")
            require(decoded == metadata(sample, "cmd"), "P1 push metadata differs from scalar instruction")
            push = {"accepted_cycle": cycle, "command": decoded, "request_cycles": [], "response_cycles": []}
            self.pushes.append(push)
            self.pending.append(push)

        request = requests["p1"]
        require(request["valid"] == bool(self.pending), "P1 request valid differs from accepted push row stream")
        if request["valid"]:
            push = self.pending[0]
            row = len(push["request_cycles"])
            require(request["mreg"] == push["command"]["mreg"], "P1 request reads wrong scalar-specified mreg")
            require(request["row"] == row and row < 32, "P1 request rows are missing, repeated or reordered")
            push["request_cycles"].append(cycle)
            self.previous = (push, row)
            if row == 31:
                self.pending.popleft()

    def finish(self):
        require(not self.pending and self.previous is None, "truncated P1 request/response stream")
        require(self.pushes, "bank witness has no P1 weight push")
        return {"status": "observed_p0_p1_requests_checked", "different_bank_overlap_cycles": self.overlap_cycles,
                "p1_pushes": [{"accepted_cycle": p["accepted_cycle"], "command": p["command"],
                               "requests": series(p["request_cycles"], p["accepted_cycle"]),
                               "responses": series(p["response_cycles"], p["accepted_cycle"])} for p in self.pushes],
                "limitations": ["Checks MXU1 P0/P1 read contention only; other TRF ports and write contention are outside this monitor.",
                                "Physical address mapping is the pinned mreg[4:0] bank and {mreg[5],row[4:0]} row contract; SRAM data and high-half selection require separate structural/functional evidence."]}


def validate_header(header):
    require(header.get("bank_capture") == BANK_SCHEMA, "missing or unsupported bank capture schema")
    require(header.get("perf_capture") == "mxu1_scalar_issue_push_pop_v1", "bank capture requires perf scalar signals")


def check_banked_trace(header, samples):
    validate_header(header)
    monitor = BankEvents()

    def observed():
        for sample in samples:
            try:
                monitor.observe(sample)
            except TraceError as error:
                raise TraceError(f"cycle {sample.get('cycle')}: {error}") from error
            yield sample

    report = check_trace(header, observed())
    report["banks"] = monitor.finish()
    return report


def check_expected_conflict(header, samples):
    """Read only actual request observations; do not require the invalid run to drain."""
    validate_header(header)
    require(header.get("schema_version") == 1 and header.get("kind") == "atlas-mxu1-cycle-trace"
            and header.get("sampling") == "settled_pre_rising_edge" and header.get("tile_rows") == 32,
            "unsupported bank observation trace")
    previous, reset_seen, count, collisions = None, False, 0, []
    for sample in samples:
        cycle = integer(sample, "cycle")
        require(previous is None or cycle == previous + 1, "missing, repeated or reordered cycle")
        previous, count = cycle, count + 1
        if flag(sample, "reset"):
            reset_seen = True
            continue
        require(reset_seen, "capture must include reset before sampled execution")
        hit = collision(read_requests(sample))
        if hit:
            collisions.append({"cycle": cycle, **hit})
    require(len(collisions) == 1, f"expected exactly one observed bank collision, found {len(collisions)}")
    return {"schema_version": 1, "status": "bank_read_conflict_observed", "sample_count": count,
            "collisions": collisions, "trace_header": header,
            "scope": "Diagnostic for an intentionally invalid execution, possibly stopped by an RTL assertion. Actual simultaneous P0/P1 requests establish contention; no issue-gap hypothesis is asserted.",
            "limitations": ["No successful temporal, drain, arithmetic or functional validation is claimed.",
                            "Only MXU1 P0/P1 read requests are covered; other physical bank users are outside scope."]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("trace", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--expect-conflict", action="store_true")
    args = parser.parse_args()
    trace, output = checked_path(args.trace), checked_path(args.output)
    require(trace != output, "report output must differ from input trace")
    before = artifact(trace)
    try:
        with trace.open() as stream:
            header = json.loads(next(stream))
            check = check_expected_conflict if args.expect_conflict else check_banked_trace
            report = check(header, (json.loads(line) for line in stream))
        require(before == artifact(trace), "trace changed while being checked")
    except (TraceError, json.JSONDecodeError, StopIteration) as error:
        report = {"schema_version": 1, "status": "bank_obligations_failed", "error": str(error)}
    report.update(input=before, checker=artifact(checked_path(__file__)))
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(json.dumps({"status": report["status"], "error": report.get("error"), "output": str(output)}))
    return int(report["status"] == "bank_obligations_failed")


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ValueError) as error:
        raise SystemExit(f"rtlgraph_mxu1_banks: {error}")

#!/usr/bin/env python3
"""Check one MXU1 trace against row/transaction obligations, then measure ages.

JSONL begins with {schema_version:1, kind:"atlas-mxu1-cycle-trace",
sampling:"settled_pre_rising_edge", tile_rows:32}. Subsequent objects contain
cycle, reset, cmd:{valid,op,mreg,accsel,wslot}, accept_compute,
p0:{valid,op,mreg,accsel,wslot,row,boundary}, mreg_req:{valid,mreg,row},
mreg_resp_valid, acc_read:{valid,accsel,row}, compute_valid, core_out_valid,
acc_write:{valid,accsel,row}, retire, comp_busy. Opcodes use MxuOp names.
Registered-bank response provenance requires mreg_resp_count and mreg_resp_banks;
the returned physical bank must equal the previous request's mreg low five bits.

Cycles must be contiguous. During reset only cycle/reset are required; otherwise
all controls and compute-relevant payloads must be known. The source contract requires a
one-cycle TRF response; arithmetic result latency is measured, never prescribed.
This checker validates observed execution, not universal scheduling safety.
"""

import argparse
from collections import deque
import json
from pathlib import Path
import sys

from rtlgraph_s0 import artifact, checked_path


COMPUTE_OPS = frozenset(("Matmul", "MatmulAcc"))
READ_OPS = frozenset(("PushWeight", "PushAccFP8", "PushAccBF16"))
OPS = COMPUTE_OPS | READ_OPS | {"PopAccFP8", "PopAccBF16"}
METADATA = ("op", "mreg", "accsel", "wslot")


class TraceError(ValueError):
    pass


def require(condition, message):
    if not condition:
        raise TraceError(message)


def field(sample, name):
    value = sample
    for component in name.split("."):
        require(isinstance(value, dict) and component in value,
                f"missing sampled value {name}")
        value = value[component]
    return value


def flag(sample, name):
    value = field(sample, name)
    require(type(value) in (bool, int) and value in (False, True),
            f"unknown/non-Boolean sampled value {name}: {value!r}")
    return bool(value)


def integer(sample, name):
    value = field(sample, name)
    require(type(value) is int and value >= 0,
            f"unknown/non-integer sampled value {name}: {value!r}")
    return value


def opcode(sample, prefix):
    value = field(sample, f"{prefix}.op")
    require(isinstance(value, str) and value in OPS,
            f"unknown/unsupported opcode {prefix}.op: {value!r}")
    return value


def metadata(sample, prefix):
    result = {"op": opcode(sample, prefix)}
    for key in METADATA[1:]:
        result[key] = integer(sample, f"{prefix}.{key}")
    require(result["accsel"] < 2 and result["wslot"] < 2,
            f"{prefix}: accumulator/weight selector exceeds the two-buffer contract")
    return result


def series(cycles, accepted):
    ages = [cycle - accepted for cycle in cycles]
    steps = [b - a for a, b in zip(ages, ages[1:])]
    return {"ages": ages, "first_age": ages[0] if ages else None,
            "steps": steps, "uniform_step": steps[0] if steps and len(set(steps)) == 1 else None}


def decode_mxu1(instruction):
    """Pinned Instructions.scala:100-113 and ScalarCore.scala:173-190 fields.

    This decodes issued words independently of the observed sequencer command.
    A different target encoding requires an explicit adapter, never a guess.
    """
    require(0 <= instruction < (1 << 32), "scalar instruction exceeds 32 bits")
    funct7 = instruction >> 25
    if instruction & 0x7f != 0x77 or not funct7 & 1:
        return None
    kinds = ("PushWeight", "PushAccFP8", "PushAccBF16", "PopAccFP8", "PopAccBF16", "Matmul", "MatmulAcc")
    require(funct7 // 2 < len(kinds), "unsupported issued MXU1 encoding")
    op = kinds[funct7 // 2]
    vd, vs1, vs2 = (instruction >> 7) & 63, (instruction >> 13) & 63, (instruction >> 19) & 63
    command = {"op": op, "mreg": vs1, "accsel": vd & 1, "wslot": vd & 1}
    if op in COMPUTE_OPS:
        command["wslot"] = vs2 & 1
    elif op in ("PopAccFP8", "PopAccBF16"):
        command.update(mreg=vd, accsel=vs2 & 1)
    return command


class PerfEvents:
    """Optional scalar alignment plus independent weight-push/FP8-pop row queues."""
    def __init__(self):
        self.issues, self.commands, self.pushes, self.pops, self.csr_events = [], [], [], [], []
        self.pending_push, self.pending_pop = deque(), deque()
        self.previous_store = None

    def reset(self):
        require(not self.pending_push and not self.pending_pop and self.previous_store is None,
                "reset interrupted an accepted push/pop")

    def observe(self, sample, cycle):
        fire = flag(sample, "scalar.fire")
        issue, decoded = None, None
        if fire:
            issue = {"cycle": cycle, "pc": integer(sample, "scalar.pc"),
                     "instruction": integer(sample, "scalar.instr")}
            decoded = decode_mxu1(issue["instruction"])
            self.issues.append(issue)
        command_valid = flag(sample, "cmd.valid")
        require(command_valid == (decoded is not None), "issued MXU1 instruction and command-valid are unmatched")
        accepted = {name: flag(sample, "accept." + name) for name in
                    ("push_p0", "push_p1", "bf16_push", "pop_fp8", "pop_bf16")}
        accepted["compute"] = flag(sample, "accept_compute")
        require(sum(accepted.values()) == int(command_valid), "issued command was rejected or multiply accepted")
        if command_valid:
            require(metadata(sample, "cmd") == decoded, "issued MXU1 operands disagree with sequencer command")
            require(decoded["op"] in COMPUTE_OPS | {"PushWeight", "PopAccFP8"},
                    "unsupported perf instruction family; only compute, weight push and FP8 pop are checked")
            event = {**issue, "command": decoded,
                     "accept_kind": next(name for name, value in accepted.items() if value)}
            self.commands.append(event)
            if decoded["op"] in COMPUTE_OPS:
                require(accepted["compute"], "compute command accepted by the wrong engine path")
            elif decoded["op"] == "PushWeight":
                require(accepted["push_p0"] or accepted["push_p1"], "weight push accepted by the wrong path")
                transaction = {**event, "write_cycles": []}
                self.pushes.append(transaction)
                self.pending_push.append(transaction)
            else:
                require(accepted["pop_fp8"], "FP8 pop accepted by the wrong path")
                transaction = {**event, "read_cycles": [], "write_cycles": []}
                self.pops.append(transaction)
                self.pending_pop.append(transaction)

        if flag(sample, "weight_write.valid"):
            require(bool(self.pending_push), "weight write without an accepted push")
            push = self.pending_push[0]
            row = len(push["write_cycles"])
            require(integer(sample, "weight_write.wslot") == push["command"]["wslot"], "weight write targets wrong slot")
            require(integer(sample, "weight_write.row") == row and row < 32, "weight push rows are missing, repeated or reordered")
            push["write_cycles"].append(cycle)
            if row == 31:
                self.pending_push.popleft()

        # Pop's synchronous accumulator read is tagged from observed requests;
        # the previous request, not current acceptance, identifies each write.
        mreg_write = flag(sample, "mreg_write.valid")
        require(mreg_write == (self.previous_store is not None), "FP8 pop read/write valid alignment mismatch")
        if mreg_write:
            pop, row = self.previous_store
            require(integer(sample, "mreg_write.mreg") == pop["command"]["mreg"], "FP8 pop writes wrong mreg")
            require(integer(sample, "mreg_write.row") == row == len(pop["write_cycles"]), "FP8 pop write row order mismatch")
            pop["write_cycles"].append(cycle)
            if row == 31:
                require(bool(self.pending_pop) and self.pending_pop[0] is pop, "FP8 pop completion order mismatch")
                self.pending_pop.popleft()
        self.previous_store = None
        if flag(sample, "acc_store.valid"):
            pending = [pop for pop in self.pending_pop if len(pop["read_cycles"]) < 32]
            require(bool(pending), "accumulator store read without an accepted FP8 pop")
            pop = pending[0]
            row = len(pop["read_cycles"])
            require(integer(sample, "acc_store.accsel") == pop["command"]["accsel"], "FP8 pop reads wrong accumulator")
            require(integer(sample, "acc_store.row") == row, "FP8 pop read row order mismatch")
            pop["read_cycles"].append(cycle)
            self.previous_store = (pop, row)

        csr_valid = flag(sample, "csr.valid")
        csr_instruction = fire and issue["instruction"] & 0x7f == 0x73 and (issue["instruction"] >> 12) & 7 in (1, 2, 3, 5, 6, 7)
        require(csr_valid == bool(csr_instruction), "scalar CSR instruction and CSR port are unmatched")
        if csr_valid:
            csr = {name: integer(sample, "csr." + name) for name in ("addr", "cmd", "wdata", "rdata")}
            require(csr["addr"] == issue["instruction"] >> 20, "CSR address disagrees with issued instruction")
            self.csr_events.append({**issue, **csr})
        return issue if decoded is not None else None

    def finish(self):
        require(not self.pending_push and not self.pending_pop and self.previous_store is None,
                "truncated capture: accepted push/pop rows have not drained")
        return {"status": "scalar_issue_and_engine_events_matched", "scalar_issues": self.issues,
                "mxu1_commands": self.commands, "csr_events": self.csr_events,
                "pushes": [{"scalar_issue": {key: push[key] for key in ("cycle", "pc", "instruction")},
                            "command": push["command"], "accept_kind": push["accept_kind"],
                            "writes": series(push["write_cycles"], push["cycle"])} for push in self.pushes],
                "pops": [{"scalar_issue": {key: pop[key] for key in ("cycle", "pc", "instruction")},
                          "command": pop["command"], "reads": series(pop["read_cycles"], pop["cycle"]),
                          "writes": series(pop["write_cycles"], pop["cycle"])} for pop in self.pops],
                "limitations": ["Scalar alignment uses the pinned Atlas instruction encoding and direct combinational issue-to-command wiring.",
                                "Weight-push write rows are checked; ReadP1 request/response and data values are not independently checked.",
                                "FP8 pop checks address/order and one-cycle store-read/write alignment, not arithmetic conversion values or accumulator read-during-write visibility."]}


def check_trace(header, samples):
    require(isinstance(header, dict), "trace header must be an object")
    require(header.get("schema_version") == 1 and header.get("kind") == "atlas-mxu1-cycle-trace",
            "unsupported trace schema")
    require(header.get("sampling") == "settled_pre_rising_edge", "unsupported sampling convention")
    require(type(header.get("tile_rows")) is int and header["tile_rows"] == 32,
            "this checker supports the pinned 32-row MXU1 contract")
    rows = header["tile_rows"]
    response_provenance = header.get("response_valid_provenance", "")
    require(isinstance(response_provenance, str), "invalid response provenance")
    bank_tags_required = response_provenance.startswith("reconstructed_from_registered_bank_tags")
    perf = None
    if "perf_capture" in header:
        require(header["perf_capture"] == "mxu1_scalar_issue_push_pop_v1", "unsupported perf capture schema")
        perf = PerfEvents()
    transactions, inflight = [], deque()
    previous_cycle = None
    previous_request = False
    previous_request_bank = None
    previous_compute_request = None
    reset_seen = False
    final_idle = False
    sample_count = 0
    unrelated_requests = 0
    response_bank_samples = 0

    for sample in samples:
        cycle = integer(sample, "cycle")
        try:
            require(previous_cycle is None or cycle == previous_cycle + 1, "missing, repeated or reordered cycle")
            previous_cycle = cycle
            sample_count += 1
            if flag(sample, "reset"):
                require(not inflight and previous_compute_request is None,
                        "reset interrupted an accepted computation")
                reset_seen = True
                previous_request, previous_compute_request = False, None
                previous_request_bank = None
                if perf:
                    perf.reset()
                final_idle = False
                continue
            require(reset_seen, "capture must include reset before sampled execution")
            command_valid = flag(sample, "cmd.valid")
            command_op = opcode(sample, "cmd") if command_valid else None
            accepted = flag(sample, "accept_compute")
            p0_valid = flag(sample, "p0.valid")
            p0_op = opcode(sample, "p0") if p0_valid else None
            require(not p0_valid or p0_op in COMPUTE_OPS | READ_OPS, "non-read operation in ReadP0")
            boundary = flag(sample, "p0.boundary")
            active_compute = p0_valid and p0_op in COMPUTE_OPS
            feed = flag(sample, "compute_valid")
            response = flag(sample, "mreg_resp_valid")
            if "mreg_resp_count" in sample:
                response_count = integer(sample, "mreg_resp_count")
                require(response_count <= 1 and response == (response_count == 1),
                        "TRF response bank tags are not one-hot or disagree with response valid")
            if bank_tags_required or "mreg_resp_banks" in sample:
                response_count = integer(sample, "mreg_resp_count")
                banks = field(sample, "mreg_resp_banks")
                require(isinstance(banks, list) and all(type(bank) is int and 0 <= bank < 32 for bank in banks),
                        "unknown/invalid sampled response bank list")
                require(len(banks) == response_count and response_count <= 1,
                        "TRF response bank list/count mismatch")
                expected_banks = [previous_request_bank] if previous_request else []
                require(banks == expected_banks,
                        f"TRF response bank routing mismatch: expected {expected_banks}, observed {banks}")
                response_bank_samples += 1
            request = flag(sample, "mreg_req.valid")
            acc_read = flag(sample, "acc_read.valid")
            result = flag(sample, "core_out_valid")
            write = flag(sample, "acc_write.valid")
            retire = flag(sample, "retire")
            busy = flag(sample, "comp_busy")
            # Current-cycle acceptance changes the FIFO at the upcoming edge.
            require(busy == bool(active_compute or inflight), "compute busy disagrees with active feed/in-flight work")
            require(feed == active_compute, "compute feed disagrees with ReadP0 command state")
            require(response == previous_request, "TRF request/response valid mismatch at one-cycle latency")
            if not p0_valid:
                require(boundary, "idle ReadP0 is not at its boundary")
            if command_op in COMPUTE_OPS:
                require(accepted, "presented compute command was rejected")
            require(not accepted or (command_valid and command_op in COMPUTE_OPS),
                    "compute accepted without a valid compute command")
            require(not accepted or boundary, "compute accepted while ReadP0 cannot be reused")
            scalar_issue = perf.observe(sample, cycle) if perf else None

            new_transaction = None
            if accepted:
                new_transaction = {"id": len(transactions), "accepted_cycle": cycle,
                                   "command": metadata(sample, "cmd"), "request_cycles": [],
                                   "feed_cycles": [], "write_cycles": [],
                                   "retired_cycle": None, "feed_boundary_cycle": None}
                if scalar_issue is not None:
                    new_transaction["scalar_issue"] = scalar_issue
                transactions.append(new_transaction)
                inflight.append(new_transaction)

            active_transaction = None
            if feed:
                require(previous_compute_request is not None, "compute feed has no preceding compute row request")
                transaction_id, row = previous_compute_request
                active_transaction = transactions[transaction_id]
                require(response, "compute feed consumes an invalid TRF response")
                require(metadata(sample, "p0") == active_transaction["command"],
                        "compute feed command metadata changed")
                require(integer(sample, "p0.row") == row, "compute feed row differs from requested row")
                require(row == len(active_transaction["feed_cycles"]) and row < rows,
                        "compute feed rows are missing, repeated or reordered")
                require(boundary == (row == rows - 1), "ReadP0 boundary disagrees with the compute row")
                active_transaction["feed_cycles"].append(cycle)
                if boundary:
                    active_transaction["feed_boundary_cycle"] = cycle
            else:
                require(previous_compute_request is None, "requested compute row was not fed")

            next_compute_request = None
            if new_transaction is not None:
                request_transaction, request_row = new_transaction, 0
            elif active_compute and not boundary:
                request_transaction = active_transaction
                request_row = len(active_transaction["feed_cycles"])
            else:
                request_transaction, request_row = None, None
            if request_transaction is not None:
                require(request, "missing compute row request")
                require(integer(sample, "mreg_req.mreg") == request_transaction["command"]["mreg"],
                        "compute row request reads the wrong mreg")
                require(integer(sample, "mreg_req.row") == request_row,
                        "compute request rows are missing, repeated or reordered")
                require(request_row == len(request_transaction["request_cycles"]) and request_row < rows,
                        "compute row request count/order mismatch")
                request_transaction["request_cycles"].append(cycle)
                next_compute_request = (request_transaction["id"], request_row)
                require(acc_read == (request_transaction["command"]["op"] == "MatmulAcc"),
                        "accumulator read enable does not match Matmul/MatmulAcc")
                if acc_read:
                    require(integer(sample, "acc_read.accsel") == request_transaction["command"]["accsel"]
                            and integer(sample, "acc_read.row") == request_row,
                            "accumulator read address does not match the compute row")
            else:
                require(not acc_read, "accumulator compute read without a compute row request")
                if request:
                    require((p0_valid and p0_op in READ_OPS) or (command_valid and command_op in READ_OPS),
                            "unattributed ReadP0 request")
                    integer(sample, "mreg_req.mreg")
                    integer(sample, "mreg_req.row")
                    unrelated_requests += 1

            require(result == write, "core result was dropped or accumulator write has no core result")
            if write:
                require(bool(inflight), "accumulator write without an accepted computation")
                transaction = inflight[0]
                row = len(transaction["write_cycles"])
                require(row < len(transaction["feed_cycles"]), "result precedes its corresponding compute feed")
                require(integer(sample, "acc_write.accsel") == transaction["command"]["accsel"],
                        "accumulator result targets the wrong buffer")
                require(integer(sample, "acc_write.row") == row and row < rows,
                        "accumulator result rows are missing, repeated or reordered")
                require(retire == (row == rows - 1), "retirement does not coincide with the final row write")
                transaction["write_cycles"].append(cycle)
                if retire:
                    transaction["retired_cycle"] = cycle
                    inflight.popleft()
            else:
                require(not retire, "retirement without a row write")
            previous_request, previous_compute_request = request, next_compute_request
            previous_request_bank = integer(sample, "mreg_req.mreg") & 31 if request else None
            final_idle = not (accepted or active_compute or inflight or busy or result)
        except TraceError as error:
            raise TraceError(f"cycle {cycle}: {error}") from error

    require(sample_count > 0 and reset_seen, "empty trace or missing reset")
    require(transactions, "trace contains no accepted compute instruction")
    require(not inflight and previous_compute_request is None and final_idle,
            "truncated capture: accepted computations must drain through a compute-idle sample")
    measured = []
    for transaction in transactions:
        require(all(len(transaction[key]) == rows for key in ("request_cycles", "feed_cycles", "write_cycles")),
                f"transaction {transaction['id']}: incomplete row counts")
        accepted = transaction["accepted_cycle"]
        measured.append({"id": transaction["id"], "accepted_cycle": accepted,
                         "command": transaction["command"],
                         "requests": series(transaction["request_cycles"], accepted),
                         "feeds": series(transaction["feed_cycles"], accepted),
                         "writes": series(transaction["write_cycles"], accepted),
                         "retire_age": transaction["retired_cycle"] - accepted,
                         "feed_boundary_age": transaction["feed_boundary_cycle"] - accepted,
                         "request_to_feed_gaps": [f - r for r, f in zip(transaction["request_cycles"],
                                                                       transaction["feed_cycles"])],
                         "feed_to_write_gaps": [w - f for w, f in zip(transaction["write_cycles"],
                                                                     transaction["feed_cycles"])]})
        if "scalar_issue" in transaction:
            issued_cycle = transaction["scalar_issue"]["cycle"]
            measured[-1].update(scalar_issue=transaction["scalar_issue"], issue_to_accept_gap=accepted - issued_cycle,
                                scalar_relative={"requests": series(transaction["request_cycles"], issued_cycle),
                                                 "feeds": series(transaction["feed_cycles"], issued_cycle),
                                                 "writes": series(transaction["write_cycles"], issued_cycle),
                                                 "retire_age": transaction["retired_cycle"] - issued_cycle})
    report = {"schema_version": 1, "status": "trace_obligations_passed", "sample_count": sample_count,
            "transaction_count": len(transactions), "unrelated_read_requests": unrelated_requests,
            "response_bank_samples_checked": response_bank_samples,
            "sampling": header["sampling"], "trace_header": header, "transactions": measured,
            "obligations": ["accepted command and feed metadata", "ordered complete operand/result rows",
                            "one-cycle TRF request/response/feed alignment", "accumulator read mode/address",
                            "in-order accumulator destination routing", "final-write retirement", "compute busy/drain"],
            "limitations": [
                "This is evidence for the captured execution only; it does not prove a scheduling distance universally safe.",
                "The pinned contract assumes 32 rows, 32 physical banks selected by mreg[4:0], one-cycle TRF responses and in-order compute writeback.",
                "Arithmetic result latency is measured from this trace, not asserted from a proposed timing table.",
                "Valid signals and addresses do not independently validate arithmetic values or same-cycle memory visibility.",
                "Other ReadP0 users are distinguished but their complete instruction contracts are not checked."]}
    if perf:
        report["perf"] = perf.finish()
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("trace", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    trace, output = checked_path(args.trace), checked_path(args.output)
    require(trace != output, "report output must differ from input trace")
    before = artifact(trace)
    try:
        with trace.open() as stream:
            header = json.loads(next(stream))
            report = check_trace(header, (json.loads(line) for line in stream))
        require(before == artifact(trace), "trace changed while being checked")
    except (TraceError, json.JSONDecodeError, StopIteration) as error:
        report = {"schema_version": 1, "status": "trace_obligations_failed", "error": str(error)}
    report.update(input=before, checker=artifact(checked_path(__file__)))
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, sort_keys=True, indent=2) + "\n")
    print(json.dumps({"status": report["status"], "output": str(output),
                      "transaction_count": report.get("transaction_count"), "error": report.get("error")}))
    return 0 if report["status"] == "trace_obligations_passed" else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ValueError) as error:
        print(f"rtlgraph_mxu1_trace: {error}", file=sys.stderr)
        sys.exit(2)

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

            new_transaction = None
            if accepted:
                new_transaction = {"id": len(transactions), "accepted_cycle": cycle,
                                   "command": metadata(sample, "cmd"), "request_cycles": [],
                                   "feed_cycles": [], "write_cycles": [],
                                   "retired_cycle": None, "feed_boundary_cycle": None}
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
    return {"schema_version": 1, "status": "trace_obligations_passed", "sample_count": sample_count,
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

#!/usr/bin/env python3
"""Bind the four finite K64 bank witnesses to functional and waveform evidence.

A simulator failure is not an expected-negative result by itself: the recorded
RTL assertion and observed P0/P1 addresses must identify the intended collision.
This reporter does not infer a universal instruction spacing rule.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from rtlgraph_s0 import artifact, checked_path


CASES = {"alias_safe", "alias_invalid", "bank_control", "alias_repaired"}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def verified(record):
    current = artifact(checked_path(record["path"]))
    require(all(current[key] == record[key] for key in ("sha256", "bytes")),
            f"artifact changed: {record['path']}")
    return current


def same_content(left, right):
    return all(left[key] == right[key] for key in ("sha256", "bytes"))


def fixture_data(fixture):
    beats = {row["word_offset"]: int(row["data"], 16) for row in fixture["dram_preloads"]}
    a, b = [beats[i] for i in range(32)], [beats[192 + i] for i in range(32)]
    return {"a00_dram_address": "0x90000000", "b10_transposed_dram_address": "0x90001800",
            "beat_bytes": 32, "beats_compared": 32,
            "different_beats": sum(x != y for x, y in zip(a, b)),
            "different_bytes": sum(x != y for left, right in zip(a, b)
                                   for x, y in zip(left.to_bytes(32, "little"), right.to_bytes(32, "little"))),
            "collision_target_rows_differ": a[31] != b[0],
            "address_provenance": "Fixed K64 addresses inspected in recorded perf_mm_mxu1_64x64x64.S setup: A00 lines76–82 and B10_T lines130–136; this reporter is not a general load-address analysis.",
            "scope": "Compares original preload bytes for m0 and renamed B10 source; functional output checks exercise their use but do not prove all SRAM address/data cases."}


def evaluate_case(name, fixture_case, replay, bank_report, simulation_log, assertion):
    require(replay.get("kind") == "rtlgraph-perf-replay" and replay.get("config") == "EE290SimConfig",
            "unsupported replay kind/config")
    require(bool(replay.get("finished_utc")) and replay.get("trace_validation", {}).get("status") == "PARSED",
            f"{name}: replay/capture is not complete and parsed")
    require(same_content(fixture_case["assembly"], replay["inputs"]["assembly"]),
            f"{name}: replay used different assembly")
    require(same_content(replay["trace"], bank_report["trace_header"]["vcd"]),
            f"{name}: bank report used a different waveform")
    result = {"model_check_returncode": fixture_case["model_check"]["returncode"],
              "functional_status": replay.get("result", {}).get("status"),
              "waveform_status": bank_report["status"]}
    if name == "alias_invalid":
        require(result["model_check_returncode"] == 1, "negative was not rejected by compiler")
        require(replay.get("status") == "failed" and result["functional_status"] != "PASS",
                "expected-negative replay did not fail")
        require(assertion in simulation_log, "negative failed without the expected RTL bank assertion")
        require(bank_report["status"] == "bank_read_conflict_observed", "negative lacks observed bank collision")
        hits = bank_report["collisions"]
        require(len(hits) == 1, "negative must have exactly one observed collision")
        hit = hits[0]
        require(hit["physical_bank"] == 0, "negative collided in a different physical bank")
        for port, mreg, row, physical_row in (("p0", 0, 31, 31), ("p1", 32, 0, 32)):
            observed = hit[port]
            require(observed == {"valid": True, "mreg": mreg, "row": row,
                                 "physical_bank": 0, "physical_row": physical_row},
                    f"negative collided on different {port} addresses")
        result.update(status="intended_bank_conflict_observed", collision=hit,
                      rtl_assertion=assertion,
                      fixture_diagnostic={"scheduled_target_issue_gap": fixture_case.get("first_compute_to_push_gap"),
                                          "hypothesized_body_collision_cycle": fixture_case.get("hypothesized_collision", {}).get("body_cycle"),
                                          "cycle_origins": "collision.cycle is the captured pre-edge cycle; the fixture body cycle starts at its first measured-body instruction, excluding the opening CSR. The scheduled gap is exposure metadata, not the collision check."},
                      scope="Expected-invalid diagnostic; no functional success or complete temporal validation claimed.")
        return result

    require(result["model_check_returncode"] == 0, f"{name}: compiler did not accept witness")
    require(replay.get("status") == "passed" and result["functional_status"] == "PASS",
            f"{name}: functional replay failed")
    functional = replay["result"]
    require(functional.get("checked_words") == 1024, f"{name}: incomplete golden comparison")
    metrics = functional["metrics"]
    require(metrics.get("dbg0") == 1 and metrics.get("csr_status", 0) & 7 == 5,
            f"{name}: missing completion/status evidence")
    require(bank_report["status"] == "trace_obligations_passed"
            and bank_report["banks"]["status"] == "observed_p0_p1_requests_checked",
            f"{name}: incomplete bank/temporal obligations")
    require(bank_report.get("transaction_count") == 8 and len(bank_report["perf"]["pushes"]) == 4
            and len(bank_report["perf"]["pops"]) == 4 and len(bank_report["banks"]["p1_pushes"]) == 4,
            f"{name}: wrong K64 transaction exposure")
    commands = bank_report["perf"]["mxu1_commands"]
    compute = next(c for c in commands if c["command"]["op"] == "Matmul")
    target = fixture_case["target_pair"]["push_mreg"]
    pushes = [c for c in commands if c["command"]["op"] == "PushWeight" and c["command"]["mreg"] == target]
    require(len(pushes) == 1 and compute["command"]["mreg"] == 0, f"{name}: target pair not exercised")
    gap = pushes[0]["cycle"] - compute["cycle"]
    require(gap == (31 if name == "bank_control" else 32), f"{name}: wrong tested issue gap")
    result.update(status="finite_positive_witness_passed", checked_words=1024,
                  dbg1_cycles=metrics["dbg1_cycles"], observed_target_issue_gap=gap,
                  different_bank_overlap_cycles=bank_report["banks"]["different_bank_overlap_cycles"],
                  scope="This captured K64 execution only; the observed gap is exposure metadata, not a universal safety bound.")
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixtures", type=Path, required=True)
    parser.add_argument("--case", nargs=3, action="append", metavar=("NAME", "REPLAY", "BANK_REPORT"), required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    names = [entry[0] for entry in args.case]
    require(len(names) == 4 and set(names) == CASES, "supply each of the four distinct named cases")
    output = checked_path(args.output)
    require(not output.exists(), "output must be a new file")
    fixture_path = checked_path(args.fixtures)
    fixtures = json.loads(fixture_path.read_text())
    require(fixtures["status"] == "fixtures_and_model_preflight_complete", "fixtures are incomplete")
    golden = verified(fixtures["inputs"]["golden_fixture"])
    report = {"schema": "atlas.rtlgraph.bank-results.v1", "status": "finite_witnesses_validated",
              "fixtures": artifact(fixture_path), "reporter": artifact(checked_path(__file__)),
              "fixture_data": fixture_data(json.loads(Path(golden["path"]).read_text())), "cases": {},
              "limitations": ["Finite simulation evidence, not a proof of all states, aliases, resource users or instruction spacings.",
                              "Only MXU1 P0/P1 reads are monitored; other MREG ports and writes are outside this monitor.",
                              "Cached simulator source-to-binary build lineage remains unverified."]}
    for name, replay_arg, banks_arg in args.case:
        replay_path, banks_path = checked_path(replay_arg), checked_path(banks_arg)
        replay, banks = json.loads(replay_path.read_text()), json.loads(banks_path.read_text())
        case = fixtures["cases"][name]
        require(same_content(golden, replay["inputs"]["golden_fixture"]), f"{name}: different golden fixture")
        for entry in (case["assembly"], case["compiler_body"], case["model_check"]["log"],
                      replay["inputs"]["assembly"], replay["trace"], banks["input"], banks["checker"]):
            verified(entry)
        commands = [c for c in replay["commands"] if Path(c["log"]["path"]).name == "simulation.log"]
        require(len(commands) == 1, f"{name}: ambiguous simulator log")
        log = verified(commands[0]["log"])
        result = evaluate_case(name, case, replay, banks, Path(log["path"]).read_text(errors="replace"),
                               fixtures["expected_negative_assertion"])
        result.update(replay=artifact(replay_path), bank_report=artifact(banks_path), simulation_log=log)
        report["cases"][name] = result
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(json.dumps({"status": report["status"], "output": str(output)}))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ValueError, KeyError, StopIteration) as error:
        raise SystemExit(f"rtlgraph_conflict_report: {error}")

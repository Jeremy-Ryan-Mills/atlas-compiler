#!/usr/bin/env python3
"""Locate MXU1 events in typed CIRCT SSA without inferring instruction ages.

The acceptance check exhausts the Boolean function of seven named cutpoints.
It does not prove their reachable values, temporal behavior, or issue safety.
All other checks establish structural connectivity only.
"""

import argparse
from collections import defaultdict, deque
import itertools
import json
from pathlib import Path
import sys

from rtlgraph_query import (digest, find_instance, identity_chain, instance_value,
                            op_summary, run)
from rtlgraph_s0 import artifact, checked_path


GUARDS = ("io_cmd_valid", "isCompute", "p0Boundary", "fifoFull",
          "accReuseHazard", "computeWslotHazard", "computePushAccHazard")
EVENTS = {
    "command_present": "io_cmd_valid",
    "compute_accepted": "acceptCompute",
    "operand_read_request": "io_mregReadReq0_valid",
    "operand_read_response": "io_mregReadResp0_valid",
    "accumulator_read_request": "io_accComputeReadEn",
    "compute_feed": "io_compute_valid",
    "core_result": "io_coreOut_valid",
    "accumulator_write_request": "io_accComputeWrite_valid",
    "feed_port_reusable": "p0Boundary",
    "inflight_head_retired": "popThisCycle",
    "compute_busy": "io_compBusy",
}
CONTEXT = (
    "io_cmd_bits_op", "io_cmd_bits_mregId", "io_cmd_bits_accSel",
    "io_cmd_bits_weightSlot", "io_mregReadReq0_bits_mregId",
    "io_mregReadReq0_bits_row", "io_accComputeReadAddr_accSel",
    "io_accComputeReadAddr_rowIdx", "io_accComputeWrite_bits_accSel",
    "io_accComputeWrite_bits_rowIdx", "p0CmdValid", "p0Row", "ifHead",
    "ifTail", "inflightValid_0", "inflightValid_1",
    "io_weightWriteReq_valid", "io_weightWriteReq_bits_weightSlot",
    "io_weightWriteReq_bits_laneIdx",
)
# Only register operations exercised by this extractor are accepted as state
# boundaries. Other Seq operations (including clock adapters) stay unsupported.
REGISTER_OPS = frozenset(("seq.compreg", "seq.firreg"))


class Graph:
    def __init__(self, module):
        self.module = module
        self.definitions = {v: op for op in module["operations"] for v in op["results"]}
        self.inputs = {p["value"]: p for p in module["ports"] if p["direction"] == "input"}
        self.uses = defaultdict(list)
        for op in module["operations"]:
            for index, value in enumerate(op["operands"]):
                self.uses[value].append((op, index))

    def named(self, name):
        ports = [p for p in self.module["ports"] if p["name"] == name]
        if len(ports) == 1:
            return ports[0]["value"]
        if ports:
            raise ValueError(f"{self.module['name']}.{name}: ambiguous port")
        values = set()
        values.update(v for op in self.module["operations"]
                      if name in (op["attributes"].get("name"),
                                  op["attributes"].get("sv.namehint"))
                      for v in op["results"])
        if len(values) != 1:
            raise ValueError(f"{self.module['name']}.{name}: missing or ambiguous SSA value")
        return next(iter(values))

    @staticmethod
    def traversable(op):
        return (len(op["results"]) == 1 and not op["has_regions"]
                and "instance" not in op and not op["kind"].startswith("seq.")
                and (op["combinational"] or op["identity_wire"]))

    def cone(self, root, stops=None):
        """Backward combinational DAG, preserving SSA edges and explicit cutpoints."""
        stops = stops or {}
        pending, seen, nodes, terminals = [root], set(), [], []
        while pending:
            value = pending.pop()
            if value in seen:
                continue
            seen.add(value)
            op = self.definitions.get(value)
            reason = ("named_cutpoint" if value in stops else
                      "input_port" if value in self.inputs else
                      "undefined_value" if op is None else
                      "state" if op["kind"] in REGISTER_OPS else
                      "unsupported_operation" if not self.traversable(op) else None)
            if reason:
                terminal = {"value": value, "reason": reason}
                if value in stops:
                    terminal["name"] = stops[value]
                if value in self.inputs:
                    terminal["port"] = self.inputs[value]
                if op:
                    terminal["operation"] = op
                terminals.append(terminal)
            else:
                nodes.append(op)
                pending.extend(reversed(op["operands"]))
        unsupported = any(t["reason"] in ("undefined_value", "unsupported_operation")
                          for t in terminals)
        return {"root_value": root, "operations": nodes, "terminals": terminals,
                "unsupported": unsupported,
                "status": "unresolved" if unsupported else "combinational_cone_extracted"}

    def path(self, start, goal):
        pending, previous = deque([start]), {start: None}
        while pending and goal not in previous:
            value = pending.popleft()
            for op, index in self.uses[value]:
                if self.traversable(op):
                    successor = op["results"][0]
                    if successor not in previous:
                        previous[successor] = (value, op, index)
                        pending.append(successor)
        if goal not in previous:
            return None
        steps, cursor = [], goal
        while previous[cursor] is not None:
            value, op, index = previous[cursor]
            steps.append({"from_value": value, "to_value": cursor,
                          "operand_index": index, "operation": op_summary(op)})
            cursor = value
        return list(reversed(steps))

    def state_consumers(self, start):
        """Report paths into register operands, without assigning operand timing."""
        consumers = []
        for op in self.module["operations"]:
            if op["kind"] not in REGISTER_OPS:
                continue
            for index, value in enumerate(op["operands"]):
                path = self.path(start, value)
                if path is not None:
                    consumers.append({"state_operation": op_summary(op),
                                      "operand_index": index, "operand_value": value,
                                      "path": path})
        return consumers

    def boolean(self, root, inputs):
        """Recover only explicit i1 Boolean operations; reject unsupported semantics."""
        active, memo = set(), {}

        def visit(value):
            op = self.definitions.get(value)
            value_type = (self.inputs[value]["type"] if value in self.inputs else
                          op["result_types"][op["results"].index(value)] if op else None)
            if value_type != "i1":
                raise ValueError(f"Boolean value {value} is not i1")
            if value in inputs:
                return {"input": inputs[value]}
            if value in memo:
                return memo[value]
            if value in active:
                raise ValueError("cycle in Boolean cone")
            if op is None or op["result_types"] != ["i1"] or not self.traversable(op):
                raise ValueError(f"unsupported Boolean definition for {value}")
            active.add(value)
            kind = op["kind"]
            if op["identity_wire"] and len(op["operands"]) == 1:
                result = visit(op["operands"][0])
            elif kind == "hw.constant" and op["attributes"].get("value") in ("true", "false"):
                result = {"constant": op["attributes"]["value"] == "true"}
            elif kind in ("comb.and", "comb.or", "comb.xor", "comb.mux"):
                args = [visit(v) for v in op["operands"]]
                if kind == "comb.mux" and len(args) != 3:
                    raise ValueError("invalid Boolean mux")
                result = {"op": kind, "args": args}
            else:
                raise ValueError(f"unsupported Boolean operation: {kind}")
            active.remove(value)
            memo[value] = result
            return result

        return visit(root)


def evaluate(expression, assignment):
    if "input" in expression:
        return assignment[expression["input"]]
    if "constant" in expression:
        return expression["constant"]
    args = [evaluate(arg, assignment) for arg in expression["args"]]
    kind = expression["op"]
    if kind == "comb.and":
        return all(args)
    if kind == "comb.or":
        return any(args)
    if kind == "comb.xor":
        return sum(args) % 2 == 1
    if kind == "comb.mux":
        return args[1] if args[0] else args[2]
    raise ValueError(f"unsupported Boolean expression: {kind}")


def acceptance(graph):
    root = graph.named("acceptCompute")
    stops = {graph.named(name): name for name in GUARDS}
    if len(stops) != len(GUARDS):
        raise ValueError("acceptance guard cutpoints are not distinct")
    expression = graph.boolean(root, stops)
    counterexamples = []
    for bits in itertools.product((False, True), repeat=len(GUARDS)):
        assignment = dict(zip(GUARDS, bits))
        expected = all(bits[:3]) and not any(bits[3:])
        actual = evaluate(expression, assignment)
        if actual != expected:
            counterexamples.append({"assignment": assignment, "actual": actual, "expected": expected})
    return {"status": "guard_function_matches" if not counterexamples else "guard_function_mismatch",
            "scope": "Combinational Boolean equivalence over named cutpoints; no reachable-state or temporal proof.",
            "expected": "io_cmd_valid && isCompute && p0Boundary && !fifoFull && !accReuseHazard && !computeWslotHazard && !computePushAccHazard",
            "assignments_checked": 2 ** len(GUARDS), "expression": expression,
            "counterexamples": counterexamples, "cone": graph.cone(root, stops)}


def wrapper_wiring(module):
    checks = []
    for source_instance, source_port, sink_instance, sink_port in (
        ("seq", "io_compute_valid", "core", "io_compute_valid"),
        ("core", "io_out_valid", "seq", "io_coreOut_valid"),
        ("seq", "io_accComputeWrite_valid", "accBuf", "io_computeWriteReq_valid"),
        ("seq", "io_accComputeReadEn", "accBuf", "io_computeReadEn"),
    ):
        source_op, sink_op = find_instance(module, source_instance), find_instance(module, sink_instance)
        source = instance_value(source_op, source_port, "output")
        sink = instance_value(sink_op, sink_port, "input")
        path = identity_chain(module, source, sink)
        checks.append({"source": f"{source_instance}.{source_port}",
                       "sink": f"{sink_instance}.{sink_port}", "source_value": source,
                       "sink_value": sink, "identity_path": path,
                       "source_location": source_op["location"], "sink_location": sink_op["location"]})
    return {"status": "direct_wiring_confirmed" if all(c["identity_path"] is not None for c in checks)
            else "unresolved", "checks": checks}


def analyze(export):
    modules = {m["name"]: m for m in export["modules"]}
    graph = Graph(modules["InnerProductTreesSequencer"])
    guards = acceptance(graph)
    events = {name: {"signal": signal, **graph.cone(graph.named(signal))}
              for name, signal in EVENTS.items()}
    context = {name: graph.cone(graph.named(name)) for name in CONTEXT}
    paths = []
    for source, sink in (("acceptCompute", "io_mregReadReq0_valid"),
                         ("acceptCompute", "io_accComputeReadEn"),
                         ("p0CmdValid", "io_compute_valid"),
                         ("io_coreOut_valid", "io_accComputeWrite_valid"),
                         ("io_coreOut_valid", "popThisCycle"),
                         ("p0Boundary", "io_mregReadReq0_valid")):
        path = graph.path(graph.named(source), graph.named(sink))
        paths.append({"source": source, "sink": sink, "path": path,
                      "status": "structural_path_found" if path is not None else "unresolved"})
    state_uses = {name: graph.state_consumers(graph.named(name))
                  for name in ("acceptCompute", "popThisCycle")}
    wiring = wrapper_wiring(modules["InnerProductTreesTop"])
    success = (guards["status"] == "guard_function_matches"
               and wiring["status"] == "direct_wiring_confirmed"
               and all(path["path"] is not None for path in paths)
               and not any(cone["unsupported"] for cone in [*events.values(), *context.values()])
               and all(state_uses.values()))
    return {"schema_version": 1, "status": "event_checks_passed" if success else "unresolved",
            "module": graph.module["name"], "acceptance": guards, "events": events,
            "context": context, "structural_paths": paths, "state_operand_uses": state_uses,
            "wrapper_wiring": wiring,
            "limitations": [
                "Event names identify source-level intent; request signals are not proof of memory commit or visibility.",
                "A structural dependency need not be sensitizable; event cones stop at state, instances and unsupported operations.",
                "Register operand uses preserve indices, without inferring state-transition or clock/reset semantics.",
                "Port boundary/reuse, final in-flight writeback and whole-engine busy are distinct signals.",
                "No cycle ages, same-cycle visibility, instruction attribution, resource capacities or scheduling distances are inferred.",
                "The Boolean acceptance check assumes two-valued cutpoints and absence of external force overrides."]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="HW/Comb/Seq IR")
    parser.add_argument("--exporter", type=Path, required=True, help="Existing typed rtlgraph_export executable")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    source, exporter, output = map(checked_path, (args.input, args.exporter, args.output))
    before = {"input": artifact(source), "exporter": artifact(exporter)}
    typed = json.loads(run([str(exporter), str(source), "InnerProductTreesSequencer",
                           "InnerProductTreesTop"], timeout=120).stdout)
    if typed["missing_modules"]:
        raise ValueError(f"Missing modules: {typed['missing_modules']}")
    report = analyze(typed)
    if before != {"input": artifact(source), "exporter": artifact(exporter)}:
        raise ValueError("IR or exporter changed during extraction")
    report.update(before)
    report["provenance_scope"] = "Exact IR/exporter/script hashes; no new source-build or exporter-build equivalence claim."
    report["scripts_sha256"] = {name: digest(checked_path(__file__).parent / name)
                                for name in ("rtlgraph_mxu1.py", "rtlgraph_query.py", "rtlgraph_s0.py")}
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, sort_keys=True, indent=2) + "\n")
    print(json.dumps({"status": report["status"], "output": str(output),
                      "events": len(report["events"]), "acceptance_assignments": report["acceptance"]["assignments_checked"]}))
    return 0 if report["status"] == "event_checks_passed" else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (KeyError, OSError, RuntimeError, ValueError) as error:
        print(f"rtlgraph_mxu1: {error}", file=sys.stderr)
        sys.exit(2)

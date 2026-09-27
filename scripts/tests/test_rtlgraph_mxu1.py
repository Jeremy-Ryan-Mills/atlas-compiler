#!/usr/bin/env python3
"""Exercise Boolean polarity, conservative state cutpoints, and port identity.

Usage: python scripts/tests/test_rtlgraph_mxu1.py PATH/rtlgraph_export
"""

import copy
import json
from pathlib import Path
import subprocess
import sys
import unittest

sys.path.insert(0, str(Path(__file__).absolute().parents[1]))
from rtlgraph_mxu1 import Graph, acceptance, evaluate, wrapper_wiring
from rtlgraph_s0 import checked_path


class EventsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixture = checked_path(Path(__file__).parent / "rtlgraph-mxu1.mlir")
        export = json.loads(subprocess.check_output(
            [str(EXPORTER), str(fixture), "AcceptanceTest", "WrapperFixture", "ClockAdapters"], text=True))
        cls.modules = {m["name"]: m for m in export["modules"]}

    def setUp(self):
        self.graph = Graph(copy.deepcopy(self.modules["AcceptanceTest"]))

    def test_guard_matches_and_has_single_accepting_assignment(self):
        report = acceptance(self.graph)
        self.assertEqual(report["status"], "guard_function_matches")
        self.assertEqual(report["assignments_checked"], 128)
        self.assertEqual(len(report["cone"]["terminals"]), 7)

    def test_reversed_fifo_guard_fails(self):
        fifo = self.graph.named("fifoFull")
        invert = next(op for op in self.graph.module["operations"]
                      if op["kind"] == "comb.xor" and fifo in op["operands"])
        guard = next(op for op in self.graph.module["operations"] if op["kind"] == "comb.and")
        guard["operands"][guard["operands"].index(invert["results"][0])] = fifo
        report = acceptance(self.graph)
        self.assertEqual(report["status"], "guard_function_mismatch")
        self.assertTrue(report["counterexamples"])

    def test_missing_named_guard_fails(self):
        self.graph.module["ports"] = [p for p in self.graph.module["ports"] if p["name"] != "fifoFull"]
        with self.assertRaisesRegex(ValueError, "missing or ambiguous"):
            acceptance(self.graph)

    def test_unsupported_boolean_function_fails(self):
        op = next(op for op in self.graph.module["operations"] if op["kind"] == "comb.and")
        op["kind"] = "comb.icmp"
        with self.assertRaisesRegex(ValueError, "unsupported Boolean operation"):
            acceptance(self.graph)

    def test_wide_boolean_cutpoint_fails(self):
        port = next(p for p in self.graph.module["ports"] if p["name"] == "fifoFull")
        port["type"] = "i8"
        with self.assertRaisesRegex(ValueError, "not i1"):
            acceptance(self.graph)

    def test_unsupported_cone_is_unresolved(self):
        op = next(op for op in self.graph.module["operations"] if op["kind"] == "comb.and")
        op["kind"], op["combinational"] = "test.unsupported", False
        cone = self.graph.cone(self.graph.named("acceptCompute"))
        self.assertEqual(cone["status"], "unresolved")
        self.assertTrue(cone["unsupported"])
        self.assertEqual(cone["terminals"][0]["reason"], "unsupported_operation")

    def test_state_cuts_paths_and_cones(self):
        accept, feed = self.graph.named("acceptCompute"), self.graph.named("fed")
        self.assertIsNone(self.graph.path(accept, feed))
        cone = self.graph.cone(feed)
        self.assertEqual([t["reason"] for t in cone["terminals"]], ["state"])
        consumers = self.graph.state_consumers(accept)
        self.assertEqual(len(consumers), 1)
        self.assertEqual(consumers[0]["operand_index"], 0)
        self.assertEqual(consumers[0]["path"], [])

    def test_clock_adapter_cones_are_unsupported(self):
        graph = Graph(self.modules["ClockAdapters"])
        for port, kind in (("clock", "seq.to_clock"), ("result", "seq.from_clock")):
            cone = graph.cone(graph.named(port))
            self.assertEqual(cone["status"], "unresolved")
            self.assertTrue(cone["unsupported"])
            self.assertEqual(cone["terminals"][0]["reason"], "unsupported_operation")
            self.assertEqual(cone["terminals"][0]["operation"]["kind"], kind)

    def test_clock_adapters_are_not_state_consumers(self):
        graph = Graph(self.modules["ClockAdapters"])
        self.assertEqual(graph.state_consumers(graph.named("raw")), [])
        self.assertEqual(graph.state_consumers(graph.named("clock")), [])

    def test_input_port_takes_priority_over_identity_wire_name(self):
        port = self.graph.named("io_cmd_valid")
        wire = next(op for op in self.graph.module["operations"] if op["identity_wire"])
        wire["attributes"]["name"] = "io_cmd_valid"
        self.assertEqual(self.graph.named("io_cmd_valid"), port)

    def test_multi_result_wiring_and_wrong_result(self):
        module = copy.deepcopy(self.modules["WrapperFixture"])
        report = wrapper_wiring(module)
        self.assertEqual(report["status"], "direct_wiring_confirmed")
        self.assertTrue(report["checks"][1]["source_value"].endswith(".r1"))
        core = next(op for op in module["operations"] if op.get("instance", {}).get("name") == "core")
        seq = next(op for op in module["operations"] if op.get("instance", {}).get("name") == "seq")
        index = seq["instance"]["input_names"].index("io_coreOut_valid")
        seq["operands"][index] = core["results"][0]
        self.assertEqual(wrapper_wiring(module)["status"], "unresolved")

    def test_mux_evaluation(self):
        expression = {"op": "comb.mux", "args": [{"input": "select"},
                                                   {"constant": True}, {"constant": False}]}
        self.assertTrue(evaluate(expression, {"select": True}))
        self.assertFalse(evaluate(expression, {"select": False}))


if __name__ == "__main__":
    EXPORTER = checked_path(sys.argv.pop(1))
    unittest.main()

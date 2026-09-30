#!/usr/bin/env python3
"""Pass the connected typed export as the first argument for hardware checks."""
import copy
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from rtlgraph_xlu_connected import Graph, Mreg, connected_case, frontend_assertions, routing, tracker_mapping

DOCUMENT = json.loads(Path(sys.argv.pop(1)).read_text()) if len(sys.argv) > 1 and not sys.argv[1].startswith('-') else None
MODULES = {m['name']: m for m in DOCUMENT['modules']} if DOCUMENT else {}


class GraphTests(unittest.TestCase):
    def test_cone_follows_register_feedback_without_looping(self):
        graph = Graph({'ports': [], 'operations': [
            {'kind': 'seq.firreg', 'results': ['q'], 'operands': ['next', 'clock']},
            {'kind': 'comb.or', 'results': ['next'], 'operands': ['q', 'busy']}]})
        self.assertEqual(graph.cone('q'), {'q', 'next', 'clock', 'busy'})


@unittest.skipUnless(DOCUMENT, 'Supply connected typed CIRCT export')
class HardwareTests(unittest.TestCase):
    def test_frontend_has_direct_routing_without_busy_interlock(self):
        facts = routing(MODULES['AtlasCore'], MODULES['ScalarCore'])
        self.assertFalse(facts['mreg_busy_interlocks_xlu_launch'])
        self.assertFalse(facts['xlu_busy_interlocks_frontend'])

    def test_frontend_assertions_still_require_logical_reservations(self):
        facts = frontend_assertions(MODULES['ScalarCore'])
        self.assertTrue(facts['vstore_requires_no_pending_destination_write'])
        self.assertEqual(facts['checks'], 384)

    def test_tracker_maps_all_architectural_registers_without_delay(self):
        self.assertEqual(tracker_mapping(MODULES['MregBankTracker'])['checks'], 64)

    def test_missing_frontend_assertion_is_rejected(self):
        module = copy.deepcopy(MODULES['ScalarCore'])
        module['operations'] = [o for o in module['operations'] if o['kind'] != 'sv.error']
        with self.assertRaisesRegex(ValueError, 'Missing MREG RAW hazard'):
            frontend_assertions(module)

    def test_routing_mutation_is_rejected(self):
        module = copy.deepcopy(MODULES['AtlasCore'])
        xlu = next(o for o in module['operations'] if o.get('instance', {}).get('name') == 'xlu')
        xlu['operands'][xlu['instance']['input_names'].index('io_cmd_valid')] = 'b0.a1'
        with self.assertRaisesRegex(ValueError, 'Non-direct connection'):
            routing(module, MODULES['ScalarCore'])

    def test_memory_latency_mutation_is_rejected(self):
        module = copy.deepcopy(MODULES['MregFile'])
        next(o for o in module['operations'] if o['kind'] == 'seq.firmem')['attributes']['readLatency'] = '2 : i32'
        with self.assertRaisesRegex(ValueError, 'Unsupported SRAM timing'):
            Mreg(module)

    def test_memory_collision_policy_mutation_is_rejected(self):
        module = copy.deepcopy(MODULES['MregFile'])
        next(o for o in module['operations'] if o['kind'] == 'seq.firmem')['attributes']['ruw'] = '1 : i32'
        with self.assertRaisesRegex(ValueError, 'Unsupported SRAM timing'):
            Mreg(module)

    def test_connected_response_and_source_release(self):
        trace = connected_case(MODULES['XluEngine'], MODULES['MregFile'], src=63, dst=63, overwrite_age=33)
        self.assertEqual(trace['response_ages'], list(range(2, 34)))
        self.assertEqual(trace['write_ages'], list(range(34, 66)))
        self.assertEqual(trace['active_read_ages'], list(range(1, 34)))

    def test_read_under_write_cannot_validate_payload(self):
        with self.assertRaisesRegex(ValueError, 'payload mismatch'):
            connected_case(MODULES['XluEngine'], MODULES['MregFile'], overwrite_age=32)

    def test_competing_read_has_no_retry(self):
        trace = connected_case(MODULES['XluEngine'], MODULES['MregFile'], competing_age=1)
        self.assertEqual(len(trace['response_ages']), 31)
        self.assertEqual(trace['write_ages'], [])


if __name__ == '__main__':
    unittest.main()

#!/usr/bin/env python3
"""Counter, reset, stall, and row mutations for the typed LSU timing slice.

Usage: python3 -B scripts/tests/test_rtlgraph_lsu_timing.py TYPED_LSU_JSON
"""
import copy
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from rtlgraph_lsu import analyze_lsu
from rtlgraph_lsu_timing import (TypedCone, analyze, row_pipeline, state_machine,
                                  summarize, temporal_model, trajectory)
from rtlgraph_mxu1 import Graph


def named(module, name):
    return next(op for op in module['operations'] if op['attributes'].get('name') == name)


def defining(module, value):
    graph = Graph(module)
    op = graph.definitions[value]
    while op['identity_wire']: op = graph.definitions[op['operands'][0]]
    return op


class LsuTimingTests(unittest.TestCase):
    def test_actual_typed_derivation_matches_existing_profile(self):
        facts = analyze(MODULE)
        self.assertEqual(sum(m['assignments_checked'] for m in facts['state_machines']), 1024)
        self.assertEqual(facts['temporal']['idle_counter_cases_checked'], 64)
        for events in facts['temporal']['events'].values():
            self.assertEqual([e['age'] for e in events['requests']], list(range(1, 33)))
            self.assertEqual(events['response_ages'], list(range(2, 34)))
            self.assertEqual([e['age'] for e in events['writes']], list(range(3, 35)))
            self.assertEqual(events['first_not_busy_age'], 35)

    def test_shorter_counter_stream_rejected(self):
        m = copy.deepcopy(MODULE)
        limit = next(op for op in m['operations'] if op['kind'] == 'hw.constant' and
                     op['attributes']['value'] == '31 : i6')
        limit['attributes']['value'] = '30 : i6'
        with self.assertRaisesRegex(ValueError, 'recurrence mismatch'):
            state_machine(m, 'vload')

    def test_skipping_counter_increment_rejected(self):
        m = copy.deepcopy(MODULE)
        increment = next(op for op in m['operations'] if op['kind'] == 'hw.constant' and
                         op['attributes']['value'] == '1 : i6')
        increment['attributes']['value'] = '2 : i6'
        with self.assertRaisesRegex(ValueError, 'recurrence mismatch'):
            state_machine(m, 'vstore')

    def test_counter_must_clear_on_command(self):
        m = copy.deepcopy(MODULE)
        count = named(m, 'vloadCounter')
        update = defining(m, count['operands'][0])
        launch = defining(m, update['operands'][1])
        launch['operands'][1] = count['results'][0]
        with self.assertRaisesRegex(ValueError, 'recurrence mismatch'):
            state_machine(m, 'vload')

    def test_drain_state_cannot_hold_forever(self):
        m = copy.deepcopy(MODULE)
        state = named(m, 'vloadState')
        selection = defining(m, state['operands'][0])
        table = defining(m, selection['operands'][0])
        # hw.array_create is reverse-indexed: operand1 is state2 (DRAIN).
        table['operands'][1] = state['results'][0]
        with self.assertRaisesRegex(ValueError, 'recurrence mismatch'):
            state_machine(m, 'vload')

    def test_response_backpressure_cannot_gate_counter(self):
        m = copy.deepcopy(MODULE)
        count = named(m, 'vloadCounter')
        update = defining(m, count['operands'][0])
        run = defining(m, update['operands'][2])
        run['operands'][0] = Graph(m).named('io_vmemVecReadData_valid')
        with self.assertRaisesRegex(ValueError, 'Unresolved'):
            state_machine(m, 'vload')

    def test_response_backpressure_cannot_gate_requests(self):
        m = copy.deepcopy(MODULE)
        output = named(m, 'io_vmemVecRead_valid')
        output['operands'][0] = Graph(m).named('io_vmemVecReadData_valid')
        with self.assertRaisesRegex(ValueError, 'Unresolved'):
            state_machine(m, 'vload')

    def test_wrong_reset_pin_rejected(self):
        m = copy.deepcopy(MODULE)
        named(m, 'vstoreCounter')['operands'][2] = Graph(m).named('io_mregReadResp_valid')
        with self.assertRaisesRegex(ValueError, 'register reset'):
            state_machine(m, 'vstore')

    def test_nonzero_counter_reset_rejected(self):
        m = copy.deepcopy(MODULE)
        reset_value = named(m, 'vloadCounter')['operands'][3]
        defining(m, reset_value)['attributes']['value'] = '1 : i6'
        with self.assertRaisesRegex(ValueError, 'register reset'):
            state_machine(m, 'vload')

    def test_clock_adaptor_requires_separate_evidence(self):
        m = copy.deepcopy(MODULE)
        named(m, 'vloadState')['operands'][1] = 'unproved-clock'
        with self.assertRaisesRegex(ValueError, 'clock'):
            state_machine(m, 'vload')

    def test_array_lut_must_have_total_index_domain(self):
        m = copy.deepcopy(MODULE)
        state = named(m, 'vloadState')
        table = defining(m, defining(m, state['operands'][0])['operands'][0])
        table['result_types'] = ['!hw.array<3xi2>']
        table['operands'].pop()
        with self.assertRaisesRegex(ValueError, 'array'):
            state_machine(m, 'vload')

    def test_wrong_source_row_slice_rejected(self):
        m = copy.deepcopy(MODULE)
        defining(m, Graph(m).named('io_mregReadReq_bits_row'))['attributes']['lowBit'] = '1 : i32'
        with self.assertRaisesRegex(ValueError, 'slice mismatch'):
            row_pipeline(m)

    def test_write_row_capture_requires_valid_response(self):
        m = copy.deepcopy(MODULE)
        mux = defining(m, named(m, 'vloadWriteRow')['operands'][0])
        mux['operands'][0] = Graph(m).named('vloadRespPending')
        with self.assertRaisesRegex(ValueError, 'capture guard mismatch'):
            row_pipeline(m)

    def test_store_address_stage_requires_previous_pending(self):
        m = copy.deepcopy(MODULE)
        mux = defining(m, named(m, 'vstoreRespLineAddr_q')['operands'][0])
        mux['operands'][0] = Graph(m).named('vstoreIssueRead')
        with self.assertRaisesRegex(ValueError, 'identity mismatch'):
            row_pipeline(m)

    def test_wrong_pending_recurrence_rejected_independently(self):
        m = copy.deepcopy(MODULE)
        named(m, 'vstoreRespPending_q')['operands'][0] = Graph(m).named('vstoreIssueRead')
        with self.assertRaises(ValueError):
            analyze_lsu(m)

    def test_two_cycle_response_is_outside_claim(self):
        with self.assertRaisesRegex(ValueError, 'one-cycle response contract'):
            trajectory(temporal_model(MODULE), response_latency=2)

    def test_zero_cycle_response_is_outside_claim(self):
        with self.assertRaisesRegex(ValueError, 'one-cycle response contract'):
            trajectory(temporal_model(MODULE), response_latency=0)

    def test_no_valid_output_is_observed_after_reported_release(self):
        trace = trajectory(temporal_model(MODULE))
        for kind, signal in (('vload', 'io_mregWriteReq_valid'), ('vstore', 'io_vmemVecWrite_valid')):
            release = summarize(trace, kind)['first_not_busy_age']
            self.assertFalse(any(edge[signal] for edge in trace if edge['age'] >= release))

    def test_idle_counter_does_not_shift_next_transfer(self):
        model = temporal_model(MODULE)
        initial = trajectory(model)
        stale = trajectory(model, initial_counter=63)
        for kind in ('vload', 'vstore'):
            self.assertEqual(summarize(initial, kind), summarize(stale, kind))


if __name__ == '__main__':
    if len(sys.argv) < 2: raise SystemExit(__doc__)
    typed = json.loads(Path(sys.argv.pop(1)).read_text())
    MODULE = next(module for module in typed['modules'] if module['name'] == 'LSU')
    unittest.main()

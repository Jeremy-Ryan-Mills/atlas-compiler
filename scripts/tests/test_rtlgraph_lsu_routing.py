#!/usr/bin/env python3
"""Mutation tests for conditional LSU routing. Pass the four-module typed JSON."""
import copy
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from rtlgraph_dma import operation
from rtlgraph_lsu_routing import (BitRoute, analyze, busy_extensions, compose_busy_tails,
                                 lsu_routes, mreg_routes, vmem_routes, wrapper)
from rtlgraph_lsu_timing import analyze as analyze_timing
from rtlgraph_mxu1 import Graph


def add_busy_tail(module, kind='vload'):
    """Synthetic typed-IR sensitivity fixture, not measured hardware evidence."""
    graph = Graph(module)
    busy = graph.definitions[graph.named(kind + 'Busy')]
    base = busy['operands'][0]
    template = copy.deepcopy(graph.definitions[graph.named(kind + 'RespPending' if kind == 'vload' else 'vstoreRespPending_d')])
    name = kind + 'BusyTail'
    template.update(id=name, results=[name + '.r0'], attributes={'name': name}, location='loc("synthetic LSU busy tail")')
    template['operands'][0] = base
    join = dict(id=name + 'Or', kind='comb.or', attributes={'twoState': 'unit'},
                operands=[base, name + '.r0'], results=[name + 'Or.r0'], result_types=['i1'],
                combinational=True, identity_wire=False, has_regions=False, location=template['location'])
    busy['operands'][0] = join['results'][0]
    module['operations'].extend([template, join])
    return template, join


class LsuRoutingTests(unittest.TestCase):
    def test_all_banks_route_and_capture(self):
        result = analyze(MODULES)
        self.assertEqual(result['operand_capture'], 'issue')
        self.assertEqual(len(result['routing']['MregFile']['banks']), 32)
        self.assertEqual(len(result['routing']['Vmem']['banks']), 6)
        self.assertEqual(result['lsu_timing']['temporal']['events']['vload']['first_not_busy_age'], 35)
        self.assertEqual(result['routing']['Vmem']['banks'][0]['facts'][1]['control_assignments_checked'], 64)

    def test_operand_capture_must_hold_after_issue(self):
        module = copy.deepcopy(MODULES['LSU']); graph = Graph(module)
        mux = operation(graph, graph.definitions[graph.named('vloadMregId')]['operands'][0], 'comb.mux')
        mux['operands'][2] = mux['operands'][1]
        with self.assertRaisesRegex(ValueError, 'identity mismatch'):
            lsu_routes(module)

    def test_payload_capture_clock_matches_control(self):
        module = copy.deepcopy(MODULES['LSU']); graph = Graph(module)
        graph.definitions[graph.named('vloadWriteData')]['operands'][1] = graph.named('reset')
        with self.assertRaisesRegex(ValueError, 'clock'):
            lsu_routes(module)

    def test_vmem_bank_selection_cannot_shift(self):
        module = copy.deepcopy(MODULES['LSU']); graph = Graph(module)
        operation(graph, graph.named('io_vmemVecRead_bits_bankIdx'), 'comb.extract')['attributes']['lowBit'] = '12 : i32'
        with self.assertRaises(ValueError): lsu_routes(module)

    def test_mreg_physical_row_keeps_logical_high_half(self):
        module = copy.deepcopy(MODULES['MregFile']); graph = Graph(module)
        concat = operation(graph, graph.named('writePhysRows_6'), 'comb.concat')
        operation(graph, concat['operands'][0], 'comb.extract')['attributes']['lowBit'] = '4 : i32'
        with self.assertRaisesRegex(ValueError, 'function mismatch'): mreg_routes(module)

    def test_mreg_write_clock_checked(self):
        module = copy.deepcopy(MODULES['MregFile']); graph = Graph(module)
        port = next(o for o in module['operations'] if o['kind'] == 'seq.firmem.write_port')
        port['operands'][2] = graph.named('reset')
        with self.assertRaisesRegex(ValueError, 'write port'): mreg_routes(module)

    def test_mreg_write_payload_cannot_select_another_writer(self):
        module = copy.deepcopy(MODULES['MregFile']); graph = Graph(module)
        port = next(o for o in module['operations'] if o['kind'] == 'seq.firmem.write_port')
        port['operands'][4] = graph.named('io_vpuWriteReq0_bits_data')
        with self.assertRaises(ValueError): mreg_routes(module)

    def test_mreg_read_row_cannot_select_another_client(self):
        module = copy.deepcopy(MODULES['MregFile']); graph = Graph(module)
        port = next(o for o in module['operations'] if o['kind'] == 'seq.firmem.read_port')
        port['operands'][1] = graph.named('readPhysRows_0')
        with self.assertRaises(ValueError): mreg_routes(module)

    def test_vmem_partial_write_mask_rejected(self):
        module = copy.deepcopy(MODULES['Vmem']); graph = Graph(module)
        port = next(o for o in module['operations'] if o['kind'] == 'seq.firmem.read_write_port')
        zero = dict(id='zero_mask', kind='hw.constant', result_types=['i32'], results=['zero_mask.r0'],
                    attributes={'value': '0 : i32'}, operands=[], has_regions=False, combinational=True, identity_wire=False, location='loc(\"test\")')
        module['operations'].append(zero)
        port['operands'][6] = zero['results'][0]
        with self.assertRaisesRegex(ValueError, 'route mismatch'): vmem_routes(module)

    def test_vmem_write_data_swap_rejected(self):
        module = copy.deepcopy(MODULES['Vmem']); graph = Graph(module)
        port = next(o for o in module['operations'] if o['kind'] == 'seq.firmem.read_write_port')
        port['operands'][4] = graph.named('io_dmaWrite_bits_data')
        with self.assertRaises(ValueError): vmem_routes(module)

    def test_response_byte_order_preserved_for_arbitrary_payloads(self):
        module = copy.deepcopy(MODULES['Vmem']); graph = Graph(module)
        memory = next(o for o in module['operations'] if o['kind'] == 'seq.firmem.read_write_port')
        route = BitRoute(module)
        root = graph.named('flatBankReadData_0')
        route.prove(root, memory['results'][0], [{}], 'unaltered SRAM response')
        concat = operation(graph, root, 'comb.concat')
        concat['operands'].reverse()
        with self.assertRaisesRegex(ValueError, 'route mismatch'):
            BitRoute(module).prove(root, memory['results'][0], [{}], 'swapped response halves')

    def test_wrapper_payload_miswire_rejected(self):
        module = copy.deepcopy(MODULES['AtlasCore']); graph = Graph(module)
        instance = next(o for o in module['operations'] if o.get('instance', {}).get('name') == 'lsu')
        ports = instance['instance']['input_names']
        instance['operands'][ports.index('io_mregReadResp_bits')] = instance['operands'][ports.index('io_vmemVecReadData_bits')]
        with self.assertRaisesRegex(ValueError, 'identity mismatch'): wrapper(module)

    def test_checked_busy_tail_changes_derived_release_only(self):
        module = copy.deepcopy(MODULES['LSU'])
        add_busy_tail(module)
        normalized, tails = busy_extensions(module)
        timing = analyze_timing(normalized)
        before = copy.deepcopy(timing['temporal']['events'])
        compose_busy_tails(timing, module, tails)
        after = timing['temporal']['events']
        self.assertEqual(after['vload']['first_not_busy_age'], 36)
        self.assertEqual(after['vload']['requests'], before['vload']['requests'])
        self.assertEqual(after['vload']['writes'], before['vload']['writes'])
        self.assertEqual(after['vstore'], before['vstore'])

    def test_busy_tail_feedback_cannot_latch_busy(self):
        module = copy.deepcopy(MODULES['LSU']); saved, _ = add_busy_tail(module)
        saved['operands'][0] = Graph(module).named('vloadBusy')
        with self.assertRaisesRegex(ValueError, 'identity mismatch'): busy_extensions(module)

    def test_busy_tail_reset_must_clear(self):
        module = copy.deepcopy(MODULES['LSU']); saved, _ = add_busy_tail(module)
        saved['operands'][3] = saved['results'][0]
        with self.assertRaises(ValueError): busy_extensions(module)

    def test_busy_tail_must_extend_logical_ownership(self):
        module = copy.deepcopy(MODULES['LSU']); _, joined = add_busy_tail(module)
        graph = Graph(module)
        graph.definitions[graph.named('io_activeMregWrite_valid')]['operands'][0] = joined['operands'][0]
        with self.assertRaisesRegex(ValueError, 'identity mismatch'): busy_extensions(module)


if __name__ == '__main__':
    if len(sys.argv) < 2: raise SystemExit(__doc__)
    MODULES = {m['name']: m for m in json.loads(Path(sys.argv.pop(1)).read_text())['modules']}
    unittest.main()

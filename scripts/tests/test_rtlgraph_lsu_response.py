#!/usr/bin/env python3
"""Mutation checks for LSU memory arbitration and one-cycle response evidence.

Usage: python3 -B scripts/tests/test_rtlgraph_lsu_response.py TYPED_RESPONSE_JSON
"""
import copy
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from rtlgraph_dma import operation
from rtlgraph_lsu_response import analyze, memories, mreg_bank, mreg_decode, or_tree, vmem_bank, vmem_decode, wrapper
from rtlgraph_lsu_timing import TypedCone
from rtlgraph_mxu1 import Graph


def named(module, name):
    return next(o for o in module['operations'] if o['attributes'].get('name') == name)


def mreg_first(module):
    return mreg_bank(module, 0, memories(module, 32, '!seq.firmem<64 x 256>')[0])


def vmem_first(module):
    return vmem_bank(module, 0, memories(module, 6, '!seq.firmem<8192 x 256, mask 32>', True)[0])


class LsuResponseTests(unittest.TestCase):
    def test_actual_all_banks_and_composed_timing(self):
        result = analyze(MODULES)
        self.assertEqual(len(result['MregFile']['banks']), 32)
        self.assertEqual(len(result['Vmem']['banks']), 6)
        self.assertEqual(result['response_contract']['latency_edges'], 1)
        for events in result['lsu_timing']['temporal']['events'].values():
            self.assertEqual(events['response_ages'], list(range(2, 34)))
            self.assertEqual(events['first_not_busy_age'], 35)

    def test_changed_sram_latency_rejected(self):
        module = copy.deepcopy(MODULES['Vmem'])
        named(module, 'banks_0')['attributes']['readLatency'] = '2 : i32'
        with self.assertRaisesRegex(ValueError, 'latency'):
            vmem_first(module)

    def test_unknown_memory_semantics_rejected(self):
        module = copy.deepcopy(MODULES['MregFile'])
        named(module, 'banks_0')['attributes']['extraClockEnable'] = 'true'
        with self.assertRaisesRegex(ValueError, 'attributes'):
            mreg_first(module)

    def test_wider_cutpoint_cannot_keep_a_partial_test_domain(self):
        module = copy.deepcopy(MODULES['MregFile'])
        port = next(p for p in module['ports'] if p['name'] == 'io_lsuReadReq_bits_mregId')
        port['type'] = 'i7'
        named(module, port['name'])['result_types'] = ['i7']
        with self.assertRaisesRegex(ValueError, 'complete typed input domains'):
            mreg_decode(module)

    def test_memory_clock_must_match_response_register(self):
        module = copy.deepcopy(MODULES['MregFile'])
        port = next(o for o in module['operations'] if o['kind'] == 'seq.firmem.read_port')
        port['operands'][2] = Graph(module).named('reset')
        with self.assertRaisesRegex(ValueError, 'clock'):
            mreg_first(module)

    def test_mreg_decode_must_include_request_valid(self):
        module = copy.deepcopy(MODULES['MregFile'])
        wire = named(module, 'readBankOHs_6')
        gate = operation(Graph(module), wire['operands'][0], 'comb.and')
        wire['operands'][0] = gate['operands'][0]
        with self.assertRaisesRegex(ValueError, 'function mismatch'):
            mreg_decode(module)

    def test_mreg_high_half_must_alias_same_physical_bank(self):
        module = copy.deepcopy(MODULES['MregFile'])
        graph = Graph(module)
        selection = graph.cone(graph.named('readBankOHs_6'))
        extract = next(o for o in selection['operations'] if o['kind'] == 'comb.extract')
        extract['attributes']['lowBit'] = '1 : i32'
        with self.assertRaisesRegex(ValueError, 'function mismatch'):
            mreg_decode(module)

    def test_vmem_out_of_range_bank_cannot_alias_a_valid_bank(self):
        module = copy.deepcopy(MODULES['Vmem'])
        graph = Graph(module)
        shift = next(o for o in graph.cone(graph.named('lsuVecReadBankOH'))['operations'] if o['kind'] == 'comb.shl')
        shift['operands'][1] = shift['operands'][0]
        with self.assertRaisesRegex(ValueError, 'function mismatch'):
            vmem_decode(module)

    def test_mreg_sram_enable_cannot_drop_a_granted_request(self):
        module = copy.deepcopy(MODULES['MregFile'])
        port = next(o for o in module['operations'] if o['kind'] == 'seq.firmem.read_port')
        port['operands'][3] = Graph(module).named('bankReadValid_d_0')
        with self.assertRaisesRegex(ValueError, 'identity mismatch'):
            mreg_first(module)

    def test_vmem_read_mode_cannot_be_write_mode(self):
        module = copy.deepcopy(MODULES['Vmem'])
        port = next(o for o in module['operations'] if o['kind'] == 'seq.firmem.read_write_port')
        port['operands'][5] = port['operands'][3]
        with self.assertRaises(ValueError):
            vmem_first(module)

    def test_vmem_priority_cannot_promote_dma_above_lsu(self):
        module = copy.deepcopy(MODULES['Vmem'])
        mux = next(o for o in module['operations'] if o['attributes'].get('sv.namehint') == '_accessSel_out_readClient_T_6')
        mux['operands'][1], mux['operands'][2] = mux['operands'][2], mux['operands'][1]
        with self.assertRaisesRegex(ValueError, 'function mismatch'):
            vmem_first(module)

    def test_response_requires_correct_registered_client(self):
        module = copy.deepcopy(MODULES['MregFile'])
        graph = Graph(module)
        cmp = next(o for o in graph.cone(graph.named('respHits_6_0'))['operations'] if o['kind'] == 'comb.icmp')
        cmp['attributes']['predicate'] = '1 : i64'
        with self.assertRaisesRegex(ValueError, 'function mismatch'):
            mreg_first(module)

    def test_missing_response_bank_rejected(self):
        module = copy.deepcopy(MODULES['Vmem'])
        graph = Graph(module)
        root = graph.named('io_lsuVecReadData_valid')
        join = operation(graph, root, 'comb.or')
        join['operands'][1] = join['operands'][0]
        with self.assertRaisesRegex(ValueError, 'Missing LSU response bank'):
            or_tree(module, root, [graph.named(f'lsuVecRespSel_leaves_{i}_valid') for i in range(6)])

    def test_response_register_cannot_become_two_cycle_delay(self):
        module = copy.deepcopy(MODULES['Vmem'])
        valid = named(module, 'r1_bankReadValid_0')
        valid['operands'][0] = valid['results'][0]
        with self.assertRaisesRegex(ValueError, 'Unresolved'):
            vmem_first(module)

    def test_wrapper_clock_is_part_of_composition(self):
        module = copy.deepcopy(MODULES['AtlasCore'])
        instance = next(o for o in module['operations'] if o.get('instance', {}).get('name') == 'mreg')
        instance['operands'][instance['instance']['input_names'].index('clock')] = Graph(module).named('reset')
        with self.assertRaisesRegex(ValueError, 'identity mismatch'):
            wrapper(module)

    def test_shift_and_replicate_are_finite_width(self):
        module = dict(ports=[dict(direction='input', value='a', type='i4'),
                             dict(direction='input', value='b', type='i4')], operations=[
            dict(id='op0', location='test', kind='comb.shl', operands=['a', 'b'], results=['shift'], result_types=['i4'],
                 attributes={}, identity_wire=False, combinational=True, has_regions=False),
            dict(id='op1', location='test', kind='comb.replicate', operands=['shift'], results=['repeated'], result_types=['i8'],
                 attributes={}, identity_wire=False, combinational=True, has_regions=False)])
        evaluate = TypedCone(module, 'repeated', dict(a='a', b='b'))
        self.assertEqual(evaluate(dict(a=3, b=3)), 0x88)
        self.assertEqual(evaluate(dict(a=15, b=15)), 0)


if __name__ == '__main__':
    if len(sys.argv) < 2: raise SystemExit(__doc__)
    typed = json.loads(Path(sys.argv.pop(1)).read_text())
    MODULES = {module['name']: module for module in typed['modules']}
    unittest.main()

#!/usr/bin/env python3
"""Adversarial checks over the pinned typed DMA modules and adapter export.

Usage: python3 -B scripts/tests/test_rtlgraph_dram.py TYPED_JSON ADAPTER_TYPED_JSON
The artifacts are explicit so tests do not silently depend on an old build path.
"""
import copy
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from rtlgraph_dram import (adapter_address, analyze, checked_address_facts,
                           engine_address, modules_from, request_progression, scalar_address, wrapper_address)
from rtlgraph_dram_profile import address_settings
from rtlgraph_mxu1 import Graph


def named(module, name):
    return next(op for op in module['operations'] if op['attributes'].get('name') == name)


def defining(module, value):
    graph = Graph(module)
    op = graph.definitions[value]
    while op['identity_wire']:
        op = graph.definitions[op['operands'][0]]
    return op


class DramAddressTests(unittest.TestCase):
    def module(self, name):
        return copy.deepcopy(MODULES[name])

    def test_verified_real_typed_pipeline(self):
        facts = analyze(MODULES)
        self.assertEqual(facts['geometry']['tilelink_address_bits'], 37)
        self.assertEqual(facts['scalar']['load_selector']['assignments_checked'], 8)
        self.assertEqual(sum(f['assignments_checked'] for f in facts['progression']['functions']), 28)
        self.assertEqual(address_settings(facts)['dram_address'], 'base32-concat-low32')

    def test_base_concat_order_is_not_interchangeable(self):
        m = self.module('ScalarCore')
        output = defining(m, Graph(m).named('io_dmaCmd_bits_addr'))
        output['operands'].reverse()
        with self.assertRaisesRegex(ValueError, 'identity mismatch'):
            scalar_address(m)

    def test_addition_cannot_replace_base_concat(self):
        m = self.module('ScalarCore')
        defining(m, Graph(m).named('io_dmaCmd_bits_addr'))['kind'] = 'comb.add'
        with self.assertRaisesRegex(ValueError, 'comb.concat'):
            scalar_address(m)

    def test_wrong_load_source_register(self):
        m = self.module('ScalarCore')
        output = defining(m, Graph(m).named('io_dmaCmd_bits_addr'))
        mux = defining(m, output['operands'][1])
        mux['operands'][1], mux['operands'][2] = mux['operands'][2], mux['operands'][1]
        with self.assertRaisesRegex(ValueError, 'identity mismatch'):
            scalar_address(m)

    def test_size_must_come_from_rs2(self):
        m = self.module('ScalarCore')
        g = Graph(m)
        size = g.definitions[g.named('io_dmaCmd_bits_size')]
        output = defining(m, g.named('io_dmaCmd_bits_addr'))
        mux = defining(m, output['operands'][1])
        size['operands'][0] = mux['operands'][1]
        with self.assertRaisesRegex(ValueError, 'identity mismatch'):
            scalar_address(m)

    def test_wrong_dma_load_opcode(self):
        m = self.module('ScalarCore')
        output = defining(m, Graph(m).named('io_dmaCmd_bits_addr'))
        mux = defining(m, output['operands'][1])
        compare = defining(m, mux['operands'][0])
        defining(m, compare['operands'][1])['attributes']['value'] = '2 : i3'
        with self.assertRaisesRegex(ValueError, 'function mismatch'):
            scalar_address(m)

    def test_nonzero_base_reset(self):
        m = self.module('ScalarCore')
        base = named(m, 'dmaBaseReg')
        defining(m, base['operands'][3])['attributes']['value'] = '1 : i32'
        with self.assertRaisesRegex(ValueError, 'register reset'):
            scalar_address(m)

    def test_wrong_saved_command_slot(self):
        m = self.module('DmaEngine')
        good = engine_address(m)
        array = defining(m, good['saved_address_selection']['selection']['operands'][0])
        array['operands'][0] = array['operands'][1]
        with self.assertRaisesRegex(ValueError, 'identity mismatch'):
            engine_address(m)

    def test_nonzero_beat_offset_padding(self):
        m = self.module('DmaEngine')
        good = engine_address(m)
        defining(m, good['beat_offset_concat']['operands'][2])['attributes']['value'] = '1 : i5'
        with self.assertRaisesRegex(ValueError, 'constant'):
            engine_address(m)

    def test_32_bit_add_would_lose_low_address_carry(self):
        m = self.module('DmaEngine')
        good = engine_address(m)
        defining(m, good['address_add']['results'][0])['result_types'] = ['i32']
        with self.assertRaisesRegex(ValueError, 'shape'):
            engine_address(m)

    def test_wrong_physical_address_mask(self):
        m = self.module('TileLinkAdapter')
        defining(m, Graph(m).named('io_tl_a_bits_address'))['result_types'] = ['i38']
        with self.assertRaisesRegex(ValueError, 'shape'):
            adapter_address(m)

    def test_bus_slice_cannot_drop_low_address_bit(self):
        m = self.module('TileLinkAdapter')
        defining(m, Graph(m).named('io_tl_a_bits_address'))['attributes']['lowBit'] = '1 : i32'
        with self.assertRaisesRegex(ValueError, 'truncation'):
            adapter_address(m)

    def test_adapter_alignment_cannot_or_in_low_bits(self):
        m = self.module('TileLinkAdapter')
        good = adapter_address(m)
        defining(m, good['aligned_concat']['operands'][2])['attributes']['value'] = '1 : i5'
        with self.assertRaisesRegex(ValueError, 'constant'):
            adapter_address(m)

    def test_adapter_must_clear_exactly_five_bits(self):
        m = self.module('TileLinkAdapter')
        good = adapter_address(m)
        defining(m, good['source_slice']['operation']['results'][0])['attributes']['lowBit'] = '4 : i32'
        with self.assertRaisesRegex(ValueError, 'slice mismatch'):
            adapter_address(m)

    def test_transfer_size_is_32_bytes_through_wrapper(self):
        for module, signal, check in (
                ('TileLinkAdapter', 'io_tl_a_bits_size', adapter_address),
                ('DmaEngine', 'io_tl_a_bits_size', engine_address),
                ('AtlasCore', 'io_dmaTL_a_bits_size', wrapper_address)):
            with self.subTest(module=module):
                m = self.module(module)
                defining(m, Graph(m).named(signal))['attributes']['value'] = '6 : i4'
                with self.assertRaisesRegex(ValueError, 'constant'):
                    check(m)

    def test_ready_and_valid_must_share_tag_guard(self):
        m = self.module('TileLinkAdapter')
        ready = defining(m, Graph(m).named('io_request_ready'))
        ready['operands'][1] = ready['operands'][0]
        with self.assertRaisesRegex(ValueError, 'function mismatch'):
            adapter_address(m)

    def test_unaccepted_request_cannot_fire_tilelink(self):
        m = self.module('TileLinkAdapter')
        valid = defining(m, Graph(m).named('io_tl_a_valid'))
        valid['operands'][1] = valid['operands'][0]
        with self.assertRaisesRegex(ValueError, 'function mismatch'):
            adapter_address(m)

    def test_selected_slot_must_be_active(self):
        m = self.module('DmaEngine')
        good = request_progression(m)
        active = good['active_selection']['selection']['results'][0]
        operation = defining(m, active)
        operation['operands'][1] = named(m, 'enqueueIdx')['results'][0]
        with self.assertRaisesRegex(ValueError, 'identity mismatch'):
            request_progression(m)

    def test_dispatched_slot_must_block_requests(self):
        m = self.module('DmaEngine')
        good = request_progression(m)
        dispatched = good['dispatched_selection']['selection']['results'][0]
        inverted = next(op for op in m['operations'] if op['kind'] == 'comb.xor' and
                        op['operands'][0] == dispatched)
        defining(m, inverted['operands'][1])['attributes']['value'] = 'false'
        with self.assertRaisesRegex(ValueError, 'constant'):
            request_progression(m)

    def test_counter_cannot_skip_a_beat(self):
        m = self.module('DmaEngine')
        good = request_progression(m)
        defining(m, good['increment']['operands'][1])['attributes']['value'] = '2 : i9'
        with self.assertRaisesRegex(ValueError, 'constant'):
            request_progression(m)

    def test_wrong_last_beat_predicate(self):
        m = self.module('DmaEngine')
        defining(m, Graph(m).named('isLastBeat'))['attributes']['predicate'] = '1 : i64'
        with self.assertRaisesRegex(ValueError, 'last-beat predicate'):
            request_progression(m)

    def test_final_beat_must_restart_counter_at_zero(self):
        m = self.module('DmaEngine')
        good = request_progression(m)
        next_count = defining(m, good['next_count']['results'][0])
        next_count['operands'][1] = good['increment']['results'][0]
        with self.assertRaisesRegex(ValueError, 'hw.constant'):
            request_progression(m)

    def test_slot_cannot_advance_on_every_accepted_beat(self):
        m = self.module('DmaEngine')
        good = request_progression(m)
        defining(m, good['next_index']['results'][0])['operands'][0] = good['update']['operands'][0]
        with self.assertRaisesRegex(ValueError, 'function mismatch'):
            request_progression(m)

    def test_counter_cannot_advance_while_backpressured(self):
        m = self.module('DmaEngine')
        good = request_progression(m)
        defining(m, good['update']['results'][0])['operands'][0] = Graph(m).named('isLastBeat')
        with self.assertRaises(ValueError):
            request_progression(m)

    def test_cached_status_does_not_authorize_changed_facts(self):
        report = analyze(MODULES)
        report.update(schema='atlas.rtlgraph.dram-address-functions.v1', config='EE290SimConfig')
        report['geometry']['tilelink_address_bits'] = 64
        with self.assertRaisesRegex(ValueError, 'fresh typed analysis'):
            checked_address_facts(report, TYPED, ADAPTER)

    def test_projection_rejects_other_geometry(self):
        facts = analyze(MODULES)
        facts['geometry']['tilelink_address_bits'] = 64
        with self.assertRaisesRegex(ValueError, 'geometry'):
            address_settings(facts)

    def test_missing_adapter_module_rejected(self):
        with self.assertRaisesRegex(ValueError, 'ambiguous'):
            modules_from(TYPED, dict(modules=[], missing_modules=[]))


if __name__ == '__main__':
    if len(sys.argv) < 3:
        raise SystemExit(__doc__)
    TYPED = json.loads(Path(sys.argv.pop(1)).read_text())
    ADAPTER = json.loads(Path(sys.argv.pop(1)).read_text())
    MODULES = modules_from(TYPED, ADAPTER)
    unittest.main()

#!/usr/bin/env python3
"""Typed local DMA fixture checks and adversarial mutations."""
import copy
import json
from pathlib import Path
import subprocess
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from rtlgraph_dma import completion_slot, vmem_bank


def named(module, name):
    return next(o for o in module['operations'] if o['attributes'].get('name') == name)


class DmaLocalTests(unittest.TestCase):
    def test_slot_fixture(self):
        result = completion_slot(MODULES['DmaSlot'], 0)
        self.assertEqual(result['zero_function']['assignments_checked'], 2048)
        self.assertEqual(result['active_function']['assignments_checked'], 128)
        self.assertEqual(len(result['command_captures']), 5)

    def test_bank_fixture(self):
        result = vmem_bank(MODULES['VmemBank'], 0)
        self.assertEqual(len(result['functions']), 4)
        self.assertEqual(sum(f['assignments_checked'] for f in result['functions']), 1024)

    def test_wrong_completion_count(self):
        module = copy.deepcopy(MODULES['DmaSlot'])
        const = next(o for o in module['operations'] if o['kind'] == 'hw.constant' and o['attributes']['value'] == '1 : i9')
        const['attributes']['value'] = '2 : i9'
        with self.assertRaisesRegex(ValueError, 'function mismatch'): completion_slot(module, 0)

    def test_wrong_reset(self):
        module = copy.deepcopy(MODULES['DmaSlot'])
        named(module, 'slotActive_0')['operands'][2] = 'invalid-reset'
        with self.assertRaisesRegex(ValueError, 'register reset'): completion_slot(module, 0)

    def test_unconditional_capture(self):
        module = copy.deepcopy(MODULES['DmaSlot'])
        definitions = {v:o for o in module['operations'] for v in o['results']}
        saved = named(module, 'commandQueue_0_dramAddress')
        mux = definitions[saved['operands'][0]]
        mux['operands'][0] = next(p['value'] for p in module['ports'] if p['name'] == 'io_command_valid')
        with self.assertRaisesRegex(ValueError, 'function mismatch'): completion_slot(module, 0)

    def test_live_address_instead_of_saved(self):
        module = copy.deepcopy(MODULES['DmaSlot'])
        definitions = {v:o for o in module['operations'] for v in o['results']}
        saved = named(module, 'commandQueue_0_dramAddress')
        mux = definitions[saved['operands'][0]]
        mux['operands'][2] = mux['operands'][1]
        with self.assertRaisesRegex(ValueError, 'identity mismatch'): completion_slot(module, 0)

    def test_early_slot_release(self):
        module = copy.deepcopy(MODULES['DmaSlot'])
        named(module, 'slotActive_0')['operands'][0] = next(o['results'][0] for o in module['operations']
            if o['kind'] == 'hw.constant' and o['attributes']['value'] == 'false')
        with self.assertRaisesRegex(ValueError, 'function mismatch'): completion_slot(module, 0)

    def test_dma_ignores_lsu_priority(self):
        module = copy.deepcopy(MODULES['VmemBank'])
        named(module, 'bankDmaReadGrant_0')['operands'][0] = named(module, 'accessSel_leaf_5_valid')['results'][0]
        with self.assertRaisesRegex(ValueError, 'function mismatch'): vmem_bank(module, 0)

    def test_wrong_bank_bit(self):
        module = copy.deepcopy(MODULES['VmemBank'])
        extract = next(o for o in module['operations'] if o['kind'] == 'comb.extract')
        extract['attributes']['lowBit'] = '1 : i32'
        with self.assertRaisesRegex(ValueError, 'slice mismatch'): vmem_bank(module, 0)

    def test_wrong_read_client(self):
        module = copy.deepcopy(MODULES['VmemBank'])
        value = next(o for o in module['operations'] if o['kind'] == 'hw.constant' and o['attributes']['value'] == '2 : i3')
        value['attributes']['value'] = '3 : i3'
        with self.assertRaisesRegex(ValueError, 'function mismatch'): vmem_bank(module, 0)


if __name__ == '__main__':
    exporter = Path(sys.argv.pop(1)).resolve()
    result = subprocess.run([str(exporter), str(Path(__file__).with_name('rtlgraph-dma.mlir')), 'DmaSlot', 'VmemBank'],
                            text=True, capture_output=True, check=True)
    MODULES = {m['name']: m for m in json.loads(result.stdout)['modules']}
    unittest.main()

#!/usr/bin/env python3
"""Run with the typed CIRCT exporter as the first argument."""
import copy
import json
from pathlib import Path
import subprocess
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from rtlgraph_mreg import Bits, mapping, memory_bank


class MregTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        data = json.loads(subprocess.check_output([str(EXPORTER),
            str(Path(__file__).with_name('rtlgraph-mreg.mlir')), 'MregSlice'], text=True))
        cls.module = data['modules'][0]

    def test_all_registers_and_rows(self):
        result = mapping(self.module, 'read', 2, 'mxu1ReadReq0')
        self.assertEqual(result['bank_assignments_checked'], 128)
        self.assertEqual(result['row_assignments_checked'], 2048)

    def test_high_half_alias_preserves_distinct_row(self):
        bits = Bits(self.module)
        g = bits.graph
        valid, reg, row = [g.named('io_mxu1ReadReq0' + suffix)
                           for suffix in ('_valid', '_bits_mregId', '_bits_row')]
        banks = [bits.evaluate(g.named('readBankOHs_2'), {valid: 1, reg: r}) for r in (0, 32)]
        rows = [bits.evaluate(g.named('readPhysRows_2'), {reg: r, row: 7}) for r in (0, 32)]
        self.assertEqual(banks, [1, 1])
        self.assertEqual(rows, [7, 39])

    def test_changed_bank_or_row_mapping_rejected(self):
        for old, new, diagnostic in (('0 : i32', '1 : i32', 'bank mapping'),
                                     ('5 : i32', '4 : i32', 'row mapping')):
            module = copy.deepcopy(self.module)
            op = next(o for o in module['operations'] if o['kind'] == 'comb.extract' and o['attributes']['lowBit'] == old)
            op['attributes']['lowBit'] = new
            with self.subTest(diagnostic=diagnostic), self.assertRaisesRegex(ValueError, diagnostic):
                mapping(module, 'read', 2, 'mxu1ReadReq0')

    def test_unsupported_logic_rejected(self):
        module = copy.deepcopy(self.module)
        next(o for o in module['operations'] if o['kind'] == 'comb.and')['kind'] = 'comb.or'
        with self.assertRaisesRegex(ValueError, 'Unsupported address operation'):
            mapping(module, 'read', 2, 'mxu1ReadReq0')

    def test_feedback_undefined_values_and_state_are_rejected(self):
        for change in ('feedback', 'undefined', 'state'):
            module = copy.deepcopy(self.module)
            op = next(o for o in module['operations'] if o['kind'] == 'comb.and')
            if change == 'feedback': op['operands'][0] = op['results'][0]
            if change == 'undefined': op['operands'][0] = 'undefined'
            if change == 'state': op['kind'] = 'seq.firreg'
            with self.subTest(change=change), self.assertRaisesRegex(ValueError, 'Feedback|input/state'):
                mapping(module, 'read', 2, 'mxu1ReadReq0')

    def test_widened_input_rejected(self):
        module = copy.deepcopy(self.module)
        next(p for p in module['ports'] if p['name'] == 'io_mxu1ReadReq0_bits_mregId')['type'] = 'i7'
        with self.assertRaisesRegex(ValueError, 'identities or widths'):
            mapping(module, 'read', 2, 'mxu1ReadReq0')

    def test_memory_ports(self):
        self.assertEqual(len(memory_bank(self.module, 0)['ports']), 2)

    def test_extra_port_rejected(self):
        module = copy.deepcopy(self.module)
        read = copy.deepcopy(next(o for o in module['operations'] if o['kind'] == 'seq.firmem.read_port'))
        read.update(id='extra', results=['extra.r0'])
        module['operations'].append(read)
        with self.assertRaisesRegex(ValueError, 'exactly one read and one write'):
            memory_bank(module, 0)

    def test_latency_and_wiring_changes_rejected(self):
        for change in ('latency', 'enable'):
            module = copy.deepcopy(self.module)
            if change == 'latency':
                next(o for o in module['operations'] if o['kind'] == 'seq.firmem')['attributes']['readLatency'] = '2 : i32'
            else:
                read = next(o for o in module['operations'] if o['kind'] == 'seq.firmem.read_port')
                read['operands'][3] = next(p['value'] for p in module['ports'] if p['name'] == 'bankWriteValid_0')
            with self.subTest(change=change), self.assertRaisesRegex(ValueError, 'latency|connection mismatch'):
                memory_bank(module, 0)


if __name__ == '__main__':
    EXPORTER = Path(sys.argv.pop(1)).resolve()
    unittest.main()

#!/usr/bin/env python3
"""Run with the typed CIRCT exporter as the first argument."""
import copy
import json
from pathlib import Path
import subprocess
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from rtlgraph_mxu1_bottleneck import Evaluator, accumulator_bank, boundary_function, weight_write_function


class BottleneckTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        exported = json.loads(subprocess.check_output([str(EXPORTER),
            str(Path(__file__).with_name('rtlgraph-mxu1-bottleneck.mlir')),
            'BoundaryWeight', 'AccumulatorSlice'], text=True))
        cls.modules = {module['name']: module for module in exported['modules']}

    def test_full_local_domains(self):
        for port in ('p0', 'p1', 'w0'):
            self.assertEqual(boundary_function(self.modules['BoundaryWeight'], port)['assignments_checked'], 128)
        self.assertEqual(weight_write_function(self.modules['BoundaryWeight'])['assignments_checked'], 1024)
        for bank in (0, 1):
            self.assertEqual(accumulator_bank(self.modules['AccumulatorSlice'], bank)['assignments_checked'], 16384)

    def test_wide_boolean_cutpoint_rejected(self):
        module = copy.deepcopy(self.modules['BoundaryWeight'])
        next(p for p in module['ports'] if p['name'] == 'p0CmdValid')['type'] = 'i2'
        with self.assertRaisesRegex(ValueError, 'identities or widths'):
            boundary_function(module, 'p0')

    def test_wrong_boundary_bit_rejected(self):
        module = copy.deepcopy(self.modules['BoundaryWeight'])
        next(op for op in module['operations'] if op['kind'] == 'comb.extract')['attributes']['lowBit'] = '4 : i32'
        with self.assertRaisesRegex(ValueError, 'boundary function mismatch'):
            boundary_function(module, 'p0')

    def test_feedback_undefined_and_state_rejected(self):
        for mutation in ('feedback', 'undefined', 'state'):
            module = copy.deepcopy(self.modules['BoundaryWeight'])
            op = next(op for op in module['operations'] if op['kind'] == 'comb.add')
            if mutation == 'feedback': op['operands'][0] = op['results'][0]
            if mutation == 'undefined': op['operands'][0] = 'undefined'
            if mutation == 'state': op['kind'] = 'seq.firreg'
            with self.subTest(mutation=mutation), self.assertRaisesRegex(ValueError, 'Feedback|Unsupported'):
                boundary_function(module, 'p0')

    def test_read_port_geometry_latency_and_wiring_rejected(self):
        for mutation in ('extra_port', 'latency', 'geometry', 'wire'):
            module = copy.deepcopy(self.modules['AccumulatorSlice'])
            memory = next(op for op in module['operations'] if op['kind'] == 'seq.firmem')
            read = next(op for op in module['operations'] if op['kind'] == 'seq.firmem.read_port')
            if mutation == 'extra_port':
                duplicate = copy.deepcopy(read)
                duplicate.update(id='extra', results=['extra.r0'])
                module['operations'].append(duplicate)
            if mutation == 'latency': memory['attributes']['readLatency'] = '2 : i32'
            if mutation == 'geometry': memory['result_types'] = ['!seq.firmem<64 x 512>']
            if mutation == 'wire': read['operands'][3] = Evaluator(module).graph.named('writeEn')
            with self.subTest(mutation=mutation), self.assertRaisesRegex(ValueError, 'port|latency|geometry|wiring'):
                accumulator_bank(module, 0)

    def test_read_priority_changed_rejected(self):
        module = copy.deepcopy(self.modules['AccumulatorSlice'])
        mux = next(op for op in module['operations'] if op['kind'] == 'comb.mux')
        mux['operands'][1:] = reversed(mux['operands'][1:])
        with self.assertRaisesRegex(ValueError, 'address selection mismatch'):
            accumulator_bank(module, 0)

    def test_weight_priority_changed_rejected(self):
        module = copy.deepcopy(self.modules['BoundaryWeight'])
        mux = next(op for op in module['operations'] if op['kind'] == 'comb.mux')
        mux['operands'][1:] = reversed(mux['operands'][1:])
        with self.assertRaisesRegex(ValueError, 'Weight write priority mismatch'):
            weight_write_function(module)


if __name__ == '__main__':
    EXPORTER = Path(sys.argv.pop(1)).resolve()
    unittest.main()

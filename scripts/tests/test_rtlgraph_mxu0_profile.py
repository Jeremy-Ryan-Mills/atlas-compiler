#!/usr/bin/env python3
"""Run with the typed CIRCT exporter as the first argument."""
import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from rtlgraph_mxu0_profile import MODULES, acc_read_function, projection, wrapper_wiring
from rtlgraph_query import find_instance, instance_value


class Mxu0ProfileTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture = Path(__file__).with_name('rtlgraph-mxu0-profile.mlir')
        cls.modules = cls.export(cls.fixture)

    @staticmethod
    def export(path):
        result = json.loads(subprocess.check_output([str(EXPORTER), str(path), *MODULES], text=True))
        return {module['name']: module for module in result['modules']}

    def guard(self):
        return copy.deepcopy(self.modules['SystolicArraySequencer'])

    def test_exhaustive_local_guard(self):
        report = acc_read_function(self.guard())
        self.assertEqual(report['assignments_checked'], 512)
        self.assertFalse(report['overwrite_reads_accumulator'])

    def test_overwrite_opcode_enables_read_rejected(self):
        module = self.guard()
        next(op for op in module['operations'] if op['kind'] == 'hw.constant'
             and op['result_types'] == ['i3'])['attributes']['value'] = '5 : i3'
        with self.assertRaisesRegex(ValueError, 'guard mismatch'):
            acc_read_function(module)

    def test_widened_opcode_with_extra_read_rejected(self):
        source = self.fixture.read_text().replace('i3', 'i4')
        source = source.replace('  hw.output %read,',
            '  %extra = hw.constant 14 : i4\n'
            '  %extra_read = comb.icmp eq %io_cmd_bits_op, %extra : i4\n'
            '  %wide_read = comb.or %read, %extra_read : i1\n'
            '  hw.output %wide_read,')
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'wide.mlir'
            path.write_text(source)
            module = self.export(path)['SystolicArraySequencer']
        with self.assertRaisesRegex(ValueError, 'identities or widths'):
            acc_read_function(module)

    def test_cutpoint_alias_and_boolean_width_rejected(self):
        for mutation in ('alias', 'width'):
            module = self.guard()
            ports = {p['name']: p for p in module['ports']}
            if mutation == 'alias': ports['acceptCompute']['value'] = ports['p0Boundary']['value']
            else: ports['acceptCompute']['type'] = 'i2'
            with self.subTest(mutation=mutation), self.assertRaisesRegex(ValueError, 'identities or widths'):
                acc_read_function(module)

    def test_feedback_undefined_state_and_unsupported_operation_rejected(self):
        for mutation in ('feedback', 'undefined', 'state', 'clock_adapter', 'unknown'):
            module = self.guard()
            mux = next(op for op in module['operations'] if op['kind'] == 'comb.mux')
            if mutation == 'feedback': mux['operands'][1] = mux['results'][0]
            if mutation == 'undefined': mux['operands'][1] = 'undefined'
            if mutation == 'state': mux['kind'] = 'seq.firreg'
            if mutation == 'clock_adapter': mux['kind'] = 'seq.from_clock'
            if mutation == 'unknown': mux['kind'] = 'comb.add'
            with self.subTest(mutation=mutation), self.assertRaisesRegex(ValueError, 'Feedback|Unsupported'):
                acc_read_function(module)

    def test_unlisted_input_constant_type_and_comparison_rejected(self):
        for mutation in ('input', 'constant', 'comparison'):
            module = self.guard()
            if mutation == 'input':
                mux = next(op for op in module['operations'] if op['kind'] == 'comb.mux')
                mux['operands'][1] = next(p['value'] for p in module['ports'] if p['name'] == 'store')
            if mutation == 'constant':
                next(op for op in module['operations'] if op['kind'] == 'hw.constant'
                     and op['result_types'] == ['i3'])['attributes']['value'] = '6 : i4'
            if mutation == 'comparison':
                next(op for op in module['operations'] if op['kind'] == 'comb.icmp')['attributes']['predicate'] = '1 : i64'
            with self.subTest(mutation=mutation), self.assertRaisesRegex(ValueError, 'Unsupported'):
                acc_read_function(module)

    def test_wrapper_identity_connections(self):
        self.assertEqual(len(wrapper_wiring(self.modules)['checks']), 8)

    def test_wrong_instance_binding_rejected(self):
        for module_name, instance in (('AtlasCore', 'mxu0'), ('SystolicArrayTop', 'seq'), ('SystolicArrayTop', 'accBuf')):
            modules = copy.deepcopy(self.modules)
            find_instance(modules[module_name], instance)['instance']['module'] = 'OtherHardware'
            with self.subTest(instance=instance), self.assertRaisesRegex(ValueError, 'module binding'):
                wrapper_wiring(modules)

    def test_wrong_instance_result_or_command_wire_rejected(self):
        for mutation in ('read_enable', 'command'):
            modules = copy.deepcopy(self.modules)
            top = modules['SystolicArrayTop']
            seq, acc = find_instance(top, 'seq'), find_instance(top, 'accBuf')
            if mutation == 'read_enable':
                index = acc['instance']['input_names'].index('io_computeReadEn')
                acc['operands'][index] = instance_value(seq, 'io_accStoreReadEn', 'output')
            else:
                index = seq['instance']['input_names'].index('io_cmd_bits_op')
                seq['operands'][index] = instance_value(seq, 'p0Cmd_op', 'input')
            with self.subTest(mutation=mutation), self.assertRaisesRegex(ValueError, 'wrapper wire'):
                wrapper_wiring(modules)

    def test_projection_has_only_one_model_override(self):
        fields = dict(line.split('=', 1) for line in projection('a' * 64, 'b' * 64).splitlines())
        self.assertEqual(fields, {'schema': 'atlas-mxu0-profile-v1', 'config': 'EE290SimConfig',
            'source_ir_sha256': 'a' * 64, 'evidence_sha256': 'b' * 64, 'overwrite_acc_read_hold': '0'})
        with self.assertRaisesRegex(ValueError, 'SHA-256'):
            projection('not-a-hash', 'b' * 64)


if __name__ == '__main__':
    EXPORTER = Path(sys.argv.pop(1)).resolve()
    unittest.main()

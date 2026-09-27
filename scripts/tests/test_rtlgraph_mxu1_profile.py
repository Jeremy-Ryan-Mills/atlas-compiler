#!/usr/bin/env python3
"""Run with the built typed CIRCT exporter as the first argument."""
import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).absolute().parents[1]))
from rtlgraph_mxu1_profile import valid_pipeline, acc_read_function, timing_agreement


def timing():
    transactions = []
    for index, op in enumerate(('Matmul', 'MatmulAcc')):
        transactions.append({'command': {'op': op}, 'accepted_cycle': index * 32,
                             'issue_to_accept_gap': 0, 'feed_boundary_age': 32, 'retire_age': 34,
                             'feed_to_write_gaps': [2] * 32,
                             **{field: {'ages': list(range(age, age + 32))}
                                for field, age in (('requests', 0), ('feeds', 1), ('writes', 3))}})
    return {'status': 'trace_obligations_passed', 'perf': {'status': 'scalar_issue_and_engine_events_matched'},
            'transactions': transactions}


class ProfileTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        data = json.loads(subprocess.check_output([str(EXPORTER), str(Path(__file__).with_name('rtlgraph-profile.mlir')),
                                                  'ValidPipeline', 'ReadGuard'], text=True))
        cls.modules = {m['name']: m for m in data['modules']}

    def test_derived_valid_latency_changes_with_register_chain(self):
        module = copy.deepcopy(self.modules['ValidPipeline'])
        self.assertEqual(valid_pipeline(module)['latency'], 2)
        regs = [op for op in module['operations'] if op['kind'] == 'seq.firreg']
        regs[-1]['operands'][0] = regs[0]['operands'][0]
        self.assertEqual(valid_pipeline(module)['latency'], 1)

    def test_reject_clock_reset_and_feedback_changes(self):
        for change in ('clock', 'reset', 'async', 'feedback', 'enable'):
            module = copy.deepcopy(self.modules['ValidPipeline'])
            reg = next(op for op in module['operations'] if op['kind'] == 'seq.firreg')
            if change == 'clock': reg['operands'][1] = reg['operands'][2]
            if change == 'reset': reg['operands'][3] = reg['operands'][0]
            if change == 'async': reg['attributes']['isAsync'] = 'unit'
            if change == 'feedback': reg['operands'][0] = reg['results'][0]
            if change == 'enable': reg['kind'] = 'seq.compreg.ce'
            with self.subTest(change=change), self.assertRaises((ValueError, KeyError)):
                valid_pipeline(module)

    def test_accumulator_guard_all_inputs(self):
        result = acc_read_function(self.modules['ReadGuard'])
        self.assertEqual(result['assignments_checked'], 512)
        self.assertFalse(result['overwrite_reads_accumulator'])

    def test_accumulator_guard_wrong_opcode(self):
        module = copy.deepcopy(self.modules['ReadGuard'])
        const = next(op for op in module['operations'] if op['kind'] == 'hw.constant' and op['result_types'] == ['i3'])
        const['attributes']['value'] = '5 : i3'
        with self.assertRaisesRegex(ValueError, 'guard mismatch'):
            acc_read_function(module)

    def test_wider_opcode_domain_rejected(self):
        # This valid circuit agrees on opcodes 0..7 but also reads for opcode 14.
        # Enumerating the old i3 domain alone would miss that extra behavior.
        source = Path(__file__).with_name('rtlgraph-profile.mlir').read_text().replace('i3', 'i4')
        source = source.replace('  hw.output %read : i1',
            '  %extra = hw.constant 14 : i4\n'
            '  %extra_read = comb.icmp eq %io_cmd_bits_op, %extra : i4\n'
            '  %widened_read = comb.or %read, %extra_read : i1\n'
            '  hw.output %widened_read : i1')
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'widened.mlir'
            path.write_text(source)
            data = json.loads(subprocess.check_output([str(EXPORTER), str(path), 'ReadGuard'], text=True))
        with self.assertRaisesRegex(ValueError, 'cutpoint types'):
            acc_read_function(data['modules'][0])

    def test_aliased_cutpoints_rejected(self):
        module = copy.deepcopy(self.modules['ReadGuard'])
        ports = {p['name']: p for p in module['ports']}
        ports['acceptCompute']['value'] = ports['p0Boundary']['value']
        with self.assertRaisesRegex(ValueError, 'distinct SSA'):
            acc_read_function(module)

    def test_unknown_boolean_operation_rejected(self):
        module = copy.deepcopy(self.modules['ReadGuard'])
        next(op for op in module['operations'] if op['kind'] == 'comb.mux')['kind'] = 'comb.add'
        with self.assertRaisesRegex(ValueError, 'Unsupported Boolean'):
            acc_read_function(module)

    def test_matching_trace(self):
        self.assertEqual(timing_agreement(timing(), 2)['first_write_age'], 3)

    def test_unaligned_issue_or_latency_rejected(self):
        record = timing()
        record['transactions'][0]['issue_to_accept_gap'] = 1
        with self.assertRaisesRegex(ValueError, 'Issue and acceptance'):
            timing_agreement(record, 2)
        with self.assertRaisesRegex(ValueError, 'timing differs'):
            timing_agreement(timing(), 4)

    def test_missing_overlap_or_accumulation_rejected(self):
        record = timing()
        record['transactions'][1]['accepted_cycle'] = 64
        with self.assertRaisesRegex(ValueError, 'back-to-back'):
            timing_agreement(record, 2)
        record['transactions'][1]['command']['op'] = 'Matmul'
        with self.assertRaisesRegex(ValueError, 'overwrite and accumulation'):
            timing_agreement(record, 2)


if __name__ == '__main__':
    EXPORTER = Path(sys.argv.pop(1)).resolve()
    unittest.main()

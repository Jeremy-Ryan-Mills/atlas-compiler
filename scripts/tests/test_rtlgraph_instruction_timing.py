#!/usr/bin/env python3
"""Mutation checks for typed hierarchical control extraction."""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from rtlgraph_instruction_timing import ControlCircuit


def operation(ident, kind, operands, typ='i1', **attrs):
    return {'id': ident, 'kind': kind, 'operands': operands, 'results': [ident],
            'result_types': [typ], 'attributes': attrs, 'has_regions': False}


def module(name='Pipe'):
    ports = [{'name': n, 'direction': 'input', 'value': n, 'type': t}
             for n, t in [('clock', '!seq.clock'), ('reset', 'i1'), ('req', 'i1'), ('payload', 'i8')]]
    ports.append({'name': 'out', 'direction': 'output', 'value': 'reg', 'type': 'i1'})
    return {'name': name, 'ports': ports,
            'arguments': [{'id': p['value'], 'type': p['type']} for p in ports if p['direction'] == 'input'],
            'operations': [operation('zero', 'hw.constant', [], value='false'),
                           operation('reg', 'seq.firreg', ['req', 'clock', 'reset', 'zero'], name='valid')]}


def build(m):
    return ControlCircuit({'modules': [m]}, m['name'], ['out'], {'clock', 'reset', 'req'})


def trace(c):
    c.cycle({'req': 0, 'reset': 1})
    return [c.cycle({'req': int(t == 0), 'reset': 0})['out'] for t in range(5)]


class ControlTests(unittest.TestCase):
    def test_pipeline_register_addition_changes_timing(self):
        m = module()
        self.assertEqual(trace(build(m)), [0, 1, 0, 0, 0])
        m['operations'].append(operation('stage2', 'seq.firreg', ['reg', 'clock', 'reset', 'zero'], name='valid2'))
        m['ports'][-1]['value'] = 'stage2'
        self.assertEqual(trace(build(m)), [0, 0, 1, 0, 0])

    def test_payload_control_dependency_rejected(self):
        m = module()
        m['operations'].insert(1, operation('select', 'comb.extract', ['payload'], lowBit='0 : i32'))
        m['operations'][-1]['operands'][0] = 'select'
        with self.assertRaisesRegex(ValueError, 'unapproved input: payload'):
            build(m)

    def test_enabled_feedback_is_executed(self):
        m = module()
        m['operations'].insert(1, operation('next', 'comb.mux', ['req', 'req', 'reg']))
        m['operations'][-1]['operands'][0] = 'next'
        self.assertEqual(trace(build(m)), [0, 1, 1, 1, 1])

    def test_alternate_clock_rejected(self):
        m = module()
        m['operations'][-1]['operands'][1] = 'req'
        with self.assertRaisesRegex(ValueError, 'clock'):
            build(m)

    def test_async_reset_rejected(self):
        m = module()
        m['operations'][-1]['attributes']['isAsync'] = 'unit'
        with self.assertRaisesRegex(ValueError, 'register attribute'):
            build(m)

    def test_unavailable_instance_rejected(self):
        m = module()
        m['operations'][-1] = operation('reg', 'hw.instance', ['req'], moduleName='@Missing',
                                       instanceName='child', argNames='["req"]', resultNames='["out"]')
        with self.assertRaisesRegex(ValueError, 'Missing module: Missing'):
            build(m)

    def test_instance_binding_follows_actual_inputs(self):
        m = module('Top')
        m['operations'][-1] = operation('reg', 'hw.instance', ['clock', 'reset', 'req', 'payload'],
                                       moduleName='@Pipe', instanceName='child',
                                       argNames='["clock", "reset", "req", "payload"]', resultNames='["out"]')
        c = ControlCircuit({'modules': [m, module()]}, 'Top', ['out'], {'clock', 'reset', 'req'})
        self.assertEqual(trace(c), [0, 1, 0, 0, 0])
        m['operations'][-1]['operands'][2] = 'payload'
        with self.assertRaisesRegex(ValueError, 'unapproved input'):
            ControlCircuit({'modules': [m, module()]}, 'Top', ['out'], {'clock', 'reset', 'req'})

    def test_unknown_operation_rejected(self):
        m = module()
        m['operations'][-1]['kind'] = 'seq.unknown_register'
        with self.assertRaisesRegex(ValueError, 'Unsupported timing operation'):
            build(m)

    def test_signed_and_unsigned_comparisons_differ(self):
        m = module()
        m['operations'] = [operation('lhs', 'hw.constant', [], 'i8', value='255 : i8'),
                           operation('rhs', 'hw.constant', [], 'i8', value='1 : i8'),
                           operation('reg', 'comb.icmp', ['lhs', 'rhs'], predicate='2 : i64')]
        self.assertEqual(build(m).cycle({})['out'], 1)
        m['operations'][-1]['attributes']['predicate'] = '6 : i64'
        self.assertEqual(build(m).cycle({})['out'], 0)

    def test_unknown_state_requires_reset_or_flush(self):
        m = module()
        m['operations'][-1]['operands'] = ['reg', 'clock']
        c = build(m)
        self.assertIsNone(c.cycle({'req': 0, 'reset': 1})['out'])
        self.assertIsNone(c.cycle({'req': 0, 'reset': 0})['out'])
        m['operations'][-1]['operands'] = ['req', 'clock']
        c = build(m)
        c.cycle({'req': 0, 'reset': 0})
        self.assertEqual(c.cycle({'req': 0, 'reset': 0})['out'], 0)

    def test_child_clock_binding_rejected(self):
        m = module('Top')
        m['operations'][-1] = operation('reg', 'hw.instance', ['req', 'reset', 'req', 'payload'],
                                       moduleName='@Pipe', instanceName='child',
                                       argNames='["clock", "reset", "req", "payload"]', resultNames='["out"]')
        with self.assertRaisesRegex(ValueError, 'clock'):
            ControlCircuit({'modules': [m, module()]}, 'Top', ['out'], {'clock', 'reset', 'req'})

    def test_array_indices_follow_hw_order(self):
        m = module()
        m['operations'] = [operation('zero', 'hw.constant', [], value='false'),
                           operation('one', 'hw.constant', [], value='true'),
                           operation('array', 'hw.array_create', ['one', 'zero'], '!hw.array<2xi1>'),
                           operation('reg', 'hw.array_get', ['array', 'zero'])]
        self.assertEqual(build(m).cycle({})['out'], 0)


if __name__ == '__main__':
    unittest.main()

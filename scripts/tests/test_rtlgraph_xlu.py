#!/usr/bin/env python3
"""Run unit checks, optionally adding the typed XluEngine JSON as first argument."""
import copy
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from rtlgraph_xlu import Bits, Circuit, derive, run

TYPED = json.loads(Path(sys.argv.pop(1)).read_text()) if len(sys.argv) > 1 and not sys.argv[1].startswith('-') else None


def operation(name, kind, operands, typ='i8', **attrs):
    return {'id': name, 'kind': kind, 'operands': operands, 'attributes': attrs,
            'has_regions': False, 'results': [name], 'result_types': [typ]}


def circuit(ops, output, inputs=None):
    inputs = inputs or {'clock': '!seq.clock', 'data': 'i16', 'select': 'i1'}
    return Circuit({'arguments': [{'id': k, 'type': t} for k, t in inputs.items()],
                    'ports': [{'name': k, 'value': k, 'direction': 'input'} for k in inputs] +
                             [{'name': 'result', 'value': output, 'direction': 'output'}],
                    'operations': ops})


class EvaluatorTests(unittest.TestCase):
    def test_symbolic_slice_concat_and_array_order(self):
        model = circuit([operation('lo', 'comb.extract', ['data'], lowBit='0 : i32'),
                         operation('hi', 'comb.extract', ['data'], lowBit='8 : i32'),
                         operation('packed', 'comb.concat', ['lo', 'hi'], typ='i16'),
                         operation('array', 'hw.array_create', ['hi', 'lo'], typ='!hw.array<2xi8>'),
                         operation('entry', 'hw.array_get', ['array', 'select']),
                         operation('selected', 'comb.concat', ['entry', 'entry'], typ='i16')], 'selected')
        payload = Bits(tuple(('bit', i) for i in range(16)))
        output, _ = model.cycle({'clock': 0, 'data': payload, 'select': 0})
        self.assertEqual(output('result'), Bits(payload.values[:8] * 2))
        model.ports['result']['value'] = 'packed'
        self.assertEqual(output('result'), Bits(payload.values[8:] + payload.values[:8]))

    def test_arithmetic_wraps_to_circt_width(self):
        model = circuit([operation('one', 'hw.constant', [], value='1 : i8'),
                         operation('sum', 'comb.add', ['data', 'one'])], 'sum', {'clock': '!seq.clock', 'data': 'i8'})
        output, _ = model.cycle({'clock': 0, 'data': 255})
        self.assertEqual(output('result'), 0)

    def test_unknown_register_state_cannot_silently_become_zero(self):
        model = circuit([operation('held', 'seq.firreg', ['data', 'clock'])], 'held',
                        {'clock': '!seq.clock', 'data': 'i8', 'select': 'i1'})
        output, advance = model.cycle({'clock': 0, 'data': 42, 'select': 0})
        self.assertIsInstance(output('result'), Bits)
        advance()
        output, _ = model.cycle({'clock': 0, 'data': 99, 'select': 0})
        self.assertEqual(output('result'), 42)

    def test_data_dependent_control_is_rejected(self):
        model = circuit([operation('zero', 'hw.constant', [], value='0 : i8'),
                         operation('same', 'comb.icmp', ['data', 'zero'], typ='i1', predicate='0 : i64')], 'same')
        output, _ = model.cycle({'clock': 0, 'data': Bits(tuple(range(16))), 'select': 0})
        with self.assertRaisesRegex(ValueError, 'Unsupported symbolic operation'):
            output('result')

    def test_unsupported_operation_is_rejected(self):
        with self.assertRaisesRegex(ValueError, 'Unsupported operation'):
            circuit([operation('value', 'comb.shl', ['data', 'select'])], 'value')

    def test_unrelated_reset_cannot_justify_initial_state(self):
        with self.assertRaisesRegex(ValueError, 'Unsupported register reset signal'):
            circuit([operation('zero', 'hw.constant', [], value='0 : i8'),
                     operation('held', 'seq.firreg', ['data', 'clock', 'select', 'zero'])], 'held',
                    {'clock': '!seq.clock', 'data': 'i8', 'select': 'i1', 'reset': 'i1'})


@unittest.skipUnless(TYPED, 'Supply the typed XluEngine export for hardware regressions')
class XluTests(unittest.TestCase):
    def test_pinned_timing_and_symbolic_transpose(self):
        self.assertEqual(derive(TYPED), {'read_age': 1, 'write_age': 34, 'first_free_age': 66})

    def test_response_delay_changes_completion(self):
        self.assertEqual(run(TYPED['modules'][0], response_latency=3),
                         {'read_age': 1, 'write_age': 36, 'first_free_age': 68})

    def test_mutated_payload_routing_is_rejected(self):
        mutated = copy.deepcopy(TYPED)
        module = mutated['modules'][0]
        response = next(p['value'] for p in module['ports'] if p['name'] == 'io_mregReadResp_bits')
        wires = {op['results'][0] for op in module['operations'] if op['kind'] == 'hw.wire' and op['operands'] == [response]}
        extract = next(op for op in module['operations'] if op['kind'] == 'comb.extract' and op['operands'][0] in wires)
        extract['attributes']['lowBit'] = '8 : i32'
        with self.assertRaisesRegex(ValueError, 'Incorrect transpose routing'):
            derive(mutated)

    def test_mutated_progression_is_rejected(self):
        mutated = copy.deepcopy(TYPED)
        increment = next(op for op in mutated['modules'][0]['operations']
                         if op['kind'] == 'hw.constant' and op['attributes']['value'] == '1 : i6')
        increment['attributes']['value'] = '2 : i6'
        with self.assertRaises(ValueError):
            derive(mutated)


if __name__ == '__main__':
    unittest.main()

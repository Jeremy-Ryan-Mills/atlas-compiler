#!/usr/bin/env python3
"""Adversarial VPU event ownership tests with independently specified timing."""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from rtlgraph_vpu_trace import check_samples, decode_vpu
from rtlgraph_vpu_vcd import SIGNALS


def word(funct, vd, vs1=0, vs2=0):
    return funct << 25 | vs2 << 19 | vs1 << 13 | vd << 7 | 0x57


def witness(kind='unary', first_write=2, start=2, samples=None, slot=1):
    op, funct, count, write_count = 9, 0x42, 64, 64
    if kind == 'row': op, funct, count, write_count = 13, 0x21, 32, 32
    if kind == 'column': op, funct, count, write_count = 14, 0x01, 128, 64
    if kind in ('binary', 'duplicate'): op, funct = 2, 0x03
    if kind == 'vli': op, funct, count, write_count = 29, None, 0, 64
    slots = (1, 2) if kind in ('row', 'binary', 'duplicate') else (slot,)
    vd, vs1, vs2 = 8, 2, 2 if kind == 'duplicate' else 4
    end = start + first_write + write_count + 2
    if samples is None: samples = [{key: 0 for key in SIGNALS} for _ in range(end)]
    while len(samples) < end: samples.append({key: 0 for key in SIGNALS})
    encoding = word(funct, vd, vs1, vs2) if funct is not None else (0x3000 << 16 | vd << 7 | 0x5f)
    samples[start].update({'scalar.fire': 1, 'scalar.pc': start, 'scalar.instr': encoding,
                          'cmd.valid': 1, 'cmd.op': op + 1, 'cmd.vd': vd, 'cmd.vs1': vs1, 'cmd.vs2': vs2,
                          'inst_fire': 1, 'inst_type': op, 'ready': 1})
    for s in slots:
        for cycle in range(start + 1, end):
            samples[cycle][f'slot{s}.resident_op'] = op if s == 1 or len(slots) == 1 else 0
        for row in range(count):
            base = vs1 + s - 1 if kind == 'row' else vs2 if s == 2 and len(slots) == 2 else vs1
            bank = base if kind == 'row' else base + row // 32 % 2
            cycle = start + row
            samples[cycle].update({f'logical.read{s}.valid': 1, f'logical.read{s}.mreg': bank, f'logical.read{s}.row': row % 32})
            if not (kind == 'duplicate' and s == 2):
                samples[cycle].update({f'physical.read{s - 1}.valid': 1, f'physical.read{s - 1}.mreg': bank,
                                       f'physical.read{s - 1}.row': row % 32})
                samples[cycle + 1][f'physical.response{s - 1}'] = 1
            if not (kind in ('binary', 'duplicate') and s == 2):
                samples[cycle + 1][f'slot{s}.response'] = 1
            if kind == 'duplicate':
                samples[cycle]['mirrored'] = 1
                samples[cycle + 1]['mirrored_d'] = 1
        if s == 2 and kind in ('binary', 'duplicate'): continue
        for row in range(write_count):
            cycle = start + first_write + row
            bank = vd + s - 1 if kind == 'row' else vd + row // 32
            samples[cycle].update({f'physical.write{s - 1}.valid': 1, f'physical.write{s - 1}.mreg': bank,
                                  f'physical.write{s - 1}.row': row % 32, f'slot{s}.result': 1})
        samples[start + first_write + write_count - 1][f'slot{s}.done'] = 1
    return samples


def check(samples):
    return check_samples((cycle, cycle * 2000, sample) for cycle, sample in enumerate(samples))


class VpuTraceTests(unittest.TestCase):
    def test_independently_measured_unary_ages(self):
        for age in (2, 5, 17):
            with self.subTest(age=age):
                command, = check(witness(first_write=age))['commands']
                self.assertEqual(command['reads']['1']['ages'], list(range(64)))
                self.assertEqual(command['responses']['1']['ages'], list(range(1, 65)))
                self.assertEqual(command['writes']['1']['first_age'], age)
                self.assertEqual(command['release_age'], age + 63)

    def test_row_reduce_binary_duplicate_and_write_only(self):
        for kind, lag in (('row', 7), ('binary', 2), ('duplicate', 2), ('vli', 1)):
            with self.subTest(kind=kind):
                result = check(witness(kind, lag))
                self.assertEqual(result['mirrored_read_count'], 64 if kind == 'duplicate' else 0)
                self.assertEqual(result['command_count'], 1)

    def test_simultaneous_unary_slots(self):
        samples = witness(first_write=2)
        # A second different independent unary uses the other slot while the
        # first remains live. Remap its operands/destination to disjoint banks.
        second = witness(first_write=3, start=3, slot=2)
        for i, sample in enumerate(second):
            for k, val in sample.items():
                if val and (k.startswith(('logical.read2.', 'physical.read1.', 'physical.write1.', 'slot2.')) or k=='physical.response1'):
                    if i >= len(samples): samples.append({key: 0 for key in SIGNALS})
                    samples[i][k] = val + 16 if k.endswith('.mreg') else val
        launch = second[3]
        samples[3].update({key: val for key, val in launch.items() if key.startswith(('scalar.', 'cmd.', 'inst_')) or key=='ready'})
        samples[3].update({'scalar.instr': word(0x4d,24,18,20), 'cmd.op':5,'cmd.vd':24,'cmd.vs1':18,'cmd.vs2':20,'inst_type':4})
        for i in range(4,len(samples)): samples[i]['slot2.resident_op']=4
        result = check(samples)
        self.assertEqual(result['maximum_inflight_commands'], 2)
        self.assertEqual(result['commands'][1]['slots'], [2])

    def test_wrong_command_and_busy_launch_rejected(self):
        for key, change, message in (('cmd.vd', 1, 'operands'), ('cmd.op', 1, 'opcode'),
                                     ('inst_fire', -1, 'unmatched'), ('ready', -1, 'busy')):
            samples = witness()
            samples[2][key] += change
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, message): check(samples)

    def test_wrong_rows_responses_and_release_rejected(self):
        for cycle, key, change, message in (
            (10, 'logical.read1.row', 1, 'operand'), (10, 'physical.read0.row', 1, 'Physical read address'),
            (10, 'physical.response0', -1, 'response'), (10, 'slot1.response', -1, 'consumed'),
            (10, 'physical.write0.row', 1, 'destination'), (10, 'slot1.done', 1, 'release'),
            (10, 'slot1.result', -1, 'result pulse'), (10, 'slot1.resident_op', 1, 'Resident')):
            samples = witness()
            samples[cycle][key] += change
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, message): check(samples)

    def test_mirrored_second_request_and_bad_tag_rejected(self):
        for cycle,key in ((10,'physical.read1.valid'),(10,'mirrored_d')):
            samples=witness('duplicate'); samples[cycle][key]^=1
            with self.subTest(key=key),self.assertRaisesRegex(ValueError,'suppression|Mirrored'):check(samples)

    def test_late_unowned_write_rejected(self):
        samples=witness();samples[-1]['physical.write0.valid']=1
        with self.assertRaisesRegex(ValueError,'no accepted'):check(samples)

    def test_unknown_missing_truncated_reset_and_cycle_gap_rejected(self):
        for mutation in ('unknown','missing','truncated','reset','gap'):
            samples=witness()
            if mutation=='unknown':samples[10]['logical.read1.row']=None
            if mutation=='missing':del samples[10]['physical.read0.valid']
            if mutation=='truncated':samples=samples[:20]
            if mutation=='reset':samples[10]['reset']=1
            with self.subTest(mutation=mutation),self.assertRaises(ValueError):
                if mutation=='gap':check_samples((i+int(i>10),i*2000,s) for i,s in enumerate(samples))
                else:check(samples)

    def test_reuse_slot_on_previous_final_write(self):
        samples=witness()
        release=2+2+63
        incoming=witness(start=release,first_write=2)
        while len(samples)<len(incoming):samples.append({key:0 for key in SIGNALS})
        for cycle,sample in enumerate(incoming):
            for key,val in sample.items():
                if val:samples[cycle][key]=val
        result=check(samples)
        self.assertEqual(result['command_count'],2)
        self.assertEqual(result['commands'][1]['accepted_cycle'],result['commands'][0]['release_cycle'])

    def test_column_reduction_discards_first_pass_results(self):
        samples=witness('column',first_write=66)
        for cycle in range(4,68):samples[cycle]['slot1.result']=1
        command,=check(samples)['commands']
        self.assertEqual(command['reads']['1']['ages'],list(range(128)))
        self.assertEqual(command['writes']['1']['ages'],list(range(66,130)))
        self.assertEqual(command['release_age'],129)

    def test_incomplete_stream_cannot_release(self):
        samples=witness()
        for key in ('logical.read1.valid','physical.read0.valid'):
            samples[65][key]=0
        samples[66]['physical.response0']=0
        samples[66]['slot1.response']=0
        with self.assertRaisesRegex(ValueError,'complete operand stream'):check(samples)

    def test_decoder_rejects_reserved_encodings(self):
        for encoding in (0x7f<<25|0x57,4<<13|0x5f):
            with self.assertRaisesRegex(ValueError,'Unsupported'):decode_vpu(encoding)
        self.assertIsNone(decode_vpu(0x13))


if __name__ == '__main__': unittest.main()

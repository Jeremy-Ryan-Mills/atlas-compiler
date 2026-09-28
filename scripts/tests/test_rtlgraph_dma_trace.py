#!/usr/bin/env python3
"""Independent DMA event accounting and adversarial buffer/completion cases."""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from rtlgraph_dma_trace import Events, decode_dma
from rtlgraph_dma_vcd import SIGNALS
from rtlgraph_lsu_vcd import SIGNALS as LSU_SIGNALS
from rtlgraph_perf import capture_signal_map
import rtlgraph_dma_vcd as selection


def capture(kind='load', gap=0, reorder=False, deny_write=False):
    op = int(kind == 'store')
    reads = [1 + gap, 2 + gap] if op else []
    requests = [4 + gap, 5 + gap] if op else [1 + gap, 2 + gap]
    replies = [7 + gap, 8 + gap] if op else [4 + gap, 5 + gap]
    if deny_write:
        assert not op
        replies = [5 + gap, 6 + gap]
    completion = replies[-1] + 1
    tags = [1, 0] if reorder else [0, 1]
    samples = []
    word = op << 25 | 3 << 20 | 2 << 15 | 1 << 7 | 0x7b
    for cycle in range(completion + 2):
        s = {key: 0 for key in SIGNALS}
        s['scalar.instr'] = 0x13
        if cycle == 0:
            s.update({'scalar.fire': 1, 'scalar.valid': 1, 'scalar.instr': word,
                      'scalar.rs1': 0 if op else 4096, 'scalar.rd': 4096 if op else 0, 'scalar.rs2': 64,
                      'dma.cmd.valid': 1, 'dma.cmd.op': op, 'dma.cmd.dram': 4096, 'dma.cmd.size': 64})
        if 1 <= cycle < completion:
            s.update({'dma.slot0.active': 1, 'dma.slot0.op': op, 'dma.slot0.dram': 4096, 'dma.slot0.size': 64,
                      'dma.busy0': 1, 'scalar.dma_busy0': 1,
                      'dma.slot0.outstanding': sum(t < cycle for t in requests) - sum(t < cycle for t in replies),
                      'dma.slot0.dispatched': int(requests[-1] < cycle)})
        if cycle in requests:
            row = requests.index(cycle)
            s.update({'dma.tl_a.valid': 1, 'dma.tl_a.ready': 1, 'dma.tl_a.source': row,
                      'dma.tl_a.address': 4096 + 32 * row, 'dma.tl_a.opcode': 0 if op else 4,
                      'dma.request_beat': row})
        if cycle in replies or (deny_write and cycle == replies[0] - 1):
            granted = cycle in replies
            row = tags[replies.index(cycle)] if granted else tags[0]
            s.update({'dma.tl_d.valid': 1, 'dma.tl_d.ready': int(granted), 'dma.tl_d.source': row})
            if not op:
                s.update({'vmem.dma_write.valid': 1, 'vmem.dma_write.grant': int(granted), 'vmem.dma_write.row': row})
        if cycle in reads:
            s.update({'vmem.dma_read.valid': 1, 'vmem.dma_read.grant': 1, 'vmem.dma_read.row': reads.index(cycle)})
        if cycle - 1 in reads:
            s['dma.vmem_response'] = 1
        if requests[-1] + 1 <= cycle <= completion:
            s.update({'scalar.valid': 1, 'scalar.fire': int(cycle == completion),
                      'scalar.stall': int(cycle != completion), 'scalar.instr': 0x0200007f})
        samples.append(s)
    return samples


def concurrent_loads():
    """Two saved command slots, interleaved OOO responses, distinct buffers."""
    records = [dict(issue=0, channel=0, line=0, dram=4096, req=(1, 2), resp=(6, 8), tags=(0, 1)),
               dict(issue=3, channel=1, line=128, dram=8192, req=(4, 5), resp=(7, 9), tags=(2, 3))]
    samples = []
    for cycle in range(12):
        s = {key: 0 for key in SIGNALS}
        s['scalar.instr'] = 0x13
        for slot, r in enumerate(records):
            if cycle == r['issue']:
                s.update({'scalar.fire': 1, 'scalar.valid': 1, 'scalar.instr': 0x7b | slot << 12,
                          'scalar.rd': r['line'] * 8, 'scalar.rs1': r['dram'], 'scalar.rs2': 64,
                          'dma.enqueue': slot, 'dma.cmd.valid': 1, 'dma.cmd.channel': slot,
                          'dma.cmd.line': r['line'], 'dma.cmd.dram': r['dram'], 'dma.cmd.size': 64})
            if r['issue'] < cycle <= max(r['resp']):
                s.update({f'dma.slot{slot}.active': 1, f'dma.slot{slot}.channel': slot,
                          f'dma.slot{slot}.line': r['line'], f'dma.slot{slot}.dram': r['dram'],
                          f'dma.slot{slot}.size': 64, f'dma.busy{slot}': 1, f'scalar.dma_busy{slot}': 1,
                          f'dma.slot{slot}.dispatched': int(max(r['req']) < cycle),
                          f'dma.slot{slot}.outstanding': sum(t < cycle for t in r['req']) - sum(t < cycle for t in r['resp'])})
            if cycle in r['req']:
                row = r['req'].index(cycle)
                s.update({'dma.request_slot': slot, 'dma.request_beat': row,
                          'dma.tl_a.valid': 1, 'dma.tl_a.ready': 1, 'dma.tl_a.opcode': 4,
                          'dma.tl_a.address': r['dram'] + row * 32, 'dma.tl_a.source': r['tags'][row]})
            if cycle in r['resp']:
                row = r['resp'].index(cycle)
                s.update({'dma.tl_d.valid': 1, 'dma.tl_d.ready': 1, 'dma.tl_d.source': r['tags'][row],
                          'vmem.dma_write.valid': 1, 'vmem.dma_write.grant': 1, 'vmem.dma_write.row': r['line'] + row})
        if 6 <= cycle <= 10:
            channel = int(cycle == 10)
            s.update({'scalar.valid': 1, 'scalar.fire': int(cycle >= 9),
                      'scalar.stall': int(cycle < 9), 'scalar.instr': 0x0200007f | channel << 12})
        samples.append(s)
    return samples


def check(samples, lsu_owners=None):
    events = Events()
    for cycle, sample in enumerate(samples):
        events.step(cycle, sample, lsu_owners)
    return events.finish()


class DmaTraceTests(unittest.TestCase):
    def test_map_preserves_lsu_and_dispatch(self):
        self.assertEqual(len(SIGNALS), 204)
        self.assertTrue(all(SIGNALS[k] == v for k, v in LSU_SIGNALS.items()))
        self.assertEqual(capture_signal_map(selection, banks=False, dma=True), SIGNALS)
        with self.assertRaisesRegex(ValueError, 'mutually exclusive'):
            capture_signal_map(selection, banks=False, dma=True, lsu=True)

    def test_decode(self):
        self.assertEqual(decode_dma(0x0200307f), {'kind': 'wait', 'channel': 3})
        self.assertEqual(decode_dma(0x0200307b), {'kind': 'store', 'op': 1, 'channel': 3})
        self.assertIsNone(decode_dma(0x13))
        self.assertIsNone(decode_dma(0x7f))
        with self.assertRaisesRegex(ValueError, 'launch encoding'): decode_dma(0x0400007b)

    def test_load_store_and_measured_latency(self):
        for kind in ('load', 'store'):
            early, late = check(capture(kind)), check(capture(kind, gap=3))
            self.assertEqual(early['command_count'], 1)
            self.assertEqual(late['commands'][0]['completion_age'] - early['commands'][0]['completion_age'], 3)
            self.assertEqual(len(early['commands'][0]['responses']['ages']), 2)
            self.assertEqual(early['waits'][0]['completion_cycle'], early['commands'][0]['completion_cycle'])

    def test_out_of_order_responses(self):
        self.assertEqual(check(capture(reorder=True))['command_count'], 1)

    def test_denied_write_is_not_completion(self):
        report = check(capture(deny_write=True))
        self.assertEqual(report['dma_requests_denied_edges']['write'], 1)
        self.assertEqual(report['commands'][0]['completion_age'], 7)
        self.assertEqual(len(report['commands'][0]['vmem_accesses']['ages']), 2)

    def test_concurrent_slots_and_interleaved_replies(self):
        report = check(concurrent_loads())
        self.assertEqual(report['command_count'], 2)
        self.assertEqual(report['maximum_busy_channels'], 2)
        self.assertEqual([c['completion_cycle'] for c in report['commands']], [9, 10])

    def test_concurrent_conflicting_buffer_rejected(self):
        s = concurrent_loads(); s[3]['dma.cmd.line'] = 0; s[3]['scalar.rd'] = 0
        with self.assertRaisesRegex(ValueError, 'conflicting live VMEM buffers'): check(s)

    def test_concurrent_response_wrong_owner(self):
        s = concurrent_loads(); s[7]['dma.tl_d.source'] = 1
        with self.assertRaisesRegex(ValueError, 'wrong VMEM line'): check(s)

    def test_saved_command_mutation(self):
        s = capture(); s[3]['dma.slot0.size'] = 32
        with self.assertRaisesRegex(ValueError, 'Saved DMA command'): check(s)

    def test_command_binding(self):
        for key, bad in (('dma.cmd.size', 96), ('dma.cmd.line', 1), ('dma.cmd.channel', 1), ('dma.cmd.dram', 8192)):
            s = capture(); s[0][key] = bad
            with self.assertRaises(ValueError): check(s)

    def test_scalar_vmem_region_alias(self):
        for kind, key in (('load', 'scalar.rd'), ('store', 'scalar.rs1')):
            s = capture(kind); s[0][key] = 0x20000000
            self.assertEqual(check(s)['commands'][0]['line'], 0)

    def test_unmapped_bank_range(self):
        s = capture(); s[0]['dma.cmd.line'] = 6 * 8192
        s[0]['scalar.rd'] = 0x20000000 + 6 * 8192 * 8
        with self.assertRaisesRegex(ValueError, 'exceeds VMEM'): check(s)

    def test_grant_requires_request(self):
        s = capture(); s[3]['vmem.dma_read.grant'] = 1
        with self.assertRaisesRegex(ValueError, 'grant without a request'): check(s)

    def test_dma_read_write_bank_conflict(self):
        s = capture(); s[4].update({'vmem.dma_read.valid': 1, 'vmem.dma_read.grant': 1})
        with self.assertRaisesRegex(ValueError, 'same physical VMEM bank'): check(s)

    def test_outstanding_counter(self):
        s = capture(); s[3]['dma.slot0.outstanding'] = 1
        with self.assertRaisesRegex(ValueError, 'outstanding count'): check(s)

    def test_wait_cannot_fire_busy(self):
        s = capture(); s[3]['scalar.fire'] = 1
        with self.assertRaisesRegex(ValueError, 'WAIT issued'): check(s)

    def test_busy_wait_stalls_frontend(self):
        s = capture(); s[3]['scalar.stall'] = 0
        with self.assertRaisesRegex(ValueError, 'WAIT issued'): check(s)

    def test_early_release(self):
        s = capture(); s[3]['dma.slot0.active'] = 0
        with self.assertRaisesRegex(ValueError, 'released before'): check(s)

    def test_channel_mapping(self):
        s = capture(); s[3]['scalar.dma_busy0'] = 0
        with self.assertRaisesRegex(ValueError, 'Scalar DMA busy'): check(s)

    def test_unknown_response_source(self):
        s = capture(); s[4]['dma.tl_d.source'] = 2
        with self.assertRaisesRegex(ValueError, 'no outstanding source'): check(s)

    def test_source_reuse(self):
        s = capture(); s[2]['dma.tl_a.source'] = 0
        with self.assertRaisesRegex(ValueError, 'still-outstanding'): check(s)

    def test_response_address(self):
        s = capture(); s[4]['vmem.dma_write.row'] = 1
        with self.assertRaisesRegex(ValueError, 'wrong VMEM line'): check(s)

    def test_response_grant(self):
        s = capture(); s[4]['vmem.dma_write.grant'] = 0
        with self.assertRaisesRegex(ValueError, 'without VMEM write grant'): check(s)

    def test_request_address(self):
        s = capture(); s[2]['dma.tl_a.address'] = 4096
        with self.assertRaisesRegex(ValueError, 'request address'): check(s)

    def test_store_response(self):
        s = capture('store'); s[2]['dma.vmem_response'] = 0
        with self.assertRaisesRegex(ValueError, 'previous granted read'): check(s)

    def test_store_read_address(self):
        s = capture('store'); s[2]['vmem.dma_read.row'] = 0
        with self.assertRaisesRegex(ValueError, 'wrong VMEM line'): check(s)

    def test_live_load_destination(self):
        s = capture(); s[3].update({'lsu.vmem.load.valid': 1, 'lsu.vmem.load.row': 1})
        with self.assertRaisesRegex(ValueError, 'live conflicting'): check(s)

    def test_live_store_source(self):
        s = capture('store'); s[3].update({'lsu.vmem.store.valid': 1, 'lsu.vmem.store.row': 1})
        with self.assertRaisesRegex(ValueError, 'live conflicting'): check(s)
        s[3]['lsu.vmem.store.valid'] = 0; s[3]['lsu.vmem.load.valid'] = 1
        self.assertEqual(check(s)['command_count'], 1)

    def test_independent_buffer_overlap(self):
        s = capture(); s[3].update({'lsu.vmem.load.valid': 1, 'lsu.vmem.load.row': 100, 'physical.write0.valid': 1})
        report = check(s)
        self.assertEqual(report['dma_busy_with_vpu_row_edges'], 1)
        self.assertEqual(report['dma_busy_with_lsu_vmem_edges'], 1)

    def test_launch_before_lsu_store_completion(self):
        s = capture('store'); s[0]['lsu.store.busy'] = 1
        with self.assertRaisesRegex(ValueError, 'unfinished LSU buffer'):
            check(s, {'store': {'base_line': 0}})
        # A prior owner's first idle edge is safe for a new DMA launch.
        s[0]['lsu.store.busy'] = 0
        self.assertEqual(check(s, {'store': {'base_line': 0}})['command_count'], 1)

    def test_busy_slot_relaunch(self):
        s = capture(); s[2].update(s[0]); s[2].update({'dma.slot0.active': 1, 'dma.slot0.op': 0,
            'dma.slot0.dram': 4096, 'dma.slot0.size': 64, 'dma.slot0.outstanding': 1,
            'dma.busy0': 1, 'scalar.dma_busy0': 1})
        with self.assertRaisesRegex(ValueError, 'busy slot/channel'): check(s)

    def test_missing_explicit_wait(self):
        s = capture(); s[-2]['scalar.fire'] = 0
        with self.assertRaisesRegex(ValueError, 'lacks an explicit'): check(s)

    def test_duplicate_wait(self):
        s = capture(); s[-1].update({'scalar.valid': 1, 'scalar.fire': 1, 'scalar.instr': 0x0200007f})
        with self.assertRaisesRegex(ValueError, 'no unmatched accepted'): check(s)

    def test_truncation(self):
        with self.assertRaisesRegex(ValueError, 'truncated'): check(capture()[:-3])


if __name__ == '__main__':
    unittest.main()

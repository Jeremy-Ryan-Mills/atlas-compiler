#!/usr/bin/env python3
"""Bind DMA commands, memory grants and completion to observed scalar issues.

Ownership is established from accepted instructions and source-tag handshakes,
not hypothesized DMA latencies. VMEM buffer reservations are conservative until
actual channel completion. Payload correctness remains the full host golden.
"""
import argparse
from collections import deque
import json
from pathlib import Path

from rtlgraph_completion import summarize as completion_summary
from rtlgraph_dma_vcd import SIGNALS
from rtlgraph_lsu_trace import Events as LsuEvents
from rtlgraph_mxu1_capture import verify_artifact
from rtlgraph_mxu1_trace import series
from rtlgraph_mxu1_vcd import edge_samples, read_header
from rtlgraph_s0 import artifact, checked_path
from rtlgraph_vpu_trace import Events as VpuEvents, require


def value(sample, key):
    result = sample.get(key)
    require(type(result) is int and 0 <= result < 1 << SIGNALS[key][1],
            'Unknown, missing, or out-of-range ' + key)
    return result


def decode_dma(word):
    require(type(word) is int and 0 <= word < 1 << 32, 'Invalid scalar instruction')
    if word & 0x7f == 0x7b:
        op = word >> 25
        require(op in (0, 1), 'Unsupported DMA launch encoding')
        return {'kind': 'load' if op == 0 else 'store', 'op': op, 'channel': word >> 12 & 7}
    if word & ~(7 << 12) == 0x0200007f:
        return {'kind': 'wait', 'channel': word >> 12 & 7}
    return None


def overlaps(a, n, b, m):
    return a < b + m and b < a + n


class Events:
    def __init__(self):
        self.commands, self.waits = [], []
        self.slots = [None] * 8
        self.channels = [None] * 8
        self.latest = [None] * 8
        self.tags, self.store_fifo = {}, deque()
        self.previous_read = None
        self.last_cycle = None
        self.sample_count = self.wait_stall_edges = self.busy_with_vpu_edges = 0
        self.busy_with_lsu_edges = self.maximum_busy_channels = 0
        self.request_denied = {'read': 0, 'write': 0}

    def step(self, cycle, sample, lsu_owners=None):
        require(type(cycle) is int and (self.last_cycle is None or cycle == self.last_cycle + 1),
                'Missing or reordered sample cycle')
        self.last_cycle, self.sample_count = cycle, self.sample_count + 1
        if sample.get('reset') != 0:
            require(not self.commands, 'Reset or unknown reset after DMA execution began')
            return

        # Inspect registered state before processing this edge's handshakes.
        for slot, owner in enumerate(self.slots):
            active = value(sample, f'dma.slot{slot}.active')
            outstanding = value(sample, f'dma.slot{slot}.outstanding')
            dispatched = value(sample, f'dma.slot{slot}.dispatched')
            if owner is None:
                require(not active and not outstanding and not dispatched, 'Active DMA slot lacks an accepted owner')
                continue
            expected = len(owner['requests']) - len(owner['responses'])
            require(outstanding == expected, 'DMA outstanding count disagrees with independent tag accounting')
            if not active:
                require(len(owner['requests']) == len(owner['responses']) == owner['beats'],
                        'DMA slot released before all independent requests/responses completed')
                require(len(owner['vmem_accesses']) == owner['beats'], 'DMA released before all VMEM grants')
                require(not dispatched, 'Retired DMA slot still dispatched')
                require(not any(tag_owner is owner for tag_owner, _ in self.tags.values()), 'Retired DMA has live source IDs')
                owner['completion_cycle'] = cycle
                self.channels[owner['channel']] = None
                self.slots[slot] = None
                continue
            require(dispatched == (len(owner['requests']) == owner['beats']), 'DMA dispatched state disagrees with counted request beats')
            for key in ('op', 'channel', 'line', 'dram', 'size'):
                require(value(sample, f'dma.slot{slot}.{key}') == owner[key], 'Saved DMA command changed while active')
        for channel, owner in enumerate(self.channels):
            busy = value(sample, f'dma.busy{channel}')
            require(busy == (owner is not None), 'DMA channel busy differs from independently owned slot')
            require(value(sample, f'scalar.dma_busy{channel}') == busy, 'Scalar DMA busy differs from engine completion')

        # An issued wait must observe completion; a presented busy wait stalls.
        fire = value(sample, 'scalar.fire')
        presented = decode_dma(value(sample, 'scalar.instr')) if value(sample, 'scalar.valid') else None
        if presented and presented['kind'] == 'wait':
            channel = presented['channel']
            if self.channels[channel] is not None:
                require(not fire and value(sample, 'scalar.stall'), 'DMA.WAIT issued while its channel remained busy')
                self.wait_stall_edges += 1
            elif fire:
                owner = self.latest[channel]
                require(owner is not None and owner['wait_cycle'] is None, 'DMA.WAIT has no unmatched accepted command')
                owner['wait_cycle'] = cycle
                self.waits.append({'channel': channel, 'cycle': cycle, 'scalar_pc': value(sample, 'scalar.pc'),
                                   'command_id': owner['id'] if owner else None,
                                   'completion_cycle': owner['completion_cycle'] if owner else None})
        decoded = decode_dma(value(sample, 'scalar.instr')) if fire else None
        launch = decoded is not None and decoded['kind'] != 'wait'
        require(value(sample, 'dma.cmd.valid') == launch, 'DMA command does not match scalar launch')
        if launch:
            slot, channel = value(sample, 'dma.enqueue'), decoded['channel']
            require(self.slots[slot] is None and self.channels[channel] is None,
                    'DMA launch overwrites a busy slot/channel')
            fields = {key: value(sample, 'dma.cmd.' + key) for key in ('op', 'channel', 'line', 'dram', 'size')}
            require(fields['op'] == decoded['op'] and fields['channel'] == channel, 'DMA launch opcode/channel mismatch')
            vmem = value(sample, 'scalar.rd' if decoded['kind'] == 'load' else 'scalar.rs1')
            dram = value(sample, 'scalar.rs1' if decoded['kind'] == 'load' else 'scalar.rd')
            require(vmem % 8 == 0 and fields['line'] == ((vmem >> 3) & 0xffff), 'DMA VMEM address differs from scalar operand')
            require(fields['dram'] == (value(sample, 'scalar.dma_base') << 32 | dram), 'DMA DRAM address differs from scalar operands')
            require(fields['size'] == value(sample, 'scalar.rs2'), 'DMA transfer size differs from scalar operand')
            require(0 < fields['size'] <= 4096 and fields['size'] % 32 == 0,
                    'Unsupported DMA transfer size/alignment')
            require(fields['dram'] % 32 == 0 and fields['dram'] + fields['size'] <= 1 << 37,
                    'Unsupported DMA DRAM alignment/address range')
            beats = fields['size'] // 32
            require(fields['line'] + beats <= 6 * 8192, 'DMA transfer exceeds VMEM')
            for old in self.channels:
                if old:
                    require(not overlaps(fields['line'], beats, old['line'], old['beats']) or
                            fields['op'] == old['op'] == 1, 'DMA commands have conflicting live VMEM buffers')
                    require(not overlaps(fields['dram'], fields['size'], old['dram'], old['size']) or
                            fields['op'] == old['op'] == 0, 'DMA commands have conflicting live DRAM buffers')
            for kind, lsu in (lsu_owners or {}).items():
                if lsu and value(sample, f'lsu.{kind}.busy') and (kind == 'store' or fields['op'] == 0):
                    require(not overlaps(fields['line'], beats, lsu['base_line'], 32),
                            'DMA launched against an unfinished LSU buffer owner')
            owner = {**decoded, **fields, 'id': len(self.commands), 'slot': slot, 'beats': beats,
                     'issue_cycle': cycle, 'scalar_pc': value(sample, 'scalar.pc'),
                     'scalar_word': value(sample, 'scalar.instr'), 'requests': [], 'responses': [],
                     'vmem_accesses': [], 'completion_cycle': None, 'wait_cycle': None}
            self.commands.append(owner)
            self.slots[slot] = self.channels[channel] = self.latest[channel] = owner

        # Conservative full-buffer lifetime checks, independent of same-cycle
        # physical-bank arbitration. An ungranted DMA request is not an access.
        for kind in ('load', 'store', 'scalar_read', 'scalar_write'):
            port = 'lsu.vmem.' + kind
            if value(sample, port + '.valid'):
                line = value(sample, port + '.bank') * 8192 + value(sample, port + '.row')
                for owner in self.channels:
                    if owner and (owner['op'] == 0 or kind in ('store', 'scalar_write')):
                        require(not overlaps(line, 1, owner['line'], owner['beats']),
                                'LSU accesses a live conflicting DMA VMEM buffer')

        granted_banks = []
        for kind in ('read', 'write'):
            port = 'vmem.dma_' + kind
            if value(sample, port + '.valid'):
                bank = value(sample, port + '.bank')
                require(bank < 6, 'DMA request targets an unmapped VMEM bank')
                if value(sample, port + '.grant'):
                    granted_banks.append(bank)
            else:
                require(not value(sample, port + '.grant'), 'DMA grant without a request')
        require(len(set(granted_banks)) == len(granted_banks), 'DMA read/write granted on the same physical VMEM bank')

        # D-channel tag identity establishes destination/owner even when replies
        # are reordered. Process old replies before potential source-ID reuse.
        prior_tags = set(self.tags)
        dvalid, dready = value(sample, 'dma.tl_d.valid'), value(sample, 'dma.tl_d.ready')
        response = None
        if dvalid:
            tag = value(sample, 'dma.tl_d.source')
            require(tag in self.tags, 'DMA response has no outstanding source ID')
            response = self.tags[tag]
        require(value(sample, 'vmem.dma_write.valid') == bool(response and response[0]['op'] == 0),
                'DMA load response and VMEM write request differ')
        if response:
            owner, row = response
            if owner['op'] == 0:
                line = value(sample, 'vmem.dma_write.bank') * 8192 + value(sample, 'vmem.dma_write.row')
                require(line == owner['line'] + row, 'DMA load response writes the wrong VMEM line')
                granted = value(sample, 'vmem.dma_write.grant')
                require(dready == granted, 'DMA load response consumed without VMEM write grant')
                self.request_denied['write'] += int(not granted)
                if granted:
                    owner['vmem_accesses'].append(cycle)
            else:
                require(dready, 'DMA store acknowledgement unexpectedly blocked')
            if dready:
                self.tags.pop(tag)
                owner['responses'].append(cycle)

        # TL A requests are bound to accepted slot/beat and exact DRAM address.
        if value(sample, 'dma.tl_a.valid') and value(sample, 'dma.tl_a.ready'):
            slot = value(sample, 'dma.request_slot')
            owner = self.slots[slot]
            require(owner is not None and owner['issue_cycle'] < cycle, 'DMA TL request has no previously accepted slot')
            row = len(owner['requests'])
            require(row < owner['beats'] and value(sample, 'dma.request_beat') == row, 'DMA TL beat sequence mismatch')
            require(value(sample, 'dma.tl_a.address') == owner['dram'] + row * 32, 'DMA TL request address mismatch')
            require(value(sample, 'dma.tl_a.opcode') == (4 if owner['op'] == 0 else 0), 'DMA TL opcode mismatch')
            tag = value(sample, 'dma.tl_a.source')
            require(tag not in prior_tags, 'DMA reused a still-outstanding source ID')
            if owner['op'] == 1:
                require(bool(self.store_fifo), 'DMA store request precedes its VMEM response')
                observed_owner, observed_row, response_cycle = self.store_fifo.popleft()
                require(observed_owner is owner and observed_row == row and response_cycle < cycle,
                        'DMA store data FIFO owner/order mismatch')
            self.tags[tag] = (owner, row)
            owner['requests'].append(cycle)

        require(value(sample, 'dma.vmem_response') == (self.previous_read is not None),
                'DMA VMEM response does not match previous granted read')
        if self.previous_read:
            owner, row = self.previous_read
            self.store_fifo.append((owner, row, cycle))
        self.previous_read = None
        if value(sample, 'vmem.dma_read.valid'):
            slot = value(sample, 'dma.request_slot')
            owner = self.slots[slot]
            require(owner is not None and owner['op'] == 1 and owner['issue_cycle'] < cycle, 'DMA VMEM read lacks a previously accepted store owner')
            row = len(owner['vmem_accesses'])
            require(row < owner['beats'], 'DMA store repeats its VMEM read stream')
            line = value(sample, 'vmem.dma_read.bank') * 8192 + value(sample, 'vmem.dma_read.row')
            require(line == owner['line'] + row, 'DMA store reads wrong VMEM line/order')
            grant = value(sample, 'vmem.dma_read.grant')
            self.request_denied['read'] += int(not grant)
            if grant:
                owner['vmem_accesses'].append(cycle)
                self.previous_read = (owner, row)
        active = sum(owner is not None for owner in self.channels)
        self.maximum_busy_channels = max(self.maximum_busy_channels, active)
        self.busy_with_vpu_edges += int(active > 0 and any(value(sample, f'physical.{direction}{port}.valid')
                                                         for direction in ('read', 'write') for port in (0, 1)))
        self.busy_with_lsu_edges += int(active > 0 and any(value(sample, f'lsu.vmem.{kind}.valid')
                                                         for kind in ('load', 'store', 'scalar_read', 'scalar_write')))

    def finish(self):
        require(self.commands and not any(self.slots) and not self.tags and not self.store_fifo and not self.previous_read,
                'No DMA commands or truncated capture with unfinished DMA work')
        require(all(owner['wait_cycle'] is not None for owner in self.commands), 'DMA command lacks an explicit completed wait')
        commands = []
        for owner in self.commands:
            record = dict(owner)
            for key in ('requests', 'responses', 'vmem_accesses'):
                record[key] = series(owner[key], owner['issue_cycle'])
            record['completion_age'] = owner['completion_cycle'] - owner['issue_cycle']
            commands.append(record)
        return {'status': 'dma_observed_completion_and_buffers_passed', 'command_count': len(commands),
                'commands': commands, 'waits': self.waits, 'sample_count': self.sample_count,
                'wait_stall_edges': self.wait_stall_edges, 'maximum_busy_channels': self.maximum_busy_channels,
                'dma_busy_with_vpu_row_edges': self.busy_with_vpu_edges,
                'dma_busy_with_lsu_vmem_edges': self.busy_with_lsu_edges,
                'dma_requests_denied_edges': self.request_denied,
                'final_dma_completion_cycle': max(command['completion_cycle'] for command in commands),
                'limitations': ['Finite execution evidence with the captured memory environment, not a universal DMA bound.',
                                'Scalar register-port values bind DMA command operands; this monitor does not independently execute the scalar register file.',
                                'Full DMA buffers remain reserved until channel completion; this intentionally does not validate finer streaming buffer reuse.',
                                'Source tags, beat counts, addresses, saved commands and grants are checked; payloads rely on full output goldens.',
                                'Only captured DMA/LSU/VPU accesses are covered; host/other-master interference is not independently reconstructed.',
                                'Cached simulator source-to-binary build linkage remains unverified.']}


def check_samples(samples):
    dma, lsu, vpu = Events(), LsuEvents(), VpuEvents()
    for cycle, timestamp, sample in samples:
        try:
            dma.step(cycle, sample, lsu.owners)
            lsu.step(cycle, sample)
            vpu.step(cycle, sample)
        except ValueError as error:
            raise ValueError(f'cycle {cycle}: {error}') from error
    return {**dma.finish(), 'lsu_vpu': {**lsu.finish(), 'vpu': vpu.finish()}}


def summarize(manifest_path):
    manifest_path = checked_path(manifest_path)
    manifest = json.loads(manifest_path.read_text())
    require(manifest.get('capture_kind') == 'dma_lsu_vpu_rows', 'Requires explicit DMA/LSU/VPU capture')
    actual = {key: (record['path'], record['width']) for key, record in manifest['signals'].items()}
    require(actual == SIGNALS, 'DMA capture signal contract differs')
    completion = completion_summary(manifest_path)
    trace = verify_artifact(manifest['trace'])
    with trace.open() as stream:
        selected, timescale = read_header(stream, SIGNALS)
        report = check_samples(edge_samples(stream, selected, SIGNALS))
    verify_artifact(manifest['trace'])
    require(report['final_dma_completion_cycle'] <= completion['last_dma_wait']['cycle'] <= completion['dbg0_write']['cycle'],
            'Final output marker precedes actual DMA completion')
    report.update(schema='atlas.rtlgraph.dma-observation.v1', timescale=timescale,
                  sampling='settled_pre_rising_edge', trace=manifest['trace'],
                  replay_manifest=artifact(manifest_path), completion=completion,
                  functional_validation='full_tensor_golden', driver=artifact(checked_path(__file__)),
                  helpers={name: artifact(checked_path(Path(__file__).parent / name)) for name in
                           ('rtlgraph_dma_vcd.py', 'rtlgraph_lsu_vcd.py', 'rtlgraph_lsu_trace.py',
                            'rtlgraph_vpu_vcd.py', 'rtlgraph_vpu_trace.py', 'rtlgraph_vpu.py',
                            'rtlgraph_mxu1_vcd.py', 'rtlgraph_mxu1_trace.py', 'rtlgraph_completion.py',
                            'rtlgraph_perf.py')})
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    output = checked_path(args.output)
    require(not output.exists(), 'Output must be a new file')
    report = summarize(args.manifest)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps({'status': report['status'], 'commands': report['command_count'],
                      'wait_stall_edges': report['wait_stall_edges'], 'output': str(output)}))


if __name__ == '__main__':
    main()

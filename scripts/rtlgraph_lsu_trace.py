#!/usr/bin/env python3
"""Measure LSU row timing and check observed LSU/VPU overlap independently.

Transaction ownership comes from scalar instructions and ordered row addresses,
not the scheduler's assumed timing table. Payload correctness remains the full
golden comparison in the replay. This is finite execution evidence, not a proof.
"""

import argparse
import json
from pathlib import Path

from rtlgraph_completion import summarize as completion_summary
from rtlgraph_lsu_vcd import SIGNALS
from rtlgraph_mxu1_capture import verify_artifact
from rtlgraph_mxu1_trace import series
from rtlgraph_mxu1_vcd import edge_samples, read_header
from rtlgraph_s0 import artifact, checked_path
from rtlgraph_vpu import DOUBLE
from rtlgraph_vpu_trace import Events as VpuEvents, decode_vpu, require, shape


def value(sample, key):
    result = sample.get(key)
    require(type(result) is int and 0 <= result < 1 << SIGNALS[key][1],
            'Unknown, missing, or out-of-range ' + key)
    return result


def decode_lsu(word):
    if word & 0x7f != 0x07:
        return None
    kind = word >> 13 & 3
    require(kind in (0, 1), 'Unsupported scalar vector-memory encoding')
    return {'op': kind + 1, 'kind': 'load' if kind == 0 else 'store', 'mreg': word >> 7 & 63}


class Events:
    def __init__(self):
        self.commands = []
        self.owners = {'load': None, 'store': None}
        self.previous = {'load': None, 'store': None}
        self.last_cycle = None
        self.sample_count = self.overlap_edges = self.dma_wait_edges = 0
        self.coincident_mreg_edges = 0

    def step(self, cycle, sample):
        require(type(cycle) is int and (self.last_cycle is None or cycle == self.last_cycle + 1),
                'Missing or reordered sample cycle')
        self.last_cycle, self.sample_count = cycle, self.sample_count + 1
        if sample.get('reset') != 0:
            require(not self.commands, 'Reset or unknown reset after LSU execution began')
            return
        for kind, response in (('load', 'lsu.vmem.response'), ('store', 'lsu.mreg.response')):
            previous = self.previous[kind]
            require(value(sample, response) == (previous is not None),
                    kind + ' response does not match prior observed request')
            if previous:
                previous['responses'].append(cycle)
            owner = self.owners[kind]
            busy = value(sample, f'lsu.{kind}.busy')
            active = 'write' if kind == 'load' else 'read'
            require(value(sample, f'lsu.active.{active}.valid') == busy, 'LSU active reservation differs from busy')
            if owner and not busy:
                require(all(len(owner[key]) == 32 for key in ('reads', 'responses', 'writes')),
                        'LSU released before its complete row stream')
                owner['first_not_busy_cycle'] = cycle
                self.owners[kind] = None
            elif owner:
                require(value(sample, f'lsu.active.{active}.mreg') == owner['mreg'], 'LSU reservation register mismatch')
            else:
                require(not busy, 'Busy LSU has no accepted command')

        fired = value(sample, 'scalar.fire')
        word = value(sample, 'scalar.instr') if fired else 0
        decoded = decode_lsu(word) if fired else None
        require(value(sample, 'lsu.cmd.valid') == (decoded is not None), 'Scalar issue and LSU command differ')
        read_busy = value(sample, 'scalar.mreg_read_busy')
        write_busy = value(sample, 'scalar.mreg_write_busy')
        if decoded:
            kind = decoded['kind']
            require(self.owners[kind] is None, 'LSU command replaces an unfinished transfer')
            require(value(sample, 'lsu.cmd.op') == decoded['op']
                    and value(sample, 'lsu.cmd.mreg') == decoded['mreg'], 'Scalar/LSU opcode or register mismatch')
            require(not (write_busy >> decoded['mreg'] & 1), 'LSU command conflicts with logical MREG writer')
            base = value(sample, 'lsu.cmd.line')
            require(base % 32 == 0 and base // 8192 == (base + 31) // 8192, 'Unsupported vector transfer alignment/range')
            owner = {**decoded, 'id': len(self.commands), 'base_line': base,
                     'issue_cycle': cycle, 'scalar_pc': value(sample, 'scalar.pc'), 'scalar_word': word,
                     'reads': [], 'responses': [], 'writes': [], 'first_not_busy_cycle': None}
            self.commands.append(owner)
            self.owners[kind] = owner
        vpu = decode_vpu(word) if fired else None
        if vpu:
            slots = (1, 2) if vpu['op'] in DOUBLE else (1,)
            reads = {reg for slot in slots for reg, row in shape(vpu, slot)}
            writes = {reg for slot in slots for reg, row in shape(vpu, slot, write=True)}
            require(all(not (write_busy >> reg & 1) for reg in reads), 'VPU issue overlaps a logical MREG writer')
            require(all(not ((read_busy | write_busy) >> reg & 1) for reg in writes),
                    'VPU issue overlaps a logical MREG reader/writer')

        for kind, request in (('load', 'lsu.vmem.load'), ('store', 'lsu.mreg.read')):
            owner = self.owners[kind]
            active = value(sample, request + '.valid')
            self.previous[kind] = owner if active else None
            if active:
                require(owner is not None and len(owner['reads']) < 32, 'LSU request has no remaining owner row')
                row = len(owner['reads'])
                if kind == 'load':
                    actual = value(sample, request + '.bank') * 8192 + value(sample, request + '.row')
                    require(actual == owner['base_line'] + row, 'VLOAD memory row order/address mismatch')
                else:
                    require((value(sample, request + '.mreg'), value(sample, request + '.row')) == (owner['mreg'], row),
                            'VSTORE source row order/register mismatch')
                owner['reads'].append(cycle)
        for kind, request in (('load', 'lsu.mreg.write'), ('store', 'lsu.vmem.store')):
            if value(sample, request + '.valid'):
                owner = self.owners[kind]
                require(owner is not None and len(owner['writes']) < 32, 'LSU write has no remaining owner row')
                row = len(owner['writes'])
                require(len(owner['responses']) > row and owner['responses'][row] < cycle,
                        'LSU write precedes its registered source response')
                if kind == 'load':
                    require((value(sample, request + '.mreg'), value(sample, request + '.row')) == (owner['mreg'], row),
                            'VLOAD destination row order/register mismatch')
                else:
                    actual = value(sample, request + '.bank') * 8192 + value(sample, request + '.row')
                    require(actual == owner['base_line'] + row, 'VSTORE memory row order/address mismatch')
                owner['writes'].append(cycle)

        ports = {}
        for direction in ('read', 'write'):
            active = []
            for port in (f'lsu.mreg.{direction}', f'physical.{direction}0', f'physical.{direction}1'):
                if value(sample, port + '.valid'):
                    active.append((value(sample, port + '.mreg') % 32,
                                   (value(sample, port + '.mreg') // 32) * 32 + value(sample, port + '.row'), port))
            require(len({bank for bank, row, port in active}) == len(active), 'LSU/VPU physical-bank ' + direction + ' conflict')
            ports[direction] = active
        require(not ({(b, r) for b, r, p in ports['read']} & {(b, r) for b, r, p in ports['write']}),
                'Same-cycle same-row MREG read/write requires an unsupported visibility contract')
        vpu_row = any(p.startswith('physical.') for direction in ports.values() for b, r, p in direction)
        lsu_row = any(p.startswith('lsu.') for direction in ports.values() for b, r, p in direction)
        if any(self.owners.values()) and vpu_row:
            self.overlap_edges += 1
        self.coincident_mreg_edges += int(lsu_row and vpu_row)
        memory = []
        for kind in ('load', 'store', 'scalar_read', 'scalar_write'):
            port = 'lsu.vmem.' + kind
            if value(sample, port + '.valid'):
                memory.append(value(sample, port + '.bank'))
        require(len(set(memory)) == len(memory), 'Deterministic LSU ports conflict on a VMEM bank')
        for kind in ('read', 'write'):
            port = 'vmem.dma_' + kind
            if value(sample, port + '.valid'):
                granted = value(sample, port + '.grant')
                self.dma_wait_edges += int(not granted)
                require(not granted or value(sample, port + '.bank') not in memory,
                        'Granted DMA request conflicts with a deterministic LSU bank access')

    def finish(self):
        require(self.commands and not any(self.owners.values()) and not any(self.previous.values()),
                'Missing LSU commands or truncated outstanding LSU work')
        commands = []
        for command in self.commands:
            result = dict(command)
            for key in ('reads', 'responses', 'writes'):
                result[key] = series(command[key], command['issue_cycle'])
            result['first_not_busy_age'] = command['first_not_busy_cycle'] - command['issue_cycle']
            commands.append(result)
        return {'status': 'lsu_vpu_observed_overlap_passed', 'command_count': len(commands),
                'commands': commands, 'sample_count': self.sample_count,
                'lsu_active_with_vpu_row_edges': self.overlap_edges,
                'lsu_and_vpu_mreg_access_edges': self.coincident_mreg_edges,
                'dma_request_not_granted_edges': self.dma_wait_edges,
                'limitations': ['Finite replay evidence; no universal safety or fixed-latency proof.',
                                'Scalar binding checks opcode and MREG ID; effective VMEM base is measured at command acceptance, not independently recomputed from scalar register values.',
                                'One-cycle memory response interfaces and register-before-write are independently checked; issue-relative read/write/release ages are measured.',
                                'Physical collision checks cover LSU/VPU ports, plus deterministic LSU VMEM accesses and granted DMA against those accesses; other engines and TileLink traffic are outside this monitor.',
                                'Payload arithmetic/data routing correctness relies on the full-tensor golden check, not row payload tracing.',
                                'Cached simulator source-to-binary build linkage remains unverified.']}


def check_samples(samples):
    lsu, vpu = Events(), VpuEvents()
    for cycle, timestamp, sample in samples:
        try:
            lsu.step(cycle, sample)
            vpu.step(cycle, sample)
        except ValueError as error:
            raise ValueError(f'cycle {cycle}: {error}') from error
    return {**lsu.finish(), 'vpu': vpu.finish()}


def summarize(manifest_path):
    manifest_path = checked_path(manifest_path)
    manifest = json.loads(manifest_path.read_text())
    require(manifest.get('capture_kind') == 'lsu_vpu_rows', 'Requires explicit LSU/VPU capture')
    actual = {key: (record['path'], record['width']) for key, record in manifest['signals'].items()}
    require(actual == SIGNALS, 'LSU capture signal contract differs')
    completion = completion_summary(manifest_path)
    trace = verify_artifact(manifest['trace'])
    with trace.open() as stream:
        selected, timescale = read_header(stream, SIGNALS)
        report = check_samples(edge_samples(stream, selected, SIGNALS))
    verify_artifact(manifest['trace'])
    report.update(schema='atlas.rtlgraph.lsu-vpu-observation.v1', timescale=timescale,
                  sampling='settled_pre_rising_edge', trace=manifest['trace'],
                  replay_manifest=artifact(manifest_path), completion=completion,
                  functional_validation='full_tensor_golden', driver=artifact(checked_path(__file__)),
                  helpers={name: artifact(checked_path(Path(__file__).parent / name)) for name in
                           ('rtlgraph_lsu_vcd.py', 'rtlgraph_vpu_vcd.py', 'rtlgraph_vpu_trace.py',
                            'rtlgraph_vpu.py', 'rtlgraph_mxu1_vcd.py', 'rtlgraph_mxu1_trace.py',
                            'rtlgraph_completion.py', 'rtlgraph_perf.py')})
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
                      'overlap_edges': report['lsu_active_with_vpu_row_edges'], 'output': str(output)}))


if __name__ == '__main__':
    main()

#!/usr/bin/env python3
"""Check a shared DMA/LSU/VPU/MXU execution using observed event ownership.

Reuses the existing DMA, LSU and VPU monitors unchanged. Both MXUs use independent
compute/pop queues; push commands are checked at acceptance only. Physical MREG
conflicts are checked across all four engines without asserting a timing LUT.
"""

import argparse
from collections import Counter
import json
from pathlib import Path

from rtlgraph_completion import summarize as completion_summary
from rtlgraph_dma_trace import Events as DmaEvents
from rtlgraph_lsu_trace import Events as LsuEvents
from rtlgraph_mixed_vcd import SIGNALS, engine_sample
from rtlgraph_mxu0_trace import Events as MxuEvents, decode_mxu0
from rtlgraph_mxu1_capture import verify_artifact
from rtlgraph_mxu1_trace import decode_mxu1
from rtlgraph_mxu1_vcd import edge_samples, read_header
from rtlgraph_s0 import artifact, checked_path
from rtlgraph_vpu_trace import Events as VpuEvents, require


def value(sample, key):
    result = sample.get(key)
    require(type(result) is int and 0 <= result < 1 << SIGNALS[key][1],
            'Unknown, missing, or out-of-range ' + key)
    return result


def physical_accesses(sample):
    """Return actual requests once per physical client port, including push rows."""
    accesses = {}
    for direction in ('read', 'write'):
        active = []
        ports = [('lsu', f'lsu.mreg.{direction}')]
        ports += [('vpu', f'physical.{direction}{port}') for port in (0, 1)]
        ports += [(f'mxu{engine}', f'mxu{engine}.port.{direction}{port}')
                  for engine in (0, 1) for port in (0, 1)]
        for engine, port in ports:
            if value(sample, port + '.valid'):
                register, row = value(sample, port + '.mreg'), value(sample, port + '.row')
                active.append(dict(engine=engine, port=port, bank=register % 32,
                                   row=(register // 32) * 32 + row))
        require(len({a['bank'] for a in active}) == len(active),
                'Mixed physical MREG ' + direction + ' bank conflict: ' + ', '.join(a['port'] for a in active))
        accesses[direction] = active
    require(not ({(a['bank'], a['row']) for a in accesses['read']} &
                 {(a['bank'], a['row']) for a in accesses['write']}),
            'Mixed same-cycle same-row MREG read/write requires an unsupported visibility contract')
    return accesses


class Events:
    def __init__(self):
        self.dma, self.lsu, self.vpu = DmaEvents(), LsuEvents(), VpuEvents()
        self.mxus = {engine: MxuEvents(engine) for engine in (0, 1)}
        self.scalar_counts = {engine: Counter() for engine in (0, 1)}
        self.dma_mxu_rows = {engine: 0 for engine in (0, 1)}
        self.dma_mxu_compute = {engine: 0 for engine in (0, 1)}
        self.coincident = Counter()
        self.physical_edges = 0

    def step(self, cycle, sample):
        # Every submonitor observes this same edge and reset epoch. A private
        # unknown/asserted engine reset must not suppress its scalar decoding.
        for field in ('clock', 'reset'):
            common = value(sample, field)
            for engine in (0, 1):
                require(value(sample, f'mxu{engine}.' + field) == common,
                        f'MXU{engine} {field} differs from the common capture domain')
        self.dma.step(cycle, sample, self.lsu.owners)
        self.lsu.step(cycle, sample)
        self.vpu.step(cycle, sample)
        for engine, events in self.mxus.items():
            observed = engine_sample(sample, engine)
            events.step(cycle, observed)
            if observed.get('reset') != 0:
                continue
            if value(sample, 'scalar.fire'):
                decoded = (decode_mxu0 if engine == 0 else decode_mxu1)(value(sample, 'scalar.instr'))
                if decoded is not None:
                    self.scalar_counts[engine][decoded['op']] += 1
            # Read request port zero is shared with pushes in the legacy MXU
            # monitor. An entirely idle engine must not hide unowned requests.
            if not events.commands:
                require(not any(value(sample, f'mxu{engine}.port.{direction}{port}.valid')
                                for direction in ('read', 'write') for port in (0, 1)),
                        f'MXU{engine} physical activity has no accepted command')
        if sample.get('reset') != 0:
            return
        accesses = physical_accesses(sample)
        active = {a['engine'] for direction in accesses.values() for a in direction}
        self.physical_edges += int(bool(active))
        engines = sorted(active)
        for index, engine in enumerate(engines):
            for other in engines[index + 1:]:
                self.coincident[engine + '+' + other] += 1
        busy = any(owner is not None for owner in self.dma.channels)
        for engine, events in self.mxus.items():
            self.dma_mxu_rows[engine] += int(busy and f'mxu{engine}' in active)
            # Include the final observed result edge even though step() just
            # retired that owner from its independent output queue.
            computing = bool(events.writing) or value(sample, f'mxu{engine}.acc_write.valid')
            self.dma_mxu_compute[engine] += int(busy and computing)

    def finish(self):
        report = self.dma.finish()
        if self.lsu.commands:
            lsu = self.lsu.finish()
        else:
            require(not any(self.lsu.owners.values()) and not any(self.lsu.previous.values()),
                    'Unowned LSU work at end of mixed capture')
            lsu = dict(status='lsu_no_issued_commands', command_count=0, commands=[], sample_count=self.lsu.sample_count)
        if self.vpu.commands:
            vpu = self.vpu.finish()
        else:
            require(not any(self.vpu.owners.values()) and not any(self.vpu.previous_reads.values()) and
                    not any(self.vpu.previous_physical.values()), 'Unowned VPU work at end of mixed capture')
            vpu = dict(status='vpu_no_issued_commands', command_count=0, commands=[], sample_count=self.vpu.sample_count)
        mxus = {}
        for engine, events in self.mxus.items():
            observed = Counter(command['op'] for command in events.commands)
            require(observed == self.scalar_counts[engine], f'MXU{engine} scalar command counts disagree')
            mxus[f'mxu{engine}'] = (events.finish(require_compute_pop=False) if events.commands else
                dict(status=f'mxu{engine}_no_issued_commands', command_count=0, commands=[],
                     computes=[], pops=[], sample_count=events.sample_count))
            mxus[f'mxu{engine}']['scalar_decoded_command_counts'] = dict(self.scalar_counts[engine])
        report.update(status='mixed_dma_lsu_vpu_mxu_observed_ownership_passed',
                      clock_reset_domain='known and equal across both MXUs and the common capture at every sampled edge',
                      lsu_vpu={**lsu, 'vpu': vpu}, mxu=mxus,
                      dma_busy_with_mxu_row_edges={f'mxu{k}': v for k, v in self.dma_mxu_rows.items()},
                      dma_busy_with_mxu_compute_edges={f'mxu{k}': v for k, v in self.dma_mxu_compute.items()},
                      physical_mreg=dict(status='all_captured_client_ports_checked',
                                         active_edges=self.physical_edges,
                                         coincident_client_row_edges=dict(self.coincident),
                                         clients=['lsu', 'vpu', 'mxu0', 'mxu1']))
        report['limitations'] += [
            'MXU compute/pop ownership is checked with independent ordered row queues; write ages are measured, not predicted.',
            'MXU weight/accumulator pushes are checked at scalar acceptance only; their row ownership, payloads and response routing are not reconstructed.',
            'Physical MREG collision checks include observed LSU, VPU and both MXU request ports, including push requests. Different rows may read/write one bank; same-row visibility is not assumed.',
            'DMA/MXU overlap counts describe this finite execution; they establish neither a universal latency nor future performance.',
            'Full output goldens validate numerical results; no row-payload arithmetic proof is claimed.']
        return report


def check_samples(samples):
    events = Events()
    for cycle, _timestamp, sample in samples:
        try:
            events.step(cycle, sample)
        except ValueError as error:
            raise ValueError(f'cycle {cycle}: {error}') from error
    return events.finish()


def summarize(manifest_path):
    manifest_path = checked_path(manifest_path)
    manifest = json.loads(manifest_path.read_text())
    require(manifest.get('capture_kind') == 'dma_lsu_vpu_mxu_rows', 'Requires explicit mixed-engine capture')
    actual = {key: (record['path'], record['width']) for key, record in manifest['signals'].items()}
    require(actual == SIGNALS, 'Mixed capture signal contract differs')
    completion = completion_summary(manifest_path)
    trace = verify_artifact(manifest['trace'])
    with trace.open() as stream:
        selected, timescale = read_header(stream, SIGNALS)
        report = check_samples(edge_samples(stream, selected, SIGNALS))
    verify_artifact(manifest['trace'])
    require(report['final_dma_completion_cycle'] <= completion['last_dma_wait']['cycle'] <= completion['dbg0_write']['cycle'],
            'Final output marker precedes actual DMA completion')
    report.update(schema='atlas.rtlgraph.mixed-observation.v1', timescale=timescale,
                  sampling='settled_pre_rising_edge', trace=manifest['trace'],
                  replay_manifest=artifact(manifest_path), completion=completion,
                  functional_validation='full_tensor_golden', driver=artifact(checked_path(__file__)),
                  helpers={name: artifact(checked_path(Path(__file__).parent / name)) for name in
                           ('rtlgraph_mixed_vcd.py', 'rtlgraph_dma_trace.py', 'rtlgraph_dma_vcd.py',
                            'rtlgraph_lsu_vcd.py', 'rtlgraph_lsu_trace.py', 'rtlgraph_vpu_vcd.py',
                            'rtlgraph_vpu_trace.py', 'rtlgraph_vpu.py', 'rtlgraph_mxu0_trace.py',
                            'rtlgraph_mxu0_vcd.py', 'rtlgraph_mxu1_vcd.py', 'rtlgraph_mxu1_trace.py',
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
    print(json.dumps(dict(status=report['status'], commands=report['command_count'],
                          mxu_commands={key: value['command_count'] for key, value in report['mxu'].items()},
                          output=str(output))))


if __name__ == '__main__':
    main()

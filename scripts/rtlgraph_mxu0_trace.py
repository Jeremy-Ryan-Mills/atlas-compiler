#!/usr/bin/env python3
"""Observe MXU0 compute/pop row ownership and overlap in a successful replay.

Uses independent command/row queues, not proposed LUT ages. Systolic write ages
are measured. The common completion checker separately binds every fired scalar
word/PC to assembled source and checks the golden result and completion markers.
This narrow monitor does not validate push data, MREG response routing, or all
shared-port conflicts, and supplies no universal timing proof.
"""

import argparse
from collections import deque
import json
from pathlib import Path

from rtlgraph_completion import summarize as completion_summary
from rtlgraph_mxu0_vcd import SIGNALS
from rtlgraph_mxu1_capture import verify_artifact
from rtlgraph_mxu1_trace import decode_mxu1, series
from rtlgraph_mxu1_vcd import OPS, edge_samples, read_header
from rtlgraph_s0 import artifact, checked_path


def require(condition, message):
    if not condition:
        raise ValueError(message)


def value(sample, key):
    result = sample.get(key)
    require(type(result) is int and 0 <= result < 1 << SIGNALS[key][1], 'Unknown, missing, or out-of-range ' + key)
    return result


def decode_mxu0(word):
    require(type(word) is int and 0 <= word < 1 << 32, 'Invalid scalar instruction')
    if word & 0x7f != 0x77 or (word >> 25) & 1:
        return None
    # Instructions.scala uses identical fields, with bit25 selecting the MXU.
    # Reuse field decoding only; no MXU1 temporal contract is reused here.
    return decode_mxu1(word | (1 << 25))


class Events:
    def __init__(self, engine=0):
        require(engine in (0, 1), 'Unsupported MXU engine')
        self.engine = engine
        self.commands, self.computes, self.pops = [], [], []
        self.reading, self.writing, self.storing = deque(), deque(), deque()
        self.previous_read = self.previous_store = None
        self.overlaps = {}
        self.maximum_inflight = 0
        self.last_cycle = None
        self.sample_count = 0

    def step(self, cycle, sample):
        require(type(cycle) is int and (self.last_cycle is None or cycle == self.last_cycle + 1), 'Missing or reordered sample cycle')
        self.last_cycle, self.sample_count = cycle, self.sample_count + 1
        reset = sample.get('reset')
        if reset != 0:
            require(not self.commands, f'Reset or unknown reset after MXU{self.engine} execution began')
            return
        fire = value(sample, 'scalar.fire')
        decoder = decode_mxu1 if self.engine else decode_mxu0
        decoded = decoder(value(sample, 'scalar.instr')) if fire else None
        command_valid = value(sample, 'cmd.valid')
        require(command_valid == (decoded is not None), f'Scalar MXU{self.engine} issue and sequencer command are unmatched')
        accepts = {key: value(sample, 'accept.' + key) for key in
                   ('compute', 'push_p0', 'push_p1', 'push_bf16', 'pop_fp8', 'pop_bf16')}
        require(sum(accepts.values()) == command_valid, f'MXU{self.engine} command was rejected or accepted more than once')
        if decoded is not None:
            observed = {'op': OPS.get(value(sample, 'cmd.op')),
                        **{name: value(sample, 'cmd.' + name) for name in ('mreg', 'accsel', 'wslot')}}
            require(decoded == observed, f'Scalar MXU{self.engine} operands disagree with sequencer command')
            op = decoded['op']
            accepted = (accepts['compute'] if op in ('Matmul', 'MatmulAcc') else
                        accepts['push_p0'] + accepts['push_p1'] if op in ('PushWeight', 'PushAccFP8') else
                        accepts['push_bf16'] if op == 'PushAccBF16' else
                        accepts['pop_fp8'] if op == 'PopAccFP8' else accepts['pop_bf16'])
            require(accepted == 1, f'Wrong MXU{self.engine} acceptance route')
            command = {'id': len(self.commands), 'accepted_cycle': cycle,
                       'scalar_pc': value(sample, 'scalar.pc'), 'scalar_word': value(sample, 'scalar.instr'), **decoded}
            self.commands.append(command)
            if op in ('Matmul', 'MatmulAcc'):
                transaction = {**command, 'requests': [], 'feeds': [], 'reads': [], 'writes': [], 'retire_cycle': None}
                self.computes.append(transaction)
                self.reading.append(transaction)
                self.writing.append(transaction)
                self.maximum_inflight = max(self.maximum_inflight, len(self.writing))
            elif op in ('PopAccFP8', 'PopAccBF16'):
                transaction = {**command, 'reads': [], 'writes': []}
                self.pops.append(transaction)
                self.storing.append(transaction)

        # A current accept must never replace ownership of the previous request.
        feed = value(sample, 'compute.valid')
        require(feed == (self.previous_read is not None), 'Compute feed does not match the prior observed compute request')
        if self.previous_read is not None:
            transaction, row = self.previous_read
            require(len(transaction['feeds']) == row, 'Compute feed row order mismatch')
            transaction['feeds'].append(cycle)
        for port in (0, 1):
            active = value(sample, f'mreg_write{port}.valid')
            expected = self.previous_store is not None and (port == 0 or self.previous_store[0]['op'] == 'PopAccBF16')
            require(active == expected, 'Pop MREG write does not match the prior observed accumulator read')
            if active:
                transaction, row = self.previous_store
                require(value(sample, f'mreg_write{port}.mreg') == transaction['mreg'] + port
                        and value(sample, f'mreg_write{port}.row') == row, 'Pop destination/row mismatch')
                if port == 0:
                    require(len(transaction['writes']) == row, 'Pop write row order mismatch')
                    transaction['writes'].append(cycle)

        current_read = None
        request = value(sample, 'mreg_read.valid')
        if self.reading and request:
            transaction = self.reading[0]
            row = len(transaction['requests'])
            require(row < 32 and value(sample, 'mreg_read.mreg') == transaction['mreg']
                    and value(sample, 'mreg_read.row') == row, 'Compute MREG request destination/row mismatch')
            transaction['requests'].append(cycle)
            current_read = transaction, row
            if row == 31:
                self.reading.popleft()
        read = value(sample, 'acc_read.valid')
        require(read == (current_read is not None and current_read[0]['op'] == 'MatmulAcc'),
                'Accumulator read ownership disagrees with the independent compute request')
        if read:
            transaction, row = current_read
            require(value(sample, 'acc_read.accsel') == transaction['accsel']
                    and value(sample, 'acc_read.row') == row, 'Accumulator compute read destination/row mismatch')
            transaction['reads'].append(cycle)

        current_store = None
        store = value(sample, 'acc_store.valid')
        if store:
            require(bool(self.storing), 'Accumulator store read has no accepted pop owner')
            transaction = self.storing[0]
            row = len(transaction['reads'])
            require(row < 32 and value(sample, 'acc_store.accsel') == transaction['accsel']
                    and value(sample, 'acc_store.row') == row, 'Accumulator pop read destination/row mismatch')
            transaction['reads'].append(cycle)
            current_store = transaction, row
            if row == 31:
                self.storing.popleft()
            if read:
                require(value(sample, 'acc_read.accsel') != transaction['accsel'], 'Compute/pop read collision on the same physical accumulator')
            if current_read is not None and current_read[0]['op'] == 'Matmul' and current_read[0]['accsel'] == transaction['accsel']:
                compute = current_read[0]
                key = transaction['id'], compute['id']
                record = self.overlaps.setdefault(key, {'pop_id': transaction['id'], 'compute_id': compute['id'],
                    'accsel': transaction['accsel'], 'pop_to_compute_issue_gap': compute['accepted_cycle'] - transaction['accepted_cycle'],
                    'cycles': [], 'pop_rows': [], 'compute_operand_rows': []})
                record['cycles'].append(cycle)
                record['pop_rows'].append(row)
                record['compute_operand_rows'].append(current_read[1])

        written, core, retired = (value(sample, key) for key in ('acc_write.valid', 'core_out.valid', 'retire'))
        require(core == written, 'Core result dropped before accumulator write')
        expected_retire = False
        if written:
            require(bool(self.writing), 'Accumulator result has no accepted compute owner')
            transaction = self.writing[0]
            row = len(transaction['writes'])
            require(row < 32 and len(transaction['feeds']) > row, 'Result row precedes its operand feed')
            require(value(sample, 'acc_write.accsel') == transaction['accsel']
                    and value(sample, 'acc_write.row') == row, 'Accumulator result destination/row mismatch')
            transaction['writes'].append(cycle)
            expected_retire = row == 31
            if expected_retire:
                transaction['retire_cycle'] = cycle
                self.writing.popleft()
        require(retired == expected_retire, 'Compute retirement is not its final independent output row')
        self.previous_read, self.previous_store = current_read, current_store

    def finish(self, *, require_compute_pop=True):
        if require_compute_pop:
            require(bool(self.computes) and bool(self.pops), 'Need both compute and pop observations')
        else:
            require(bool(self.commands), 'Need accepted MXU commands')
        require(not self.reading and not self.writing and not self.storing
                and self.previous_read is None and self.previous_store is None,
                f'Truncated capture with pending MXU{self.engine} work')
        for transaction in self.computes:
            require(all(len(transaction[key]) == 32 for key in ('requests', 'feeds', 'writes')), 'Incomplete compute row stream')
            require(len(transaction['reads']) == (32 if transaction['op'] == 'MatmulAcc' else 0), 'Incomplete or unexpected accumulator reads')
        for transaction in self.pops:
            require(len(transaction['reads']) == len(transaction['writes']) == 32, 'Incomplete pop row stream')

        def measured(transaction, fields):
            return {**transaction, **{key: series(transaction[key], transaction['accepted_cycle']) for key in fields}}
        return {'status': f'mxu{self.engine}_observed_row_ownership_passed', 'sample_count': self.sample_count,
                'command_count': len(self.commands), 'commands': self.commands,
                'computes': [measured(t, ('requests', 'feeds', 'reads', 'writes')) for t in self.computes],
                'pops': [measured(t, ('reads', 'writes')) for t in self.pops],
                'maximum_inflight_computes': self.maximum_inflight, 'pop_overwrite_overlaps': list(self.overlaps.values()),
                'scope': 'Accepted commands agree with issued scalar words; independent queues check32 compute/pop rows and read ownership. Output ages are observed, not assumed.',
                'limitations': ['No universal timing or schedule-safety proof; no fixed SA first-write or reuse age is asserted.',
                                'Weight/accumulator push commands are bound at acceptance, but their data/row streams are not checked.',
                                'MREG response bank routing and data, accumulator visibility/arithmetic, and other-engine conflicts are not checked.',
                                'Prior observed request-to-feed and accumulator-read-to-pop-write alignment assume the pinned one-cycle memory interface.',
                                'Overlap means simultaneous same-accumulator pop reads and overwrite operand requests with no compute accumulator read.']}


def check_samples(samples):
    events = Events()
    for cycle, _time, sample in samples:
        try:
            events.step(cycle, sample)
        except ValueError as error:
            raise ValueError(f'cycle {cycle}: {error}') from error
    return events.finish()


def summarize(manifest_path):
    manifest_path = checked_path(manifest_path)
    manifest = json.loads(manifest_path.read_text())
    require(manifest.get('capture_kind') == 'mxu0_overlap', 'Requires an explicit MXU0 capture')
    actual = {key: (record['path'], record['width']) for key, record in manifest['signals'].items()}
    require(actual == SIGNALS, 'MXU0 capture signal contract differs')
    completion = completion_summary(manifest_path)
    trace = verify_artifact(manifest['trace'])
    with trace.open() as stream:
        selected, timescale = read_header(stream, SIGNALS)
        report = check_samples(edge_samples(stream, selected, SIGNALS))
    verify_artifact(manifest['trace'])
    report.update(schema='atlas.rtlgraph.mxu0-observation.v1', sampling='settled_pre_rising_edge',
                  timescale=timescale, trace=manifest['trace'], replay_manifest=artifact(manifest_path),
                  driver=artifact(checked_path(__file__)), completion=completion,
                  functional_result=completion['functional_result'],
                  helpers={name: artifact(checked_path(Path(__file__).parent / name)) for name in
                           ('rtlgraph_mxu0_vcd.py', 'rtlgraph_mxu1_vcd.py', 'rtlgraph_mxu1_trace.py', 'rtlgraph_completion.py')})
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--require-overlap', action='store_true')
    args = parser.parse_args()
    output = checked_path(args.output)
    require(not output.exists(), 'Output must be a new file')
    report = summarize(args.manifest)
    require(not args.require_overlap or report['pop_overwrite_overlaps'], 'No observed pop/overwrite overlap')
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps({'status': report['status'], 'computes': len(report['computes']), 'pops': len(report['pops']),
                      'overlaps': len(report['pop_overwrite_overlaps']), 'output': str(output)}))


if __name__ == '__main__':
    try:
        main()
    except (OSError, KeyError, ValueError) as error:
        raise SystemExit('MXU0 trace check failed: ' + str(error))

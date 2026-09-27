#!/usr/bin/env python3
"""Attribute observed VPU row events to scalar commands, then measure ages.

No proposed timing table determines event ownership or expected write ages.
The monitor checks source-correlated row shapes and the pinned one-cycle MREG
response interface; numerical result timing is measured from the capture.
"""

import argparse
import json
from pathlib import Path

from rtlgraph_completion import summarize as completion_summary
from rtlgraph_mxu1_capture import verify_artifact
from rtlgraph_mxu1_trace import series
from rtlgraph_mxu1_vcd import edge_samples, read_header
from rtlgraph_s0 import artifact, checked_path
from rtlgraph_vpu import OPS, DOUBLE, ROW
from rtlgraph_vpu_vcd import SIGNALS


# Instructions.scala and VPUOp source correlation, independent of command wires.
VR_OPS = {0x00: 0, 0x02: 1, 0x03: 2, 0x04: 24, 0x06: 23,
          0x01: 14, 0x05: 22, 0x07: 21, 0x21: 13, 0x24: 20, 0x26: 19,
          0x40: 25, 0x41: 3, 0x42: 9, 0x43: 10, 0x46: 11, 0x47: 12,
          0x44: 16, 0x45: 17, 0x48: 18, 0x49: 5, 0x4a: 6, 0x4b: 7,
          0x4c: 8, 0x4d: 4}
COL = frozenset((14, 21, 22))
VLI = frozenset((26, 27, 28, 29))


def require(condition, message):
    if not condition:
        raise ValueError(message)


def value(sample, key):
    result = sample.get(key)
    require(type(result) is int and 0 <= result < 1 << SIGNALS[key][1],
            'Unknown, missing, or out-of-range ' + key)
    return result


def decode_vpu(word):
    require(type(word) is int and 0 <= word < 1 << 32, 'Invalid scalar instruction')
    opcode = word & 0x7f
    if opcode not in (0x57, 0x5f):
        return None
    result = {'vd': (word >> 7) & 63}
    if opcode == 0x5f:
        index = (word >> 13) & 7
        require(index < 4, 'Unsupported VLI encoding')
        result['op'] = (29, 28, 27, 26)[index]
    else:
        require(word >> 25 in VR_OPS, 'Unsupported VPU encoding')
        result.update(op=VR_OPS[word >> 25], vs1=(word >> 13) & 63, vs2=(word >> 19) & 63)
    return result


def shape(command, slot, write=False):
    """Logical row-address sequence; no cycle ages are assumed."""
    op = command['op']
    if write:
        if op in ROW:
            return [(command['vd'] + slot - 1, row) for row in range(32)]
        if op in DOUBLE and slot == 2:
            return []
        count = 32 if op in (16, 26, 27) else 64
        return [(command['vd'] + row // 32, row % 32) for row in range(count)]
    if op in VLI:
        return []
    if op in ROW:
        return [(command['vs1'] + slot - 1, row) for row in range(32)]
    base = command['vs2'] if op in (16, 17) or op in DOUBLE and slot == 2 else command['vs1']
    count = 128 if op in COL else 32 if op == 17 else 64
    return [(base + row // 32 % 2, row % 32) for row in range(count)]


class Events:
    def __init__(self):
        self.commands = []
        self.owners = {1: None, 2: None}
        self.previous_reads = {1: None, 2: None}
        self.previous_physical = {0: False, 1: False}
        self.last_cycle = None
        self.sample_count = self.maximum_inflight = self.mirrored_count = 0

    def step(self, cycle, sample):
        require(type(cycle) is int and (self.last_cycle is None or cycle == self.last_cycle + 1),
                'Missing or reordered sample cycle')
        self.last_cycle, self.sample_count = cycle, self.sample_count + 1
        if sample.get('reset') != 0:
            require(not self.commands, 'Reset or unknown reset after VPU execution began')
            return

        # Responses are bound to previous observed requests, before slot reuse.
        for port in (0, 1):
            require(value(sample, f'physical.response{port}') == self.previous_physical[port],
                    'Physical response does not match the prior observed request')
        require(value(sample, 'mirrored_d') == bool(self.previous_reads[1] and self.previous_reads[2]
                    and self.previous_reads[1][2] == self.previous_reads[2][2]), 'Mirrored response tag mismatch')
        for slot in (1, 2):
            previous = self.previous_reads[slot]
            if previous is not None:
                owner, index, address = previous
                owner['responses'][str(slot)].append(cycle)
                # Double binary operations advance both read streams using slot1.
                # Slot2's dataInFire may be false despite an actual operand response.
                if not (slot == 2 and owner['op'] in DOUBLE - ROW):
                    require(value(sample, f'slot{slot}.response') == 1, 'Operand response was not consumed')

        # Attribute old writes before assigning a simultaneous new command.
        for slot in (1, 2):
            owner = self.owners[slot]
            write = value(sample, f'physical.write{slot - 1}.valid')
            if owner is None:
                require(not write, 'MREG write has no accepted VPU owner')
                continue
            if slot == 1 or owner['op'] not in DOUBLE:
                require(value(sample, f'slot{slot}.resident_op') == owner['op'], 'Resident opcode differs from its accepted owner')
            expected = shape(owner, slot, write=True)
            stream = owner['writes'][str(slot)]
            if write:
                require(len(stream) < len(expected), 'Unexpected or duplicate VPU write')
                address = tuple(value(sample, f'physical.write{slot - 1}.{key}') for key in ('mreg', 'row'))
                require(address == expected[len(stream)], 'VPU destination or write row mismatch')
                require(value(sample, f'slot{slot}.result') == 1, 'MREG write lacks a result pulse')
                if owner['op'] not in VLI:
                    require(bool(owner['responses'][str(slot if owner['op'] not in DOUBLE else 1)]),
                            'VPU result precedes every operand response')
                stream.append(cycle)
            if expected:
                finished = len(stream) == len(expected)
                require(value(sample, f'slot{slot}.done') == finished, 'Slot release does not match final independently counted write')
                if finished:
                    owner['releases'][str(slot)] = cycle

        for owner in {id(o): o for o in self.owners.values() if o is not None}.values():
            slots = owner['slots']
            if all(len(owner['writes'][str(s)]) == len(shape(owner, s, write=True)) for s in slots):
                require(all(len(owner['reads'][str(s)]) == len(shape(owner, s)) for s in slots),
                        'VPU release precedes its complete operand stream')
                owner['release_cycle'] = cycle
                for slot in slots:
                    self.owners[slot] = None

        fire = value(sample, 'scalar.fire')
        decoded = decode_vpu(value(sample, 'scalar.instr')) if fire else None
        command_valid = value(sample, 'cmd.valid')
        require(command_valid == (decoded is not None) == bool(value(sample, 'inst_fire')),
                'Scalar VPU issue and engine/FSM command are unmatched')
        if decoded is not None:
            require(value(sample, 'cmd.op') == decoded['op'] + 1
                    and value(sample, 'inst_type') == decoded['op'], 'VPU command opcode mismatch')
            for key in decoded.keys() - {'op'}:
                require(value(sample, 'cmd.' + key) == decoded[key], 'VPU command operands mismatch')
            require(value(sample, 'ready') == 1 and not (value(sample, 'issue_busy') >> (decoded['op'] + 1) & 1),
                    'VPU issued while selected resources were busy')
            slots = [1, 2] if decoded['op'] in DOUBLE else [1 if self.owners[1] is None else 2]
            require(all(self.owners[s] is None for s in slots), 'VPU launch replaced an unfinished owner')
            command = {**decoded, 'id': len(self.commands), 'name': OPS[decoded['op']],
                       'accepted_cycle': cycle, 'scalar_pc': value(sample, 'scalar.pc'),
                       'scalar_word': value(sample, 'scalar.instr'), 'slots': slots,
                       'reads': {str(s): [] for s in slots}, 'responses': {str(s): [] for s in slots},
                       'writes': {str(s): [] for s in slots}, 'releases': {}, 'release_cycle': None}
            self.commands.append(command)
            for slot in slots:
                self.owners[slot] = command
            self.maximum_inflight = max(self.maximum_inflight, len({id(o) for o in self.owners.values() if o is not None}))

        current = {1: None, 2: None}
        for slot in (1, 2):
            if value(sample, f'logical.read{slot}.valid'):
                owner = self.owners[slot]
                require(owner is not None, 'VPU read has no accepted owner')
                expected, stream = shape(owner, slot), owner['reads'][str(slot)]
                require(len(stream) < len(expected), 'Unexpected or duplicate VPU operand read')
                address = tuple(value(sample, f'logical.read{slot}.{key}') for key in ('mreg', 'row'))
                require(address == expected[len(stream)], 'VPU operand register or row mismatch')
                current[slot] = (owner, len(stream), address)
                stream.append(cycle)

        mirrored = bool(current[1] and current[2] and current[1][2] == current[2][2])
        require(value(sample, 'mirrored') == mirrored, 'Duplicate-read detection mismatch')
        self.mirrored_count += int(mirrored)
        for port in (0, 1):
            read = current[port + 1]
            active = read is not None and not (port == 1 and mirrored)
            require(value(sample, f'physical.read{port}.valid') == active, 'Physical read request/suppression mismatch')
            if active:
                address = tuple(value(sample, f'physical.read{port}.{key}') for key in ('mreg', 'row'))
                require(address == read[2], 'Physical read address differs from logical request')
            self.previous_physical[port] = active
        self.previous_reads = current

    def finish(self):
        require(bool(self.commands), 'No VPU commands observed')
        require(not any(self.owners.values()) and not any(self.previous_reads.values()), 'Truncated capture with pending VPU work')
        commands = []
        for command in self.commands:
            result = dict(command)
            for kind in ('reads', 'responses', 'writes'):
                result[kind] = {slot: series(cycles, command['accepted_cycle']) for slot, cycles in command[kind].items()}
            result['release_age'] = command['release_cycle'] - command['accepted_cycle']
            commands.append(result)
        return {'status': 'vpu_observed_row_ownership_passed', 'command_count': len(commands),
                'sample_count': self.sample_count, 'commands': commands,
                'maximum_inflight_commands': self.maximum_inflight, 'mirrored_read_count': self.mirrored_count,
                'scope': 'Scalar words bind accepted VPU commands; independently counted logical row shapes bind physical requests/responses, destination writes and final slot release. Numerical ages are measured.',
                'limitations': ['Finite observations are not universal timing or schedule-safety proofs.',
                                'Row shapes and decoder labels are source-correlated; temporal ages are observed, not statically derived.',
                                'One-cycle MREG response alignment is checked in this execution; response data and bank-return ownership are not checked.',
                                'Command binding covers opcode and register IDs; immediate and scaling payloads are outside the capture.',
                                'Internal FSM fire pulses outside attributed physical row events are not completely accounted; column reductions intentionally discard early result pulses.',
                                'Arithmetic correctness comes from the replay golden, not individual row payload comparison.',
                                'Slot assignment follows lowest-free-slot policy; it does not prove FSM reachability or source-to-simulator build linkage.']}


def check_samples(samples):
    events = Events()
    for cycle, _time, sample in samples:
        try:
            events.step(cycle, sample)
        except ValueError as error:
            raise ValueError(f'cycle {cycle}: {error}') from error
    return events.finish()


def probe_summary(manifest_path, manifest):
    """Separate spot-check evidence; never route a probe through a golden check."""
    from rtlgraph_kernel import load_assembler
    from rtlgraph_perf import probe_host_result
    from rtlgraph_vpu_probes import probe_contract, analyze_samples

    require(manifest.get('kind') == 'rtlgraph-vpu-probe-replay' and manifest.get('config') == 'EE290SimConfig',
            'Unsupported VPU probe manifest')
    require(manifest.get('status') == 'passed' and manifest.get('finished_utc')
            and manifest.get('result', {}).get('status') == 'SPOT_CHECKS_PASS', 'Probe requires complete successful spot checks')
    require(manifest.get('trace_validation', {}).get('status') == 'PARSED', 'Probe requires a fully parsed capture')
    assembly = verify_artifact(manifest['inputs']['assembly'])
    assembler = verify_artifact(manifest['inputs']['assembler'])
    verify_artifact(manifest['binary'])
    trace = verify_artifact(manifest['trace'])
    contract = probe_contract(assembly.read_text(), load_assembler(assembler))
    require(json.loads(verify_artifact(manifest['probe_contract']).read_text()) == contract,
            'Recorded probe contract differs from assembled source')
    simulations = [command for command in manifest['commands'] if command['argv'] == manifest['prepared_command']['argv']]
    require(len(simulations) == 1, 'Expected one recorded prepared probe simulator command')
    simulation = simulations[0]
    host = probe_host_result(simulation, verify_artifact(simulation['log']).read_text(errors='replace'), assembly.stem)
    require(host['status'] == 'HOST_COMPLETION_ONLY' and host['metrics'] == manifest['result']['metrics'],
            'Probe host completion/metric differs from recorded replay')
    with trace.open() as stream:
        selected, timescale = read_header(stream, SIGNALS)
        observed = analyze_samples(edge_samples(stream, selected, SIGNALS), contract)
    require({**observed, 'timescale': timescale} == json.loads(verify_artifact(manifest['probe_observation']).read_text()),
            'Recorded probe observation differs from checked waveform')
    require(observed['metrics']['csr_counter_delta'] == host['metrics']['dbg1_cycles'], 'Probe trace/host cycle mismatch')
    verify_artifact(manifest['trace'])
    return {**observed, 'validation_class': 'numerical_spot_checks', 'replay_manifest': artifact(manifest_path),
            'assembly': artifact(assembly), 'assembler': artifact(assembler), 'trace': manifest['trace']}


def summarize(manifest_path):
    manifest_path = checked_path(manifest_path)
    manifest = json.loads(manifest_path.read_text())
    require(manifest.get('capture_kind') == 'vpu_rows', 'Requires an explicit VPU capture')
    actual = {key: (record['path'], record['width']) for key, record in manifest['signals'].items()}
    require(actual == SIGNALS, 'VPU capture signal contract differs')
    is_probe = manifest.get('kind') == 'rtlgraph-vpu-probe-replay'
    completion = probe_summary(manifest_path, manifest) if is_probe else completion_summary(manifest_path)
    trace = verify_artifact(manifest['trace'])
    with trace.open() as stream:
        selected, timescale = read_header(stream, SIGNALS)
        report = check_samples(edge_samples(stream, selected, SIGNALS))
    verify_artifact(manifest['trace'])
    report.update(schema='atlas.rtlgraph.vpu-observation.v1', sampling='settled_pre_rising_edge',
                  timescale=timescale, trace=manifest['trace'], replay_manifest=artifact(manifest_path),
                  driver=artifact(checked_path(__file__)), completion=completion,
                  functional_validation='numerical_spot_checks' if is_probe else 'full_tensor_golden',
                  helpers={name: artifact(checked_path(Path(__file__).parent / name)) for name in
                           ('rtlgraph_vpu_vcd.py', 'rtlgraph_vpu.py', 'rtlgraph_mxu1_vcd.py', 'rtlgraph_mxu1_trace.py', 'rtlgraph_completion.py', 'rtlgraph_vpu_probes.py', 'rtlgraph_perf.py')})
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
    print(json.dumps({'status': report['status'], 'command_count': report['command_count'], 'output': str(output)}))


if __name__ == '__main__':
    main()

#!/usr/bin/env python3
"""Replay full-output Atlas kernels with fixed-host controls and RTL completion timing."""

import argparse
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import struct
import subprocess
import sys
import time

from rtlgraph_replay_control import controlled_source, inspect_binary, patch_binary, source_program, PROGRAM

CORE = 'TestDriver.testHarness.chiptop0.system.domain.atlasTile.core'
SIGNALS = {name: (CORE + '.scalar.' + rtl, width) for name, rtl, width in (
    ('clock', 'clock', 1), ('reset', 'reset', 1), ('fire', 's1_fire', 1),
    ('instruction', 'decoder.io_instr', 32), ('pc', 'pc_ctrl.io_s1_pc', 32),
    ('csr_valid', 'io_csrPort_valid', 1), ('csr_addr', 'io_csrPort_addr', 12),
    ('csr_op', 'io_csrPort_op', 3), ('csr_data', 'io_csrPort_wdata', 32))}
SIGNALS.update({f'dma_busy_{i}': (CORE + f'.scalar.io_dma_busy_{i}', 1) for i in range(8)})


def digest(path):
    value = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for data in iter(lambda: stream.read(1024 * 1024), b''):
            value.update(data)
    return value.hexdigest()


def artifact(path):
    path = Path(path).resolve()
    return {'path': str(path), 'sha256': digest(path)}


def verify(info, path=None):
    path = Path(path or info['path']).resolve()
    if digest(path) != info['sha256']:
        raise ValueError(f'artifact hash mismatch: {path}')
    return path


def fixture_words(path):
    fixture = json.loads(Path(path).read_text())
    checks = fixture.get('dram_checks', [])
    if not checks:
        raise ValueError('full-output replay needs nonempty dram_checks')
    for kind, value in (('dram_checks', 'expected'), ('dram_preloads', 'data')):
        seen = set()
        for item in fixture.get(kind, []):
            offset = item['word_offset']
            if type(offset) is not int or offset < 0 or offset in seen:
                raise ValueError(f'invalid or duplicate {kind} beat offset')
            seen.add(offset)
            if not 0 <= int(item[value], 0) < 1 << 256:
                raise ValueError('fixture beat does not fit 256 bits')
    return len(checks) * 8


def validate_issued(events, program, witness=None):
    words = struct.unpack(f'<{len(program) // 4}I', program)
    if not events:
        raise ValueError('empty issue trace')
    for event in events:
        pc = event['pc']
        if not 0 <= pc < len(words) or event['word'] != words[pc]:
            raise ValueError(f'issued PC/word differs from assembled program: {pc}')
    counts = Counter(event['pc'] for event in events)
    expected = (witness or {}).get('expected_pc_visits', {})
    if any(counts[int(pc)] != count for pc, count in expected.items()):
        raise ValueError('witness PC visit count differs from execution')
    return {'issued_instructions': len(events), 'pc_words_match': True,
            'expected_pc_visits_checked': expected}


def trace_metrics(path):
    """Sample settled values before each rising edge, independent of VCD line ordering."""
    scopes, declarations, selected = [], {}, {}
    values, changes, cycle, first, finish, halted = {}, {}, 0, None, None, None
    period, last_rise, time_ticks = None, None, None
    issued, completion_count = [], 0

    def batch():
        nonlocal cycle, first, finish, halted, period, last_rise, completion_count
        clock = selected['clock']
        if values.get(clock) == 0 and changes.get(clock, values.get(clock)) == 1:
            if last_rise is not None:
                gap = time_ticks - last_rise
                if period is not None and gap != period:
                    raise ValueError('nonuniform Atlas clock')
                period = gap
            last_rise = time_ticks
            sample = {name: values.get(code) for name, code in selected.items()}
            if sample['reset'] == 0:
                if sample['fire'] == 1:
                    if sample['instruction'] is None or sample['pc'] is None:
                        raise ValueError('unknown issued instruction')
                    if first is None:
                        first = cycle
                    issued.append({'cycle': cycle, 'pc': sample['pc'], 'word': sample['instruction']})
                    if sample['instruction'] == 0x73:
                        halted = cycle
                if sample['csr_valid'] == 1 and sample['csr_addr'] == 0xc10 and sample['csr_op'] != 0:
                    if sample['csr_data'] != 1 or any(sample[f'dma_busy_{i}'] != 0 for i in range(8)):
                        raise ValueError('DBG0 completion failed or DMA still pending')
                    finish = cycle
                    completion_count += 1
            cycle += 1
        values.update(changes)

    with Path(path).open() as stream:
        directive = []
        for line in stream:
            directive.extend(line.split())
            if '$end' not in directive:
                continue
            words, directive = directive[:-1], []
            if words[0] == '$scope':
                scopes.append(words[2])
            elif words[0] == '$upscope':
                scopes.pop()
            elif words[0] == '$var':
                declarations['.'.join(scopes + [words[4]])] = (words[3], int(words[2]))
            elif words[0] == '$enddefinitions':
                break
        for key, (name, width) in SIGNALS.items():
            if name not in declarations or declarations[name][1] != width:
                raise ValueError(f'missing or wrong-width trace signal: {name}')
            selected[key] = declarations[name][0]
        codes = set(selected.values())
        for raw in stream:
            line = raw.strip()
            if not line or line.startswith('$'):
                continue
            if line.startswith('#'):
                new_time = int(line[1:])
                if time_ticks is not None and new_time < time_ticks:
                    raise ValueError('nonmonotonic VCD time')
                if time_ticks is not None and new_time != time_ticks:
                    batch()
                    changes = {}
                time_ticks = new_time
            else:
                bits, code = line[1:].split() if line[0].lower() == 'b' else (line[0], line[1:])
                if code in codes:
                    value = None if re.search('[xz]', bits, re.I) else int(bits, 2)
                    if code in changes and changes[code] != value:
                        raise ValueError('multiple selected transitions at one timestamp')
                    changes[code] = value
        if time_ticks is not None:
            batch()
    if first is None or finish is None or completion_count != 1 or first > finish:
        raise ValueError('expected exactly one successful completion after first issue')
    return {'first_issue_cycle': first, 'completion_cycle': finish, 'ecall_cycle': halted,
            'first_issue_to_completion_edges': finish - first, 'first_issue_to_ecall_edges': halted - first if halted is not None else None,
            'issued': issued}


def replay(smoke_manifest, assembly, golden_json, output, *, control_manifest=None, capacity=1024,
           timeout=900, capture=True, witness_manifest=None):
    smoke_manifest, assembly, golden_json, output = map(lambda p: Path(p).resolve(),
                                                       (smoke_manifest, assembly, golden_json, output))
    output.mkdir(parents=True, exist_ok=False)
    record = {'schema': 'atlas.rtlgraph.perf-replay.v2', 'status': 'preparing', 'commands': [],
              'simulator_build_lineage': 'Unverified cached build; artifact equality is not a source-to-binary proof.',
              'measurement': 'Atlas first accepted instruction through successful DBG0 after DMA completion; host setup and output checking excluded.'}
    manifest = output / 'manifest.json'
    def save():
        manifest.write_text(json.dumps(record, indent=2) + '\n')
    try:
        witness = None
        if witness_manifest:
            witness = json.loads(Path(witness_manifest).read_text())
            for key in ('compiler', 'profile', 'generator', 'native_input', 'native_output', 'baremetal', 'golden'):
                verify(witness[key])
            for key in ('adapter', 'assembler'):
                if key in witness:
                    verify(witness[key])
            if verify(witness['baremetal']) != assembly or verify(witness['golden']) != golden_json:
                raise ValueError('witness assembly/golden do not match replay inputs')
            record['witness_manifest'] = artifact(witness_manifest)
        smoke = json.loads(smoke_manifest.read_text())
        if smoke['status'] != 'passed' or smoke['config'] != 'EE290SimConfig':
            raise ValueError('expected a passed EE290SimConfig smoke record')
        runtime = smoke_manifest.parent / 'runtime'
        simulator = verify(smoke['inputs']['simulator'], runtime / Path(smoke['inputs']['simulator']['path']).name)
        assembler, gcc = [verify(smoke['inputs'][key]) for key in ('assembler', 'gcc')]
        if witness and 'assembler' in witness and witness['assembler']['sha256'] != smoke['inputs']['assembler']['sha256']:
            raise ValueError('witness assembler differs from replay assembler')
        for key, base in (('runtime_files', Path(str(simulator) + '.daidir')),
                          ('coverage_design_inputs', runtime / 'coverage-template.vdb'),
                          ('dramsim_inputs', runtime / 'dramsim2_ini')):
            for info in smoke[key]:
                verify(info, base / info['relative_path'])
        for key in ('toolchain_link_inputs', 'runtime_libraries'):
            for info in smoke[key].values():
                verify(info)
        record.update(inputs={key: artifact(path) for key, path in (
            ('smoke_manifest', smoke_manifest), ('assembly', assembly), ('golden', golden_json),
            ('assembler', assembler), ('gcc', gcc))}, simulator=artifact(simulator),
            expected_words=fixture_words(golden_json), replay_driver=artifact(__file__),
            control_driver=artifact(Path(__file__).with_name('rtlgraph_replay_control.py')))
        env = os.environ.copy()
        env['PATH'] = str(gcc.parent) + os.pathsep + env.get('PATH', '')
        env['LD_LIBRARY_PATH'] = smoke['environment']['LD_LIBRARY_PATH']
        env['PYTHONDONTWRITEBYTECODE'] = '1'

        def run(argv, name, command_env=env):
            began = time.monotonic()
            with (output / name).open('w') as log:
                process = subprocess.Popen(list(map(str, argv)), cwd=output, env=command_env,
                                           stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
                try:
                    status = process.wait(timeout=timeout)
                except (subprocess.TimeoutExpired, KeyboardInterrupt):
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
                    raise RuntimeError(f'timeout or interruption: {name}')
            record['commands'].append({'argv': list(map(str, argv)), 'returncode': status,
                                       'seconds': time.monotonic() - began, 'log': artifact(output / name)})
            save()
            if status:
                raise RuntimeError(f'command failed ({status}): {name}')

        raw, source, binary = output / 'assembled.c', output / 'kernel.c', output / 'kernel.riscv'
        run([sys.executable, assembler, assembly, '--golden-json', golden_json, '--out-c', raw], 'assemble.log')
        text, host_name = raw.read_text(), assembly.stem
        base = None
        if control_manifest:
            base = json.loads(Path(control_manifest).read_text())
            if base['status'] != 'passed' or base['replay_control']['mode'] != 'template':
                raise ValueError('control must be a passed template replay')
            for key in ('smoke_manifest', 'golden', 'assembler', 'gcc'):
                verify(base['inputs'][key])
                if base['inputs'][key]['sha256'] != record['inputs'][key]['sha256']:
                    raise ValueError('control input differs: ' + key)
            capacity = base['replay_control']['capacity_words']
        if capacity:
            text, control = controlled_source(text, assembly.stem, capacity)
            record['replay_control'] = dict(mode='patch' if base else 'template', **control)
            host_name = control['host_name']
        source.write_text(text)
        if base:
            original_c = verify(base['host_source']).read_text()
            if PROGRAM.sub('ATLAS_PROGRAM_CONTENTS', text) != PROGRAM.sub('ATLAS_PROGRAM_CONTENTS', original_c):
                raise ValueError('host source or golden data differ from template')
            patched, region = patch_binary(verify(base['binary']).read_bytes(), source_program(original_c),
                                           source_program(text), capacity)
            binary.write_bytes(patched)
            record['control_manifest'] = artifact(control_manifest)
        else:
            flags = ['-g', '-march=rv64imafd', '-mabi=lp64d', '-mcmodel=medany', '-specs=htif_nano.specs']
            obj = output / 'kernel.o'
            run([gcc, *flags, '-std=gnu99', '-O2', '-Wall', '-Wextra', '-fno-common', '-fno-builtin-printf',
                 '-c', source, '-o', obj], 'compile.log')
            run([gcc, *flags, '-static', '-T', 'htif.ld', obj, '-o', binary], 'link.log')
            if capacity:
                region = inspect_binary(binary.read_bytes(), source_program(text), capacity)
        if capacity:
            record['replay_control']['elf_region'] = region
        record.update(host_source=artifact(source), binary=artifact(binary))
        shutil.copytree(runtime / 'coverage-template.vdb', output / 'coverage.vdb')
        argv = [simulator, '+permissive', '-no_save', '+dramsim', '+dramsim_ini_dir=' + str(runtime / 'dramsim2_ini'),
                '+max-cycles=70000000', '+ntb_random_seed=1', '+loadmem=' + str(binary),
                '-cm', 'line+cond+fsm+branch+tgl+assert', '-cm_dir', str(output / 'coverage.vdb'), '-cm_name', 'kernel']
        if capture:
            tcl, vpd, vcd = output / 'capture.tcl', output / 'completion.vpd', output / 'completion.vcd'
            tcl.write_text('set fid [dump -file {' + str(vpd) + '} -type VPD]\n' +
                           ''.join('dump -add {' + name + '} -fid $fid\n' for name, _ in SIGNALS.values()) +
                           'run\ndump -close\nquit\n')
            argv += ['-ucli', '-i', str(tcl), '-k', 'off']
        argv += ['+permissive-off', binary]
        run(argv, 'simulation.log')
        log = (output / 'simulation.log').read_text(errors='replace')
        if f'*** PASSED *** ({host_name} — all DRAM checks passed)' not in log or any(
                marker in log for marker in ('*** FAILED ***', 'FAIL:', 'Assertion failed', 'Error-[', 'Fatal-[')):
            raise RuntimeError('RTL execution did not pass numerical/assertion checks')
        if re.findall(r'Verifying DRAM results \(([0-9]+) words\)', log) != [str(record['expected_words'])]:
            raise RuntimeError('RTL DRAM check count differs from fixture')
        counters = {}
        for key in ('dbg1_cycles', 'mcycles', 'minstret'):
            found = re.findall(r'^\s*' + key + r'\s*=\s*([0-9]+)\s*$', log, re.M)
            if len(found) == 1:
                counters[key] = int(found[0])
        record['diagnostic_counters'] = counters
        record['counter_scope'] = 'CSR brackets may move between variants; raw host counters and CSR deltas are not whole-kernel timing comparisons.'
        if capture:
            converter = Path('/tools/synopsys/vcs/W-2024.09-1/bin/vpd2vcd')
            conversion_env = dict(env, VCS_HOME=str(converter.parent.parent))
            run([converter, vpd, vcd], 'conversion.log', conversion_env)
            metrics = trace_metrics(vcd)
            record['execution_audit'] = validate_issued(metrics['issued'], source_program(text), witness)
            events = output / 'issued.json'
            events.write_text(json.dumps(metrics.pop('issued'), separators=(',', ':')) + '\n')
            record.update(metrics=metrics, trace=artifact(vcd), issued=artifact(events),
                          converter=artifact(converter))
        record.update(status='passed', checked_words=record['expected_words'], simulation_log=artifact(output / 'simulation.log'))
        for info in record['inputs'].values():
            verify(info)
    except Exception as error:
        record.update(status='failed', error=str(error))
        raise
    finally:
        save()
    return record


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--smoke-manifest', type=Path, required=True)
    parser.add_argument('--assembly', type=Path, required=True)
    parser.add_argument('--golden-json', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--control-manifest', type=Path)
    parser.add_argument('--witness-manifest', type=Path)
    parser.add_argument('--capacity', type=int, default=1024, help='fixed host IMEM words; 0 disables control for CFG witnesses')
    parser.add_argument('--timeout-seconds', type=int, default=900)
    parser.add_argument('--no-capture', action='store_true')
    args = parser.parse_args()
    result = replay(args.smoke_manifest, args.assembly, args.golden_json, args.output,
                    control_manifest=args.control_manifest, capacity=args.capacity,
                    timeout=args.timeout_seconds, capture=not args.no_capture, witness_manifest=args.witness_manifest)
    print(json.dumps({key: result[key] for key in ('status', 'checked_words', 'metrics') if key in result}))


if __name__ == '__main__':
    main()

#!/usr/bin/env python3
"""Replay the generated DMA CFG witness on a hash-checked cached EE290 simulator."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import time


def digest(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def verify(info, path=None):
    path = Path(path or info['path']).resolve()
    if digest(path) != info['sha256']:
        raise ValueError(f'artifact hash mismatch: {path}')
    return path


def artifact(path):
    return {'path': str(path), 'sha256': digest(path)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--smoke-manifest', type=Path, required=True)
    parser.add_argument('--witness-manifest', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--timeout-seconds', type=int, default=900)
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    record = {'schema': 'atlas.rtlgraph.dma-replay.v1', 'status': 'preparing', 'commands': [],
              'simulator_build_lineage': 'Unverified cached build; artifact equality is not a source-to-binary proof.'}
    manifest = output / 'manifest.json'

    def save():
        manifest.write_text(json.dumps(record, indent=2) + '\n')

    try:
        smoke = json.loads(args.smoke_manifest.read_text())
        witness = json.loads(args.witness_manifest.read_text())
        if smoke['status'] != 'passed' or smoke['config'] != 'EE290SimConfig':
            raise ValueError('expected a passed EE290SimConfig smoke record')
        for key in ('compiler', 'profile', 'generator', 'native_input', 'native_output', 'baremetal', 'golden'):
            verify(witness[key])
        runtime = args.smoke_manifest.resolve().parent / 'runtime'
        simulator = verify(smoke['inputs']['simulator'], runtime / Path(smoke['inputs']['simulator']['path']).name)
        assembler = verify(smoke['inputs']['assembler'])
        gcc = verify(smoke['inputs']['gcc'])
        for info in smoke['runtime_files']:
            verify(info, Path(str(simulator) + '.daidir') / info['relative_path'])
        for field, base in (('coverage_design_inputs', runtime / 'coverage-template.vdb'),
                            ('dramsim_inputs', runtime / 'dramsim2_ini')):
            for info in smoke[field]:
                verify(info, base / info['relative_path'])
        for field in ('toolchain_link_inputs', 'runtime_libraries'):
            for info in smoke[field].values():
                verify(info)
        record.update(smoke_manifest=artifact(args.smoke_manifest.resolve()),
                      witness_manifest=artifact(args.witness_manifest.resolve()), simulator=artifact(simulator),
                      replay_driver=artifact(Path(__file__).resolve()),
                      verified_runtime_files=len(smoke['runtime_files']),
                      expected_dram_words=witness['expected_dram_words'])
        env = os.environ.copy()
        env['PATH'] = str(gcc.parent) + os.pathsep + env.get('PATH', '')
        env['LD_LIBRARY_PATH'] = smoke['environment']['LD_LIBRARY_PATH']
        env['PYTHONDONTWRITEBYTECODE'] = '1'

        def run(command, name):
            began = time.monotonic()
            with (output / name).open('w') as log:
                process = subprocess.Popen(command, cwd=output, env=env, stdout=log, stderr=subprocess.STDOUT,
                                           start_new_session=True)
                try:
                    status = process.wait(timeout=args.timeout_seconds)
                except (subprocess.TimeoutExpired, KeyboardInterrupt):
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
                    raise RuntimeError(f'timeout: {name}')
            record['commands'].append({'argv': command, 'returncode': status,
                                       'seconds': time.monotonic() - began, 'log': name})
            save()
            if status:
                raise RuntimeError(f'command failed ({status}): {name}')

        source = output / 'dma_cfg_copy.c'
        binary = output / 'dma_cfg_copy.riscv'
        run([sys.executable, str(assembler), witness['baremetal']['path'], '--golden-json',
             witness['golden']['path'], '--out-c', str(source)], 'assemble.log')
        flags = ['-g', '-march=rv64imafd', '-mabi=lp64d', '-mcmodel=medany', '-specs=htif_nano.specs']
        obj = output / 'dma_cfg_copy.o'
        run([str(gcc), *flags, '-std=gnu99', '-O2', '-Wall', '-Wextra', '-fno-common', '-fno-builtin-printf',
             '-c', str(source), '-o', str(obj)], 'compile.log')
        run([str(gcc), *flags, '-static', '-T', 'htif.ld', str(obj), '-o', str(binary)], 'link.log')
        record['host_source'] = artifact(source)
        record['binary'] = artifact(binary)
        shutil.copytree(runtime / 'coverage-template.vdb', output / 'coverage.vdb')
        run([str(simulator), '+permissive', '-no_save', '+dramsim',
             '+dramsim_ini_dir=' + str(runtime / 'dramsim2_ini'), '+max-cycles=70000000',
             '+ntb_random_seed=1', '+loadmem=' + str(binary), '-cm', 'line+cond+fsm+branch+tgl+assert',
             '-cm_dir', str(output / 'coverage.vdb'), '-cm_name', 'dma_cfg_copy', '+permissive-off', str(binary)],
            'simulation.log')
        log = (output / 'simulation.log').read_text()
        if '*** PASSED *** (dma_cfg_copy — all DRAM checks passed)' not in log or any(
                marker in log for marker in ('*** FAILED ***', 'FAIL:', 'Assertion failed', 'Error-[', 'Fatal-[')):
            raise RuntimeError('RTL execution did not pass numerical/assertion checks')
        if re.findall(r'Verifying DRAM results \(([0-9]+) words\)', log) != [str(witness['expected_dram_words'])]:
            raise RuntimeError('RTL DRAM check count differs from witness')
        cycles = re.findall(r'^\s*dbg1_cycles\s*=\s*([0-9]+)\s*$', log, re.M)
        if len(cycles) != 1 or int(cycles[0]) <= 0:
            raise RuntimeError('missing whole-copy cycle bracket')
        record.update(status='passed', checked_words=witness['expected_dram_words'], dbg1_cycles=int(cycles[0]),
                      measurement=witness['measurement'], simulation_log=artifact(output / 'simulation.log'))
    except Exception as error:
        record.update(status='failed', error=str(error))
        raise
    finally:
        save()
    print(manifest)


if __name__ == '__main__':
    main()

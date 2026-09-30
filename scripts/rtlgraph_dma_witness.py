#!/usr/bin/env python3
"""Build a DMA loop/join copy witness for the original baremetal assembler."""

import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess


SOURCE = """dma.config.ch0 x0
lui x1, 0x90000
addi x3, x1, 1024
addi x4, x0, 0
addi x2, x0, 128
addi x8, x0, 3
addi x9, x0, 2
csrrs x29, x0, 0xC00
loop:
dma.load.ch0 x4, x1, x2
addi x1, x1, 128
beq x8, x9, alternate
nop
addi x10, x0, 11
jal x0, joined
nop
alternate:
addi x10, x0, 22
joined:
dma.store.ch1 x3, x4, x2
addi x3, x3, 128
addi x8, x8, -1
bne x8, x0, loop
nop
dma.wait.ch1
csrrs x30, x0, 0xC00
sub x31, x30, x29
csrrw x0, x31, 0xC11
addi x28, x0, 1
csrrw x0, x28, 0xC10 # atlas.release
ecall
"""


def baremetal(source):
    """Only the witness's scalar/DMA subset; unknown operations fail closed."""
    result = ['# @TIMEOUT 100000', '# @DRAM_BASE 0x90000000', '# @PERF_REPORT']
    scalar = {'lui', 'addi', 'sub', 'beq', 'bne', 'jal', 'nop', 'ecall', 'delay'}
    for line in source.splitlines():
        line = line.split('#', 1)[0].strip()
        if not line:
            continue
        if line.endswith(':'):
            result.append(line)
            continue
        op, *args = re.split(r'[\s,]+', line)
        match = re.fullmatch(r'dma\.(load|store|config|wait)\.ch([0-7])', op)
        if match:
            op = 'DMA.' + match[1].upper()
            args.append(match[2])
        elif op in {'csrrw', 'csrrs'} and len(args) == 3:
            args[1], args[2] = args[2], args[1]
        elif op not in scalar:
            raise ValueError(f'unsupported witness instruction: {line}')
        result.append(op.upper() + (' ' + ', '.join(args) if args else ''))
    return '\n'.join(result) + '\n'


def artifact(path):
    return {'path': str(path.resolve()), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--compiler', type=Path, required=True)
    parser.add_argument('--profile', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    before = args.output / 'before.S'
    after = args.output / 'after.S'
    before.write_text(SOURCE)
    command = [str(args.compiler.resolve()), str(before), '-o', str(after),
               '--rtl-dma-profile', str(args.profile.resolve()),
               '--passes', 'strip-artifacts,insert-dma-waits,fill-delay-slots,schedule']
    run = subprocess.run(command, text=True, capture_output=True)
    (args.output / 'compiler.log').write_text(run.stdout + run.stderr)
    if run.returncode:
        raise RuntimeError('compiler rejected witness; see compiler.log')
    assembly = args.output / 'dma_cfg_copy.S'
    assembly.write_text(baremetal(after.read_text()))
    entries = [hex(int.from_bytes(bytes((beat * 37 + byte * 11) & 255 for byte in range(32)), 'little'))
               for beat in range(12)]
    golden = args.output / 'dma_cfg_copy.json'
    golden.write_text(json.dumps({
        'dram_preloads': [{'word_offset': i, 'data': value} for i, value in enumerate(entries)],
        'dram_checks': [{'word_offset': 32 + i, 'expected': value} for i, value in enumerate(entries)],
    }, indent=2) + '\n')
    record = {
        'schema': 'atlas.rtlgraph.dma-witness.v1',
        'compiler': artifact(args.compiler), 'profile': artifact(args.profile), 'generator': artifact(Path(__file__)),
        'command': command, 'native_input': artifact(before), 'native_output': artifact(after),
        'baremetal': artifact(assembly), 'golden': artifact(golden),
        'scope': 'Three 128-byte copies, both branch outcomes, pending DMA across joins/backedges, and captured pointer reuse.',
        'expected_dram_words': 96,
        'measurement': 'CSR cycle bracket includes matching final DMA wait; hardware execution not performed by this generator.',
        'translation': 'Restricted scalar/DMA spelling and CSR operand-order adapter; no general assembly contract conversion.',
    }
    (args.output / 'manifest.json').write_text(json.dumps(record, indent=2) + '\n')
    print(assembly)


if __name__ == '__main__':
    main()

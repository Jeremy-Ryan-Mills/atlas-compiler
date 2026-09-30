#!/usr/bin/env python3
"""Build a DMA loop/join copy witness for the original baremetal assembler."""

import argparse
import hashlib
import json
from pathlib import Path
import subprocess

from rtlgraph_assembly import assemble, load_assembler, tokens, translate


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
    return '# @TIMEOUT 100000\n# @DRAM_BASE 0x90000000\n# @PERF_REPORT\n' + translate(source, to_compiler=False)


def artifact(path):
    return {'path': str(path.resolve()), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--compiler', type=Path, required=True)
    parser.add_argument('--profile', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--assembler', type=Path, help='verify instruction PC coverage against the original assembler')
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
        'adapter': artifact(Path(__file__).with_name('rtlgraph_assembly.py')),
        'command': command, 'native_input': artifact(before), 'native_output': artifact(after),
        'baremetal': artifact(assembly), 'golden': artifact(golden),
        'scope': 'Three 128-byte copies, both branch outcomes, pending DMA across joins/backedges, and captured pointer reuse.',
        'expected_dram_words': 96,
        'measurement': 'CSR cycle bracket includes matching final DMA wait; hardware execution not performed by this generator.',
        'translation': 'Restricted scalar/DMA spelling and CSR operand-order adapter; no general assembly contract conversion.',
    }
    if args.assembler:
        assembler = load_assembler(args.assembler)
        native = after.read_text()
        labels, instructions = {}, []
        for line in native.splitlines():
            fields = tokens(line)
            if not fields:
                continue
            if fields[0].endswith(':'):
                labels[fields[0][:-1]] = len(instructions)
            else:
                instructions.append(fields)
        words = assemble(assembler, assembly.read_text())
        if len(words) != len(instructions) or any(fields[0] == 'li' for fields in instructions):
            raise ValueError('witness PC map requires one encoded word per native instruction')
        expected = {labels['loop']: 3, labels['alternate']: 1, labels['joined']: 3}
        for pc, fields in enumerate(instructions):
            if fields == ['addi', 'x10', 'x0', '11']:
                expected[pc] = 2
            if fields == ['addi', 'x1', 'x1', '128']:
                expected[pc] = 3
        record['assembler'] = artifact(args.assembler)
        record['expected_pc_visits'] = {str(pc): count for pc, count in sorted(expected.items())}
    (args.output / 'manifest.json').write_text(json.dumps(record, indent=2) + '\n')
    print(assembly)


if __name__ == '__main__':
    main()

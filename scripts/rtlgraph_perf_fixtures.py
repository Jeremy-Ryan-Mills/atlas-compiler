#!/usr/bin/env python3
"""Add complete output checks to three perf probes that lack external goldens."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import struct


EXPECTED = {
    'perf_vpu_binary': (4 + 2, 4 - 2, 4 * 2, min(4, 2), max(4, 2)),
    'perf_vpu_reduction': (64 * 4, 4, 4, 32 * 4, 4, 4),
    'perf_mm_single': (32 * 1 * 1, 32 * 1 * 2),
}


def exact_bf16(value):
    bits = struct.unpack('>I', struct.pack('>f', value))[0]
    if bits & 0xffff:
        raise ValueError('fixture arithmetic must be exact in BF16')
    return bits >> 16


def output_dma(byte_count):
    lines = ['LI x5, 0', 'DMA.CONFIG x5, 0']
    for offset in range(0, byte_count, 4096):
        lines += [f'LI x6, {0x20000000 + offset // 4}', f'LI x10, {0x90010000 + offset}',
                  f'LI x12, {min(4096, byte_count - offset)}',
                  'DMA.STORE x10, x6, x12, 0', 'DMA.WAIT 0']
    return '\n'.join(lines) + '\n'


def generate(baremetal, output):
    output.mkdir(parents=True, exist_ok=False)
    records = []
    for name, expected in EXPECTED.items():
        values = tuple(map(exact_bf16, expected))
        path = baremetal / 'assembly' / (name + '.S')
        original = path.read_text()
        source = re.sub(r'^\s*#\s*@(?:PYTHON_GEN|PERF_UTIL_THRESHOLD).*\n', '', original, flags=re.M)
        preload = []
        if name == 'perf_mm_single':
            # Exact powers of two avoid engine-specific accumulation rounding.
            for tile, value in enumerate((0x38, 0x38, 0x40)):
                preload += [{'word_offset': tile * 32 + row, 'data': '0x' + f'{value:02x}' * 32}
                            for row in range(32)]
            tail = ['DELAY 128']
            for engine in (0, 1):
                dest, base = 8 + 2 * engine, 0x20000000 + 512 * engine
                tail += [f'VMATPOP.BF16.MXU{engine} {dest}, 0', 'DELAY 64',
                         f'LI x8, {base}', f'VSTORE {dest}, x8, 0', 'DELAY 33',
                         f'VSTORE {dest + 1}, x8, 8', 'DELAY 33']
            marker = re.search(r'^\s*LI\s+x30,\s*1\s*$', source, flags=re.M)
            if marker is None:
                raise ValueError('missing single-matmul completion marker')
            source = source[:marker.start()] + '\n'.join(tail) + '\n' + output_dma(4096) + source[marker.start():]
            changes = 'Constant FP8 inputs X=1, W0=1, W1=2 replace random preloads; append drain, both BF16 accumulator pops, complete MREG stores and output DMA; remove utilization gate.'
        else:
            slots = iter(range(len(values)))
            source, count = re.subn(r'^\s*LI\s+x8,\s*0x20000000\s*$',
                                    lambda _: f'    LI x8, {0x20000000 + 512 * next(slots)}', source, flags=re.M)
            if count != len(values):
                raise ValueError('unexpected number of VPU result stores')
            source, count = re.subn(r'^(\s*VSTORE\s+8,\s*x8,\s*0\s*)$',
                                    r'\1\n    DELAY 33\n    VSTORE 9, x8, 8', source, flags=re.M)
            if count != len(values) or source.count('pass:') != 1:
                raise ValueError('unexpected VPU result layout')
            byte_slots = iter(range(len(values)))
            source = re.sub(r'^(\s*LHU\s+x10,\s*x8,\s*0\s*)$',
                            lambda match: f'    LI x8, {0x20000000 + 2048 * next(byte_slots)}\n' + match[0],
                            source, flags=re.M)
            source = source.replace('pass:', 'pass:\n' + output_dma(len(values) * 2048))
            changes = 'Preserve uniform inputs, operations, scalar checks and branches; place each result pair in separate VMEM and append complete output DMA before success.'
        source = '# Validation variant: additional output observations; not original-kernel performance.\n# @DRAM_BASE 0x90000000\n' + source.rstrip() + '\n'
        checks = [{'word_offset': 0x10000 // 32 + tile * 64 + row,
                   'expected': '0x' + f'{value:04x}' * 16}
                  for tile, value in enumerate(values) for row in range(64)]
        fixture = {'dram_base': '0x90000000', 'timeout': 400000,
                   'dram_preloads': preload, 'dram_checks': checks}
        assembly, golden = output / (name + '.S'), output / (name + '.json')
        assembly.write_text(source)
        golden.write_text(json.dumps(fixture, indent=2) + '\n')
        records.append({'kernel': name, 'source': str(path.resolve()),
                        'source_sha256': hashlib.sha256(original.encode()).hexdigest(),
                        'assembly': str(assembly.resolve()), 'golden': str(golden.resolve()),
                        'assembly_sha256': hashlib.sha256(assembly.read_bytes()).hexdigest(),
                        'golden_sha256': hashlib.sha256(golden.read_bytes()).hexdigest(),
                        'changes': changes, 'expected_words': len(checks) * 8,
                        'expected_bf16': [hex(value) for value in values]})
    (output / 'manifest.json').write_text(json.dumps({'schema': 'atlas.rtlgraph.perf-fixtures.v1', 'kernels': records}, indent=2) + '\n')
    return records


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baremetal', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    for record in generate(args.baremetal, args.output):
        print(record['kernel'], record['expected_words'])

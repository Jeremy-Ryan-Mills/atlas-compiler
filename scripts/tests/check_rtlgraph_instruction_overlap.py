#!/usr/bin/env python3
"""Compare the native VPU overlap table with a generated typed-control report."""
import argparse
import json
from pathlib import Path
import subprocess


parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('probe', type=Path)
parser.add_argument('evidence', type=Path, help='Instruction extractor output directory')
args = parser.parse_args()
profile = json.loads((args.evidence / 'vpu' / 'profile.json').read_text())
pairs = json.loads((args.evidence / 'vpu-overlap.json').read_text())
names = {n: 'v'+n+'.bf16' for n in ('add', 'sub', 'mul', 'sqrt', 'sin', 'cos', 'tanh', 'exp', 'exp2', 'square', 'cube', 'relu')}
names.update(rcp='vrecip.bf16', log='vlog2.bf16', rsum='vredsum.row.bf16', rmax='vredmax.row.bf16',
             rmin='vredmin.row.bf16', csum='vredsum.bf16', cmax='vredmax.bf16', cmin='vredmin.bf16',
             fp8pack='vpack.bf16.fp8', fp8unpack='vunpack.fp8.bf16', pairmax='vmaximum.bf16',
             pairmin='vminimum.bf16', mov='vmov', vliOne='vli.one', vliCol='vli.col', vliRow='vli.row', vliAll='vli.all')
commands = ''.join(names[p['first']]+' '+names[p['second']]+'\n' for p in pairs)
actual = [int(x) for x in subprocess.check_output([str(args.probe.resolve())], input=commands, text=True).split()]
if len(actual) != len(pairs) or len({(p['first'], p['second']) for p in pairs}) != len(names)**2:
    raise SystemExit('Incomplete command-pair comparison')
for pair, native in zip(pairs, actual):
    expected = pair['gap'] < profile['instructions'][pair['first']]['write_release']
    if native not in (0, 1) or bool(native) != expected:
        raise SystemExit(f'Native overlap mismatch: {pair}, native={native}')
print(f'All {len(actual)} native VPU overlap decisions match typed engine issue masks.')

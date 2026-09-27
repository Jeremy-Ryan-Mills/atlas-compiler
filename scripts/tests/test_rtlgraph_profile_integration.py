#!/usr/bin/env python3
"""Show synthetic CIRCT pipeline changes reaching atlas-opt's emitted spacing.

Usage: python test_rtlgraph_profile_integration.py EXPORTER ATLAS_OPT
This test creates explicitly synthetic profiles, not evidence about production RTL.
"""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).absolute().parents[1]))
from rtlgraph_mxu1_profile import valid_pipeline


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    exporter, compiler = [str(Path(value).resolve()) for value in sys.argv[1:]]
    fixture = Path(__file__).with_name('rtlgraph-profile.mlir').read_text()
    changed = fixture.replace('  hw.output %stage1 : i1',
        '  %stage2 = seq.firreg %stage1 clock %clock reset sync %reset, %zero : i1\n  hw.output %stage2 : i1')
    results = []
    with tempfile.TemporaryDirectory(prefix='rtlgraph-sensitivity-') as directory:
        root = Path(directory)
        source = root / 'kernel.S'
        source.write_text('vmatmul.mxu1 acc0, m0, w0\nvmatpop.fp8.acc.mxu1 m8, acc0, e0\n')
        for number, text in enumerate((fixture, changed)):
            ir, evidence, profile, output = [root / f'{number}.{suffix}' for suffix in ('mlir', 'json', 'profile', 'S')]
            ir.write_text(text)
            graph = json.loads(subprocess.check_output([exporter, str(ir), 'ValidPipeline'], text=True))
            derived = valid_pipeline(graph['modules'][0])
            evidence.write_text(json.dumps({'scope': 'SYNTHETIC TEST ONLY', 'pipeline': derived}))
            profile.write_text('schema=atlas-mxu1-profile-v1\nconfig=EE290SimConfig\n'
                f'source_ir_sha256={digest(ir)}\nevidence_sha256={digest(evidence)}\n'
                f'first_write_age={derived["latency"] + 1}\noverwrite_acc_read_hold=0\n')
            subprocess.run([compiler, str(source), '--passes', 'strip-artifacts,schedule',
                            '--experimental-mxu1-profile', str(profile), '-o', str(output), '-q'], check=True)
            cycle, pop_cycle = 0, None
            for line in output.read_text().splitlines():
                tokens = line.split('#', 1)[0].strip().split()
                if not tokens:
                    continue
                if tokens[0].startswith('vmatpop'):
                    pop_cycle = cycle
                cycle += 1 + (int(tokens[1]) if tokens[0] == 'delay' else 0)
            results.append((derived['latency'], pop_cycle))
    if results != [(2, 4), (3, 5)]:
        raise AssertionError(f'Pipeline change did not reach emitted schedule: {results}')
    print('PASS: typed CIRCT valid depth 2→3 changes extracted write age 3→4 and emitted pop issue 4→5')


if __name__ == '__main__':
    main()

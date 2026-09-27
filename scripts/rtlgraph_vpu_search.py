#!/usr/bin/env python3
"""Search a prepared VPU experiment and splice a model-checked fixed-op candidate.

The solver uses the built-in compiler model; this runner does not infer timing
from RTL. The new schedule-experiment manifest is accepted by rtlgraph_compare.
Every distinct candidate still requires a separate golden-checked RTL replay.
"""
from __future__ import annotations

import argparse
import copy
import json
import math
import os
from pathlib import Path
import subprocess

from rtlgraph_kernel import KernelError, assemble, load_assembler
from rtlgraph_mxu1_capture import verify_artifact
from rtlgraph_s0 import artifact
from rtlgraph_schedule import compute_slice, issue_summary, splice, split, translate


def prepare(candidate_manifest, solver, library, output, seconds):
    if not math.isfinite(seconds) or not 0 < seconds <= 3600:
        raise KernelError('seconds must be finite and in (0, 3600]')
    previous = json.loads(candidate_manifest.read_text())
    if (previous.get('schema') != 'atlas.rtlgraph.schedule-experiment.v1'
            or previous.get('status') != 'model_candidates_ready'):
        raise KernelError('expected a prepared schedule-experiment manifest')
    if previous['inputs'].get('profiles'):
        raise KernelError('VPU search uses the built-in model; partial MXU profiles are unsupported')
    source = verify_artifact(previous['inputs']['source']).read_text()
    assembler = load_assembler(verify_artifact(previous['inputs']['assembler']))
    verify_artifact(previous['inputs']['compiler'])
    preserve = 'memory_wrapper' in previous
    compute = compute_slice(split(source).body, preserve_memory_wrapper=preserve)
    compiled = translate(compute.body, to_compiler=True) + 'ecall\n'
    splice(source, compiled, assembler, preserve_memory_wrapper=preserve)
    output.mkdir(parents=True, exist_ok=False)
    prepared = output / 'original.compiler.S'
    prepared.write_text(compiled)
    scheduled = output / 'exact.compiler.S'
    report = output / 'search.json'
    command = [str(solver), '--source', str(prepared), '--output', str(scheduled),
               '--report', str(report), '--seconds', str(seconds)]
    run = subprocess.run(command, capture_output=True, text=True, env=os.environ.copy())
    log = output / 'search.log'
    log.write_text(run.stdout + run.stderr)
    if run.returncode:
        raise KernelError(f'VPU search failed; see {log}')
    search = json.loads(report.read_text())
    if search.get('schema') != 'atlas.rtlgraph.vpu-search.v1':
        raise KernelError('unexpected solver report schema')
    candidate, body = splice(source, scheduled.read_text(), assembler,
                             preserve_memory_wrapper=preserve)
    assembly = output / 'exact.S'
    assembly.write_text(candidate)
    words = assemble(assembler, candidate)
    identical = None
    for name, case in previous['cases'].items():
        if assemble(assembler, verify_artifact(case['assembly']).read_text()) == words:
            identical = name
            break
    result = copy.deepcopy(previous)
    root = Path(__file__).resolve().parent.parent
    result['search'] = {
        'input_manifest': artifact(candidate_manifest),
        'driver': artifact(Path(__file__).resolve()),
        'adapter': artifact(root / 'scripts/rtlgraph_schedule.py'),
        'solver_source': artifact(root / 'scripts/rtlgraph_vpu_search.cpp'),
        'solver_executable': artifact(solver), 'supplied_compiler_library': artifact(library),
        'build_provenance': 'The supplied library and current source hashes are recorded as inputs. '
                            'This runner does not establish that this executable was built from them; '
                            'retain the compiler invocation/build log separately.',
        'compiler_sources': [artifact(path) for path in sorted((root / 'src').rglob('*'))
                             if path.suffix in {'.cpp', '.h'}],
        'prepared_body': artifact(prepared), 'report': artifact(report),
        'status': search['status'], 'command': command,
        'scope': 'Fixed operations/operands and original dependency graph under the built-in model. '
                 'Resource conflicts and capacity-two VPU slots constrain the search. '
                 'A model optimum is not a temporal RTL proof; no numerical timing is newly extracted.',
        'validation': 'Complete reservation-table and compiler-simulator checks, assembler roundtrip, '
                      'and unchanged encoded non-idle operations; RTL replay remains separate.'}
    result['cases']['exact'] = {
        'assembly': artifact(assembly), 'scheduled': artifact(scheduled),
        'command': command, 'log': artifact(log), 'search_report': artifact(report),
        'issue_summary': issue_summary(body, assembler, preserve_memory_wrapper=preserve),
        'identical_encoded_stream_to': identical,
        'unchanged_non_idle_words': True, 'full_assembly_parts_equal': True}
    (output / 'manifest.json').write_text(json.dumps(result, indent=2) + '\n')
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('candidate-manifest', 'solver', 'library', 'output'):
        parser.add_argument('--' + name, type=Path, required=True)
    parser.add_argument('--seconds', type=float, default=60)
    args = parser.parse_args()
    result = prepare(args.candidate_manifest.resolve(), args.solver.resolve(),
                     args.library.resolve(), args.output.resolve(), args.seconds)
    case = result['cases']['exact']
    print(json.dumps({'status': result['search']['status'], 'candidate': case['assembly']['path'],
                      'static_window': case['issue_summary']['cycle_csr_dispatch_window'],
                      'duplicate': case['identical_encoded_stream_to']}))


if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError, KeyError) as error:
        raise SystemExit(f'rtlgraph_vpu_search: {error}')

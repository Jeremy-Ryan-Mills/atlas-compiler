#!/usr/bin/env python3
"""Bundle and verify the RTL-derived atlas-opt model used by a native kernel."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

SCHEMA = 'atlas.rtlgraph.contract.v1'
CONFIG = 'EE290SimConfig'
FLAGS = {'mxu0': '--experimental-mxu0-profile', 'mxu1': '--experimental-mxu1-profile',
         'dma': '--rtl-dma-profile', 'lsu': '--rtl-lsu-profile', 'xlu': '--rtl-xlu-profile', 'vpu': '--rtl-vpu-profile'}
MERLIN_REF = '81a585b857838baeba35bc55eab7db10525db7cb'
MERLIN_URL = ('https://github.com/ucb-bar/merlin/blob/' + MERLIN_REF +
              '/examples/atlas/phase1/contracts/hwbringup_atlas_v0/schedule_contract.yaml')


def require(value, message):
    if not value:
        raise ValueError(message)


def read_json(path):
    return json.loads(path.read_text())


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2) + '\n')


def identity(path, root=None):
    path = path.resolve()
    return {'path': str(path.relative_to(root) if root else path),
            'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}


def checked(record, root=None):
    require(set(record) == {'path', 'sha256'} and re.fullmatch('[0-9a-f]{64}', record['sha256']),
            'Invalid artifact identity')
    path = Path(record['path'])
    if root:
        require(not path.is_absolute() and '..' not in path.parts, 'Bundle path escapes its directory')
        path = (root / path).resolve()
        require(path.is_relative_to(root.resolve()), 'Bundle symlink escapes its directory')
    require(identity(path)['sha256'] == record['sha256'], f'Artifact changed: {path}')
    return path


def profile_fields(path):
    fields = {}
    for line in path.read_text().splitlines():
        line = line.split('#', 1)[0].strip()
        if not line:
            continue
        require('=' in line, 'Expected profile key=value')
        key, value = (part.strip() for part in line.split('=', 1))
        require(key not in fields, f'Duplicate profile field: {key}')
        fields[key] = value
    return fields


def validate_profile(role, projection, evidence):
    fields, report = profile_fields(projection), read_json(evidence)
    schemas = [f'atlas-{role}-profile-v1'] + ([f'atlas-{role}-profile-v2'] if role in ('dma', 'lsu', 'xlu', 'mxu0') else [])
    require(fields.get('schema') in schemas and fields.get('config') == report.get('config') == CONFIG,
            f'Unsupported {role} profile')
    if role in ('dma', 'lsu', 'xlu', 'vpu'):
        version = fields['schema'].rsplit('-', 1)[1]
        require(report.get('schema') == f'atlas.rtlgraph.{role}-profile.{version}',
                f'Unsupported {role} evidence')
    else:
        # Connected MXU1 evidence evolved without changing its native projection.
        versions = (1, 2) if role == 'mxu1' else ((2,) if fields['schema'].endswith('-v2') else (1,))
        require(report.get('schema_version') in versions and report.get('kind') == f'atlas-partial-{role}-profile',
                f'Unsupported {role} evidence')
    require(fields.get('evidence_sha256') == identity(evidence)['sha256'],
            f'{role} profile/evidence identity differs')
    hardware = fields.get('source_ir_sha256', '')
    require(re.fullmatch('[0-9a-f]{64}', hardware) and
            report.get('inputs', {}).get('hardware_ir', {}).get('sha256') == hardware,
            f'{role} hardware identity differs')
    metadata = {'schema', 'config', 'source_ir_sha256', 'evidence_sha256'}
    require({k: v for k, v in fields.items() if k not in metadata} ==
            {k: str(v) for k, v in report.get('compiler_overrides', {}).items()},
            f'{role} profile settings differ from evidence')
    return hardware


def model_flags(entries, root):
    return [value for entry in entries
            for value in (FLAGS[entry['role']], str(checked(entry['projection'], root)))]


def query(compiler, source, entries, root, output):
    command = [str(compiler), str(source), '--dump-footprints', str(output), '--dma-timing', 'robust',
               *model_flags(entries, root)]
    result = subprocess.run(command, text=True, capture_output=True)
    require(result.returncode == 0, 'atlas-opt rejected the bundled model:\n' + result.stdout + result.stderr)
    return read_json(output)


def contract_semantics(footprints, roles):
    model, native = footprints.get('model', {}), footprints.get('semantics', {})
    rtl_dma = model.get('rtl_dma')
    require(type(rtl_dma) is bool and rtl_dma == ('dma' in roles),
            'Footprint DMA model differs from selected profiles')
    require(native.get('age_origin') == 'instruction issue' and native.get('hold_end') == 'inclusive',
            'Unsupported native footprint timing semantics')
    completion = ('memory lifetime until explicit matching DMA.WAIT; age is not a bound' if rtl_dma
                  else 'access at modeled DMA completion')
    require(native.get('at_completion') == completion, 'Unsupported native DMA completion semantics')
    return {'assembly': 'atlas-opt-native', 'age_zero': 'instruction-issue',
            'hold_interval': 'inclusive',
            'dma_completion': 'explicit-wait' if rtl_dma else 'modeled-completion'}


def load(path, compiler_override=None, fresh=True):
    contract, root = read_json(path), path.resolve().parent
    require(contract.get('schema') == SCHEMA and contract.get('config') == CONFIG,
            'Unsupported contract')
    compiler_record = dict(contract['compiler'])
    if compiler_override:
        compiler_record['path'] = str(compiler_override.resolve())
    compiler = checked(compiler_record)
    source = checked(contract['source'], root)
    footprints = checked(contract['footprints'], root)
    roles, hardware = [], set()
    for entry in contract['profiles']:
        require(set(entry) == {'role', 'projection', 'evidence'} and entry['role'] in FLAGS,
                'Unsupported profile entry')
        roles.append(entry['role'])
        hardware.add(validate_profile(entry['role'], checked(entry['projection'], root),
                                      checked(entry['evidence'], root)))
    require(roles and len(roles) == len(set(roles)) and hardware == {contract['source_ir_sha256']},
            'Missing, duplicate, or mixed-hardware profiles')
    saved = read_json(footprints)
    require(saved.get('schema') == 'atlas.footprints.v1' and
            saved.get('model', {}).get('source_ir_sha256') == contract['source_ir_sha256'],
            'Footprints do not describe the selected hardware model')
    require(contract.get('semantics') == contract_semantics(saved, roles),
            'Contract semantics differ from the selected native model')
    if fresh:
        with tempfile.TemporaryDirectory(prefix='rtlgraph-verify-') as directory:
            current = query(compiler, source, contract['profiles'], root, Path(directory) / 'footprints.json')
        require(current == saved, 'Saved footprints differ from a fresh atlas-opt query')
    return contract


def export(source, compiler, selected, output):
    require(selected and set(selected) <= set(FLAGS), 'Select at least one supported profile')
    prepared, hardware = [], set()
    for role, projection in sorted(selected.items()):
        projection, evidence = projection.resolve(), projection.resolve().with_name('profile.json')
        hardware.add(validate_profile(role, projection, evidence))
        prepared.append((role, projection, evidence))
    require(len(hardware) == 1, 'Cannot combine profiles from different hardware IR')
    source, compiler, output = source.resolve(), compiler.resolve(), output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    shutil.copyfile(source, output / 'before.S')
    entries = []
    for role, projection, evidence in prepared:
        destination = output / role
        destination.mkdir()
        shutil.copyfile(projection, destination / projection.name)
        shutil.copyfile(evidence, destination / 'profile.json')
        entries.append({'role': role, 'projection': identity(destination / projection.name, output),
                        'evidence': identity(destination / 'profile.json', output)})
    footprints = output / 'footprints.json'
    native = query(compiler, output / 'before.S', entries, output, footprints)
    contract = {
        'schema': SCHEMA, 'config': CONFIG, 'source_ir_sha256': hardware.pop(),
        'compiler': identity(compiler), 'source': identity(output / 'before.S', output),
        'profiles': entries, 'footprints': identity(footprints, output),
        'semantics': contract_semantics(native, selected),
        'merlin': {'reference': MERLIN_URL, 'adapter_status': 'no-lossless-gap-projection',
                   'precision_loss': ['row-specific operand ages and resolved ranges',
                                      'ports, resource capacities, and multi-instruction conflicts',
                                      'variable completion and wait-dependent reservations']},
        'limitations': ['Unselected machine rules remain inherited from this exact compiler.',
                        'The bundle records evidence identities; it does not rerun RTL proofs or validation.']}
    write_json(output / 'contract.json', contract)
    load(output / 'contract.json', fresh=False)
    return contract


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_subparsers(dest='action', required=True)
    create = actions.add_parser('export')
    create.add_argument('--source', type=Path, required=True)
    create.add_argument('--atlas-opt', type=Path, required=True)
    create.add_argument('--output', type=Path, required=True)
    for role in FLAGS:
        create.add_argument(f'--{role}-profile', type=Path)
    verify = actions.add_parser('verify')
    verify.add_argument('--contract', type=Path, required=True)
    verify.add_argument('--atlas-opt', type=Path)
    args = parser.parse_args()
    if args.action == 'export':
        selected = {role: getattr(args, role + '_profile') for role in FLAGS if getattr(args, role + '_profile')}
        export(args.source, args.atlas_opt, selected, args.output)
        print(json.dumps({'contract': str(args.output / 'contract.json')}))
    else:
        load(args.contract.resolve(), args.atlas_opt)
        print(json.dumps({'status': 'verified', 'contract': str(args.contract)}))


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise SystemExit(f'rtlgraph_contract: {error}')

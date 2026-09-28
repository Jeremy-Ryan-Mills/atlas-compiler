#!/usr/bin/env python3
"""Bundle partial RTL profiles and a native assembly handoff for atlas-opt."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess


SCHEMA = 'atlas.rtlgraph.contract.v1'
CONFIG = 'EE290SimConfig'
FLAGS = {'mxu0': '--experimental-mxu0-profile', 'mxu1': '--experimental-mxu1-profile',
         'dma': '--rtl-dma-profile'}
MERLIN_REF = '81a585b857838baeba35bc55eab7db10525db7cb'
MERLIN_URL = ('https://github.com/ucb-bar/merlin/blob/' + MERLIN_REF +
              '/examples/atlas/phase1/contracts/hwbringup_atlas_v0/schedule_contract.yaml')
SEMANTICS = {
    'assembly': 'atlas-opt-native', 'publication_annotation': 'atlas.release',
    'runtime_entry': 'idle engines, no pending DMA, zero scalar registers; runtime parameters must be established by assembly setup',
    'age_zero': 'instruction-issue', 'hold_interval': 'inclusive',
    'dma_completion': 'explicit-wait-when-dma-profile-selected',
    'dma_cost_estimate_is_completion_bound': False,
    'required_consumer_rules': ['row-access-dependencies', 'interval-resource-capacity',
                               'physical-mreg-ports', 'logical-mreg-reservations',
                               'vpu-slot-compatibility', 'variable-wait-reservations',
                               'control-flow-and-publication-admission'],
}
LIMITS = [
    'Only selected profile overrides are RTL-derived; all other rules are inherited from the pinned compiler.',
    'Hashes bind saved evidence, not a new proof or a re-execution of extraction and replay.',
    'The footprint query describes the model; scheduling, admission and reservation checks remain necessary.',
    'Each emitted schedule requires independent RTL execution and numerical validation.',
]


def require(condition, message):
    if not condition:
        raise ValueError(message)


def unique(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, f'Duplicate field: {key}')
        result[key] = value
    return result


def read_json(path):
    return json.loads(path.read_text(), object_pairs_hook=unique)


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2) + '\n')


def identity(path, *, relative_to=None):
    return dict(path=str(path.relative_to(relative_to) if relative_to else path.resolve()),
                sha256=hashlib.sha256(path.read_bytes()).hexdigest())


def checked_file(record, root=None):
    require(set(record) == {'path', 'sha256'} and
            re.fullmatch('[0-9a-f]{64}', record['sha256']), 'Invalid artifact identity')
    path = Path(record['path'])
    if root is not None:
        require(not path.is_absolute() and '..' not in path.parts, 'Bundle path escapes its directory')
        path = (root / path).resolve()
        require(path.is_relative_to(root.resolve()), 'Bundle symlink escapes its directory')
    require(identity(path)['sha256'] == record['sha256'], f'Artifact changed: {path}')
    return path


def read_profile(path):
    pairs = []
    for line in path.read_text().splitlines():
        line = line.split('#', 1)[0].strip()
        if line:
            require('=' in line, 'Expected profile key=value')
            pairs.append(tuple(part.strip() for part in line.split('=', 1)))
    return unique(pairs)


def checked_profile(role, path, evidence):
    fields, report = read_profile(path), read_json(evidence)
    schemas = [f'atlas-{role}-profile-v1'] + (['atlas-dma-profile-v2'] if role == 'dma' else [])
    require(fields.get('schema') in schemas and fields.get('config') == CONFIG and
            report.get('config') == CONFIG, 'Unsupported profile schema or configuration')
    if role == 'dma':
        version = fields['schema'].rsplit('-', 1)[1]
        require(report.get('schema') == f'atlas.rtlgraph.dma-profile.{version}', 'Wrong DMA evidence schema')
    else:
        require(report.get('kind') == f'atlas-partial-{role}-profile' and report.get('schema_version') == 1,
                'Wrong MXU evidence schema')
    require(fields.get('evidence_sha256') == identity(evidence)['sha256'], 'Profile evidence identity differs')
    hardware_hash = fields.get('source_ir_sha256', '')
    require(re.fullmatch('[0-9a-f]{64}', hardware_hash) and
            report['inputs']['hardware_ir']['sha256'] == hardware_hash, 'Hardware IR identity differs')
    metadata = {'schema', 'config', 'source_ir_sha256', 'evidence_sha256'}
    require({key: value for key, value in fields.items() if key not in metadata} ==
            {key: str(value) for key, value in report['compiler_overrides'].items()},
            'Profile settings differ from canonical evidence')
    return fields


def model_flags(profiles, root):
    return [item for entry in profiles for item in
            (FLAGS[entry['role']], str(checked_file(entry['projection'], root)))]


def execute(command, log):
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    log.write_text(result.stdout + result.stderr)
    require(result.returncode == 0, f'atlas-opt rejected handoff; see {log}')


def export(source, compiler, selected, output):
    require(selected and set(selected) <= set(FLAGS), 'Select at least one supported profile')
    prepared = []
    for role in sorted(selected):
        profile = selected[role].resolve()
        evidence = profile.with_name('profile.json')
        fields = checked_profile(role, profile, evidence)
        prepared.append((role, profile, evidence, fields))
    hashes = {fields['source_ir_sha256'] for _, _, _, fields in prepared}
    require(len(hashes) == 1, 'Cannot combine profiles from different hardware IR')
    source, compiler, output = source.resolve(), compiler.resolve(), output.resolve()
    inputs = {name: identity(path) for name, path in (('source', source), ('compiler', compiler))}
    output.mkdir(parents=True, exist_ok=False)
    shutil.copyfile(source, output / 'before.S')
    profiles = []
    for role, profile, evidence, fields in prepared:
        destination = output / role
        destination.mkdir()
        projected, canonical = destination / profile.name, destination / 'profile.json'
        shutil.copyfile(profile, projected)
        shutil.copyfile(evidence, canonical)
        profiles.append(dict(role=role, projection=identity(projected, relative_to=output),
                             evidence=identity(canonical, relative_to=output)))
    query = [str(compiler), str(output / 'before.S'), '--dump-footprints', str(output / 'footprints.json'),
             '--dma-timing', 'robust', *model_flags(profiles, output)]
    execute(query, output / 'query.log')
    require(read_json(output / 'footprints.json').get('schema') == 'atlas.footprints.v1',
            'Unsupported native footprint schema')
    for record in inputs.values():
        checked_file(record)
    contract = dict(schema=SCHEMA, config=CONFIG, source_ir_sha256=hashes.pop(),
                    compiler=inputs['compiler'], source=identity(output / 'before.S', relative_to=output),
                    profiles=profiles, footprints=identity(output / 'footprints.json', relative_to=output),
                    semantics=SEMANTICS, limitations=LIMITS,
                    merlin=dict(reference=MERLIN_URL, adapter_status='no-lossless-gap-projection',
                                precision_loss=['row-specific operand ages and operand-resolved ranges',
                                                'port exclusions and resource capacities across multiple instructions',
                                                'explicit variable-latency completion and wait-dependent reservations']),
                    query=dict(command=query, log=identity(output / 'query.log', relative_to=output)))
    write_json(output / 'contract.json', contract)
    load_contract(output / 'contract.json')
    return contract


def load_contract(path, compiler_override=None):
    contract, root = read_json(path), path.resolve().parent
    require(set(contract) == {'schema', 'config', 'source_ir_sha256', 'compiler', 'source', 'profiles',
                              'footprints', 'semantics', 'limitations', 'merlin', 'query'}, 'Unknown or missing contract fields')
    require(contract['schema'] == SCHEMA and contract['config'] == CONFIG and contract['semantics'] == SEMANTICS,
            'Unsupported contract or assembly semantics')
    compiler_record = dict(contract['compiler'])
    if compiler_override is not None:
        compiler_record['path'] = str(compiler_override.resolve())
    compiler = checked_file(compiler_record)
    source = checked_file(contract['source'], root)
    footprints = checked_file(contract['footprints'], root)
    dump = read_json(footprints)
    require(dump.get('schema') == 'atlas.footprints.v1' and dump.get('schedule_validated') is False,
            'Unsupported native footprint schema or validation claim')
    require(dump['model']['source_ir_sha256'] == contract['source_ir_sha256'], 'Footprint hardware IR identity differs')
    checked_file(contract['query']['log'], root)
    roles = []
    for entry in contract['profiles']:
        require(set(entry) == {'role', 'projection', 'evidence'} and entry['role'] in FLAGS, 'Unsupported profile entry')
        roles.append(entry['role'])
        fields = checked_profile(entry['role'], checked_file(entry['projection'], root), checked_file(entry['evidence'], root))
        require(fields['source_ir_sha256'] == contract['source_ir_sha256'], 'Mixed hardware IR identities')
    require(roles and len(roles) == len(set(roles)), 'Missing or duplicate profile roles')
    return contract, root, compiler, source


def schedule(contract_path, output, priority='critical', compiler_override=None):
    require(priority in ('critical', 'input'), 'Unsupported scheduling priority')
    contract_path, output = contract_path.resolve(), output.resolve()
    contract, root, compiler, source = load_contract(contract_path, compiler_override)
    snapshot = identity(contract_path)
    output.mkdir(parents=True, exist_ok=False)
    flags = ['--dma-timing', 'robust', *model_flags(contract['profiles'], root)]
    query = [str(compiler), str(source), '--dump-footprints', str(output / 'footprints.json'), *flags]
    execute(query, output / 'query.log')
    require(read_json(output / 'footprints.json') == read_json(root / contract['footprints']['path']),
            'Bundled footprints differ from fresh native query')
    after = output / 'after.S'
    command = [str(compiler), str(source), '--passes', 'strip-artifacts,schedule',
               '--schedule-priority', priority, *flags, '-o', str(after)]
    execute(command, output / 'schedule.log')
    check = [str(compiler), str(after), '--check', *flags]
    execute(check, output / 'check.log')
    checked_file(snapshot)
    load_contract(contract_path, compiler_override)
    result = dict(schema='atlas.rtlgraph.handoff.v1', status='native-model-checked',
                  contract=snapshot, compiler=identity(compiler), before=identity(source), after=identity(after),
                  priority=priority, commands=dict(query=query, schedule=command, check=check),
                  logs={name: identity(output / f'{name}.log') for name in ('query', 'schedule', 'check')},
                  footprints=identity(output / 'footprints.json'),
                  rtl_validation='required-separately')
    write_json(output / 'handoff.json', result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='action', required=True)
    create = commands.add_parser('export', help='Capture native before.S, profiles, evidence and resolved model')
    create.add_argument('--source', type=Path, required=True)
    create.add_argument('--atlas-opt', type=Path, required=True)
    for role in FLAGS:
        create.add_argument(f'--{role}-profile', type=Path)
    create.add_argument('--output', type=Path, required=True)
    run = commands.add_parser('schedule', help='Schedule the bundled native before.S using its pinned model')
    run.add_argument('--contract', type=Path, required=True)
    run.add_argument('--atlas-opt', type=Path, help='Relocated compiler binary with the same SHA-256')
    run.add_argument('--priority', choices=('critical', 'input'), default='critical')
    run.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.action == 'export':
        export(args.source, args.atlas_opt, {role: getattr(args, role + '_profile') for role in FLAGS
                                           if getattr(args, role + '_profile')}, args.output)
        print(json.dumps(dict(contract=str(args.output / 'contract.json'))))
    else:
        result = schedule(args.contract, args.output, args.priority, args.atlas_opt)
        print(json.dumps(dict(status=result['status'], after=result['after'])))


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise SystemExit(f'rtlgraph_contract: {error}')

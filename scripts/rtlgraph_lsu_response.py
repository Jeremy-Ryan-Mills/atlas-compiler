#!/usr/bin/env python3
"""Derive conditional one-cycle LSU read responses through typed memory logic."""
import argparse
import itertools
import json
from pathlib import Path
import subprocess

from rtlgraph_dma import direct, operation, register, slice_fact, stripped
from rtlgraph_lsu import analyze as analyze_local
from rtlgraph_lsu_timing import TypedCone, analyze as analyze_timing
from rtlgraph_mxu1 import Graph
from rtlgraph_mxu1_capture import verify_artifact
from rtlgraph_mxu1_profile import require
from rtlgraph_query import find_instance, instance_value
from rtlgraph_s0 import artifact


READERS = ('mxu0ReadReq0', 'mxu0ReadReq1', 'mxu1ReadReq0', 'mxu1ReadReq1',
           'vpuReadReq0', 'vpuReadReq1', 'lsuReadReq', 'xluReadReq')
VMEM_ORDER = ('lsuScalarWrite', 'lsuVecWrite', 'lsuScalarRead', 'lsuVecRead',
              'dmaWrite', 'dmaRead', 'tlWrite', 'tlRead')


def check(module, root, names, domains, expected, law):
    graph = Graph(module)
    evaluate = TypedCone(module, root, {n: graph.named(n) for n in names})
    require(len(names) == len(domains) and all(isinstance(domain, range) and
            domain == range(1 << evaluate.cut_widths[name]) for name, domain in zip(names, domains)),
            'LSU response check requires complete typed input domains')
    count = 0
    for values in itertools.product(*domains):
        require(evaluate(dict(zip(names, values))) == int(expected(*values)),
                'LSU response function mismatch: ' + law)
        count += 1
    return dict(law=law, assignments_checked=count, cone=evaluate.evidence)


def or_tree(module, root, leaves):
    """Structural OR decomposition covers arbitrary simultaneous bank responses."""
    graph = Graph(module)
    expected = {stripped(graph, value) for value in leaves}
    require(len(expected) == len(leaves), 'Aliased LSU response leaves')
    active, found = set(), set()

    def visit(value):
        value = stripped(graph, value)
        if value in expected:
            found.add(value)
            return
        require(value not in active, 'Cyclic LSU response tree')
        active.add(value)
        op = operation(graph, value, 'comb.or')
        require(op['result_types'] == ['i1'] and op['operands'], 'Unsupported response OR')
        for operand in op['operands']: visit(operand)
        active.remove(value)

    visit(root)
    require(found == expected, 'Missing LSU response bank')
    return graph.cone(root, {value: f'bank{i}' for i, value in enumerate(leaves)})


def memories(module, count, shape, read_write=False):
    graph = Graph(module)
    banks = [o for o in module['operations'] if o['kind'] == 'seq.firmem']
    require(len(banks) == count, 'Unsupported LSU memory bank count')
    result = []
    for bank in range(count):
        mem = operation(graph, graph.named(f'banks_{bank}'), 'seq.firmem')
        require(set(mem['attributes']) == {'name', 'prefix', 'readLatency', 'writeLatency', 'ruw', 'wuw'} and
                mem['attributes']['prefix'] == '' and mem['attributes']['wuw'] == '1 : i32',
                'Unsupported LSU memory attributes')
        require(mem['result_types'] == [shape] and not mem['operands'] and
                mem['attributes'].get('readLatency') == '1 : i32' and
                mem['attributes'].get('writeLatency') == '1 : i32' and
                mem['attributes'].get('ruw') == '0 : i32', 'Unsupported LSU memory latency or shape')
        users = [o for o, _ in graph.uses[mem['results'][0]]]
        kinds = ['seq.firmem.read_write_port'] if read_write else ['seq.firmem.read_port', 'seq.firmem.write_port']
        require(sorted(o['kind'] for o in users) == sorted(kinds), 'Unsupported LSU memory ports')
        port = next(o for o in users if o['kind'] == kinds[0])
        require(not port['has_regions'] and len(port['operands']) == (7 if read_write else 4) and
                port['result_types'] == ['i256'] and port['operands'][2] == graph.named('clock'),
                'Unsupported LSU memory read clock or port shape')
        if read_write:
            require(port['attributes'] == {'operandSegmentSizes': 'array<i32: 1, 1, 1, 1, 1, 1, 1>'},
                    'Unsupported LSU memory operand segments')
        else:
            require(not port['attributes'], 'Unsupported LSU memory read-port attributes')
        result.append(dict(memory=mem, read_port=port))
    return result


def mreg_decode(module):
    graph = Graph(module)
    return [check(module, graph.named(f'readBankOHs_{i}'),
                  ('io_' + port + '_valid', 'io_' + port + '_bits_mregId'),
                  (range(2), range(64)), lambda valid, reg: (1 << (reg % 32)) if valid else 0,
                  port + ': valid request selects physical bank mregId[4:0]')
            for i, port in enumerate(READERS)]


def mreg_bank(module, bank, memory):
    graph = Graph(module)
    names = [f'readHits_{bank}_{i}' if bank else f'readHits_{i}' for i in range(8)]
    slices = [slice_fact(module, graph.named(name), graph.named(f'readBankOHs_{i}'), bank, 1)
              for i, name in enumerate(names)]
    valid = register(module, f'bankReadValid_d_{bank}', 1)
    client = register(module, f'bankReadPort_d_{bank}', 3, reset=False)
    functions = [check(module, valid['operands'][0], names, [range(2)] * 8, lambda *hits: any(hits),
                       f'MREG bank {bank}: next valid is any read request'),
                 check(module, client['operands'][0], names, [range(2)] * 8,
                       lambda *hits: next((i for i, hit in enumerate(hits) if hit), 7),
                       f'MREG bank {bank}: next client is first requested port')]
    enable = direct(module, valid['operands'][0], memory['read_port']['operands'][3], 'MREG read enable equals captured valid')
    leaf = graph.named(f'respHits_6_{bank}')
    functions.append(check(module, leaf, (f'bankReadValid_d_{bank}', f'bankReadPort_d_{bank}'),
                           (range(2), range(8)), lambda v, p: v and p == 6,
                           f'MREG bank {bank}: LSU response iff previous selected client was LSU'))
    return dict(bank=bank, request_slices=slices, valid_register=valid, client_register=client,
                memory=memory, read_enable=enable, functions=functions), leaf


def vmem_decode(module):
    graph = Graph(module)
    return [check(module, graph.named(name + 'BankOH'),
                  ('io_' + name + '_valid', 'io_' + name + '_bits_bankIdx'),
                  (range(2), range(8)), lambda valid, bank: (1 << bank) if valid and bank < 6 else 0,
                  name + ': valid in-range request selects exactly one of six banks')
            for name in VMEM_ORDER[:4]]


def vmem_bank(module, bank, memory):
    graph = Graph(module)
    names = ['accessSel_leaf' + (f'_{bank * 8 + i}' if bank * 8 + i else '') + '_valid' for i in range(8)]
    slices = [slice_fact(module, graph.named(name), graph.named(port + 'BankOH'), bank, 1)
              for name, port in zip(names, VMEM_ORDER)]
    valid = register(module, f'r1_bankReadValid_{bank}', 1)
    client = register(module, f'r1_bankReadClient_{bank}', 3, reset=False)
    clients = (0, 0, 1, 2, 0, 3, 0, 4)
    selected = lambda hits: next((i for i, hit in enumerate(hits) if hit), None)
    read_client = lambda hits: clients[selected(hits)] if any(hits) else 0
    mode = operation(graph, memory['read_port']['operands'][5], 'comb.and')
    require(len(mode['operands']) == 3 and mode['result_types'] == ['i1'], 'Unsupported VMEM write-mode gate')
    mode_enable = direct(module, memory['read_port']['operands'][3], mode['operands'][0], 'VMEM write mode requires access enable')
    functions = [check(module, valid['operands'][0], names, [range(2)] * 8,
                       lambda *hits: read_client(hits) != 0, f'VMEM bank {bank}: next valid is winning read'),
                 check(module, client['operands'][0], names, [range(2)] * 8,
                       lambda *hits: read_client(hits), f'VMEM bank {bank}: next client follows strict port priority'),
                 check(module, memory['read_port']['operands'][3], names, [range(2)] * 8,
                       lambda *hits: any(hits), f'VMEM bank {bank}: SRAM enabled iff a request wins'),
                 check(module, mode['operands'][1], names, [range(2)] * 8,
                       lambda *hits: selected(hits) in (0, 1, 4, 6), f'VMEM bank {bank}: winning read forces write mode low regardless of mask')]
    leaf = graph.named(f'lsuVecRespSel_leaves_{bank}_valid')
    functions.append(check(module, leaf, (f'r1_bankReadValid_{bank}', f'r1_bankReadClient_{bank}'),
                           (range(2), range(8)), lambda v, p: v and p == 2,
                           f'VMEM bank {bank}: vector response iff previous selected client was LSU vector'))
    return dict(bank=bank, request_slices=slices, valid_register=valid, client_register=client,
                memory=memory, write_mode_gate=mode, write_mode_enable=mode_enable, functions=functions), leaf


def wrapper(module):
    graph = Graph(module)
    instances = {name: find_instance(module, name) for name in ('lsu', 'mreg', 'vmem')}
    wires = []
    for name, instance in instances.items():
        for signal in ('clock', 'reset'):
            wires.append(direct(module, graph.named(signal), instance_value(instance, signal, 'input'), name + ' common ' + signal))
    for port, target, sink in (('io_mregReadReq_bits_mregId', 'mreg', 'io_lsuReadReq_bits_mregId'),
                               ('io_vmemVecRead_bits_bankIdx', 'vmem', 'io_lsuVecRead_bits_bankIdx')):
        wires.append(direct(module, instance_value(instances['lsu'], port, 'output'),
                            instance_value(instances[target], sink, 'input'), 'LSU request bank -> ' + target))
    return wires


def analyze(modules):
    local = analyze_local(modules)
    result = dict(status='typed_lsu_response_contract_derived', wrapper=wrapper(modules['AtlasCore']),
                  lsu_local=local, lsu_timing=analyze_timing(modules['LSU']))
    for name, count, shape, decode, bank_query, output in (
            ('MregFile', 32, '!seq.firmem<64 x 256>', mreg_decode, mreg_bank, 'io_lsuReadResp_valid'),
            ('Vmem', 6, '!seq.firmem<8192 x 256, mask 32>', vmem_decode, vmem_bank, 'io_lsuVecReadData_valid')):
        module = modules[name]
        checked = [bank_query(module, bank, memory) for bank, memory in
                   enumerate(memories(module, count, shape, name == 'Vmem'))]
        result[name] = dict(bank_decode=decode(module), banks=[facts for facts, _ in checked],
                            response_or_tree=or_tree(module, Graph(module).named(output), [leaf for _, leaf in checked]))
    result['response_contract'] = dict(
        latency_edges=1,
        mreg='Each LSU read request returns valid at the next edge if it is the sole read request to its physical bank; reset is low.',
        vmem='Each in-range LSU vector read returns valid at the next edge if no LSU scalar write, vector write, or scalar read targets that bank; reset is low. DMA and TileLink have lower priority.',
        composition='Per-bank request decode, arbitration, SRAM enables, one-cycle valid/client registers and response OR trees establish the source-valid contract used by lsu_timing. Common wrapper clock/reset and interface wires are checked.',
        evidence='Exhaustive local finite-domain checks plus structural composition, not a circt-bmc run or whole-kernel proof.')
    result['remaining_assumptions'] = [
        'Transfer starts idle with empty LSU pending stages and memory response-valid registers; reset remains low and no second command enters the same path.',
        'Software maintains the stated physical-bank exclusion conditions on every request edge; arbitration priority is not permission for an MREG conflict.',
        'Valid response timing and memory read latency are established; payload values, selected row/address/data routing, writes, and same-row read/write visibility are not proved.',
        'VMEM addresses are aligned, in range and nonwrapping. LSU address-to-bank arithmetic and scalar instruction acceptance remain outside this query.',
        'No new compiler timing override, DMA completion bound, or source-to-cached-simulator build proof is introduced.']
    return result


def prepare(evidence_path, output):
    local = json.loads(evidence_path.read_text())
    require(local['schema'] == 'atlas.rtlgraph.lsu-local-functions.v1' and local['config'] == 'EE290SimConfig',
            'Wrong LSU response evidence schema/configuration')
    require(not output.exists(), 'Output must be a new evidence directory')
    for record in local['inputs'].values(): verify_artifact(record)
    hardware, exporter = (verify_artifact(local['inputs'][name]) for name in ('hardware_ir', 'exporter'))
    inputs = dict(evidence=artifact(evidence_path), hardware_ir=artifact(hardware), exporter=artifact(exporter),
                  driver=artifact(Path(__file__).resolve()))
    for name in ('rtlgraph_dma.py', 'rtlgraph_lsu.py', 'rtlgraph_lsu_timing.py', 'rtlgraph_mxu1.py',
                 'rtlgraph_mxu1_capture.py', 'rtlgraph_mxu1_profile.py', 'rtlgraph_query.py', 'rtlgraph_s0.py', 'rtlgraph_vpu.py'):
        inputs[name] = artifact(Path(__file__).with_name(name).resolve())
    output.mkdir(parents=True)
    typed_path = output / 'typed.json'
    command = [str(exporter), str(hardware), 'LSU', 'AtlasCore', 'MregFile', 'Vmem']
    with typed_path.open('w') as stream: subprocess.run(command, stdout=stream, check=True)
    typed = json.loads(typed_path.read_text())
    modules = {m['name']: m for m in typed['modules']}
    require(not typed['missing_modules'] and len(typed['modules']) == 4 and
            set(modules) == {'LSU', 'AtlasCore', 'MregFile', 'Vmem'}, 'Missing or ambiguous response modules')
    report = analyze(modules)
    require(all(local.get(key) == value for key, value in report['lsu_local'].items()), 'LSU local evidence differs from fresh analysis')
    report.update(schema='atlas.rtlgraph.lsu-response.v1', config='EE290SimConfig', inputs=inputs,
                  typed=artifact(typed_path), export_command=command)
    for record in inputs.values(): verify_artifact(record)
    path = output / 'lsu-response.json'
    path.write_text(json.dumps(report, indent=2) + '\n')
    return dict(status=report['status'], output=str(path), mreg_banks=32, vmem_banks=6,
                response_latency_edges=report['response_contract']['latency_edges'])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--lsu-evidence', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(prepare(args.lsu_evidence.resolve(), args.output.resolve())))


if __name__ == '__main__':
    try: main()
    except (OSError, ValueError, KeyError) as error:
        raise SystemExit(f'rtlgraph_lsu_response: {error}')

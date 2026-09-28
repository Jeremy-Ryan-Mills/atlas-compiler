#!/usr/bin/env python3
"""Check selected DMA launch, completion, and VMEM arbitration facts in typed CIRCT.

These are local structural/finite-domain facts, not a bounded DMA latency or a
proof of complete DMA correctness. No compiler latency is fitted or changed.
"""
import argparse
import itertools
import json
from pathlib import Path
import subprocess

from rtlgraph_mxu1 import Graph
from rtlgraph_mxu1_capture import verify_artifact
from rtlgraph_mxu1_profile import constant, require
from rtlgraph_query import find_instance, identity_chain, instance_value
from rtlgraph_s0 import artifact, checked_path
from rtlgraph_vpu import Cone


def stripped(graph, value):
    seen = set()
    while value in graph.definitions and graph.definitions[value]['identity_wire']:
        require(value not in seen, 'Cyclic identity wire')
        seen.add(value)
        op = graph.definitions[value]
        require(len(op['operands']) == 1, 'Unsupported identity wire')
        value = op['operands'][0]
    return value


def operation(graph, value, kind):
    value = stripped(graph, value)
    op = graph.definitions.get(value)
    require(op is not None and op['kind'] == kind and not op['has_regions'],
            'Expected ' + kind)
    return op


def direct(module, source, target, description):
    path = identity_chain(module, source, target)
    require(path is not None, 'DMA identity mismatch: ' + description)
    return {'connection': description, 'path': path}


def slice_fact(module, root, source, low, width):
    graph = Graph(module)
    op = operation(graph, root, 'comb.extract')
    require(op['result_types'] == [f'i{width}'] and op['attributes'].get('lowBit') == f'{low} : i32'
            and len(op['operands']) == 1, 'DMA address slice mismatch')
    path = direct(module, source, op['operands'][0], 'address slice input')
    return {'low_bit': low, 'width': width, 'operation': op, 'input': path}


def local(module, root, cuts, expected, law, result_width=1):
    """Exhaust all assignments of explicitly named integer cutpoints."""
    labels, values, widths = zip(*cuts)
    cone = Cone(module, root, list(values), list(widths), result_width)
    checked = 0
    for assignment in itertools.product(*(range(1 << width) for width in widths)):
        require(cone(assignment) == int(expected(*assignment)), 'DMA local function mismatch: ' + law)
        checked += 1
    return {'law': law, 'cutpoints': list(labels), 'assignments_checked': checked, 'cone': cone.evidence}


def named_cuts(graph, names, widths):
    return [(n, graph.named(n), w) for n, w in zip(names, widths)]


def register(module, name, width, reset=True):
    graph = Graph(module)
    op = operation(graph, graph.named(name), 'seq.firreg')
    require(op['result_types'] == [f'i{width}'] and len(op['operands']) == (4 if reset else 2),
            'Unsupported DMA register shape')
    require(set(op['attributes']) <= {'name', 'inner_sym', 'sv.namehint', 'firrtl.random_init_start'},
            'Unsupported DMA register attributes')
    require(op['operands'][1] == graph.named('clock'), 'Unsupported DMA register clock')
    if reset:
        require(op['operands'][2] == graph.named('reset') and
                constant(operation(graph, op['operands'][3], 'hw.constant')) == 0,
                'Unsupported DMA register reset')
    return op


def array_selection(module, root, index, entries):
    graph = Graph(module)
    get = operation(graph, root, 'hw.array_get')
    require(len(get['operands']) == 2, 'Unsupported DMA array get')
    direct(module, index, get['operands'][1], 'array index')
    create = operation(graph, get['operands'][0], 'hw.array_create')
    require(len(create['operands']) == len(entries), 'Unsupported DMA array size')
    paths = [direct(module, expected, actual, 'array entry ' + str(i))
             for i, (expected, actual) in enumerate(zip(entries, reversed(create['operands'])))]
    return {'selection': get, 'array': create, 'entries': paths}


def scalar_facts(module):
    graph = Graph(module)
    decoder, regfile, pc = (find_instance(module, n) for n in ('decoder', 'regfile', 'pc_ctrl'))
    require(decoder['instance']['module'] == 'ScalarDecoder' and
            regfile['instance']['module'] == 'ScalarRegFile' and pc['instance']['module'] == 'PcControl',
            'Unsupported scalar DMA bindings')
    dec = instance_value(decoder, 'io_decoded_dma_cmd', 'output')
    channel = instance_value(decoder, 'io_decoded_funct3', 'output')
    fire = graph.named('s1_fire')
    functions = [local(module, graph.named('io_dmaCmd_valid'),
                       [('s1_fire', fire, 1), ('decoded_dma_cmd', dec, 3)],
                       lambda f, op: f and op in (1, 2), 'DMA launch = scalar fire && decoded LD/ST'),
                 local(module, graph.named('is_dma_config'),
                       [('s1_fire', fire, 1), ('decoded_dma_cmd', dec, 3)],
                       lambda f, op: f and op == 3, 'DMA.CONFIG updates scalar state; only LD/ST launch DMA')]
    wait_root = graph.named('dma_wait_stall')
    # Identify exactly one selected channel-busy value inside the wait cone.
    candidates = [o for o in graph.cone(wait_root, {dec: 'opcode',
                   instance_value(pc, 'io_s1_valid', 'output'): 'valid'})['operations']
                  if o['kind'] == 'hw.array_get']
    require(len(candidates) == 1, 'Ambiguous DMA wait channel selection')
    selected = candidates[0]['results'][0]
    selection = array_selection(module, selected, channel, [graph.named(f'io_dma_busy_{i}') for i in range(8)])
    functions.append(local(module, wait_root,
                     [('s1_valid', instance_value(pc, 'io_s1_valid', 'output'), 1),
                      ('decoded_dma_cmd', dec, 3), ('selected_channel_busy', selected, 1)],
                     lambda valid, op, busy: valid and op == 4 and busy,
                     'DMA.WAIT stalls exactly for the selected busy channel'))
    base = register(module, 'dmaBaseReg', 32)
    outer = operation(graph, base['operands'][0], 'comb.mux')
    require(len(outer['operands']) == 3, 'Unsupported DMA base update')
    direct(module, graph.named('hostStart'), outer['operands'][0], 'host start resets DMA base')
    require(constant(operation(graph, outer['operands'][1], 'hw.constant')) == 0, 'DMA base restart is nonzero')
    inner = operation(graph, outer['operands'][2], 'comb.mux')
    require(len(inner['operands']) == 3, 'Unsupported DMA config update')
    config_paths = [direct(module, source, target, label) for source, target, label in (
        (graph.named('is_dma_config'), inner['operands'][0], 'config enable'),
        (instance_value(regfile, 'io_rs1_data', 'output'), inner['operands'][1], 'config captures rs1'),
        (graph.named('dmaBaseReg'), inner['operands'][2], 'base held without config'))]
    return {'functions': functions, 'wait_channel_selection': selection, 'base_register': base,
            'base_restart_mux': outer, 'base_config_mux': inner, 'base_paths': config_paths,
            'scope': 'Decoded-opcode cutpoints are source-correlated; raw instruction decoding and scalar register-file values are not proved here.'}


def completion_slot(module, slot):
    graph = Graph(module)
    zero = 'willBeZero' + ('_' + str(slot) if slot else '')
    cuts = named_cuts(graph, (f'slotAFireOH_{slot}', f'slotDFireOH_{slot}', f'slotOutstanding_{slot}'), (1, 1, 9))
    function = local(module, graph.named(zero), cuts,
                     lambda inc, dec, count: False if inc and not dec else count == (1 if dec and not inc else 0),
                     f'slot {slot}: zero after accepted request/response deltas')
    active = register(module, f'slotActive_{slot}', 1)
    active_function = local(module, active['operands'][0],
                           named_cuts(graph, ('io_command_valid', 'enqueueIdx', f'slotActive_{slot}',
                                             f'slotDispatched_{slot}', zero), (1, 3, 1, 1, 1)),
                           lambda valid, index, active, dispatched, zero:
                               not (active and dispatched and zero) and (active or (valid and index == slot)),
                           f'slot {slot}: retire only after all requests dispatched and outstanding zero; retirement has priority over enqueue')
    captures = []
    for field, width in (('opType', 1), ('channelId', 3), ('vmemLineAddr', 16), ('dramAddress', 64), ('transferSize', 13)):
        name = f'commandQueue_{slot}_{field}'
        saved = register(module, name, width, reset=False)
        mux = operation(graph, saved['operands'][0], 'comb.mux')
        require(len(mux['operands']) == 3, 'Unsupported command capture')
        incoming = direct(module, graph.named('io_command_bits_' + field), mux['operands'][1], 'captured ' + field)
        held = direct(module, graph.named(name), mux['operands'][2], 'held ' + field)
        guard = local(module, mux['operands'][0], named_cuts(graph, ('io_command_valid', 'enqueueIdx'), (1, 3)),
                      lambda valid, index: valid and index == slot, f'slot {slot} captures {field} on command valid')
        captures.append({'field': field, 'register': saved, 'mux': mux, 'incoming': incoming, 'held': held, 'guard': guard})
    return {'slot': slot, 'zero_function': function, 'active_register': active,
            'active_function': active_function, 'command_captures': captures}


def vmem_bank(module, bank):
    graph = Graph(module)
    order = ('lsuScalarWrite', 'lsuVecWrite', 'lsuScalarRead', 'lsuVecRead', 'dmaWrite', 'dmaRead', 'tlWrite', 'tlRead')
    cuts, leaf_slices = [], []
    for index, name in enumerate(order):
        number = bank * 8 + index
        leaf = 'accessSel_leaf' + ('_' + str(number) if number else '') + '_valid'
        value = graph.named(leaf)
        leaf_slices.append(slice_fact(module, value, graph.named(name + 'BankOH'), bank, 1))
        cuts.append((name + '_selected_bank', value, 1))
    functions = []
    for dma_name, index in (('Read', 5), ('Write', 4)):
        functions.append(local(module, graph.named(f'bankDma{dma_name}Grant_{bank}'), cuts,
                         lambda *active, i=index: active[i] and not any(active[:i]),
                         f'bank {bank}: DMA {dma_name.lower()} grant iff requested and every higher-priority request absent'))
    # Check the selected read client: 0=none, 1=LSU scalar, 2=LSU vector,
    # 3=DMA, 4=TileLink. Payload/row selection and memory response are separate.
    client = register(module, f'r1_bankReadClient_{bank}', 3, reset=False)
    valid = register(module, f'r1_bankReadValid_{bank}', 1)
    clients = (0, 0, 1, 2, 0, 3, 0, 4)
    expected_client = lambda *active: next((clients[i] for i, value in enumerate(active) if value), 0)
    functions.append(local(module, client['operands'][0], cuts, expected_client,
                           f'bank {bank}: selected read client follows strict LSU-before-DMA-before-TileLink priority', 3))
    functions.append(local(module, valid['operands'][0], cuts, lambda *active: expected_client(*active) != 0,
                           f'bank {bank}: read response valid is registered from selected read'))
    return {'bank': bank, 'leaf_slices': leaf_slices, 'functions': functions,
            'read_client_register': client, 'read_valid_register': valid}


def response_backpressure(module):
    graph = Graph(module)
    adapter = find_instance(module, 'tlAdapter')
    require(adapter['instance']['module'] == 'TileLinkAdapter', 'Unsupported DMA adapter binding')
    write = graph.named('io_vmemWrite_valid')
    cone = graph.cone(write, {instance_value(adapter, 'io_response_valid', 'output'): 'response_valid'})
    gets = [o for o in cone['operations'] if o['kind'] == 'hw.array_get']
    require(len(gets) == 1, 'Ambiguous DMA response direction selection')
    selected = gets[0]['results'][0]
    selection = array_selection(module, selected, instance_value(adapter, 'io_response_bits_tag', 'output'),
                                [graph.named(f'sourceMeta_{i}_isLoad') for i in range(64)])
    functions = [local(module, write,
                       [('response_is_load', selected, 1),
                        ('response_valid', instance_value(adapter, 'io_response_valid', 'output'), 1)],
                       lambda load, valid: load and valid, 'Load response requests VMEM write'),
                 local(module, instance_value(adapter, 'io_response_ready', 'input'),
                       [('response_is_load', selected, 1), ('write_grant', graph.named('io_vmemWriteGrant'), 1)],
                       lambda load, grant: not load or grant,
                       'Load response retires only with VMEM write grant; store acknowledgement needs no VMEM grant')]
    return {'response_direction_selection': selection, 'functions': functions}


def wrapper(module):
    graph = Graph(module)
    scalar, dma, vmem = (find_instance(module, n) for n in ('scalar', 'dma', 'vmem'))
    require([o['instance']['module'] for o in (scalar, dma, vmem)] == ['ScalarCore', 'DmaEngine', 'Vmem'],
            'Unsupported DMA wrapper binding')
    wires = []
    for a, ap, b, bp in ((scalar, 'io_dmaCmd_valid', dma, 'io_command_valid'),
                         (scalar, 'io_dmaCmd_bits_channel', dma, 'io_command_bits_channelId'),
                         (scalar, 'io_dmaCmd_bits_addr', dma, 'io_command_bits_dramAddress'),
                         (dma, 'io_vmemRead_valid', vmem, 'io_dmaRead_valid'),
                         (dma, 'io_vmemWrite_valid', vmem, 'io_dmaWrite_valid'),
                         (vmem, 'io_dmaReadGrant', dma, 'io_vmemReadGrant'),
                         (vmem, 'io_dmaWriteGrant', dma, 'io_vmemWriteGrant')):
        wires.append(direct(module, instance_value(a, ap, 'output'), instance_value(b, bp, 'input'), ap + ' -> ' + bp))
    address = slice_fact(module, instance_value(dma, 'io_command_bits_vmemLineAddr', 'input'),
                         instance_value(scalar, 'io_dmaCmd_bits_vmemAddr', 'output'), 3, 16)
    size = slice_fact(module, instance_value(dma, 'io_command_bits_transferSize', 'input'),
                      instance_value(scalar, 'io_dmaCmd_bits_size', 'output'), 0, 13)
    busy = []
    for i in range(8):
        busy.append(local(module, instance_value(scalar, f'io_dma_busy_{i}', 'input'),
                          [('engine_busy', instance_value(dma, f'io_channelBusy_{i}', 'output'), 1),
                           ('launch_pending', graph.named(f'dmaLaunched_{i}'), 1)],
                          lambda engine, launch: engine or launch, f'channel {i}: scalar busy includes launch-pending latch'))
    return {'wires': wires, 'word_address_to_line': address, 'transfer_size_low_bits': size, 'busy_functions': busy}


def analyze(modules):
    dma = modules['DmaEngine']
    memories = [o for o in modules['Vmem']['operations'] if o['kind'] == 'seq.firmem']
    require(len(memories) == 6 and all(o['result_types'] == ['!seq.firmem<8192 x 256, mask 32>'] for o in memories),
            'Unsupported DMA/VMEM geometry')
    return {'status': 'typed_local_dma_functions_checked', 'scalar': scalar_facts(modules['ScalarCore']),
            'slots': [completion_slot(dma, i) for i in range(8)],
            'response_backpressure': response_backpressure(dma),
            'vmem_banks': [vmem_bank(modules['Vmem'], i) for i in range(6)],
            'vmem_memories': memories,
            'wrapper': wrapper(modules['AtlasCore']),
            'geometry': {'command_slots': 8, 'channels': 8, 'vmem_banks': 6,
                         'bank_line_address_bits': 13, 'line_bytes': 32,
                         'dma_vmem_word_address_mask': '0x7ffff', 'dma_transfer_size_bits': 13},
            'limitations': [
                'Local cutpoint functions and register structures only; no complete FSM reachability, fairness, DMA completion bound, or end-to-end proof.',
                'VMEM arbitration begins at per-bank one-hot request cutpoints; their address decode, memory payload/row selection, and response routing are not proved.',
                'Slot request/response cutpoints are not independently proved to correspond to unique TileLink source IDs; counters, full per-channel busy recurrence, store queue safety, and TileLink errors remain outside this proof.',
                'Commands are captured on valid without a checked ready/slot-free gate. Software must avoid live-slot overwrite and channel reuse until explicit completion.',
                'DMA.CONFIG scalar semantics and launch-captured addresses differ from the inherited simulator timing model; that model cannot certify these transformations.',
                'No fixed DMA latency, shorter numerical latency, or automatic DMA.WAIT insertion is introduced.',
                'Cached simulator source-to-binary lineage remains unverified.']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--s0-manifest', type=Path, required=True)
    parser.add_argument('--exporter', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    source, exporter, output = (checked_path(p) for p in (args.s0_manifest, args.exporter, args.output))
    s0 = json.loads(source.read_text())
    require(s0['status'] == 'complete' and s0['config'] == 'EE290SimConfig', 'Requires complete EE290SimConfig S0')
    hardware = verify_artifact(s0['artifacts']['hardware_ir'])
    require(not output.exists(), 'Output must be a new evidence directory')
    inputs = {'s0': artifact(source), 'exporter': artifact(exporter), 'hardware_ir': artifact(hardware),
              'driver': artifact(checked_path(__file__))}
    for name in ('rtlgraph_mxu1.py', 'rtlgraph_mxu1_capture.py', 'rtlgraph_mxu1_profile.py',
                 'rtlgraph_query.py', 'rtlgraph_s0.py', 'rtlgraph_vpu.py'):
        inputs[name] = artifact(checked_path(Path(__file__).with_name(name)))
    output.mkdir(parents=True)
    typed_path = output / 'typed.json'
    command = [str(exporter), str(hardware), 'ScalarCore', 'DmaEngine', 'Vmem', 'AtlasCore']
    with typed_path.open('w') as stream:
        subprocess.run(command, stdout=stream, check=True)
    typed = json.loads(typed_path.read_text())
    require(not typed['missing_modules'] and len(typed['modules']) == 4, 'Missing or ambiguous DMA modules')
    report = analyze({m['name']: m for m in typed['modules']})
    report.update(schema='atlas.rtlgraph.dma-local-functions.v1', config=s0['config'], inputs=inputs,
                  typed=artifact(typed_path), export_command=command)
    for record in inputs.values(): verify_artifact(record)
    path = output / 'dma.json'
    path.write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps({'status': report['status'], 'output': str(path)}))


if __name__ == '__main__': main()

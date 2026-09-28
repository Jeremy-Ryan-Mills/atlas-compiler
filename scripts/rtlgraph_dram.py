#!/usr/bin/env python3
"""Check DMA address formation and request progression in pinned typed CIRCT.

Complements the existing DMA facts without rewriting their cached evidence.
The checks establish local wiring/recurrences, not complete queue correctness,
TileLink liveness, or a latency bound.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess

from rtlgraph_dma import array_selection, direct, local, operation, register, slice_fact
from rtlgraph_dma_profile import checked_facts
from rtlgraph_mxu1 import Graph
from rtlgraph_mxu1_capture import verify_artifact
from rtlgraph_mxu1_profile import constant, require
from rtlgraph_query import find_instance, instance_value
from rtlgraph_s0 import artifact


def shape(graph, value, kind, width, operands):
    op = operation(graph, value, kind)
    require(op['result_types'] == [f'i{width}'] and len(op['operands']) == operands,
            'Unsupported DRAM ' + kind + ' shape')
    return op


def literal(graph, value, width, expected):
    op = shape(graph, value, 'hw.constant', width, 0)
    require(constant(op) == expected, 'Unexpected DRAM constant')
    return op


def scalar_address(module):
    graph = Graph(module)
    decoder, regfile = (find_instance(module, name) for name in ('decoder', 'regfile'))
    require(decoder['instance']['module'] == 'ScalarDecoder' and
            regfile['instance']['module'] == 'ScalarRegFile', 'Unsupported DRAM scalar instances')
    output = shape(graph, graph.named('io_dmaCmd_bits_addr'), 'comb.concat', 64, 2)
    high = direct(module, graph.named('dmaBaseReg'), output['operands'][0], 'upper address is CONFIG base')
    low = shape(graph, output['operands'][1], 'comb.mux', 32, 3)
    load = local(module, low['operands'][0],
                 [('decoded_dma_cmd', instance_value(decoder, 'io_decoded_dma_cmd', 'output'), 3)],
                 lambda command: command == 1, 'LOAD selects rs1 low address; STORE selects rd')
    paths = [direct(module, instance_value(regfile, port, 'output'), value, label)
             for port, value, label in (
                 ('io_rs1_data', low['operands'][1], 'LOAD low address is rs1'),
                 ('io_rd_data', low['operands'][2], 'STORE low address is rd'))]
    paths.append(direct(module, instance_value(regfile, 'io_rs2_data', 'output'),
                        graph.named('io_dmaCmd_bits_size'), 'command byte count is rs2'))
    # The existing DMA scalar facts separately check reset/hostStart priority
    # and CONFIG capture. Width matters for concat rather than signed addition.
    base = register(module, 'dmaBaseReg', 32)
    return dict(output_concat=output, high=high, low_mux=low, load_selector=load,
                operand_paths=paths, base_register=base,
                law='command = (uint64(CONFIG_base32) << 32) | uint32(LOAD_rs1_or_STORE_rd)')


def engine_address(module):
    graph = Graph(module)
    adapter = find_instance(module, 'tlAdapter')
    require(adapter['instance']['module'] == 'TileLinkAdapter', 'Unsupported DRAM adapter instance')
    address = shape(graph, instance_value(adapter, 'io_request_bits_address', 'input'), 'comb.add', 64, 2)
    selection = array_selection(module, address['operands'][0], graph.named('requestIdx'),
                                [graph.named(f'commandQueue_{i}_dramAddress') for i in range(8)])
    offset = shape(graph, address['operands'][1], 'comb.concat', 64, 3)
    zeros = [literal(graph, offset['operands'][0], 50, 0), literal(graph, offset['operands'][2], 5, 0)]
    count = register(module, 'requestBeatCount', 9)
    count_path = direct(module, graph.named('requestBeatCount'), offset['operands'][1], 'beat offset is count * 32')
    output = direct(module, instance_value(adapter, 'io_tl_a_bits_address', 'output'),
                    graph.named('io_tl_a_bits_address'), 'adapter address reaches DMA TileLink output')
    handshake = [direct(module, source, target, label) for source, target, label in (
        (graph.named('io_tl_a_ready'), instance_value(adapter, 'io_tl_a_ready', 'input'), 'DMA TileLink ready reaches adapter'),
        (instance_value(adapter, 'io_tl_a_valid', 'output'), graph.named('io_tl_a_valid'), 'adapter TileLink valid reaches DMA output'))]
    size = literal(graph, graph.named('io_tl_a_bits_size'), 4, 5)
    return dict(address_add=address, saved_address_selection=selection, beat_offset_concat=offset,
                zero_padding=zeros, beat_counter=count, count_path=count_path, output_path=output,
                handshake_paths=handshake, tilelink_size=size,
                law='request_address = (captured_command_address + 32 * requestBeatCount) mod 2^64')


def request_progression(module):
    """Check the control recurrence supporting contiguous admitted byte ranges.

Starting with count zero, each accepted request advances count, except the
last request resets count while advancing the slot. A stable captured command
with 1..128 beats therefore emits indices 0..beats-1. Slot stability remains a
compiler admission obligation; complete queue/response invariants are separate.
"""
    graph = Graph(module)
    adapter = find_instance(module, 'tlAdapter')
    valid = shape(graph, instance_value(adapter, 'io_request_valid', 'input'), 'comb.and', 1, 4)
    active_selection = array_selection(module, valid['operands'][0], graph.named('requestIdx'),
                                       [graph.named(f'slotActive_{i}') for i in range(8)])
    not_dispatched = shape(graph, valid['operands'][1], 'comb.xor', 1, 2)
    literal(graph, not_dispatched['operands'][1], 1, 1)
    dispatched_selection = array_selection(module, not_dispatched['operands'][0], graph.named('requestIdx'),
                                           [graph.named(f'slotDispatched_{i}') for i in range(8)])
    valid_guard = local(module, valid['results'][0],
                        [('selected_slot_active', valid['operands'][0], 1),
                         ('selected_slot_dispatched', not_dispatched['operands'][0], 1),
                         ('capacity_gate', valid['operands'][2], 1),
                         ('store_data_gate', valid['operands'][3], 1)],
                        lambda active, dispatched, capacity, data: active and not dispatched and capacity and data,
                        'Requests require an active, not-yet-dispatched selected slot')
    count = register(module, 'requestBeatCount', 9)
    index = register(module, 'requestIdx', 3)
    needed = shape(graph, graph.named('requestBeatsNeeded'), 'comb.concat', 13, 2)
    zero_high = literal(graph, needed['operands'][0], 5, 0)
    size_slice = shape(graph, needed['operands'][1], 'comb.extract', 8, 1)
    require(size_slice['attributes'].get('lowBit') == '5 : i32', 'Wrong DRAM transfer beat slice')
    size_select = array_selection(module, size_slice['operands'][0], graph.named('requestIdx'),
                                  [graph.named(f'commandQueue_{i}_transferSize') for i in range(8)])

    last = shape(graph, graph.named('isLastBeat'), 'comb.icmp', 1, 2)
    require(last['attributes'].get('predicate') == '0 : i64', 'Wrong DRAM last-beat predicate')
    count_ext = shape(graph, last['operands'][0], 'comb.concat', 13, 2)
    count_zero = literal(graph, count_ext['operands'][0], 4, 0)
    count_path = direct(module, graph.named('requestBeatCount'), count_ext['operands'][1], 'last-beat count')
    limit = shape(graph, last['operands'][1], 'comb.add', 13, 2)
    limit_path = direct(module, graph.named('requestBeatsNeeded'), limit['operands'][0], 'last-beat count limit')
    minus_one = literal(graph, limit['operands'][1], 13, (1 << 13) - 1)

    update = shape(graph, count['operands'][0], 'comb.mux', 9, 3)
    accepted = local(module, update['operands'][0],
                     [('request_ready', instance_value(adapter, 'io_request_ready', 'output'), 1),
                      ('request_valid', instance_value(adapter, 'io_request_valid', 'input'), 1)],
                     lambda ready, valid: ready and valid, 'beat counter changes only on accepted adapter request')
    count_hold = direct(module, graph.named('requestBeatCount'), update['operands'][2], 'beat count holds without acceptance')
    next_count = shape(graph, update['operands'][1], 'comb.mux', 9, 3)
    last_path = direct(module, graph.named('isLastBeat'), next_count['operands'][0], 'last beat resets count')
    reset_zero = literal(graph, next_count['operands'][1], 9, 0)
    increment = shape(graph, next_count['operands'][2], 'comb.add', 9, 2)
    increment_path = direct(module, graph.named('requestBeatCount'), increment['operands'][0], 'accepted nonfinal beat increments count')
    increment_one = literal(graph, increment['operands'][1], 9, 1)

    next_index = shape(graph, index['operands'][0], 'comb.mux', 3, 3)
    advance = local(module, next_index['operands'][0],
                    [('request_ready', instance_value(adapter, 'io_request_ready', 'output'), 1),
                     ('request_valid', instance_value(adapter, 'io_request_valid', 'input'), 1),
                     ('last_beat', graph.named('isLastBeat'), 1)],
                    lambda ready, valid, last: ready and valid and last,
                    'request slot advances exactly on accepted last beat')
    index_hold = direct(module, graph.named('requestIdx'), next_index['operands'][2], 'slot holds before accepted last beat')
    index_inc = shape(graph, next_index['operands'][1], 'comb.add', 3, 2)
    index_path = direct(module, graph.named('requestIdx'), index_inc['operands'][0], 'next request slot increments modulo eight')
    index_one = literal(graph, index_inc['operands'][1], 3, 1)
    return dict(beat_count_register=count, slot_index_register=index, beats_needed=needed,
                size_slice=size_slice, size_selection=size_select, last_beat=last,
                count_extension=count_ext, limit=limit, update=update, next_count=next_count,
                increment=increment, next_index=next_index, index_increment=index_inc,
                constants=[zero_high, count_zero, minus_one, reset_zero, increment_one, index_one],
                paths=[count_path, limit_path, count_hold, last_path, increment_path, index_hold, index_path],
                active_selection=active_selection, dispatched_selection=dispatched_selection,
                functions=[valid_guard, accepted, advance],
                conditional_invariant='From reset count zero, stable admitted slot payload (positive size divisible by 32, <=4096) emits only indices 0..size/32-1; accepted final beat resets count and advances request slot.',
                assumptions=['No overwrite of a live command slot', 'Captured payload remains stable during dispatch',
                             'Decoded command size satisfies compiler admission', 'Reset starts the queue recurrence'])


def adapter_address(module):
    graph = Graph(module)
    output = shape(graph, graph.named('io_tl_a_bits_address'), 'comb.extract', 37, 1)
    require(output['attributes'].get('lowBit') == '0 : i32', 'Wrong DRAM bus truncation')
    aligned = shape(graph, output['operands'][0], 'comb.concat', 71, 3)
    high = literal(graph, aligned['operands'][0], 7, 0)
    low = literal(graph, aligned['operands'][2], 5, 0)
    source = slice_fact(module, aligned['operands'][1], graph.named('io_request_bits_address'), 5, 59)
    size = literal(graph, graph.named('io_tl_a_bits_size'), 4, 5)
    handshake = [local(module, graph.named('io_request_ready'),
                       [('tilelink_ready', graph.named('io_tl_a_ready'), 1), ('tag_free', graph.named('tagIsFree'), 1)],
                       lambda ready, free: ready and free, 'Adapter ready = TileLink ready && tag free'),
                 local(module, graph.named('io_tl_a_valid'),
                       [('request_valid', graph.named('io_request_valid'), 1), ('tag_free', graph.named('tagIsFree'), 1)],
                       lambda valid, free: valid and free, 'TileLink valid = adapter request valid && tag free')]
    return dict(output_slice=output, aligned_concat=aligned, constants=[high, low], source_slice=source,
                tilelink_size=size, handshake_functions=handshake,
                handshake_law='request_valid && request_ready == tilelink_valid && tilelink_ready',
                law='tilelink_address = request_address & ((1 << 37) - 32)')


def wrapper_address(module):
    graph = Graph(module)
    dma = find_instance(module, 'dma')
    require(dma['instance']['module'] == 'DmaEngine', 'Unsupported DRAM wrapper instance')
    paths = [direct(module, source, target, label) for source, target, label in (
        (instance_value(dma, 'io_tl_a_bits_address', 'output'), graph.named('io_dmaTL_a_bits_address'), 'DMA address reaches AtlasCore output'),
        (instance_value(dma, 'io_tl_a_valid', 'output'), graph.named('io_dmaTL_a_valid'), 'DMA valid reaches AtlasCore output'),
        (graph.named('io_dmaTL_a_ready'), instance_value(dma, 'io_tl_a_ready', 'input'), 'AtlasCore ready reaches DMA input'))]
    size = literal(graph, graph.named('io_dmaTL_a_bits_size'), 4, 5)
    return dict(paths=paths, tilelink_size=size,
                scope='AtlasCore external DMA TileLink interface; legal external-memory mapping remains an environment assumption.')


def analyze(modules):
    require(set(modules) == {'ScalarCore', 'DmaEngine', 'TileLinkAdapter', 'AtlasCore'}, 'Wrong DRAM module set')
    return dict(status='typed_local_dram_address_functions_checked',
                scalar=scalar_address(modules['ScalarCore']), engine=engine_address(modules['DmaEngine']),
                progression=request_progression(modules['DmaEngine']), adapter=adapter_address(modules['TileLinkAdapter']),
                wrapper=wrapper_address(modules['AtlasCore']),
                geometry=dict(base_bits=32, low_address_bits=32, command_address_bits=64,
                              tilelink_address_bits=37, alignment_bytes=32, beat_count_bits=9),
                limitations=['Address formation and request recurrence are structural/local facts, not whole-DMA formal verification.',
                             'The conditional range invariant assumes admitted positive aligned size and stable captured command slots.',
                             'No new proof of response metadata, complete queue/channel invariants, payload correctness, liveness or latency.',
                             'Truncation establishes possible physical aliases; it does not prove a requested address is a legal external-memory region.'])


def modules_from(typed, adapter):
    require(not typed['missing_modules'] and not adapter['missing_modules'] and
            len(typed['modules']) == 4 and len(adapter['modules']) == 1,
            'Missing or ambiguous DRAM typed modules')
    original = {module['name']: module for module in typed['modules']}
    require(set(original) == {'ScalarCore', 'DmaEngine', 'Vmem', 'AtlasCore'} and
            adapter['modules'][0]['name'] == 'TileLinkAdapter', 'Wrong DRAM typed module identities')
    return {name: original[name] for name in ('ScalarCore', 'DmaEngine', 'AtlasCore')} | {'TileLinkAdapter': adapter['modules'][0]}


def checked_address_facts(report, typed, adapter):
    require(report.get('schema') == 'atlas.rtlgraph.dram-address-functions.v1' and
            report.get('config') == 'EE290SimConfig', 'Wrong DRAM evidence schema or configuration')
    fresh = analyze(modules_from(typed, adapter))
    require(all(report.get(key) == value for key, value in fresh.items()),
            'Recorded DRAM evidence differs from fresh typed analysis')
    return fresh


def prepare(evidence_path, output):
    report = json.loads(evidence_path.read_text())
    for item in report['inputs'].values(): verify_artifact(item)
    typed_path = verify_artifact(report['typed'])
    typed = json.loads(typed_path.read_text())
    checked_facts(report, typed)
    hardware, exporter = (verify_artifact(report['inputs'][key]) for key in ('hardware_ir', 'exporter'))
    s0 = json.loads(verify_artifact(report['inputs']['s0']).read_text())
    require(s0['status'] == 'complete' and s0['config'] == 'EE290SimConfig' and
            s0['artifacts']['hardware_ir']['sha256'] == report['inputs']['hardware_ir']['sha256'],
            'DRAM evidence and elaboration lineage differ')
    output.mkdir(parents=True, exist_ok=False)
    adapter_path = output / 'adapter-typed.json'
    command = [str(exporter), str(hardware), 'TileLinkAdapter']
    with adapter_path.open('w') as stream:
        subprocess.run(command, stdout=stream, check=True)
    facts = analyze(modules_from(typed, json.loads(adapter_path.read_text())))
    inputs = dict(dma_evidence=artifact(evidence_path), typed=artifact(typed_path),
                  hardware_ir=report['inputs']['hardware_ir'], exporter=report['inputs']['exporter'],
                  s0=report['inputs']['s0'], driver=artifact(Path(__file__).resolve()))
    for name in ('rtlgraph_dma.py', 'rtlgraph_dma_profile.py', 'rtlgraph_mxu1.py', 'rtlgraph_mxu1_capture.py',
                 'rtlgraph_mxu1_profile.py', 'rtlgraph_query.py', 'rtlgraph_s0.py', 'rtlgraph_vpu.py'):
        inputs[name] = artifact(Path(__file__).with_name(name).resolve())
    facts.update(schema='atlas.rtlgraph.dram-address-functions.v1', config='EE290SimConfig',
                 inputs=inputs, adapter_typed=artifact(adapter_path), export_command=command)
    for item in inputs.values(): verify_artifact(item)
    path = output / 'dram.json'
    path.write_text(json.dumps(facts, indent=2) + '\n')
    return dict(status=facts['status'], output=str(path))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dma-evidence', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(prepare(args.dma_evidence.resolve(), args.output.resolve())))


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, KeyError) as error:
        raise SystemExit(f'rtlgraph_dram: {error}')

#!/usr/bin/env python3
"""Build a source-bound XLU/MREG/LSU Verilator witness from pinned CIRCT."""
import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
from rtlgraph_xlu import require, sha


def extract(text, names):
    start = text.index('module {\n') + len('module {\n')
    header = text[start:text.index('  hw.module', start)]
    pieces = [header]
    for name in names:
        matches = list(re.finditer(r'^  hw.module(?: private)? @' + re.escape(name) + r'\(', text, re.M))
        require(len(matches) == 1, f'Expected one {name} module')
        begin = matches[0].start()
        end = re.search(r'^  }(?: loc\(#loc\d+\))?\n', text[begin:], re.M)
        require(end is not None, f'Module boundary missing: {name}')
        pieces.append(text[begin:begin + end.end()].replace('hw.module private @', 'hw.module @', 1))
    return 'module {\n' + re.sub(r' loc\(#loc\d+\)', '', ''.join(pieces)) + '}\n'


def wrapper(modules):
    text = ['module XluConnected(input clock, reset, cmd, store_cmd, init_write, contend,',
            ' input [5:0] src, dst, init_id, input [4:0] init_row, input [255:0] init_data,',
            ' output busy, read_valid, response_valid, write_valid, store_read_valid, store_write_valid, collision,',
            ' output [4:0] write_row, output [255:0] write_data, store_data);']
    outputs = {}
    for unit, module_name in [('xlu', 'XluEngine'), ('mreg', 'MregFile'), ('lsu', 'LSU')]:
        for p in modules[module_name]['ports']:
            if p['direction'] == 'output':
                width = int(p['type'][1:])
                signal = unit + '_' + p['name']
                outputs[unit, p['name']] = signal
                text.append(f' wire [{width-1}:0] {signal};')
    for unit, module_name in [('xlu', 'XluEngine'), ('mreg', 'MregFile'), ('lsu', 'LSU')]:
        pins = []
        for p in modules[module_name]['ports']:
            name = p['name']
            if p['direction'] == 'output':
                value = outputs[unit, name]
            elif name in ('clock', 'reset'):
                value = name
            else:
                value = "'0"
                if unit == 'xlu':
                    value = {'io_cmd_valid': 'cmd', 'io_cmd_bits_srcMregId': 'src', 'io_cmd_bits_dstMregId': 'dst'}.get(name, value)
                    if name.startswith('io_mregReadResp_'):
                        value = outputs['mreg', name.replace('io_mreg', 'io_xlu')]
                elif unit == 'lsu':
                    value = {'io_cmd_valid': 'store_cmd', 'io_cmd_bits_op': "2'd2", 'io_cmd_bits_mregBank': 'dst'}.get(name, value)
                    if name.startswith('io_mregReadResp_'):
                        value = outputs['mreg', name.replace('io_mreg', 'io_lsu')]
                elif unit == 'mreg':
                    for engine in ['xlu', 'lsu']:
                        if name.startswith('io_' + engine):
                            value = outputs[engine, name.replace('io_' + engine, 'io_mreg')]
                    value = {'io_mxu0WriteReq0_valid': 'init_write', 'io_mxu0WriteReq0_bits_mregId': 'init_id',
                             'io_mxu0WriteReq0_bits_row': 'init_row', 'io_mxu0WriteReq0_bits_data': 'init_data',
                             'io_mxu0ReadReq0_valid': 'contend', 'io_mxu0ReadReq0_bits_mregId': 'src'}.get(name, value)
            pins.append(f'.{name}({value})')
        text.append(f' {module_name} {unit} (' + ', '.join(pins) + ');')
    probes = {'busy': ('xlu', 'io_busy'), 'read_valid': ('xlu', 'io_mregReadReq_valid'),
              'response_valid': ('mreg', 'io_xluReadResp_valid'), 'write_valid': ('xlu', 'io_mregWriteReq_valid'),
              'write_row': ('xlu', 'io_mregWriteReq_bits_row'), 'write_data': ('xlu', 'io_mregWriteReq_bits_data'),
              'store_read_valid': ('lsu', 'io_mregReadReq_valid'),
              'store_write_valid': ('lsu', 'io_vmemVecWrite_valid'), 'store_data': ('lsu', 'io_vmemVecWrite_bits_data')}
    text.extend(f' assign {name} = {outputs[pin]};' for name, pin in probes.items())
    text.append(' assign collision = lsu_io_mregReadReq_valid && xlu_io_mregWriteReq_valid && '
                '(lsu_io_mregReadReq_bits_mregId == xlu_io_mregWriteReq_bits_mregId) && '
                '(lsu_io_mregReadReq_bits_row == xlu_io_mregWriteReq_bits_row);')
    return '\n'.join(text + ['endmodule', ''])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--hardware-ir', type=Path, required=True)
    parser.add_argument('--query', type=Path, required=True)
    parser.add_argument('--circt-opt', type=Path, required=True)
    parser.add_argument('--verilator', type=Path, required=True)
    parser.add_argument('--cxx', type=Path, default=shutil.which('g++'))
    parser.add_argument('--ar', type=Path, default=shutil.which('ar'))
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    out = args.output.resolve()
    out.mkdir(parents=True, exist_ok=True)
    names = ['XluEngine', 'MregFile', 'LSU']
    typed = subprocess.check_output([str(args.query.resolve()), str(args.hardware_ir.resolve()), *names])
    modules = {m['name']: m for m in json.loads(typed)['modules']}
    (out / 'connected.hw.mlir').write_text(extract(args.hardware_ir.read_text(), names))
    (out / 'wrapper.sv').write_text(wrapper(modules))
    commands = [[str(args.circt_opt.resolve()), str(out / 'connected.hw.mlir'), '--lower-seq-to-sv', '--hw-memory-sim', '--export-verilog',
                 '-o', str(out / 'connected.lowered.mlir')],
                [str(args.verilator.resolve()), '--cc', '--exe', '--build', '-j', '2', '-Wno-fatal', '-DSYNTHESIS',
                 '-MAKEFLAGS', 'CXX=' + str(args.cxx.resolve()) + ' LINK=' + str(args.cxx.resolve()) + ' AR=' + str(args.ar.resolve()),
                 '--top-module', 'XluConnected', '--Mdir', str(out / 'obj'), str(out / 'connected.sv'), str(out / 'wrapper.sv'),
                 str(Path(__file__).resolve().parent / 'tests/xlu_connected_rtl_check.cpp')]]
    with (out / 'connected.sv').open('w') as sv, (out / 'lower.log').open('w') as log:
        subprocess.run(commands[0], stdout=sv, stderr=log, check=True)
    with (out / 'build.log').open('w') as log:
        subprocess.run(commands[1], stdout=log, stderr=subprocess.STDOUT, check=True)
    binary = out / 'obj/VXluConnected'
    result = json.loads(subprocess.check_output([str(binary)], text=True))
    files = {'hardware_ir': args.hardware_ir, 'query': args.query, 'circt_opt': args.circt_opt,
             'verilator': args.verilator, 'cxx': args.cxx, 'ar': args.ar, 'extractor': Path(__file__),
             'harness': Path(__file__).parent / 'tests/xlu_connected_rtl_check.cpp',
             'module_ir': out / 'connected.hw.mlir', 'module_sv': out / 'connected.sv', 'wrapper': out / 'wrapper.sv', 'executable': binary}
    manifest = {'schema': 'atlas.rtlgraph.xlu-connected-rtl.v1', 'status': 'passed', 'results': result,
                'artifacts': {name: {'path': str(path), 'sha256': sha(path)} for name, path in files.items()},
                'commands': commands, 'environment': {'VERILATOR_ROOT': os.environ.get('VERILATOR_ROOT')},
                'scope': 'Actual XLU, MREG, LSU modules; direct command injection and VMEM write observation.',
                'limitations': ['Finite numerical witnesses; no ScalarCore instruction decode or full-system VMEM/DMA execution.',
                                'SYNTHESIS disables embedded diagnostics so negative collision cases can complete.',
                                'Same-address read/write remains undefined in source CIRCT regardless of the simulator value.']}
    (out / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    print(json.dumps(result, sort_keys=True))


if __name__ == '__main__':
    main()

#!/usr/bin/env python3
"""Checked baremetal/native spelling adapter for the Atlas performance corpus."""
from collections import Counter
import importlib.util
from pathlib import Path
import re
import subprocess


class KernelError(ValueError):
    pass


def require(condition, message):
    if not condition:
        raise KernelError(message)


def tokens(line):
    return re.split(r'[\s,]+', line.split('#', 1)[0].strip()) if line.split('#', 1)[0].strip() else []


def integer(value, maximum, prefix=''):
    if prefix:
        require(re.fullmatch(re.escape(prefix) + r'\d+', value.lower()), f'expected {prefix} register: {value}')
        value = value[len(prefix):]
    result = int(value, 0) if value.lower().startswith(('0x', '-0x')) else int(value)
    require(0 <= result <= maximum, f'operand outside 0..{maximum}: {value}')
    return result


def scalar(value):
    return f'x{integer(value, 31, "x")}'


def immediate(value, bits):
    value = int(value, 0) if value.lower().startswith(('0x', '-0x')) else int(value)
    require(-(1 << (bits - 1)) <= value < (1 << bits), 'scalar immediate out of range')
    return str(value & ((1 << bits) - 1))


OPS = {
    'VMAX.BF16': ('vmaximum.bf16', ('m', 'm', 'm')),
    'VMIN.BF16': ('vminimum.bf16', ('m', 'm', 'm')),
    **{name: (name.lower(), ('m', 'm', 'm')) for name in ('VMUL.BF16', 'VADD.BF16', 'VSUB.BF16')},
    **{name: (name.lower(), ('m', 'm')) for name in ('VMOV', 'VRECIP.BF16', 'VSQUARE.BF16', 'VTRPOSE.XLU')},
    **{name: (name.lower() + '.bf16', ('m', 'm')) for name in ('VSQRT', 'VTANH', 'VLOG2', 'VEXP')},
    **{f'VRED{op}{axis}.BF16': (f'vred{op.lower()}{axis.lower()}.bf16', ('m', 'm'))
       for op in ('MAX', 'MIN', 'SUM') for axis in ('', '.ROW')},
}
for engine in (0, 1):
    for bare, native, kinds in (
        ('VMATPUSH.W', 'vmatpush.weight', ('w', 'm')),
        ('VMATMUL', 'vmatmul', ('acc', 'm', 'w')),
        ('VMATMUL.ACC', 'vmatmul.acc', ('acc', 'm', 'w')),
        ('VMATPOP.BF16', 'vmatpop.bf16.acc', ('m', 'acc')),
        ('VMATPUSH.ACC.BF16', 'vmatpush.acc.bf16', ('acc', 'm')),
    ):
        OPS[f'{bare}.MXU{engine}'] = (f'{native}.mxu{engine}', kinds)
REG_ALU = {'ADD', 'SUB', 'SLL', 'SLT', 'SLTU', 'XOR', 'SRL', 'SRA', 'OR', 'AND'}
IMM_ALU = {'ADDI', 'SLTI', 'SLTIU', 'XORI', 'ORI', 'ANDI', 'SLLI', 'SRLI', 'SRAI'}
CSR = {'CSRRW', 'CSRRS', 'CSRRC'}
BRANCH = {'BEQ', 'BNE', 'BLT', 'BGE', 'BLTU', 'BGEU'}
MEMORY = {'VLOAD', 'VSTORE', 'LB', 'LBU', 'LH', 'LHU', 'LW', 'LDU', 'SB', 'SH', 'SW'}


def translate(source, *, to_compiler):
    out = []
    for line in source.splitlines():
        p = tokens(line)
        release = '#' in line and 'atlas.release' in line.split('#', 1)[1].split()
        if not p:
            require(not release, 'atlas.release must annotate a CSR instruction')
            continue
        op, csr_address = p[0].upper(), None
        require(not release or op in CSR | {'CSRR', 'CSRW'}, 'atlas.release must annotate a CSR instruction')
        if re.fullmatch(r'[A-Za-z_]\w*:', p[0]):
            require(len(p) == 1, 'label must occupy its own line')
            out.append(p[0])
            continue
        if op.startswith('DMA.'):
            if to_compiler:
                require(op in {'DMA.LOAD', 'DMA.STORE', 'DMA.CONFIG', 'DMA.WAIT'}, 'unsupported DMA instruction')
                require(len(p) == {'DMA.LOAD': 5, 'DMA.STORE': 5, 'DMA.CONFIG': 3, 'DMA.WAIT': 2}[op], 'DMA operand count')
                channel, args = integer(p[-1], 7), [scalar(x) for x in p[1:-1]]
                result = f'{op.lower()}.ch{channel}' + (' ' + ', '.join(args) if args else '')
            else:
                match = re.fullmatch(r'(DMA\.(?:LOAD|STORE|CONFIG|WAIT))\.CH([0-7])', op)
                require(match, 'unsupported native DMA instruction')
                op, channel = match.groups()
                require(len(p) == {'DMA.LOAD': 4, 'DMA.STORE': 4, 'DMA.CONFIG': 2, 'DMA.WAIT': 1}[op], 'DMA operand count')
                result = op + ' ' + ', '.join([*(scalar(x) for x in p[1:]), channel])
        elif op in CSR | {'CSRR', 'CSRW'}:
            if to_compiler:
                if op == 'CSRR':
                    require(len(p) == 3, 'CSRR operand count')
                    p, op = ['CSRRS', p[1], p[2], 'x0'], 'CSRRS'
                elif op == 'CSRW':
                    require(len(p) == 3, 'CSRW operand count')
                    p, op = ['CSRRW', 'x0', p[2], p[1]], 'CSRRW'
                require(len(p) == 4, 'CSR operand count')
                csr_address = integer(p[2], 4095)
                result = f'{op.lower()} {scalar(p[1])}, {scalar(p[3])}, {csr_address}'
            else:
                require(op in CSR and len(p) == 4, 'native CSR operand count')
                csr_address = integer(p[3], 4095)
                result = f'{op} {scalar(p[1])}, {csr_address}, {scalar(p[2])}'
        elif op in REG_ALU | IMM_ALU | {'LI', 'LUI'}:
            require(len(p) == (3 if op in {'LI', 'LUI'} else 4), 'scalar operand count')
            args = [scalar(p[1])]
            if op in REG_ALU | IMM_ALU:
                args.append(scalar(p[2]))
            args.append(scalar(p[-1]) if op in REG_ALU else immediate(p[-1], 32 if op == 'LI' else 20 if op == 'LUI' else 12))
            result = (op.lower() if to_compiler else op) + ' ' + ', '.join(args)
            if op == 'ADDI' and args == ['x0', 'x0', '0']:
                result = 'nop' if to_compiler else 'NOP'
        elif op in BRANCH | {'JAL'}:
            require(len(p) == (3 if op == 'JAL' else 4), 'branch operand count')
            require(re.fullmatch(r'[A-Za-z_]\w*', p[-1]), 'branch target must be a label')
            result = (op.lower() if to_compiler else op) + ' ' + ', '.join([*(scalar(x) for x in p[1:-1]), p[-1]])
        elif op in MEMORY:
            kind = 'm' if op.startswith('V') else 'x'
            if to_compiler:
                require(len(p) == 4, 'memory operand count')
                rd = integer(p[1], 63 if kind == 'm' else 31, '' if kind == 'm' else 'x')
                result = f'{op.lower()} {kind}{rd}, {integer(p[3], 4095)}({scalar(p[2])})'
            else:
                require(len(p) == 3, 'native memory operand count')
                match = re.fullmatch(r'(\d+)\((x\d+)\)', p[2])
                require(match, 'unsupported memory operand')
                rd = integer(p[1], 63 if kind == 'm' else 31, kind)
                result = f'{op} {"" if kind == "m" else "x"}{rd}, {scalar(match[2])}, {integer(match[1], 4095)}'
        elif op in {'NOP', 'ECALL', 'FENCE', 'DELAY', 'SELI', 'VLI.ALL'}:
            expected = 2 if op == 'DELAY' else 3 if op in {'SELI', 'VLI.ALL'} else 1
            require(len(p) == expected, f'{op} operand count')
            args = []
            if op in {'SELI', 'VLI.ALL'}:
                kind, maximum = ('e', 31) if op == 'SELI' else ('m', 63)
                reg = integer(p[1], maximum, '' if to_compiler else kind)
                args.append((kind if to_compiler else '') + str(reg))
            if expected > 1:
                args.append(str(integer(p[-1], 65535 if op == 'VLI.ALL' else 4095)))
            result = (op.lower() if to_compiler else op) + (' ' + ', '.join(args) if args else '')
        else:
            fp8 = re.fullmatch(r'VMATPOP\.FP8(?:\.ACC)?\.MXU([01])', op)
            if fp8:
                require(len(p) == 4, 'FP8 pop operand count')
                specs = ('m', 'e', 'acc') if to_compiler else ('m', 'acc', 'e')
                vals = [integer(x, {'m': 63, 'e': 31, 'acc': 1}[k], '' if to_compiler else k) for x, k in zip(p[1:], specs)]
                vals[1], vals[2] = vals[2], vals[1]
                dest = ('m', 'acc', 'e') if to_compiler else ('', '', '')
                name = f'vmatpop.fp8.acc.mxu{fp8[1]}' if to_compiler else f'VMATPOP.FP8.MXU{fp8[1]}'
            else:
                match = next(((bare, native, kinds) for bare, (native, kinds) in OPS.items() if op == (bare if to_compiler else native.upper())), None)
                require(match, f'unsupported instruction: {line}')
                bare, native, specs = match
                require(len(p) == len(specs) + 1, f'{op} operand count')
                vals = [integer(x, {'m': 63, 'e': 31, 'acc': 1, 'w': 1}[k], '' if to_compiler else k) for x, k in zip(p[1:], specs)]
                dest, name = (specs if to_compiler else [''] * len(specs)), native if to_compiler else bare
            result = name + ' ' + ', '.join(k + str(v) for k, v in zip(dest, vals))
        if release or csr_address == 0xc10:
            result += ' # atlas.release'
        if op == 'DELAY' and '#' in line and 'keep' in line.split('#', 1)[1]:
            result += ' # keep'
        out.append(result)
    require(out, 'empty kernel')
    return '\n'.join(out) + '\n'


def load_assembler(path):
    spec = importlib.util.spec_from_file_location('atlas_original_assembler', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def assemble(assembler, source):
    words = assembler.assemble(source)
    require(isinstance(words, list) and all(type(x) is int and 0 <= x <= 0xffffffff for x in words), 'invalid assembly encoding')
    return words


def straight_line_prefix(source, assembler):
    """Drop only unreachable text and address-free labels for fixed-host replay."""
    words = assemble(assembler, source)
    require(0x73 in words, 'missing terminal ECALL')
    prefix = words[:words.index(0x73) + 1]
    require(not any(word & 0x7f in (0x63, 0x6f) or (word & 0x7f == 0x67 and word & 0xfffff != 0x1067) for word in prefix),
            'controlled prefix must not contain branches or jumps')
    lines = []
    for line in source.splitlines():
        fields = tokens(line)
        if not fields or not fields[0].endswith(':'):
            lines.append(line)
        if fields and fields[0].upper() == 'ECALL':
            break
    result = '\n'.join(lines) + '\n'
    require(assemble(assembler, result) == prefix, 'normalizing controlled prefix changed encoding')
    return result


def relocate_markers(source):
    """Remove a private counter expression, preserving all functional operations."""
    translate(source, to_compiler=True)
    lines = [line.strip() for line in source.splitlines() if tokens(line)]
    require(not any(tokens(line)[0].upper() in BRANCH | {'JAL'} for line in lines), 'cannot relocate counters across branches')
    reads = [i for i, line in enumerate(lines) if tokens(line)[0].upper() in {'CSRR', 'CSRRS'} and int(tokens(line)[2], 0) == 0xc00]
    require(len(reads) == 2, 'marker relocation needs exactly two cycle reads')
    begin, end = reads
    for index in reads:
        fields = tokens(lines[index])
        require((fields[0].upper() == 'CSRR' and len(fields) == 3) or (fields[0].upper() == 'CSRRS' and len(fields) == 4 and scalar(fields[3]) == 'x0'), 'cycle marker must be read-only')
    require(end + 2 < len(lines), 'truncated cycle expression')
    a, b = [integer(tokens(lines[pos])[1], 31, 'x') for pos in reads]
    sub = tokens(lines[end + 1])
    require(len(sub) == 4 and sub[0].upper() == 'SUB' and sub[2:] == [f'x{b}', f'x{a}'], 'unsupported cycle subtraction')
    elapsed = integer(sub[1], 31, 'x')
    require(len({a, b, elapsed}) == 3 and 0 not in {a, b, elapsed}, 'counter registers must be distinct and nonzero')
    require(translate(lines[end + 2], to_compiler=True).strip() == f'csrrw x0, x{elapsed}, 3089', 'counter must write only DBG1')
    selected = {begin, end, end + 1, end + 2}
    for index in selected:
        require('atlas.release' not in lines[index], 'cannot relocate a publication marker')
    body = [line for i, line in enumerate(lines) if i not in selected]
    private = {a, b, elapsed}
    for line in body:
        canonical = translate(line, to_compiler=True)
        fields = tokens(canonical)
        require(not (fields[0].upper() in CSR and int(fields[3], 0) in (0xc00, 0xc11)), 'additional benchmark CSR access')
        require(not {int(x) for x in re.findall(r'\bx(\d+)\b', translate(line, to_compiler=True))} & private, 'counter register used by functional code')
    return '\n'.join(body) + '\n', (lines[begin], *lines[end:end + 3])


def functional_words(assembler, source, *, omit_waits=False):
    result, ordinary = Counter(), []
    for line in source.splitlines():
        fields = tokens(line)
        if not fields or fields[0].upper() in {'DELAY', 'NOP'} | ({'DMA.WAIT'} if omit_waits else set()):
            continue
        if fields[0].upper() in BRANCH | {'JAL'}:
            result[tuple(tokens(translate(line, to_compiler=True)))] += 1
        else:
            ordinary.append(line)
    result.update(assemble(assembler, '\n'.join(ordinary)))
    return result


def prepare(source_path, assembler_path, compiler_path, output, *, profiles, priority='critical', insert_waits=False, relocate=True, validation='dynamic'):
    require(validation in ('dynamic', 'static'), 'unknown validation mode')
    require(priority in ('critical', 'input'), 'unknown scheduler priority')
    source, assembler = Path(source_path).read_text(), load_assembler(assembler_path)
    native = translate(source, to_compiler=True)
    require(assemble(assembler, source) == assemble(assembler, translate(native, to_compiler=False)), 'source encoding roundtrip mismatch')
    body, markers = relocate_markers(source) if relocate else (source, None)
    if insert_waits:
        body = '\n'.join(line for line in body.splitlines() if not tokens(line) or tokens(line)[0].upper() != 'DMA.WAIT') + '\n'
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    before, after = output / 'before.S', output / 'after.S'
    before.write_text(translate(body, to_compiler=True))
    flags = {'mxu0': '--experimental-mxu0-profile', 'mxu1': '--experimental-mxu1-profile', **{role: f'--rtl-{role}-profile' for role in ('dma', 'lsu', 'xlu', 'vpu')}}
    passes = 'strip-artifacts,' + ('insert-dma-waits,' if insert_waits else '') + 'schedule'
    command = [str(Path(compiler_path).resolve()), str(before), '-o', str(after), '--passes', passes]
    for role, profile in profiles.items():
        command += [flags[role], str(Path(profile).resolve())]
    if priority == 'input':
        command += ['--schedule-priority', 'input']
    if validation == 'static':
        command += ['--validation', 'static']
    run = subprocess.run(command, text=True, capture_output=True)
    (output / 'compiler.log').write_text(run.stdout + run.stderr)
    require(run.returncode == 0, f'compiler rejected input; see {output / "compiler.log"}')
    candidate = translate(after.read_text(), to_compiler=False)
    if markers:
        lines = candidate.splitlines()
        waits = [i for i, line in enumerate(lines) if tokens(line)[:1] == ['DMA.WAIT']]
        require(waits, 'missing final output wait')
        boundary = waits[-1]
        candidate = '\n'.join([markers[0], *lines[:boundary], *markers[1:], *lines[boundary:]]) + '\n'
    require(functional_words(assembler, source, omit_waits=insert_waits) == functional_words(assembler, candidate, omit_waits=insert_waits), 'schedule changed non-idle functional instruction encodings')
    final = output / 'final-check.S'
    final.write_text(translate(candidate, to_compiler=True))
    check_command = [str(Path(compiler_path).resolve()), str(final), '--check'] + command[command.index('--passes') + 2:]
    checked = subprocess.run(check_command, text=True, capture_output=True)
    (output / 'final-check.log').write_text(checked.stdout + checked.stderr)
    require(checked.returncode == 0, f'final schedule failed native hazard check; see {output / "final-check.log"}')
    directives = '\n'.join(line for line in source.splitlines() if re.match(r'^\s*#\s*@', line))
    path = output / 'candidate.S'
    path.write_text(directives + '\n' + candidate)
    return {'path': path, 'command': command, 'native_input': before, 'native_output': after, 'relocated_markers': bool(markers), 'validation': validation, 'priority': priority,
            'counter_scope': 'Diagnostic CSR bracket ends before final wait; compare trace completion instead.' if markers else 'Original counter positions preserved.',
            'original_waits': sum(tokens(l)[:1] == ['DMA.WAIT'] for l in source.splitlines()),
            'inserted_waits': sum(tokens(l)[:1] == ['DMA.WAIT'] for l in candidate.splitlines())}

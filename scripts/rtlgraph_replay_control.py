#!/usr/bin/env python3
"""Keep replay host code/layout fixed while changing only Atlas IMEM contents.

Padding is copied after the program's terminal ECALL; it is not an executed delay.
The control fixes host programming work and ELF layout, not every possible memory
system effect of changed Atlas instruction contents or execution timing.
"""

from __future__ import annotations

import hashlib
import re
import struct


HOST_NAME = 'rtlgraph_control'
PROGRAM = re.compile(r'static (?:volatile )?const uint32_t atlas_program\[ATLAS_PROGRAM_LEN\] = \{\n(?P<words>(?:\s*0x[0-9A-Fa-f]{8},?\n)+)\};')


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def controlled_source(text: str, original_name: str, capacity: int) -> tuple[str, dict]:
    """Normalize only assembler-identification text, capacity, and instruction array."""
    if not isinstance(capacity, int) or not 1 <= capacity <= 1024:
        raise ValueError('Control capacity must be between 1 and 1024 words')
    matches = list(PROGRAM.finditer(text))
    lengths = re.findall(r'^#define ATLAS_PROGRAM_LEN ([0-9]+)$', text, re.M)
    if len(matches) != 1 or len(lengths) != 1:
        raise ValueError('Expected exactly one generated program array and length')
    match = matches[0]
    words = [int(word, 16) for word in re.findall(r'0x[0-9A-Fa-f]{8}', match['words'])]
    if len(words) != int(lengths[0]) or len(words) > capacity:
        raise ValueError('Program length differs from declaration or exceeds control capacity')
    if not words or words[-1] != 0x73 or 0x73 in words[:-1]:
        raise ValueError('Control requires exactly one terminal ECALL')
    # Atlas DELAY shares JALR's opcode with funct3=1 and zero rd/rs1.
    if any(word & 0x7f in (0x63, 0x6f) or (word & 0x7f == 0x67 and word & 0xfffff != 0x1067)
           for word in words):
        raise ValueError('Control currently requires a straight-line Atlas program')
    # No source instructions are inserted, removed, or changed.
    padded = words + [0] * (capacity - len(words))
    initializer = ''.join(f'    0x{word:08X},\n' for word in padded)
    result = text[:match.start('words')] + initializer + text[match.end('words'):]
    result = result.replace('static const uint32_t atlas_program[', 'static volatile const uint32_t atlas_program[')
    result = re.sub(r'^#define ATLAS_PROGRAM_LEN [0-9]+$', f'#define ATLAS_PROGRAM_LEN {capacity}', result, flags=re.M)
    old_comment = f'/* Auto-generated from {original_name} by assembler.py */'
    old_pass = f'*** PASSED *** ({original_name} — all DRAM checks passed)'
    if result.count(old_comment) != 1 or result.count(old_pass) != 1:
        raise ValueError('Control requires the expected full-golden generated host')
    result = result.replace(old_comment, f'/* Fixed host replay control from {HOST_NAME} */')
    result = result.replace(old_pass, f'*** PASSED *** ({HOST_NAME} — all DRAM checks passed)')
    masked = PROGRAM.sub('ATLAS_PROGRAM_CONTENTS', result)
    info = {'capacity_words': capacity, 'program_words': len(words), 'padding_words': capacity - len(words),
            'padding_word': 0, 'padding_scope': 'Unexecuted storage after the sole terminal ECALL; host writes and verifies the complete fixed-capacity array.',
            'host_name': HOST_NAME, 'host_source_without_program_sha256': digest(masked.encode()),
            'program_storage': 'volatile const: host reads instruction bytes from the array rather than constant-folding them',
            'program_sha256': digest(struct.pack(f'<{len(words)}I', *words)),
            'padded_program_sha256': digest(struct.pack(f'<{capacity}I', *padded))}
    return result, info


def source_program(text: str) -> bytes:
    matches = list(PROGRAM.finditer(text))
    if len(matches) != 1:
        raise ValueError('Expected exactly one controlled program array')
    words = [int(word, 16) for word in re.findall(r'0x[0-9A-Fa-f]{8}', matches[0]['words'])]
    return struct.pack(f'<{len(words)}I', *words)


def elf_program_region(data: bytes, capacity: int) -> dict:
    """Find a bounded, allocated atlas_program OBJECT in a little-endian RV64 ELF."""
    def unpack(fmt, offset):
        size = struct.calcsize(fmt)
        if offset < 0 or offset + size > len(data):
            raise ValueError('Truncated ELF structure')
        return struct.unpack_from(fmt, data, offset)

    def span(offset, size):
        if offset < 0 or size < 0 or offset + size > len(data):
            raise ValueError('ELF data outside file')
        return data[offset:offset + size]

    if span(0, 7) != b'\x7fELF\x02\x01\x01':
        raise ValueError('Control requires ELF64 little-endian version 1')
    header = unpack('<16sHHIQQQIHHHHHH', 0)
    _, kind, machine, version, _, phoff, shoff, _, ehsize, phsize, phnum, shsize, shnum, _ = header
    if (kind, machine, version, ehsize, phsize, shsize) != (2, 243, 1, 64, 56, 64) or not shnum or not phnum:
        raise ValueError('Unsupported RISC-V executable ELF layout')
    sections = [unpack('<IIQQQQIIQQ', shoff + i * shsize) for i in range(shnum)]
    found = []
    for section in sections:
        _, sec_type, _, _, sec_offset, sec_size, link, _, _, entry_size = section
        if sec_type != 2:  # SHT_SYMTAB
            continue
        if entry_size != 24 or sec_size % entry_size or not 0 <= link < len(sections):
            raise ValueError('Unsupported ELF symbol table')
        strings_sec = sections[link]
        if strings_sec[1] != 3:
            raise ValueError('ELF symbol strings are not SHT_STRTAB')
        strings = span(strings_sec[4], strings_sec[5])
        span(sec_offset, sec_size)
        for offset in range(sec_offset, sec_offset + sec_size, entry_size):
            name, info, _, index, value, size = unpack('<IBBHQQ', offset)
            if name >= len(strings) or b'\0' not in strings[name:]:
                raise ValueError('Invalid ELF symbol name')
            if strings[name:].split(b'\0', 1)[0] != b'atlas_program':
                continue
            if info & 15 != 1 or not 0 < index < len(sections) or size != capacity * 4:
                raise ValueError('Unexpected atlas_program symbol type, section, or size')
            target = sections[index]
            _, target_type, flags, address, file_offset, section_size, *_ = target
            if target_type != 1 or flags & 2 == 0 or flags & 4:
                raise ValueError('atlas_program must be allocated non-executable PROGBITS')
            relative = value - address
            if relative < 0 or relative + size > section_size:
                raise ValueError('atlas_program exceeds its ELF section')
            location = file_offset + relative
            span(location, size)
            found.append({'file_offset': location, 'virtual_address': value, 'bytes': size})
    if len(found) != 1:
        raise ValueError('Expected one atlas_program ELF symbol')
    region = found[0]
    # Ensure the symbol bytes actually reach the same address through a load segment.
    loads = []
    for i in range(phnum):
        ptype, _, offset, address, _, file_size, memory_size, _ = unpack('<IIQQQQQQ', phoff + i * phsize)
        if ptype == 1:
            span(offset, file_size)
            if file_size > memory_size:
                raise ValueError('Invalid ELF load segment extent')
            if (offset <= region['file_offset'] and region['file_offset'] + region['bytes'] <= offset + file_size
                    and address + region['file_offset'] - offset == region['virtual_address']):
                loads.append(i)
    if len(loads) != 1:
        raise ValueError('atlas_program must be covered by exactly one matching ELF load segment')
    return {**region, 'load_segment_index': loads[0]}


def inspect_binary(data: bytes, program: bytes, capacity: int) -> dict:
    region = elf_program_region(data, capacity)
    offset, size = region['file_offset'], region['bytes']
    if len(program) != size or data[offset:offset + size] != program:
        raise ValueError('ELF program contents differ from generated controlled source')
    return {**region, 'elf_without_program_sha256': digest(data[:offset] + bytes(size) + data[offset + size:])}


def patch_binary(base: bytes, base_program: bytes, program: bytes, capacity: int) -> tuple[bytes, dict]:
    original = inspect_binary(base, base_program, capacity)
    offset, size = original['file_offset'], original['bytes']
    if len(program) != size:
        raise ValueError('Replacement program does not match fixed capacity')
    patched = base[:offset] + program + base[offset + size:]
    info = inspect_binary(patched, program, capacity)
    if info != original:
        raise ValueError('ELF layout or bytes outside the program changed')
    return patched, info



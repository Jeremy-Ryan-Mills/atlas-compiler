#!/usr/bin/env python3
"""Control safety checks: actual ELF patch boundaries and fixture/code isolation."""

from pathlib import Path
import struct
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).absolute().parents[1]))
from rtlgraph_replay_control import audit_control, controlled_source, elf_program_region, inspect_binary, patch_binary, source_program
from rtlgraph_s0 import artifact


def source(name='original', words=(0x13, 0x73)):
    return (f'/* Auto-generated from {name} by assembler.py */\n'
            f'#define ATLAS_PROGRAM_LEN {len(words)}\n'
            'static const uint32_t atlas_program[ATLAS_PROGRAM_LEN] = {\n'
            + ''.join(f'    0x{word:08X},\n' for word in words) + '};\n'
            'static const unsigned checks[] = {123};\n'
            f'printf("*** PASSED *** ({name} — all DRAM checks passed)\\n");\n')


def elf(program):
    """Small actual ELF64 fixture, with one program object in one PT_LOAD."""
    data = bytearray(1024)
    ident = b'\x7fELF\x02\x01\x01' + bytes(9)
    struct.pack_into('<16sHHIQQQIHHHHHH', data, 0, ident, 2, 243, 1, 0, 64, 512, 0, 64, 56, 1, 64, 4, 0)
    struct.pack_into('<IIQQQQQQ', data, 64, 1, 4, 128, 0x80000000, 0x80000000, 128, 128, 8)
    data[160:160 + len(program)] = program
    # Sections: null; program rodata; symbols; strings.
    struct.pack_into('<IIQQQQIIQQ', data, 512 + 64, 0, 1, 2, 0x80000000, 128, 128, 0, 0, 8, 0)
    struct.pack_into('<IIQQQQIIQQ', data, 512 + 128, 0, 2, 0, 0, 256, 48, 3, 1, 8, 24)
    strings = b'\0atlas_program\0'
    struct.pack_into('<IIQQQQIIQQ', data, 512 + 192, 0, 3, 0, 0, 320, len(strings), 0, 0, 1, 0)
    struct.pack_into('<IBBHQQ', data, 280, 1, 1, 0, 1, 0x80000020, len(program))
    data[320:320 + len(strings)] = strings
    return bytes(data)


class ReplayControlTests(unittest.TestCase):
    def test_program_preserved_and_only_post_ecall_storage_padded(self):
        text, info = controlled_source(source(), 'original', 4)
        self.assertEqual(source_program(text), struct.pack('<4I', 0x13, 0x73, 0, 0))
        self.assertEqual((info['program_words'], info['padding_words']), (2, 2))
        self.assertIn('static volatile const', text)
        delay, _ = controlled_source(source(words=(0x02101067, 0x73)), 'original', 4)
        self.assertEqual(source_program(delay), struct.pack('<4I', 0x02101067, 0x73, 0, 0))
        other, other_info = controlled_source(source('candidate', (0x1013, 0x13, 0x73)), 'candidate', 4)
        self.assertEqual(info['host_source_without_program_sha256'], other_info['host_source_without_program_sha256'])
        self.assertNotEqual(info['padded_program_sha256'], other_info['padded_program_sha256'])

    def test_fixture_and_host_code_changes_are_visible(self):
        text, info = controlled_source(source(), 'original', 4)
        for modified in (source().replace('{123}', '{124}'), source() + 'unexpected_host_work();\n'):
            _, changed = controlled_source(modified, 'original', 4)
            self.assertNotEqual(info['host_source_without_program_sha256'], changed['host_source_without_program_sha256'])

    def test_unsupported_program_control_flow_and_capacity_rejected(self):
        for words, capacity in (((0x13,), 4), ((0x73, 0x13, 0x73), 4), ((0x13, 0x73), 1),
                                ((0x63, 0x73), 4), ((0x67, 0x73), 4), ((0x6f, 0x73), 4)):
            with self.subTest(words=words, capacity=capacity), self.assertRaises(ValueError):
                controlled_source(source(words=words), 'original', capacity)
        for capacity in (0, -1, 1025):
            with self.assertRaises(ValueError):
                controlled_source(source(), 'original', capacity)

    def test_malformed_generated_hosts_fail_closed(self):
        for text in (source().replace('LEN 2', 'LEN 3'), source() + source(),
                     source().replace('all DRAM checks passed', 'spot check'), source().replace('original', 'elsewhere')):
            with self.assertRaises(ValueError):
                controlled_source(text, 'original', 4)

    def test_patched_elf_bytes_identical_outside_symbol(self):
        a = struct.pack('<4I', 0x13, 0x73, 0, 0)
        b = struct.pack('<4I', 0x1013, 0x13, 0x73, 0)
        original = elf(a)
        patched, info = patch_binary(original, a, b, 4)
        self.assertEqual(info['file_offset'], 160)
        self.assertEqual(patched[:160], original[:160])
        self.assertEqual(patched[176:], original[176:])
        self.assertEqual(patched[160:176], b)
        self.assertEqual(inspect_binary(original, a, 4)['elf_without_program_sha256'], info['elf_without_program_sha256'])

    def test_wrong_program_bytes_or_extent_rejected(self):
        program = struct.pack('<4I', 0x13, 0x73, 0, 0)
        data = elf(program)
        for original, replacement, capacity in ((bytes(16), program, 4), (program, bytes(12), 4), (program, program, 3)):
            with self.assertRaises(ValueError):
                patch_binary(data, original, replacement, capacity)

    def test_control_audit_rechecks_sources_bytes_and_recorded_facts(self):
        self.assertIsNone(audit_control({}, []))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            raw, controlled, assembly, binary = [root / name for name in ('raw.c', 'controlled.c', 'original.S', 'program.elf')]
            raw.write_text(source())
            text, info = controlled_source(source(), 'original', 4)
            controlled.write_text(text)
            assembly.write_text('NOP\nECALL\n')
            binary.write_bytes(elf(source_program(text)))
            manifest = {'inputs': {'assembly': artifact(assembly)}, 'assembler_generated_c': artifact(raw),
                        'generated_c': artifact(controlled), 'binary': artifact(binary),
                        'replay_control': {'mode': 'fixed_capacity_template', **info,
                                           'elf_region': inspect_binary(binary.read_bytes(), source_program(text), 4)}}
            verified = audit_control(manifest, [0x13, 0x73])
            self.assertEqual(verified['template_binary_sha256'], artifact(binary)['sha256'])
            with self.assertRaisesRegex(ValueError, 'independently assembled'):
                audit_control(manifest, [0x1013, 0x73])
            manifest['replay_control']['host_name'] = 'untrusted_name'
            with self.assertRaisesRegex(ValueError, 'recorded control facts'):
                audit_control(manifest, [0x13, 0x73])
            manifest['replay_control']['host_name'] = info['host_name']
            changed = bytearray(binary.read_bytes())
            changed[160] = 0
            binary.write_bytes(changed)
            manifest['binary'] = artifact(binary)
            with self.assertRaisesRegex(ValueError, 'ELF program contents'):
                audit_control(manifest, [0x13, 0x73])

    def test_unsupported_and_malformed_elf_rejected(self):
        data = elf(bytes(16))
        cases = [data[:100], data[:1], data[:300]]
        # Unsupported target, bad symbol section, non-object, unallocated/executable section,
        # absent load coverage, wrong symbol size, and nonterminated symbol string.
        for offset, fmt, value in ((18, '<H', 62), (286, '<H', 999), (284, 'B', 2),
                                   (584, '<Q', 0), (584, '<Q', 6), (80, '<Q', 0),
                                   (296, '<Q', 128), (333, 'B', 1)):
            changed = bytearray(data)
            struct.pack_into(fmt, changed, offset, value)
            cases.append(bytes(changed))
        for changed in cases:
            with self.subTest(data=changed[:24]), self.assertRaises(ValueError):
                elf_program_region(changed, 4)


if __name__ == '__main__':
    unittest.main()

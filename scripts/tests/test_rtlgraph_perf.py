#!/usr/bin/env python3
import json
from pathlib import Path
import struct
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from rtlgraph_perf import SIGNALS, fixture_words, trace_metrics, validate_issued
from rtlgraph_perf_fixtures import generate, exact_bf16
from rtlgraph_replay_control import controlled_source, inspect_binary, patch_binary


def elf(program):
    data = bytearray(1024)
    ident = b'\x7fELF\x02\x01\x01' + bytes(9)
    struct.pack_into('<16sHHIQQQIHHHHHH', data, 0, ident, 2, 243, 1, 0, 64, 512, 0, 64, 56, 1, 64, 4, 0)
    struct.pack_into('<IIQQQQQQ', data, 64, 1, 4, 128, 0x80000000, 0x80000000, 128, 128, 8)
    data[160:160 + len(program)] = program
    struct.pack_into('<IIQQQQIIQQ', data, 576, 0, 1, 2, 0x80000000, 128, 128, 0, 0, 8, 0)
    struct.pack_into('<IIQQQQIIQQ', data, 640, 0, 2, 0, 0, 256, 48, 3, 1, 8, 24)
    strings = b'\0atlas_program\0'
    struct.pack_into('<IIQQQQIIQQ', data, 704, 0, 3, 0, 0, 320, len(strings), 0, 0, 1, 0)
    struct.pack_into('<IBBHQQ', data, 280, 1, 1, 0, 1, 0x80000020, len(program))
    data[320:320 + len(strings)] = strings
    return bytes(data)


class ReplayTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def trace(self, pending=False, reverse=False):
        names = list(SIGNALS)
        codes = {name: f'n{i}' for i, name in enumerate(names)}
        header = '$timescale 1ns $end\n'
        for name, (path, width) in SIGNALS.items():
            scopes = path.split('.')
            header += ''.join(f'$scope module {part} $end\n' for part in scopes[:-1])
            header += f'$var wire {width} {codes[name]} {scopes[-1]} $end\n'
            header += '$upscope $end\n' * (len(scopes) - 1)
        header += '$enddefinitions $end\n#0\n'
        state = {name: 0 for name in names}
        state.update(fire=1, instruction=0x13)
        value = lambda name, x: f'b{x:b} {codes[name]}\n'
        text = header + ''.join(value(name, x) for name, x in state.items())
        text += '#5\n' + value('clock', 1)
        text += '#10\n' + value('clock', 0) + value('csr_valid', 1) + value('csr_addr', 0xc10) + value('csr_op', 1) + value('csr_data', 1)
        if pending:
            text += value('dma_busy_0', 1)
        # Changes at the edge belong to the next sample, independent of line order.
        changes = [value('clock', 1), value('instruction', 0x73), value('csr_valid', 0)]
        text += '#15\n' + ''.join(changes[::-1] if reverse else changes)
        text += '#20\n' + value('clock', 0) + '#25\n' + value('clock', 1)
        output = self.path / 'trace.vcd'
        output.write_text(text)
        return output

    def test_issue_trace_binds_program_and_branch_coverage(self):
        program = struct.pack('<3I', 0x13, 0x1013, 0x73)
        events = [{'pc': 0, 'word': 0x13}, {'pc': 1, 'word': 0x1013}, {'pc': 0, 'word': 0x13}]
        self.assertEqual(validate_issued(events, program, {'expected_pc_visits': {'0': 2}})['issued_instructions'], 3)
        for changed, witness in ((events, {'expected_pc_visits': {'0': 3}}),
                                 ([{'pc': 1, 'word': 0x13}], {}), ([{'pc': 3, 'word': 0}], {})):
            with self.assertRaises(ValueError):
                validate_issued(changed, program, witness)

    def test_completion_uses_preedge_values(self):
        result = trace_metrics(self.trace())
        self.assertEqual(result['first_issue_to_completion_edges'], 1)
        self.assertEqual(result['first_issue_to_ecall_edges'], 2)
        self.assertEqual(result, trace_metrics(self.trace(reverse=True)))

    def test_pending_dma_cannot_be_success(self):
        with self.assertRaisesRegex(ValueError, 'DMA still pending'):
            trace_metrics(self.trace(pending=True))

    def test_complete_fixture_required(self):
        fixture = self.path / 'fixture.json'
        fixture.write_text(json.dumps({'dram_checks': [{'word_offset': 4, 'expected': '0x1'}]}))
        self.assertEqual(fixture_words(fixture), 8)
        fixture.write_text(json.dumps({'dram_checks': []}))
        with self.assertRaises(ValueError):
            fixture_words(fixture)

    def test_overlapping_fixture_entries_rejected(self):
        fixture = self.path / 'fixture.json'
        fixture.write_text(json.dumps({'dram_checks': [{'word_offset': 4, 'expected': '0x1'}] * 2}))
        with self.assertRaisesRegex(ValueError, 'duplicate'):
            fixture_words(fixture)

    def test_augmented_fixtures_cover_both_halves_with_word_addresses(self):
        assembly = self.path / 'assembly'
        assembly.mkdir()
        for name, count in (('perf_vpu_binary', 5), ('perf_vpu_reduction', 6)):
            (assembly / (name + '.S')).write_text(
                'LI x8, 0x20000000\nVSTORE 8, x8, 0\nDELAY 33\nLHU x10, x8, 0\n' * count +
                'pass:\nECALL\n')
        (assembly / 'perf_mm_single.S').write_text('LI x30, 1\nECALL\n')
        records = generate(self.path, self.path / 'fixtures')
        self.assertEqual([r['expected_words'] for r in records], [2560, 3072, 1024])
        for record in records:
            self.assertEqual(fixture_words(record['golden']), record['expected_words'])
        binary = Path(records[0]['assembly']).read_text()
        self.assertIn('LI x8, 536871424', binary)  # Second tile: 2048 bytes / 4.
        self.assertIn('LI x8, 536872960\n', binary)  # Scalar access: byte address.
        self.assertEqual(binary.count('VSTORE 9, x8, 8'), 5)
        self.assertIn('LI x6, 536871936', binary)  # Second DMA: 4096 bytes / 4.
        self.assertIn('LI x12, 2048', binary)

    def test_fixture_goldens_require_exact_bf16(self):
        self.assertEqual(exact_bf16(6), 0x40c0)
        self.assertEqual(exact_bf16(256), 0x4380)
        with self.assertRaisesRegex(ValueError, 'exact in BF16'):
            exact_bf16(1.00001)

    def test_elf_patch_preserves_every_other_byte(self):
        before = struct.pack('<4I', 0x13, 0x73, 0, 0)
        after = struct.pack('<4I', 0x1013, 0x13, 0x73, 0)
        original = elf(before)
        patched, region = patch_binary(original, before, after, 4)
        self.assertEqual(patched[:160], original[:160])
        self.assertEqual(patched[176:], original[176:])
        self.assertEqual(patched[160:176], after)
        self.assertEqual(region, inspect_binary(original, before, 4))
        with self.assertRaisesRegex(ValueError, 'contents differ'):
            patch_binary(original, after, before, 4)
        with self.assertRaises(ValueError):
            patch_binary(original, before, after, 3)

    def test_fixed_host_padding_follows_halt(self):
        text = ('/* Auto-generated from sample by assembler.py */\n#define ATLAS_PROGRAM_LEN 2\n'
                'static const uint32_t atlas_program[ATLAS_PROGRAM_LEN] = {\n    0x00000013,\n    0x00000073,\n};\n'
                '*** PASSED *** (sample — all DRAM checks passed)')
        output, info = controlled_source(text, 'sample', 4)
        self.assertEqual(info['padding_words'], 2)
        self.assertIn('0x00000073,\n    0x00000000,', output)
        with self.assertRaisesRegex(ValueError, 'terminal ECALL'):
            controlled_source(text.replace('0x00000073', '0x00000013'), 'sample', 4)


if __name__ == '__main__':
    unittest.main()

#!/usr/bin/env python3
"""Adversarial assembly and benchmark-boundary checks for native DMA scheduling."""
from pathlib import Path
import json
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from rtlgraph_dma_compile import active, extract_markers, insert_markers, prepare, same_operations, translate
from rtlgraph_kernel import KernelError
from rtlgraph_s0 import artifact
from test_rtlgraph_attention import TokenAssembler


SOURCE = '''LI x5, 0
DMA.CONFIG x5, 3
LI x12, 1024
LI x6, 0x20000000
LI x1, 0x90000000
DMA.LOAD x6, x1, x12, 7
DMA.WAIT 7
CSRRS x14, 0xC00, x0
VLOAD 0, x6, 0
DELAY 34
VSQUARE.BF16 2, 0
DELAY 66
VSTORE 2, x6, 0
DELAY 34
CSRRS x15, 0xC00, x0
SUB x16, x15, x14
CSRRW x0, 0xC11, x16
LI x1, 0x90000400
DMA.STORE x1, x6, x12, 7
DMA.WAIT 7
LI x30, 1
CSRW x30, 0xC10
ECALL
'''


class NativeDmaAdapterTests(unittest.TestCase):
    def test_full_stream_mxu_and_scale_operand_roles_roundtrip(self):
        source = 'SELI 7, 127\nVLOAD 2, x6, 0\n'
        for engine in (0, 1):
            source += (f'VMATPUSH.W.MXU{engine} 1, 2\nVMATMUL.MXU{engine} 0, 4, 1\n'
                       f'VMATMUL.ACC.MXU{engine} 0, 6, 1\nVMATPOP.FP8.MXU{engine} 30, 7, 0\n'
                       f'VMATPUSH.ACC.BF16.MXU{engine} 1, 2\nVMATPOP.BF16.MXU{engine} 4, 1\n')
        native = translate(source, to_compiler=True)
        self.assertIn('seli e7, 127\n', native)
        for engine in (0, 1):
            self.assertIn(f'vmatpop.fp8.acc.mxu{engine} m30, acc0, e7\n', native)
            self.assertIn(f'vmatpush.acc.bf16.mxu{engine} acc1, m2\n', native)
        restored = translate(native, to_compiler=False)
        self.assertEqual(restored, source)
        same_operations(source, restored, TokenAssembler())
        for changed in (restored.replace('SELI 7, 127', 'SELI 7, 126'),
                        restored.replace('VMATPOP.FP8.MXU1 30, 7, 0', 'VMATPOP.FP8.MXU1 30, 0, 7'),
                        restored.replace('VMATMUL.MXU1', 'VMATMUL.MXU0')):
            with self.subTest(changed=changed), self.assertRaisesRegex(KernelError, 'encoded non-idle'):
                same_operations(source, changed, TokenAssembler())

    def test_mxu_and_scale_invalid_operands_fail_closed(self):
        for instruction in ('SELI 32, 1', 'SELI 0, 4096', 'SELI 0, -1', 'SELI 0',
                            'VMATPUSH.ACC.BF16.MXU0 2, 0', 'VMATPOP.FP8.MXU1 0, 32, 1',
                            'VMATMUL.MXU0 0, 64, 0', 'VMATMUL.MXU2 0, 2, 0',
                            'VMATUNKNOWN.MXU0 1, 2'):
            with self.subTest(instruction=instruction), self.assertRaises(KernelError):
                translate(instruction, to_compiler=True)
        for instruction in ('seli x0, 1', 'seli e32, 1', 'seli e0, 4096'):
            with self.subTest(instruction=instruction), self.assertRaises(KernelError):
                translate(instruction, to_compiler=False)

    def test_optional_mxu_profiles_reach_both_scheduler_and_exact_final_check(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            files = {}
            for name in ('source', 'assembler', 'compiler', 'dma', 'mxu0', 'mxu1'):
                files[name] = root / name
                files[name].write_text(name + '\n')
            files['source'].write_text('SELI 7, 127\nVMATPUSH.ACC.BF16.MXU1 1, 2\n'
                                       'VMATPOP.FP8.MXU1 30, 7, 1\nECALL\n')

            def run(command, **_):
                if '-o' in command:
                    Path(command[command.index('-o') + 1]).write_text(Path(command[1]).read_text())
                return SimpleNamespace(returncode=0, stdout='', stderr='')

            for enabled in ((), (0,), (1,), (0, 1)):
                with self.subTest(enabled=enabled), patch('rtlgraph_dma_compile.load_assembler', return_value=TokenAssembler()), \
                        patch('rtlgraph_dma_compile.subprocess.run', side_effect=run) as called:
                    options = {f'mxu{engine}_profile': files[f'mxu{engine}'] for engine in enabled}
                    report = prepare(files['source'], files['assembler'], files['compiler'], files['dma'],
                                     root / ('out-' + ''.join(map(str, enabled))), **options)
                    expected = ['--rtl-dma-profile', str(files['dma'])]
                    for engine in enabled:
                        expected += [f'--experimental-mxu{engine}-profile', str(files[f'mxu{engine}'])]
                    self.assertEqual(called.call_count, 4)
                    for engine in (0, 1):
                        self.assertEqual(f'mxu{engine}_profile' in report['inputs'], engine in enabled)
                    for case in report['cases'].values():
                        command, check = case['command'], case['final_check_command']
                        self.assertEqual(command[command.index('--rtl-dma-profile'):-2], expected)
                        self.assertEqual(check[3:], expected)
                        self.assertTrue(case['unchanged_non_idle_words'])
                        self.assertEqual(case['final_check_returncode'], 0)

    def test_native_dma_uses_channel_mnemonic_and_preserves_operand_roles(self):
        text = translate(SOURCE, to_compiler=True)
        self.assertIn('dma.config.ch3 x5\n', text)
        self.assertIn('dma.load.ch7 x6, x1, x12\n', text)
        self.assertIn('dma.store.ch7 x1, x6, x12\n', text)
        self.assertEqual(text.count('dma.wait.ch7\n'), 2)
        self.assertIn('vload m0, 0(x6)', text)
        self.assertIn('csrrs x14, x0, 3072', text)
        self.assertIn('csrrw x0, x30, 3088', text)
        restored = translate(text, to_compiler=False)
        self.assertIn('DMA.LOAD x6, x1, x12, 7', restored)
        self.assertIn('DMA.CONFIG x5, 3', restored)

    def test_lsu_profile_is_checked_and_used_by_scheduler_and_final_check(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / 'source.S'
            source.write_text('VLOAD 0, x6, 0\nECALL\n')
            profiles = {}
            for role in ('dma', 'lsu'):
                directory = root / role
                directory.mkdir()
                evidence = directory / 'profile.json'
                evidence.write_text(json.dumps(dict(schema=f'atlas.rtlgraph.{role}-profile.v1',
                    config='EE290SimConfig', compiler_overrides={}, inputs=dict(hardware_ir=dict(sha256='a' * 64)))))
                profile = directory / f'atlas-{role}.profile'
                profile.write_text(f'schema=atlas-{role}-profile-v1\nconfig=EE290SimConfig\n'
                                   f'source_ir_sha256={"a" * 64}\nevidence_sha256={artifact(evidence)["sha256"]}\n')
                profiles[role] = profile

            def run(command, **_):
                if '-o' in command:
                    Path(command[command.index('-o') + 1]).write_text(Path(command[1]).read_text())
                return SimpleNamespace(returncode=0, stdout='', stderr='')

            with patch('rtlgraph_dma_compile.load_assembler', return_value=TokenAssembler()), \
                    patch('rtlgraph_dma_compile.subprocess.run', side_effect=run):
                report = prepare(source, source, source, profiles['dma'], root / 'output', lsu_profile=profiles['lsu'])
            self.assertEqual(report['inputs']['lsu_profile'], artifact(profiles['lsu']))
            for case in report['cases'].values():
                for field in ('command', 'final_check_command'):
                    command = case[field]
                    self.assertEqual(command[command.index('--rtl-lsu-profile') + 1], str(profiles['lsu']))
            profile = profiles['lsu']
            profile.write_text(profile.read_text() + 'vload_write_age=4\n')
            with self.assertRaisesRegex(ValueError, 'settings differ'):
                prepare(source, source, source, profiles['dma'], root / 'invalid', lsu_profile=profile)
            self.assertFalse((root / 'invalid').exists())

    def test_marker_relocation_is_independent_of_register_and_dma_layout(self):
        body, markers = extract_markers(SOURCE)
        self.assertEqual(markers.private_registers, (14, 15, 16))
        self.assertNotIn('0xC00', body)
        self.assertIn('CSRW x30, 0xC10', body)
        self.assertEqual(body.count('DMA.WAIT'), 2)
        candidate = insert_markers(body, markers)
        self.assertTrue(candidate.startswith('CSRRS x14, 0xC00, x0\n'))
        self.assertIn('CSRRW x0, 0xC11, x16\nDMA.WAIT 7', candidate)
        self.assertEqual(sorted(active(SOURCE)), sorted(active(candidate)))

    def test_counter_register_aliasing_functional_operands_rejected(self):
        for instruction in ('LI x14, 9', 'ADDI x7, x16, 0', 'VLOAD 0, x15, 0',
                            'DMA.LOAD x6, x14, x12, 2'):
            with self.subTest(instruction=instruction), self.assertRaisesRegex(KernelError, 'functional'):
                extract_markers(SOURCE.replace('LI x5, 0', instruction + '\nLI x5, 0'))

    def test_functional_counter_use_and_wrong_elapsed_expression_rejected(self):
        for source in (SOURCE.replace('SUB x16, x15, x14', 'SUB x16, x14, x15'),
                       SOURCE.replace('CSRRW x0, 0xC11, x16', 'CSRRW x3, 0xC11, x16'),
                       SOURCE.replace('CSRRS x14, 0xC00, x0', 'CSRRS x14, 0xC00, x5'),
                       SOURCE.replace('SUB x16', 'SUB x14'),
                       SOURCE.replace('LI x5, 0', 'CSRRW x0, 0xC11, x5\nLI x5, 0')):
            with self.subTest(source=source), self.assertRaises(KernelError):
                extract_markers(source)

    def test_control_flow_labels_and_unknown_ops_are_rejected(self):
        for instruction in ('JAL x0, 4', 'BEQ x1, x2, 3', 'label:', 'AUIPC x1, 1', 'UNKNOWN x2'):
            with self.subTest(instruction=instruction), self.assertRaises(KernelError):
                translate(instruction, to_compiler=True)

    def test_dma_channels_and_operand_counts_are_checked(self):
        for instruction in ('DMA.LOAD x1, x2, x3, 8', 'DMA.STORE x1, x2, 1',
                            'DMA.CONFIG x32, 0', 'DMA.WAIT 0, 1'):
            with self.subTest(instruction=instruction), self.assertRaises(KernelError):
                translate(instruction, to_compiler=True)

    def test_scalar_and_csr_immediates_roundtrip_in_compiler_order(self):
        text = translate('LUI x2, 524288\nADDI x2, x2, -2048\nCSRR x4, 0xc10\n', to_compiler=True)
        self.assertEqual(text, 'lui x2, 524288\naddi x2, x2, 2048\ncsrrs x4, x0, 3088 # atlas.release\n')
        self.assertEqual(translate(text, to_compiler=False),
                         'LUI x2, 524288\nADDI x2, x2, 2048\nCSRRS x4, 3088, x0 # atlas.release\n')

    def test_publication_and_explicit_release_metadata_survive_dialect_and_marker_changes(self):
        text = translate('CSRW x30, 0xC10\nCSRRW x0, 0xC12, x7 # atlas.release\n', to_compiler=True)
        self.assertEqual(text.count('# atlas.release'), 2)
        self.assertEqual(translate(text, to_compiler=False).count('# atlas.release'), 2)
        source = SOURCE.replace('LI x5, 0', 'CSRRW x0, 0xC12, x7 # atlas.release\nLI x5, 0')
        body, markers = extract_markers(source)
        self.assertIn('# atlas.release', insert_markers(body, markers))
        for source in ('LI x5, 0 # atlas.release', '# atlas.release\nLI x5, 0'):
            with self.subTest(source=source), self.assertRaisesRegex(KernelError, 'annotate a CSR'):
                translate(source, to_compiler=True)

    def test_kept_delay_survives_translation_and_marker_relocation(self):
        source = SOURCE.replace('DELAY 34', 'DELAY 34 # keep')
        native = translate(source, to_compiler=True)
        self.assertEqual(native.count('delay 34 # keep'), 2)
        self.assertEqual(translate(native, to_compiler=False).count('DELAY 34 # keep'), 2)
        body, markers = extract_markers(source)
        self.assertEqual(insert_markers(body, markers).count('DELAY 34 # keep'), 2)

    def test_release_markers_and_orphan_metadata_cannot_be_relocated(self):
        for source in (SOURCE.replace('CSRRS x14, 0xC00, x0', 'CSRRS x14, 0xC00, x0 # atlas.release'),
                       SOURCE.replace('CSRRS x15, 0xC00, x0', 'CSRRS x15, 0xC00, x0 # atlas.release'),
                       '# atlas.release\n' + SOURCE):
            with self.subTest(source=source), self.assertRaisesRegex(KernelError, 'atlas.release'):
                extract_markers(source)

    def test_final_wait_cannot_disappear_during_reinsertion(self):
        body, markers = extract_markers(SOURCE)
        with self.assertRaisesRegex(KernelError, 'final explicit DMA wait'):
            insert_markers('\n'.join(line for line in active(body) if not line.startswith('DMA.WAIT')), markers)


if __name__ == '__main__':
    unittest.main()

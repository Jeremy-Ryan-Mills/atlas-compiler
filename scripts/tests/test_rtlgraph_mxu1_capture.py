#!/usr/bin/env python3
"""Validate capture provenance and outcomes without launching VCS."""

from pathlib import Path
from contextlib import redirect_stdout
import io
import json
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).absolute().parents[1]))
from rtlgraph_mxu1_capture import (capture_command, capture_outcome, capture_tcl,
                                  conversion_environment, finalize_existing, relative_runtime_path, validate_smoke, verify_artifact)
from rtlgraph_s0 import artifact
from rtlgraph_smoke import MXU_TEST, PASS_MARKERS


class CaptureTests(unittest.TestCase):
    def test_vcs_flags_stay_inside_permissive_envelope(self):
        command = capture_command(*(Path('/tmp') / name for name in ('sim', 'test.riscv', 'ini', 'coverage.vdb', 'capture.tcl')))
        for flag in ('-no_save', '-ucli', '-i', '-k', '-cm_dir'):
            self.assertLess(command.index('+permissive'), command.index(flag))
            self.assertLess(command.index(flag), command.index('+permissive-off'))
        self.assertEqual(command[command.index('-k') + 1], 'off')

    def test_tcl_deduplicates_signals_and_closes_before_completion(self):
        text = capture_tcl({'a': ('Top.u.valid', 1), 'alias': ('Top.u.valid', 1)}, Path('/tmp/test.vcd'), Path('/tmp/done'))
        self.assertEqual(text.count('dump -add'), 1)
        self.assertLess(text.index('dump -close'), text.index('rtlgraph-capture-complete'))
        self.assertIn('-type VPD', text)

    def test_unsafe_tcl_names_and_paths_are_rejected(self):
        with self.assertRaises(ValueError):
            capture_tcl({'a': ('Top.u;quit', 1)}, Path('/tmp/test.vcd'), Path('/tmp/done'))
        with self.assertRaises(ValueError):
            capture_tcl({'a': ('Top.u', 1)}, Path('/tmp/{bad}.vcd'), Path('/tmp/done'))

    def test_artifact_mutation_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'input'
            path.write_text('original')
            info = artifact(path)
            path.write_text('modified')
            with self.assertRaisesRegex(ValueError, 'SHA-256 changed'):
                verify_artifact(info)

    def test_runtime_paths_cannot_escape_root(self):
        for value in ('../outside', '/absolute'):
            with self.assertRaises(ValueError):
                relative_runtime_path(value)

    def test_failed_smoke_manifest_is_not_eligible(self):
        record = {'schema_version': 1, 'kind': 'rtlgraph-functional-smoke', 'config': 'EE290SimConfig', 'status': 'failed'}
        with self.assertRaisesRegex(ValueError, 'must have passed'):
            validate_smoke(record)

    def test_functional_pass_requires_closed_nonempty_capture(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            trace, done = root / 'trace.vcd', root / 'done'
            result = {'returncode': 0, 'timed_out': False}
            log = f'DBG0 = 1\nstatus = 0x5\n{PASS_MARKERS[MXU_TEST]}'
            self.assertEqual(capture_outcome(result, log, trace, done), 'CAPTURE_NOT_CLOSED')
            done.write_text('rtlgraph-capture-complete\n')
            self.assertEqual(capture_outcome(result, log, trace, done), 'MISSING_TRACE')
            trace.write_text('$enddefinitions $end\n')
            self.assertEqual(capture_outcome(result, log, trace, done), 'CAPTURED')
            self.assertEqual(capture_outcome({**result, 'interrupted': True}, log, trace, done), 'INTERRUPTED')

    def test_normal_vcs_finish_closes_without_tcl_sentinel(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            trace = root / 'trace.vcd'
            trace.write_text('trace')
            log = f'DBG0 = 1\nstatus = 0x5\n{PASS_MARKERS[MXU_TEST]}\n$finish at simulation time 123\nV C S S i m u l a t i o n R e p o r t\nCPU Time: 10 seconds'
            result = {'returncode': 0, 'timed_out': False}
            self.assertEqual(capture_outcome(result, log, trace, root / 'absent'), 'CAPTURED')
            self.assertEqual(capture_outcome({**result, 'timed_out': True}, log, trace, root / 'absent'), 'TIMEOUT')
            self.assertEqual(capture_outcome(result, log.replace('CPU Time:', 'truncated'), trace, root / 'absent'), 'CAPTURE_NOT_CLOSED')

    def test_converter_uses_dispatcher_os_and_separate_64bit_mode(self):
        env = conversion_environment({'PATH': '/saved/bin', 'LD_LIBRARY_PATH': '/saved/lib', 'VCS_ARCH_OVERRIDE': 'linux64'}, Path('/tools/example'))
        self.assertEqual(env['PATH'], '/saved/bin')
        self.assertEqual(env['LD_LIBRARY_PATH'], '/saved/lib')
        self.assertEqual(env['VCS_ARCH_OVERRIDE'], 'linux')
        self.assertEqual(env['VCS_MODE_FLAG'], '64')

    def test_finalizer_preserves_history_and_never_reruns_simulator(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = {name: root / name for name in ('sim', 'elf', 'capture.tcl', 'wave.vpd', 'simulation.log')}
            for path in paths.values():
                path.write_text('input')
            paths['simulation.log'].write_text(f'DBG0 = 1\nstatus = 0x5\n{PASS_MARKERS[MXU_TEST]}\n$finish at simulation time 123\nV C S S i m u l a t i o n R e p o r t\nCPU Time: 10 seconds')
            command = [str(paths['sim']), '-i', str(paths['capture.tcl']), str(paths['elf'])]
            old_commands = [{'argv': command, 'returncode': 0, 'timed_out': False, 'log': artifact(paths['simulation.log'])}, {'argv': ['old-converter'], 'returncode': 1}]
            record = {'schema_version': 1, 'kind': 'rtlgraph-mxu1-capture', 'status': 'failed',
                      'commands': old_commands, 'prepared_command': {'argv': command},
                      'simulator': artifact(paths['sim']), 'binary': artifact(paths['elf']),
                      'capture_tcl': artifact(paths['capture.tcl']), 'vpd': artifact(paths['wave.vpd']),
                      'environment': {'PATH': '/saved/bin', 'LD_LIBRARY_PATH': '/saved/lib'},
                      'signals': {'clock': {'path': 'Top.clock', 'width': 1}}}
            manifest = root / 'manifest.json'
            manifest.write_text(json.dumps(record))
            original = manifest.read_bytes()
            converter = root / 'vcs/bin/vpd2vcd'
            for path in (converter, converter.parent / 'common.vpd', converter.parent.parent / 'linux64/bin/vpd2vcd.exe'):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('converter')

            def fake_conversion(argv, cwd, env, log, timeout):
                self.assertEqual(argv[0], str(converter))
                self.assertEqual(env['VCS_ARCH_OVERRIDE'], 'linux')
                Path(argv[-1]).write_text('$timescale 1ns $end\n$scope module Top $end\n$var wire 1 ! clock $end\n$upscope $end\n$enddefinitions $end\n#0\n0!\n#5\n1!\n')
                log.write_text('Version : TEST\n')
                return {'argv': argv, 'returncode': 0, 'timed_out': False, 'log': artifact(log)}

            with patch('rtlgraph_mxu1_capture.VPD2VCD', converter), patch('rtlgraph_mxu1_capture.run_command', side_effect=fake_conversion) as run, redirect_stdout(io.StringIO()):
                self.assertEqual(finalize_existing(manifest, 10), 0)
                self.assertEqual(run.call_count, 1)
            updated = json.loads(manifest.read_text())
            self.assertEqual(updated['status'], 'captured')
            self.assertEqual(updated['commands'][:2], old_commands)
            self.assertEqual(updated['source_status_before_finalization'], 'failed')
            self.assertEqual(updated['finalizations'][0]['trace_validation']['rising_edges'], 1)
            self.assertEqual((root / 'manifest-before-finalize-1.json').read_bytes(), original)

            paths['wave.vpd'].write_text('tampered')
            before = manifest.read_bytes()
            with patch('rtlgraph_mxu1_capture.run_command') as run:
                with self.assertRaisesRegex(ValueError, 'SHA-256 changed'):
                    finalize_existing(manifest, 10)
                run.assert_not_called()
            self.assertEqual(manifest.read_bytes(), before)


if __name__ == '__main__':
    unittest.main()

from pathlib import Path
import json
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from rtlgraph_corpus import assignments, classify_source, encoded_digest, report, stage_text
from rtlgraph_s0 import artifact


class CorpusTests(unittest.TestCase):
    def test_staging_adds_only_reporting_comment_and_is_idempotent(self):
        source = '# original\nADDI x1,x0,1\nECALL\n'
        staged = stage_text(source)
        self.assertEqual(staged, '# @PERF_REPORT\n' + source)
        self.assertEqual(stage_text(staged), staged)

    def test_comment_mentions_do_not_hide_missing_report_directive(self):
        source = '# this test needs @PERF_REPORT later\nECALL\n'
        self.assertTrue(stage_text(source).startswith('# @PERF_REPORT\n'))

    def test_golden_checked_case_counts_words(self):
        source = 'CSRR x1,0xC00\nCSRRS x2,0xC00,x0\n'
        case = classify_source(source, {'dram_checks': [{}, {}]})
        self.assertEqual(case['readiness'], 'golden_checked_baseline')
        self.assertEqual(case['expected_check_words'], 16)
        self.assertEqual(case['cycle_read_count'], 2)

    def test_empty_fixture_never_becomes_correctness_pass(self):
        case = classify_source('# @PERF_UTIL_THRESHOLD 5\n', {'dram_checks': []})
        self.assertEqual(case['readiness'], 'no_output_golden')
        self.assertEqual(case['utilization_threshold'], ['5'])

    def test_multiple_windows_require_special_handling(self):
        case = classify_source('CSRRS x1,0xC00,x0\n' * 14 + 'BNE x1,x2,fail\n', None)
        self.assertEqual(case['readiness'], 'self_check_multiple_windows')
        self.assertTrue(case['has_control_flow'])

    def test_word_digest_is_order_sensitive(self):
        self.assertNotEqual(encoded_digest([1, 2]), encoded_digest([2, 1]))

    def test_duplicate_and_malformed_assignment_rejected(self):
        for values in [['bad'], ['perf_one=/tmp/a', 'perf_one=/tmp/b']]:
            with self.assertRaises(ValueError):
                assignments(values)

    def test_reused_baseline_revalidated_in_report(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            assembler = root / 'assembler.py'
            assembler.write_text('def assemble(source): return []\n')
            replay = root / 'manifest.json'
            replay.write_text('{"status":"passed"}')
            case = {'readiness': 'golden_checked_baseline',
                    'reused_baseline': {'status': 'passed', 'manifest': artifact(replay)}}
            inventory = root / 'inventory.json'
            inventory.write_text(json.dumps({'assembler': artifact(assembler), 'cases': {'perf_one': case}}))
            output = root / 'report.json'
            with patch('rtlgraph_corpus.verify_replay', side_effect=ValueError('capture no longer matches')) as verify:
                report(inventory, {}, output)
            verify.assert_called_once()
            entry = json.loads(output.read_text())['cases']['perf_one']
            self.assertEqual(entry['status'], 'not_verified_pass')
            self.assertIn('capture no longer matches', entry['reason'])

    def test_malformed_replay_stays_visible_as_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            assembler = root / 'assembler.py'
            assembler.write_text('def assemble(source): return []\n')
            replay = root / 'manifest.json'
            replay.write_text('{"status":')
            inventory = root / 'inventory.json'
            inventory.write_text(json.dumps({'assembler': artifact(assembler),
                'cases': {'perf_one': {'readiness': 'golden_checked_baseline'}}}))
            output = root / 'report.json'
            report(inventory, {'perf_one': replay}, output)
            entry = json.loads(output.read_text())['cases']['perf_one']
            self.assertEqual(entry['status'], 'not_verified_pass')
            self.assertEqual(entry['reported_status'], 'unreadable_manifest')


if __name__ == '__main__':
    unittest.main()

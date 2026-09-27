#!/usr/bin/env python3
"""Typed fixture and adversarial mutations for selected LSU control facts."""
import copy
import json
from pathlib import Path
import subprocess
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from rtlgraph_lsu import analyze_lsu


class LsuLocalTests(unittest.TestCase):
    def test_fixture(self):
        report = analyze_lsu(MODULE)
        self.assertEqual(report['assignments_checked'], 78)
        self.assertEqual(len(report['registers']), 5)

    def test_wrong_reset(self):
        module = copy.deepcopy(MODULE)
        register = next(o for o in module['operations'] if o['attributes'].get('name') == 'vloadRespPending')
        register['operands'][2] = 'unknown-reset'
        with self.assertRaisesRegex(ValueError, 'clock/reset'): analyze_lsu(module)

    def test_enabled_register(self):
        module = copy.deepcopy(MODULE)
        register = next(o for o in module['operations'] if o['attributes'].get('name') == 'vloadRespPending')
        register['operands'].append('enable')
        with self.assertRaisesRegex(ValueError, 'control register'): analyze_lsu(module)

    def test_broken_pending(self):
        module = copy.deepcopy(MODULE)
        register = next(o for o in module['operations'] if o['attributes'].get('name') == 'vloadRespPending')
        register['operands'][0] = register['results'][0]
        with self.assertRaisesRegex(ValueError, 'Unsupported operation'): analyze_lsu(module)

    def test_wrong_guard(self):
        module = copy.deepcopy(MODULE)
        cmp = next(o for o in module['operations'] if o['kind'] == 'comb.icmp')
        cmp['attributes']['predicate'] = '1 : i64'
        with self.assertRaisesRegex(ValueError, 'function mismatch'): analyze_lsu(module)


if __name__ == '__main__':
    exporter = Path(sys.argv.pop(1)).resolve()
    result = subprocess.run([str(exporter), str(Path(__file__).with_name('rtlgraph-lsu.mlir')), 'LSU'],
                            text=True, capture_output=True, check=True)
    MODULE = json.loads(result.stdout)['modules'][0]
    unittest.main()

#!/usr/bin/env python3
"""Check LSU projection bounds and fail closed on stale or unsupported evidence."""
import copy
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from rtlgraph_lsu_profile import checked_facts, settings


def facts():
    events = {kind: dict(requests=[dict(age=row + 1, row=row) for row in range(32)],
                         writes=[dict(age=row + 3, row=row) for row in range(32)],
                         first_not_busy_age=35) for kind in ('vload', 'vstore')}
    return dict(status='typed_conditional_lsu_routing_derived', operand_capture='issue',
                lsu_timing=dict(temporal=dict(events=events)))


class LsuProjectionTests(unittest.TestCase):
    def test_row_profiles_have_distinct_access_and_release_ages(self):
        values = settings(facts())
        self.assertEqual(values['operand_capture'], 'issue')
        self.assertEqual(values['rows'], 32)
        for kind in ('vload', 'vstore'):
            self.assertEqual(values[f'{kind}_read_age'], 1)
            self.assertEqual(values[f'{kind}_write_age'], 3)
            self.assertEqual(values[f'{kind}_first_free_age'], 35)
        self.assertNotIn('same_cycle_visibility', values)

    def test_changed_events_change_exported_timing(self):
        report = facts()
        events = report['lsu_timing']['temporal']['events']['vload']
        for row in events['writes']:
            row['age'] += 1
        events['first_not_busy_age'] += 2
        values = settings(report)
        self.assertEqual(values['vload_write_age'], 4)
        self.assertEqual(values['vload_first_free_age'], 37)
        self.assertEqual(values['vstore_write_age'], 3)

    def test_missing_duplicate_or_reordered_rows_are_rejected(self):
        for event in ('requests', 'writes'):
            for mutation in ('missing', 'duplicate', 'reordered'):
                report = facts()
                rows = report['lsu_timing']['temporal']['events']['vload'][event]
                if mutation == 'missing':
                    rows.pop()
                elif mutation == 'duplicate':
                    rows[-1]['row'] = 30
                else:
                    rows[0], rows[1] = rows[1], rows[0]
                with self.subTest(event=event, mutation=mutation), self.assertRaisesRegex(ValueError, 'geometry or ordering'):
                    settings(report)

    def test_irregular_or_out_of_range_ages_are_rejected(self):
        for age in (-1, 0, 65, 1.0, True):
            report = facts()
            rows = report['lsu_timing']['temporal']['events']['vstore']['requests']
            for index, row in enumerate(rows):
                row['age'] = age + index if type(age) is not bool else age
            with self.subTest(age=age), self.assertRaisesRegex(ValueError, 'timing or stride'):
                settings(report)
        report = facts()
        report['lsu_timing']['temporal']['events']['vstore']['writes'][7]['age'] += 1
        with self.assertRaisesRegex(ValueError, 'timing or stride'):
            settings(report)

    def test_release_cannot_precede_last_write(self):
        for free in (34, 129, True):
            report = facts()
            report['lsu_timing']['temporal']['events']['vload']['first_not_busy_age'] = free
            with self.subTest(free=free), self.assertRaisesRegex(ValueError, 'drain timing'):
                settings(report)

    def test_changed_operand_capture_is_not_silently_inherited(self):
        report = facts()
        report['operand_capture'] = 'completion'
        with self.assertRaisesRegex(ValueError, 'operand capture'):
            settings(report)

    def test_status_is_not_a_substitute_for_typed_analysis(self):
        fresh = facts()
        report = dict(fresh, schema='atlas.rtlgraph.lsu-routing.v1', config='EE290SimConfig')
        typed = dict(modules=[dict(name=name) for name in ('LSU', 'AtlasCore', 'MregFile', 'Vmem')], missing_modules=[])
        with patch('rtlgraph_lsu_profile.analyze', return_value=fresh) as query:
            self.assertEqual(checked_facts(report, typed), fresh)
            query.assert_called_once()
            changed = copy.deepcopy(report)
            changed['lsu_timing']['temporal']['events']['vload']['first_not_busy_age'] = 36
            with self.assertRaisesRegex(ValueError, 'fresh typed analysis'):
                checked_facts(changed, typed)

    def test_wrong_configuration_and_ambiguous_modules_are_rejected(self):
        report = dict(facts(), schema='atlas.rtlgraph.lsu-routing.v1', config='AtlasDefaultConfig')
        with self.assertRaisesRegex(ValueError, 'configuration'):
            checked_facts(report, dict(modules=[], missing_modules=[]))
        report['config'] = 'EE290SimConfig'
        with self.assertRaisesRegex(ValueError, 'ambiguous'):
            checked_facts(report, dict(modules=[dict(name='LSU')] * 4, missing_modules=[]))


if __name__ == '__main__':
    unittest.main()

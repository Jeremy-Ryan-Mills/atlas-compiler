#!/usr/bin/env python3
"""Reject unsupported DMA projection geometry and stale cached evidence."""
import copy
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from rtlgraph_dma_profile import checked_facts, settings


FACTS = dict(status='typed_local_dma_functions_checked',
             geometry=dict(command_slots=8, channels=8, vmem_banks=6,
                           bank_line_address_bits=13, line_bytes=32,
                           dma_vmem_word_address_mask='0x7ffff', dma_transfer_size_bits=13),
             wrapper=dict(word_address_to_line=dict(low_bit=3, width=16),
                          transfer_size_low_bits=dict(low_bit=0, width=13)))


class DmaProjectionTests(unittest.TestCase):
    def test_projection_contains_no_numeric_dma_latency(self):
        values = settings(FACTS)
        self.assertEqual(values['completion'], 'explicit-wait')
        self.assertEqual(values['vmem_lines'], 49152)
        self.assertEqual(values['operand_capture'], 'issue')
        self.assertFalse(any('latency' in key for key in values))
        self.assertEqual(values['supported_max_transfer_bytes'], 4096)

    def test_altered_geometry_or_slice_cannot_silently_project(self):
        for group, field in (('geometry', 'command_slots'), ('geometry', 'vmem_banks')):
            facts = copy.deepcopy(FACTS)
            facts[group][field] += 1
            with self.assertRaisesRegex(ValueError, 'geometry'):
                settings(facts)
        facts = copy.deepcopy(FACTS)
        facts['wrapper']['word_address_to_line']['low_bit'] = 2
        with self.assertRaisesRegex(ValueError, 'projection'):
            settings(facts)

    def test_status_does_not_replace_recomputed_typed_facts(self):
        modules = [dict(name=name) for name in ('ScalarCore', 'DmaEngine', 'Vmem', 'AtlasCore')]
        typed = dict(modules=modules, missing_modules=[])
        report = dict(FACTS, schema='atlas.rtlgraph.dma-local-functions.v1', config='EE290SimConfig')
        with patch('rtlgraph_dma_profile.analyze', return_value=FACTS) as analyzer:
            self.assertEqual(checked_facts(report, typed), FACTS)
            analyzer.assert_called_once()
        altered = copy.deepcopy(report)
        altered['geometry']['channels'] = 7
        with patch('rtlgraph_dma_profile.analyze', return_value=FACTS), self.assertRaisesRegex(ValueError, 'fresh typed analysis'):
            checked_facts(altered, typed)

    def test_ambiguous_modules_and_wrong_target_rejected_before_projection(self):
        report = dict(FACTS, schema='atlas.rtlgraph.dma-local-functions.v1', config='EE290SimConfig')
        with self.assertRaisesRegex(ValueError, 'ambiguous'):
            checked_facts(report, dict(modules=[dict(name='ScalarCore')] * 4, missing_modules=[]))
        report['config'] = 'AtlasDefaultConfig'
        with self.assertRaisesRegex(ValueError, 'configuration'):
            checked_facts(report, dict(modules=[], missing_modules=[]))


if __name__ == '__main__':
    unittest.main()

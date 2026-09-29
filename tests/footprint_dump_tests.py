"""Exercise the native model export through its JSON CLI boundary."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


COMPILER = Path(sys.argv.pop(1)).resolve()


class FootprintDumpTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.source = self.root / 'before.S'
        self.output = self.root / 'footprints.json'

    def profile(self, schema, **fields):
        path = self.root / (schema + '.profile')
        values = dict(schema=schema, config='EE290SimConfig', source_ir_sha256='a' * 64,
                      evidence_sha256='b' * 64, **fields)
        path.write_text(''.join(f'{key}={value}\n' for key, value in values.items()))
        return str(path)

    def dma_profile(self):
        return self.profile('atlas-dma-profile-v2', operand_capture='issue', config_update='issue',
            vmem_word_address_low_bit=3, vmem_line_address_bits=16, transfer_size_bits=13,
            vmem_line_bytes=32, vmem_lines=49152, channels=8, command_slots=8,
            supported_max_transfer_bytes=4096, completion='explicit-wait', lsu_priority_over_dma=1,
            dram_address='base32-concat-low32', dram_address_bits=37, dram_alignment_bytes=32,
            dram_base_reset=0, dram_wrap='conservative-alias')

    def run_query(self, source, *flags, success=True):
        self.source.write_text(source)
        self.output.unlink(missing_ok=True)
        run = subprocess.run([str(COMPILER), str(self.source), '--dump-footprints', str(self.output),
                              *flags], capture_output=True, text=True)
        self.assertEqual(self.source.read_text(), source)
        if not success:
            self.assertNotEqual(run.returncode, 0, run.stderr)
            self.assertFalse(self.output.exists())
            return run
        self.assertEqual(run.returncode, 0, run.stderr)
        return json.loads(self.output.read_text())

    def test_query_does_not_claim_to_validate_unscheduled_program(self):
        data = self.run_query('vmatmul.mxu1 acc0, m0, w0\nvmatpop.fp8.acc.mxu1 m8, acc0, e0\n')
        self.assertEqual(data['schema'], 'atlas.footprints.v1')
        self.assertFalse(data['schedule_validated'])
        self.assertTrue(any(e['kind'] == 'RAW' and e['distance'] > 1 for e in data['blocks'][0]['edges']))
        checked = subprocess.run([str(COMPILER), '--check', str(self.source)], capture_output=True)
        self.assertNotEqual(checked.returncode, 0)

    def test_profile_changes_actual_accesses_and_holds(self):
        source = 'vmatmul.mxu1 acc0, m0, w0\n'
        baseline = self.run_query(source)['blocks'][0]['instructions'][0]['footprint']
        profile = self.profile('atlas-mxu1-profile-v1', first_write_age=4, overwrite_acc_read_hold=0)
        changed = self.run_query(source, '--experimental-mxu1-profile', profile)
        f = changed['blocks'][0]['instructions'][0]['footprint']
        write = next(a for a in f['accesses'] if a['resource'] == 'Acc' and a['write'])
        self.assertEqual((write['age'], write['step'], write['count']), (4, 1, 32))
        self.assertTrue(any(h['unit'] == 'accumulator read' for h in baseline['holds']))
        self.assertFalse(any(h['unit'] == 'accumulator read' for h in f['holds']))
        self.assertEqual(next(h['capacity'] for h in f['holds'] if h['unit'] == 'MXU in-flight matmuls'), 2)
        self.assertEqual(changed['model']['source_ir_sha256'], 'a' * 64)

    def test_lsu_profile_exports_selected_accesses_and_inherited_rules(self):
        source = 'vload m0, 0(x0)\nvstore m0, 8(x0)\n'
        baseline = self.run_query(source)
        fields = dict(rows=32, row_step=1, operand_capture='issue', vload_read_age=1,
                      vload_write_age=3, vload_first_free_age=35, vstore_read_age=1,
                      vstore_write_age=3, vstore_first_free_age=35)
        current = self.run_query(source, '--rtl-lsu-profile', self.profile('atlas-lsu-profile-v1', **fields))
        self.assertFalse(baseline['model']['rtl_lsu'])
        self.assertTrue(current['model']['rtl_lsu'])
        self.assertEqual(baseline['blocks'], current['blocks'])
        fields.update(vload_read_age=2, vload_write_age=5, vload_first_free_age=38)
        changed = self.run_query(source, '--rtl-lsu-profile', self.profile('atlas-lsu-profile-v1', **fields))
        self.assertEqual(changed['model']['vload_first_free_age'], 38)
        self.assertIn('inherited', changed['semantics']['lsu_scope'])
        f = changed['blocks'][0]['instructions'][0]['footprint']
        self.assertEqual(next(a['age'] for a in f['accesses'] if a['resource'] == 'MReg'), 5)
        self.assertEqual(next(a['age'] for a in f['accesses'] if a['resource'] == 'Vmem'), 2)
        self.assertEqual(next(h['to'] for h in f['holds'] if h['unit'] == 'VLOAD path'), 37)
        self.assertEqual(f['mreg_writes'], [0])
        self.assertTrue(f['write_during_read'])
        self.assertEqual(f['done_age'], 37)
        self.assertTrue(any(e['distance'] == 38 for e in changed['blocks'][0]['edges']))

    def test_dma_ranges_and_lifetimes_are_preserved(self):
        source = ('addi x5, x0, 3\ndma.config.ch0 x5\nlui x1, 0x90000\n'
                  'lui x6, 0x20000\naddi x12, x0, 32\ndma.load.ch0 x6, x1, x12\n'
                  'dma.wait.ch0\ncsrrw x0, x0, 0xc10 # atlas.release\necall\n')
        data = self.run_query(source, '--rtl-dma-profile', self.dma_profile())
        instructions = data['blocks'][0]['instructions']
        f = instructions[5]['footprint']
        dram = next(a for a in f['accesses'] if a['resource'] == 'Dram')
        self.assertEqual((dram['dram_first_byte'], dram['dram_bytes']), (0x390000000, 32))
        self.assertTrue(dram['at_completion'])
        self.assertFalse(dram['anywhere'])
        self.assertIn('explicit matching DMA.WAIT', data['semantics']['at_completion'])
        self.assertTrue(all(a['age'] == 0 and not a['at_completion'] for a in f['accesses']
                            if a['resource'] in ('XReg', 'DmaBase')))
        self.assertTrue(instructions[7]['release'])
        self.assertTrue(instructions[7]['barrier'])
        self.assertEqual(data['blocks'][0]['entry']['dma_base'], 0)

    def test_cfg_joins_keep_unknown_values_and_delay_slots(self):
        source = ('csrrs x3, x0, 0xc00\nbeq x3, x0, other\naddi x0, x0, 0\n'
                  'addi x1, x0, 32\njal x0, join\naddi x0, x0, 0\n'
                  'other:\naddi x1, x0, 64\njoin:\nvload m0, 0(x1)\necall\n')
        data = self.run_query(source)
        join = next(b for b in data['blocks'] if 'join' in b['labels'])
        self.assertIsNone(join['entry']['xregs'][1])
        self.assertEqual(join['entry']['xregs'][0], 0)
        self.assertTrue(next(a for a in join['instructions'][0]['footprint']['accesses']
                             if a['resource'] == 'Vmem')['anywhere'])
        self.assertEqual(sum(i['delay_slot'] for b in data['blocks'] for i in b['instructions']), 2)

    def test_invalid_input_or_cli_combination_cannot_export(self):
        self.run_query('vsquare.bf16 m1, m0\n', success=False)
        self.run_query('dma.load.ch0 x6, x1, x12\necall\n',
                       '--rtl-dma-profile', self.dma_profile(), success=False)
        for flags in (['--check'], ['--passes', 'schedule'], ['-o', str(self.root / 'after.S')],
                      ['--viz', str(self.root / 'graph.html')]):
            with self.subTest(flags=flags):
                run = self.run_query('ecall\n', *flags, success=False)
                self.assertEqual(run.returncode, 2)


if __name__ == '__main__':
    unittest.main()

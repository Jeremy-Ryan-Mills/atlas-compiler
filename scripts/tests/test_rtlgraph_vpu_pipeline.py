#!/usr/bin/env python3
"""Exercise valid-chain proof against typed fixtures and adversarial mutations."""
import copy
import json
from pathlib import Path
import subprocess
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from rtlgraph_vpu_pipeline import analyze
EXPORTER = None


class PipelineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if EXPORTER is None:
            raise unittest.SkipTest('Pass the typed CIRCT exporter path when running this test directly')
        fixture = Path(__file__).with_name('rtlgraph-vpu-pipeline.mlir')
        result = json.loads(subprocess.check_output([str(EXPORTER), str(fixture),
                            'ResetPipeline', 'UnresetPipeline', 'Combinational'], text=True))
        cls.modules = {m['name']: m for m in result['modules']}

    def test_reset_unreset_and_combinational_pipeline_laws(self):
        for name, count, unreset in (('ResetPipeline',2,False),('UnresetPipeline',1,True),('Combinational',0,False)):
            with self.subTest(name=name):
                result=analyze(self.modules[name])
                self.assertEqual(result['pipeline_cycles'],count)
                self.assertEqual(result['has_unreset_stages'],unreset)

    def test_hidden_input_feedback_and_enable_rejected(self):
        for mutation in ('input','feedback','enable','attribute'):
            module=copy.deepcopy(self.modules['ResetPipeline'])
            reg=next(o for o in module['operations'] if o['kind']=='seq.firreg')
            if mutation=='input':reg['operands'][0]='unknown'
            if mutation=='feedback':reg['operands'][0]=reg['results'][0]
            if mutation=='enable':reg['operands'].append(reg['operands'][0])
            if mutation=='attribute':reg['attributes']['isAsync']='unit'
            with self.subTest(mutation=mutation),self.assertRaisesRegex(ValueError,'Unsupported|Feedback|Enabled|width'):
                analyze(module)

    def test_clock_reset_and_initial_value_changes_rejected(self):
        for mutation in ('clock','reset','init'):
            module=copy.deepcopy(self.modules['ResetPipeline'])
            reg=next(o for o in module['operations'] if o['kind']=='seq.firreg')
            if mutation=='clock':reg['operands'][1]='other-clock'
            if mutation=='reset':reg['operands'][2]=reg['operands'][0]
            if mutation=='init':
                const=next(o for o in module['operations'] if o['kind']=='hw.constant')
                const['attributes']['value']='true'
            with self.subTest(mutation=mutation),self.assertRaisesRegex(ValueError,'clock|reset'):
                analyze(module)

    def test_width_direction_and_unsupported_logic_rejected(self):
        for mutation in ('width','direction','logic'):
            module=copy.deepcopy(self.modules['ResetPipeline'])
            if mutation=='width':next(p for p in module['ports'] if p['name']=='io_resp_valid')['type']='i2'
            if mutation=='direction':next(p for p in module['ports'] if p['name']=='io_req_valid')['direction']='output'
            if mutation=='logic':next(o for o in module['operations'] if o['kind']=='seq.firreg')['kind']='comb.xor'
            with self.subTest(mutation=mutation),self.assertRaisesRegex(ValueError,'Unsupported'):
                analyze(module)


if __name__ == '__main__':
    EXPORTER=Path(sys.argv.pop(1)).resolve()
    unittest.main()

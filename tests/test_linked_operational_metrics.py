import json
from pathlib import Path
import tempfile
import unittest
from sandbox.experiments.linked.contracts import paper_conditions
from tools.export_linked_operational_metrics import metrics, export


class OperationalMetricsTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.batch=self.root/'batch';self.batch.mkdir()
        self.secret='PRIVATE_DIALOGUE_AND_IDENTIFIER';self.case='PRIVATE_CASE';self.condition=paper_conditions()[0]
        (self.batch/'batch.lock').touch();self.put(self.batch/'batch_summary.json',{})
        self.put(self.batch/'specification.json',{'mode':'matrix','conditions':[c.to_dict() for c in paper_conditions()],
            'cases':[{'case_id':self.case,'runtime_input_hash':'a'*64}],
            'modeled_activity_seconds':{'inquiry':30}})
        self.runpath=self.batch/self.case/'run.json'
        self.run={'run_id':self.secret,'case_id':self.case,'config_id':self.condition.config_id,'data_kind':'synthetic_independent',
            'input_hash':'a'*64,'status':'failed','evaluation_unit':'fixed_input_task','error_type':'ModelCallError',
            'activities':[{'role':'nurse','activity_type':'inquiry','active_seconds':30,'timing_source':'constructed_uncalibrated'}],
            'calls':[],'artifacts':{'transcript':[{'speaker_role':'doctor','phase':'doctor_handoff_verification','text':self.secret}]}}
        self.put(self.runpath,self.run)
        self.attempts={c.config_id.replace(':','__'):{'status':'not_run_budget_limit','config_id':c.config_id}
            for c in paper_conditions()}
        self.attempts[self.condition.config_id.replace(':','__')].update(status='failed',result_path=str(self.runpath))
        self.put(self.batch/self.case/'attempts.json',{'attempts':self.attempts})

    def put(self,path,value):path.parent.mkdir(exist_ok=True,parents=True);path.write_text(json.dumps(value))

    def test_all_conditions_failures_and_unscored_denominators_remain(self):
        data=metrics(self.batch);self.assertEqual(len(data['conditions']),15)
        first=data['conditions'][0]
        self.assertEqual(first['status_counts'],{'failed':1})
        self.assertEqual(first['activity_totals'][0]['modeled_active_seconds'],30)
        self.assertEqual(first['programmatic_delivery']['qualified_tasks'],0)
        self.assertIsNone(first['programmatic_delivery']['effort_per_qualified_task_seconds'])
        self.assertEqual(first['physician_handoff_verification_questions'],1)
        self.assertEqual(first['semantic_redundancy'],'not_assessed')
        result=export(self.batch,self.root/'public')
        self.assertEqual(result['planned_stage_attempts'],15)
        self.assertEqual(result['completed_stage_attempts'],0)
        for path in (self.root/'public').iterdir():
            raw=path.read_text();self.assertNotIn(self.secret,raw);self.assertNotIn(self.case,raw)
        self.assertNotIn('pending',(self.root/'public/operational_metrics.csv').read_text())

    def test_missing_or_inconsistent_activity_evidence_does_not_become_zero(self):
        self.run.pop('activities');self.put(self.runpath,self.run)
        with self.assertRaisesRegex(ValueError,'Missing activity'):metrics(self.batch)
        self.run['activities']=[{'role':'nurse','activity_type':'inquiry','active_seconds':1}]
        self.put(self.runpath,self.run)
        with self.assertRaisesRegex(ValueError,'declared timing'):metrics(self.batch)

    def test_fully_virtual_scope_has_programmatic_denominators_and_no_human_gate(self):
        path=self.batch/'specification.json';spec=json.loads(path.read_text())
        spec['evaluation_protocol']='fully_virtual_workflow_v1';self.put(path,spec)
        data=metrics(self.batch);first=data['conditions'][0]
        self.assertEqual(data['evaluation_protocol'],'fully_virtual_workflow_v1')
        self.assertNotIn('independent_quality_qualified_cases',first)
        self.assertEqual(first['programmatic_delivery']['qualified_tasks'],0)
        self.assertEqual(first['programmatic_delivery']['observed_staff_seconds'],30)
        self.assertIsNone(first['programmatic_delivery']['effort_per_qualified_task_seconds'])
        export(self.batch,self.root/'public')
        raw=(self.root/'public/operational_metrics.csv').read_text()
        self.assertNotIn('pending',raw)
        self.assertNotIn(self.secret,json.dumps(data))

    def test_missing_planned_cell_and_mismatched_case_are_rejected(self):
        del self.attempts[paper_conditions()[-1].config_id.replace(':','__')]
        self.put(self.batch/self.case/'attempts.json',{'attempts':self.attempts})
        with self.assertRaisesRegex(ValueError,'planned condition'):metrics(self.batch)
        self.run['case_id']='WRONG';self.put(self.runpath,self.run)
        with self.assertRaisesRegex(ValueError,'identity'):metrics(self.batch)

    def test_glm_pair_reports_the_declared_fifteen_conditions(self):
        conditions=paper_conditions(model_families=('glm51','glm52'))
        spec=json.loads((self.batch/'specification.json').read_text())
        spec['models']={'role_model':'glm-5.2','tool_models':{'glm51':'glm-5.1','glm52':'glm-5.2'}}
        spec['conditions']=[c.to_dict() for c in conditions];self.put(self.batch/'specification.json',spec)
        attempts={c.config_id.replace(':','__'):{'status':'not_run_missing_frozen_input','config_id':c.config_id}
            for c in conditions}
        attempts[self.condition.config_id.replace(':','__')]=self.attempts[self.condition.config_id.replace(':','__')]
        self.put(self.batch/self.case/'attempts.json',{'attempts':attempts})
        data=metrics(self.batch)
        self.assertEqual(len(data['conditions']),15)
        encoded=json.dumps(data)
        self.assertIn('glm51',encoded);self.assertIn('glm52',encoded);self.assertNotIn('deepseek',encoded)


if __name__=='__main__':unittest.main()

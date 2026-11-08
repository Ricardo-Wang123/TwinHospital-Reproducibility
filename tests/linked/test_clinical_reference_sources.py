from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from sandbox.experiments.linked.clinical_reference_sources import assess, review_packet
from sandbox.experiments.linked.clinical_faults import registry
from sandbox.experiments.linked.contracts import digest, runtime_case
from sandbox.experiments.linked.safe_context_adapter import VisibleDocumentedCandidatePPRS
from tools.prepare_original_prescription_references import prepare, sha
from test_safe_context_adapter import fixture


class ReferenceSourceTests(unittest.TestCase):
    def test_originals_preserved_unknown_not_negative_and_blind_packet_has_no_predictions(self):
        context,rx,formulary,catalogue=fixture()
        stage={'prescription':rx,'clinical_context':context};before=deepcopy(stage)
        value=assess(stage,formulary,catalogue)
        self.assertEqual(stage,before)
        self.assertIsNone(value['clinical_gold_label'])
        self.assertFalse(value['clinical_reference_qualified'])
        self.assertEqual(len(value['rule_applicability']),6)
        dose=next(f for f in value['rule_applicability'] if f['category']=='dosage_error')
        self.assertEqual(dose['status'],'indeterminate')
        self.assertFalse(dose['complete_scoped_baseline_for_mutation'])
        packet=review_packet('rxref_blinded',value)
        self.assertEqual(packet['prescription'],rx)
        self.assertEqual(packet['clinical_context'],context)
        text=json.dumps(packet)
        for forbidden in ('rule_applicability','rule_projection','independent_gold_label',
                          'source_status','case_id','patient_key','model_name'):
            self.assertNotIn(forbidden,text)
        self.assertIsNone(packet['rating']['overall_appropriateness'])

    def test_scoped_applicability_does_not_create_whole_prescription_reference(self):
        context,rx,formulary,catalogue=fixture()
        rx['items'][0].update(drug_code='SIM-TIOTROPIUM-18',dose=18,dose_unit='microgram',
            route='inhaled',device='HANDIHALER',frequency='QD',quantity_unit='capsule',
            intended_course={'start_offset_hours':24,'duration_days':14,'basis':'Constructed planned course.'})
        context['consultation']['prescription']=deepcopy(rx)
        value=assess({'prescription':rx,'clinical_context':context},formulary,catalogue)
        route=next(f for f in value['rule_applicability'] if f['category']=='administration_route_error')
        self.assertEqual(route['status'],'scoped_nonviolation')
        self.assertTrue(route['complete_scoped_baseline_for_mutation'])
        self.assertIsNone(route['whole_prescription_compliant'])
        self.assertFalse(route['other_error_categories_excluded'])
        self.assertIsNone(value['clinical_gold_label'])

    def test_hidden_evaluation_and_modified_prepared_context_are_rejected(self):
        context,rx,formulary,catalogue=fixture()
        stage={'prescription':rx,'clinical_context':context}
        for hidden in ('evaluation','provenance','subject_id','gold_label'):
            bad=deepcopy(stage);bad['clinical_context'][hidden]='hidden'
            with self.subTest(hidden=hidden),self.assertRaises(ValueError):
                assess(bad,formulary,catalogue)
        prepared=VisibleDocumentedCandidatePPRS(formulary,catalogue).prepare_context(context)['shared_context']
        prepared['documented_rule_context']['allergies']['assessment_status']='complete'
        with self.assertRaises(ValueError):assess({'prescription':rx,'clinical_context':prepared},formulary,catalogue)


class SourceBatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.batch=self.root/'batch';self.batch.mkdir()
        self.private=self.root/'private';self.public=self.root/'public'
        self.manifest=self.root/'manifest.json';self.catalogue=self.root/'catalogue.json'
        context,rx,formulary,catalogue=fixture()
        context['handoff']={'history':'CLINICAL_TEXT_SENTINEL'}
        self.write(self.catalogue,{'products':catalogue})
        entries=[];planned=[];rows=[];self.run_paths=[]
        for i in range(3):
            case={'case_id':f'private_case_{i}','patient_key':f'private_patient_{i}',
                'group':'type2_diabetes','split':'development','data_kind':'synthetic_independent',
                'patient_initial':{'patient_facts':[]},'his_initial':{},'formulary':formulary}
            path=self.root/f'case_{i}.json';self.write(path,case)
            entries.append({k:case[k] for k in ('case_id','patient_key','group','split','data_kind')})
            entries[-1].update(path=str(path))
            planned.append({'case_id':case['case_id'],'file_sha256':sha(path),
                'runtime_input_hash':digest(runtime_case(case))})
            status='completed' if i==0 else 'stopped_provider'
            stage={'prescription':rx,'clinical_context':context}
            run={'case_id':case['case_id'],'input_hash':digest(runtime_case(case)),
                'status':status,'evaluation_unit':'complete_encounter',
                'stage_inputs':{'prescription':stage} if i<2 else {},'artifacts':{}}
            if i==0:
                run['artifacts']['original_prescription_review']={'prescription_hash':digest(rx),
                    'rule_review':VisibleDocumentedCandidatePPRS(formulary,catalogue).review(rx,context)}
            path=self.batch/f'run_{i}.json';self.write(path,run);self.run_paths.append(path)
            rows.append({'case_id':case['case_id'],'attempts':{'linked_baseline_anchor':{
                'status':status,'result_path':str(path)}}})
        self.write(self.manifest,{'cases':entries})
        self.write(self.batch/'specification.json',{'mode':'linked','cases':planned,
            'rules':{'source_catalogue_file_sha256':sha(self.catalogue),'registry_hash':digest(registry())}})
        self.write(self.batch/'batch_summary.json',{'cases':rows})

    @staticmethod
    def write(path,value):path.write_text(json.dumps(value))

    def prepare(self):return prepare(self.batch,self.manifest,self.catalogue,self.private,self.public)

    def test_includes_interrupted_sources_tracks_missing_and_does_not_publish_clinical_text(self):
        before={p:sha(p) for p in self.root.rglob('*.json')}
        result=self.prepare()
        self.assertEqual(result['planned_patients'],3)
        self.assertEqual(result['original_prescription_inputs'],2)
        self.assertEqual(result['missing_input_counts'],{'original_prescription_input_absent':1})
        self.assertEqual(result['rule_assessments_replayed_identically'],1)
        self.assertEqual(result['original_inputs_without_recorded_rule_review'],1)
        self.assertEqual(result['human_ratings_completed'],0)
        self.assertTrue(all(sha(p)==h for p,h in before.items()))
        for path in self.public.rglob('*'):
            if path.is_file():
                text=path.read_text()
                for private in ('CLINICAL_TEXT_SENTINEL','private_patient_','private_case_',str(self.root)):
                    self.assertNotIn(private,text)
        for name in ('reviewer_1.json','reviewer_2.json','adjudication_blank.json'):
            self.assertEqual(len(json.loads((self.private/name).read_text())['items']),2)
        with self.assertRaises(FileExistsError):self.prepare()

    def test_catalogue_change_and_registry_change_fail_before_writing(self):
        original=self.catalogue.read_bytes();self.catalogue.write_bytes(original+b' ')
        with self.assertRaisesRegex(ValueError,'catalogue'):self.prepare()
        self.assertFalse(self.private.exists());self.assertFalse(self.public.exists())
        self.catalogue.write_bytes(original)
        path=self.batch/'specification.json';spec=json.loads(path.read_text());spec['rules']['registry_hash']='bad'
        self.write(path,spec)
        with self.assertRaisesRegex(ValueError,'registry'):self.prepare()

    def test_missing_archived_rule_key_and_tampered_rule_result_are_not_silently_skipped(self):
        path=self.run_paths[0];run=json.loads(path.read_text());original=deepcopy(run)
        review=run['artifacts']['original_prescription_review']
        review['rule']=review.pop('rule_review');self.write(path,run)
        with self.assertRaisesRegex(ValueError,'lacks'):self.prepare()
        original['artifacts']['original_prescription_review']['rule_review']['decision']='invented'
        self.write(path,original)
        with self.assertRaisesRegex(ValueError,'differs'):self.prepare()
        self.assertFalse(self.private.exists())

    def test_frozen_case_hash_and_run_binding_are_enforced(self):
        path=self.root/'case_1.json';raw=path.read_bytes();path.write_bytes(raw+b' ')
        with self.assertRaisesRegex(ValueError,'case input'):self.prepare()
        path.write_bytes(raw)
        path=self.run_paths[1];run=json.loads(path.read_text());run['input_hash']='wrong';self.write(path,run)
        with self.assertRaisesRegex(ValueError,'run binding'):self.prepare()


if __name__=='__main__':unittest.main()

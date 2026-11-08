from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from sandbox.experiments.linked.case_preparation.formulary_overlay import apply_formulary_overlay,generate,file_hash
from sandbox.experiments.linked.contracts import runtime_case

class FormularyOverlayTests(unittest.TestCase):
    def setUp(self):
        self.old=[{'drug_code':'OLD','name':'old','dose_unit':'mg','quantity_unit':'tablet','route':'oral'}]
        self.new=[{'drug_code':'NEW','name':'new','dose_unit':'mg','quantity_unit':'capsule','route':'oral'}]
        self.case={'case_id':'fixture_1','patient_key':'person_1','group':'test_group','split':'development','data_kind':'synthetic_independent',
            'patient_initial':{'patient_facts':[{'fact_id':'f1','status':'unknown','value':None}]},
            'his_initial':{'historical_orders':[{'dose':1,'time':{'event_offset_hours':-10}}]},
            'evaluation':{'clinical_reference_status':'pending'},'provenance':{'source':'immutable'},'formulary':self.old}

    def test_only_formulary_and_overlay_metadata_change(self):
        before=deepcopy(self.case);after,audit=apply_formulary_overlay(self.case,self.new,{'overlay_version':'v2'})
        self.assertEqual(self.case,before)
        for k in ('patient_initial','his_initial','evaluation','provenance','case_id','patient_key','split'):self.assertEqual(after[k],before[k])
        self.assertEqual(set(audit['changed_top_level_fields']),{'formulary','runtime_overlay_provenance'})
        self.assertFalse(after['runtime_overlay_provenance']['source_product_exact_mapping_verified_by_this_overlay'])

    def test_generate_new_version_and_reject_overwrite(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);c=root/'original.json';c.write_text(json.dumps(self.case));m=root/'manifest.json'
            row={k:self.case[k] for k in ('case_id','patient_key','group','split','data_kind')};row.update(path=str(c),sha256=file_hash(c))
            m.write_text(json.dumps({'version':'original','cases':[row]}));f=root/'formulary.json';f.write_text(json.dumps(self.new));old=file_hash(c)
            out=root/'new';summary=generate(m,f,out,'v2','new_batch',1)
            self.assertTrue(summary['all_other_protected_fields_unchanged']);self.assertEqual(file_hash(c),old)
            result=json.loads((out/'manifest.json').read_text());self.assertEqual(result['cases'][0]['case_id'],row['case_id'])
            self.assertTrue(result['new_run_required']);self.assertFalse(result['reuse_existing_business_state'])
            self.assertEqual(json.loads(Path(result['cases'][0]['path']).read_text())['formulary'],self.new)
            with self.assertRaises(FileExistsError):generate(m,f,out,'v2','new_batch',1)

    def test_duplicate_codes_rejected_without_mutating_case(self):
        with self.assertRaises(ValueError):apply_formulary_overlay(self.case,self.new+self.new,{})
        self.assertEqual(self.case['formulary'],self.old)

    def test_overlay_and_evaluation_provenance_stay_out_of_role_input(self):
        result,_=apply_formulary_overlay(self.case,self.new,{'source_formulary_file':'private/local/catalogue'})
        actor=runtime_case(result)
        self.assertNotIn('runtime_overlay_provenance',actor);self.assertNotIn('evaluation',actor)
        self.assertNotIn('provenance',actor);self.assertEqual(actor['formulary'],self.new)

if __name__=='__main__':unittest.main()

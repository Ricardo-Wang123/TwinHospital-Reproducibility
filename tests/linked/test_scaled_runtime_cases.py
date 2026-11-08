from copy import deepcopy
import json,tempfile,unittest
from pathlib import Path
from sandbox.experiments.linked.case_preparation.case_contract import _opaque
from sandbox.experiments.linked.case_preparation.closed_followup_v3 import build_linked
from sandbox.experiments.linked.case_preparation.scaled_runtime_cases import retained_entry
from sandbox.experiments.linked.case_preparation.v4_source_qualification import load_frequency_context
from sandbox.experiments.linked.case_preparation.formulary_overlay import file_hash


class ScaledInputsTests(unittest.TestCase):
    def test_heldout_source_is_never_relabelled_development(self):
        seed={'patient':{'anchor_age':'50','anchor_year':'2100'},'anchor_admission':{'dischtime':'2100-01-01 00:00:00'}}
        package={'case':{'case_id':'test_fixture','patient_key':'fixture_key','clinical':{
            'demographics':{'age_at_source_admission':{'status':'approximate'},'sex_recorded':'F'},
            'history':{'encounters':[]},'medications':{'orders':[]},'observations':[],
            'allergies':{'status':'unknown','items':[]}}}}
        original=deepcopy(package)
        case=build_linked(package,seed,{'group':'type2_diabetes','split':'test'},0,None)
        self.assertEqual(case['split'],'test');self.assertEqual(package,original)
        with self.assertRaisesRegex(ValueError,'partition'):
            build_linked(package,seed,{'group':'type2_diabetes','split':'unexpected'},0,None)

    def test_retained_cases_cannot_cross_partitions_or_change_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            f=Path(tmp)/'case.json';f.write_text('{}')
            selection={'subject_id':'FICTIONAL','group':'copd','split':'development'}
            key=_opaque(b'test-only','pt','FICTIONAL')
            row={'patient_key':key,'group':'copd','split':'development','path':str(f),'file_sha256':file_hash(f)}
            active={key:row};self.assertEqual(retained_entry(selection,active,b'test-only'),row)
            with self.assertRaisesRegex(ValueError,'partition'):
                retained_entry({**selection,'split':'test'},active,b'test-only')
            f.write_text('{"changed":true}')
            with self.assertRaisesRegex(ValueError,'changed'):retained_entry(selection,active,b'test-only')

    def test_explicit_source_directory_joins_frequency_without_using_other_packages(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);folder=root/'new'/'fixture';folder.mkdir(parents=True)
            row={'subject_id':'F','hadm_id':'H','pharmacy_id':'P'}
            (folder/'provenance.json').write_text(json.dumps({'source_rows':{'r':{'row':row}}}))
            (folder/'pharmacy_private.json').write_text(json.dumps([{**row,'frequency':'BID','status':'historic'}]))
            case={'provenance':{'source_case_id':'fixture'},'his_initial':{'historical_orders':[{'record_id':'order','source_ref':'r'}]}}
            context,hashes,counts=load_frequency_context(case,root,source_package_root=root/'new')
            self.assertEqual(context['order']['frequency'],'BID');self.assertEqual(counts,{'unique_agreement':1})
            self.assertEqual(len(hashes),2)
            with self.assertRaises(ValueError):load_frequency_context(case,root)


if __name__=='__main__':unittest.main()

import unittest, json, tempfile
from pathlib import Path
from sandbox.experiments.linked.medication_catalogue import get_formulary_v3
from sandbox.experiments.linked.medication_catalogue_v4 import additions,formulary,FIRST5

class V4CatalogueTests(unittest.TestCase):
    def test_prior_products_untouched(self):
        self.assertEqual(formulary()[:14],get_formulary_v3())
    def test_first5_is_shared_18_product_catalogue(self):
        self.assertEqual(len(formulary(first5=True)),18)
        self.assertEqual(tuple(x['drug_code'] for x in additions(first5=True)),FIRST5)
    def test_succinate_mass_is_not_label_dose(self):
        rows={x['drug_code']:x for x in additions()}
        p=rows['SIM-METOPROLOL-SUCCINATE-ER-25']
        self.assertEqual(p['components'][0]['strength_value'],25)
        self.assertEqual(p['active_salt_mass']['value'],23.75)
        self.assertIn('tartrate equivalent',p['dose_basis'])
    def test_devices_and_combo_cannot_accept_unqualified_mass(self):
        for p in additions():
            if '+' in p['ingredient']:
                self.assertNotIn('mg',p['allowed_dose_units'])
                self.assertNotIn('mcg',p['allowed_dose_units'])
        diskus=next(p for p in additions() if 'DISKUS' in p['drug_code'])
        self.assertEqual(diskus['dose_unit'],'inhalation')
        self.assertEqual(diskus['quantity_unit'],'inhaler')
        self.assertEqual(diskus['label_package']['metered_inhalations_per_device'],60)
    def test_no_patient_ndc_or_regimen_inference(self):
        for p in additions():
            self.assertFalse(p['historical_ndc_verified'])
            self.assertFalse(p['clinical_rule_gold'])
            self.assertIsNone(p['clinical_regimen'])
            self.assertFalse(p['current_use_inferred'])
    def test_copies_and_unique_codes(self):
        a=additions();self.assertEqual(len({p['drug_code'] for p in a}),len(a))
        a[0]['components'][0]['strength_value']=999
        self.assertEqual(additions()[0]['components'][0]['strength_value'],500)
    def test_all_requirements_add_23_unique_products(self):
        self.assertEqual(len(additions()),23)
        self.assertEqual(len(formulary()),37)
        for p in additions():
            self.assertTrue(p['source_references'][0]['setid'])
            self.assertIsNone(p['source_references'][0]['source_file_sha256'])
    def test_compound_mass_and_device_quantity_rejected_before_write(self):
        from sandbox.experiments.linked.native_his import NativeOpenHISPort,NativeBlocked
        class NoWrites:
            def post(self,*args):raise AssertionError('native write must not be reached')
        mapping=json.loads((Path(__file__).resolve().parents[2]/'deploy/openhis-experiment/catalog_mapping_v4.json').read_text())
        with tempfile.TemporaryDirectory() as tmp:
            port=NativeOpenHISPort(tmp,clients={'doctor':NoWrites()},catalog_mapping=mapping)
            for p in additions():
                if '+' not in p['ingredient']:continue
                item={'drug_code':p['drug_code'],'dose':250,'dose_unit':'mg','quantity':1,
                      'quantity_unit':p['quantity_unit'],'frequency':'QD','route':p['route']}
                with self.assertRaises(NativeBlocked):
                    port._save_order({'order_id':'x','items':[item]},{'run_id':'x'},
                                     {'orders':{},'contract_id':'c'},{'patientId':'p','encounterId':'e'})
    def test_native_mapping_preserves_legacy_identity_and_no_ratio(self):
        base=Path(__file__).resolve().parents[2]/'deploy/openhis-experiment'
        old=json.loads((base/'catalog_mapping_v3.json').read_text())
        new=json.loads((base/'catalog_mapping_v4.json').read_text())
        for code,m in old.items():self.assertEqual(new[code],m)
        for p in additions():
            m=new[p['drug_code']]
            self.assertEqual(set(m['dose_unit_codes']),set(p['allowed_dose_units']))
            self.assertEqual(m['source_quantity_unit'],p['quantity_unit'])
            self.assertFalse(m['historical_ndc_verified'])

if __name__=='__main__':unittest.main()

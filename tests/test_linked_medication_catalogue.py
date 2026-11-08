import copy,json,unittest
from pathlib import Path
from sandbox.experiments.linked.medication_catalogue import get_catalogue,get_formulary,normalize_medication,normalize_unit

class MedicationCatalogueTests(unittest.TestCase):
 def test_candidate_scope_does_not_supply_regimen_or_cohort_key(self):
  core=get_formulary();self.assertEqual(len(core),14)
  self.assertTrue(all(not({'dose','frequency','quantity','group'}&set(p)) for p in core))
  self.assertTrue(all(p['route'] in ('oral','inhaled') for p in core))
  self.assertEqual(len(get_catalogue(include_iv=True)),18)
  self.assertEqual(len(get_catalogue(include_iv=True,include_fault_only=True)),19)
 def test_exact_strength_prevents_100_to_90_substitution(self):
  x={'ingredient':'salbutamol','dosage_form':'metered_dose_inhalation_aerosol','strength':'100 μg/actuation'}
  self.assertEqual(normalize_medication(x)['status'],'unknown')
  x['strength']='90 μg/actuation';self.assertEqual(normalize_medication(x)['catalogue_code'],'SIM-ALBUTEROL-90')
 def test_release_form_mismatch_never_uses_immediate_release(self):
  x={'ingredient':'metformin','dosage_form':'oral_tablet_extended_release','strength':'500 mg/tablet'}
  self.assertEqual(normalize_medication(x)['status'],'unknown')
 def test_mimic_codes_alone_do_not_fabricate_strength_or_current_use(self):
  x={'drug_name_raw':'metformin','codes_raw':{'ndc':'00000000000'},'regimen_raw':{'prod_strength':'500mg Tablet'}}
  before=copy.deepcopy(x);n=normalize_medication(x)
  self.assertEqual(n['status'],'unknown');self.assertFalse(n['current_use_inferred']);self.assertEqual(x,before)
 def test_unknown_route_is_recorded_not_silently_corrected(self):
  n=normalize_medication({'drug_code':'SIM-TIOTROPIUM-18','route':'oral'})
  self.assertEqual(n['status'],'matched');self.assertFalse(n['route_matches_product']);self.assertEqual(n['route_original'],'oral')
 def test_conflicting_code_and_ingredient_cannot_choose_product(self):
  n=normalize_medication({'drug_code':'SIM-METFORMIN-500','drug_name':'warfarin'})
  self.assertEqual(n['status'],'unknown')
 def test_nitrofurantoin_formulation_and_combination_doses_are_explicit(self):
  x={'ingredient':'nitrofurantoin','dosage_form':'oral_capsule','strength':'100 mg/capsule'}
  self.assertEqual(normalize_medication(x)['status'],'unknown')
  combo=next(p for p in get_catalogue() if p['drug_code']=='SIM-TMP-SMX-160-800')
  self.assertEqual(combo['dose_unit'],'tablet');self.assertEqual([c['strength_value'] for c in combo['components']],[160,800])
 def test_v2_mappings_match_candidate_units_and_route_only(self):
  path=Path(__file__).resolve().parents[1]/'deploy/openhis-experiment/catalog_mapping_v2.json'
  mapping=json.loads(path.read_text())
  for p in get_catalogue():
   m=mapping[p['drug_code']]
   self.assertEqual(m['source_dose_unit'],p['dose_unit']);self.assertEqual(m['source_quantity_unit'],p['quantity_unit'])
   self.assertEqual(set(m['route_codes']),set(p['allowed_routes']))
  self.assertNotIn('SIM-SALBUTAMOL-100',mapping)
  self.assertNotIn('SIM-CEFTRIAXONE-1G',mapping)
 def test_unit_aliases_do_not_equate_milligram_and_microgram(self):
  self.assertEqual(normalize_unit('μg'),'mcg');self.assertEqual(normalize_unit('µg'),'mcg')
  self.assertNotEqual(normalize_unit('mg'),normalize_unit('mcg'))
if __name__=='__main__':unittest.main()

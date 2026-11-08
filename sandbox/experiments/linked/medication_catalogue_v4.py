"""Append-only generic hospital products; source NDC identity stays unverified.

Labels support product identity, not a regimen, eligibility decision or gold label.
All cases see the same catalogue. No source prescription is mapped automatically.
"""
from copy import deepcopy
from .medication_catalogue import _product, _component, get_catalogue, get_formulary_v3

VERSION = 'TH-CATALOGUE-3.0'
FIRST5 = ('SIM-METFORMIN-ER-500', 'SIM-METOPROLOL-SUCCINATE-ER-25',
          'SIM-FLUTICASONE-SALMETEROL-DISKUS-250-50', 'SIM-LEVOFLOXACIN-750')

def make(code, name, zh, ingredient, form, strength, dose, quantity, route, setid,
         components, *, notes=(), dose_basis=None, package=None, salt_mass=None):
    p = _product(code, name, zh, ingredient, form, strength, dose, quantity, route,
                 setid, components, notes=notes)
    p.update(catalogue_version=VERSION, allowed_dose_units=[dose], dose_basis=dose_basis,
             label_package=package, active_salt_mass=salt_mass,
             historical_product_identity='unverified', historical_ndc_verified=False,
             independent_review_status='pending', clinical_rule_gold=False,
             native_product_scope='new_independent_simulated_hospital_generic_product')
    p['source_references'][0].update(source_file_sha256=None,
        source_file_hash_status='official_page_read; full_SPL_bytes_not_archived',
        support_sections=['Description', 'Dosage forms and strengths', 'Ingredients and appearance / principal display'])
    return p

def c(name, value, unit='mg', per='tablet', basis=None):
    return _component(name,value,unit,per,basis=basis)

NEW = [
 make('SIM-METFORMIN-ER-500','metformin hydrochloride ER','盐酸二甲双胍缓释片500mg','metformin',
      'oral_tablet_extended_release','500 mg/tablet','mg','tablet','oral',
      'ef77c7dd-a65f-4adc-a0d9-a93779b459a0',[c('metformin hydrochloride',500)],
      notes=('Separate from IR. Reference label is an osmotic 24-hour ER tablet; historical XR release technology is unverified.',),
      dose_basis='mg of metformin hydrochloride, not metformin base'),
 make('SIM-METOPROLOL-SUCCINATE-ER-25','metoprolol succinate ER (labeled 25 mg)','琥珀酸美托洛尔缓释片（标示25mg）','metoprolol',
      'oral_tablet_extended_release','25 mg tartrate-equivalent/tablet','mg','tablet','oral',
      '58441cd3-6043-4bb8-adab-1b32a203bfb2',[c('metoprolol succinate',25,basis='metoprolol tartrate equivalent')],
      notes=('Extended-release succinate, distinct from tartrate immediate-release. Do not alter a proposed dose by salt-mass conversion.',),
      dose_basis='labeled mg expressed as metoprolol tartrate equivalent; delivered as succinate ER',
      salt_mass={'ingredient':'metoprolol succinate','value':23.75,'unit':'mg','per':'tablet'}),
 make('SIM-FLUTICASONE-SALMETEROL-DISKUS-250-50','fluticasone propionate/salmeterol Diskus 250/50','氟替卡松/沙美特罗Diskus吸入粉250/50μg',
      'fluticasone propionate+salmeterol','dry_powder_inhaler_diskus','250 mcg fluticasone propionate + 50 mcg salmeterol/inhalation',
      'inhalation','inhaler','inhaled','4eeb5f6a-593f-4a9e-9692-adefa2caf8fc',
      [c('fluticasone propionate',250,'mcg','inhalation'),c('salmeterol xinafoate',50,'mcg','inhalation','salmeterol base')],
      notes=('One inhalation uses one pre-metered blister. No mg or mcg dose accepted for this combination.',),
      dose_basis='count of 250/50 mcg metered blister inhalations, not devices or component mass',
      package={'device':'Diskus','metered_inhalations_per_device':60},
      salt_mass={'ingredient':'salmeterol xinafoate','value':72.5,'unit':'mcg','per':'inhalation'}),
 make('SIM-LEVOFLOXACIN-750','levofloxacin','左氧氟沙星片750mg','levofloxacin','oral_tablet_immediate_release',
      '750 mg/tablet','mg','tablet','oral','c6027cde-b9fe-e593-e053-2995a90ac2c1',
      [c('levofloxacin hemihydrate',750,basis='levofloxacin')],dose_basis='mg of levofloxacin equivalent, supplied as hemihydrate'),
]
for label, salt in ((50,47.5),(100,95)):
    p=deepcopy(NEW[1]);p['drug_code']='SIM-METOPROLOL-SUCCINATE-ER-'+str(label)
    p['name']=f'metoprolol succinate ER (labeled {label} mg)'
    p['display_name_zh']=f'琥珀酸美托洛尔缓释片（标示{label}mg）'
    p['strength']=f'{label} mg tartrate-equivalent/tablet'
    p['components'][0]['strength_value']=label;p['active_salt_mass']['value']=salt;NEW.append(p)
p=deepcopy(NEW[3]);p.update(drug_code='SIM-LEVOFLOXACIN-500',display_name_zh='左氧氟沙星片500mg',strength='500 mg/tablet')
p['components'][0]['strength_value']=500;NEW.append(p)
NEW += [
 make('SIM-IPRATROPIUM-NEB-05-25ML','ipratropium bromide nebulizer solution','异丙托溴铵雾化液0.5mg/2.5mL','ipratropium',
      'nebulizer_solution','0.5 mg ipratropium bromide/2.5 mL','mL','vial','inhaled',
      '51350fab-a919-4d97-9d0c-d9dc7286244c',[c('ipratropium bromide',0.5,'mg','2.5 mL','ipratropium bromide anhydrous')],
      notes=('One unit-dose vial contains 2.5 mL. Requires a nebulizer; not injection. Historical concentration remains unverified if missing.',),
      dose_basis='mL of this solution, no implicit mg conversion',package={'volume_mL_per_vial':2.5,'device_required':'nebulizer'}),
 make('SIM-IPRATROPIUM-ALBUTEROL-RESPIMAT-20-100','ipratropium bromide/albuterol Respimat','异丙托溴铵/沙丁胺醇Respimat20/100μg',
      'ipratropium+albuterol','metered_inhalation_spray_respimat','20 mcg ipratropium bromide + 100 mcg albuterol/actuation',
      'puff','inhaler','inhaled','9832312e-0310-4ab6-8501-afa976fc7eb2',
      [c('ipratropium bromide monohydrate',20,'mcg','actuation'),c('albuterol sulfate',100,'mcg','actuation','albuterol base')],
      notes=('One puff is one metered actuation; not a nebulizer solution. Unqualified mass dose is not accepted.',),
      dose_basis='count of metered actuations',package={'device':'Respimat','metered_actuations_per_device':120,'cartridge_fill_g':4},
      salt_mass={'ingredient':'albuterol sulfate','value':120,'unit':'mcg','per':'actuation'}),
 make('SIM-IPRATROPIUM-ALBUTEROL-NEB-05-3-3ML','ipratropium bromide/albuterol sulfate nebulizer solution','异丙托溴铵/硫酸沙丁胺醇雾化液0.5/3mg每3mL',
      'ipratropium+albuterol','nebulizer_solution','0.5 mg ipratropium bromide + 3 mg albuterol sulfate/3 mL',
      'mL','vial','inhaled','89d59bd9-c826-413c-8ed9-a1521a0c7cc5',
      [c('ipratropium bromide',0.5,'mg','3 mL','ipratropium bromide anhydrous'),c('albuterol sulfate',2.5,'mg','3 mL','albuterol base')],
      notes=('The 3 mg is albuterol sulfate, equivalent to 2.5 mg base. A dose of 3 mL is volume, not a 3 mg single-ingredient dose.',),
      dose_basis='mL of this fixed-combination solution only',package={'volume_mL_per_vial':3,'device_required':'nebulizer'},
      salt_mass={'ingredient':'albuterol sulfate','value':3,'unit':'mg','per':'3 mL'}),
]

# Identity evidence independently read by collaborators; source facts hashed in metadata.
NEW += [
 make('SIM-LISINOPRIL-5', 'lisinopril', '赖诺普利片5mg', 'lisinopril', 'oral_tablet_immediate_release', '5 mg/tablet', 'mg', 'tablet', 'oral', '7d6c31e2-b5a4-4279-8013-a8dad37ea73b', [{'ingredient': 'lisinopril', 'strength_value': 5, 'strength_unit': 'mg', 'per': 'tablet', 'basis_of_strength': 'lisinopril'}],
      notes=('Strength uses label lisinopril basis, not a recalculated anhydrous mass.',), dose_basis='mg on label basis: lisinopril', package=None),
 make('SIM-LOSARTAN-POTASSIUM-50', 'losartan potassium', '氯沙坦钾片50mg', 'losartan', 'oral_tablet_immediate_release', '50 mg/tablet', 'mg', 'tablet', 'oral', 'ca012edc-923d-4ada-a261-db82b71b3c4f', [{'ingredient': 'losartan potassium', 'strength_value': 50, 'strength_unit': 'mg', 'per': 'tablet', 'basis_of_strength': 'losartan potassium'}],
      notes=('Salt basis explicitly losartan potassium; not hydrochlorothiazide combination.',), dose_basis='mg on label basis: losartan potassium', package=None),
 make('SIM-METOPROLOL-TARTRATE-25', 'metoprolol tartrate IR', '酒石酸美托洛尔普通片25mg', 'metoprolol', 'oral_tablet_immediate_release', '25 mg/tablet', 'mg', 'tablet', 'oral', '35922e9c-6a4c-446a-8294-a4cd3f3f0d10', [{'ingredient': 'metoprolol tartrate', 'strength_value': 25, 'strength_unit': 'mg', 'per': 'tablet', 'basis_of_strength': 'metoprolol tartrate'}],
      notes=('Separate from metoprolol succinate extended-release; do not substitute automatically.',), dose_basis='mg on label basis: metoprolol tartrate', package=None),
 make('SIM-GLIPIZIDE-IR-5', 'glipizide IR', '格列吡嗪普通片5mg', 'glipizide', 'oral_tablet_immediate_release', '5 mg/tablet', 'mg', 'tablet', 'oral', 'e0de2d19-b24b-46af-a10e-0b78c51350a4', [{'ingredient': 'glipizide', 'strength_value': 5, 'strength_unit': 'mg', 'per': 'tablet', 'basis_of_strength': 'glipizide'}],
      notes=('Conventional tablets; distinct product from extended-release forms.',), dose_basis='mg on label basis: glipizide', package=None),
 make('SIM-GLIPIZIDE-ER-5', 'glipizide ER', '格列吡嗪缓释片5mg', 'glipizide', 'oral_tablet_extended_release', '5 mg/tablet', 'mg', 'tablet', 'oral', '0ffe361a-c99b-4f68-a7e2-a67a71c0c496', [{'ingredient': 'glipizide', 'strength_value': 5, 'strength_unit': 'mg', 'per': 'tablet', 'basis_of_strength': 'glipizide'}],
      notes=('Use the labeled delivered dose; do not change to total drug load.', 'Keep whole-tablet instructions and release form distinct; catalogue identity does not authorize changing a historical regimen.'), dose_basis='mg on label basis: glipizide', package=None),
 make('SIM-GLIPIZIDE-ER-10', 'glipizide ER', '格列吡嗪缓释片10mg', 'glipizide', 'oral_tablet_extended_release', '10 mg/tablet', 'mg', 'tablet', 'oral', '0ffe361a-c99b-4f68-a7e2-a67a71c0c496', [{'ingredient': 'glipizide', 'strength_value': 10, 'strength_unit': 'mg', 'per': 'tablet', 'basis_of_strength': 'glipizide'}],
      notes=('Use the labeled delivered dose; do not change to total drug load.', 'Keep whole-tablet instructions and release form distinct; catalogue identity does not authorize changing a historical regimen.'), dose_basis='mg on label basis: glipizide', package=None),
 make('SIM-AMOXICILLIN-CLAVULANATE-875-125', 'amoxicillin/clavulanate', '阿莫西林/克拉维酸片875/125mg', 'amoxicillin+clavulanic acid', 'oral_tablet_immediate_release', '875 mg amoxicillin + 125 mg clavulanic acid/tablet', 'tablet', 'tablet', 'oral', 'af4c2ea8-73bd-429b-8392-fa20fec6fb3f', [{'ingredient': 'amoxicillin trihydrate', 'strength_value': 875, 'strength_unit': 'mg', 'per': 'tablet', 'basis_of_strength': 'amoxicillin anhydrous'}, {'ingredient': 'clavulanate potassium', 'strength_value': 125, 'strength_unit': 'mg', 'per': 'tablet', 'basis_of_strength': 'clavulanic acid'}],
      notes=('The 875/125 tablet contains both components; 875 mg alone denotes the amoxicillin component, not total mixture mass.', 'The source record shorthand 875 does not independently verify clavulanate strength or source NDC. This is an explicitly defined simulation product.', 'Select the 875/125 tablet product; discussion of XR products elsewhere in the label does not turn this product into XR.'), dose_basis='count of fixed-combination tablets, never unqualified component mg', package=None),
 make('SIM-DOXYCYCLINE-HYCLATE-100', 'doxycycline hyclate', '多西环素盐酸盐胶囊100mg', 'doxycycline', 'oral_capsule', '100 mg doxycycline/capsule', 'mg', 'capsule', 'oral', '80b06267-a8ff-4744-8594-b5533127768c', [{'ingredient': 'doxycycline hyclate (hydrochloride hemiethanolate hemihydrate)', 'strength_value': 100, 'strength_unit': 'mg', 'per': 'capsule', 'basis_of_strength': 'doxycycline anhydrous'}],
      notes=('Preserve hyclate salt identity; do not silently map this capsule to doxycycline monohydrate or a delayed-release tablet.', '100 mg is doxycycline-equivalent strength, not 100 mg of the hyclate salt mass.'), dose_basis='doxycycline anhydrous; 100 mg per capsule', package=None),
 make('SIM-CIPROFLOXACIN-250', 'ciprofloxacin hydrochloride IR', '盐酸环丙沙星片250mg', 'ciprofloxacin', 'oral_tablet_immediate_release', '250 mg ciprofloxacin/tablet', 'mg', 'tablet', 'oral', '6a935846-3c97-4517-bc4e-2377f673285e', [{'ingredient': 'ciprofloxacin hydrochloride monohydrate', 'strength_value': 250, 'strength_unit': 'mg', 'per': 'tablet', 'basis_of_strength': 'ciprofloxacin'}],
      notes=('Strength is ciprofloxacin-equivalent, not hydrochloride-monohydrate mass.', 'Use the conventional film-coated tablet in this label, not a ciprofloxacin extended-release product.', 'The same label supports the two strengths as distinct catalogue products; do not collapse their tablet counts.'), dose_basis='ciprofloxacin free-base equivalent', package=None),
 make('SIM-CIPROFLOXACIN-500', 'ciprofloxacin hydrochloride IR', '盐酸环丙沙星片500mg', 'ciprofloxacin', 'oral_tablet_immediate_release', '500 mg ciprofloxacin/tablet', 'mg', 'tablet', 'oral', '6a935846-3c97-4517-bc4e-2377f673285e', [{'ingredient': 'ciprofloxacin hydrochloride monohydrate', 'strength_value': 500, 'strength_unit': 'mg', 'per': 'tablet', 'basis_of_strength': 'ciprofloxacin'}],
      notes=('Strength is ciprofloxacin-equivalent, not hydrochloride-monohydrate mass.', 'Use the conventional film-coated tablet in this label, not a ciprofloxacin extended-release product.', 'The same label supports the two strengths as distinct catalogue products; do not collapse their tablet counts.'), dose_basis='ciprofloxacin free-base equivalent', package=None),
 make('SIM-NITROFURANTOIN-MACRO-50', 'nitrofurantoin macrocrystals', '呋喃妥因大晶体胶囊50mg', 'nitrofurantoin', 'oral_capsule_macrocrystals', '50 mg/capsule', 'mg', 'capsule', 'oral', 'cfa20cfd-478f-48c6-9b59-3c425abd766a', [{'ingredient': 'nitrofurantoin macrocrystals', 'strength_value': 50, 'strength_unit': 'mg', 'per': 'capsule', 'basis_of_strength': 'nitrofurantoin'}],
      notes=('This product is macrocrystals only, not the separately catalogued nitrofurantoin monohydrate/macrocrystals 100 mg combination.', 'Do not inherit release/form or administration schedule from the monohydrate/macrocrystals product. No schedule is assigned by this identity evidence.'), dose_basis='nitrofurantoin macrocrystals; 50 mg per capsule', package=None),
 make('SIM-TMP-SMX-SUSP-40-200-5ML', 'trimethoprim/sulfamethoxazole suspension', '复方磺胺甲噁唑口服混悬液TMP40/SMX200mg每5mL', 'trimethoprim+sulfamethoxazole', 'oral_suspension', '40 mg trimethoprim + 200 mg sulfamethoxazole/5 mL', 'mL', 'mL', 'oral', '777da85a-a776-4681-976d-326ec421cfc9', [{'ingredient': 'sulfamethoxazole', 'strength_value': 200, 'strength_unit': 'mg', 'per': '5 mL', 'basis_of_strength': 'sulfamethoxazole'}, {'ingredient': 'trimethoprim', 'strength_value': 40, 'strength_unit': 'mg', 'per': '5 mL', 'basis_of_strength': 'trimethoprim'}],
      notes=('Order of 800/160 on this label is sulfamethoxazole/trimethoprim. In TMP/SMX order the same 20 mL is 160/800 mg.', 'The 20 mL amount is a volume and is also the reference unit-dose cup size; it is not a tablet or an automatically selected clinical dose.', 'Store concentration by both components per mL; keep prescribed volume and dispensed volume separate.', 'This does not replace the separate DS tablet product even when total active ingredient amounts match.'), dose_basis='mL of fixed-concentration suspension, not mg or cup count', package={'unit_dose_cup_volume_mL': 20, 'reference_label_package_ndc': '0121-0853-20', 'cups_per_tray': 10}),
 make('SIM-AMPICILLIN-250', 'ampicillin', '氨苄西林胶囊250mg', 'ampicillin', 'oral_capsule', '250 mg ampicillin/capsule', 'mg', 'capsule', 'oral', '7f8b1ab1-73c6-4cdf-a838-7a4554c62586', [{'ingredient': 'ampicillin trihydrate', 'strength_value': 250, 'strength_unit': 'mg', 'per': 'capsule', 'basis_of_strength': 'ampicillin'}],
      notes=('250 mg denotes ampicillin-equivalent strength from the trihydrate.', 'This oral capsule is distinct from injectable ampicillin sodium and from oral suspension.'), dose_basis='ampicillin equivalent; 250 mg per capsule', package=None),
]

def additions(*, first5=False):
    return deepcopy([p for p in NEW if not first5 or p['drug_code'] in FIRST5])

def products(*, first5=False):
    old=get_catalogue()
    return old+additions(first5=first5)

def formulary(*, first5=False):
    keys=('drug_code','name','display_name_zh','ingredient','dosage_form','strength','dose_unit','quantity_unit',
          'allowed_routes','route','components','notes','catalogue_version','allowed_dose_units','dose_basis','label_package','active_salt_mass')
    return get_formulary_v3()+[{k:p.get(k) for k in keys} for p in additions(first5=first5)]

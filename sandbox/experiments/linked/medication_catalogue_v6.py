"""Additional independently defined products for common concomitant medicines.

Official labels establish identities only. No historical NDC match, default
regimen, treatment indication, or clinical approval follows from availability.
"""
from copy import deepcopy

from .medication_catalogue_v4 import make, c

VERSION = 'TH-CATALOGUE-3.2'

_ADDITIONS = [
    make('SIM-OMEPRAZOLE-DR-20', 'omeprazole delayed release', '奥美拉唑肠溶胶囊20mg',
         'omeprazole', 'oral_capsule_delayed_release', '20 mg/capsule', 'mg', 'capsule', 'oral',
         '0fb314cb-6291-422c-98ff-24d8449992f6', [c('omeprazole', 20, per='capsule')],
         dose_basis='mg of omeprazole in enteric-coated pellets; not omeprazole magnesium mass',
         notes=('This is the labeled omeprazole product, not an assumed magnesium salt product.',)),
    make('SIM-TAMSULOSIN-HCL-04', 'tamsulosin hydrochloride', '盐酸坦索罗辛胶囊0.4mg',
         'tamsulosin', 'oral_capsule', '0.4 mg tamsulosin hydrochloride/capsule', 'mg', 'capsule', 'oral',
         '13a5032d-0be1-4001-916e-459f0b1a051d', [c('tamsulosin hydrochloride', 0.4, per='capsule')],
         dose_basis='mg of tamsulosin hydrochloride, not tamsulosin base',
         notes=('Do not crush, chew or open this capsule. Availability does not establish an indication.',)),
    make('SIM-PANTOPRAZOLE-DR-40', 'pantoprazole sodium delayed release', '泮托拉唑钠肠溶片（泮托拉唑40mg）',
         'pantoprazole', 'oral_tablet_delayed_release', '40 mg pantoprazole/tablet', 'mg', 'tablet', 'oral',
         'fa8eed8c-f5d0-4b90-b184-cff8d9c8fb6a', [c('pantoprazole sodium sesquihydrate', 40, basis='pantoprazole')],
         dose_basis='mg pantoprazole equivalent, supplied as sodium sesquihydrate',
         salt_mass={'ingredient': 'pantoprazole sodium sesquihydrate', 'value': 45.11, 'unit': 'mg', 'per': 'tablet'},
         notes=('Delayed-release tablet; do not split, crush or chew. The 40 mg is pantoprazole-equivalent mass.',)),
    make('SIM-DOCUSATE-SODIUM-100', 'docusate sodium softgel', '多库酯钠软胶囊100mg',
         'docusate sodium', 'oral_capsule_softgel', '100 mg/capsule', 'mg', 'capsule', 'oral',
         'eb5e036b-dc1c-4a8d-b2c6-de1395384c57', [c('docusate sodium', 100, per='capsule')],
         dose_basis='mg of docusate sodium per softgel',
         notes=('Separate from oral solution and docusate calcium; no concentration or volume conversion is assigned.',)),
    make('SIM-ISOSORBIDE-MONONITRATE-ER-30', 'isosorbide mononitrate extended release', '单硝酸异山梨酯缓释片30mg',
         'isosorbide mononitrate', 'oral_tablet_extended_release', '30 mg/tablet', 'mg', 'tablet', 'oral',
         '8cbb364a-bafe-454b-a53f-bd7cd69a5593', [c('isosorbide mononitrate', 30)],
         dose_basis='mg of isosorbide mononitrate in the extended-release tablet',
         notes=('Distinct from isosorbide dinitrate and immediate-release products. No administration schedule is assigned.',)),
    make('SIM-ASPIRIN-EC-81', 'aspirin delayed release', '阿司匹林肠溶片81mg',
         'aspirin', 'oral_tablet_delayed_release', '81 mg/tablet', 'mg', 'tablet', 'oral',
         '44730dfb-5062-4015-924b-c30b6c2b19c2', [c('aspirin', 81)],
         dose_basis='mg aspirin in a delayed-release tablet',
         notes=('Not a chewable or immediate-release aspirin product; presence in the catalogue is not cardiovascular indication evidence.',)),
]


def additions():
    rows = deepcopy(_ADDITIONS)
    for row in rows:
        row['catalogue_version'] = VERSION
        row['source_references'][0]['support_sections'] = [
            'Ingredients and appearance', 'Description or active ingredient', 'Dosage form and route']
    return rows


FORMULARY_KEYS = (
    'drug_code', 'name', 'display_name_zh', 'ingredient', 'dosage_form', 'strength',
    'dose_unit', 'quantity_unit', 'allowed_routes', 'route', 'components', 'notes',
    'catalogue_version', 'allowed_dose_units', 'dose_basis', 'label_package', 'active_salt_mass',
)


def formulary_additions():
    return [{key: p.get(key) for key in FORMULARY_KEYS} for p in additions()]

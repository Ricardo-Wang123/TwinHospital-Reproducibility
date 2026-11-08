"""Generate additive catalogue files offline; this script never connects to HIS."""
from copy import deepcopy
import argparse
import hashlib
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sandbox.experiments.linked.medication_catalogue_v6 import VERSION, additions, formulary_additions


def generate(deployment_dir, output):
    dep, output = Path(deployment_dir), Path(output)
    if output.exists():
        raise FileExistsError('Keep earlier catalogue versions')
    names = ['catalog_mapping_v5.json', 'catalogue_metadata_v4.json', 'formulary_v5.json', 'experiment-fixtures-v5.sql']
    before = {name: (dep/name).read_bytes() for name in names}
    mapping = json.loads(before[names[0]])
    metadata = json.loads(before[names[1]])
    formulary = json.loads(before[names[2]])
    products = additions()
    codes = [p['drug_code'] for p in products]
    if len(set(codes)) != len(codes) or set(codes) & set(mapping):
        raise ValueError('New product code collides with existing catalogue')
    old_mapping, old_formulary, old_products = deepcopy(mapping), deepcopy(formulary), deepcopy(metadata['products'])
    old_ids = {v['medicationDefinitionId'] for v in mapping.values()}
    for i, p in enumerate(products, 1):
        quantity = p['quantity_unit']
        base = deepcopy(mapping['SIM-DOXYCYCLINE-HYCLATE-100' if quantity == 'capsule' else 'SIM-METOPROLOL-SUCCINATE-ER-25'])
        if quantity not in {'tablet', 'capsule'} or p['dose_unit'] != 'mg' or p['route'] != 'oral':
            raise ValueError('This builder only handles explicit oral mg solid products')
        native_id = str(8700000000004700000+i)
        if native_id in old_ids:
            raise ValueError('Native product identity collision')
        base.update(medicationDefinitionId=native_id, catalog_bus_no='V6-'+p['drug_code'],
                    catalogue_version=VERSION, source_quantity_unit=quantity,
                    quantity_unit_code='5' if quantity == 'capsule' else '1')
        for key in ('ingredient', 'strength', 'dosage_form', 'dose_basis', 'active_salt_mass', 'label_package', 'source_references'):
            base[key] = deepcopy(p[key])
        mapping[p['drug_code']] = base
    formulary += formulary_additions()
    metadata['products'] += products
    metadata.update(version=VERSION, subset='38 previous products plus 6 independently defined concomitant products',
                    previous_mapping_sha256=hashlib.sha256(before[names[0]]).hexdigest(),
                    additions_only=True, clinical_appropriateness_established=False)
    tail = before[names[3]].decode().split('DO $$ BEGIN', 1)[1]
    tail = 'DO $$ BEGIN'+tail
    for old, new in [('th_catalog_v5','th_catalog_v6'), ('8700000000003700000','8700000000004700000'),
                     ('8700000000003800000','8700000000004800000'), ('8700000000003900000','8700000000004900000'),
                     ('TH-CATALOGUE-3.1',VERSION), ('V5','V6')]:
        tail = tail.replace(old, new)
    def q(value):
        return "'"+str(value).replace("'", "''")+"'"
    values = []
    for i, p in enumerate(products, 1):
        vals = [i, 'V6-'+p['drug_code'], p['display_name_zh']+'〔实验V6〕', p['ingredient'], p['strength'],
                '5' if p['quantity_unit'] == 'capsule' else '1', 'mg', '1', p['dosage_form']]
        values.append('('+','.join(q(v) for v in vals)+')')
    sql = '-- Additive independent virtual products; no default regimen. OFFLINE GENERATED.\nBEGIN;\n'
    sql += 'CREATE TEMP TABLE th_catalog_v6(n int,code text,name text,ingredient text,strength text,quantity_unit text,dose_unit text,route text,form text) ON COMMIT DROP;\n'
    sql += 'INSERT INTO th_catalog_v6 VALUES\n'+',\n'.join(values)+';\n'+tail
    if any(mapping[k] != v for k, v in old_mapping.items()) or formulary[:len(old_formulary)] != old_formulary or metadata['products'][:len(old_products)] != old_products:
        raise ValueError('Existing product changed')
    if any((dep/name).read_bytes() != raw for name, raw in before.items()):
        raise ValueError('Input changed during construction')
    output.mkdir(parents=True, mode=0o700)
    files = {'catalog_mapping_v6.json':mapping, 'catalogue_metadata_v5.json':metadata,
             'formulary_v6.json':formulary, 'experiment-fixtures-v6.sql':sql}
    for name, value in files.items():
        with (output/name).open('x') as f:
            f.write(value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2)+'\n')
    summary = {'new_products':len(products), 'products':len(formulary), 'native_mutations':0,
               'model_calls':0, 'previous_products_unchanged':True, 'clinical_regimens_assigned':0,
               'source_hashes':{name:hashlib.sha256(raw).hexdigest() for name, raw in before.items()},
               'files':{name:hashlib.sha256((output/name).read_bytes()).hexdigest() for name in files}}
    (output/'generation_receipt.json').write_text(json.dumps(summary, indent=2)+'\n')
    return summary


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--deployment-dir', required=True, type=Path)
    p.add_argument('--output', required=True, type=Path)
    args = p.parse_args()
    print(json.dumps(generate(args.deployment_dir, args.output)))

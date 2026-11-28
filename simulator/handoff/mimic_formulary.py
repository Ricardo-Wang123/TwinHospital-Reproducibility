"""Build a shared order-entry directory from MIMIC-IV and its published FHIR terminology."""
import argparse
from collections import defaultdict
import csv
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import tarfile
import tempfile

from sandbox.experiments.linked.contracts import digest
from sandbox.experiments.linked.formulary_search import CODE_SYSTEM, FILENAME, NAME_SYSTEM


VERSION = 'mimic-iv-2.2-ig-1.3.0/formulary-v2'
FREQUENCY_CODES = {code: code for code in (
    'AC BID BIW HS PC PRN Q12H Q1H Q2D Q2H Q3D Q3H Q4H Q5D Q6H Q8D Q8H QAM QD QID QN QOD QW SOS ST STAT TID TIW'
).split()}
ROUTES = set('PO ORAL IV IM SC SUBCUT PR TP TD TRANSDERMAL SL NG NEB INHALATION ID BUCCAL NAS'.split())
ROUTES.update(('IV BOLUS', 'IV DRIP', 'IV INFUSION', 'BOTH EYES', 'RIGHT EYE', 'LEFT EYE',
               'BOTH EARS', 'RIGHT EAR', 'LEFT EAR', 'G TUBE', 'J TUBE'))
DOSE_UNITS = set('mg mcg gm g mL ml L UNIT units IU mEq mmol TAB CAP PUFF NEB PKT DROP SPRY PTCH '
                'LOZ SUPP SYR FILM BAG VIAL AMP INH dose Appl TUBE mcg/hr mcg/h mg/hr mg/day'.split())
DISPENSE_UNITS = set('TAB VIAL mL ml BAG CAP SYR UDCUP PKT TUBE BTL SUPP PTCH INH AMP DBTL NEB AERO '
                    'mg mcg g gm LOZ UNIT dose KIT DRP CAN SPRY DEV JAR CART PEN FILM DROP PAD PACK PKG'.split())
DISPENSE_UNITS.add('HALF TAB')
EXCLUDED = re.compile(r"\b(?:placebo|investigational|non[- ]?formulary|patient.?s? own|patient specific|study drug|"
                      r"research drug|dummy|placeholder|desensitization protocol)\b|^inv\b", re.I)
PRODUCT_BASE = 8710000000000000000
LOCATION = '322954949549363200'


def official_terms(archive):
    with tarfile.open(archive, 'r:gz') as package:
        def resource(name):
            return json.load(package.extractfile('package/' + name))
        info = resource('package.json')
        if (info['name'], info['version'], info['fhirVersions']) != ('kindlab.fhir.mimic', '1.3.0', ['4.0.1']):
            raise ValueError('Expected the MIMIC FHIR R4 implementation guide 1.3.0')
        def vocabulary(system):
            slug = system.rsplit('/', 1)[1]
            data = resource('CodeSystem-' + slug + '.json')
            if data['url'] != system or data['content'] != 'complete':
                raise ValueError('Unexpected MIMIC terminology resource')
            return {c['code'] for c in data['concept']}
        return vocabulary(NAME_SYSTEM), vocabulary(CODE_SYSTEM)


def observations(path):
    opener = gzip.open if path.suffix == '.gz' else open
    with opener(path, 'rt', newline='') as stream:
        rows = csv.reader(stream)
        head = next(rows)
        n, c, s, r, q, u = map(head.index, ('drug', 'formulary_drug_cd', 'prod_strength', 'route',
                                           'form_unit_disp', 'dose_unit_rx'))
        for row in rows:
            yield row[n], row[c], row[s], row[r], row[q], row[u]


def stable_number(identity):
    return int(digest(identity)[:11], 16)


def unit_code(value, route=False):
    return ('TH-MR-' if route else 'TH-MU-') + digest(value)[:12].upper()


def products(rows, names, codes):
    variants = defaultdict(set)
    for raw_name, raw_code, raw_strength, route, quantity, dose in set(rows):
        # FHIR code permits single spaces between tokens. Preserve spelling, case and punctuation.
        name, code = ' '.join(raw_name.split()), ' '.join(raw_code.split())
        strength = raw_strength.strip()
        if (name in names and code in codes and route in ROUTES and dose in DOSE_UNITS and quantity in DISPENSE_UNITS
                and strength and len(strength) <= 150 and any(c.isdigit() for c in strength)
                and all(s.isprintable() for s in (name, code, strength)) and len(name) <= 150
                and not EXCLUDED.search(name)):
            variants[name, code, strength, route, quantity].add(dose)
    catalogue, mapping = [], {}
    ids = set()
    for identity, dose_units in sorted(variants.items()):
        name, code, strength, route, quantity = identity
        n = stable_number(identity)
        if n in ids:
            raise ValueError('Distinct source presentations received the same product identity')
        ids.add(n)
        product_id = 'MIM-' + format(n, '011X')
        units = sorted(dose_units)
        product = {'drug_code': product_id, 'name': name, 'source_formulary_code': code, 'strength': strength,
                   'route': route, 'allowed_routes': [route], 'allowed_dose_units': units,
                   'quantity_unit': quantity}
        catalogue.append(product)
        mapping[product_id] = {'medicationDefinitionId': str(PRODUCT_BASE + n), 'catalog_bus_no': product_id,
            'quantity_unit_code': unit_code(quantity), 'source_quantity_unit': quantity,
            'source_dose_unit': units[0], 'dose_unit_code': unit_code(units[0]),
            'dose_unit_codes': {u: unit_code(u) for u in units}, 'route_codes': {route: unit_code(route, True)},
            'frequency_codes': FREQUENCY_CODES, 'dispenseLocationId': LOCATION, 'catalogue_version': VERSION}
    return catalogue, mapping


def sql_literal(value):
    return "'" + str(value).replace("'", "''") + "'"


def sql_values(rows):
    return ',\n'.join('(' + ','.join(map(sql_literal, row)) + ')' for row in rows)


def fixture(products, mapping):
    terms, records = {}, []
    for p in products:
        if 'source_formulary_code' not in p:
            continue
        sku = p['drug_code']
        m = mapping[sku]
        records.append((int(m['medicationDefinitionId']) - PRODUCT_BASE, sku, p['name'], p['source_formulary_code'],
                        p['strength'], m['quantity_unit_code'], m['dose_unit_code'], m['route_codes'][p['route']]))
        term_rows = [(225, m['route_codes'][p['route']], p['route']),
                     (279125561775755265, m['quantity_unit_code'], p['quantity_unit'])]
        term_rows.extend((900001, unit_code(u), u) for u in p['allowed_dose_units'])
        for system, code, label in term_rows:
            key = system, code
            if key in terms and terms[key] != label:
                raise ValueError('A terminology code has two displays')
            terms[key] = label
    if not records:
        raise ValueError('No source products meet the order-entry contract')
    term_records = [(stable_number([s, c]), s, c, v) for (s, c), v in sorted(terms.items())]
    template = Path(__file__).with_name('mimic_formulary.sql').read_text()
    return template.replace('/* TERMS */', sql_values(term_records)).replace('/* PRODUCTS */', sql_values(records))


def build(guide, prescriptions, destination):
    if destination.exists():
        raise ValueError('Choose a fresh directory for this catalogue version')
    names, codes = official_terms(guide)
    clinical, native = products(observations(prescriptions), names, codes)
    content = {'version': VERSION, 'guide_sha256': hashlib.sha256(guide.read_bytes()).hexdigest(),
               'official_names': sorted(names), 'official_formulary_codes': sorted(codes),
               'native_mapping_sha256': digest(native), 'products': clinical}
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=destination.parent) as temp:
        built = Path(temp) / 'catalogue'
        built.mkdir()
        for filename, data in [(FILENAME, content), ('catalog.json', native)]:
            (built / filename).write_text(json.dumps(data, ensure_ascii=False, separators=(',', ':')) + '\n')
        (built / 'products.sql').write_text(fixture(clinical, native))
        built.rename(destination)
    print(f'{len(names)} official names, {len(codes)} official codes, {len(clinical)} selectable products')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for flag in ('guide', 'prescriptions', 'output'):
        parser.add_argument('--' + flag, required=True, type=Path)
    args = parser.parse_args()
    os.umask(0o077)
    build(args.guide, args.prescriptions, args.output)


if __name__ == '__main__':
    main()

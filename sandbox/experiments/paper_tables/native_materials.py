"""Verified native HTTP material references; no SQL and no invented master IDs."""
from __future__ import annotations

import copy
import hashlib
import json
import os
from decimal import Decimal
from pathlib import Path

from ..contracts import digest, utc_now
from .openhis_native import NativeOpenHISClient


class NativePreparationClient(NativeOpenHISClient):
    def _routes(self):
        tenant = str(self.config.get('tenant_id', 1))
        return super()._routes() | {('GET', '/system/tenant-option/detail-list/' + tenant),
                                  ('GET', '/data-dictionary/disease/information-page')}

    def _archive(self, record):
        if record.get('request', {}).get('path', '').startswith('/system/tenant-option/detail-list/'):
            rows = record.get('response', {}).get('body', {}).get('data')
            if isinstance(rows, list):
                for row in rows:
                    if isinstance(row, dict) and str(row.get('code')) not in ('yb_switch', 'invoiceSwitch', 'orderPricingSource'):
                        row['content'] = '[REDACTED_CONFIGURATION]'
        return super()._archive(record)


class MaterialGap(ValueError):
    pass


_REFERENCE_CACHE = {}


def resolve_native_config(config):
    """Resolve only named native credentials from an explicitly selected .env."""
    result = copy.deepcopy(config)
    values = {}
    if result.get('env_file'):
        from dotenv import dotenv_values
        values = dotenv_values(result['env_file'])
    for name in ('token', 'username', 'password'):
        variable = result.get(name + '_env', 'OPENHIS_' + name.upper())
        if not result.get(name):
            value = os.environ.get(variable) or values.get(variable)
            if value:
                result[name] = value
    return result


def pointer(value, path):
    """RFC 6901 subset, also usable for precise response-field provenance."""
    if path == '':
        return value
    if not isinstance(path, str) or not path.startswith('/'):
        raise MaterialGap('an explicit JSON pointer is required')
    for part in path[1:].split('/'):
        part = part.replace('~1', '/').replace('~0', '~')
        value = value[int(part)] if isinstance(value, list) else value[part]
    return value


def equivalent(left, right):
    if isinstance(left, (dict, list)) or isinstance(right, (dict, list)):
        return left == right
    if left is None or right is None or isinstance(left, bool) or isinstance(right, bool):
        return left == right
    if isinstance(left, str) and isinstance(right, str):
        return left == right
    try:
        return Decimal(str(left)) == Decimal(str(right))
    except Exception:
        return str(left) == str(right)


def reference_value(reference, root, expected=None):
    path = Path(reference['evidence'])
    path = path if path.is_absolute() else Path(root) / path
    stat = path.stat()
    key = (str(path.resolve()), reference.get('sha256'), stat.st_mtime_ns, stat.st_size)
    record = _REFERENCE_CACHE.get(key)
    if record is None:
        raw = path.read_bytes()
        actual_hash = hashlib.sha256(raw).hexdigest()
        if reference.get('sha256') != actual_hash:
            raise MaterialGap('native evidence hash missing or mismatched: ' + str(path))
        record = json.loads(raw)
        _REFERENCE_CACHE[key] = record
    if record.get('response', {}).get('http_status') != 200 or record.get('response', {}).get('body', {}).get('code') not in (0, 200):
        raise MaterialGap('material source is not a successful native HTTP response')
    route = record.get('request', {}).get('path', '')
    if not route.startswith('/') or route.startswith('/sandbox/'):
        raise MaterialGap('material source must identify a native route')
    selected = pointer(record, reference['pointer'])
    if expected is not None and not equivalent(selected, expected):
        raise MaterialGap('native source value differs from the supplied material')
    return selected


def load_manifest(path):
    path = Path(path).resolve()
    value = json.loads(path.read_text(encoding='utf-8-sig'))
    if value.get('schema_version') != 'native-http-materials-1':
        raise MaterialGap('unsupported native material manifest schema')
    value['_root'] = str(path.parent)
    value['_manifest_path'] = str(path)
    value['_manifest_sha256'] = hashlib.sha256(path.read_bytes()).hexdigest()
    return value


def validate_case(case_id, material, manifest, needs):
    gaps = []
    root = manifest['_root']
    def check(fn, label):
        try:
            fn()
        except (OSError, ValueError, TypeError, KeyError, IndexError) as exc:
            gaps.append(label + ': ' + str(exc))
    if material.get('dedicated') is not True:
        gaps.append('dedicated experimental object confirmation missing')
    for key in ('patient_id', 'encounter_id'):
        check(lambda key=key: reference_value(material['binding'][key], root, material[key]), key)
    check(lambda: reference_value(material['binding']['encounter_patient_id'], root, material['patient_id']), 'encounter patient ownership')
    def name_check():
        name = str(reference_value(material['binding']['patient_name'], root))
        if not manifest.get('dedicated_name_prefix') or not name.startswith(manifest['dedicated_name_prefix']):
            raise MaterialGap('patient name is not in the dedicated experimental namespace')
    check(name_check, 'dedicated patient name')
    if 'note' in needs:
        if material.get('emr', {}).get('content_field_origin') == 'experiment_schema_in_generic_native_contextJson':
            if not material['emr'].get('content_field'):
                gaps.append('explicit experimental contextJson field name required')
        else:
            check(lambda: reference_value(material['emr']['field_evidence'], root, material['emr']['content_field']), 'EMR dictionary field')
    if 'order' in needs:
        def order_check():
            dto = material['order_save_request']
            if not dto.get('organizationId') or not dto.get('adviceSaveList'):
                raise MaterialGap('organizationId and adviceSaveList required')
            if len(dto['adviceSaveList']) != len(material['prescription']['items']):
                raise MaterialGap('prescription and native DTO item counts differ')
            # Protocol quantities/doses are explicit constructed input. Native
            # identifiers, price, inventory batch and dictionary codes need API evidence.
            evidence = material['native_field_sources']
            fields = ('patientId', 'encounterId', 'practitionerId', 'founderOrgId', 'accountId',
                      'adviceDefinitionId', 'definitionId', 'definitionDetailId', 'locationId',
                      'unitCode', 'doseUnitCode', 'methodCode', 'rateCode', 'unitPrice', 'lotNumber', 'adviceName')
            reference_value(evidence['/organizationId'], root, dto['organizationId'])
            for index, item in enumerate(dto['adviceSaveList']):
                if str(item.get('dbOpType')) != '1' or item.get('requestId') is not None or item.get('chargeItemId') is not None:
                    raise MaterialGap('only a new native order is a valid starting material')
                if item.get('adviceType') != 1:
                    raise MaterialGap('this adapter currently supports medication orders')
                if str(item.get('patientId')) != str(material['patient_id']) or str(item.get('encounterId')) != str(material['encounter_id']):
                    raise MaterialGap('order DTO is bound to another patient or encounter')
                for field in fields:
                    reference_value(evidence['/adviceSaveList/' + str(index) + '/' + field], root, item[field])
                for field in ('quantity', 'dose', 'totalPrice', 'minUnitQuantity'):
                    if Decimal(str(item[field])) <= 0:
                        raise MaterialGap('nonpositive protocol order quantity/price')
                if Decimal(str(item['totalPrice'])) != Decimal(str(item['quantity'])) * Decimal(str(item['unitPrice'])):
                    raise MaterialGap('explicit native totalPrice must equal quantity times unitPrice')
            if Decimal(str(material['payment_amount'])) != sum(Decimal(str(x['totalPrice'])) for x in dto['adviceSaveList']):
                raise MaterialGap('payment amount differs from selected native order totals')
        check(order_check, 'native order/price/quantity materials')
    return [{'case_id': case_id, 'gap': gap} for gap in gaps]


def requirements(e2e_cases, local_tests):
    needed = {row['case_id']: {'note', 'order'} for row in e2e_cases}
    for row in local_tests:
        category = row['category']
        needed[row['test_id']] = {'note'} if category == 'locked_note' else (set() if category == 'missing_encounter' else {'order'})
        if category == 'patient_bill' and row['is_error']:
            needed[row['test_id'] + ':other'] = {'order'}
    return needed


def validate_manifest(manifest, needed):
    gaps, patients, encounters = [], set(), set()
    if manifest.get('isolated') is not True or not manifest.get('instance_id'):
        gaps.append({'gap': 'isolated instance identity confirmation missing'})
    for case_id, needs in needed.items():
        material = manifest.get('cases', {}).get(case_id)
        if not material:
            gaps.append({'case_id': case_id, 'gap': 'native initial object material missing'})
            continue
        patient, encounter = str(material.get('patient_id')), str(material.get('encounter_id'))
        if patient in patients or encounter in encounters:
            gaps.append({'case_id': case_id, 'gap': 'a native patient/encounter is reused across independent units'})
        patients.add(patient)
        encounters.add(encounter)
        gaps.extend(validate_case(case_id, material, manifest, needs))
    channels = manifest.get('external_channels', {})
    for channel in ('funds', 'insurance', 'invoice'):
        row = channels.get(channel, {})
        if channel == 'funds' and row.get('mode') == 'native_cash_accounting_only':
            # This selects the existing cash accounting branch; it is not a
            # claim that OpenHIS exposes a separate funds-disable option.
            if row.get('external_transfer_requested') is not False:
                gaps.append({'gap': 'funds mode must explicitly exclude external transfers'})
            continue
        if row.get('disabled') is not True or not row.get('evidence'):
            gaps.append({'gap': channel + ' channel disabled evidence missing'})
            continue
        try:
            if not equivalent(reference_value(row['evidence'], manifest['_root']), row['disabled_value']):
                raise MaterialGap('disabled configuration value differs from actual API evidence')
        except (OSError, ValueError, KeyError, TypeError) as exc:
            gaps.append({'gap': channel + ': ' + str(exc)})
    return {'ready': not gaps, 'checked_at': utc_now(), 'n_required_objects': len(needed),
            'manifest_sha256': manifest.get('_manifest_sha256'), 'gaps': gaps}


def attach_materials(e2e_cases, tests, manifest):
    cases, local = copy.deepcopy(e2e_cases), copy.deepcopy(tests)
    for row in cases:
        material = manifest['cases'][row['case_id']]
        row['input']['native_refs'] = {'patient_id': str(material['patient_id']), 'encounter_id': str(material['encounter_id']),
                                     'payment_amount': material['payment_amount']}
        row['input']['prescription'] = copy.deepcopy(material['prescription'])
    for row in local:
        material = manifest['cases'][row['test_id']]
        row['native_refs'] = {'patient_id': str(material['patient_id']), 'encounter_id': str(material['encounter_id']),
                              'payment_amount': material.get('payment_amount')}
        row['prescription'] = copy.deepcopy(material.get('prescription'))
        if row['category'] == 'patient_bill' and row['is_error']:
            other = manifest['cases'][row['test_id'] + ':other']
            row['other_native_refs'] = {'patient_id': str(other['patient_id']), 'encounter_id': str(other['encounter_id']),
                                      'payment_amount': other['payment_amount']}
            row['other_prescription'] = copy.deepcopy(other['prescription'])
    return cases, local


def acquire_readonly(client, destination, *, queries=()):
    """Acquire named read materials only. Never login around captcha or create objects."""
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    results = []
    calls = [('identity', client.identity), ('organizations', client.organizations), ('medications', client.medications)]
    calls += [(kind, lambda kind=kind: client.dictionary(kind)) for kind in ('unit_code', 'method_code', 'rate_code')]
    calls += [(q['name'], lambda q=q: client.request('GET', q['path'], params=q.get('params'))) for q in queries]
    for name, call in calls:
        result = call()
        results.append({'name': name, 'http_status': result['http_status'], 'business_code': result['business_code'],
                        'evidence': result['evidence']})
        if not result['success']:
            break
    (destination / 'acquisition.json').write_text(json.dumps({'recorded_at': utc_now(), 'results': results}, ensure_ascii=False, indent=2), encoding='utf-8')
    return results


def main():
    import argparse
    from .openhis_native import NativeOpenHISClient
    parser = argparse.ArgumentParser(description='Acquire necessary native materials through authenticated read-only APIs; no clinical writes.')
    parser.add_argument('--config', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--queries', help='Optional JSON array of name/path/params for mapped native read routes')
    args = parser.parse_args()
    config = json.loads(Path(args.config).read_text(encoding='utf-8-sig'))
    native = {**config.get('openhis', {}), **config.get('native_http', {})}
    # A stand-alone NativeOpenHISClient configuration is also accepted.
    if not native:
        native = config
    native = resolve_native_config(native)
    native['allow_writes'] = False
    queries = json.loads(Path(args.queries).read_text(encoding='utf-8-sig')) if args.queries else []
    result = acquire_readonly(NativePreparationClient(native, Path(args.output) / 'http'), args.output, queries=queries)
    print(json.dumps({'status': 'materials_read', 'requests': len(result),
                      'successful': sum(r['business_code'] in (0, 200) and r['http_status'] == 200 for r in result),
                      'output': str(Path(args.output).resolve())}, ensure_ascii=False))


if __name__ == '__main__':
    main()

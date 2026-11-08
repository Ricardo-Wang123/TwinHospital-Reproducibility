"""Install the prepared additive formulary on the isolated experiment database."""
import argparse
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sandbox.experiments.linked.contracts import digest
from sandbox.experiments.linked.native_his import NativeOpenHISPort, write_private
from sandbox.experiments.linked.native_observation import CaptureSession, IsolatedObservationDB


def read(path):
    return json.loads(path.read_text())


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def apply(deploy, artifacts, completed_batch, output):
    if output.exists():
        raise FileExistsError('Retain prior native catalogue evidence')
    receipt = read(artifacts/'generation_receipt.json')
    for root, hashes in ((deploy, receipt['source_hashes']), (artifacts, receipt['files'])):
        if any(sha(root/name) != expected for name, expected in hashes.items()):
            raise ValueError('Frozen catalogue source or generated file changed')
    old = read(deploy/'catalog_mapping_v5.json')
    new = read(artifacts/'catalog_mapping_v6.json')
    added = {key: value for key, value in new.items() if key not in old}
    if len(added) != 6 or any(new.get(key) != value for key, value in old.items()):
        raise ValueError('Only the six additive catalogue entries may change')
    done = read(completed_batch/'batch_summary.json')
    if done['native_observation']['status'] != 'closed':
        raise ValueError('Previous native capture has not closed')
    case = next(r for r in done['cases'] if
                r['attempts'].get('linked_baseline_anchor', {}).get('status') == 'completed')
    state = completed_batch/case['case_id']/'linked_baseline_anchor'/'native'
    state_hash = sha(state/'native_state.json')
    db = IsolatedObservationDB(deploy)
    # Reuse the same lock and live owner check as the observed writers.
    lock = CaptureSession(output.parent/'catalogue_v6_install_lock', db=db)
    ids = [str(x['medicationDefinitionId']) for x in old.values()]
    if not all(x.isdigit() for x in ids):
        raise ValueError('Invalid previous medication ID')
    query = ("BEGIN READ ONLY; SELECT coalesce(json_agg(m ORDER BY m.id),'[]'::json) "
             "FROM med_medication_definition m WHERE id IN ("+','.join(ids)+"); COMMIT;")
    with lock.lock():
        control = db.control()
        if control['session_id'] is not None or control['request_id'] is not None or control['quarantined']:
            raise ValueError('Isolated instance has an active or unresolved capture')
        before = json.loads(db.sql(query))
        if len(before) != len(old):
            raise ValueError('The existing native catalogue is incomplete')
        db.sql((artifacts/'experiment-fixtures-v6.sql').read_text())
        after = json.loads(db.sql(query))
        if before != after:
            raise ValueError('An existing medication definition changed')
        port = NativeOpenHISPort(state, credentials_path=deploy/'runtime/accounts/credentials.json',
            catalog_mapping=new, diagnosis_mapping=read(deploy/'diagnosis_mapping_v3.json'))
        binding = next(iter(port.state['bindings'].values()))
        response = port._client('doctor').post('/doctorStationAdvice/pageDoctorStationAdviceCatalog', {
            'patientId': binding['patient_id'], 'encounterId': binding['encounter_id'],
            'keyword': 'V6-', 'pageNo': 1, 'pageSize': 100,
            'productTableNames': ['med_medication_definition']})
    checks = {}
    for key, product in added.items():
        matches = [r for r in response['content']
                   if str(r['productDefinitionId']) == str(product['medicationDefinitionId'])]
        native = matches[0] if len(matches) == 1 else {}
        bands = {b['bandKind']: b for b in native.get('unitBands', [])}
        checks[key] = (len(matches) == 1 and native.get('busNo') == product['catalog_bus_no']
            and native.get('totalVolume') == product['strength']
            and bands.get('DOSE', {}).get('unitCode') == product['dose_unit_code']
            and native.get('unitCode') == product['quantity_unit_code']
            and Decimal(str(native.get('availableQuantity') or 0)) > 0
            and Decimal(str(native.get('unitPrice') or 0)) == 1
            and native.get('dose') is None and native.get('rateCode') is None
            and bands.get('DOSE', {}).get('ratioToSplit') is None)
    summary = {'verified_products': sum(checks.values()), 'added_products': len(added),
        'products': len(new), 'each_product_readback': checks,
        'all_verified': all(checks.values()) and sha(state/'native_state.json') == state_hash,
        'old_definition_hash_unchanged': digest(before) == digest(after),
        'new_patient_encounters': 0, 'new_prescriptions': 0, 'model_calls': 0,
        'clinical_appropriateness_established': False}
    write_private(output, {**summary, 'observed_at': datetime.now(timezone.utc).isoformat(),
                           'native_product_readback': response})
    if not summary['all_verified']:
        raise ValueError('One or more added products did not match the native catalogue readback')
    return summary


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    for name in ('deployment-dir', 'artifacts', 'completed-batch', 'output'):
        p.add_argument('--'+name, type=Path, required=True)
    a = p.parse_args()
    print(json.dumps(apply(a.deployment_dir, a.artifacts, a.completed_batch, a.output)))

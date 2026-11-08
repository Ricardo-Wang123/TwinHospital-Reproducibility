"""Replay paid encounter artifacts over both HIS interfaces without model calls."""

import argparse
from collections import Counter
from contextlib import nullcontext
from decimal import Decimal
import json
import os
from pathlib import Path
from uuid import uuid4

from handoff.evidence import VERSION as EVIDENCE_VERSION, contained_file, paid_index, private_output
from handoff.inputs import digest as file_sha256
from handoff.run import read
from results.render_linked_manuscript import cohort_fingerprint, paid_cohort

from .fhir_his import VERSION, fhir_workflow, operation_definitions
from .native_his import NativeOpenHISPort, VALID_SIGNED_REQUEST_STATES, digest, isolated_port, write_private
from .protocol_his import PaperProtocolPort
from .safety_native import observe_native_state
from .verify_his_interfaces import FhirWireCheck, INTERFACES, require


OPERATIONS = {
    'registration.create', 'queue.enter', 'diagnosis.catalogue', 'handoff.save', 'handoff.read',
    'encounter.begin_consultation', 'consultation.save', 'order.save', 'personnel.registry',
    'order.attest', 'note.save', 'note.revise', 'note.attest', 'payment.prepare', 'payment.commit',
}
DIAGNOSIS_FIELDS = ('conditionDefinitionId', 'conditionCode', 'mainDiagnosisFlag',
                    'verificationStatusEnum', 'diagnosisSortNo')
ORDER_FIELDS = ('productDefinitionId', 'dose', 'doseUnitCode', 'methodCode', 'rateCode', 'quantity', 'unitCode')


def verified_selection(bundle, catalog, diagnoses):
    receipt = read(bundle / 'bundle.json')
    require(receipt['version'] == EVIDENCE_VERSION, 'Unrecognized evidence receipt')

    def verified_path(name):
        path = contained_file(bundle, name, portable=True)
        require(file_sha256(path) == receipt['files'][name], 'Evidence checksum differs from the receipt')
        return path

    audit = read(verified_path('audit.json'))
    paid = paid_cohort(audit)
    spec = verified_path('batch/specification.json')
    require(len(paid) == receipt['verified_paid'] and cohort_fingerprint(paid) == receipt['paid_cohort_sha256']
            and audit.get('audit', audit)['source_specification_sha256'] == file_sha256(spec),
            'Evidence audit is not bound to the frozen paid cohort')
    spec = read(spec)
    require(digest(catalog) == spec['catalog_hash'] and digest(diagnoses) == spec['diagnosis_catalog_hash'],
            'Replay requires the frozen study catalogues')
    selected = {}
    for row in sorted(paid, key=lambda row: row['source_run_file_sha256']):
        selected.setdefault(row['group'], row['source_run_file_sha256'])
    require(selected, 'No paid archived encounters')
    groups = {key: group for group, key in selected.items()}
    manifest = read(verified_path('inputs/manifest.json'))
    cases = {row['case_id']: row for row in manifest['cases']}
    frozen = {row['case_id']: row for row in spec['cases']}
    verified_path('batch/batch_summary.json')
    inputs = []
    for case_id, item in paid_index(bundle / 'batch'):
        name = 'batch/' + item['result_path']
        fingerprint = receipt['files'][name]
        if fingerprint not in groups:
            continue
        group = groups[fingerprint]
        run = read(verified_path(name))
        path = verified_path('inputs/' + cases[case_id]['path'])
        case = read(path)
        require(run['case_id'] == case_id == case['case_id'] and run['group'] == group == case['group']
                and frozen[case_id]['file_sha256'] == file_sha256(path),
                'Run and frozen case identities do not match')
        inputs.append((group, case, run))
    require(Counter(group for group, _, _ in inputs) == Counter(selected.keys()),
            'Expected one bound archived trace per disease stratum')
    return sorted(inputs, key=lambda row: row[0]), selected


def fields(rows, names):
    return Counter(tuple(str(row[name]) for name in names) for row in rows)


def archived_payload(op, response, case, artifacts):
    data = response['data']
    if op == 'registration.create':
        return {'patient_key': case['patient_key'], 'demographics': case['patient_initial']['demographics']}
    if op == 'queue.enter':
        return {'encounter_id': data['encounter_id']}
    if op == 'handoff.save':
        return {key: data[key] for key in ('artifact_id', 'content', 'version')}
    if op == 'handoff.read':
        return {'artifact_id': data['artifact_id']}
    if op == 'consultation.save':
        return {key: data[key] for key in ('artifact_id', 'content', 'transcript')}
    if op == 'order.save':
        version = next(row for row in artifacts['prescription_versions']
                       if row['order_id'] == data['order_id'] and row['native_write_attempted'] is True)
        return {'order_id': data['order_id'], 'items': version['prescription']['items'],
                'parent_order_id': version['native_parent_order_id']}
    if op == 'order.attest':
        return {'order_id': data['order_id'], 'signatory_id': data['signatory_verification']['signatory_id']}
    if op in ('note.save', 'note.revise'):
        version = data['version']
        note = artifacts['note_versions'][version - 1]
        require(note['version'] == version and note['author_role'] == data['content_author_role'],
                'Archived note version or author differs from the save receipt')
        return {'note_id': data['note_id'], 'content': note['content'],
                'expected_version': version - 1, 'parent_version': version - 1 or None}
    if op == 'note.attest':
        return {'note_id': artifacts['attested_note']['note_id'], 'version': data['version']}
    if op == 'payment.prepare':
        return {'order_id': data['order_id']}
    if op == 'payment.commit':
        return {**{key: data[key] for key in ('bill_id', 'amount', 'currency', 'transaction_id')}, 'channel': 'CASH'}
    return {}


def verified_steps(case, run):
    steps = []
    for event in run['events']:
        op = event.get('operation')
        if event['event'] != 'business_operation' or op not in OPERATIONS:
            continue
        response = event['response']
        require(response['readback_verified'] is True, f'Unverified source operation {op}')
        payload = archived_payload(op, response, case, run['artifacts'])
        require(digest(payload) == event['payload_hash'], f'Archived payload hash differs for {op}')
        steps.append((op, event['role'], payload, response))
    counts = Counter(op for op, *_ in steps)
    require(OPERATIONS - {'note.revise'} <= counts.keys() and counts['payment.commit'] == 1
            and steps[-1][0] == 'payment.commit', 'Incomplete archived operation sequence')
    return steps


def replay(native, wire_check, interface, run_id, group, steps, output):
    protocol = PaperProtocolPort(native)
    scope = {'case_id': 'archived-' + group, 'run_id': run_id, 'config_id': 'archived-interface-replay'}
    connection = fhir_workflow(protocol, case_id=scope['case_id'], run_id=run_id) if interface == 'fhir-r4' else nullcontext(protocol)
    records = []
    results = {}
    observation = {}
    try:
        with connection as port:
            if interface == 'fhir-r4' and wire_check:
                wire_check.attach(port)
            for op, role, recorded, source in steps:
                payload = dict(recorded)
                if op == 'queue.enter':
                    payload['encounter_id'] = scope['encounter_id']
                elif op == 'order.attest':
                    payload['signatory_id'] = results['personnel.registry']['data']['signatory_id']
                elif op == 'payment.commit':
                    bill = results['payment.prepare']['data']
                    payload.update({key: bill[key] for key in ('bill_id', 'amount', 'currency')},
                                   transaction_id=run_id + ':payment')
                context = {**scope, 'role': role, 'actor_id': role + '-1'}
                receipt = port.execute(op, payload, context)
                records.append({'operation': op, 'role': role, 'receipt': receipt})
                require(receipt['readback_verified'] is True, f'No verified read-back for {op}')
                data, readback = receipt['data'], receipt['readback']
                if op == 'registration.create':
                    scope.update(patient_id=data['patient_id'], encounter_id=data['encounter_id'])
                elif op in ('handoff.save', 'handoff.read'):
                    require(data == source['data'], 'Handoff differs from the archived artifact')
                elif op == 'consultation.save':
                    require(readback['sidecar_content_hash'] == source['readback']['sidecar_content_hash'],
                            'Consultation narrative or transcript differs')
                elif op in ('order.save', 'order.attest'):
                    require(fields(readback, ORDER_FIELDS) == fields(source['readback'], ORDER_FIELDS),
                            'Native order lines differ from the archived lines')
                elif op in ('note.save', 'note.revise', 'note.attest'):
                    require(data['version'] == source['data']['version']
                            and readback['status'] == source['readback']['status']
                            and readback['content'] == source['readback']['content'], 'Native note version differs')
                results[op] = receipt

        doctor, cashier = (native._client(role) for role in ('doctor', 'cashier'))
        state = observe_native_state(native, scope, {'doctor': doctor, 'cashier': cashier})
        diagnosis = doctor.post('/doctorStationDiagnosis/getEncounterDiagnosis',
                                {'patientId': scope['patient_id'], 'encounterId': scope['encounter_id']})
        observation.update(state=state, diagnosis=diagnosis)
        archived_diagnosis = next(source['readback']['native_diagnoses'] for op, _, _, source in reversed(steps)
                                  if op == 'consultation.save')
        require(fields(diagnosis['diagnosisList'], DIAGNOSIS_FIELDS)
                == fields(archived_diagnosis['diagnosisList'], DIAGNOSIS_FIELDS), 'Native diagnoses differ')
        signed = results['order.attest']['data']
        bill = results['payment.prepare']['data']
        paid = results['payment.commit']['data']
        order_ids = set(signed['native_ids'])
        charge_ids = set(bill['charge_item_ids'])
        require(signed['status'] == 'signed' and signed['order_id'] == bill['order_id'] == paid['order_id'],
                'Payment was not linked to the signed logical order')
        prepared_items = results['payment.prepare']['readback']['native_charge_items']
        require({str(row['serviceId']) for row in prepared_items} == order_ids
                and {str(row['chargeItemId']) for row in prepared_items} == charge_ids, 'Bill does not bind the signed lines')
        native_orders = [row for row in state['orders'] if str(row['requestId']) in order_ids]
        require({str(row['requestId']) for row in native_orders} == order_ids
                and all(str(row['patientId']) == scope['patient_id'] and str(row['encounterId']) == scope['encounter_id']
                        and row['statusEnum'] in VALID_SIGNED_REQUEST_STATES and row['prescriptionNo']
                        and str(row['primaryChargeItemId']) in charge_ids
                        and row['chargeStatusEnum'] == 'SETTLED' for row in native_orders),
                'Fresh read-back did not confirm the signed and settled native orders')
        note = state['note']
        require(note['status'] == 'LOCKED' and str(note['patient_id']) == scope['patient_id']
                and str(note['encounter_id']) == scope['encounter_id']
                and note['content_hash'] == digest(results['note.attest']['readback']['content']),
                'Fresh read-back did not confirm the bound locked note')
        payment_id = paid['payment_id']
        observed = state['payments'][payment_id]
        amount = Decimal(bill['amount'])
        archived_payment = steps[-1][3]['data']
        linked_payments = {key for key, row in state['payments'].items()
                           if charge_ids.intersection(map(str, row.get('charge_item_ids') or []))}
        require(paid['status'] == 'paid' and paid['currency'] == archived_payment['currency'] == 'CNY'
                and amount == Decimal(archived_payment['amount']) == Decimal(str(observed['amount']))
                and linked_payments == {payment_id}
                and set(map(str, observed['charge_item_ids'])) == charge_ids, 'Native cash amount or charges differ')
        settled = [row for row in state['charge_items'] if str(row['chargeItemId']) in charge_ids]
        require({str(row['chargeItemId']) for row in settled} == charge_ids
                and {str(row['serviceId']) for row in settled} == order_ids
                and all(row['statusCode'] == 'SETTLED'
                        and (row['paymentId'] is None or str(row['paymentId']) == payment_id) for row in settled),
                'Fresh read-back did not confirm the settled charge items')
        return [scope['patient_id'], scope['encounter_id'], payment_id]
    finally:
        write_private(output / 'checkpoints.json', {'scope': scope, 'receipts': records, 'observation': observation})


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('bundle', 'catalog', 'diagnoses', 'credentials', 'output'):
        parser.add_argument('--' + name, required=True, type=Path)
    parser.add_argument('--fhir-schema', type=Path)
    args = parser.parse_args()
    port = isolated_port()
    require(port != 28081, 'Use a fresh dedicated smoke project on an alternate loopback port')
    output = private_output(args.output)
    output.mkdir(mode=0o700)
    catalog, diagnoses = read(args.catalog), read(args.diagnoses)
    cases, selected = verified_selection(args.bundle.resolve(), catalog, diagnoses)
    plan = [(group, verified_steps(case, run)) for group, case, run in cases]
    write_private(output / 'source_selection.json', selected)
    wire = FhirWireCheck(args.fhir_schema) if args.fhir_schema else None
    batch = 'archived-interface-' + uuid4().hex
    counts = dict.fromkeys(INTERFACES, 0)
    identities = []
    matched = 0
    for group, steps in plan:
        arms = {}
        for interface in INTERFACES:
            folder = output / group / interface
            native = NativeOpenHISPort(folder / 'native', credentials_path=args.credentials,
                                       base_url=f'http://127.0.0.1:{port}/api/app',
                                       catalog_mapping=catalog, diagnosis_mapping=diagnoses)
            try:
                ids = replay(native, wire, interface, f'{batch}-{group}-{interface}', group, steps, folder)
                identities.append(ids)
                counts[interface] += 1
                arms[interface] = True
            except Exception as exc:
                write_private(folder / 'failure.json', {'type': type(exc).__name__, 'message': str(exc)})
                arms[interface] = False
        matched += all(arms.values())
        print(json.dumps({'stratum': group, 'matched_source': arms}), flush=True)
    distinct = len(identities) == len(plan) * len(INTERFACES) and all(
        len(set(column)) == len(identities) for column in zip(*identities))
    report = {'scope': 'paired replay of archived accepted workflow operations', 'model_calls': 0,
              'archived_pairs': len(plan), 'matched_pairs': matched, 'completed_encounters': counts,
              'distinct_native_identities': distinct,
              'source_diagnoses': sum(len(source['readback']['native_diagnoses']['diagnosisList'])
                                      for _, steps in plan for op, _, _, source in steps if op == 'consultation.save'),
              'source_signed_lines': sum(len(source['readback']) for _, steps in plan
                                         for op, _, _, source in steps if op == 'order.attest'),
              'source_note_versions': sum(op in ('note.save', 'note.revise')
                                          for _, steps in plan for op, *_ in steps),
              'fhir_bridge_version': VERSION, 'operation_definitions_sha256': digest(operation_definitions())}
    write_private(output / 'aggregate.json', report)
    require(matched == len(plan) and distinct, 'Archived interface replay did not match the source')
    if wire:
        write_private(output / 'fhir_schema.json', wire.summary())


if __name__ == '__main__':
    main()

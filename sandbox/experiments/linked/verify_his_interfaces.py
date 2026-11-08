"""Compare the two HIS interfaces on isolated, scripted fictional encounters."""

import argparse
from collections import Counter
from contextlib import nullcontext
from decimal import Decimal
from hashlib import sha256
from io import BytesIO
import json
from pathlib import Path
from uuid import uuid4
from zipfile import ZipFile

from .cases import FORMULARY
from .fhir_his import MEDIA_TYPE, VERSION, fhir_workflow, operation_definitions
from .native_his import NativeBlocked, NativeOpenHISPort, VALID_SIGNED_REQUEST_STATES, digest, isolated_port, write_private
from .protocol_his import PaperProtocolPort
from .safety_native import DEPLOY, observe_native_state


FIXTURES = (
    ('type2_diabetes', 'E11.9', 'SIM-METFORMIN-500'),
    ('heart_failure', 'I50.9', 'SIM-FUROSEMIDE-20'),
    ('copd', 'J44.9', 'SIM-SALBUTAMOL-100'),
    ('pneumonia', 'J18.9', 'SIM-AMOXICILLIN-500'),
    ('uti', 'N39.0', 'SIM-NITROFURANTOIN-100'),
)
INTERFACES = ('native', 'fhir-r4')
R4_SCHEMA_URL = 'https://hl7.org/fhir/R4/fhir.schema.json.zip'
R4_SCHEMA_SHA256 = '75e5560da3cf503895a44c8ca7af17a83b4cca6c2cb5ba1883d2aec0d1cb5ac6'


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def verify_parameters(resource, definition, direction):
    fields = {row['name']: row for row in definition['parameter'] if row['use'] == direction}
    counts = Counter()
    for row in resource.get('parameter', []):
        name = row['name']
        require(name in fields, f'Undeclared FHIR {direction} parameter: {name}')
        kind = fields[name]['type']
        require('value' + kind[0].upper() + kind[1:] in row, f'Incorrect FHIR parameter type: {name}')
        counts[name] += 1
    for name, field in fields.items():
        require(field['min'] <= counts[name] <= int(field['max']), f'Incorrect FHIR parameter count: {name}')


class FhirWireCheck:
    """Check the actual loopback HTTP exchange using HL7's schema and served metadata."""

    def __init__(self, schema_path):
        from jsonschema import Draft6Validator

        archive = schema_path.read_bytes()
        require(sha256(archive).hexdigest() == R4_SCHEMA_SHA256, 'Unexpected HL7 R4 schema archive checksum')
        with ZipFile(BytesIO(archive)) as bundle:
            definitions = json.loads(bundle.read('fhir.schema.json'))['definitions']
        self.validators = {kind: Draft6Validator({'$ref': f'#/definitions/{kind}', 'definitions': definitions})
                           for kind in ('CapabilityStatement', 'OperationDefinition', 'Parameters', 'OperationOutcome')}
        self.resources = Counter()
        self.operations = Counter()

    def validate(self, resource, kind):
        self.validators[kind].validate(resource)
        self.resources[kind] += 1

    def attach(self, port):
        def get(path, kind):
            response = port.session.get(port.base_url + path, timeout=10, allow_redirects=False)
            require(response.status_code == 200 and response.headers.get('Content-Type') == MEDIA_TYPE,
                    f'Invalid discovery response for {path}')
            resource = response.json()
            self.validate(resource, kind)
            return resource

        capability = get('/metadata', 'CapabilityStatement')
        require(capability['fhirVersion'] == '4.0.1', 'Expected FHIR 4.0.1 capability metadata')
        definitions = {}
        for operation in capability['rest'][0]['operation']:
            code = operation['name']
            definition = get('/OperationDefinition/' + code, 'OperationDefinition')
            require(code not in definitions and definition['url'] == operation['definition']
                    and definition['id'] == definition['code'] == code and definition['system'] is True,
                    f'OperationDefinition does not match its discovery record: {code}')
            definitions[code] = definition
        require(set(definitions) == set(operation_definitions()), 'Incomplete FHIR operation discovery')

        def inspect(response, **_kwargs):
            request = response.request
            code = request.url.removeprefix(port.base_url + '/$')
            require(request.method == 'POST' and code in definitions
                    and request.headers.get('Content-Type') == response.headers.get('Content-Type') == MEDIA_TYPE,
                    'Unexpected FHIR workflow exchange')
            payload = json.loads(request.body)
            self.validate(payload, 'Parameters')
            verify_parameters(payload, definitions[code], 'in')
            resource = response.json()
            if response.status_code == 200:
                self.validate(resource, 'Parameters')
                verify_parameters(resource, definitions[code], 'out')
            else:
                self.validate(resource, 'OperationOutcome')
            self.operations[code] += 1

        port.session.hooks['response'].append(inspect)

    def summary(self):
        require(set(self.operations) == set(operation_definitions()), 'Some FHIR operations were not exercised')
        return {'scope': 'FHIR R4 JSON Schema and published operation parameter checks', 'model_calls': 0,
                'schema_url': R4_SCHEMA_URL, 'schema_archive_sha256': R4_SCHEMA_SHA256,
                'operation_definitions_sha256': digest(operation_definitions()),
                'resources_validated': dict(sorted(self.resources.items())),
                'requests_by_operation': dict(sorted(self.operations.items()))}


def fixture_cases():
    products = {row['drug_code']: row for row in FORMULARY}
    for group, code, product_code in FIXTURES:
        product = products[product_code]
        yield {'case_id': f'fictional-{group}', 'diagnosis_code': code,
               'demographics': {'age': 55, 'sex': 'F'},
               'order_item': {**product, 'quantity': 1 if product['quantity_unit'] == 'inhaler' else 14,
                              'duration_days': 7},
               'handoff': {'text': 'Fictional outpatient workflow fixture.'},
               'initial_note': {'text': 'Initial fictional note.'},
               'revised_note': {'text': 'Revised fictional note with a verified order.'}}


def verify_assignment(case, run_id, output, interface, native, *, wire_check=None):
    protocol = PaperProtocolPort(native)
    scope = {'case_id': case['case_id'], 'run_id': run_id, 'config_id': 'scripted-interface-check'}
    trace = []
    evidence = {}
    connection = fhir_workflow(protocol, case_id=case['case_id'], run_id=run_id) if interface == 'fhir-r4' else nullcontext(protocol)
    with connection as port:
        if interface == 'fhir-r4' and wire_check is not None:
            wire_check.attach(port)

        def perform(operation, payload, role='doctor', *, key=None):
            context = {**scope, 'role': role, 'actor_id': role + '-1'}
            if key is not None:
                context['idempotency_key'] = key
            receipt = port.execute(operation, payload, context)
            require(receipt.get('readback_verified'), f'{operation} has no verified read-back')
            trace.append({'operation': operation, 'status': receipt['status'], 'native_effect': receipt['native_effect']})
            return receipt

        def observe():
            return observe_native_state(native, scope, {role: native._client(role) for role in ('doctor', 'cashier')})

        def verify_block(operation, payload, role, checkpoint):
            before = observe()
            try:
                perform(operation, payload, role)
            except NativeBlocked as exc:
                trace.append({'operation': operation, 'status': 'blocked', 'reason': str(exc)})
            else:
                raise RuntimeError(f'{checkpoint} was accepted')
            after = observe()
            evidence[checkpoint] = {'before': before, 'after': after}
            require(before == after, f'{checkpoint} changed native records')
            return before

        try:
            registration = perform('registration.create', {'patient_key': case['case_id'],
                'demographics': case['demographics']}, 'administrator')['data']
            scope.update(patient_id=registration['patient_id'], encounter_id=registration['encounter_id'])
            perform('queue.enter', {'encounter_id': scope['encounter_id']}, 'administrator')
            perform('handoff.save', {'artifact_id': 'intake', 'version': 1, 'content': case['handoff']}, 'nurse')
            handoff = perform('handoff.read', {'artifact_id': 'intake'})['data']['content']
            require(handoff == case['handoff'], 'Physician did not retrieve the saved handoff')
            perform('encounter.begin_consultation', {})
            catalogue = perform('diagnosis.catalogue', {})['data']['items']
            diagnosis = next(row for row in catalogue if row['code'] == case['diagnosis_code'])
            perform('consultation.save', {'artifact_id': 'consultation', 'transcript': [], 'content': {
                'diagnoses': [{**diagnosis, 'primary': True, 'certainty': 'working'}]}})
            draft = perform('order.save', {'order_id': 'rx', 'items': [case['order_item']], 'parent_order_id': None})
            draft_ids = set(draft['data']['native_ids'])
            unsigned = verify_block('payment.prepare', {'order_id': 'rx'}, 'cashier', 'unsigned_charge')
            require({str(row['requestId']) for row in unsigned['orders']} == draft_ids
                    and all(row['statusEnum'] == 'DRAFT' for row in unsigned['orders']), 'Expected a native draft before signing')
            signer = perform('personnel.registry', {})['data']['signatory_id']
            signed = perform('order.attest', {'order_id': 'rx', 'signatory_id': signer})['data']
            require(signed['status'] == 'signed' and set(signed['native_ids']) == draft_ids, 'Signed order does not match draft')

            note = perform('note.save', {'note_id': 'note', 'content': case['initial_note'],
                'expected_version': 0, 'parent_version': None})['data']
            revised = perform('note.revise', {'note_id': 'note', 'content': case['revised_note'],
                'expected_version': note['version'], 'parent_version': note['version']})
            locked = perform('note.attest', {'note_id': 'note', 'version': revised['data']['version']})['data']
            verify_block('note.revise', {'note_id': 'note', 'content': case['initial_note'],
                'expected_version': locked['version'], 'parent_version': locked['version']}, 'doctor', 'locked_note')

            prepared = perform('payment.prepare', {'order_id': 'rx'}, 'cashier')
            bill = prepared['data']
            charge_items = prepared['readback']['native_charge_items']
            require({str(row['serviceId']) for row in charge_items} == draft_ids
                    and {str(row['chargeItemId']) for row in charge_items} == set(bill['charge_item_ids']),
                    'Prepared bill does not bind the signed order')
            payload = {'bill_id': bill['bill_id'], 'amount': bill['amount'], 'currency': bill['currency'],
                       'channel': 'CASH', 'transaction_id': f'{run_id}:{case["case_id"]}:payment'}
            paid = perform('payment.commit', payload, 'cashier', key='medication-cash')
            state = observe()
            repeated = perform('payment.commit', payload, 'cashier', key='medication-cash')
            repeated_state = observe()
            payment_id = paid['data']['payment_id']
            require(paid['data']['status'] == 'paid' and repeated['status'] == 'replayed'
                    and repeated['data']['payment_id'] == payment_id and state == repeated_state,
                    'The same transaction produced a different payment or native state')
            charge_ids = set(bill['charge_item_ids'])
            settled = state['payments'].get(payment_id)
            require(settled and payment_id not in unsigned['payments']
                    and {str(item) for item in settled['charge_item_ids']} == charge_ids
                    and Decimal(str(settled['amount'])) == Decimal(bill['amount']),
                    'Fresh read-back did not confirm the payment identity, charges and amount')
            settled_items = [row for row in state['charge_items'] if str(row['chargeItemId']) in charge_ids]
            require({str(row['chargeItemId']) for row in settled_items} == charge_ids
                    and {str(row['serviceId']) for row in settled_items} == draft_ids
                    and all(row['statusCode'] == 'SETTLED' for row in settled_items),
                    'Fresh read-back did not confirm settled charge items for the signed order')
            require({str(row['requestId']) for row in state['orders']} == draft_ids
                    and all(str(row['patientId']) == scope['patient_id'] and str(row['encounterId']) == scope['encounter_id']
                            and row['statusEnum'] in VALID_SIGNED_REQUEST_STATES and row['prescriptionNo']
                            and str(row['primaryChargeItemId']) in charge_ids and row['chargeStatusEnum'] == 'SETTLED'
                            for row in state['orders']), 'Signed order ownership or state was not verified')
            native_note = state['note']
            require(native_note['status'] == 'LOCKED' and str(native_note['patient_id']) == scope['patient_id']
                    and str(native_note['encounter_id']) == scope['encounter_id']
                    and native_note['content_hash'] == digest(revised['readback']['content'])
                    and locked['version'] == 2, 'Locked note ownership, version or content was not verified')
            evidence.update(after_settlement=state, after_duplicate=repeated_state)
            return {'complete': True, 'native_ids': [scope['patient_id'], scope['encounter_id'], payment_id],
                    'outcome': {'handoff_sha256': digest(handoff), 'diagnosis_code': diagnosis['code'],
                                'order_lines': [{key: row[key] for key in ('statusEnum', 'chargeStatusEnum', 'dose',
                                                 'doseUnitCode', 'quantity', 'unitCode')} for row in state['orders']],
                                'locked_note_sha256': native_note['content_hash'], 'note_version': locked['version'],
                                'medication_amount_cny': format(Decimal(bill['amount']), 'f'),
                                'unsigned_charge_blocked': True, 'locked_note_preserved': True,
                                'duplicate_cash_replayed': True}}
        finally:
            write_private(output / 'checkpoints.json', {'scope': scope, 'trace': trace, 'evidence': evidence})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--catalog', default=DEPLOY / 'catalog_mapping.json', type=Path)
    parser.add_argument('--credentials', type=Path)
    parser.add_argument('--fhir-schema', type=Path, help='Check live FHIR messages against the HL7 R4 JSON Schema ZIP')
    args = parser.parse_args()
    args.output.mkdir(mode=0o700, parents=True, exist_ok=False)
    cases = list(fixture_cases())
    mapping = json.loads(args.catalog.read_text())
    missing = {case['order_item']['drug_code'] for case in cases} - set(mapping)
    require(not missing, f'Install the corresponding fictional formulary before verifying: {sorted(missing)}')
    wire_check = FhirWireCheck(args.fhir_schema) if args.fhir_schema else None
    run = 'interface-check-' + uuid4().hex
    base_url = f'http://127.0.0.1:{isolated_port()}/api/app'
    records = []
    for case in cases:
        arms = {}
        for interface in INTERFACES:
            folder = args.output / case['case_id'] / interface
            native = NativeOpenHISPort(folder / 'native', catalog_mapping=mapping,
                                       credentials_path=args.credentials, base_url=base_url)
            try:
                arms[interface] = verify_assignment(case, f'{run}-{interface}', folder, interface, native,
                                                    wire_check=wire_check)
            except Exception as exc:
                arms[interface] = {'complete': False, 'error_type': type(exc).__name__, 'error': str(exc)}
        matched = all(row['complete'] for row in arms.values()) and arms['native']['outcome'] == arms['fhir-r4']['outcome']
        records.append({'fixture': case['case_id'], 'input_sha256': digest(case), 'matched': matched, 'arms': arms})
        print(json.dumps({'fixture': case['case_id'], 'matched': matched,
                          'complete': {key: row['complete'] for key, row in arms.items()}}), flush=True)
    completed = [row for record in records for row in record['arms'].values() if row['complete']]
    ids = [tuple(row['native_ids']) for row in completed]
    distinct = len(ids) == len(cases) * len(INTERFACES) and all(len(set(column)) == len(ids) for column in zip(*ids))
    report = {
        'scope': 'independent fictional transport and state verification', 'model_calls': 0,
        'fixture_pairs': len(cases), 'matched_pairs': sum(row['matched'] for row in records),
        'completed_encounters': {interface: sum(record['arms'][interface]['complete'] for record in records)
                                 for interface in INTERFACES},
        'distinct_native_identities': distinct,
        'verified_encounter_checks': {check: sum(row['outcome'][check] for row in completed) for check in (
            'unsigned_charge_blocked', 'locked_note_preserved', 'duplicate_cash_replayed')},
        'fhir_bridge_version': VERSION,
        'operation_definitions_sha256': digest(operation_definitions()),
    }
    write_private(args.output / 'summary.json', {**report, 'pairs': records})
    write_private(args.output / 'aggregate.json', report)
    require(report['matched_pairs'] == len(cases) and distinct, 'Transport or native state comparison did not pass')
    if wire_check is not None:
        write_private(args.output / 'fhir_schema.json', wire_check.summary())


if __name__ == '__main__':
    main()

from collections import Counter
from datetime import datetime, timezone
from hashlib import sha256

import pytest

from sandbox.experiments.linked import native_his
from sandbox.experiments.linked.continuation import EncounterContinuation, EncounterDeferred
from sandbox.experiments.linked.native_his import NativeOpenHISPort, NativeTransportError, digest


CONTEXT = {'case_id': 'fixture', 'run_id': 'batch:fixture', 'role': 'cashier',
           'idempotency_key': 'register-one', 'action_request_id': 'request-one'}
PAYLOAD = {'patient_key': 'fixture-key', 'demographics': {'age': 45, 'sex': 'female'}}


class Midnight(datetime):
    @classmethod
    def now(cls, tz=None):
        return cls(2026, 11, 26, 16, 1, tzinfo=timezone.utc).astimezone(tz)


class RegistrationClient:
    def __init__(self, interrupted=None):
        self.patient = None
        self.registered = False
        self.draft_created = False
        self.calls = Counter()
        self.interrupted = interrupted

    def post(self, path, payload):
        endpoint = path.split('/')[-1]
        self.calls[endpoint] += 1
        if endpoint == 'getPatientPage':
            return {'content': [{'id': '101', 'name': self.patient['name']}]
                    if self.patient and payload['keyword'] in (self.patient['name'], self.patient['idCard']) else []}
        if endpoint == 'createPatient':
            self.patient = payload
            self.drop_reply(endpoint)
            return {'patientId': '101'}
        if endpoint == 'getPatient':
            assert payload['value'] == '101'
            return self.patient
        if endpoint == 'loadRegisterFormInit':
            return {'contractOptions': [{'value': 'cash', 'label': '自费'}],
                    'serviceTypeOptions': [{'value': '322957264586018816'}]}
        if endpoint == 'listBookableSlotHints':
            assert payload['registrationDate'] == '2026-11-27'
            return {'hints': []}
        if endpoint == 'preRegisterSettlement':
            self.draft_created = True
            self.drop_reply(endpoint)
            return {'paymentDraftId': '102', 'encounterId': '103', 'totalAmount': 9}
        if endpoint == 'payRegisterFee':
            assert payload['paymentDraftId'] == '102'
            self.registered = True
            self.drop_reply(endpoint)
            return {'paymentId': '104', 'encounterId': '103'}
        if endpoint == 'pageClinicRegisters':
            assert payload['patientId'] == '101'
            assert payload['dateFrom'] <= '2026-11-27' <= payload['dateTo']
            self.drop_reply(endpoint)
            return {'content': [{'encounterId': '103', 'statusEnum': 'REGISTERED', 'paymentId': '104'}]
                    if self.registered else [{'encounterId': '103', 'statusEnum': 'PLANNED',
                                               'paymentDraftId': '102', 'amount': 9}] if self.draft_created else []}
        pytest.fail(f'Unexpected operation {endpoint}')

    def drop_reply(self, endpoint):
        if endpoint == self.interrupted:
            self.interrupted = None
            raise NativeTransportError('Unconfirmed fixture response')


@pytest.mark.parametrize('interrupted', [None, 'createPatient', 'preRegisterSettlement',
                                         'payRegisterFee', 'pageClinicRegisters'])
def test_registration_uses_hospital_day_and_resumes_same_encounter(tmp_path, monkeypatch, interrupted):
    monkeypatch.setattr(native_his.dt, 'datetime', Midnight)
    client = RegistrationClient(interrupted)
    continuation_path = tmp_path / 'continuation.json'
    native_path = tmp_path / 'native'

    def open_encounter():
        native = NativeOpenHISPort(native_path, clients={'cashier': client}, defer_consultation_start=True)
        continuation = EncounterContinuation(continuation_path, {'run': 'fixture'})
        return continuation, continuation.backend(native)

    continuation, backend = open_encounter()
    if interrupted:
        with pytest.raises(EncounterDeferred) as failure:
            backend.execute('registration.create', PAYLOAD, CONTEXT)
        assert failure.value.issue['reason_code'] == 'registration_readback_pending'
        continuation, backend = open_encounter()
    receipt = backend.execute('registration.create', PAYLOAD, CONTEXT)
    assert receipt['data'] == {'patient_id': '101', 'encounter_id': '103'}
    assert receipt['readback']['registration_payment']['paymentId'] == '104'
    assert client.calls['createPatient'] == client.calls['preRegisterSettlement'] == client.calls['payRegisterFee'] == 1
    if interrupted:
        assert continuation.data['interruptions'][0]['status'] == 'response_recovered'


def test_legacy_failed_readback_recovers_without_writing_again(tmp_path):
    client = RegistrationClient()
    key = CONTEXT['run_id'] + ':' + CONTEXT['case_id']
    code = sha256(key.encode()).hexdigest()
    client.patient = {'idCard': 'fixture-identity', 'name': '虚构实验' + code[:10],
                      'phone': '000' + str(int(code[:8], 16) % 100000000).zfill(8)}
    client.registered = True
    port = NativeOpenHISPort(tmp_path, clients={'cashier': client}, defer_consultation_start=True)
    port.state['idempotency'][CONTEXT['idempotency_key']] = {
        'state': 'unresolved', 'operation': 'registration.create',
        'fingerprint': digest(['registration.create', PAYLOAD, None, None, 'cashier']),
    }
    port._save()
    port._event({'at': '2026-11-27T00:01:00', 'run_id': CONTEXT['run_id'], 'case_id': CONTEXT['case_id'],
                 'operation': 'registration.create', 'error': 'Registered encounter not observed'})
    outcome = port.execute('registration.create', PAYLOAD, CONTEXT)
    assert outcome['data']['encounter_id'] == '103'
    assert not any(client.calls[x] for x in ('createPatient', 'preRegisterSettlement', 'payRegisterFee'))

import unittest
from unittest.mock import patch

from sandbox.experiments.linked.native_his import NativeBlocked, NativeError, NativeRejected
from sandbox.experiments.linked.wst_workflow import (
    PATIENT, header, patient_body, registration_name, validate_patient, wst_workflow,
)


class Backend:
    def __init__(self):
        self.calls = []

    def execute(self, operation, payload, context):
        self.calls.append((operation, payload, context))
        return {'readback_verified': True, 'status': 'executed',
                'readback': {'patient': {'name': registration_name(context)}},
                'data': {'patient_id': 'p-1', 'encounter_id': 'e-1', 'signatory_id': 's-1'}}

    def snapshot(self, case_id):
        return {'case_id': case_id}


class WstWorkflowTests(unittest.TestCase):
    def test_all_hospital_steps_use_real_soap_without_fhir_requests(self):
        backend = Backend()
        scope = {'case_id': 'case-1', 'run_id': 'run-1', 'role': 'doctor', 'actor_id': 'doctor-1'}
        calls = [
            ('encounter.begin_consultation', {}), ('diagnosis.catalogue', {}),
            ('consultation.save', {'artifact_id': 'c', 'content': {}, 'transcript': []}),
            ('order.save', {'order_id': 'o', 'parent_order_id': None,
                            'items': [{'drug_code': 'SIM-1', 'dose': 5, 'dose_unit': 'mg'}]}),
            ('order.attest', {'order_id': 'o', 'signatory_id': 's-1'}),
            ('note.save', {'note_id': 'n', 'content': {}, 'expected_version': 0}),
            ('note.revise', {'note_id': 'n', 'content': {}, 'expected_version': 1, 'parent_version': None}),
            ('note.attest', {'note_id': 'n', 'version': 2}),
            ('payment.prepare', {'order_id': 'o'}),
            ('payment.commit', {'bill_id': 'b', 'amount': '1.00', 'currency': 'CNY',
                                'channel': 'CASH', 'transaction_id': 't'}),
        ]
        with patch('sandbox.experiments.linked.fhir_his._FhirPort.execute', side_effect=AssertionError('FHIR used')):
            with wst_workflow(backend, case_id='case-1', run_id='run-1') as port:
                port.execute('registration.create', {'patient_key': 'external-patient', 'demographics': {}}, scope)
                scope.update(patient_id='p-1', encounter_id='e-1')
                port.execute('personnel.registry', {}, scope)
                for operation, payload in calls:
                    port.execute(operation, payload, scope)
                    self.assertEqual(backend.calls[-1], (operation, payload, scope))
                wire = port.snapshot('case-1')['standard_exchanges']
                self.assertEqual(len(wire['wst846']), 11)
                self.assertEqual(wire['fhir_r4'], [])
                self.assertEqual([x['service'] for x in wire['wst846'] if x['national_service']],
                                 ['PatientInfoRegister', 'OrderInfoAdd'])

    def test_patient_standard_fields_are_cross_checked(self):
        scope = {'case_id': 'c', 'run_id': 'r'}
        payload = {'patient_key': 'p', 'demographics': {}}
        root = header(PATIENT, 'message-1')
        patient_body(root, payload, scope)
        validate_patient(root, payload, scope)
        with self.assertRaisesRegex(ValueError, 'patient fields'):
            validate_patient(root, {**payload, 'patient_key': 'other-patient'}, scope)

    def test_rejections_and_uncertainty_keep_their_types(self):
        backend = Backend()
        scope = {'case_id': 'c', 'run_id': 'r', 'role': 'doctor', 'actor_id': 'doctor-1',
                 'patient_id': 'p', 'encounter_id': 'e'}
        with wst_workflow(backend, case_id='c', run_id='r') as port:
            for error in [NativeBlocked('blocked'), NativeRejected('/note', 'rejected'), NativeError('unknown')]:
                with patch.object(backend, 'execute', side_effect=error):
                    with self.assertRaises(type(error)) as found:
                        port.execute('note.attest', {'note_id': 'n', 'version': 1}, scope)
                    self.assertIs(type(found.exception), type(error))

    def test_cross_assignment_cannot_reach_backend(self):
        backend = Backend()
        scope = {'case_id': 'c', 'run_id': 'other-r', 'role': 'doctor', 'actor_id': 'doctor-1',
                 'patient_id': 'p', 'encounter_id': 'e'}
        with wst_workflow(backend, case_id='c', run_id='r') as port:
            with self.assertRaises(NativeBlocked):
                port.execute('note.attest', {'note_id': 'n', 'version': 1}, scope)
        self.assertEqual(backend.calls, [])

import unittest
from unittest.mock import patch

from sandbox.experiments.linked.fhir_his import fhir_workflow
from sandbox.experiments.linked.native_his import NativeBlocked, NativeRejected, NativeError
from sandbox.experiments.linked.wst846_server import wst_order_workflow


class Backend:
    def __init__(self):
        self.calls = []

    def execute(self, operation, payload, context):
        self.calls.append((operation, payload, context))
        return {'status':'executed', 'readback_verified':True,
                'readback':{'patient':{'name':'虚构患者'}},
                'data':{'status':'saved','signatory_id':'native-physician-fixture'}}


class WstServiceTests(unittest.TestCase):
    def test_registration_uses_fhir_and_order_uses_real_soap_http(self):
        backend = Backend()
        scope = {'case_id':'case-1','run_id':'run-1','role':'cashier','actor_id':'cashier-1'}
        with fhir_workflow(backend, case_id='case-1', run_id='run-1') as fhir:
            with wst_order_workflow(fhir, case_id='case-1', run_id='run-1') as port:
                with patch.object(fhir.session,'post',wraps=fhir.session.post) as fhir_http, \
                        patch.object(port.http,'post',wraps=port.http.post) as soap_http:
                    port.execute('registration.create',{'patient_key':'patient-fixture','demographics':{}},scope)
                    scope.update(role='doctor',actor_id='doctor-1',patient_id='p-1',encounter_id='e-1')
                    port.execute('personnel.registry',{},scope)
                    payload = {'order_id':'order-1','items':[{'drug_code':'SIM-1','dose':5,'dose_unit':'mg'}]}
                    port.execute('order.save',payload,scope)
                self.assertEqual(fhir_http.call_count,1)
                self.assertEqual(soap_http.call_count,1)
                self.assertEqual(port.exchanges[0]['acknowledgement'],'AA')
                self.assertEqual(backend.calls[-1],('order.save',payload,scope))

    def test_rejected_and_uncertain_hospital_writes_remain_distinct(self):
        backend = Backend()
        scope = {'case_id':'case-1','run_id':'run-1','role':'doctor','actor_id':'doctor-1',
                 'patient_id':'p-1','encounter_id':'e-1'}
        payload={'order_id':'order-1','items':[{'drug_code':'SIM-1','dose':5,'dose_unit':'mg'}]}
        with fhir_workflow(backend,case_id='case-1',run_id='run-1') as fhir:
            with wst_order_workflow(fhir,case_id='case-1',run_id='run-1') as port:
                port.patient_names[('case-1','run-1')]='虚构患者'
                port.author_ids[('case-1','run-1')]='native-physician-fixture'
                for exception in [NativeBlocked('blocked'), NativeRejected('/orders','rejected'), NativeError('uncertain')]:
                    with patch.object(backend,'execute',side_effect=exception):
                        with self.assertRaises(type(exception)) as found:
                            port.execute('order.save',payload,scope)
                        self.assertIs(type(found.exception),type(exception))

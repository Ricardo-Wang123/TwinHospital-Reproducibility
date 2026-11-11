"""The standard wire codec must not change gateway payload/correlation hashes."""
import unittest

from sandbox.experiments.linked.fhir_his import fhir_workflow


class Backend:
    def execute(self, operation, payload, context):
        self.received = (operation,payload,context)
        return {'status':'executed','data':{'order_id':payload['order_id']}}


class FhirOptionalValuesTests(unittest.TestCase):
    def test_missing_explicit_null_and_present_optional_values_remain_distinct(self):
        backend=Backend()
        scope={'case_id':'case-1','run_id':'run-1','role':'doctor','actor_id':'doctor-1',
               'patient_id':'p-1','encounter_id':'e-1','config_id':None}
        with fhir_workflow(backend,case_id='case-1',run_id='run-1') as port:
            for extra in [{},{'parent_order_id':None},{'parent_order_id':'prior-order'}]:
                payload={'order_id':'order-1','items':[],**extra}
                port.execute('order.save',payload,scope)
                self.assertEqual(backend.received,('order.save',payload,scope))

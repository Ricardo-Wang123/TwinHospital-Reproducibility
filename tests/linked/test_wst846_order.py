import copy
import unittest

from sandbox.experiments.linked.wst846_order import encode_order, decode_order


class OrderInfoAddTests(unittest.TestCase):
    def setUp(self):
        self.context = {'case_id':'case-1', 'run_id':'run-1', 'patient_id':'p-1',
                        'encounter_id':'e-1', 'role':'doctor', 'actor_id':'doctor-1'}
        self.payload = {'order_id':'order-1', 'items':[{'drug_code':'SIM-METFORMIN-500',
            'dose':500, 'dose_unit':'mg', 'route':'oral', 'frequency':'BID',
            'quantity':14, 'quantity_unit':'tablet'}]}

    def message(self):
        return encode_order(self.payload, self.context, patient_name='虚构患者',
                            author_id='doctor-fixture', message_id='message-1')

    def test_exact_clinical_payload_survives_standard_message_mapping(self):
        payload, context, message_id = decode_order(self.message(), expected_context=self.context)
        self.assertEqual(payload, self.payload)
        self.assertEqual(context, self.context)
        self.assertEqual(message_id, 'message-1')

    def test_changed_standard_dose_or_patient_is_not_masked_by_the_extension(self):
        for old, new in [('value="500"', 'value="50"'), ('extension="p-1"', 'extension="p-2"')]:
            with self.assertRaises(ValueError):
                decode_order(self.message().replace(old, new), expected_context=self.context)

    def test_message_cannot_move_an_order_to_another_assignment(self):
        other = {**self.context, 'encounter_id':'e-2'}
        with self.assertRaises(ValueError):
            decode_order(self.message(), expected_context=other)

    def test_optional_author_identifier_does_not_reorder_the_clinical_workflow(self):
        message=encode_order(self.payload,self.context,patient_name='虚构患者',
                             author_id=None,message_id='message-1')
        self.assertNotIn('assignedEntity',message)
        self.assertEqual(decode_order(message,expected_context=self.context)[0],self.payload)

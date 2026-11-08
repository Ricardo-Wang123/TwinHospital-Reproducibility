from copy import deepcopy
import unittest

from sandbox.experiments.linked.order_encoding import encode_items, preflight, OrderEncodingError
from sandbox.experiments.linked.clinical import LinkedEncounterRunner
from sandbox.experiments.linked.contracts import paper_conditions
from test_linked_clinical import FakeFactory, FakePort, FakeRules, example_case, new_rx


MAPPING={'NEW':{'medicationDefinitionId':'product','frequency_codes':{'QD':'QD','PRN':'PRN'},
    'source_dose_unit':'mg','dose_unit_code':'mg','source_quantity_unit':'tablet',
    'quantity_unit_code':'tab','route_codes':{'oral':'PO'},'dispenseLocationId':'local'}}


class EncodingTests(unittest.TestCase):
    def test_bedtime_frequency_maps_without_changing_dose_or_timing(self):
        mapping = deepcopy(MAPPING)
        mapping['NEW']['frequency_codes']['HS'] = 'HS'
        rx = new_rx()
        item = rx['items'][0]
        item['frequency'] = 'QHS'
        before = deepcopy(rx)
        self.assertFalse(preflight(rx, MAPPING)['native_encodable'])
        self.assertTrue(preflight(rx, mapping)['native_encodable'])
        encoded = encode_items(rx['items'], mapping)
        self.assertEqual(encoded['medications'][0]['rateCode'], 'HS')
        self.assertEqual(encoded['medications'][0]['dose'], item['dose'])
        self.assertEqual(encoded['frequency_normalizations'][0]['original'], 'QHS')
        self.assertEqual(rx, before)
        item['frequency'] = 'QHS or PRN'
        self.assertFalse(preflight(rx, mapping)['native_encodable'])

    def test_preflight_gives_native_field_options_for_multiple_bad_items(self):
        rx = new_rx()
        item = rx['items'][0]
        rx['items'] = [{**item, 'frequency': 'unknown'}, {**item, 'route': 'intravenous'}]
        result = preflight(rx, MAPPING)
        self.assertFalse(result['native_encodable'])
        self.assertEqual([(r['item_index'], r['field'], r['accepted_field_values'])
                          for r in result['findings']],
                         [(0, 'frequency', ['PRN', 'QD']), (1, 'route', ['oral'])])

    def test_valid_values_and_original_strings_survive_the_same_encoder(self):
        rx=new_rx();before=deepcopy(rx)
        self.assertTrue(preflight(rx,MAPPING)['native_encodable'])
        med=encode_items(rx['items'],MAPPING)['medications'][0]
        self.assertEqual(med['dose'],'5');self.assertEqual(med['quantity'],'7')
        self.assertEqual(med['rateCode'],'QD');self.assertEqual(med['methodCode'],'PO')
        self.assertEqual(before,rx)

    def test_bad_candidate_fields_are_explicit_and_never_substituted(self):
        for field,value,code in [('route','intravenous','unmapped_route'),('dose_unit','g','unmapped_dose_unit'),
            ('frequency','daily or PRN','unmapped_or_ambiguous_frequency'),
            ('quantity_unit','box','unmapped_quantity_unit'),('dose','NaN','invalid_positive_number'),
            ('dose',True,'invalid_positive_number'),('quantity',-1,'invalid_positive_number'),
            ('route',[],'unmapped_route'),('dose_unit',{},'unmapped_dose_unit')]:
            with self.subTest(field=field,value=value):
                rx=new_rx();rx['items'][0][field]=value;before=deepcopy(rx)
                result=preflight(rx,MAPPING)
                self.assertFalse(result['native_encodable'])
                self.assertEqual(result['findings'][0]['code'],code)
                self.assertFalse(result['native_write_attempted'])
                self.assertEqual(rx,before)
                with self.assertRaises(OrderEncodingError):encode_items(rx['items'],MAPPING)

    def test_prn_requires_actual_instruction_but_is_not_converted_to_daily(self):
        rx=new_rx();rx['items'][0]['frequency']='PRN'
        self.assertFalse(preflight(rx,MAPPING)['native_encodable'])
        rx['items'][0]['administration_instructions']='Use only as explicitly instructed in this fictional fixture.'
        self.assertTrue(preflight(rx,MAPPING)['native_encodable'])
        self.assertEqual(encode_items(rx['items'],MAPPING)['medications'][0]['rateCode'],'PRN')


class EncodingPort(FakePort):
    def preflight_order(self,rx):return preflight(rx,MAPPING)
    def execute(self,op,payload,context):
        if op=='order.save' and not self.preflight_order({'items':payload['items']})['native_encodable']:
            raise AssertionError('Unencodable order reached native write')
        return super().execute(op,payload,context)


class WrongRouteFactory(FakeFactory):
    def __init__(self,persistent=False):super().__init__();self.persistent=persistent
    def session(self,scope,role,model_family=None):
        session=super().session(scope,role,model_family);original=session.respond
        def respond(obs):
            value=original(obs)
            if value.get('action')=='consultation_complete' or (self.persistent and obs['action']=='revise_prescription'):
                value['prescription']['items'][0]['route']='intravenous'
            return value
        session.respond=respond;return session


class EncodingFollowthroughTests(unittest.TestCase):
    def test_each_paper_condition_reviews_original_then_repairs_and_pays(self):
        for condition in paper_conditions('prescription'):
            with self.subTest(condition=condition.config_id):
                factory=WrongRouteFactory();port=EncodingPort()
                result=LinkedEncounterRunner(factory,port,FakeRules()).run_case(
                    example_case(),run_id='encoding-repair',prescription=condition)
                self.assertEqual(result['status'],'completed',result.get('failure_reason'))
                a=result['artifacts'];versions=a['prescription_versions']
                self.assertEqual(a['original_prescription']['items'][0]['route'],'intravenous')
                # Preserve this deliberately permissive fake review: native
                # prevention is not falsely counted as clinical detection.
                self.assertEqual(a['original_prescription_review']['decision'],'approve')
                self.assertEqual(len(versions),2)
                self.assertFalse(versions[0]['native_write_attempted'])
                self.assertEqual(versions[0]['business_validation']['native_encoding']['findings'][0]['field'],'route')
                self.assertTrue(versions[1]['native_write_attempted'])
                writes=[p for op,p,_ in port.calls if op=='order.save']
                self.assertEqual(len(writes),1);self.assertEqual(writes[0]['items'][0]['route'],'oral')
                self.assertIsNone(writes[0]['parent_order_id'])
                self.assertEqual(a['attested_note']['status'],'locked')
                self.assertEqual(a['settlement']['payment']['status'],'paid')
                self.assertFalse(result['native_complete']) # persistence double
                observed=next(o for role,_,o in factory.observations if role=='doctor' and o['action']=='revise_prescription')
                self.assertFalse(observed['business_validation']['automatic_substitution'])

    def test_unchanged_bad_encoding_cannot_be_signed_or_paid_after_approve(self):
        port=EncodingPort()
        result=LinkedEncounterRunner(WrongRouteFactory(persistent=True),port,FakeRules(),
            limits={'prescription_revisions':1}).run_case(example_case(),run_id='not-fixed')
        self.assertEqual(result['status'],'failed')
        self.assertEqual(len(result['artifacts']['prescription_versions']),2)
        for op in ('order.save','order.attest','payment.commit'):
            self.assertNotIn(op,[o for o,_,_ in port.calls])
        self.assertFalse(result['workflow_complete'])


if __name__=='__main__':unittest.main()

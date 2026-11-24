from copy import deepcopy
import unittest

from sandbox.experiments.linked.order_encoding import encode_items, preflight, OrderEncodingError
def new_rx():
    return {'items': [{'drug_code': 'NEW', 'dose': '5', 'dose_unit': 'mg', 'quantity': '7',
                       'quantity_unit': 'tablet', 'frequency': 'QD', 'route': 'oral'}]}


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

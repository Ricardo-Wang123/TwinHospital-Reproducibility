import unittest
from tools.report_linked_provider_incident import probe_metadata


class ProviderIncidentTests(unittest.TestCase):
    def test_current_health_probe_rows_do_not_disappear(self):
        observed=probe_metadata({'observed_at':'2026-11-07T07:26:58+00:00','rows':[
            {'model':'openpangu-2.0-pro','status':'completed','returned_model':'openpangu-2.0-pro','http_status':200},
            {'model':'glm-5.1','status':'failed','http_status':403,'provider_error_code':'ModelArts.81006'}]})
        self.assertEqual([r['healthy_observed'] for r in observed],[True,False])
        self.assertEqual(observed[1]['provider_error_code'],'ModelArts.81006')

    def test_empty_or_unrecognized_probe_cannot_report_healthy(self):
        for value in ({'rows':[]},{'rows':[{}]},{'unrecognized':'format'}):
            with self.assertRaises(ValueError):probe_metadata(value)

    def test_preflight_contract_cannot_invent_an_http_status_or_hide_mismatch(self):
        rows=probe_metadata({'observed_at':'2026-11-07T15:26:58+08:00','rows':[
            {'model':'a','status':'completed','returned_model':'a'},
            {'model':'b','status':'completed','returned_model':'different','http_status':200}]})
        self.assertIsNone(rows[0]['http_status']);self.assertTrue(rows[0]['healthy_observed'])
        self.assertFalse(rows[1]['healthy_observed'])
        self.assertEqual(rows[0]['recorded_at'],'2026-11-07T07:26:58+00:00')


if __name__=='__main__':unittest.main()

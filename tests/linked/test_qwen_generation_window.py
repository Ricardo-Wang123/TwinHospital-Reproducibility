from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from sandbox.experiments.linked.contracts import paper_conditions
from sandbox.experiments.linked.provider import (
    BudgetLedger, BudgetedTransport, GENERATION_POLICY_V10, GENERATION_POLICY_V11,
    ProviderUnavailable, generation_settings,
)
from test_provider import FakeHTTP, FakeResponse


class QwenWindowTests(unittest.TestCase):
    def config(self):
        return {'endpoint':'https://api.modelarts-maas.com/v2',
                'api_protocol':'maas_standard_v2','role_model':'qwen3-32b',
                'generation_policy':deepcopy(GENERATION_POLICY_V11),'max_attempts':1}

    def test_role_backbone_and_both_tools_are_explicit_and_old_policy_unchanged(self):
        cfg=self.config()
        for role in ('patient','nurse','doctor','pharmacist'):
            setting=generation_settings(cfg,role,'qwen3-32b')
            self.assertEqual(setting['role_class'],'simulated_role')
            self.assertTrue(setting['thinking'])
            self.assertIsNone(setting['reasoning_effort'])
            with self.assertRaises(ValueError):generation_settings(cfg,role,'qwen3-30b-a3b')
        for model in ('qwen3-32b','qwen3-30b-a3b'):
            self.assertEqual(generation_settings(cfg,'scribe',model)['tool_family'],'qwen')
        self.assertEqual(len(paper_conditions(model_families=('qwen32b','qwen30b'))),15)
        cfg['generation_policy']=deepcopy(GENERATION_POLICY_V10)
        with self.assertRaises(ValueError):generation_settings(cfg,'doctor','qwen3-32b')
        with self.assertRaises(ValueError):generation_settings(cfg,'scribe','qwen3-30b-a3b')

    def test_exact_qwen30_wire_parameters_and_accounting(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);ledger=BudgetLedger(root/'ledger.sqlite')
            response=FakeResponse();response.value['model']='qwen3-30b-a3b'
            http=FakeHTTP(response)
            transport=BudgetedTransport(self.config(),ledger,root/'calls',key='fixture',http_session=http)
            _,rid=transport.complete([],model='qwen3-30b-a3b',case_id='c',role='scribe',data_kind='synthetic_independent')
            body=http.calls[0][1]['json']
            self.assertEqual(body['thinking'],{'type':'enabled'})
            self.assertEqual(body['max_completion_tokens'],16384)
            self.assertNotIn('reasoning_effort',body)
            record=json.loads((root/'calls'/(rid+'.json')).read_text())
            self.assertAlmostEqual(record['accounted_upper_cny'],.00009)

    def test_closed_window_prevents_http_and_reservation(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);ledger=BudgetLedger(root/'ledger.sqlite');http=FakeHTTP()
            cfg=self.config();cfg['dispatch_deadline_utc']='2000-01-01T00:00:00+00:00'
            transport=BudgetedTransport(cfg,ledger,root/'calls',key='fixture',http_session=http)
            with self.assertRaisesRegex(ProviderUnavailable,'window has closed'):
                transport.complete([],model='qwen3-32b',case_id='c',role='doctor',data_kind='synthetic_independent')
            self.assertEqual(http.calls,[]);self.assertEqual(ledger.summary()['requests'],0)

    def test_window_is_rechecked_after_queue_wait(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);ledger=BudgetLedger(root/'ledger.sqlite');http=FakeHTTP()
            cfg=self.config();cfg['minimum_request_interval_seconds']=1
            cfg['dispatch_deadline_utc']=(datetime.now(timezone.utc)+timedelta(hours=1)).isoformat()
            transport=BudgetedTransport(cfg,ledger,root/'calls',key='fixture',http_session=http)
            def close_window(**kwargs):
                transport.dispatch_deadline=datetime(2000,1,1,tzinfo=timezone.utc)
                return 0
            with patch.object(ledger,'dispatch_delay',side_effect=close_window):
                with self.assertRaises(ProviderUnavailable):
                    transport.complete([],model='qwen3-32b',case_id='c',role='doctor',data_kind='synthetic_independent')
            self.assertEqual(http.calls,[]);self.assertEqual(ledger.summary()['reserved_cny'],0)


if __name__=='__main__':unittest.main()

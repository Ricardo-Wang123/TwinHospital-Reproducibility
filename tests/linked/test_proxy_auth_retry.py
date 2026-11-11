from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from sandbox.experiments.linked.provider import (
    BudgetedTransport, BudgetLedger, GENERATION_POLICY_V9, ModelCallError,
    PROVIDER_FROZEN_CODE, ProviderUnavailable,
)

MODEL = 'openpangu-2.0-pro'


class Response:
    def __init__(self, status, body):
        self.status_code, self.body = status, body

    def json(self):
        return self.body


def auth_error(error_type='shell_api_error', **extra):
    return Response(401, {'error': {'type': error_type, 'message': 'Invalid token. (request id: fixture)', **extra}})


def success():
    return Response(200, {'model': MODEL, 'usage': {'prompt_tokens': 20, 'completion_tokens': 10},
                         'choices': [{'finish_reason': 'stop', 'message': {'content': '{"ok":true}'}}]})


class HTTP:
    def __init__(self, responses):
        self.responses, self.calls = responses, []

    def post(self, *args, **kwargs):
        self.calls.append(deepcopy(kwargs))
        return self.responses[len(self.calls) - 1]


class ProxyAuthRetryTests(unittest.TestCase):
    def transport(self, folder, responses, attempts):
        ledger = BudgetLedger(folder / 'ledger.sqlite', None)
        http = HTTP(responses)
        config = {'endpoint': 'https://api.modelarts-maas.com/v2', 'api_protocol': 'maas_standard_v2',
                  'generation_policy': deepcopy(GENERATION_POLICY_V9), 'role_model': MODEL,
                  'max_attempts': attempts}
        return BudgetedTransport(config, ledger, folder / 'calls', key='fixture-secret', http_session=http), ledger, http

    def call(self, transport):
        return transport.complete([{'role': 'user', 'content': 'fixed nonclinical fixture'}], model=MODEL,
                                  role='doctor', case_id='fixture', data_kind='synthetic_independent')

    def test_transient_proxy_auth_retries_same_body_and_key_and_accounts_each_attempt(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            transport, ledger, http = self.transport(root, [auth_error(), success()], 5)
            with patch('sandbox.experiments.linked.provider.time.sleep') as sleep:
                value, _ = self.call(transport)
            self.assertEqual(value['choices'][0]['message']['content'], '{"ok":true}')
            self.assertEqual(len(http.calls), 2)
            self.assertEqual(http.calls[0], http.calls[1])
            sleep.assert_called_once_with(30)
            records = [json.loads(p.read_bytes()) for p in (root / 'calls').glob('*.json')]
            error = next(row for row in records if row['http_status'] == 401)
            self.assertTrue(error['retry_planned'])
            self.assertEqual(error['retry_reason'], 'transient_proxy_authentication_same_input')
            self.assertEqual(error['retry_delay_seconds'], 30)
            self.assertEqual(ledger.summary()['reserved_cny'], 0)
            self.assertAlmostEqual(ledger.summary()['accounted_upper_cny'], .000272)

    def test_persistent_auth_failure_is_bounded_and_never_yields_clinical_output(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            transport, ledger, http = self.transport(root, [auth_error() for _ in range(5)], 5)
            with patch('sandbox.experiments.linked.provider.time.sleep') as sleep:
                with self.assertRaises(ModelCallError):
                    self.call(transport)
            self.assertEqual(len(http.calls), 5)
            self.assertEqual([call.args[0] for call in sleep.call_args_list], [30, 60, 120, 240])
            records = [json.loads(p.read_bytes()) for p in (root / 'calls').glob('*.json')]
            last = next(row for row in records if row['attempt'] == 5)
            self.assertFalse(last['retry_planned'])
            self.assertNotIn('retry_delay_seconds', last)
            self.assertEqual(ledger.summary()['reserved_cny'], 0)
            self.assertEqual(ledger.summary()['accounted_upper_cny'], 0)

    def test_ordinary_invalid_credentials_do_not_use_proxy_retry(self):
        with tempfile.TemporaryDirectory() as name:
            transport, _, http = self.transport(Path(name), [auth_error('invalid_request_error')], 5)
            with patch('sandbox.experiments.linked.provider.time.sleep') as sleep:
                with self.assertRaises(ModelCallError):
                    self.call(transport)
            self.assertEqual(len(http.calls), 1)
            sleep.assert_not_called()

    def test_frozen_provider_resource_still_halts_without_retry(self):
        with tempfile.TemporaryDirectory() as name:
            transport, _, http = self.transport(Path(name), [auth_error(code=PROVIDER_FROZEN_CODE)], 5)
            with patch('sandbox.experiments.linked.provider.time.sleep') as sleep:
                with self.assertRaises(ProviderUnavailable):
                    self.call(transport)
            self.assertEqual(len(http.calls), 1)
            sleep.assert_not_called()


if __name__ == '__main__':
    unittest.main()

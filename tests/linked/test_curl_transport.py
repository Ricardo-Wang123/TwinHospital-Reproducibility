"""Check credential handling, exact request forwarding and streamed readback."""
import io
import json
from pathlib import Path
import unittest
from unittest.mock import patch

import requests

from sandbox.experiments.linked.curl_transport import configure


ROUTE = {'mode': 'curl_macos_bound_interface', 'hostname': 'api.example.com',
         'interface': 'en0', 'resolved_ipv4': '1.1.1.1', 'dns_evidence': 'test fixture'}


class Input(io.BytesIO):
    def close(self):
        self.saved = self.getvalue()
        super().close()


class Process:
    def __init__(self, raw):
        self.stdin, self.stdout, self.stderr = Input(), io.BytesIO(raw), io.BytesIO()
        self.returncode = 0

    def wait(self, timeout=None):
        return self.returncode

    def poll(self):
        return self.returncode


class CurlTransportTests(unittest.TestCase):
    def session(self):
        session = requests.Session()
        with patch('sandbox.experiments.linked.curl_transport.sys.platform', 'darwin'), \
                patch('sandbox.experiments.linked.curl_transport.socket.if_nametoindex', return_value=1):
            configure(session, 'https://api.example.com/v1', ROUTE)
        return session

    def test_exact_body_and_authorization_are_absent_from_process_arguments(self):
        process = Process(b'HTTP/2 200\r\nContent-Type: application/json\r\n\r\n{"ok":true}')
        captured = {}

        def launch(args, **kwargs):
            captured['args'] = args
            body = args[args.index('--data-binary') + 1][1:]
            captured['body'] = Path(body).read_bytes()
            captured['mode'] = Path(body).stat().st_mode & 0o777
            captured['directory'] = Path(body).parent
            return process

        with patch('sandbox.experiments.linked.curl_transport.subprocess.Popen', side_effect=launch):
            response = self.session().post('https://api.example.com/v1/chat/completions',
                json={'content': 'private clinical input'},
                headers={'Authorization': 'Bearer test-secret'}, timeout=(5, 30),
                verify=True, allow_redirects=False)
        self.assertEqual(response.json(), {'ok': True})
        self.assertEqual(json.loads(captured['body']), {'content': 'private clinical input'})
        self.assertNotIn('test-secret', ' '.join(captured['args']))
        self.assertEqual(captured['args'][1], '--disable')
        self.assertNotIn('private clinical input', ' '.join(captured['args']))
        self.assertIn(b'Bearer test-secret', process.stdin.saved)
        self.assertEqual(captured['mode'], 0o600)
        self.assertFalse(captured['directory'].exists())

    def test_sse_lines_and_status_are_forwarded_without_reassembly(self):
        raw = b'HTTP/1.1 100 Continue\r\n\r\nHTTP/2 200\r\n\r\ndata: {"delta":"x"}\n\ndata: [DONE]\n\n'
        process = Process(raw)
        with patch('sandbox.experiments.linked.curl_transport.subprocess.Popen', return_value=process):
            response = self.session().post('https://api.example.com/v1/chat/completions',
                json={}, timeout=(5, 30), verify=True, allow_redirects=False, stream=True)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(list(response.iter_lines(chunk_size=1)),
                             [b'data: {"delta":"x"}', b'', b'data: [DONE]', b''])
            response.close()

    def test_rejects_unverified_or_cross_origin_requests_before_dispatch(self):
        with patch('sandbox.experiments.linked.curl_transport.subprocess.Popen') as launch:
            with self.assertRaises(ValueError):
                self.session().post('https://api.example.com/v1/chat/completions',
                    json={}, timeout=(5, 30), verify=False)
            with self.assertRaises(ValueError):
                self.session().post('https://api.example.com/v1/other',
                    json={}, timeout=(5, 30), verify=True)
            launch.assert_not_called()

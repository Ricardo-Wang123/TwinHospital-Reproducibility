"""Verified macOS curl transport for a single declared model endpoint.

Only the TLS/network implementation changes. Requests still prepares the exact
body, and the existing provider parses responses, records usage and owns retries.
Credentials go through a private stdin configuration, never process arguments.
"""

import ipaddress
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
from tempfile import TemporaryDirectory
from urllib.parse import urlsplit

import requests
from requests.adapters import BaseAdapter
from requests.structures import CaseInsensitiveDict


class _Body:
    def __init__(self, process, directory):
        self.process, self.directory = process, directory
        self.closed = False

    def read(self, amount=None, **_kwargs):
        if self.closed:
            return b''
        value = self.process.stdout.read(-1 if amount is None else amount)
        if not value:
            code = self.process.wait()
            if code:
                self.close()
                error = requests.ReadTimeout if code == 28 else requests.ConnectionError
                raise error(f'curl transport ended with code {code}; reconcile this request')
            self.close()
        return value

    def close(self):
        if self.closed:
            return
        self.closed = True
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
        self.process.stdout.close()
        self.process.stderr.close()
        self.directory.cleanup()

    def release_conn(self):
        self.close()


class CurlAdapter(BaseAdapter):
    """An opt-in, no-redirect adapter retaining hostname/certificate checking."""

    def __init__(self, endpoint, route):
        u = urlsplit(endpoint)
        if (sys.platform != 'darwin' or u.scheme != 'https' or u.port not in (None, 443)
                or u.username or u.query or u.fragment or route.get('hostname') != u.hostname
                or not route.get('dns_evidence')):
            raise ValueError('Declare one HTTPS model endpoint and its public DNS evidence')
        if not re.fullmatch(r'en[0-9]+', route.get('interface', '')):
            raise ValueError('Select an existing physical macOS network interface')
        socket.if_nametoindex(route['interface'])
        address = ipaddress.ip_address(route.get('resolved_ipv4', ''))
        if address.version != 4 or not address.is_global:
            raise ValueError('The pinned address must be a public IPv4 DNS result')
        self.endpoint, self.hostname, self.route = endpoint.rstrip('/'), u.hostname, dict(route)

    def send(self, request, stream=False, timeout=None, verify=True, cert=None, proxies=None):
        if (request.method != 'POST' or request.url != self.endpoint + '/chat/completions'
                or verify is not True or cert is not None or any((proxies or {}).values())):
            raise ValueError('curl transport accepts only verified calls to the declared completion endpoint')
        connect, read = timeout if isinstance(timeout, tuple) else (timeout, timeout)
        if not all(isinstance(x, (int, float)) and x > 0 for x in (connect, read)):
            raise ValueError('Positive bounded connect and read timeouts are required')
        directory = TemporaryDirectory(prefix='twinhospital-request-')
        process = None
        try:
            body = Path(directory.name) / 'body.json'
            raw = request.body or b''
            fd = os.open(body, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'wb') as handle:
                handle.write(raw.encode() if isinstance(raw, str) else raw)
            # JSON string escaping is also curl-config string escaping for the
            # ASCII header names/values accepted here. Reject line injection.
            headers = []
            for name, value in request.headers.items():
                if any(ch in name + str(value) for ch in '\r\n\0'):
                    raise ValueError('Invalid HTTP header')
                headers.append('header = ' + json.dumps(name + ': ' + str(value)))
            config = '\n'.join(headers) + '\n'
            args = ['/usr/bin/curl', '--disable', '--silent', '--show-error', '--no-buffer', '--include',
                    '--compressed', '--noproxy', '*', '--interface', self.route['interface'],
                    '--resolve', f'{self.hostname}:443:{self.route["resolved_ipv4"]}',
                    '--connect-timeout', str(connect), '--max-time', str(read),
                    '--max-redirs', '0', '--config', '-', '--header', 'Expect:',
                    '--data-binary', '@' + str(body), request.url]
            process = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                       stderr=subprocess.PIPE)
            process.stdin.write(config.encode())
            process.stdin.close()
            while True:
                first = process.stdout.readline()
                if not first.startswith(b'HTTP/'):
                    code = process.wait()
                    error = requests.ConnectTimeout if code == 28 else requests.ConnectionError
                    raise error(f'curl did not establish a response; exit code {code}')
                status = int(first.split()[1])
                fields = CaseInsensitiveDict()
                while True:
                    line = process.stdout.readline()
                    if line in (b'\r\n', b'\n'):
                        break
                    if not line or b':' not in line:
                        raise requests.ConnectionError('Incomplete curl HTTP response headers')
                    name, value = line.decode('iso-8859-1').split(':', 1)
                    fields[name.strip()] = value.strip()
                if status >= 200:
                    break
            result = requests.Response()
            result.status_code, result.headers = status, fields
            result.url, result.request, result.connection = request.url, request, self
            result.raw, result.encoding = _Body(process, directory), 'utf-8'
            return result
        except BaseException:
            if process is not None:
                if process.poll() is None:
                    process.kill()
                process.wait()
                for handle in (process.stdin, process.stdout, process.stderr):
                    if handle is not None and not handle.closed:
                        handle.close()
            directory.cleanup()
            raise

    def close(self):
        pass


def configure(session, endpoint, route):
    session.trust_env = False
    session.proxies = {}
    adapter = CurlAdapter(endpoint, route)
    session.mount('https://' + adapter.hostname + '/', adapter)
    return session

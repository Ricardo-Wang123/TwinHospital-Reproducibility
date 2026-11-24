"""Process-only macOS routing for a declared HTTPS endpoint.

Binding a socket to an existing physical interface avoids a TUN route without
altering system VPN settings. TLS hostname validation and the original Host/SNI
are retained. The public DNS address and interface are explicit run inputs.
"""
import hashlib
import ipaddress
from pathlib import Path
import re
import socket
import ssl
import subprocess
import sys
from urllib.parse import urlsplit, urlunsplit

import certifi
from requests.adapters import HTTPAdapter


def _intermediate_context(evidence):
    """Complete an incomplete server chain under the existing CA roots."""
    with Path(evidence['path']).open('rb') as source:
        certificate = source.read(16385)
    if (not 0 < len(certificate) <= 16384
            or hashlib.sha256(certificate).hexdigest() != evidence['sha256']):
        raise ValueError('TLS intermediate does not match its frozen certificate hash')
    pem = ssl.DER_cert_to_PEM_cert(certificate)
    ca_bundle = certifi.where()
    verified = subprocess.run(['openssl', 'verify', '-trusted', ca_bundle],
                              input=pem, text=True, capture_output=True, timeout=10)
    if verified.returncode != 0:
        raise ValueError('TLS intermediate does not chain to the installed CA roots')
    context = ssl.create_default_context(cafile=ca_bundle)
    if hasattr(ssl, 'VERIFY_X509_PARTIAL_CHAIN'):
        context.verify_flags &= ~ssl.VERIFY_X509_PARTIAL_CHAIN
    context.load_verify_locations(cadata=pem)
    return context


class BoundInterfaceAdapter(HTTPAdapter):
    def __init__(self, endpoint, route):
        parsed = urlsplit(endpoint)
        if sys.platform != 'darwin':
            raise ValueError('macos_bound_interface requires macOS')
        if (parsed.scheme != 'https' or parsed.username or parsed.port not in (None,443)
                or route.get('hostname') != parsed.hostname or not route.get('dns_evidence')):
            raise ValueError('bind a documented public DNS address to the exact HTTPS hostname')
        if not re.fullmatch(r'en[0-9]+', route.get('interface','')):
            raise ValueError('select an existing physical en interface, not a VPN or wildcard')
        address = ipaddress.ip_address(route.get('resolved_ipv4',''))
        if address.version != 4 or not address.is_global:
            raise ValueError('pinned API address must be a public IPv4 DNS result')
        self.hostname = parsed.hostname
        self.address = str(address)
        self.interface_index = socket.if_nametoindex(route['interface'])
        intermediate = route.get('tls_intermediate')
        self.ssl_context = _intermediate_context(intermediate) if intermediate is not None else None
        super().__init__(max_retries=0)

    def init_poolmanager(self, connections, maxsize, block=False, **kwargs):
        # IP_BOUND_IF=25 in the installed macOS SDK netinet/in.h.
        kwargs.update(socket_options=[(socket.IPPROTO_TCP,socket.TCP_NODELAY,1),
                                      (socket.IPPROTO_IP,25,self.interface_index)],
                      server_hostname=self.hostname, assert_hostname=self.hostname)
        if self.ssl_context is not None:
            kwargs['ssl_context'] = self.ssl_context
        return super().init_poolmanager(connections,maxsize,block,**kwargs)

    def build_connection_pool_key_attributes(self, request, verify, cert=None):
        host, pool = super().build_connection_pool_key_attributes(request, verify, cert)
        if self.ssl_context is not None:
            pool['ssl_context'] = self.ssl_context
        return host, pool

    def send(self, request, **kwargs):
        original = urlsplit(request.url)
        if (original.scheme != 'https' or original.hostname != self.hostname
                or original.port not in (None,443) or original.username):
            raise ValueError('bound route cannot send a request to another origin')
        if kwargs.get('verify') is not True or any((kwargs.get('proxies') or {}).values()):
            raise ValueError('bound route requires certificate verification and no extra proxy')
        forwarded = request.copy()
        forwarded.url = urlunsplit(('https',self.address,original.path,original.query,''))
        forwarded.headers['Host'] = self.hostname
        response = super().send(forwarded,**kwargs)
        response.url = request.url
        return response


def configure(session, endpoint, route):
    adapter = BoundInterfaceAdapter(endpoint,route)
    session.trust_env = False
    session.proxies = {}
    session.mount('https://' + adapter.hostname + '/',adapter)
    return session

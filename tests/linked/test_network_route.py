from unittest.mock import patch
import ssl
import unittest
import requests

from sandbox.experiments.linked.network_route import BoundInterfaceAdapter, configure


def route():
    return {'mode':'macos_bound_interface','hostname':'api.example.com','interface':'en0',
            'resolved_ipv4':'1.1.1.1','dns_evidence':'Independent test fixture, not a real provider route.'}


class NetworkRouteTests(unittest.TestCase):
    def adapter(self):
        with patch('sandbox.experiments.linked.network_route.sys.platform','darwin'), \
             patch('sandbox.experiments.linked.network_route.socket.if_nametoindex',return_value=14):
            return BoundInterfaceAdapter('https://api.example.com/v1',route())

    def test_physical_binding_keeps_original_sni_and_hostname_validation(self):
        a=self.adapter();kwargs=a.poolmanager.connection_pool_kw
        self.assertEqual(kwargs['server_hostname'],'api.example.com')
        self.assertEqual(kwargs['assert_hostname'],'api.example.com')
        self.assertIn((0,25,14),kwargs['socket_options'])
        req=requests.Request('POST','https://api.example.com/v1/chat/completions',json={'test':True}).prepare()
        response=requests.Response()
        with patch('requests.adapters.HTTPAdapter.send',return_value=response) as send:
            result=a.send(req,verify=True,proxies={})
        forwarded=send.call_args.args[0]
        self.assertEqual(forwarded.url,'https://1.1.1.1/v1/chat/completions')
        self.assertEqual(forwarded.headers['Host'],'api.example.com')
        self.assertEqual(req.url,'https://api.example.com/v1/chat/completions')
        self.assertEqual(result.url,req.url)

    def test_route_refuses_tls_bypass_proxy_or_different_origin(self):
        a=self.adapter();req=requests.Request('GET','https://api.example.com/v1/models').prepare()
        with patch('requests.adapters.HTTPAdapter.send') as send:
            for kwargs in [{'verify':False},{'verify':True,'proxies':{'https':'http://other'}}]:
                with self.assertRaises(ValueError):a.send(req,**kwargs)
            other=requests.Request('GET','https://different.example.com/v1/models').prepare()
            with self.assertRaises(ValueError):a.send(other,verify=True)
        send.assert_not_called()

    def test_requests_pool_key_preserves_the_verified_intermediate_context(self):
        r = {**route(), 'tls_intermediate': {'path': 'frozen.der', 'sha256': 'fixture'}}
        context = ssl.create_default_context()
        with patch('sandbox.experiments.linked.network_route.sys.platform', 'darwin'), \
             patch('sandbox.experiments.linked.network_route.socket.if_nametoindex', return_value=14), \
             patch('sandbox.experiments.linked.network_route._intermediate_context', return_value=context):
            adapter = BoundInterfaceAdapter('https://api.example.com/v1', r)
        request = requests.Request('GET', 'https://api.example.com/v1/models').prepare()
        _, pool = adapter.build_connection_pool_key_attributes(request, verify=True)
        self.assertIs(pool['ssl_context'], context)
        self.assertEqual(pool['ssl_context'].verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(pool['ssl_context'].check_hostname)

    def test_local_or_vpn_address_cannot_be_silently_selected(self):
        for key,value in [('resolved_ipv4','127.0.0.1'),('interface','utun8'),('hostname','different.example.com')]:
            r=route();r[key]=value
            with patch('sandbox.experiments.linked.network_route.sys.platform','darwin'):
                with self.assertRaises(ValueError):BoundInterfaceAdapter('https://api.example.com/v1',r)


if __name__=='__main__':unittest.main()

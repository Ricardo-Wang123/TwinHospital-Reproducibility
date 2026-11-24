"""Assignment-scoped WS/T service transport, composed with a FHIR workflow port.

This development increment implements OrderInfoAdd. Other clinical service
bodies are added explicitly; this module does not claim all WS/T 846 services.
"""
from contextlib import contextmanager
from datetime import datetime
from hashlib import sha256
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hmac
import json
import secrets
from threading import Thread
import xml.etree.ElementTree as ET

import requests

from .fhir_his import _FhirPort
from .native_his import NativeBlocked, NativeError, NativeRejected
from .wst846 import MESSAGE_OID, MAX_XML_BYTES, q, read_request, read_response, request, response, parse_xml
from .wst846_order import CONTEXT_CODE, NS, SERVICE, XSI, add, decode_order, encode_order


VERSION = 'wst846-order-service-1'
MEDIA_TYPE = 'application/soap+xml'


def extension_value(root, code):
    for node in root.findall('.//' + q(NS, 'observation')):
        identifier = node.find(q(NS, 'code'))
        if identifier is not None and identifier.get('code') == code:
            value = node.find(q(NS, 'value'))
            if value is not None:
                return json.loads(value.get('value', ''))
    raise ValueError('Missing declared workflow extension')


def acknowledgement(request_id, *, receipt=None, error=None):
    root = ET.Element(q(NS, 'MCCI_IN000002UV01'), {'xmlns:dt': NS})
    add(root, 'id', root=MESSAGE_OID, extension=secrets.token_hex(16))
    add(root, 'creationTime', value=datetime.now().strftime('%Y%m%d%H%M%S'))
    ack = add(root, 'acknowledgement', typeCode='AA' if error is None else 'AE')
    add(ack, 'targetMessage/id', root=MESSAGE_OID, extension=request_id)
    add(ack, 'acknowledgementDetail/text', value='Read-back verified' if error is None else error['message'][:2000])
    obs = add(root, 'controlActProcess/extension/component/observation', classCode='OBS', moodCode='EVN')
    code = add(obs, 'code', code='TwinHospital.WorkflowReceipt.v1')
    add(code, 'originalText', value='Observed workflow receipt or typed failure')
    value = add(obs, 'value', value=json.dumps({'receipt': receipt, 'error': error},
                                              ensure_ascii=False, allow_nan=False))
    value.set(q(XSI, 'type'), 'dt:ST')
    return response(ET.tostring(root, encoding='unicode'))


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def reply(self, code, packet):
        self.send_response(code)
        self.send_header('Content-Type', MEDIA_TYPE + '; charset=utf-8')
        self.send_header('Content-Length', str(len(packet)))
        self.end_headers()
        self.wfile.write(packet)

    def do_POST(self):
        message_id = 'unparsed-request'
        if not hmac.compare_digest(self.headers.get('Authorization', '').encode(), self.server.authorization):
            return self.reply(401, acknowledgement(message_id, error={'kind':'blocked','message':'Assignment token required'}))
        if self.path != '/wst846/HIPMessageServer' or self.headers.get_content_type() != MEDIA_TYPE:
            return self.reply(400, acknowledgement(message_id, error={'kind':'blocked','message':'SOAP 1.2 endpoint required'}))
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 < length <= MAX_XML_BYTES:
                raise ValueError('Invalid request length')
            service, message = read_request(self.rfile.read(length))
            if service != SERVICE:
                raise ValueError('This declared service profile does not implement the requested body')
            value = extension_value(parse_xml(message), CONTEXT_CODE)
            context = value['context']
            payload, context, message_id = decode_order(message, expected_context=context)
            if (context.get('case_id'), context.get('run_id')) != self.server.assignment:
                raise NativeBlocked('The WS/T request is outside the assigned encounter')
            if context.get('role') != 'doctor' or context.get('actor_id') != 'doctor-1':
                raise NativeBlocked('The order writer must be the assigned physician')
            receipt = self.server.backend.execute('order.save', payload, context)
            if receipt.get('readback_verified') is not True:
                raise NativeError('The hospital did not return a verified order read-back')
        except NativeRejected as exc:
            return self.reply(422, acknowledgement(message_id, error={
                'kind':'rejected','message':str(exc),'native_path':exc.path}))
        except NativeBlocked as exc:
            return self.reply(409, acknowledgement(message_id, error={'kind':'blocked','message':str(exc)}))
        except (KeyError, TypeError, ValueError) as exc:
            return self.reply(400, acknowledgement(message_id, error={'kind':'invalid','message':str(exc)}))
        except NativeError as exc:
            return self.reply(502, acknowledgement(message_id, error={'kind':'uncertain','message':str(exc)}))
        self.reply(200, acknowledgement(message_id, receipt=receipt))


class OrderPort:
    def __init__(self, fhir, base_url, authorization):
        if not isinstance(fhir, _FhirPort):
            raise ValueError('Other operations must retain their declared FHIR transport')
        self.fhir, self.base_url = fhir, base_url
        self.http = requests.Session()
        self.http.trust_env = False
        self.http.headers.update(Authorization=authorization, **{'Content-Type': MEDIA_TYPE})
        self.patient_names = {}
        self.author_ids = {}
        self.exchanges = []
        self.fhir_exchanges = []

    def __getattr__(self, name):
        return getattr(self.fhir, name)

    def execute(self, operation, payload, context):
        if operation != 'order.save':
            result = self.fhir.execute(operation, payload, context)
            if operation == 'registration.create':
                patient = result.get('readback', {}).get('patient', {})
                self.patient_names[(context['case_id'], context['run_id'])] = patient.get('name')
            if operation == 'personnel.registry':
                self.author_ids[(context['case_id'], context['run_id'])] = result.get('data', {}).get('signatory_id')
            from .fhir_his import OPERATIONS
            if operation in OPERATIONS and self.fhir_exchanges:
                self.fhir_exchanges[-1]['action_request_id'] = context.get('action_request_id')
            return result
        message_id = secrets.token_hex(16)
        message = encode_order(payload, context,
            patient_name=self.patient_names.get((context['case_id'], context['run_id'])),
            author_id=self.author_ids.get((context['case_id'], context['run_id'])), message_id=message_id)
        packet = request(SERVICE, message)
        try:
            result = self.http.post(self.base_url + '/HIPMessageServer', data=packet,
                                    timeout=(5,600), allow_redirects=False)
            body, status = read_response(result.content, namespace=NS, request_id=message_id)
            decoded = extension_value(body, 'TwinHospital.WorkflowReceipt.v1')
        except (requests.RequestException, ValueError, KeyError) as exc:
            raise NativeError('The WS/T request outcome is uncertain; reconcile before retry') from exc
        self.exchanges.append({'service':'OrderInfoAdd','message_id':message_id,
            'action_request_id':context.get('action_request_id'),
            'request_sha256':sha256(packet).hexdigest(), 'response_sha256':sha256(result.content).hexdigest(),
            'http_status':result.status_code,'acknowledgement':status})
        if status != 'AA' or result.status_code != 200:
            error = decoded.get('error') or {}
            if error.get('kind') == 'rejected':
                raise NativeRejected(error['native_path'], error['message'])
            if error.get('kind') in {'blocked','invalid'}:
                raise NativeBlocked(error['message'])
            raise NativeError(error.get('message', 'The WS/T operation is not confirmed'))
        receipt = decoded.get('receipt')
        if not isinstance(receipt, dict) or receipt.get('readback_verified') is not True:
            raise NativeError('The WS/T response lacks a verified hospital read-back')
        return receipt

    def snapshot(self, case_id):
        value = self.fhir.snapshot(case_id)
        value['standard_exchanges'] = {'wst846':list(self.exchanges),'fhir_r4':list(self.fhir_exchanges)}
        return value


@contextmanager
def wst_order_workflow(fhir, *, case_id, run_id):
    with ThreadingHTTPServer(('127.0.0.1',0), Handler) as server:
        authorization = 'Bearer '+secrets.token_urlsafe(32)
        server.authorization = authorization.encode()
        server.assignment = (case_id, run_id)
        server.backend = fhir.port
        thread = Thread(target=server.serve_forever, kwargs={'poll_interval':0.05}, daemon=True)
        thread.start()
        client = OrderPort(fhir, f'http://127.0.0.1:{server.server_port}/wst846', authorization)
        try:
            yield client
        finally:
            client.http.close()
            server.shutdown()
            thread.join()

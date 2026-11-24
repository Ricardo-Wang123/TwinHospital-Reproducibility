"""A SOAP-only hospital workflow profile; no FHIR transport is used.

Patient registration implements WS/T 846.2 PatientInfoRegister; order creation
implements WS/T 846.8 OrderInfoAdd. The remaining workflow
commands have explicitly project-owned service identifiers and XML namespaces.
They use the SOAP 1.2 HIPMessageServer transport and the string extension shape
of WS/T 846.1, but are NOT national WS/T clinical service definitions. This
declared coverage is part of every frozen interface contract and wire record.
"""
from contextlib import contextmanager
from datetime import datetime
from hashlib import sha256
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hmac
import json
from pathlib import Path
import secrets
from threading import Thread
import xml.etree.ElementTree as ET

import requests

from .fhir_his import ARTIFACT_TYPES, OPTIONAL, OPERATIONS, contract
from .native_his import NativeBlocked, NativeError, NativeRejected, ROLE_SETTINGS
from .wst846 import (
    INTERACTION_OID, MAX_XML_BYTES, MESSAGE_OID, SERVICES, Service, parse_xml,
    q, read_request, read_response, request, response, validate_header,
)
from .wst846_order import CONTEXT_CODE, XSI, decode_order, encode_order, identifier


VERSION = 'twinhospital-soap-wst846-order-workflow-1'
NS = 'urn:twinhospital:workflow-extension:1'
MEDIA_TYPE = 'application/soap+xml'
ORDER = SERVICES['OrderInfoAdd']
PATIENT = SERVICES['PatientInfoRegister']
NATIONAL = {'order.save': ORDER, 'registration.create': PATIENT}
REGISTRATION_ORGANIZATION = '322663105141149696'  # The isolated OpenHIS institution, not a real-world provider credential.
REGISTRATION_STAFF = ROLE_SETTINGS['cashier'][2]
PROJECT = {op: Service('TH_' + ''.join(part.title() for part in op.split('.')),
                      'THWorkflowRequest', NS, 'TwinHospital project service; not a national service code')
           for op in OPERATIONS if op not in NATIONAL}
REGISTRY = {service.action: service for service in [*NATIONAL.values(), *PROJECT.values()]}
ROUTES = {service.action: op for op, service in {**PROJECT, **NATIONAL}.items()}


def interface_contract():
    root = Path(__file__).parent
    return {
        'version': VERSION, 'transport': 'SOAP 1.2 HIPMessageServer(action,message)',
        'national_service': {'registration.create': 'WS/T 846.2-2024 PatientInfoRegister',
                             'order.save': 'WS/T 846.8-2024 OrderInfoAdd'},
        'registration_profile': 'Sender patient identifier maps to a fresh native identifier; encounter creation and simulated registration fee are explicit workflow extensions.',
        'registration_authority': {'isolated_institution': REGISTRATION_ORGANIZATION,
                                   'native_cashier_practitioner': REGISTRATION_STAFF},
        'project_extension_services': {op: service.action for op, service in PROJECT.items()},
        'project_extension_namespace': NS,
        'coverage': 'national PatientInfoRegister and OrderInfoAdd plus declared project workflow extensions; not all WS/T 846 services',
        'fhir_http_requests': 0,
        'sidecars': ['queue', 'history', 'handoff', 'personnel.registry'],
        'source_sha256': {name: sha256((root / name).read_bytes()).hexdigest()
                          for name in ['wst_workflow.py', 'wst846.py', 'wst846_order.py', 'fhir_his.py']},
    }


def child(parent, namespace, path, **attributes):
    for name in path.split('/'):
        parent = ET.SubElement(parent, q(namespace, name))
    parent.attrib.update({key: str(value) for key, value in attributes.items()})
    return parent


def string_extension(root, namespace, code, value):
    control = root.find(q(namespace, 'controlActProcess'))
    if control is None:
        control = child(root, namespace, 'controlActProcess')
    obs = child(control, namespace, 'extension/component/observation',
                classCode='OBS', moodCode='EVN')
    desc = child(obs, namespace, 'code', code=code)
    child(desc, namespace, 'originalText', value='TwinHospital project workflow data')
    node = child(obs, namespace, 'value', value=json.dumps(value, ensure_ascii=False,
                                                        allow_nan=False, separators=(',', ':')))
    node.set(q(XSI, 'type'), 'dt:ST')


def extension(root, namespace, code):
    nodes = [node for node in root.findall('.//' + q(namespace, 'observation'))
             if node.find(q(namespace, 'code')) is not None
             and node.find(q(namespace, 'code')).get('code') == code]
    if len(nodes) != 1:
        raise ValueError('Exactly one declared workflow extension is required')
    value = nodes[0].find(q(namespace, 'value'))
    if value is None or value.get(q(XSI, 'type')) != 'dt:ST':
        raise ValueError('A string-valued workflow extension is required')
    return json.loads(value.get('value', ''))


def header(service, message_id):
    namespace = service.namespace
    root = ET.Element(q(namespace, service.request_root), {'xmlns:dt': namespace})
    child(root, namespace, 'id', root=MESSAGE_OID, extension=message_id)
    child(root, namespace, 'creationTime', value=datetime.now().strftime('%Y%m%d%H%M%S'))
    child(root, namespace, 'interactionId', root=INTERACTION_OID, extension=service.request_root)
    child(root, namespace, 'processingCode', code='T')
    child(root, namespace, 'processingModeCode', code='T')
    child(root, namespace, 'acceptAckCode', code='AL')
    for role, system in [('sender', 'TwinHospital.MAS'), ('receiver', 'TwinHospital.OpenHIS')]:
        child(root, namespace, role + '/device/id/item', root='2.16.156.10011.2.5.1.3', extension=system)
    return root


def registration_name(context):
    # Same nonclinical identity as the native connector; never a source name.
    key = context['run_id'] + ':' + context['case_id']
    return '虚构实验' + sha256(key.encode()).hexdigest()[:10]


def patient_body(root, payload, context):
    """WS/T 846.2 Table 2 required patient fields; no invented clinical facts."""
    namespace = PATIENT.namespace
    patient = child(root, namespace, 'controlActProcess/subject/registrationRequest/subject1/patient')
    child(patient, namespace, 'id/item', root='2.16.156.10011.2.5.1.4',
          extension=identifier(payload['patient_key']))
    child(patient, namespace, 'statusCode', code='active')
    child(patient, namespace, 'effectiveTime/any', value=root.find(q(namespace, 'creationTime')).get('value'))
    child(patient, namespace, 'patientPerson/name/item/part', value=registration_name(context))
    child(patient, namespace, 'providerOrganization/id/item', root='2.16.156.10011.1.5',
          extension=REGISTRATION_ORGANIZATION)
    registration = root.find('/'.join(q(namespace, name) for name in
        'controlActProcess/subject/registrationRequest'.split('/')))
    child(registration, namespace, 'author/assignedEntity/id/item', root='2.16.156.10011.1.4',
          extension=REGISTRATION_STAFF)
    sex = str(payload['demographics'].get('sex', payload['demographics'].get('gender', ''))).lower()
    if sex in {'m', 'male', '男', 'f', 'female', '女'}:
        child(patient, namespace, 'patientPerson/administrativeGenderCode',
              code='2' if sex in {'f', 'female', '女'} else '1', codeSystem='2.16.156.10011.2.3.3.4')


def validate_patient(root, payload, context):
    namespace = PATIENT.namespace
    patient = root.find('/'.join(q(namespace, name) for name in
        'controlActProcess/subject/registrationRequest/subject1/patient'.split('/')))
    def value(path, attr):
        node = patient.find('/'.join(q(namespace, name) for name in path.split('/'))) if patient is not None else None
        return node.get(attr) if node is not None else None
    stamp = value('effectiveTime/any', 'value')
    if (value('id/item', 'root') != '2.16.156.10011.2.5.1.4'
            or value('id/item', 'extension') != identifier(payload['patient_key'])
            or value('statusCode', 'code') != 'active'
            or value('patientPerson/name/item/part', 'value') != registration_name(context)
            or value('providerOrganization/id/item', 'root') != '2.16.156.10011.1.5'
            or value('providerOrganization/id/item', 'extension') != REGISTRATION_ORGANIZATION
            or stamp != root.find(q(namespace, 'creationTime')).get('value')):
        raise ValueError('Standardized patient fields differ from the registered workflow identity')
    author = root.find('/'.join(q(namespace, name) for name in
        'controlActProcess/subject/registrationRequest/author/assignedEntity/id/item'.split('/')))
    if author is None or author.get('root') != '2.16.156.10011.1.4' or author.get('extension') != REGISTRATION_STAFF:
        raise ValueError('The national patient registration author differs from the isolated cashier')
    datetime.strptime(stamp, '%Y%m%d%H%M%S')
    if len(root.findall(q(namespace, 'controlActProcess'))) != 1:
        raise ValueError('Exactly one patient control act is required')


def acknowledgement(service, message_id, *, receipt=None, error=None):
    namespace = service.namespace
    root = ET.Element(q(namespace, 'MCCI_IN000002UV01'), {'xmlns:dt': namespace})
    child(root, namespace, 'id', root=MESSAGE_OID, extension=secrets.token_hex(16))
    child(root, namespace, 'creationTime', value=datetime.now().strftime('%Y%m%d%H%M%S'))
    ack = child(root, namespace, 'acknowledgement', typeCode='AA' if error is None else 'AE')
    child(ack, namespace, 'targetMessage/id', root=MESSAGE_OID, extension=message_id)
    child(ack, namespace, 'acknowledgementDetail/text',
          value='Read-back verified' if error is None else error['message'][:200])
    string_extension(root, namespace, 'TwinHospital.WorkflowReceipt.v1', {'receipt': receipt, 'error': error})
    return response(ET.tostring(root, encoding='unicode'))


def validate_command(operation, payload, context):
    scope, fields = contract(operation)
    if not isinstance(payload, dict) or not isinstance(context, dict):
        raise ValueError('Workflow payload and context must be objects')
    for schema, values in [(scope, context), (fields, payload)]:
        for key, datatype in schema.items():
            if key not in OPTIONAL and (key not in values or values[key] is None):
                raise ValueError('Missing workflow field: ' + key)
            value = values.get(key)
            if value is not None and datatype in {'Identifier', 'Reference', 'code', 'string'} and not isinstance(value, str):
                raise ValueError('Workflow identifiers must be strings')
            if value is not None and datatype == 'unsignedInt' and (type(value) is not int or value < 0):
                raise ValueError('Workflow versions must be unsigned integers')
            if value is not None and datatype == 'Attachment' and not isinstance(value, ARTIFACT_TYPES[key]):
                raise ValueError('Structured workflow artifact has the wrong type')
    if set(payload) - set(fields):
        raise ValueError('Unknown workflow payload field')


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def reply(self, status, service, message_id, *, receipt=None, error=None):
        packet = acknowledgement(service, message_id, receipt=receipt, error=error)
        self.send_response(status)
        self.send_header('Content-Type', MEDIA_TYPE + '; charset=utf-8')
        self.send_header('Content-Length', str(len(packet)))
        self.end_headers()
        self.wfile.write(packet)

    def do_POST(self):
        service, message_id = ORDER, 'unparsed-request'
        try:
            if not hmac.compare_digest(self.headers.get('Authorization', '').encode(), self.server.authorization):
                raise NativeBlocked('Assignment token required')
            if self.path != '/wst846/HIPMessageServer' or self.headers.get_content_type() != MEDIA_TYPE:
                raise ValueError('SOAP 1.2 HIPMessageServer endpoint required')
            size = int(self.headers.get('Content-Length', '0'))
            if not 0 < size <= MAX_XML_BYTES:
                raise ValueError('Invalid SOAP request size')
            service, message = read_request(self.rfile.read(size), services=REGISTRY)
            root, message_id = validate_header(service, message)
            data = extension(root, service.namespace, CONTEXT_CODE)
            if set(data) != {'operation', 'payload', 'context'} and service != ORDER:
                raise ValueError('Malformed project service extension')
            operation = ROUTES[service.action]
            context, payload = data['context'], data['payload']
            if service == ORDER:
                payload, context, message_id = decode_order(message, expected_context=context)
            elif data['operation'] != operation:
                raise ValueError('The SOAP action differs from its workflow operation')
            validate_command(operation, payload, context)
            if service == PATIENT:
                validate_patient(root, payload, context)
            if ((context.get('case_id'), context.get('run_id')) != self.server.assignment
                    or context.get('actor_id') != context.get('role', '') + '-1'):
                raise NativeBlocked('The SOAP request is outside its assigned encounter or role')
            receipt = self.server.backend.execute(operation, payload, context)
            if receipt.get('readback_verified') is not True:
                raise NativeError('The hospital did not verify the requested business state')
        except NativeRejected as exc:
            return self.reply(422, service, message_id, error={
                'kind': 'rejected', 'message': str(exc), 'native_path': exc.path})
        except NativeBlocked as exc:
            return self.reply(409, service, message_id, error={'kind': 'blocked', 'message': str(exc)})
        except (ValueError, TypeError, KeyError) as exc:
            return self.reply(400, service, message_id, error={'kind': 'invalid', 'message': str(exc)})
        except NativeError as exc:
            return self.reply(502, service, message_id, error={'kind': 'uncertain', 'message': str(exc)})
        self.reply(200, service, message_id, receipt=receipt)


class WstWorkflowPort:
    def __init__(self, backend, base_url, authorization):
        self.port, self.base_url = backend, base_url
        self.session = requests.Session()
        self.session.trust_env = False
        self.session.headers.update(Authorization=authorization, **{'Content-Type': MEDIA_TYPE})
        self.patient_names, self.author_ids, self.exchanges = {}, {}, []

    def __getattr__(self, name):
        return getattr(self.port, name)

    def execute(self, operation, payload, context):
        if operation not in OPERATIONS:
            result = self.port.execute(operation, payload, context)
            if operation == 'personnel.registry':
                self.author_ids[context['case_id'], context['run_id']] = result.get('data', {}).get('signatory_id')
            return result
        validate_command(operation, payload, context)
        service = NATIONAL.get(operation) or PROJECT[operation]
        message_id = secrets.token_hex(16)
        if operation == 'order.save':
            message = encode_order(payload, context, message_id=message_id,
                patient_name=self.patient_names.get((context['case_id'], context['run_id'])),
                author_id=self.author_ids.get((context['case_id'], context['run_id'])))
        else:
            root = header(service, message_id)
            if service == PATIENT:
                patient_body(root, payload, context)
            string_extension(root, service.namespace, CONTEXT_CODE,
                             {'operation': operation, 'payload': payload, 'context': context})
            message = ET.tostring(root, encoding='unicode')
        packet = request(service, message)
        try:
            reply = self.session.post(self.base_url + '/HIPMessageServer', data=packet,
                                      timeout=(5, 600), allow_redirects=False)
            root, ack = read_response(reply.content, namespace=service.namespace, request_id=message_id)
            data = extension(root, service.namespace, 'TwinHospital.WorkflowReceipt.v1')
        except (requests.RequestException, ValueError, KeyError) as exc:
            raise NativeError('SOAP outcome is uncertain; reconcile before retry') from exc
        self.exchanges.append({'operation': operation, 'service': service.action,
            'service_definition': service.standard, 'message_id': message_id,
            'action_request_id': context.get('action_request_id'),
            'request_sha256': sha256(packet).hexdigest(), 'response_sha256': sha256(reply.content).hexdigest(),
            'http_status': reply.status_code, 'acknowledgement': ack,
            'national_service': service in NATIONAL.values()})
        if ack != 'AA' or reply.status_code != 200:
            error = data.get('error') or {}
            if error.get('kind') == 'rejected':
                raise NativeRejected(error['native_path'], error['message'])
            if error.get('kind') in {'blocked', 'invalid'}:
                raise NativeBlocked(error['message'])
            raise NativeError(error.get('message', 'SOAP operation lacks a confirmed receipt'))
        receipt = data.get('receipt')
        if not isinstance(receipt, dict) or receipt.get('readback_verified') is not True:
            raise NativeError('SOAP response lacks verified hospital state')
        if operation == 'registration.create':
            self.patient_names[context['case_id'], context['run_id']] = receipt.get('readback', {}).get('patient', {}).get('name')
        return receipt

    def snapshot(self, case_id):
        value = self.port.snapshot(case_id)
        value['standard_exchanges'] = {'wst846': list(self.exchanges), 'fhir_r4': []}
        return value


@contextmanager
def wst_workflow(port, *, case_id, run_id):
    with ThreadingHTTPServer(('127.0.0.1', 0), Handler) as server:
        authorization = 'Bearer ' + secrets.token_urlsafe(32)
        server.authorization = authorization.encode()
        server.assignment = (case_id, run_id)
        server.backend = port
        thread = Thread(target=server.serve_forever, kwargs={'poll_interval': 0.05}, daemon=True)
        thread.start()
        client = WstWorkflowPort(port, f'http://127.0.0.1:{server.server_port}/wst846', authorization)
        try:
            yield client
        finally:
            client.session.close()
            server.shutdown()
            thread.join()

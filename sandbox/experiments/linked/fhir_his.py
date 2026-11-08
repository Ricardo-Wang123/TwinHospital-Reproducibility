"""FHIR R4 operations for the isolated HIS boundary of a TwinHospital encounter."""

from base64 import b64decode, b64encode
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hmac
import json
import secrets
from threading import Thread

import requests

from .native_his import NativeBlocked, NativeError


VERSION = 'twinhospital-fhir-r4-1'
MEDIA_TYPE = 'application/fhir+json'
CANONICAL = 'https://github.com/Ricardo-Wang123/TwinHospital-Reproducibility/fhir'
MAX_BODY = 8 * 1024 * 1024

# The operation definitions expose portable identities and action fields. JSON
# attachments carry role-produced artifacts without changing their contents.
OPERATIONS = {
    'registration.create': {'patient_key': 'Identifier', 'demographics': 'Attachment'},
    'encounter.begin_consultation': {},
    'diagnosis.catalogue': {},
    'consultation.save': {'artifact_id': 'Identifier', 'content': 'Attachment', 'transcript': 'Attachment'},
    'order.save': {'order_id': 'Identifier', 'items': 'Attachment', 'parent_order_id': 'Identifier'},
    'order.attest': {'order_id': 'Identifier', 'signatory_id': 'Identifier'},
    'note.save': {'note_id': 'Identifier', 'content': 'Attachment', 'expected_version': 'unsignedInt',
                  'parent_version': 'unsignedInt'},
    'note.revise': {'note_id': 'Identifier', 'content': 'Attachment', 'expected_version': 'unsignedInt',
                    'parent_version': 'unsignedInt'},
    'note.attest': {'note_id': 'Identifier', 'version': 'unsignedInt'},
    'payment.prepare': {'order_id': 'Identifier'},
    'payment.commit': {'bill_id': 'Identifier', 'amount': 'string', 'currency': 'code',
                       'channel': 'code', 'transaction_id': 'Identifier'},
}
SCOPE = {'case_id': 'Identifier', 'run_id': 'Identifier', 'config_id': 'Identifier',
         'role': 'code', 'actor_id': 'Identifier',
         'patient_id': 'Reference', 'encounter_id': 'Reference', 'idempotency_key': 'Identifier',
         'action_request_id': 'Identifier'}
OPTIONAL = {'config_id', 'idempotency_key', 'action_request_id', 'parent_order_id', 'parent_version'}
REFERENCES = {'patient_id': 'Patient', 'encounter_id': 'Encounter'}
ARTIFACT_TYPES = {'demographics': dict, 'content': dict, 'transcript': list, 'items': list, 'receipt': dict}


def operation_code(operation):
    return 'th-' + operation.replace('.', '-').replace('_', '-')


def contract(operation):
    scope = SCOPE if operation != 'registration.create' else {
        key: value for key, value in SCOPE.items() if key not in REFERENCES}
    return scope, OPERATIONS[operation]


def operation_definitions():
    definitions = {}
    for operation in OPERATIONS:
        code = operation_code(operation)
        scope, payload = contract(operation)
        definitions[code] = {
            'resourceType': 'OperationDefinition', 'id': code,
            'url': f'{CANONICAL}/OperationDefinition/{code}', 'version': VERSION,
            'name': ''.join(part.title() for part in code.split('-')), 'status': 'active',
            'kind': 'operation', 'code': code, 'affectsState': operation != 'diagnosis.catalogue',
            'system': True, 'type': False, 'instance': False,
            'description': f'Execute the {operation} action on one isolated hospital assignment.',
            'parameter': [{'name': name, 'use': 'in', 'min': int(name not in OPTIONAL),
                           'max': '1', 'type': kind} for name, kind in {**scope, **payload}.items()]
            + [{'name': 'receipt', 'use': 'out', 'min': 1, 'max': '1', 'type': 'Attachment',
                'documentation': 'JSON operation receipt including observed business read-back.'}],
        }
    return definitions


def capability_statement(base_url):
    return {
        'resourceType': 'CapabilityStatement', 'status': 'active', 'date': '2026-11-09',
        'kind': 'instance', 'fhirVersion': '4.0.1', 'format': [MEDIA_TYPE],
        'implementation': {'description': 'Isolated TwinHospital workflow bridge', 'url': base_url},
        'rest': [{'mode': 'server', 'resource': [{'type': 'OperationDefinition',
                                                'interaction': [{'code': 'read'}]}],
                  'operation': [{'name': code, 'definition': definition['url']}
                                for code, definition in operation_definitions().items()]}],
    }


def parameter(name, kind, value):
    if kind in {'Identifier', 'Reference', 'string', 'code'} and (not isinstance(value, str) or not value):
        raise ValueError(f'{name} must be a nonempty string')
    if kind == 'code' and any(char.isspace() for char in value):
        raise ValueError(f'{name} must be a single code')
    if kind == 'unsignedInt' and (type(value) is not int or not 0 <= value < 2**31):
        raise ValueError(f'{name} is outside the unsignedInt range')
    if kind == 'Attachment':
        if not isinstance(value, ARTIFACT_TYPES[name]):
            raise ValueError(f'Invalid {name} artifact')
        raw = json.dumps(value, ensure_ascii=False, allow_nan=False).encode()
        value = {'contentType': 'application/json', 'data': b64encode(raw).decode('ascii')}
    elif kind in {'Identifier', 'Reference'}:
        identifier = {'system': f'urn:twinhospital:{name}', 'value': value}
        value = identifier if kind == 'Identifier' else {'type': REFERENCES[name], 'identifier': identifier}
    return {'name': name, 'value' + kind[0].upper() + kind[1:]: value}


def encode(values, schema):
    if set(values) - set(schema):
        raise ValueError('Unknown operation parameter')
    return [parameter(name, kind, values.get(name)) for name, kind in schema.items()
            if name not in OPTIONAL or values.get(name) is not None]


def decode(resource, schema):
    if not isinstance(resource, dict) or resource.get('resourceType') != 'Parameters':
        raise ValueError('Expected a FHIR Parameters resource')
    values = {}
    for item in resource.get('parameter', []):
        name = item['name']
        if name in values or name not in schema:
            raise ValueError('Unexpected or repeated operation parameter')
        kind = schema[name]
        key = 'value' + kind[0].upper() + kind[1:]
        value = item[key]
        if kind == 'Attachment':
            if value['contentType'] != 'application/json':
                raise ValueError('Expected JSON artifact content')
            value = json.loads(b64decode(value['data'], validate=True))
        elif kind == 'Identifier':
            value = value['value']
        elif kind == 'Reference':
            value = value['identifier']['value']
        expected = parameter(name, kind, value)
        if set(item) != {'name', key} or (kind != 'Attachment' and expected != item):
            raise ValueError('Invalid parameter type or identifier system')
        values[name] = value
    if set(schema) - set(values) - OPTIONAL:
        raise ValueError('Required operation parameter is missing')
    return values


def outcome(code, message):
    return {'resourceType': 'OperationOutcome', 'issue': [
        {'severity': 'error', 'code': code, 'diagnostics': message}]}


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass  # The experiment journals evidence without HTTP credentials.

    def reply(self, status, body):
        content = json.dumps(body, ensure_ascii=False, allow_nan=False).encode()
        self.send_response(status)
        self.send_header('Content-Type', MEDIA_TYPE)
        self.send_header('Content-Length', str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def do_GET(self):
        if self.path == '/fhir/metadata':
            return self.reply(200, capability_statement(self.server.base_url))
        code = self.path.removeprefix('/fhir/OperationDefinition/')
        if code != self.path and code in self.server.definitions:
            return self.reply(200, self.server.definitions[code])
        self.reply(404, outcome('not-found', 'Unknown FHIR resource'))

    def do_POST(self):
        if not hmac.compare_digest(self.headers.get('Authorization', '').encode(), self.server.authorization):
            return self.reply(401, outcome('login', 'The assignment token is required'))
        operation = self.server.routes.get(self.path)
        if operation is None:
            return self.reply(404, outcome('not-found', 'Unknown workflow operation'))
        if self.headers.get_content_type() != MEDIA_TYPE:
            return self.reply(415, outcome('not-supported', 'Use application/fhir+json'))
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 < length <= MAX_BODY:
                raise ValueError('Invalid operation request length')
            scope, payload_schema = contract(operation)
            values = decode(json.loads(self.rfile.read(length)), {**scope, **payload_schema})
            context = {key: values[key] for key in scope if key in values}
            payload = {key: values.get(key) for key in payload_schema}
        except (KeyError, TypeError, ValueError) as exc:
            return self.reply(400, outcome('invalid', str(exc)))
        if ((context['case_id'], context['run_id']) != self.server.assignment
                or context['actor_id'] != context['role'] + '-1'):
            return self.reply(403, outcome('forbidden', 'Operation is outside the assigned case or actor'))
        try:
            receipt = self.server.port.execute(operation, payload, context)
        except NativeBlocked as exc:
            return self.reply(409, outcome('business-rule', str(exc)))
        except NativeError as exc:
            return self.reply(502, outcome('exception', str(exc)))
        self.reply(200, {'resourceType': 'Parameters', 'parameter': [parameter('receipt', 'Attachment', receipt)]})


class _FhirPort:
    def __init__(self, port, base_url, authorization):
        self.port = port
        self.base_url = base_url
        self.session = requests.Session()
        self.session.trust_env = False
        self.session.headers.update({'Authorization': authorization, 'Content-Type': MEDIA_TYPE,
                                     'Accept': MEDIA_TYPE})

    def __getattr__(self, name):
        return getattr(self.port, name)

    def execute(self, operation, payload, context):
        if operation not in OPERATIONS:
            return self.port.execute(operation, payload, context)
        scope, payload_schema = contract(operation)
        try:
            params = encode({key: context[key] for key in scope if key in context}, scope)
            params += encode(payload, payload_schema)
        except (TypeError, ValueError) as exc:
            raise NativeBlocked(f'Invalid FHIR workflow input: {exc}') from exc
        message = {'resourceType': 'Parameters', 'parameter': params}
        try:
            response = self.session.post(f'{self.base_url}/${operation_code(operation)}', json=message,
                                         allow_redirects=False, timeout=(5, 600))
            body = response.json()
        except (requests.RequestException, ValueError) as exc:
            raise NativeError('FHIR operation outcome is uncertain; reconcile before retry') from exc
        if response.status_code != 200:
            diagnostics = None
            if isinstance(body, dict) and body.get('resourceType') == 'OperationOutcome':
                issues = body.get('issue') or []
                if issues and isinstance(issues[0], dict):
                    diagnostics = issues[0].get('diagnostics')
            if response.status_code in {400, 401, 403, 409} and diagnostics:
                raise NativeBlocked(diagnostics)
            raise NativeError(diagnostics or 'FHIR gateway did not confirm an operation receipt')
        try:
            return decode(body, {'receipt': 'Attachment'})['receipt']
        except (KeyError, TypeError, ValueError) as exc:
            raise NativeError('FHIR receipt is invalid; reconcile before retry') from exc


@contextmanager
def fhir_workflow(port, *, case_id, run_id):
    """Scope an actual loopback HTTP bridge to one execution assignment."""
    with ThreadingHTTPServer(('127.0.0.1', 0), _Handler) as server:
        authorization = 'Bearer ' + secrets.token_urlsafe(32)
        server.authorization = authorization.encode()
        server.assignment = (case_id, run_id)
        server.port = port
        server.base_url = f'http://127.0.0.1:{server.server_port}/fhir'
        server.definitions = operation_definitions()
        server.routes = {f'/fhir/${operation_code(op)}': op for op in OPERATIONS}
        thread = Thread(target=server.serve_forever, kwargs={'poll_interval': 0.05}, daemon=True)
        thread.start()
        client = _FhirPort(port, server.base_url, authorization)
        try:
            yield client
        finally:
            client.session.close()
            server.shutdown()
            thread.join()

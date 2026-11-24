"""WS/T 846.1-2024 SOAP 1.2 transport and message correlation.

The standard defines HIPMessageServer(action, message), where message is an XML
character stream. Each clinical service must supply its own body validator from
the applicable part of WS/T 846. A SOAP envelope alone is not service conformance.
"""

from dataclasses import dataclass
from datetime import datetime
import re
import xml.etree.ElementTree as ET


SOAP = 'http://www.w3.org/2003/05/soap-envelope'
# The standard names the method and arguments, but not a vendor WSDL namespace.
WSDL_NAMESPACE = 'urn:twinhospital:wst846:2024'
MESSAGE_OID = '2.16.156.10011.2.5.1.1'
INTERACTION_OID = '2.16.156.10011.2.5.1.2'
MAX_XML_BYTES = 8 * 1024 * 1024
ET.register_namespace('soap', SOAP)
ET.register_namespace('hip', WSDL_NAMESPACE)


@dataclass(frozen=True)
class Service:
    action: str
    request_root: str
    namespace: str
    standard: str


# Service identifiers and message types are published by WS/T 846, not names
# invented for the simulator. This registry does not assert body implementation.
SERVICES = {
    'PatientInfoRegister': Service('PatientInfoRegister', 'PRPA_IN201311UV02',
        'https://www.chiss.org.cn', 'WS/T 846.2-2024'),
    'OutPatientInfoAdd': Service('OutPatientInfoAdd', 'PRPA_IN400001UV',
        'www.chiss.org.cn', 'WS/T 846.7-2024'),
    'OutPatientInfoUpdate': Service('OutPatientInfoUpdate', 'PRPA_IN400002UV',
        'www.chiss.org.cn', 'WS/T 846.7-2024'),
    'OrderInfoAdd': Service('OrderInfoAdd', 'POOR_IN200901UV',
        'https://www.chiss.org.cn', 'WS/T 846.8-2024'),
    'OrderInfoUpdate': Service('OrderInfoUpdate', 'POOR_IN200902UV',
        'https://www.chiss.org.cn', 'WS/T 846.8-2024'),
    'DocumentRegister': Service('DocumentRegister', 'RCMR_IN000002UV02',
        'https://www.chiss.org.cn', 'WS/T 846.6-2024'),
}


def parse_xml(value):
    raw = value.encode('utf-8') if isinstance(value, str) else value
    if not isinstance(raw, bytes) or not 0 < len(raw) <= MAX_XML_BYTES:
        raise ValueError('Invalid XML message length')
    if b'<!DOCTYPE' in raw.upper() or b'<!ENTITY' in raw.upper():
        raise ValueError('DTD and entity declarations are not accepted')
    try:
        return ET.fromstring(raw)
    except ET.ParseError as exc:
        raise ValueError('Malformed XML message') from exc


def q(namespace, name):
    return '{' + namespace + '}' + name


def request(service, message):
    """Use the prescribed two string arguments, not embedded arbitrary elements."""
    validate_header(service, message)
    envelope = ET.Element(q(SOAP, 'Envelope'))
    body = ET.SubElement(envelope, q(SOAP, 'Body'))
    method = ET.SubElement(body, q(WSDL_NAMESPACE, 'HIPMessageServer'))
    ET.SubElement(method, q(WSDL_NAMESPACE, 'action')).text = service.action
    ET.SubElement(method, q(WSDL_NAMESPACE, 'message')).text = message
    return ET.tostring(envelope, encoding='utf-8', xml_declaration=True)


def read_request(value, *, services=None):
    envelope = parse_xml(value)
    if envelope.tag != q(SOAP, 'Envelope'):
        raise ValueError('SOAP 1.2 is required')
    body = envelope.find(q(SOAP, 'Body'))
    if body is None or len(body) != 1 or body[0].tag != q(WSDL_NAMESPACE, 'HIPMessageServer'):
        raise ValueError('HIPMessageServer is required')
    method = body[0]
    if [node.tag for node in method] != [q(WSDL_NAMESPACE, 'action'), q(WSDL_NAMESPACE, 'message')]:
        raise ValueError('Exactly one action and one message argument are required')
    if any(len(node) for node in method):
        raise ValueError('HIPMessageServer arguments must be character streams')
    action, message = method[0].text, method[1].text
    registry = SERVICES if services is None else services
    if action not in registry or not message:
        raise ValueError('Unknown or empty hospital service message')
    service = registry[action]
    validate_header(service, message)
    return service, message


def validate_header(service, message):
    root = parse_xml(message)
    if root.tag != q(service.namespace, service.request_root):
        raise ValueError('The message interaction does not match the selected service')
    ns = {'m': service.namespace}
    node = root.find('m:id', ns)
    identifier = node.get('extension', '') if node is not None else ''
    if node is None or node.get('root') != MESSAGE_OID or not 0 < len(identifier) <= 50:
        raise ValueError('A valid message identifier and OID are required')
    created = root.find('m:creationTime', ns)
    stamp = created.get('value', '') if created is not None else ''
    if not re.fullmatch('[0-9]{14}', stamp):
        raise ValueError('The message creation timestamp is required')
    datetime.strptime(stamp, '%Y%m%d%H%M%S')
    interaction = root.find('m:interactionId', ns)
    if (interaction is None or interaction.get('root') != INTERACTION_OID
            or interaction.get('extension') != service.request_root):
        raise ValueError('The interaction identifier does not match the message')
    process = root.find('m:processingCode', ns)
    if process is None or process.get('code') not in {'P', 'T', 'D'}:
        raise ValueError('A production, training or debug processing code is required')
    mode = root.find('m:processingModeCode', ns)
    if mode is None or mode.get('code') not in {None, 'A', 'I', 'R', 'T'}:
        raise ValueError('Invalid processing mode')
    ack = root.find('m:acceptAckCode', ns)
    if ack is None or ack.get('code') not in {'AL', 'NE', 'ER', 'SU'}:
        raise ValueError('Invalid acknowledgement policy')
    for role in ('sender', 'receiver'):
        device = root.find(f'm:{role}/m:device/m:id/m:item', ns)
        if device is None or not device.get('extension'):
            raise ValueError('An identified sending and receiving system is required')
    if root.find('m:controlActProcess', ns) is None:
        raise ValueError('The control act package is required')
    return root, identifier


def response(message):
    """Return the response XML as the service's string-valued result."""
    parse_xml(message)
    envelope = ET.Element(q(SOAP, 'Envelope'))
    body = ET.SubElement(envelope, q(SOAP, 'Body'))
    method = ET.SubElement(body, q(WSDL_NAMESPACE, 'HIPMessageServerResponse'))
    ET.SubElement(method, q(WSDL_NAMESPACE, 'HIPMessageServerResult')).text = message
    return ET.tostring(envelope, encoding='utf-8', xml_declaration=True)


def read_response(value, *, namespace, request_id):
    envelope = parse_xml(value)
    if envelope.tag != q(SOAP, 'Envelope'):
        raise ValueError('SOAP 1.2 response is required')
    result = envelope.find('.//' + q(WSDL_NAMESPACE, 'HIPMessageServerResult'))
    if result is None or len(result) or not result.text:
        raise ValueError('The response character stream is required')
    root = parse_xml(result.text)
    acknowledgement = root.find(q(namespace, 'acknowledgement'))
    if acknowledgement is None or acknowledgement.get('typeCode') not in {'AA', 'AE'}:
        raise ValueError('A success or failure acknowledgement is required')
    target = acknowledgement.find(q(namespace, 'targetMessage') + '/' + q(namespace, 'id'))
    if target is None or target.get('root') != MESSAGE_OID or target.get('extension') != request_id:
        raise ValueError('The acknowledgement does not identify the submitted message')
    return root, acknowledgement.get('typeCode')

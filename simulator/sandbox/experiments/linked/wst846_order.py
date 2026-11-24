"""WS/T 846.8-2024 OrderInfoAdd body for a bound simulated encounter.

National message identifiers and mandatory paths follow Table 2. The hospital
drug catalogue owns product codes. TwinHospital-specific workflow context and
fields not mapped into national code tables use the explicitly named extension
mechanism from WS/T 846.1 section 7.5; they are never assigned national codes.
"""
from datetime import datetime
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json
import xml.etree.ElementTree as ET

from .wst846 import INTERACTION_OID, MESSAGE_OID, SERVICES, q, validate_header


SERVICE = SERVICES['OrderInfoAdd']
NS = SERVICE.namespace
PREFIX = '2.16.156.10011.'
CONTEXT_CODE = 'TwinHospital.WorkflowContext.v1'
XSI = 'http://www.w3.org/2001/XMLSchema-instance'


def add(parent, path, **attributes):
    for name in path.split('/'):
        parent = ET.SubElement(parent, q(NS, name))
    parent.attrib.update({key: str(value) for key, value in attributes.items()})
    return parent


def required(root, path, attribute):
    node = root.find('/'.join(q(NS, part) for part in path.split('/')))
    value = node.get(attribute) if node is not None else None
    if not isinstance(value, str) or not value:
        raise ValueError(f'Missing WS/T order field {path}/@{attribute}')
    return value


def identifier(value):
    value = str(value)
    if not value:
        raise ValueError('Empty hospital identifier')
    return value if len(value) <= 50 else 'th-' + sha256(value.encode()).hexdigest()[:40]


def number(value):
    try:
        result = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError('Invalid dose quantity') from exc
    if not result.is_finite():
        raise ValueError('Nonfinite dose quantity')
    return format(result.normalize(), 'f')


def encode_order(payload, context, *, patient_name, author_id, message_id, timestamp=None):
    if not payload.get('order_id') or not isinstance(payload.get('items'), list) or not payload['items']:
        raise ValueError('An identified nonempty order is required')
    for name in ('patient_id', 'encounter_id', 'case_id', 'run_id'):
        if not context.get(name):
            raise ValueError('An order requires bound encounter context')
    if not patient_name:
        raise ValueError('The registered patient name is required')
    stamp = timestamp or datetime.now().strftime('%Y%m%d%H%M%S')
    root = ET.Element(q(NS, SERVICE.request_root), {'ITSVersion': 'XML_1.0', 'xmlns:dt': NS})
    add(root, 'id', root=MESSAGE_OID, extension=identifier(message_id))
    add(root, 'creationTime', value=stamp)
    add(root, 'interactionId', root=INTERACTION_OID, extension=SERVICE.request_root)
    add(root, 'processingCode', code='T')
    add(root, 'processingModeCode', code='T')
    add(root, 'acceptAckCode', code='AL')
    for name, code, system in [('receiver', 'RCV', 'TwinHospital.OpenHIS'), ('sender', 'SND', 'TwinHospital.MAS')]:
        party = add(root, name, typeCode=code)
        device = add(party, 'device', classCode='DEV', determinerCode='INSTANCE')
        add(device, 'id/item', root=PREFIX+'2.5.1.3', extension=system)
    control = add(root, 'controlActProcess', classCode='CACT', moodCode='EVN')
    subject = add(control, 'subject', typeCode='SUBJ')
    group = add(subject, 'placerGroup', classCode='GROUPER', moodCode='RQO')
    author = add(group, 'author', typeCode='AUT', contextControlCode='OP')
    add(author, 'time', value=stamp)
    # The staff identifier is optional in Table 2. The original clinical flow
    # obtains its signatory registry after draft creation; do not invent a staff
    # identifier or insert an extra workflow step when that registry is absent.
    if author_id is not None:
        assigned = add(author, 'assignedEntity', classCode='ASSIGNED')
        add(assigned, 'id/item', root=PREFIX+'1.4', extension=identifier(author_id))
    for index, item in enumerate(payload['items']):
        component = add(group, 'component2', typeCode='COMP')
        add(component, 'sequenceNumber', value=index+1)
        order = add(component, 'substanceAdministrationRequest', classCode='SBADM', moodCode='RQO')
        add(order, 'id', root=PREFIX+'1.28',
            extension=identifier(payload['order_id']+':'+str(index+1)))
        add(order, 'doseQuantity', value=number(item['dose']), unit=item['dose_unit'])
        consumable = add(order, 'consumable2', typeCode='CSM')
        made = add(consumable, 'manufacturedProduct1', classCode='MANU')
        product = add(made, 'manufacturedProduct', classCode='MMAT', determinerCode='KIND')
        add(product, 'code', code=item['drug_code'], codeSystem=PREFIX+'2.5.1.14')
    component = add(group, 'componentOf1', contextConductionInd='false')
    encounter = add(component, 'encounter', classCode='ENC', moodCode='EVN')
    ids = add(encounter, 'id')
    add(ids, 'item', root=PREFIX+'2.5.1.8', extension='1')
    add(ids, 'item', root=PREFIX+'2.5.1.9', extension=identifier(context['encounter_id']))
    code = add(encounter, 'code', code='1', codeSystem=PREFIX+'2.3.1.271')
    add(code, 'displayName', value='门诊')
    patient = add(encounter, 'subject/patient', classCode='PAT')
    add(patient, 'id/item', root=PREFIX+'2.5.1.4', extension=identifier(context['patient_id']))
    add(patient, 'patientPerson/name/item/part', value=patient_name)
    extension = add(control, 'extension/component/observation', classCode='OBS', moodCode='EVN')
    code = add(extension, 'code', code=CONTEXT_CODE)
    add(code, 'originalText', value='TwinHospital workflow payload, context and non-national catalogue fields')
    value = add(extension, 'value', value=json.dumps({'payload': payload, 'context': context},
        ensure_ascii=False, allow_nan=False, separators=(',', ':')))
    value.set(q(XSI, 'type'), 'dt:ST')
    message = ET.tostring(root, encoding='unicode')
    decode_order(message, expected_context=context)
    return message


def decode_order(message, *, expected_context):
    root, message_id = validate_header(SERVICE, message)
    cap = root.find(q(NS, 'controlActProcess'))
    observations = cap.findall(q(NS, 'extension')+'/'+q(NS, 'component')+'/'+q(NS, 'observation'))
    contexts = [node for node in observations if node.find(q(NS, 'code')) is not None
                and node.find(q(NS, 'code')).get('code') == CONTEXT_CODE]
    if len(contexts) != 1:
        raise ValueError('Exactly one declared workflow extension is required')
    value = contexts[0].find(q(NS, 'value'))
    if value is None or value.get(q(XSI, 'type')) != 'dt:ST':
        raise ValueError('The workflow extension must be a string-valued observation')
    data = json.loads(value.get('value', ''))
    if set(data) != {'payload', 'context'} or data['context'] != expected_context:
        raise ValueError('WS/T message context differs from the assigned encounter')
    p = data['payload']
    group = cap.find(q(NS, 'subject')+'/'+q(NS, 'placerGroup'))
    if group is None:
        raise ValueError('The order group is required')
    datetime.strptime(required(group, 'author/time', 'value'), '%Y%m%d%H%M%S')
    components = group.findall(q(NS, 'component2'))
    if not components or len(components) != len(p['items']):
        raise ValueError('The WS/T order lines differ from the workflow payload')
    for index, (component, item) in enumerate(zip(components, p['items'], strict=True)):
        order = component.find(q(NS, 'substanceAdministrationRequest'))
        if (order is None or required(order, 'id', 'root') != PREFIX+'1.28'
                or required(order, 'id', 'extension') != identifier(p['order_id']+':'+str(index+1))
                or required(order, 'doseQuantity', 'value') != number(item['dose'])
                or required(order, 'doseQuantity', 'unit') != item['dose_unit']):
            raise ValueError('The standardized dose or order identity was changed')
        product = 'consumable2/manufacturedProduct1/manufacturedProduct/code'
        if (required(order, product, 'code') != item['drug_code']
                or required(order, product, 'codeSystem') != PREFIX+'2.5.1.14'):
            raise ValueError('The standardized drug code differs from the clinical order')
    encounter = group.find(q(NS, 'componentOf1')+'/'+q(NS, 'encounter'))
    if encounter is None:
        raise ValueError('The encounter is required')
    nodes = encounter.findall(q(NS, 'id')+'/'+q(NS, 'item'))
    pairs = {node.get('root'): node.get('extension') for node in nodes}
    if len(nodes) != 2:
        raise ValueError('The encounter count and identifier must occur exactly once')
    if pairs != {PREFIX+'2.5.1.8': '1', PREFIX+'2.5.1.9': identifier(expected_context['encounter_id'])}:
        raise ValueError('The standardized encounter binding differs')
    if (required(encounter, 'code', 'code') != '1'
            or required(encounter, 'code', 'codeSystem') != PREFIX+'2.3.1.271'):
        raise ValueError('The declared outpatient type is required')
    required(encounter, 'code/displayName', 'value')
    patient = encounter.find(q(NS, 'subject')+'/'+q(NS, 'patient'))
    if (patient is None or required(patient, 'id/item', 'root') != PREFIX+'2.5.1.4'
            or required(patient, 'id/item', 'extension') != identifier(expected_context['patient_id'])):
        raise ValueError('The standardized patient binding differs')
    required(patient, 'patientPerson/name/item/part', 'value')
    return p, data['context'], message_id

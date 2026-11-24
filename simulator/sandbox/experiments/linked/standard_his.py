"""A declared dual-standard profile with explicit service-level coverage."""
from contextlib import contextmanager
from hashlib import sha256
from pathlib import Path
from urllib.parse import urlsplit

from .fhir_his import fhir_workflow, interface_contract as fhir_contract
from .wst846_server import wst_order_workflow


VERSION = 'fhir-r4-wst8468-order-profile-1'


def interface_contract():
    root = Path(__file__).parent
    return {'version':VERSION, 'fhir':fhir_contract(),
        'wst_standard':'WS/T 846.8-2024', 'wst_transport':'WS/T 846.1-2024 SOAP 1.2 HIPMessageServer',
        'service_routes':{'order.save':'WS/T 846.8 OrderInfoAdd',
                          'other_declared_hospital_operations':'FHIR R4 project workflow operations',
                          'queue_history_and_role_registry':'persistent_runtime_sidecars'},
        'conformance_scope':'declared_order_service_profile_not_all_WS/T_846_services',
        'source_sha256':{name:sha256((root/name).read_bytes()).hexdigest()
            for name in ['standard_his.py','wst846.py','wst846_order.py','wst846_server.py']}}


@contextmanager
def standard_workflow(port, *, case_id, run_id):
    with fhir_workflow(port,case_id=case_id,run_id=run_id) as fhir:
        with wst_order_workflow(fhir,case_id=case_id,run_id=run_id) as standard:
            def observe(response, **_kwargs):
                body = response.request.body or b''
                if isinstance(body,str):
                    body = body.encode()
                standard.fhir_exchanges.append({'protocol':'FHIR R4',
                    'operation_path':urlsplit(response.url).path,
                    'request_sha256':sha256(body).hexdigest(),
                    'response_sha256':sha256(response.content).hexdigest(),
                    'http_status':response.status_code})
            fhir.session.hooks['response'].append(observe)
            yield standard

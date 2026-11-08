"""Create or check a provider migration profile without reading a key or sending data.

After filling the template, the existing probe_linked_provider.py command checks
the actual three model routes using only a fixed, nonclinical JSON request.
"""
import argparse
from copy import deepcopy
import json
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from sandbox.experiments.linked.provider_profile import VERSION, PROTOCOL, validate
from sandbox.experiments.linked.provider import check_data_policy, DataPolicyError
from sandbox.experiments.linked.json_boundary import POLICY as PARSING
from sandbox.experiments.linked.provider_content import POLICY as CONTENT


def template():
    names=['ROLE_MODEL_ID','TOOL_A_MODEL_ID','TOOL_B_MODEL_ID']
    endpoint='https://PROVIDER_HOST/v1'
    return {'endpoint':endpoint,'api_protocol':PROTOCOL,'key_file':'/ABSOLUTE/PRIVATE/NEW_PROVIDER.key',
        'credential_scope':{'credential_id':'NEW_PROVIDER_KEY_ID','endpoint':endpoint},
        'role_model':names[0],'tool_models':dict(zip(['tool_a','tool_b'],names[1:])),
        'pricing_cny_per_million_tokens':{m:[None,None] for m in names},'pricing_evidence':None,
        'generation_policy':{'version':VERSION,'response_parsing':deepcopy(PARSING),
            'provider_content_boundary':deepcopy(CONTENT),
            'models':{m:{'thinking':False,'temperature':0.2,'max_completion_tokens':8192,
                'completion_limit_field':'max_tokens','completion_limit_includes_reasoning':False,
                'context_window_tokens':None,'request_parameters':{}} for m in names}},
        'max_attempts':3,'max_calls_per_run':150,'timeout_seconds':180,'connect_timeout_seconds':15,
        'minimum_request_interval_seconds':1,'response_transport':'nonstream',
        'network_route':{'mode':'system_default'},
        'data_policy':{'mode':'independently_verified','endpoint':endpoint,
            'authorized_data_access':False,'verified_no_retention':False,'no_training':False,
            'no_human_review':False,'cache_policy_verified':False,'verified_at':None,'evidence':None}}


def inspect(config):
    validate(config)
    key=Path(config.get('key_file','/nonexistent'))
    private_key_present=key.is_file() and not key.stat().st_mode & 0o077
    try:
        check_data_policy('mimic_derived',config.get('data_policy',{}),config['endpoint'].rstrip('/'))
        data_status='authorization_basis_recorded_for_endpoint'
    except DataPolicyError:
        data_status='restricted_input_authorization_not_established'
    return {'configuration_valid':True,'credential_file_present_and_private':private_key_present,
        'credential_contents_read':False,'network_requests':0,'role_model':config['role_model'],
        'tool_models':config['tool_models'],'endpoint':config['endpoint'],
        'restricted_data_status':data_status,'api_availability':'requires_nonclinical_live_probe',
        'clinical_performance':'not_inferred_from_configuration','paid_execution_started':False}


def main():
    p=argparse.ArgumentParser(description=__doc__)
    g=p.add_mutually_exclusive_group(required=True)
    g.add_argument('--write-template',type=Path);g.add_argument('--config',type=Path)
    a=p.parse_args()
    if a.write_template:
        a.write_template.parent.mkdir(parents=True,exist_ok=True)
        with a.write_template.open('x') as f:json.dump(template(),f,ensure_ascii=False,indent=2);f.write('\n')
        print(json.dumps({'template':str(a.write_template),'status':'fill_and_verify_provider_specific_values_before_use'}))
        return 0
    value=inspect(json.loads(a.config.read_text()))
    print(json.dumps(value,ensure_ascii=False,indent=2))
    return 0 if value['credential_file_present_and_private'] else 1


if __name__=='__main__':raise SystemExit(main())

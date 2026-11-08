#!/usr/bin/env python3
"""Create source-locked programmatic analysis inputs; unknown meaning stays unknown."""
from __future__ import annotations
import argparse
from collections import Counter
from copy import deepcopy
import hashlib
import json
import math
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sandbox.experiments.linked.analysis_counts import MODULES, analyze
from sandbox.experiments.linked.contracts import digest, runtime_case
from sandbox.experiments.linked.programmatic_delivery import evaluate, VERSION as DELIVERY_VERSION


def read(path):
    return json.loads(Path(path).read_text())


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write(path, value):
    if path.exists():
        raise FileExistsError(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False)+'\n')
    path.chmod(0o600)


def blank_counts(task):
    items = dict.fromkeys(('applicable','correct','incorrect','absent','indeterminate'))
    claims = dict.fromkeys(('supported_correct','contradictory','unsupported','indeterminate'))
    if task == 'prescription':
        return {'prescription':dict.fromkeys(('is_error','high_risk_error','original_decision'))}
    if task == 'workflow':
        return {}
    counts = {'items':items, 'claims':claims, 'final_quality_qualified':None}
    if task == 'preconsult':
        counts.update(modules={name:deepcopy(items) for name in MODULES},
            risks=dict.fromkeys(('applicable','correctly_identified_and_flagged','indeterminate')),
            handoff_usable_without_critical_correction=None,
            first_pass_quality_qualified_without_correction=None)
    else:
        counts['first_draft_quality_accepted'] = None
    return counts


def make_record(run, path, task, version, unit_id):
    artifacts = run.get('artifacts', {})
    artifact = {'preconsult':'final_handoff', 'prescription':'original_prescription',
        'documentation':'attested_note', 'workflow':'settlement'}[task]
    result={'unit_id':unit_id, 'run_id':run['run_id'], 'implementation_version':version,
        'execution':{'status':run['status'], 'native_complete':run.get('native_complete'),
            'workflow_complete':run.get('workflow_complete'), 'three_task_eligible':run.get('three_task_eligible'),
            'clinical_disposition_status':(artifacts.get('clinical_disposition') or {}).get('status'),
            'disposition_documentation_status':(artifacts.get('disposition_documentation_outcome') or {}).get('status'),
            'call_count':run.get('call_count'), 'whole_run_wall_seconds':run.get('wall_seconds')},
        'source':{'run_path':str(path), 'run_file_sha256':sha(path),
            'input_hash':run['input_hash'], 'stage_input_hash':run.get('stage_input_hash'),
            'evaluation_unit':run['evaluation_unit'], 'quality_artifact_field':artifact},
        'artifact_available':bool(artifacts.get(artifact)),
        'measurement':{'status':'unavailable', 'source':'programmatic', 'covered_metrics':[],
            'criteria_version':DELIVERY_VERSION, 'source_refs':[sha(path)]},
        'counts':blank_counts(task),
        'documentation_scope':'clinical_disposition_only' if task=='documentation' and
            (artifacts.get('clinical_disposition') or run.get('stage_inputs',{}).get('documentation',{}).get('clinical_disposition')) else
            'standard_documentation' if task=='documentation' else None,
        'draft_metrics_applicable': task=='documentation' and
            (run.get('conditions',{}).get('documentation') or {}).get('workflow',
                'manual_documentation' if ':manual_documentation' in run['config_id'] else None) != 'manual_documentation',
        'effort':{'modeled_role_seconds':dict.fromkeys(('nurse','physician','documentation_staff')),
            'timing_scheme_ref':None, 'redundant_inquiry_programmatically_classified':None,
            'redundant_inquiry_modeled_seconds':None, 'draft_rework_rounds':None,
            'draft_rework_modeled_seconds':None, 'first_submission_boundary_version':None,
            'turnaround_same_encounter_clock_verified':None, 'consultation_to_attestation_seconds':None},
        'safety':{'whole_run_state_audit_complete':None, 'audit_source_ref':None,
            'observation_window_complete':None, 'correct_complete':None, 'unsafe_effect':None,
            'timed_out':None, 'verified_queue_to_paid_seconds':None}}
    if task in {'preconsult','documentation'}:
        delivery=evaluate(run,task)
        result['programmatic_delivery']=delivery
        result['counts']['final_quality_qualified']=delivery['qualifies']
        activities=run.get('activities')
        valid=isinstance(activities,list) and all(isinstance(a,dict) and type(a.get('active_seconds')) in (int,float)
            and math.isfinite(a['active_seconds']) and a['active_seconds']>=0 for a in activities)
        if valid and run.get('timing_source'):
            roles=Counter()
            for a in activities:roles[a.get('role')]+=a['active_seconds']
            result['effort']['modeled_role_seconds']={'nurse':roles['nurse'],'physician':roles['doctor'],'documentation_staff':0}
            result['effort']['timing_scheme_ref']=run['timing_source']
            result['measurement'].update(status='complete',covered_metrics=[
                'effort_per_qualifying_task','physician_effort_per_qualifying_task',
                'nurse_effort_per_qualifying_task' if task=='preconsult' else 'documentation_effort_per_qualifying_task'])
    return result


def linked_configuration_id(spec):
    """Resolve the canonical assignment from the frozen task specification.

    An unstarted anchor has an index placeholder, not a second configuration.
    Derive the same ID as the runner without reading successful outcomes.
    """
    expected = (('preconsult', 'nurse_only'),
                ('prescription', 'pprs_baseline'),
                ('documentation', 'manual_documentation'))
    canonical = []
    for task, workflow in expected:
        matches = [condition for condition in spec['conditions']
                   if condition['task'] == task and condition['workflow'] == workflow]
        if len(matches) != 1 or matches[0].get('model_family') is not None:
            raise ValueError('Frozen specification lacks one canonical baseline per task')
        canonical.append(matches[0])
    return 'linked:' + digest(canonical)[:12]


def prepare(batch, manifest, output, *, slice_name):
    batch, output = Path(batch).resolve(), Path(output).resolve()
    if output.exists():
        raise FileExistsError('choose a new output folder; previous analysis inputs are immutable')
    spec_path = batch/'specification.json'; spec = read(spec_path)
    expected = {c['case_id']:c for c in spec['cases']}
    source_cases = {c['case_id']:c for c in read(manifest)['cases']}
    units = []
    for case_id, expected_case in expected.items():
        row = source_cases[case_id]; case_path = Path(row['path'])
        if sha(case_path) != expected_case['file_sha256']:
            raise ValueError('case byte hash differs from executed source')
        case = read(case_path)
        if digest(runtime_case(case)) != expected_case['runtime_input_hash'] or case['patient_key'] != row['patient_key']:
            raise ValueError('case runtime/patient provenance differs')
        units.append({'unit_id':case_id+':r0', 'case_id':case_id,
            'patient_key':case['patient_key'], 'group':case['group'], 'replicate':0})
    conditions = spec['conditions'] if slice_name=='matrix' else []
    configs = {c['config_id']:{'config_id':c['config_id'], 'task':c['task'], 'records':[]} for c in conditions}
    canonical_id = linked_configuration_id(spec) if slice_name == 'workflow' else None
    if canonical_id is not None:
        configs[canonical_id] = {'config_id': canonical_id, 'task': 'workflow', 'records': []}
    frozen_attempts, omitted = {}, []
    for unit in units:
        index_path = batch/unit['case_id']/'attempts.json'
        frozen_attempts[str(index_path)] = sha(index_path)
        index = read(index_path)
        if index['case_id'] != unit['case_id']:
            raise ValueError('attempt index case mismatch')
        for key, row in index['attempts'].items():
            is_anchor = key == 'linked_baseline_anchor'
            include = is_anchor if slice_name=='workflow' else not is_anchor
            if not include:
                omitted.append({'case_id':unit['case_id'],'attempt':key,'reason':'different_analysis_slice'})
                continue
            config_id = row['config_id']
            if slice_name=='workflow':
                if config_id == 'linked_baseline_anchor' and not row.get('result_path'):
                    config_id = canonical_id
                elif config_id != canonical_id:
                    raise ValueError('Linked attempt differs from the frozen canonical baseline')
            if config_id not in configs:
                raise ValueError('attempt configuration absent from frozen specification')
            raw_path = row.get('result_path')
            if not raw_path:
                omitted.append({'case_id':unit['case_id'],'attempt':key,'reason':row['status']})
                continue
            path = Path(raw_path).resolve()
            if not path.is_relative_to(batch):
                raise ValueError('attempt evidence path escapes its batch')
            run = read(path)
            if (run['case_id'] != unit['case_id'] or run['config_id'] != config_id
                    or run['status'] != row['status'] or run['input_hash'] != expected[unit['case_id']]['runtime_input_hash']):
                raise ValueError('run differs from frozen case/attempt')
            expected_unit = 'complete_encounter' if is_anchor else 'fixed_input_task'
            if run['evaluation_unit'] != expected_unit:
                raise ValueError('cannot mix stage and linked encounter observations')
            configs[config_id]['records'].append(make_record(run,path,configs[config_id]['task'],spec['batch'],unit['unit_id']))
    dataset = {'schema_version':'twinhospital.programmatic-analysis/1',
        'implementation_version':spec['batch'], 'analysis_kind':'engineering_programmatic_source_locked',
        'planned_units':units, 'bootstrap':{'seed':20261107,'repeats':10000,'alpha':.05,'stratify_by_disease':True},
        'configurations':list(configs.values()), 'contrasts':[],
        'provenance':{'batch_path':str(batch), 'specification_sha256':sha(spec_path),
            'source_case_manifest_sha256':sha(manifest), 'attempt_index_sha256':frozen_attempts,
            'code_hashes':spec['code_hashes'], 'slice':slice_name, 'omitted_attempts':omitted,
            'slice_policy':'Matrix uses only the 15 frozen stage conditions; linked anchors are excluded. Workflow uses only linked anchors. No version or success-based selection.',
            'unstarted_anchor_policy':'Unstarted anchor placeholders belong to the canonical assignment derived from the frozen specification; they never form a separate configuration.',
            'assessment_policy':'Programmatic delivery checks and logged activities supply only explicitly covered metrics. Unresolved clinical meaning stays unassessed; whole-run safety requires its separate machine audit.',
            'contrasts_policy':'Empty until prespecified comparisons are supplied; no automatic favorable contrast selection.'}}
    result = analyze(dataset)
    output.mkdir(parents=True,mode=0o700)
    write(output/'analysis_input.json',dataset)
    result['input_file_sha256']=sha(output/'analysis_input.json')
    result['analysis_code_sha256']=sha(Path(__file__).resolve().parents[1]/'sandbox/experiments/linked/analysis_counts.py')
    code_path=output/'analysis_counts.source.py'
    code_path.write_bytes((Path(__file__).resolve().parents[1]/'sandbox/experiments/linked/analysis_counts.py').read_bytes())
    code_path.chmod(0o600)
    write(output/'analysis_output.json',result)
    for path, fingerprint in frozen_attempts.items():
        if sha(path) != fingerprint:
            raise ValueError('attempt index changed while preparing analysis')
    statuses = Counter((r.get('execution') or {}).get('status') for c in configs.values() for r in c['records'])
    return {'batch':spec['batch'],'slice':slice_name,'planned_units':len(units),
        'configurations':len(configs),'observed_attempts':sum(len(c['records']) for c in configs.values()),
        'observed_statuses':dict(statuses),'evaluation_protocol':'fully_virtual_workflow_v1','output':str(output)}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--batch',type=Path,required=True)
    parser.add_argument('--case-manifest',type=Path,required=True)
    parser.add_argument('--slice',choices=('matrix','workflow'),required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    print(json.dumps(prepare(args.batch,args.case_manifest,args.output,slice_name=args.slice),ensure_ascii=False))


if __name__=='__main__':main()

"""Run independent constructed cases through the existing workflows and real model API."""
from __future__ import annotations

import json
import os
import shutil
import threading
import time
import uuid
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ..actors import FixturePPRS
from ..backends import InMemoryHIS
from ..config import default_config
from ..contracts import digest
from ..data import CaseProvider, read_jsonl
from ..engine import CaseRuntime, variants
from ..gateway import SandboxGateway
from ..store import RunStore,file_hash,safe_child
from ..workflows import execute_workflow
from .materials import prepare, constructed_survey
from .model_api import ModelAPI
from .model_evaluation import assess
from .run import ExperimentFactory, model_spec, write_json
from .table_export import export_tables


def _job(api,judge,config,case,variant,reference,folder,medical_api=None):
    path=Path(folder)/variant['config_id'].replace(':','__')/case['case_id']
    if (path/'observation.json').exists():
        return json.loads((path/'observation.json').read_text(encoding='utf-8'))
    if path.exists():
        # A partial job is not silently overwritten; a new attempt retains old files.
        path=path/('attempt-'+uuid.uuid4().hex[:8])
    store=RunStore(path)
    backend=InMemoryHIS();backend.restore({'personnel':config['personnel']})
    gateway=SandboxGateway(backend,epoch=1,sink=lambda row:store.append('action_outcomes.jsonl',row),observation_window_s=0)
    factory=ExperimentFactory(api,variant['config_id'],medical_api=medical_api)
    runtime=CaseRuntime(config,store,backend,gateway,factory,case,variant,'constructed-'+uuid.uuid4().hex[:10],1)
    store.write_json('case_input.json',case)
    started=time.monotonic()
    try:
        runtime.bind()
        result=execute_workflow(case,variant,actor_factory=runtime.case_factory,pprs=FixturePPRS(),
            emit=runtime.emit,perform=runtime.perform,effort=runtime.effort,max_interactions=config['max_interactions'])
    except Exception as exc:
        result={'case_id':case['case_id'],**variant,'status':'failed','failure_reason':str(exc)}
    result.update(source_kind='constructed',clinical_validation=False,
                  execution_backend='local_artifact_store; actual model HTTP',wall_seconds=time.monotonic()-started,
                  rework_seconds=sum(a['active_seconds'] for a in runtime.activities if a['activity_type']=='note_rework'))
    if 'encounter.end' in runtime.milestones:
        origin=datetime(2026,1,1,tzinfo=timezone.utc)
        result['encounter_ended']=(origin+timedelta(seconds=runtime.milestones['encounter.end'])).isoformat()
        if 'note.attest' in runtime.milestones:
            result['physician_attested']=(origin+timedelta(seconds=runtime.seconds)).isoformat()
        result['turnaround_time_source']='constructed_activity_timeline_not_hospital_measurement'
    store.write_json('result.json',result)
    store.write_json('activities.json',runtime.activities)
    evidence=[str((path/'case_input.json').resolve()),str((path/'result.json').resolve()),
              str((path/'activities.json').resolve())]+factory.evidence_refs()
    observations=[]
    common={'case_id':case['case_id'],'config_id':variant['config_id'],'row_id':variant['config_id'],
            'source_kind':'constructed','status':result['status'],'evidence_refs':evidence,
            'input_source_kind':case.get('input_source_kind','unknown'),
            'input_sha256':digest(case['input']),'formal_eligible':False}
    if case['task']=='prescription':
        observations.append({**common,'table_ids':[4],'stage':'original_prescription_final_decision',
            'data':{'prescription':{'case_id':case['case_id'],'status':result['status'],
                'final_decision':result.get('final_decision'),'reference':reference}}})
    else:
        try:
            evaluation=assess(judge,case,variant,result,reference)
            store.write_json('evaluation.json',evaluation)
            evidence.extend([str((path/'evaluation.json').resolve())]+evaluation['evidence_refs'])
            stages=evaluation['stages']
            result['quality_source']='model_review'
            if case['task']=='preconsult':
                first=stages['first_handoff'];final=stages['doctor_completed']
                result.update(qualified=final['qualified'] and result['status']=='completed',
                    first_handoff_qualified=first['qualified'],
                    downstream_supplementation_or_correction=result.get('downstream_supplementation'),
                    first_pass_completed=first['qualified'] and result.get('downstream_supplementation') is False)
                observations.append({**common,'table_ids':[5,7],'stage':'first_handoff',
                    'data':{**first,'module_inventory_complete':True}})
                tables=[10];stage='doctor_completed'
            else:
                first=stages['first_draft'];final=stages['final_signed']
                result.update(qualified=final['qualified'] and result['status']=='completed',
                    first_draft_accepted=first['qualified'])
                observations.append({**common,'table_ids':[8],'stage':'final_signed',
                    'artifact_version':len(result.get('artifact_versions',[])),
                    'data':{**final,'module_inventory_complete':True}})
                tables=[12];stage='final_signed'
            observations.append({**common,'table_ids':tables,'stage':stage,'data':{'result':result,
                'activities':runtime.activities,'activity_capture_complete':True,
                'timing_note':'constructed durations; not real human active-work measurements'}})
        except Exception as exc:
            store.write_json('evaluation-failure.json',{'error':type(exc).__name__,'detail':str(exc)})
            table_ids=[5,7,10] if case['task']=='preconsult' else [8,12]
            observations.append({**common,'table_ids':table_ids,'data':{
                'result':{**result,'qualified':None,'quality_source':None},
                'activities':runtime.activities,'activity_capture_complete':True,
                'evaluation_failure':str(exc)}})
    bundle={'case_id':case['case_id'],'config_id':variant['config_id'],'status':result['status'],
            'failure_reason':result.get('failure_reason'),'observations':observations,
            'folder':str(path.resolve()),'api_evidence':factory.evidence_refs()}
    store.write_json('observation.json',bundle)
    return bundle


def model_roles_from_file(path=None):
    roles={'foundation':model_spec(),'medical':None}
    if path:
        supplied=json.loads(Path(path).read_text(encoding='utf-8-sig'))
        if not isinstance(supplied,dict) or set(supplied)-set(roles):
            raise ValueError('model config must contain only foundation and medical objects')
        allowed={'model','endpoint','api_key_env','env_file','temperature','max_tokens',
            'timeout_seconds','max_calls','role','revision','specialization_evidence'}
        for family,value in supplied.items():
            if family=='medical' and value is None: continue
            if not isinstance(value,dict) or set(value)-allowed:
                raise ValueError('model specification permits documented fields and credential references only')
            roles[family]={**roles['foundation'],**value,'role':family}
        if roles['medical']:
            if not roles['medical'].get('specialization_evidence'):
                raise ValueError('medical model needs explicit specialization_evidence; a role prompt is not a medical model')
            if (roles['medical']['model'],roles['medical']['endpoint'])==(roles['foundation']['model'],roles['foundation']['endpoint']):
                raise ValueError('the same model endpoint cannot fill both medical and foundation rows')
    return roles


def freeze_existing_dataset(source,destination):
    source=Path(source).resolve();destination=Path(destination).resolve()
    provider=CaseProvider(source)
    destination.mkdir(parents=True,exist_ok=False)
    for item in provider.catalog['artifacts']:
        original=safe_child(source,item['path']);target=safe_child(destination,item['path'])
        target.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(original,target)
    shutil.copy2(source/'data_catalog.json',destination/'data_catalog.json')
    # CaseProvider verifies the copied artifact digests before a model is called.
    CaseProvider(destination)
    write_json(destination/'source-dataset.json',{'original_directory':str(source),
        'original_catalog_sha256':file_hash(source/'data_catalog.json'),
        'frozen_dataset_hash':provider.dataset_hash,'copied':'catalog and all explicitly cataloged artifacts'})
    return destination


def freeze_source(output):
    manifest=Path(output)/'source-manifest.json'
    if manifest.exists(): return
    root=Path(__file__).resolve().parents[1];files=[]
    for path in sorted(root.rglob('*.py')):
        if 'tests' in path.relative_to(root).parts: continue
        target=Path(output)/'frozen-source'/path.relative_to(root)
        target.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(path,target)
        files.append({'source':str(path),'snapshot':str(target.resolve()),'sha256':file_hash(target)})
    write_json(manifest,{'recorded_at':datetime.now(timezone.utc).isoformat(),
        'capture_boundary':'after module imports and input validation, before any role/judge request',
        'files':files,'not_a_test_or_code_review':True})


def run_batch(output,workers=4,limit=None,*,model_config=None,dataset=None,selected=None,survey_example=False):
    output=Path(output).resolve()
    # Configurable runs have distinct inputs/models. Never relabel observations
    # already saved by another run or rewrite its frozen input directory.
    if (model_config or dataset or selected) and output.exists():
        raise ValueError('explicit model/dataset/configuration selection requires a new output directory')
    if workers<1 or (limit is not None and not 1<=limit<=200):
        raise ValueError('workers must be positive and an explicit subset limit must be between 1 and 200')
    roles=model_roles_from_file(model_config);spec=roles['foundation']
    available=[v for v in variants() if v.get('model_family')!='medical' or roles['medical']]
    if selected:
        known={v['config_id']:v for v in available}
        if len(set(selected))!=len(selected) or any(key not in known for key in selected):
            raise ValueError('selected configuration is duplicated, unknown, or requires an unavailable medical model')
        selected=[known[key] for key in selected]
    else: selected=available
    output.mkdir(parents=True,exist_ok=True)
    original_dataset=str(Path(dataset).resolve()) if dataset else None
    dataset=freeze_existing_dataset(dataset,output/'dataset') if dataset else prepare(output/'dataset')
    provider=CaseProvider(dataset)
    task_cases={task:provider.execution_cases(task) for task in {v['task'] for v in selected}}
    for task,cases in task_cases.items():
        if (limit is None and len(cases)!=200) or (limit is not None and len(cases)<limit):
            raise ValueError('requested task must provide 200 cases, or at least the explicitly selected subset: '+task)
    actor=ModelAPI(spec,output/'model-api')
    medical=ModelAPI(roles['medical'],output/'medical-model-api') if roles['medical'] else None
    judge_spec={**spec,'max_tokens':6500,'max_calls':2400,'temperature':0.0}
    judge=ModelAPI(judge_spec,output/'judge-api')
    config=default_config();config.update(purpose='engineering_demo',max_interactions=12,
        model_timeout_s=90,case_timeout_s=600,his_backend='in_memory',suites=['clinical'])
    configuration={'schema_version':1,'data_source':'constructed; no clinical verification',
        'configurations':[dict(v,available=v in available,selected=v in selected,
            unavailable_reason='No confirmed medical-specialist model is configured' if v not in available else None) for v in variants()],
        'dataset':str(dataset),'dataset_origin':original_dataset or 'constructed_by_materials',
        'input_source_kinds':dict(Counter(c.get('source_kind','unknown') for c in provider.cases.values())),
        'model_config_file':str(Path(model_config).resolve()) if model_config else None,
        'model_roles':{'foundation':spec,'medical':roles['medical'],'patient':'deterministic released facts',
            'other_roles':'actual foundation-model API; simulated people'},
        'configuration_concurrency':1,'independent_case_http_workers':workers,'cases_per_task':limit or 200,
        'timing':config['timing'],'clinical_persistence':'existing InMemoryHIS per case; NOT an OpenHIS execution result',
        'native_operations':{'tables':[14,15],'ready':False,'reason':'native semantics/runtime evidence handled separately'},
        'hospital_independent':{'tables':[6,9,11,13],'ready':False,'reason':'no real independent hospital material'},
        'human_questionnaires':{'tables':[3,16,17],'ready':False,'reason':'no real participant responses; constructed import example separate'}}
    write_json(output/'experiment-config.json',configuration)
    context={'source_kind':'constructed_actual_model_run','model_roles':configuration['model_roles'],
        'material_catalog':[dict(c,split='test' if c['split']=='validation' else c['split'],verified=c.get('verified',False),
            evidence_refs=[str((dataset/'cases.jsonl').resolve())]) for c in provider.cases.values()],
        'operation_inputs':[dict(r,evaluation_set='end_to_end',evidence_refs=[str((dataset/'end_to_end_cases.jsonl').resolve())]) for r in (read_jsonl(dataset/'end_to_end_cases.jsonl') if (dataset/'end_to_end_cases.jsonl').exists() else [])]+
            [dict(r,case_id=r['test_id'],evaluation_set='local_injection',evidence_refs=[str((dataset/'safety_tests.jsonl').resolve())]) for r in (read_jsonl(dataset/'safety_tests.jsonl') if (dataset/'safety_tests.jsonl').exists() else [])],
        'participant_rows':[]}
    write_json(output/'table-context.json',context)
    freeze_source(output)
    all_bundles=[];lock=threading.Lock()
    def progress(current=None):
        with lock:
            status={'pid':os.getpid(),'state':'running','current_config':current,
                'completed_case_configurations':len(all_bundles),'case_status_counts':dict(Counter(b['status'] for b in all_bundles)),
                'model_calls_this_process':actor.calls,'judge_calls_this_process':judge.calls,
                'medical_model_calls_this_process':medical.calls if medical else 0,
                'started_or_updated_at':datetime.now(timezone.utc).isoformat()}
            write_json(output/'progress.json',status)
    progress()
    for variant in selected:
        cases=[{**case,'input_source_kind':provider.cases[case['case_id']].get('source_kind','unknown')}
            for case in task_cases[variant['task']]]
        if limit: cases=cases[:limit]
        print(json.dumps({'config_id':variant['config_id'],'starting_cases':len(cases)},ensure_ascii=False),flush=True)
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures=[pool.submit(_job,actor,judge,config,case,variant,provider.references[case['case_id']],output/'clinical',medical) for case in cases]
            for future in as_completed(futures):
                try: bundle=future.result()
                except Exception as exc:
                    bundle={'status':'failed','failure_reason':type(exc).__name__+': '+str(exc),'observations':[]}
                all_bundles.append(bundle);progress(variant['config_id'])
                if len(all_bundles)%10==0:
                    print(json.dumps({'completed':len(all_bundles),'counts':dict(Counter(b['status'] for b in all_bundles)),
                        'actor_calls':actor.calls,'judge_calls':judge.calls},ensure_ascii=False),flush=True)
        observations=[row for bundle in all_bundles for row in bundle['observations']]
        write_json(output/'observations.json',observations)
        exported=export_tables(output,observations,context)
        print(json.dumps({'config_finished':variant['config_id'],'exported_tables':exported['complete_tables'],
                          'missing_cells':exported['missing_cells']},ensure_ascii=False),flush=True)
    write_json(output/'case-bundles.json',all_bundles)
    observations=[row for bundle in all_bundles for row in bundle['observations']]
    write_json(output/'observations.json',observations)
    exported=export_tables(output,observations,context)
    # Questionnaire calculations are an independent E8/E9 import, not an
    # automatic extra cohort attached to every clinical execution/recovery.
    demo_export=None
    if survey_example:
        import_root=output/'constructed-import-example';import_root.mkdir(exist_ok=True)
        survey=constructed_survey(import_root)
        import_context={**context,'source_kind':'constructed_import_example_not_hospital_or_people','participant_rows':survey}
        write_json(import_root/'context.json',import_context)
        demo_export=export_tables(import_root,observations,import_context)
    final={'state':'finished','case_configurations':len(all_bundles),
        'case_status_counts':dict(Counter(b['status'] for b in all_bundles)),
        'actor_calls_this_process':actor.calls,'judge_calls_this_process':judge.calls,
        'medical_model_calls_this_process':medical.calls if medical else 0,
        'exports':exported,'constructed_import_exports':demo_export,
        'formal_eligible':False,'source_kind':'constructed actual API execution',
        'output':str(output),'real_hospital_results':0,'real_participant_responses':0}
    write_json(output/'summary.json',final);write_json(output/'progress.json',{**final,'pid':os.getpid()})
    print(json.dumps(final,ensure_ascii=False),flush=True)
    return final

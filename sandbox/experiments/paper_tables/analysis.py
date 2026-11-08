"""Reanalyse saved real API outputs without rerunning their clinical workflows."""
from __future__ import annotations
import argparse,hashlib,json,shutil
from collections import Counter,defaultdict
from copy import deepcopy
from datetime import datetime,timedelta,timezone
from pathlib import Path
from json_repair import repair_json
from .model_api import ModelAPI
from .model_evaluation import (repair_risk_annotations,validate_answer,document_outputs,
    missing_output_stage,assess_frozen_document)
from .run import model_spec,write_json
from .table_export import export_tables


def read(path): return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def normalize_attestation_timeline(observations,output):
    """Repair constructed timestamps from actually saved attestation activity."""
    records=[];evidence=Path(output).resolve()/'timeline-corrections.json'
    for row in observations:
        result=row.get('data',{}).get('result',{})
        if (12 not in row.get('table_ids',[]) or result.get('status')!='completed' or
            result.get('turnaround_time_source')!='constructed_activity_timeline_not_hospital_measurement'):
            continue
        activities=[activity for activity in row['data'].get('activities',[])
            if activity.get('role')=='doctor' and activity.get('activity_type')=='attestation']
        if not activities: continue
        end=max(activity['end_sim_s'] for activity in activities)
        after=(datetime(2026,1,1,tzinfo=timezone.utc)+timedelta(seconds=end)).isoformat()
        before=result.get('physician_attested')
        if before==after: continue
        records.append({'case_id':row['case_id'],'config_id':row['config_id'],
            'before':before,'after':after,'attestation_activity_ids':[a['activity_id'] for a in activities],
            'source':'saved constructed activity end_sim_s; not a hospital timestamp',
            'reason':'include completion of the actually saved 20-second attestation activity',
            'original_evidence_refs':row.get('evidence_refs',[])[:]})
        result['original_physician_attested_before_activity']=before
        result['physician_attested']=after
        row['timeline_normalization_version']='attestation-activity-end-20261025-1'
        row['evidence_refs']=row.get('evidence_refs',[])+[str(evidence)]
    write_json(evidence,{'records':records,'new_api_calls':0,
        'original_clinical_results_overwritten':False})
    return records


def reexport_saved(source,output):
    """Recalculate actual saved observations; no clinical or judge replay."""
    source=Path(source).resolve();output=Path(output).resolve()
    if source==output or output.exists():
        raise ValueError('recalculation requires a new output directory')
    observations=read(source/'observations.json');context=read(source/'table-context.json')
    correction=read(source/'corrections.json') if (source/'corrections.json').is_file() else {}
    output.mkdir(parents=True)
    for name in ('observations.json','table-context.json','corrections.json'):
        if (source/name).is_file(): shutil.copy2(source/name,output/name)
    timeline=normalize_attestation_timeline(observations,output)
    write_json(output/'observations.json',observations)
    previous=source/'tables/table_10.csv'
    if previous.is_file(): shutil.copy2(previous,output/'table_10_before_first_pass_fix.csv')
    provenance={'source_snapshot':str(source),'new_model_calls':0,
        'input_sha256':{name:hashlib.sha256((source/name).read_bytes()).hexdigest()
            for name in ('observations.json','table-context.json')},
        'reason':'Recalculate the same real API outputs after the known-false first-pass handling fix; preserve failed cases and source labels.'}
    write_json(output/'recalculation-provenance.json',provenance)
    exported=export_tables(output,observations,context)
    cases={(row.get('case_id'),row.get('config_id')) for row in observations}
    summary={'source_run':correction.get('source_run'),'source_snapshot':str(source),
        'case_configurations':len(cases),'corrected':len(correction.get('corrected',[])),
        'uncorrected':len(correction.get('uncorrected',[])),
        'focused_risk_calls_this_process':0,'timeline_corrections':len(timeline),
        'exports':exported,'formal_eligible':False}
    write_json(output/'summary.json',summary)
    print(json.dumps({'recalculated_cases':len(cases),'new_api_calls':0,'export':exported},ensure_ascii=False),flush=True)


def _cached_observations(paths):
    indexed={};provenance=[]
    for path in paths:
        path=Path(path).resolve();rows=read(path)
        provenance.append({'path':str(path),'sha256':hashlib.sha256(path.read_bytes()).hexdigest()})
        grouped=defaultdict(list)
        for row in rows:grouped[row['case_id'],row['config_id']].append(row)
        for pair,values in grouped.items():indexed[pair]=(values,str(path))
    return indexed,provenance


def _normalise_stage_layout(answer,outputs):
    """Move explicitly named actual fields, without adding or inferring labels."""
    answer=deepcopy(answer);changes=[]
    if not isinstance(answer,dict):raise ValueError('judge answer is not an object')
    stages=answer.get('stages')
    if not isinstance(stages,dict):raise ValueError('judge stages is not an object')
    for stage in outputs:
        if stage not in stages and isinstance(answer.get(stage),dict):
            stages[stage]=answer[stage];changes.append('move_root_'+stage+'_into_stages')
        value=stages.get(stage)
        if not isinstance(value,dict):continue
        if isinstance(value.get('claims'),list):
            annotations=value.get('annotations')
            if not isinstance(annotations,list):continue
            existing={a.get('claim_id'):a for a in annotations if a.get('unit')=='claim'}
            for claim in value['claims']:
                if not isinstance(claim,dict) or claim.get('unit')!='claim':
                    raise ValueError('separate claims entry lacks explicit unit=claim')
                if claim.get('claim_id') in existing:
                    if existing[claim['claim_id']]!=claim:raise ValueError('conflicting duplicate claim fields')
                else:annotations.append(deepcopy(claim))
            changes.append('merge_explicit_claims_into_annotations_'+stage)
    return answer,changes


def _document_observations(bundle,result,activities,evaluation,destination):
    original=bundle['observations']
    common={k:deepcopy(v) for k,v in original[0].items() if k not in ('table_ids','data','stage','artifact_version')}
    common['evidence_refs']=list(dict.fromkeys(common.get('evidence_refs',[])+evaluation['evidence_refs']+
        [str((destination/'evaluation.json').resolve())]))
    common['analysis_version']='saved-output-analysis-20261025-4'
    first=evaluation['stages'].get('first_draft');final=evaluation['stages'].get('final_signed')
    result=deepcopy(result)
    result.update(quality_source='model_review',
        qualified=(final['qualified'] and result['status']=='completed') if final else None,
        first_draft_accepted=first['qualified'] if first else None)
    quality={**final,'module_inventory_complete':True} if final else {
        'evaluation_failure':'Signed-final-stage automatic evaluation remains unresolved'}
    return [{**common,'table_ids':[8],'stage':'final_signed',
        'artifact_version':len(result.get('artifact_versions',[])),'data':quality},
        {**common,'table_ids':[12],'stage':'final_signed','data':{
            'result':result,'activities':activities,'activity_capture_complete':True}}]


def analyse(run,output,*,completed_configurations_only=False,cache_observations=(),
            supplement_observations=(),allow_model_corrections=False,plan_only=False):
    run=Path(run).resolve();output=Path(output).resolve()
    if output==run or (output/'summary.json').exists():
        raise ValueError('analysis requires a new output directory; original/final analysis is never overwritten')
    output.mkdir(parents=True,exist_ok=True)
    cached,cache_provenance=_cached_observations(cache_observations)
    supplements,supplement_provenance=_cached_observations(supplement_observations)
    api=None
    def model_api():
        nonlocal api
        if not allow_model_corrections:raise ValueError('Actual model correction required; see pending-model-corrections.json')
        if api is None:
            frozen=read(run/'experiment-config.json')
            judge=frozen.get('model_roles',{}).get('foundation') or model_spec()
            api=ModelAPI({**judge,'temperature':0,'max_tokens':2200,'max_calls':100},output/'correction-api')
        return api
    completed_pairs=None;excluded_partial=0
    if completed_configurations_only:
        captured=(run/'observations.json').read_bytes()
        saved=json.loads(captured)
        completed_pairs={(row['case_id'],row['config_id']) for row in saved}
        (output/'source-observations-snapshot.json').write_bytes(captured)
        write_json(output/'snapshot-scope.json',{'completed_configurations_only':True,
            'unique_case_configuration_pairs':len(completed_pairs),
            'source_sha256':hashlib.sha256(captured).hexdigest(),
            'boundary':'clinical batch writes observations only after the whole configuration has finished'})
    references={v['case_id']:v for v in (json.loads(line) for line in (run/'dataset/reference.jsonl').read_text(encoding='utf-8').splitlines() if line.strip())}
    judge_index={}
    index=run/'judge-api/requests.jsonl'
    if index.exists():
        for line in index.read_text(encoding='utf-8').splitlines():
            if not line.strip(): continue
            row=json.loads(line)
            if row.get('role')=='automatic_output_judge':
                judge_index[(row['case_id'],row['config_id'])]=run/'judge-api'/(row['request_id']+'.json')
    all_observations=[];corrected=[];uncorrected=[];cases=0
    stage_corrections=[];pending=[];case_statuses=Counter();risk_reused=0;supplement_reused=0
    for path in sorted((run/'clinical').rglob('observation.json')):
        if completed_pairs is not None:
            relative=path.relative_to(run/'clinical')
            pair=(relative.parts[1],relative.parts[0].replace('__',':'))
            if pair not in completed_pairs:
                excluded_partial+=1;continue
        bundle=read(path);cases+=1;original=bundle['observations']
        case_statuses[bundle['status']]+=1
        config_id=bundle['config_id'];case_id=bundle['case_id'];pair=(case_id,config_id)
        result=read(path.parent/'result.json');task=result['task']
        broken=any(row.get('data',{}).get('evaluation_failure') for row in original)
        destination=output/'corrected'/config_id.replace(':','__')/case_id
        cache=destination/'observations.json'
        if pair in supplements:
            rows,source=supplements[pair]
            if any(row.get('input_sha256')!=original[0].get('input_sha256') for row in rows):
                raise ValueError('supplement input hash differs from the original case/configuration')
            all_observations.extend(deepcopy(rows));supplement_reused+=1
            corrected.append({'case_id':case_id,'config_id':config_id,'supplement':source});continue
        if task=='preconsult' and broken and pair in cached:
            rows,source=cached[pair]
            if any(row.get('data',{}).get('evaluation_failure') for row in rows):
                raise ValueError('cached risk correction still has evaluation failure')
            if any(row.get('input_sha256')!=original[0].get('input_sha256') for row in rows):
                raise ValueError('risk-cache input hash differs from the original case/configuration')
            all_observations.extend(deepcopy(rows));risk_reused+=1
            corrected.append({'case_id':case_id,'config_id':config_id,'risk_cache':source});continue
        if task!='documentation' and not broken:
            all_observations.extend(original);continue
        try:
            activities=read(path.parent/'activities.json')
            reference=references[case_id];task=result['task']
            original_evaluation=path.parent/'evaluation.json'
            raw_path=judge_index.get(pair)
            extra=[];changes=[]
            if original_evaluation.exists():
                evaluation=read(original_evaluation);answer={'stages':evaluation['stages']}
                extra=evaluation.get('evidence_refs',[])+[str(original_evaluation)]
            elif raw_path is not None:
                raw=read(raw_path)
                choices=raw.get('response_body',{}).get('choices')
                if choices:
                    content=choices[0]['message']['content']
                    try: answer=json.loads(content)
                    except ValueError: answer=repair_json(content,return_objects=True)
                else:answer={'stages':{}}
                extra=[str(raw_path)]
            else:answer={'stages':{}}
            if task=='preconsult':
                outputs={'first_handoff':result.get('first_handoff_output'),'doctor_completed':result.get('post_consultation_output')}
                answer,focused=repair_risk_annotations(model_api(),answer,outputs,reference,
                    task=task,case_id=case_id,config_id=config_id)
                extra+=focused
                stages=validate_answer(answer,task,outputs,reference)
            else:
                outputs=document_outputs(result)
                answer,changes=_normalise_stage_layout(answer,outputs)
                stages={};stage_errors=[]
                for stage,content in outputs.items():
                    if not content:
                        stages[stage]=missing_output_stage(reference,task='documentation')
                        changes.append(stage+'_known_absent_output')
                        continue
                    try:
                        stage_answer={'stages':{stage:answer.get('stages',{}).get(stage)}}
                        stages[stage]=validate_answer(stage_answer,task,{stage:content},reference)[stage]
                    except (ValueError,TypeError,KeyError,AttributeError) as exc:
                        item={'case_id':case_id,'config_id':config_id,'stage':stage,'reason':str(exc),
                            'source_result_path':str(path.parent/'result.json'),
                            'source_result_sha256':hashlib.sha256((path.parent/'result.json').read_bytes()).hexdigest(),
                            'original_judge_evidence':str(raw_path) if raw_path else None,
                            'source_kind':result.get('source_kind'),'clinical_validation':result.get('clinical_validation'),
                            'reference_source_kind':reference.get('source_kind'),
                            'reference_clinical_verification':reference.get('clinical_verification'),
                            'minimal_payload':{'task':'documentation','requested_stage':stage,
                                'reference':{'required_items':reference['required_items']},'outputs':{stage:content}}}
                        if allow_model_corrections and not plan_only:
                            try:
                                if result.get('source_kind')!='constructed' or reference.get('source_kind')!='engineering_example' or reference.get('clinical_verification') is not False:
                                    raise ValueError('New model corrections are restricted to the verified constructed dataset')
                                fixed=assess_frozen_document(model_api(),{stage:content},reference,case_id=case_id,config_id=config_id)
                                stages[stage]=fixed['stages'][stage];extra+=fixed['evidence_refs']
                                changes.append(stage+'_actual_saved_output_real_api_reassessment')
                            except Exception as supplement_exc:
                                item['correction_error']=str(supplement_exc);stage_errors.append(item)
                        else:stage_errors.append(item)
                pending.extend(stage_errors)
                for error in stage_errors:
                    uncorrected.append({key:error[key] for key in ('case_id','config_id','stage','reason')})
                stage_corrections.append({'case_id':case_id,'config_id':config_id,
                    'clinical_status':result['status'],'stage_output_present':{key:value is not None for key,value in outputs.items()},
                    'structural_changes':changes,'unresolved_stages':[item['stage'] for item in stage_errors],
                    'original_judge_refs':extra[:]})
            evaluation={'stages':stages,'evidence_refs':list(dict.fromkeys(extra)),
                'evaluation_source':'automatic_model_review_not_clinician_validation',
                'correction_reason':'Re-use frozen actual judge labels; normalize explicit field layout and require true signed-final provenance.',
                'structural_changes':changes,'formal_eligible':False}
            if not plan_only:write_json(destination/'evaluation.json',evaluation)
            common={k:deepcopy(v) for k,v in original[0].items() if k not in ('table_ids','data','stage','artifact_version')}
            common['evidence_refs']+=evaluation['evidence_refs']+[str((destination/'evaluation.json').resolve())]
            common['analysis_version']='saved-output-analysis-20261025-4'
            result['quality_source']='model_review'
            if task=='preconsult':
                first,final=stages['first_handoff'],stages['doctor_completed']
                result.update(qualified=final['qualified'] and result['status']=='completed',
                    first_handoff_qualified=first['qualified'],
                    downstream_supplementation_or_correction=result.get('downstream_supplementation'),
                    first_pass_completed=first['qualified'] and result.get('downstream_supplementation') is False)
                observations=[{**common,'table_ids':[5,7],'stage':'first_handoff','data':{**first,'module_inventory_complete':True}},
                    {**common,'table_ids':[10],'stage':'doctor_completed','data':{'result':result,'activities':activities,'activity_capture_complete':True}}]
            else:
                observations=_document_observations(bundle,result,activities,evaluation,destination)
            if not plan_only:write_json(cache,observations)
            all_observations.extend(observations)
            if broken or changes:corrected.append({'case_id':case_id,'config_id':config_id,'evidence':evaluation['evidence_refs']})
        except Exception as exc:
            all_observations.extend(original)
            uncorrected.append({'case_id':case_id,'config_id':config_id,'error':type(exc).__name__,'detail':str(exc)})
    plan={'tasks':pending,'count':len(pending),
        'generator_source':'MedicalAgent/sandbox/experiments/paper_tables/materials.py',
        'generator_sha256':hashlib.sha256((Path(__file__).parent/'materials.py').read_bytes()).hexdigest(),
        'dataset_catalog_note':read(run/'dataset/data_catalog.json').get('note'),
        'source_run':str(run),'formal_eligible':False}
    write_json(output/'pending-model-corrections.json',plan)
    if not (output/'planned-model-corrections.json').exists():
        write_json(output/'planned-model-corrections.json',plan)
    write_json(output/'stage-corrections.json',stage_corrections)
    write_json(output/'analysis-provenance.json',{'cache_observations':cache_provenance,
        'supplement_observations':supplement_provenance,'risk_cached_cases':risk_reused,
        'supplement_cases':supplement_reused,'case_status_counts':dict(case_statuses),
        'clinical_reruns':0,'new_api_calls':api.calls if api else 0,
        'all_original_clinical_results_untouched':True})
    if plan_only:
        print(json.dumps({'planned_cases':cases,'risk_cached_cases':risk_reused,'supplement_cases':supplement_reused,
            'pending_model_stages':len(pending),'uncorrected':len(uncorrected),'new_api_calls':0}),flush=True)
        return
    context=read(run/'table-context.json')
    timeline=normalize_attestation_timeline(all_observations,output)
    write_json(output/'observations.json',all_observations)
    write_json(output/'table-context.json',context)
    write_json(output/'corrections.json',{'source_run':str(run),'cases_in_snapshot':cases,'corrected':corrected,'uncorrected':uncorrected})
    report=export_tables(output,all_observations,context)
    write_json(output/'summary.json',{'source_run':str(run),'case_configurations':cases,'corrected':len(corrected),
        'uncorrected':len(uncorrected),'focused_risk_calls_this_process':api.calls if api else 0,
        'case_status_counts':dict(case_statuses),'risk_cached_cases':risk_reused,
        'supplement_cases':supplement_reused,'pending_model_stages':len(pending),
        'completed_configurations_only':completed_configurations_only,'excluded_partial_jobs':excluded_partial,
        'timeline_corrections':len(timeline),'exports':report,'formal_eligible':False})
    print(json.dumps({'snapshot_cases':cases,'corrected':len(corrected),'uncorrected':len(uncorrected),
                      'exported_complete_tables':report['complete_tables']},ensure_ascii=False),flush=True)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    mode=parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--run');mode.add_argument('--saved-snapshot')
    parser.add_argument('--completed-configurations-only',action='store_true',
        help='Use only case/configuration pairs in the batch-completed observations snapshot')
    parser.add_argument('--output',required=True)
    parser.add_argument('--cache-observations',action='append',default=[])
    parser.add_argument('--supplement-observations',action='append',default=[])
    parser.add_argument('--allow-model-corrections',action='store_true')
    parser.add_argument('--plan-only',action='store_true')
    args=parser.parse_args()
    if args.saved_snapshot: reexport_saved(args.saved_snapshot,args.output)
    else: analyse(args.run,args.output,completed_configurations_only=args.completed_configurations_only,
        cache_observations=args.cache_observations,supplement_observations=args.supplement_observations,
        allow_model_corrections=args.allow_model_corrections,plan_only=args.plan_only)

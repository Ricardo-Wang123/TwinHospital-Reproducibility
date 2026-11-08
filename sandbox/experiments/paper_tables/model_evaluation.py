"""Separate model-judge calls over frozen outputs; not clinician validation."""
from __future__ import annotations
from copy import deepcopy
from .materials import MODULE_BY_FACT

JUDGE_PROMPT='''You assess constructed outpatient experiment outputs. You are an automatic model judge, not a clinician participant. Return JSON {"stages":{stage_id:{"annotations":[...],"risk_hits":[...],"handoff_usable":bool,"qualified":bool}}}.
For each supplied stage, list EVERY reference.required_items entry exactly once as {unit:"required_item",item_id:the supplied id,module:the supplied module,applicable:true,label:"correct"|"incorrect"|"omitted"}. Correct equivalent wording counts correct. A wrong value counts incorrect, not omitted. Missing/unknown where a known fact was supplied counts omitted. All information is fictional; assess correspondence, not medical advice.
Also split every positive factual assertion in that stage into atomic claims, each {unit:"claim",claim_id:unique string,text:claim,label:"correct"|"incorrect"|"unsupported"}. Do not invent claims. A statement that information was not provided is not a positive clinical claim. Empty/missing output has zero claims. Incorrect is contradicted by supplied reference; unsupported has no basis in source. References contain only the facts available for this experiment. No diagnosis or treatment can be inferred.
For each reference risk signal return {signal_id,hit:bool}. A risk is identified only when the summary explicitly flags that signal as a risk, beyond merely including it among ordinary facts; examine risk_flags. For documentation keep risk_hits empty. handoff_usable means usable without a critical correction or missing required information. qualified means every applicable required_item is correct, no incorrect/unsupported claim, and usable. Assess each stage independently. Do not use later outputs to excuse omissions in earlier stages.'''


def missing_output_stage(reference,*,task=None):
    return {'annotations':[{'unit':'required_item','item_id':r.get('item_id',r['fact_id']),
        'module':r.get('module',MODULE_BY_FACT[r['fact_id']]),'applicable':True,'label':'omitted'}
        for r in reference['required_items']],
        'risk_hits':[{'signal_id':s['signal_id'],'hit':False} for s in reference.get('risk_signals',[])] if task!='documentation' else [],
        'handoff_usable':False,'qualified':False,'source':'observed_missing_output'}


def document_outputs(result):
    """Read actual version stages; an unsigned draft is never a signed final note."""
    versions=result.get('artifact_versions') or []
    signed=[version for version in versions if version.get('status')=='locked'
        and version.get('signatory_id') and (version.get('attested_at') or
            result.get('physician_attested_at') or result.get('physician_attested'))]
    return {'first_draft':deepcopy(versions[0].get('content')) if versions else None,
        'final_signed':deepcopy(signed[-1].get('content')) if signed else None}


def assess(api,case,variant,result,reference):
    task=case['task'];outputs={}
    if task=='preconsult':
        outputs={'first_handoff':result.get('first_handoff_output'),
                 'doctor_completed':result.get('post_consultation_output')}
    else:
        outputs=document_outputs(result)
    if result['status']!='completed' and all(not value for value in outputs.values()):
        return {'stages':{stage:missing_output_stage(reference,task=task) for stage in outputs},
                'evidence_refs':[],'evaluation_source':'observed_missing_output'}
    if task=='documentation':
        return assess_frozen_document(api,outputs,reference,case_id=case['case_id'],
            config_id=variant['config_id'])
    response=api.complete(JUDGE_PROMPT,{'task':task,'reference':reference,'outputs':outputs},
        case_id=case['case_id'],role='automatic_output_judge',config_id=variant['config_id'])
    answer,extra_refs=repair_risk_annotations(api,response['answer'],outputs,reference,
        task=task,case_id=case['case_id'],config_id=variant['config_id'])
    stages=validate_answer(answer,task,outputs,reference)
    return {'stages':stages,'evidence_refs':[response['evidence']]+extra_refs,
            'evaluation_source':'automatic_model_review_not_clinician_validation'}


def assess_frozen_document(api,outputs,reference,*,case_id,config_id):
    """Judge each saved document stage separately, keeping every actual response.

    This addresses observed omitted-stage/omitted-item responses; it does not
    revise source text or infer the missing labels in Python.
    """
    stages={};refs=[]
    for stage,content in outputs.items():
        if not content:
            stages[stage]=missing_output_stage(reference,task='documentation')
            continue
        prompt=(JUDGE_PROMPT+'\nThis request is DOCUMENTATION ONLY. Return exactly ONE stage, '
            +stage+'. The supplied required_items inventory, including risk_signal when present, '
            'must still be fully assessed. risk_hits is NOT a documentation metric and MUST be []. '
            'Do not omit any supplied required item. Do not merge equal stages; even identical '
            'first and final text are assessed under their explicitly requested stage IDs.')
        response=api.complete(prompt,{'task':'documentation','requested_stage':stage,
            'required_item_ids':[item.get('item_id') or item['fact_id'] for item in reference['required_items']],
            'reference':{'required_items':deepcopy(reference['required_items'])},
            'outputs':{stage:content}},case_id=case_id,
            role='automatic_document_stage_judge',config_id=config_id)
        refs.append(response['evidence'])
        validated=validate_answer(response['answer'],'documentation',{stage:content},reference)
        stages[stage]=validated[stage]
    return {'stages':stages,'evidence_refs':refs,
        'evaluation_source':'automatic_model_review_not_clinician_validation',
        'evaluation_version':'separate-frozen-document-stages-20261025-1'}


def repair_risk_annotations(api,answer,outputs,reference,*,task,case_id,config_id):
    """A missing judge field triggers a focused real call, never a guessed hit."""
    if task!='preconsult': return answer,[]
    expected={s['signal_id'] for s in reference.get('risk_signals',[])}
    stages=answer.get('stages',{})
    invalid=any(not isinstance(stages.get(stage,{}).get('risk_hits'),list) or
        {h.get('signal_id') for h in stages.get(stage,{}).get('risk_hits',[]) if isinstance(h,dict)}!=expected
        for stage in outputs)
    if not invalid: return answer,[]
    if not expected:
        for stage in outputs: stages[stage]['risk_hits']=[]
        return answer,[]
    prompt=('Return JSON {"by_stage":{stage_id:{signal_id:boolean}}}. Include EVERY supplied stage '
        'and EVERY supplied reference signal ID, even when its value is false. A hit requires '
        'the same signal explicitly marked in summary risk_flags. Merely including it among '
        'ordinary facts is not a hit. Empty risk_flags means false. This is an automatic '
        'annotation of constructed experiment output, not clinical validation.')
    response=api.complete(prompt,{'reference_signals':reference.get('risk_signals',[]),
        'summary_risk_flags':{stage:(value or {}).get('risk_flags',[]) for stage,value in outputs.items()}},
        case_id=case_id,role='automatic_risk_flag_judge',config_id=config_id)
    by_stage=response['answer'].get('by_stage')
    if not isinstance(by_stage,dict) or set(by_stage)!=set(outputs):
        raise ValueError('focused risk judge omitted a stage')
    for stage in outputs:
        hits=by_stage[stage]
        if not isinstance(hits,dict) or set(hits)!=expected or any(type(v) is not bool for v in hits.values()):
            raise ValueError('focused risk judge returned invalid signal inventory')
        stages[stage]['original_judge_risk_hits']=stages[stage].get('risk_hits')
        stages[stage]['risk_hits']=[{'signal_id':key,'hit':value} for key,value in hits.items()]
        stages[stage]['risk_annotation_source']='separate_actual_model_call_after_missing_field'
    return answer,[response['evidence']]


def validate_answer(answer,task,outputs,reference):
    # Validation annotates normalized fields, so preserve the caller's raw parsed answer.
    answer=deepcopy(answer)
    stages=answer.get('stages')
    if not isinstance(stages,dict) or set(stages)!=set(outputs):
        raise ValueError('judge response must contain exactly the requested stages')
    expected={item.get('item_id',item['fact_id']):item for item in reference['required_items']}
    for stage,value in stages.items():
        if not isinstance(value.get('annotations'),list): raise ValueError('judge annotations missing')
        required={};claim_ids=set()
        for annotation in value['annotations']:
            if annotation.get('unit')=='required_item':
                item_id=annotation.get('item_id')
                if item_id not in expected or item_id in required: raise ValueError('unknown/duplicate required item')
                if annotation.get('module')!=expected[item_id]['module']: raise ValueError('judge changed required module')
                if annotation.get('label') not in ('correct','incorrect','omitted'): raise ValueError('invalid required-item label')
                annotation['applicable']=True;required[item_id]=annotation
            elif annotation.get('unit')=='claim':
                claim_id=annotation.get('claim_id')
                if not claim_id or claim_id in claim_ids: raise ValueError('missing/duplicate atomic claim id')
                if annotation.get('label') not in ('correct','incorrect','unsupported'): raise ValueError('invalid claim label')
                claim_ids.add(claim_id)
            else: raise ValueError('judge annotation unit must be claim or required_item')
        if set(required)!=set(expected): raise ValueError('judge omitted required inventory entries')
        for flag in ('handoff_usable','qualified'):
            if type(value.get(flag)) is not bool: raise ValueError('judge '+flag+' must be boolean')
        references={s['signal_id'] for s in reference.get('risk_signals',[])} if task=='preconsult' else set()
        hits=value.get('risk_hits',[])
        if task=='documentation':
            # Observed judges copied the reference's risk signals into an inapplicable
            # preconsultation-only field. Discarding this field is structural N/A,
            # never a clinical re-score; retain it and keep required risk_signal labels.
            if hits:
                value['original_judge_risk_hits']=deepcopy(hits)
                value['risk_hits_normalization']='not_applicable_to_documentation'
            value['risk_hits']=[]
            hits=[]
        if not isinstance(hits,list) or {h.get('signal_id') for h in hits}!=references or any(type(h.get('hit')) is not bool for h in hits):
            raise ValueError('judge risk hits do not match reference signals')
        if task=='preconsult' and not (outputs[stage] or {}).get('risk_flags'):
            for hit in hits: hit['hit']=False
        # Qualification cannot overrule the judge's explicit item/claim labels.
        value['qualified']=value['qualified'] and all(a['label']=='correct' for a in value['annotations'])
        if not outputs[stage]: value['qualified']=False
    return stages

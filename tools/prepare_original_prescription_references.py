"""Read completed batch journals and prepare local original-Rx reference forms.

No provider or HIS is contacted. Include every planned source patient, including
interrupted runs; absent original inputs stay absent. Public output is aggregate
only. Patient text, identity links and candidate rule findings remain private.
"""
import argparse
from collections import Counter, defaultdict
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import html
import json
import os
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from sandbox.experiments.linked.clinical_reference_sources import assess, review_packet, PROTOCOL
from sandbox.experiments.linked.clinical_faults import registry
from sandbox.experiments.linked.contracts import digest, runtime_case
from sandbox.experiments.linked.safe_context_adapter import VisibleDocumentedCandidatePPRS
from sandbox.experiments.linked.provider import private_json


def sha(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def prepare(batch, case_manifest, catalogue_path, private_dir, public_dir):
    batch=Path(batch);private_dir=Path(private_dir);public_dir=Path(public_dir)
    if private_dir.exists() or public_dir.exists():raise FileExistsError('keep prior material versions')
    sources={}
    def read(path):
        path=Path(path);raw=path.read_bytes();sources[str(path)]=hashlib.sha256(raw).hexdigest()
        return json.loads(raw)
    summary=read(batch/'batch_summary.json');spec=read(batch/'specification.json')
    if spec.get('mode')!='linked':raise ValueError('use a linked original-prescription anchor batch')
    manifest=read(case_manifest);catalogue=read(catalogue_path)
    declared=spec.get('rules',{}).get('source_catalogue_file_sha256')
    if not declared or declared!=sha(catalogue_path):raise ValueError('catalogue does not match frozen batch')
    if spec['rules'].get('registry_hash')!=digest(registry()):
        raise ValueError('rule registry does not match frozen batch')
    entries={c['case_id']:c for c in manifest['cases']}
    if len(entries)!=len(manifest['cases']):raise ValueError('duplicate case IDs in source manifest')
    planned={r['case_id']:r for r in spec['cases']}
    indexed={r['case_id']:r for r in summary['cases']}
    if len(planned)!=len(spec['cases']) or len(indexed)!=len(summary['cases']) or set(planned)!=set(indexed):
        raise ValueError('batch accounting must include every planned case exactly once')
    coordinator=[];forms=[];counts=defaultdict(Counter);missing=Counter();replayed=0;patients=set()
    for case_id,expected in planned.items():
        entry=entries.get(case_id)
        if not entry:raise ValueError('source case absent from manifest')
        case=read(entry['path'])
        if sha(entry['path'])!=expected['file_sha256'] or digest(runtime_case(case))!=expected['runtime_input_hash']:
            raise ValueError('case input does not match frozen batch')
        if any(case.get(k)!=entry.get(k) for k in ('case_id','patient_key','group','split','data_kind')):
            raise ValueError('case identity differs from source manifest')
        if case['patient_key'] in patients:raise ValueError('this source pack requires one encounter per source patient')
        patients.add(case['patient_key'])
        row=indexed[case_id]['attempts'].get('linked_baseline_anchor')
        if not row:raise ValueError('planned original anchor missing from batch index')
        token='rxref_'+digest({'batch_spec':sources[str(batch/'specification.json')],'case_id':case_id})[:24]
        key={'review_id':token,'case_id':case_id,'patient_key':case['patient_key'],
            'group':case['group'],'source_status':row['status'],'source_case_hash':digest(runtime_case(case))}
        path=row.get('result_path')
        if not path:
            missing['original_run_absent']+=1;coordinator.append({**key,'availability':'original_run_absent'});continue
        path=Path(path).resolve()
        if not path.is_relative_to(batch.resolve()):raise ValueError('source run outside declared batch')
        run=read(path)
        if (run.get('case_id')!=case_id or run.get('input_hash')!=expected['runtime_input_hash']
                or run.get('status')!=row['status'] or run.get('evaluation_unit')!='complete_encounter'):
            raise ValueError('run binding differs from frozen source')
        stage=run.get('stage_inputs',{}).get('prescription')
        if stage is None:
            missing['original_prescription_input_absent']+=1
            coordinator.append({**key,'availability':'original_prescription_input_absent','source_run_sha256':sha(path)})
            continue
        value=assess(stage,case['formulary'],catalogue['products'])
        # Validate replay against the real original working-system assessment;
        # never use that assessment as the independent accuracy reference.
        recorded=run.get('artifacts',{}).get('original_prescription_review')
        if run['status']=='completed' and recorded is None:
            raise ValueError('completed encounter lacks its original prescription review')
        if recorded is not None:
            if not isinstance(recorded,dict) or not isinstance(recorded.get('rule_review'),dict):
                raise ValueError('archived original review lacks its working rule assessment')
            if recorded.get('prescription_hash')!=digest(stage['prescription']):
                raise ValueError('archived original review is bound to a different prescription')
            original=recorded['rule_review']
            replay=VisibleDocumentedCandidatePPRS(case['formulary'],catalogue['products']).review(stage['prescription'],stage['clinical_context'])
            if replay!=original:raise ValueError('replayed rule assessment differs from archived original')
            replayed+=1
        for finding in value['rule_applicability']:counts[finding['category']][finding['status']]+=1
        forms.append(review_packet(token,value))
        coordinator.append({**key,'availability':'original_prescription_available',
            'source_run_sha256':sha(path),'source_run_path':str(path),'reference_material':value})
    if not all(sha(path)==h for path,h in sources.items()):raise ValueError('source changed during preparation')
    private_dir.mkdir(parents=True,mode=0o700)
    private_json(private_dir/'coordinator_only.json',{'protocol':PROTOCOL,'sources_sha256':sources,'items':coordinator})
    for name in ('reviewer_1','reviewer_2'):
        # A separate deterministic order per reviewer; no prediction or outcome field.
        ordered=sorted(deepcopy(forms),key=lambda r:digest({'reviewer':name,'review_id':r['review_id']}))
        private_json(private_dir/(name+'.json'),{'protocol':PROTOCOL,'items':ordered})
    private_json(private_dir/'shared_rule_materials.json',registry())
    private_json(private_dir/'shared_product_sources.json',catalogue)
    private_json(private_dir/'adjudication_blank.json',{'status':'awaiting_two_independent_reviews',
        'items':[{'review_id':r['review_id'],'reviewer_1_received':False,'reviewer_2_received':False,
            'adjudicated_evidence_sufficient':None,'adjudicated_appropriateness':None,
            'adjudicated_error_categories':None,'basis_refs':[],'rationale':None} for r in forms]})
    private_json(private_dir/'review_status.json',{'status':'independent_review_pending','human_ratings_completed':0,
        'clinical_reference_qualified':0,'clinical_gold_labels':0})
    public={'generated_at':datetime.now(timezone.utc).isoformat(),'protocol':PROTOCOL,
        'planned_patients':len(planned),'distinct_source_patients':len(patients),
        'original_prescription_inputs':len(forms),'missing_input_counts':dict(missing),
        'rule_assessments_replayed_identically':replayed,
        'original_inputs_without_recorded_rule_review':len(forms)-replayed,
        'applicability_counts':{k:dict(v) for k,v in sorted(counts.items())},
        'review_packets_per_reviewer':len(forms),'human_ratings_completed':0,
        'clinical_gold_labels':0,'native_writes':0,'model_calls':0,
        'source_files_unchanged':True,
        'interpretation':'Scoped rule preconditions are not independent labels or whole-prescription normality. No source patient was selected or excluded using model outcomes. No faults were injected by this audit.'}
    public_dir.mkdir(parents=True)
    (public_dir/'summary.json').write_text(json.dumps(public,ensure_ascii=False,indent=2)+'\n')
    table=''.join('<tr><td>'+html.escape(k)+'</td>'+''.join('<td>'+str(v.get(s,0))+'</td>' for s in ('scoped_nonviolation','violation','indeterminate','not_applicable'))+'</tr>' for k,v in public['applicability_counts'].items())
    (public_dir/'index.html').write_text('<!doctype html><meta charset="utf-8"><title>原始处方参照准备</title><style>body{font:16px/1.7 system-ui;max-width:1000px;margin:40px auto;padding:20px}td,th{padding:12px;border-bottom:1px solid #ddd;text-align:left}table{border-collapse:collapse}</style><h1>实际原始处方：注错条件与独立参照</h1><p>计划来源患者 '+str(len(planned))+' 名；可用原始处方 '+str(len(forms))+' 份。保留中断病例，未用模型结果筛选。</p><table><tr><th>类别</th><th>满足窄规则基线条件</th><th>窄规则已有违规</th><th>依据不足</th><th>不适用</th></tr>'+table+'</table><p>“满足窄规则基线条件”不等于整张处方正常。工作规则的判定不能成为评价自身准确率的真值。两位独立评审材料已准备，人工评分及正式标签仍为0；本步骤没有调用模型、注错或写入医院。</p>')
    return public


if __name__=='__main__':
    os.umask(0o077)
    p=argparse.ArgumentParser(description=__doc__)
    for field in ('batch','case-manifest','catalogue','private-dir','public-dir'):p.add_argument('--'+field,required=True,type=Path)
    a=p.parse_args();print(json.dumps(prepare(a.batch,a.case_manifest,a.catalogue,a.private_dir,a.public_dir),ensure_ascii=False))

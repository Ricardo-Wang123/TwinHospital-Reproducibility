"""Prepare private route interventions from audited original inputs; no execution."""
import argparse
from collections import Counter
from datetime import datetime,timezone
import hashlib
import html
import json
import os
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from sandbox.experiments.linked.contracts import digest,paper_conditions
from sandbox.experiments.linked.order_encoding import preflight
from sandbox.experiments.linked.prescription_variants import (
    make_route_variant,validate_variant,continuation_state,registry,POLICY,VariantNotApplicable)
from sandbox.experiments.linked.provider import private_json


def sha(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def prepare(source_review,native_catalogue,private_dir,public_dir):
    source_review=Path(source_review);native_catalogue=Path(native_catalogue)
    private_dir=Path(private_dir);public_dir=Path(public_dir)
    if private_dir.exists() or public_dir.exists():raise FileExistsError('retain prior material versions')
    source=json.loads(source_review.read_text());protected=source['sources_sha256'].copy()
    protected[str(source_review)]=sha(source_review);protected[str(native_catalogue)]=sha(native_catalogue)
    def verify():
        if any(sha(p)!=h for p,h in protected.items()):raise ValueError('audited source files changed')
    verify()
    specs=[Path(p) for p in protected if Path(p).name=='specification.json']
    if len(specs)!=1:raise ValueError('one frozen source batch specification is required')
    spec=json.loads(specs[0].read_text());mapping=json.loads(native_catalogue.read_text())
    if digest(mapping)!=spec.get('catalog_hash'):raise ValueError('native mapping differs from frozen source batch')
    conditions=[c.to_dict() for c in paper_conditions('prescription',spec['models']['tool_models'])]
    rows=[];bundles=[];status=Counter();source_status=Counter();encoding=Counter()
    for item in source['items']:
        source_status[item['source_status']]+=1
        row={k:item[k] for k in ('review_id','case_id','patient_key','group','source_status')}
        if item['availability']!='original_prescription_available':
            row['status']='original_prescription_unavailable';rows.append(row);status[row['status']]+=1;continue
        material=item['reference_material'];input=material['independent_review_input']
        stage={k:input[k] for k in ('prescription','clinical_context')}
        if digest(stage)!=material['stage_input_hash']:raise ValueError('original input hash mismatch')
        path=Path(item['source_run_path'])
        if sha(path)!=item['source_run_sha256']:raise ValueError('source run changed')
        run=json.loads(path.read_text())
        if (run.get('case_id')!=item['case_id'] or run.get('input_hash')!=item['source_case_hash']
                or run.get('stage_inputs',{}).get('prescription')!=stage):
            raise ValueError('source run and reviewed original input differ')
        try:variant=make_route_variant(stage,input['formulary'])
        except VariantNotApplicable as exc:
            row.update(status='variant_not_applicable',reason=str(exc))
        else:
            validate_variant(variant,input['formulary'])
            continuation=continuation_state(run,variant,input['formulary'])
            base_check=preflight(stage['prescription'],mapping)
            changed_check=preflight(variant['stage_input']['prescription'],mapping)
            encoding['source_native_encodable' if base_check['native_encodable'] else 'source_native_unencodable']+=1
            encoding['variant_native_encodable' if changed_check['native_encodable'] else 'variant_native_unencodable']+=1
            filename=variant['variant_id']+'.json'
            bundle={'variant':variant,'continuation_state':continuation,
                'source_case_id':item['case_id'],'source_run_sha256':item['source_run_sha256'],
                'source_native_encoding':base_check,'variant_native_encoding':changed_check,
                'tested_conditions':conditions,'actual_system_runs':0,'formal_negative_control_qualified':False}
            bundles.append((filename,bundle))
            row.update(status='variant_prepared',file=filename,variant_hash=digest(variant),
                source_stage_input_hash=variant['source_stage_input_hash'],stage_input_hash=variant['stage_input_hash'])
        rows.append(row);status[row['status']]+=1
    if len({r['case_id'] for r in rows})!=len(rows):raise ValueError('duplicate source case in reviewed material')
    verify()
    private_dir.mkdir(parents=True,mode=0o700)
    for filename,bundle in bundles:private_json(private_dir/filename,bundle)
    private_json(private_dir/'manifest.json',{'policy':POLICY,'registry_hash':digest(registry()),
        'sources_sha256':protected,'conditions':conditions,'cases':rows})
    summary={'generated_at':datetime.now(timezone.utc).isoformat(),'policy':POLICY,
        'source_prescriptions':len(rows),'source_status_counts':dict(source_status),
        'preparation_status_counts':dict(status),'native_encoding_checks':dict(encoding),
        'paired_conditions_per_variant':len(conditions),'actual_system_runs':0,'model_calls':0,'native_writes':0,
        'scoped_route_interventions':len(bundles),'covered_clinical_error_categories':['administration_route_error'] if bundles else [],
        'remaining_clinical_error_categories':['dosage_error','drug_drug_interaction','duplicate_medication','contraindication','solvent_error'],
        'whole_prescription_reference_labels':0,'qualified_normal_controls':0,'source_files_unchanged':True,
        'all_source_runs_reported_completed':all(x=='completed' for x in source_status),
        'anchor_reuse_execution_preflight_performed':False,
        'interpretation':'Prepared variants are not observed experiment outcomes. Route-mismatch verification establishes only the specified introduced fault, not source normality, clinical severity, single-fault status across all categories or full six-category coverage.'}
    public_dir.mkdir(parents=True)
    (public_dir/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2)+'\n')
    refs='；'.join('<a href="'+html.escape(p['source']['url'])+'">'+html.escape(k)+'</a>' for k,p in registry()['products'].items())
    (public_dir/'index.html').write_text('<!doctype html><meta charset="utf-8"><title>受控处方变体准备</title><style>body{font:17px/1.8 system-ui;max-width:980px;margin:40px auto;padding:20px}a{color:#1565a0}</style><h1>实际原始处方的受控变体</h1><p>已保留全部 '+str(len(rows))+' 例来源，生成 '+str(len(bundles))+' 份给药途径变体；每份供同一组5种审方配置使用。原医生回答、病史、患者口述均不改；变更仅发生在接诊后的处方录入候选。</p><h2>已验证</h2><p>变更前后差异、具体产品说明书、输入哈希及原始工作预算已绑定。无法录入HIS的错误候选仍须先审方；医生真实修订后才能签署和缴费。未使用模型输出好坏筛选患者。</p><p>参考资料：'+refs+'</p><h2>尚未完成</h2><p>本步骤没有运行模型或写入医院，不能计入正式结果。其余5类错误、整张处方的独立参照与正常对照仍待完成；目前不能声称已覆盖六类错误。</p>')
    return summary


if __name__=='__main__':
    os.umask(0o077);p=argparse.ArgumentParser(description=__doc__)
    for k in ('source-review','native-catalogue','private-dir','public-dir'):p.add_argument('--'+k,required=True,type=Path)
    a=p.parse_args();print(json.dumps(prepare(a.source_review,a.native_catalogue,a.private_dir,a.public_dir),ensure_ascii=False))

"""Audit all planned route-variant conditions; export only counts, states and hashes."""
import argparse
from datetime import datetime,timezone
import hashlib
import html
import json
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from sandbox.experiments.linked.contracts import digest
from sandbox.experiments.linked.prescription_variants import make_route_variant,VariantNotApplicable
from sandbox.experiments.linked.variant_analysis import audit_route_run,summarize_route_rows


def read(path):return json.loads(Path(path).read_text())
def sha(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def report(batch,source_review,output):
    batch=Path(batch).resolve();source_review=Path(source_review);output=Path(output)
    if output.exists():raise FileExistsError('keep prior audit versions')
    if not (batch/'batch_summary.json').is_file():raise ValueError('finished batch required')
    spec=read(batch/'specification.json');source=read(source_review)
    if (spec.get('controlled_prescription_variant_policy') or {}).get('kind')!='exact-product-route':
        raise ValueError('not a declared controlled route experiment')
    protected={**source['sources_sha256'],str(source_review):sha(source_review)}
    for p in (batch/'specification.json',batch/'batch_summary.json'):protected[str(p)]=sha(p)
    def verify():
        if any(sha(p)!=v for p,v in protected.items()):raise ValueError('source changed during audit')
    verify();sources={x['case_id']:x for x in source['items']};rows=[]
    if len(sources)!=len(source['items']):raise ValueError('duplicate source case')
    for number,case in enumerate(spec['cases'],1):
        original=sources.get(case['case_id'])
        if original is None:raise ValueError('planned case absent from independently prepared source material')
        if original.get('source_case_hash')!=case['runtime_input_hash']:raise ValueError('source runtime input mismatch')
        variant=None;formulary=None
        if original['availability']=='original_prescription_available':
            inp=original['reference_material']['independent_review_input'];formulary=inp['formulary']
            try:variant=make_route_variant({k:inp[k] for k in ('prescription','clinical_context')},formulary)
            except VariantNotApplicable:pass
        index_path=batch/case['case_id']/'attempts.json'
        if index_path.exists():
            protected[str(index_path)]=sha(index_path);index=read(index_path)
        else:index={'attempts':{}}
        for condition in spec['conditions']:
            config=condition['config_id'];attempt=index['attempts'].get(config.replace(':','__'),{})
            row={'unit':f'S{number:04d}','config_id':config,'status':attempt.get('status','missing_planned_attempt'),
                'variant_applicable':variant is not None,'first_review_decision':None,
                'paid_repair_branch_observed':False}
            if attempt.get('result_path'):
                p=Path(attempt['result_path']).resolve()
                if batch not in p.parents or variant is None:raise ValueError('result has no valid scoped source')
                protected[str(p)]=sha(p);run=read(p)
                if (run.get('case_id')!=case['case_id'] or run.get('config_id')!=config
                        or run.get('input_hash')!=case['runtime_input_hash']):raise ValueError('run identity mismatch')
                row.update(audit_route_run(run,variant,formulary),run_file_sha256=protected[str(p)])
            rows.append(row)
    verify();summary=summarize_route_rows(rows)
    summary.update(observed_at=datetime.now(timezone.utc).isoformat(),batch=spec['batch'],
        source_review_sha256=sha(source_review),specification_sha256=sha(batch/'specification.json'),
        planned_source_patients=len(spec['cases']),planned_condition_cells=len(rows),
        sources_unchanged=True,rows=rows)
    output.mkdir(parents=True)
    (output/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2)+'\n')
    cells=''.join('<tr>'+''.join('<td>'+html.escape(str(value))+'</td>' for value in (
        x['config_id'],x['planned_candidates'],x['first_reviews_observed'],x['approved_known_route_fault'],
        x['paid_repair_branches_observed']))+'</tr>' for x in summary['configurations'])
    (output/'index.html').write_text('<!doctype html><meta charset="utf-8"><title>受控给药途径实验</title><style>body{font:17px/1.8 system-ui;max-width:1100px;margin:35px auto;padding:20px}td,th{padding:10px;border-bottom:1px solid #ddd}</style><h1>首次审核与修复后缴费分开统计</h1><p>这是给药途径错误的工程测试，不能代表六类错误覆盖、总体临床正确率或正式实验结果。所有计划条件均保留；未知不是零。</p><table><tr><th>配置</th><th>计划候选</th><th>观察到首次审核</th><th>首次批准已知途径错误</th><th>修复后签署缴费的原生轨迹</th></tr>'+cells+'</table><p>后续修复不覆盖首次结论；拒绝处方也不自动证明识别了指定错误。没有正常对照，因此不报告特异度。此处轨迹核验与医院独立读回、临床评审分开；任务分支不增加完整患者就诊数。</p>')
    return {k:v for k,v in summary.items() if k!='rows'}


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    for name in ('batch-dir','source-review','output-dir'):p.add_argument('--'+name,type=Path,required=True)
    a=p.parse_args();print(json.dumps(report(a.batch_dir,a.source_review,a.output_dir),ensure_ascii=False))

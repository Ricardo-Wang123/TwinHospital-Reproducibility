#!/usr/bin/env python3
"""Readable, actual-run walkthroughs for independently fictional cases only."""
import argparse
from copy import deepcopy
import hashlib
import html
import json
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from sandbox.experiments.linked.contracts import digest, runtime_case

NAMES={'type2_diabetes':'二型糖尿病','heart_failure':'心衰','copd':'慢阻肺','pneumonia':'肺炎','uti':'尿路感染'}


def read(path): return json.loads(Path(path).read_text())
def sha(path): return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def build_sections(case, run):
    if case.get('data_kind')!='synthetic_independent' or run.get('data_kind')!='synthetic_independent':
        raise ValueError('Readable public walkthroughs accept independently fictional cases only')
    if case['case_id']!=run.get('case_id') or digest(runtime_case(case))!=run.get('input_hash'):
        raise ValueError('Case does not match actual run input')
    if run.get('evaluation_unit')!='complete_encounter':
        raise ValueError('A stage branch is not a complete-encounter walkthrough')
    a=run['artifacts']
    return {
        '01_患者起始视图.json':{'scope':'仅患者Agent；不含医院完整底稿和评价答案','patient_initial':deepcopy(case['patient_initial'])},
        '02_医院起始资料.json':{'scope':'医院保存历史资料与本次已声明的测量；每条记录保留来源和时间','his_initial':deepcopy(case['his_initial']),'formulary':deepcopy(case['formulary'])},
        '03_预问诊与医生交接.json':{'scope':'实际采集和交接结果，不是预填标准答案',
            'handoff':deepcopy(a.get('final_handoff')),'physician_verified_handoff':deepcopy(a.get('physician_verified_handoff')),
            'conversation':[deepcopy(s) for s in a.get('transcript',[]) if s.get('phase')!='consultation']},
        '04_接诊开方与审方闭环.json':{'scope':'保留原方、首次审方、每次修改及最终处置；首次审方与修改后的结果分开',
            **{k:deepcopy(a.get(k)) for k in ('consultation','current_encounter_measurements','original_prescription',
                'original_prescription_review','prescription_versions','prescription_followups','final_prescription',
                'prescription_business_state','prescription_termination','clinical_disposition','final_care_plan')},
            'physician_conversation':[deepcopy(s) for s in a.get('transcript',[]) if s.get('phase')=='consultation']},
        '05_病历草稿到签署.json':{'scope':'实际模型生成的草稿、医生复核和签署结果；临床准确性尚待独立评价',
            **{k:deepcopy(a.get(k)) for k in ('note_versions','note_reviews','attested_note','disposition_documentation_outcome')}},
        '06_模拟缴费与最终状态.json':{'scope':'本地模拟现金记账；业务完成不等同临床正确',
            **{k:run.get(k) for k in ('status','evaluation_unit','workflow_complete','native_complete','error_type','failure_reason')},
            'settlement':deepcopy(a.get('settlement')),'clinical_quality':deepcopy(run.get('clinical_quality')),
            'clinical_appropriateness':'pending_independent_review'},
        '07_精简运行记录.json':{'scope':'只保留操作、角色、模型、版本、顺序及证据哈希；不导出模型思考或接口密钥',
            **{k:run.get(k) for k in ('case_id','run_id','conditions','started_at','ended_at','call_count','wall_seconds')},
            'calls':[{k:deepcopy(c.get(k)) for k in ('role','phase','model_family','model_name','observation_hash','response_hash','usage')} for c in run.get('calls',[])],
            'events':[{k:deepcopy(e.get(k)) for k in ('sequence','occurred_at','event','role','operation','status','readback_verified') if k in e} for e in run.get('events',[])],
            'activities':deepcopy(run.get('activities',[]))}}


def export(batch, manifest, output):
    batch,manifest,output=map(Path,(batch,manifest,output))
    if not (batch/'batch_summary.json').is_file(): raise ValueError('Batch must finish before export')
    if output.exists(): raise FileExistsError('Use a new output version; never replace earlier evidence')
    staged=[]
    for entry in read(manifest)['cases']:
        case=read(entry['path']);index=read(batch/case['case_id']/'attempts.json')
        item=index['attempts']['linked_baseline_anchor'];run_path=Path(item['result_path']).resolve()
        if batch.resolve() not in run_path.parents: raise ValueError('Run path outside batch')
        run=read(run_path);sections=build_sections(case,run)
        staged.append((case,run,sections,{'input_file_sha256':sha(entry['path']),
            'run_file_sha256':sha(run_path),'runtime_input_hash':run['input_hash']}))
    output.mkdir(parents=True)
    rows=[]
    for i,(case,run,sections,hashes) in enumerate(staged,1):
        label=NAMES.get(case['group'],case['group']);folder=output/(f'{i:02d}_'+label);folder.mkdir()
        for name,data in sections.items():
            (folder/name).write_text(json.dumps(data,ensure_ascii=False,indent=2,allow_nan=False)+'\n')
        (folder/'08_来源核对.json').write_text(json.dumps({'data_kind':'synthetic_independent',
            'case_id':case['case_id'],'clinical_reference_status':'pending_independent_review',**hashes},indent=2)+'\n')
        links=' '.join(f'<a href="{html.escape(name)}">{html.escape(name[:2])}</a>' for name in sections)
        blocks=''.join(f'<details><summary>{html.escape(name[:-5])}</summary><pre>{html.escape(json.dumps(value,ensure_ascii=False,indent=2))}</pre></details>' for name,value in sections.items())
        (folder/'00_看实际流程.html').write_text(page(label,f'<p>实际运行状态：{html.escape(run["status"])}；模型调用 {run["call_count"]} 次。全为独立虚构病例，不含 MIMIC 病例；临床质量未完成独立评审。</p><p>{links}</p>{blocks}'))
        rows.append(f'<tr><td><a href="{html.escape(folder.name)}/00_看实际流程.html">{html.escape(label)}</a></td><td>{html.escape(run["status"])}</td><td>{run["call_count"]}</td><td>{len(run["artifacts"].get("prescription_versions",[]))}</td></tr>')
    (output/'00_从这里开始.html').write_text(page('五类病例的实际全流程',
        '<p>每个文件夹是一名患者的一次真实程序运行。按 01—07 看：患者与医院最初知道什么，如何交接、开方、审方、签署病历，最后如何模拟缴费。结果来自已保存的运行记录，失败也保留。</p>'
        '<p><strong>这些是独立构造病例的工程验证，不能当作真实患者试验或临床质量已达标。</strong></p>'
        '<table><tr><th>病例</th><th>运行状态</th><th>调用次数</th><th>处方版本数</th></tr>'+''.join(rows)+'</table>'
        '<p>各阶段 JSON 供程序接入；HTML 供人工快速查看。首次审方用于原方比较，修改后的审核和缴费另行记录。</p>'))
    return {'cases':len(staged),'output':str(output),'clinical_scores_inferred':False}


def page(title,body):
    return '<!doctype html><meta charset="utf-8"><title>'+html.escape(title)+'</title><style>body{max-width:1100px;margin:40px auto;padding:0 24px;font:16px/1.7 system-ui;color:#19263a}table{border-collapse:collapse;width:100%}td,th{padding:10px;text-align:left;border-bottom:1px solid #ddd}details{margin:18px 0;padding:12px;background:#f3f6fa}summary{cursor:pointer;font-weight:700}pre{white-space:pre-wrap;overflow-wrap:anywhere;font-size:13px}a{color:#245fa0}</style><h1>'+html.escape(title)+'</h1>'+body


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    for name in ('batch','manifest','output'):p.add_argument('--'+name,required=True,type=Path)
    args=p.parse_args();print(json.dumps(export(args.batch,args.manifest,args.output),ensure_ascii=False))

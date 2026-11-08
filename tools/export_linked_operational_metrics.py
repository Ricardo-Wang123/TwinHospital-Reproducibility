"""Export numeric stage-workflow observations without inventing clinical scores."""
import argparse
from collections import Counter
import csv
import fcntl
import hashlib
import html
import json
import math
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from sandbox.experiments.linked.contracts import paper_conditions
from sandbox.experiments.linked.reporting import summarize_runs
from sandbox.experiments.linked.programmatic_delivery import PROTOCOL, aggregate, timing_sensitivity
from tools.export_linked_appendix import safe


def read(p):return json.loads(Path(p).read_text())
def sha(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def metrics(batch):
    batch=Path(batch).resolve()
    if not (batch/'batch_summary.json').exists():raise ValueError('Finished batch required')
    spec=read(batch/'specification.json')
    if spec['mode']!='matrix':raise ValueError('Stage matrix required; do not pool linked and fixed tasks')
    if spec['conditions']!=[c.to_dict() for c in paper_conditions(model_families=spec.get('models',{}).get('tool_models'))]:
        raise ValueError('All declared task conditions must be retained')
    before={str(batch/'specification.json'):sha(batch/'specification.json')}
    rows=[]
    with (batch/'batch.lock').open() as lock:
        try:fcntl.flock(lock,fcntl.LOCK_SH|fcntl.LOCK_NB)
        except BlockingIOError:raise ValueError('Batch is still running')
        for condition in spec['conditions']:
            label=condition['config_id'].replace(':','__');runs=[];statuses=Counter();unstarted=0
            for case in spec['cases']:
                path=batch/case['case_id']/'attempts.json';before[str(path)]=sha(path)
                entry=read(path)['attempts'].get(label)
                if not entry:raise ValueError('Missing planned condition in attempt index')
                statuses[entry['status']]+=1
                if not entry.get('result_path'):
                    if str(entry['status']).startswith('not_run_'):unstarted+=1
                    continue
                p=Path(entry['result_path']).resolve()
                if not p.is_relative_to(batch):raise ValueError('Run path escapes batch')
                before[str(p)]=sha(p);run=read(p)
                if (run['case_id']!=case['case_id'] or run['config_id']!=condition['config_id']
                    or run['status']!=entry['status'] or run['input_hash']!=case['runtime_input_hash']
                    or run['evaluation_unit']!='fixed_input_task'):
                    raise ValueError('Run identity differs from its frozen assignment')
                if not isinstance(run.get('activities'),list):raise ValueError('Missing activity evidence cannot become zero work')
                for a in run['activities']:
                    if (type(a.get('active_seconds')) not in (int,float) or not math.isfinite(a['active_seconds'])
                        or a['active_seconds']<0 or a['active_seconds']!=spec['modeled_activity_seconds'].get(a.get('activity_type'))):
                        raise ValueError('Activity duration differs from declared timing scheme')
                runs.append(run)
            result=summarize_runs(runs)
            # Closed projection: no dialogue, source IDs, private paths or free prose.
            row={**condition,'planned_cases':len(spec['cases']),'observed_runs':len(runs),
                'status_counts':dict(statuses),'activity_totals':result['activity_totals'],
                'model_calls':result['model_calls'], 'original_prescription_dispositions':
                    result['prescription_execution']['recorded_system_dispositions'],
                'recorded_original_prescriptions':result['prescription_execution']['candidates'].get('recorded_candidates',0),
                'physician_handoff_verification_questions':sum(
                    x.get('speaker_role')=='doctor' and x.get('phase')=='doctor_handoff_verification'
                    for r in runs for x in r.get('artifacts',{}).get('transcript',[])),
                'saved_note_versions':sum(len(r.get('artifacts',{}).get('note_versions',[])) for r in runs),
                'saved_physician_note_revisions':sum(v.get('reason')=='physician_revision'
                    for r in runs for v in r.get('artifacts',{}).get('note_versions',[])),
                'clinical_accuracy':None}
            row['programmatic_delivery']=aggregate(runs,condition['task'],planned=len(spec['cases']),unstarted=unstarted)
            row['timing_sensitivity']=timing_sensitivity(runs,condition['task'],planned=len(spec['cases']),unstarted=unstarted)
            row['semantic_redundancy']='not_assessed'
            rows.append(safe(row))
        if any(sha(p)!=h for p,h in before.items()):raise ValueError('Source changed during aggregation')
    result={'version':'engineering-stage-operational-observations-v2','formal_result':False,
        'source_specification_sha256':before[str(batch/'specification.json')],
        'source_batch_summary_sha256':sha(batch/'batch_summary.json'),
        'source_run_and_index_hashes':sorted(before.values()),
        'conditions':rows,'source_cases':len(spec['cases']),
        'timing_scheme':safe(spec['modeled_activity_seconds']),
        'interpretation':['All planned conditions and observed failures remain included.',
          'Activity totals are uncalibrated simulated seconds, not measured human staff time.',
          'API wall seconds are separate latency observations; costs including retries come from the appendix request ledger.',
          'Question counts do not identify redundant questions; role approvals do not establish clinical correctness.',
          'APP II Nq uses declared programmatic delivery checks.']}
    result.update(evaluation_protocol=PROTOCOL)
    result['interpretation'][-1]='APP II Nq uses the named programmatic delivery checks; clinical narrative truth remains unassessed.'
    return result


def export(batch,output):
    output=Path(output)
    if output.exists():raise FileExistsError('Use a new immutable report directory')
    result=metrics(batch);output.mkdir(parents=True)
    (output/'operational_metrics.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    flat=[]
    for row in result['conditions']:
        roles=Counter()
        for a in row['activity_totals']:roles[a['role']]+=a['modeled_active_seconds']
        flat.append({'condition':row['config_id'],'planned':row['planned_cases'],
            'completed':row['status_counts'].get('completed',0),'status_counts':json.dumps(row['status_counts']),
            'nurse_modeled_seconds':roles['nurse'] if row['observed_runs'] else None,
            'physician_modeled_seconds':roles['doctor'] if row['observed_runs'] else None,
            'observed_runs':row['observed_runs'],'role_calls':sum(x['calls'] for x in row['model_calls']),
            'physician_handoff_questions':row['physician_handoff_verification_questions'],
            'saved_note_versions':row['saved_note_versions'],'quality_qualified_Nq':None})
        delivery=row['programmatic_delivery']
        flat[-1]['quality_qualified_Nq']=delivery['qualified_tasks'] if delivery['applicable'] else 'not_applicable'
        flat[-1]['modeled_staff_seconds_per_qualified_task']=delivery['effort_per_qualified_task_seconds']
    with (output/'operational_metrics.csv').open('w',newline='') as f:
        writer=csv.DictWriter(f,fieldnames=list(flat[0]));writer.writeheader();writer.writerows(flat)
    headers=['配置','计划','有最终日志','完成','已观察护士模拟秒数','已观察医生模拟秒数','调用数','程序核验合格Nq']
    table='<table><tr>'+''.join('<th>'+h+'</th>' for h in headers)+'</tr>'
    for r in flat:
        values=[r[k] for k in ('condition','planned','observed_runs','completed','nurse_modeled_seconds','physician_modeled_seconds','role_calls','quality_qualified_Nq')]
        table+='<tr>'+''.join('<td>'+html.escape(str(v))+'</td>' for v in values)+'</tr>'
    scope='Nq 按已记录的交付、格式、版本、来源引用和业务读回核验计算；未知及零分母保持未定义。其范围不包含自由文本的全面临床正确性。'
    (output/'operational_metrics.html').write_text('<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>阶段运行与模拟工作量</title><style>body{font:16px/1.7 system-ui;max-width:1250px;margin:35px auto;padding:20px}table{border-collapse:collapse}td,th{border:1px solid #ccd;padding:9px}</style><h1>阶段运行与模拟工作量</h1><p>以下是已冻结开发病例的实际运行记录。表中秒数由预先设置的活动耗时计算，不是真人时间；包含失败尝试中实际发生的活动。'+scope+'</p>'+table+'</table><p>所有状态、各角色明细及口径见同目录JSON和CSV。角色审核通过本身不证明临床正确；提问次数也不等于重复问询。</p></html>')
    return {'conditions':len(flat),'planned_stage_attempts':sum(r['planned'] for r in flat),
        'completed_stage_attempts':sum(r['completed'] for r in flat),'evaluation_protocol':PROTOCOL,'output':str(output)}


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--batch',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
    a=p.parse_args();print(json.dumps(export(a.batch,a.output),ensure_ascii=False))

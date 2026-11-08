"""Explain an observed API outage without rewriting any experiment outcome.

Only selected metadata is read. No clinical text, credentials, response bodies,
patient identifiers or request identifiers are included in the public report.
"""
import argparse
from collections import Counter
from datetime import datetime,timezone
import hashlib
from html import escape
import json
from pathlib import Path
import re
import sqlite3
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from tools.export_linked_appendix import project,S

FROZEN='ModelArts.81006'
RUN={'status':S,'error_type':S,'failure_reason':S,
    'calls':[{'status':S,'request_id':S,'error':S,'error_type':S}]}
ERROR={'http_status':S,'provider_error_code':S,'recorded_at':S,
    'response_body':{'error':{'code':S},'code':S}}


def digest(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def probe_metadata(value):
    """Normalize actual probe formats without treating empty evidence as success."""
    entries=value.get('rows',value.get('results',[value] if 'model' in value else []))
    if not isinstance(entries,list) or not entries:
        raise ValueError('probe report contains no supported observations')
    result=[]
    for entry in entries:
        if not isinstance(entry,dict) or not isinstance(entry.get('model'),str):
            raise ValueError('probe observation is missing its model')
        at=entry.get('recorded_at') or value.get('observed_at')
        if not isinstance(at,str):raise ValueError('probe observation is missing its timestamp')
        stamp=datetime.fromisoformat(at.replace('Z','+00:00'))
        if stamp.tzinfo is None:raise ValueError('probe timestamp needs a timezone')
        status=entry.get('outcome') or entry.get('status')
        # Do not manufacture a missing HTTP status. Validated route probes carry
        # their own completion contract and returned model instead.
        healthy=(status=='completed' and entry.get('returned_model') in (None,entry['model'])
            and (entry.get('http_status')==200 or
            entry.get('http_status') is None and entry.get('returned_model')==entry['model']))
        result.append({**{k:entry.get(k) for k in ('model','http_status','provider_error_code')},
            'recorded_at':stamp.astimezone(timezone.utc).isoformat(),
            'api_protocol':entry.get('api_protocol',value.get('api_protocol')),
            'outcome':status,'healthy_observed':healthy,
            'network_route':entry.get('client') or entry.get('network_route',{}).get('mode')})
    return result


def failed_provider_code(run,evidence):
    failed=[r for r in run.get('calls',[]) if r.get('status')=='failed']
    if not failed:return None
    last=failed[-1];rid=last.get('request_id')
    if not rid:
        match=re.search(r'request_id=([a-f0-9]{32})',str(last.get('error','')))
        rid=match.group(1) if match else None
    if not isinstance(rid,str) or not re.fullmatch('[a-f0-9]{32}',rid):return None
    path=evidence/(rid+'.json')
    if not path.exists():return None
    response=project(path,ERROR);body=response.get('response_body') or {}
    code=response.get('provider_error_code') or (body.get('error') or {}).get('code') or body.get('code')
    return FROZEN if code==FROZEN and response.get('http_status') in (401,403) else None


def report(batches,evidence,ledger,probe,output):
    output=Path(output);output.mkdir(parents=True,exist_ok=False)
    evidence=Path(evidence);sources={};reports=[]
    for folder in map(Path,batches):
        end=folder/'batch_summary.json';sources[str(end)]=digest(end)
        summary=json.loads(end.read_text());counts=Counter();raw=Counter()
        frozen_stop=summary['budget'].get('halt_reason')=='provider_resource_frozen_'+FROZEN
        for p in folder.glob('*/attempts.json'):
            sources[str(p)]=digest(p);index=json.loads(p.read_text())
            for label,row in index['attempts'].items():
                status=row['status'];raw[status]+=1
                if status=='not_run_budget_limit' and frozen_stop:
                    cause='provider_dispatch_stopped_legacy_budget_label'
                elif status=='not_run_missing_frozen_input':cause='upstream_input_missing'
                elif status=='completed':cause='recorded_execution_completed'
                elif status=='incomplete_clinical_decision':cause='unfinished_clinical_work'
                elif row.get('result_path'):
                    path=Path(row['result_path']);sources[str(path)]=digest(path);run=project(path,RUN)
                    cause='provider_resource_frozen' if failed_provider_code(run,evidence)==FROZEN else 'other_execution_failure'
                else:cause='not_started_or_unknown'
                counts[cause]+=1
        reports.append({'batch':folder.name,'raw_status_counts_unchanged':dict(raw),
            'incident_cause_counts':dict(counts),'legacy_dispatch_label_explanation':
            'The old runtime called a shared dispatcher gate a budget stop. The recorded gate reason was provider-resource freezing; the user budget remained uncapped.' if frozen_stop else None})
    probes=[]
    for p in map(Path,probe):
        sources[str(p)]=digest(p);probe_data=json.loads(p.read_text())
        probes.extend(probe_metadata(probe_data))
    latest={r['model']:r for r in sorted(probes,key=lambda r:r['recorded_at'])}
    available=sorted(m for m,r in latest.items() if r['healthy_observed'])
    with sqlite3.connect('file:'+str(Path(ledger).resolve())+'?mode=ro',uri=True) as db:
        charged,reserved,n=db.execute('SELECT sum(charged),sum(reserved),count(*) FROM calls').fetchone()
        cap=db.execute('SELECT cap FROM settings').fetchone()[0]
    unchanged=all(digest(p)==h for p,h in sources.items())
    if not unchanged:raise ValueError('Source evidence changed during report')
    value={'generated_at':datetime.now(timezone.utc).isoformat(),'incident':'provider_resource_frozen',
        'provider_error_code':FROZEN,'account_arrears_confirmed':False,
        'available_models_in_latest_probes':available,
        'all_probed_models_available':bool(latest) and len(available)==len(latest),
        'availability_scope':'Each observed model only, not an account-wide availability claim',
        'probes':probes,'probe_clinical_data_sent':False,
        'budget_cap_cny':None if cap==0 else cap/1e6,'accounted_upper_cny':charged/1e6,
        'reserved_cny':reserved/1e6,'requests_in_ledger':n,'actual_provider_invoice_read':False,
        'batches':reports,'original_evidence_unchanged':unchanged,
        'interpretation':'All unpaid workflows remain in progress. These annotations explain infrastructure failures; they do not replace raw statuses or establish clinical correctness.',
        'references':['https://support.huaweicloud.com/model-call-maas/model-call-035.html',
            'https://support.huaweicloud.com/maas_faq/maas_faq_0010.html']}
    (output/'summary.json').write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n')
    rows=''.join('<tr>'+''.join('<td>'+escape(str(r.get(k,'')))+'</td>' for k in
        ('model','api_protocol','http_status','provider_error_code'))+'</tr>' for r in probes)
    html='''<!doctype html><meta charset="utf-8"><title>TwinHospital API 实测</title>
<style>body{font:16px/1.7 system-ui;max-width:1000px;margin:40px auto;padding:20px;color:#24374c}table{border-collapse:collapse;width:100%}td,th{padding:12px;border-bottom:1px solid #cbd5e1;text-align:left}</style>
<h1>TwinHospital API 实测记录</h1><p>重新建立独立连接；每个接口、每个模型各发一次固定的非病例请求。没有发送患者资料。</p>
<table><tr><th>模型</th><th>接口</th><th>HTTP</th><th>错误码</th></tr>'''+rows+'''</table>
<p>原实验型号返回资源冻结，不能据此确认欠费，也不能认为整个账号不可用。未读取华为账号账单。</p>
<p>后续探测成功的可用型号：'''+escape('、'.join(available) or '尚无')+'''。每个型号的结果单独保留。</p>
<p>本地累计保守费用估算：'''+f'{charged/1e6:.6f}'+''' 元；API 预算上限已取消。这个数不是实际账单。</p>
<p>未缴费病例仍为进行中；原始运行结果未修改，接口错误与临床结果分开记录。</p>
<p>参考资料：<a href="https://support.huaweicloud.com/model-call-maas/model-call-035.html">华为官方错误码</a>；<a href="https://support.huaweicloud.com/maas_faq/maas_faq_0010.html">冻结状态说明</a>。</p>'''
    (output/'index.html').write_text(html)
    return value


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--batch',action='append',required=True);p.add_argument('--evidence',required=True)
    p.add_argument('--ledger',required=True);p.add_argument('--probe',action='append',required=True);p.add_argument('--output',required=True)
    a=p.parse_args();r=report(a.batch,a.evidence,a.ledger,a.probe,a.output)
    print(json.dumps({'public_report':a.output,'accounted_upper_cny':r['accounted_upper_cny'],
        'available_models':r['available_models_in_latest_probes'],'original_evidence_unchanged':r['original_evidence_unchanged']}))

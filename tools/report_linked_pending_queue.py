"""Read-only work queue: unfinished encounters remain pending until paid.

This is an operational status view, not a replacement for immutable trial
outcomes or an independent clinical/safety score. It never retries model calls.
"""
from collections import Counter
from datetime import datetime, timezone
import argparse
import hashlib
import html
import json
from pathlib import Path


def observed(run, operation):
    def identity(e):
        if any(e.get(k) != run.get(k) for k in ('run_id', 'case_id')):
            return False
        binding=e.get('response', {}).get('data', {}) if operation=='registration.create' else e
        return all(run.get(k) is not None and binding.get(k)==run[k] for k in ('patient_id','encounter_id'))
    return any(e.get('event') == 'business_operation' and e.get('operation') == operation
        and identity(e)
        and e.get('response', {}).get('status') == 'executed'
        and e.get('response', {}).get('readback_verified') is True for e in run.get('events', []))


def paid_complete(run):
    a = run.get('artifacts', {})
    return (run.get('evaluation_unit') == 'complete_encounter'
        and run.get('workflow_complete') is True and run.get('native_complete') is True
        and a.get('prescription_business_state', {}).get('status') == 'signed'
        and a.get('attested_note', {}).get('status') == 'locked'
        and a.get('settlement', {}).get('payment', {}).get('status') == 'paid'
        and all(observed(run, op) for op in ('registration.create', 'handoff.save',
            'handoff.read', 'encounter.begin_consultation', 'order.attest', 'note.attest', 'payment.commit')))


def report(batch, output):
    batch, output = Path(batch), Path(output)
    spec = json.loads((batch / 'specification.json').read_text())
    rows = []
    source_hashes = {}
    for n, case in enumerate(spec['cases'], 1):
        root = batch / case['case_id']
        index = root / 'attempts.json'
        attempts = json.loads(index.read_text()).get('attempts', {}) if index.exists() else {}
        record = attempts.get('linked_baseline_anchor', {})
        run_path = record.get('result_path')
        run = json.loads(Path(run_path).read_text()) if run_path else {}
        if run and (run.get('case_id') != case['case_id'] or run.get('evaluation_unit') != 'complete_encounter'):
            raise ValueError('Anchor identity or evaluation unit mismatch')
        if run_path:
            source_hashes[f'C{n:03d}'] = hashlib.sha256(Path(run_path).read_bytes()).hexdigest()
        complete = paid_complete(run)
        rows.append({'case_label':f'C{n:03d}', 'group':case['group'],
            'workflow_state':'complete_paid' if complete else 'in_progress',
            'latest_attempt_status':run.get('status', 'running' if root.exists() else 'not_started'),
            'needs_followup':bool(run) and not complete,
            'last_error_type':run.get('error_type'),
            'stage_attempts_recorded':sum(k != 'linked_baseline_anchor' for k in attempts),
            'stage_attempt_status_counts':dict(Counter(v.get('status', 'unknown')
                for k,v in attempts.items() if k != 'linked_baseline_anchor'))})
    result = {'batch':spec['batch'], 'updated_at':datetime.now(timezone.utc).isoformat(),
        'planned_full_encounters':len(rows),
        'workflow_counts':dict(Counter(r['workflow_state'] for r in rows)),
        'completed_attempts_needing_followup':sum(r['needs_followup'] for r in rows),
        'status_scope':'Operational work queue; no failed trial deleted, no automated retry or inferred clinical/safety qualification.',
        'success_requires':'Entire required chain plus bound signed prescription, locked note and verified medication payment.',
        'original_attempt_source_hashes':source_hashes, 'rows':rows}
    output.mkdir(parents=True, exist_ok=True)
    tmp=output/'queue.json.tmp'; tmp.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n');tmp.replace(output/'queue.json')
    table=''.join('<tr>'+''.join('<td>'+html.escape(str(r[k]))+'</td>' for k in
        ('case_label','group','workflow_state','latest_attempt_status','stage_attempts_recorded'))+'</tr>' for r in rows)
    page=f'''<!doctype html><meta charset="utf-8"><title>TwinHospital 待完成队列</title>
<style>body{{font:16px/1.8 system-ui;max-width:1100px;margin:30px auto}}td,th{{padding:8px;border-bottom:1px solid #ccc;text-align:left}}</style>
<h1>完整流程待完成队列</h1><p>更新时间：{result['updated_at']}。</p>
<p>完整缴费：{result['workflow_counts'].get('complete_paid',0)} / {len(rows)}。
其余继续作为进行中；已经结束的中间尝试仍保留原状态，不能替换或删除。此页不代表临床评审通过。</p>
<table><tr><th>病例</th><th>病种</th><th>整链状态</th><th>最近一次尝试</th><th>已记录阶段分支</th></tr>{table}</table>'''
    tmp=output/'index.html.tmp';tmp.write_text(page);tmp.replace(output/'index.html')
    return {k:v for k,v in result.items() if k not in {'rows','original_attempt_source_hashes'}}


if __name__ == '__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--batch',required=True,type=Path);p.add_argument('--output',required=True,type=Path)
    a=p.parse_args();print(json.dumps(report(a.batch,a.output),ensure_ascii=False))

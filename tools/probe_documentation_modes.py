"""Real CAMEL/API drafting probe on one independently authored text consultation.

The in-memory business port is deliberately NOT OpenHIS. This tests both note
input schedules, model routing, drafts and role review; it cannot count as a
completed hospital encounter, clinical accuracy evaluation, or an audio test.
"""
from copy import deepcopy
from datetime import datetime, timezone
import argparse
import hashlib
import json
import os
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from sandbox.experiments.linked.camel_provider import BudgetedCamelFactory
from sandbox.experiments.linked.chart import HistoryChart
from sandbox.experiments.linked.clinical import LinkedEncounterRunner
from sandbox.experiments.linked.contracts import TaskCondition, digest
from sandbox.experiments.linked.provider import BudgetLedger, private_json
from sandbox.experiments.linked.provider_profile import validate


class ProbePort:
    """Only local persistence; every response explicitly denies native effects."""
    def __init__(self):
        self.history={}; self.notes={}; self.calls=[]
    def execute(self,operation,payload,context):
        self.calls.append(operation); data={}
        if operation=='registration.create':
            data={'patient_id':'probe-patient','encounter_id':context['run_id']}
        elif operation in {'queue.enter','encounter.begin_consultation'}:
            data={'encounter_id':context['encounter_id']}
        elif operation=='history.import':self.history=deepcopy(payload['history'])
        elif operation=='history.index':data={'history':HistoryChart(self.history).initial_view()}
        elif operation=='diagnosis.catalogue':data={'items':[]}
        elif operation=='personnel.registry':data={'signatory_id':'probe-doctor','registry_version':'fictional-probe-v1'}
        elif operation in {'note.save','note.revise'}:
            note=self.notes.get(payload['note_id'],{'version':0})
            if note['version']!=payload['expected_version']:raise ValueError('Probe note version conflict')
            data={'version':note['version']+1,'content':deepcopy(payload['content'])}
            self.notes[payload['note_id']]=deepcopy(data)
        elif operation=='note.attest':
            note=self.notes[payload['note_id']]
            if note['version']!=payload['version']:raise ValueError('Probe attestation version conflict')
            data={'status':'locked','version':note['version']}
        else:raise ValueError('Unimplemented probe operation: '+operation)
        return {'status':'executed','data':data,'readback_verified':True,'native_effect':False,
                'protection_source':'explicit_in_memory_documentation_probe'}


class UnusedRules:
    def review(self,*args):raise AssertionError('Documentation probe must not invoke prescription rules')


def fixture():
    case={'case_id':'independent_note_timing_fixture_v1','patient_key':'fictional_note_patient_v1',
          'group':'independent_capability_probe','data_kind':'synthetic_independent','split':'engineering',
          'patient_initial':{'demographics':{'age':54,'sex':'F'},'patient_facts':[]},
          'his_initial':{},'formulary':[]}
    lines=[
        ('doctor','这次来复诊主要有什么不舒服？'),
        ('patient','我想复查血压，最近有点头痛，开始我记得是三天前。'),
        ('doctor','之前有什么病，现在吃什么药？'),
        ('patient','高血压三年了，每天吃一片5毫克氨氯地平。'),
        ('doctor','有没有药物过敏？'),
        ('patient','现在一下想不起来，我要看一下家里的记录。'),
        ('doctor','请核对一下头痛开始日期和过敏记录。'),
        ('patient','我看了日历，刚才说错了，头痛是五天前开始的，不是三天。'),
        ('doctor','过敏记录找到了吗？'),
        ('patient','找到了，我小时候吃青霉素出过皮疹。'),
        ('doctor','还有其他药物或者胸痛、呼吸困难、肢体无力吗？'),
        ('patient','没有吃其他药，没有胸痛、呼吸困难或者肢体无力。'),
        ('doctor','今天测得血压145/90毫米汞柱，继续当前氨氯地平方案，记录居家血压，一周后复诊；若出现突发剧烈头痛或神经症状及时就诊。'),
        ('patient','好的，我会记录血压并按时复诊。')]
    transcript=[{'sequence':i,'speaker_role':role,'phase':'consultation','text':text}
                for i,(role,text) in enumerate(lines,1)]
    rx={'items':[{'drug_code':'FICTIONAL_AMLODIPINE_5MG','display':'氨氯地平',
                 'dose':'5','dose_unit':'mg','route':'oral','frequency':'QD',
                 'quantity':'7','quantity_unit':'tablet'}]}
    source={'transcript':transcript,'handoff':{'text':'54岁女性来门诊复查血压。'},
        'consultation':{'assessment':'高血压随访；患者自述头痛五天，曾先误报三天后更正；自述青霉素皮疹史。',
                        'plan':'维持当前氨氯地平方案，记录居家血压，一周后复诊；已告知警示症状。'},
        'approved_prescription':rx,
        'observed_business_state':{'prescription':{'status':'signed','source':'independently_authored_probe_fixture'}}}
    return case,source


def audit(run,workflow,expected_model):
    calls=run['calls']; drafts=[c for c in calls if c['observation']['action']=='document']
    review=[c for c in calls if c['observation']['action']=='review_note']
    expected=[6,12,14] if workflow=='realtime' else [14]
    checks={'finished_task':run['status']=='completed',
        'expected_input_schedule':[len(c['observation']['transcript']) for c in drafts]==expected,
        'fixed_glm_model':all(c['model_name']==expected_model for c in drafts),
        'final_context_only_at_end':all(bool(c['observation'].get('consultation'))==c['observation']['is_final_prefix'] for c in drafts),
        'previous_draft_carried':all((c['observation']['previous_draft'] is not None)==(i>0) for i,c in enumerate(drafts)),
        'doctor_review_present':bool(review),
        'locked_note_in_probe_port':run['artifacts'].get('attested_note',{}).get('status')=='locked',
        'no_native_completion_claim':not run.get('native_complete') and not run.get('workflow_complete'),
        'all_draft_citations_in_visible_prefix':all(
            isinstance(c.get('response',{}).get('content',{}).get('source_sequences'),list)
            and set(c['response']['content']['source_sequences'])<=set(c['observation']['allowed_source_scope']['source_sequences'])
            for c in drafts)}
    return {'workflow':workflow,'checks':checks,'all_checks_pass':all(checks.values()),
        'draft_input_lengths':[len(c['observation']['transcript']) for c in drafts],
        'draft_calls':len(drafts),'physician_review_calls':len(review),
        'saved_versions':len(run['artifacts'].get('note_versions',[])),
        'model_names':sorted({c['model_name'] for c in calls}),
        'api_call_count':len(calls),'api_wall_seconds':sum(c['wall_seconds'] for c in calls),
        'task_status':run['status'],'failure_reason':run.get('failure_reason'),
        'run_id':run['run_id'],'input_hash':run['stage_input_hash']}


def main():
    os.umask(0o077)
    p=argparse.ArgumentParser(description=__doc__)
    for name in ('config','ledger','output'):p.add_argument('--'+name,type=Path,required=True)
    args=p.parse_args();cfg=json.loads(args.config.read_text());validate(cfg)
    if set(cfg['tool_models'])!={'tool_a'}:raise ValueError('Probe requires the revised single-tool profile')
    args.output.mkdir(parents=True,exist_ok=False,mode=0o700)
    ledger=BudgetLedger(args.ledger,cap_cny=None)
    case,source=fixture();private_json(args.output/'independent_fixture.json',{'case':case,'stage_input':source})
    audits=[]
    for workflow in ('realtime','postvisit'):
        ident=args.output.name+'_'+workflow
        factory=BudgetedCamelFactory(cfg,ledger,args.output/'model_calls',case_id=case['case_id'],
            data_kind=case['data_kind'],run_id=ident)
        runner=LinkedEncounterRunner(factory,ProbePort(),UnusedRules(),output_dir=args.output/'runs')
        result=runner.run_task(case,TaskCondition('documentation',workflow,'tool_a'),run_id=ident,stage_input=source)
        row=audit(result,workflow,cfg['tool_models']['tool_a']);audits.append(row)
        print(json.dumps(row,ensure_ascii=False),flush=True)
    summary={'observed_at':datetime.now(timezone.utc).isoformat(),
        'scope':'real CAMEL/model calls; independent fictional text; in-memory business persistence',
        'not_tested':['audio or speech recognition','native OpenHIS persistence','complete hospital encounter','clinical correctness'],
        'source_hash':digest(source),'same_completed_source_for_both_modes':len({x['input_hash'] for x in audits})==1,
        'config_sha256':hashlib.sha256(args.config.read_bytes()).hexdigest(),
        'driver_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        'rows':audits,'all_checks_pass':all(x['all_checks_pass'] for x in audits)}
    private_json(args.output/'summary.json',summary)
    return 0 if summary['all_checks_pass'] else 1


if __name__=='__main__':sys.exit(main())

"""Describe source availability, never certify clinical eligibility or normality."""
from collections import Counter,defaultdict
from decimal import Decimal,InvalidOperation
from pathlib import Path
import argparse
import hashlib
import html
import json
import os

LABS={'creatinine':('Creatinine','Blood'), 'potassium':('Potassium','Blood'),
      'glucose':('Glucose','Blood'), 'hba1c':('% Hemoglobin A1c','Blood')}
PARENTS={'type2_diabetes':'endocrine_metabolic','heart_failure':'circulatory',
         'copd':'respiratory','pneumonia':'respiratory','uti':'genitourinary'}
def sha(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()
def read(p):return json.loads(Path(p).read_text())
def write(p,x):p.write_text(json.dumps(x,ensure_ascii=False,indent=2)+'\n');p.chmod(0o600)
def numeric(v):
    if v is None or isinstance(v,bool):return False
    try:return Decimal(str(v)).is_finite()
    except InvalidOperation:return False

def main():
    os.umask(0o077);p=argparse.ArgumentParser(description=__doc__)
    for k in ('manifest','qualification','private-output','public-output'):p.add_argument('--'+k,type=Path,required=True)
    a=p.parse_args();m=read(a.manifest);q=read(a.qualification)
    qualifications={r['case_id']:r for r in q['cases']}
    if len(m['cases'])!=50 or set(qualifications)!={r['case_id'] for r in m['cases']}:
        raise ValueError('Expected the same fixed50 sources, not outcome-selected cases')
    if a.private_output.exists() or a.public_output.exists():raise FileExistsError('Use new audit folders')
    rows=[];stats=defaultdict(Counter);protected={str(a.manifest):sha(a.manifest),str(a.qualification):sha(a.qualification)}
    for e in m['cases']:
        if sha(e['path'])!=e['sha256']:raise ValueError('Frozen source changed')
        protected[e['path']]=e['sha256'];case=read(e['path']);h=case['his_initial'];group=case['group']
        row={'case_id':case['case_id'],'group':group,'system':PARENTS[group],
             'source_core_pattern_gate':qualifications[case['case_id']]['source_material_gate'],
             'source_allergy_status':h.get('allergy_record',{}).get('status'),
             'prior_admission_present':len(h['historical_encounters'])>1,
             'numeric_history':{},'clinical_sufficiency':'pending_independent_review',
             'interpretation':'Presence is not adequacy at the new visit; absence is not a clinical negative.'}
        for key,(name,fluid) in LABS.items():
            eligible=[o for o in h['historical_observations'] if o.get('display')==name and o.get('fluid')==fluid
                and numeric(o.get('numeric_value_raw')) and o.get('time',{}).get('available_by_source_cutoff') is True]
            eligible.sort(key=lambda o:o.get('time',{}).get('event_offset_hours') if type(o.get('time',{}).get('event_offset_hours')) in (int,float) else -1e30,reverse=True)
            latest=eligible[0] if eligible else None
            row['numeric_history'][key]={'available':bool(latest),
                'latest_record_ref':latest.get('record_id') if latest else None,
                'latest_event_offset_hours':latest.get('time',{}).get('event_offset_hours') if latest else None,
                'unit':latest.get('unit_raw') if latest else None,
                'latest_text_is_placeholder':bool(latest and latest.get('value_raw')=='___')}
            stats[group][key+'_numeric_available']+=int(bool(latest))
            stats[group][key+'_numeric_despite_text_placeholder']+=int(row['numeric_history'][key]['latest_text_is_placeholder'])
        stats[group].update(cases=1,core_source_pattern=int(row['source_core_pattern_gate']),
            prior_admission=int(row['prior_admission_present']),source_allergy_unknown=int(row['source_allergy_status'] in ('unknown','not_supplied')))
        rows.append(row)
    if any(sha(p)!=h for p,h in protected.items()):raise ValueError('Input changed during read')
    summary={'version':'pilot50-input-availability-v15','case_count':50,'model_outputs_read':False,'model_calls':0,
        'all_source_hashes_unchanged':True,'classification':PARENTS,'by_group':dict(stats),
        'clinical_approved_cases':None,'clinical_review':'pending; this audit assigns no eligibility or normal-control label',
        'meaning':{'core_source_pattern':'Previously frozen family/window/frequency/form source gate, not an indication or prescribing decision',
            'numeric_available':'At least one original numeric value with known source-cutoff availability, same exact analyte and specimen. No freshness threshold or unit conversion is used.',
            'text_placeholder':'Latest available numeric row has value_raw equal to ___; its independent numeric_value_raw is still present.',
            'classification':'ICD-based disease stratum and system are not changed when execution fails.'},
        'references':['https://mimic.mit.edu/docs/iv/modules/hosp/labevents/','https://mimic.mit.edu/docs/iv/modules/hosp/diagnoses_icd/',
                      'https://www.nice.org.uk/guidance/ng5/chapter/Recommendations']}
    a.private_output.mkdir(parents=True,mode=0o700);a.public_output.mkdir(parents=True,mode=0o700)
    write(a.private_output/'case_availability.json',{'cases':rows,'protected_inputs':protected})
    write(a.public_output/'summary.json',summary)
    names={'type2_diabetes':'二型糖尿病','heart_failure':'心衰','copd':'慢阻肺','pneumonia':'肺炎','uti':'尿路感染'}
    table=''.join('<tr><td>'+names[g]+'</td>'+''.join('<td>'+str(stats[g][k])+'</td>' for k in ['cases','core_source_pattern','prior_admission','creatinine_numeric_available','potassium_numeric_available','glucose_numeric_available','hba1c_numeric_available'])+'</tr>' for g in PARENTS)
    doc='''<!doctype html><html lang="zh"><meta charset="utf-8"><title>50例源资料完整性检查</title><style>body{font:16px/1.7 system-ui;margin:40px auto;max-width:1100px;color:#243044}table{border-collapse:collapse;width:100%}td,th{border:1px solid #ccd3dd;padding:10px;text-align:left}p{max-width:950px}.note{background:#eef4ff;padding:16px}</style><h1>50例源资料：有什么、还缺什么</h1><p class="note">这是数据可用性统计，不是临床合格率。仍保留四个系统、五类疾病，每类10例；运行失败不会改变疾病分类。</p><table><tr><th>疾病</th><th>病例数</th><th>核心药物来源格式可解析</th><th>有更早住院</th><th>有肌酐数值</th><th>有血钾数值</th><th>有血糖数值</th><th>有HbA1c数值</th></tr>'''+table+'''</table><p>化验统计要求原记录的采样类型匹配，数值列存在，且记录在原数据截点前可用。不把它们改成当前化验，也不据此断言足以开药。药物来源格式可解析不代表当前正在用、适应证正确或剂量适宜。</p><h2>接入时必须保留的区别</h2><p>文字值显示为 ___ 时，另一个数值字段可能仍有结果。应同时保留文字值、数值、单位、时间和来源；尚未读取不能记作没有检查。新门诊来由、患者当前体验、带来的药单与当前测量须分别标明构造来源。</p><h2>仍待完成</h2><p>逐例确认新门诊信息是否充分；感染类另外制定当前症状、诊疗资料和疗程规则；临床适宜性、正常对照和质量标签待独立评价。本表不筛掉已发生的不良结果，也不把模型通过的患者作为代表性样本。</p><p>参考资料：<a href="https://mimic.mit.edu/docs/iv/modules/hosp/labevents/">MIMIC 检验数据说明</a>；<a href="https://mimic.mit.edu/docs/iv/modules/hosp/diagnoses_icd/">MIMIC 诊断编码</a>；<a href="https://www.nice.org.uk/guidance/ng5/chapter/Recommendations">NICE 用药信息核对</a>。</p></html>'''
    (a.public_output/'report.html').write_text(doc)
    print(json.dumps(summary,ensure_ascii=False))

if __name__=='__main__':main()

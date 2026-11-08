"""Expose time-eligible supplemental source history in a new case version.

Only hospital history changes. Patient memory, current scenario, original
historical records, group and patient partition remain byte-equivalent values.
"""
from collections import Counter,defaultdict
from copy import deepcopy
from datetime import datetime,time
import argparse,json,os,sqlite3
from pathlib import Path

from .case_contract import _opaque
from .formulary_overlay import file_hash,write
from .supplemental_history import parsed
from ..cases import validate_case
from ..chart import HistoryChart
from ..chart_projection import project_history_page,verify_projection
from ..contracts import digest

VERSION='hospital_supplemental_source_overlay_v1'
SECTIONS=('historical_administrations','historical_microbiology','historical_outpatient_measurements')


def hours(value,cutoff):return round((value-cutoff).total_seconds()/3600,6)


def bounds(row,exact,date,cutoff):
    value=parsed(row.get(exact)) if exact else None
    if value is not None:return {'offset_hours':hours(value,cutoff),'precision':'datetime',
        'lower_hours':hours(value,cutoff),'upper_hours':hours(value,cutoff)}
    day=parsed(row.get(date)) if date else None
    if day is None:return {'offset_hours':None,'precision':'unknown','lower_hours':None,'upper_hours':None}
    return {'offset_hours':None,'precision':'date','lower_hours':hours(datetime.combine(day.date(),time.min),cutoff),
        'upper_hours':hours(datetime.combine(day.date(),time.max),cutoff)}


def timing(table,row,cutoff):
    event=bounds(row,None,'chartdate',cutoff) if table=='omr' else bounds(row,'charttime','chartdate' if table=='microbiologyevents' else None,cutoff)
    recorded=event if table=='omr' else bounds(row,'storetime','storedate' if table=='microbiologyevents' else None,cutoff)
    return {'basis':'source_relative_to_anchor_discharge',
        'event_offset_hours':event['offset_hours'],'recorded_offset_hours':recorded['offset_hours'],
        'event_offset_lower_hours':event['lower_hours'],'event_offset_upper_hours':event['upper_hours'],
        'recorded_offset_lower_hours':recorded['lower_hours'],'recorded_offset_upper_hours':recorded['upper_hours'],
        'event_precision':event['precision'],'recorded_precision':recorded['precision'],
        'event_time_known':event['precision']=='datetime','available_by_source_cutoff':True,
        'availability_basis':'source_chartdate_only_conservative_day_bound_no_storetime' if table=='omr'
            else 'source_event_and_recorded_result_times_or_conservative_date_bounds_before_discharge'}


def normalized(table,number,row,cutoff,secret,known_encounters,components=()):
    sid=row['subject_id'];eref=_opaque(secret,'enc',[sid,row['hadm_id']]) if row.get('hadm_id') else None
    if eref not in known_encounters:eref=None
    common={'record_id':_opaque(secret,'rx' if table=='emar' else 'obs',[table,number]),
        'source_ref':_opaque(secret,'src',[table,number,row]),'source_encounter_ref':eref,
        'source_kind':'mimic_structured','status':'historical_source_record_not_current_observation',
        'time':timing(table,row,cutoff)}
    if table=='emar':
        keys=('administration_type','complete_dose_not_given','dose_due','dose_due_unit','dose_given','dose_given_unit',
            'product_amount_given','product_unit','product_description','route','infusion_rate','infusion_rate_unit','infusion_complete')
        return {**common,'medication':row.get('medication'),'event_status_raw':row.get('event_txt'),
            'administration_is_not_current_use':True,
            'components':[{'component_ordinal':d.get('parent_field_ordinal'),**{k:d.get(k) for k in keys}} for d in components]}
    if table=='microbiologyevents':
        names={'spec_type_desc':'specimen_type','test_name':'test_name','org_name':'organism','isolate_num':'isolate_number',
            'quantity':'quantity','ab_name':'antibiotic','dilution_text':'dilution_text',
            'dilution_comparison':'dilution_comparison','dilution_value':'dilution_value','interpretation':'interpretation'}
        return {**common,'specimen_ref':_opaque(secret,'obs',['micro_specimen',sid,row.get('micro_specimen_id')]),
            **{new:row.get(old) for old,new in names.items()}}
    if table=='omr':return {**common,'display':row['result_name'],'value_raw':row['result_value'],
        'unit_raw':None,'coding':{'system':'MIMIC-IV OMR result_name','version':'2.2','code':row['result_name']}}
    raise ValueError('Unknown supplemental record type')


def adapt(case,target,db,secret):
    if case['patient_key']!=_opaque(secret,'pt',target['subject_id']):raise ValueError('Patient key does not match source')
    if case['split']!=target['split'] or case['group']!=target['group']:raise ValueError('Group/partition changed')
    cutoff=parsed(target['cutoff']);result=deepcopy(case)
    if any(k in result['his_initial'] for k in SECTIONS):raise ValueError('Do not overwrite an earlier supplement')
    records=list(db.execute('SELECT table_name,csv_row,payload FROM records WHERE subject_id=? AND eligible_through<=? ORDER BY table_name,csv_row',
        (target['subject_id'],target['cutoff'])))
    details=defaultdict(list)
    for table,number,raw in records:
        if table=='emar_detail':
            row=json.loads(raw);details[row['emar_id']].append(row)
    sections={k:[] for k in SECTIONS};known={e['encounter_ref'] for e in case['his_initial']['historical_encounters']}
    names={'emar':SECTIONS[0],'microbiologyevents':SECTIONS[1],'omr':SECTIONS[2]}
    for table,number,raw in records:
        if table=='emar_detail':continue
        row=json.loads(raw)
        sections[names[table]].append(normalized(table,number,row,cutoff,secret,known,details.get(row.get('emar_id'),())))
    result['his_initial'].update(sections);result['case_id']+='_source9'
    result['provenance']['supplemental_hospital_history']={'version':VERSION,'parent_case_id':case['case_id'],
        'parent_case_hash':digest(case),'source_kind':'mimic_structured','sections':{k:len(v) for k,v in sections.items()},
        'cutoff':'same original anchor discharge; every record was filtered for this anchor',
        'missing_rows_do_not_establish_absent_disease_or_no_administration':True,
        'original_patient_memory_unchanged':True,'current_measurements_unchanged':True,
        'current_medication_plan_unchanged':True,'clinical_suitability':'not_inferred'}
    for key in ('patient_initial','patient_key','group','split','data_kind','formulary','evaluation'):
        if result[key]!=case[key]:raise ValueError('Protected original field changed')
    for key in case['his_initial']:
        if result['his_initial'][key]!=case['his_initial'][key]:raise ValueError('Existing hospital history changed')
    validate_case(result);chart=HistoryChart(result['his_initial']);pages=0
    for section in SECTIONS:
        for offset in range(0,len(sections[section]),5):
            page=chart.query(section,offset=offset,limit=5)
            projected=project_history_page(page)
            if not verify_projection(page,projected):raise ValueError('Supplemental page lost clinical fields')
            pages+=1
    return result,{'source_rows':{k:len(v) for k,v in sections.items()},'roundtrip_pages':pages,
        'patient_memory_changed':False,'existing_clinical_fields_changed':False,'current_observations_created':0}


def prepare(manifest,cache,secret_path,output):
    os.umask(0o077)
    manifest,cache,secret_path,output=map(Path,(manifest,cache,secret_path,output))
    if output.exists():raise FileExistsError('Keep prior case input versions')
    summary=json.loads((cache/'summary.json').read_text())
    if summary['status']!='source_cache_complete_runtime_integration_pending':raise ValueError('Cache is unfinished')
    if file_hash(cache/'source_rows.sqlite')!=summary['cache_sha256']:raise ValueError('Cache changed')
    secret=secret_path.read_bytes();targets=json.loads((cache/'target_anchors_private.json').read_text())['targets']
    lookup={_opaque(secret,'case',[t['subject_id'],t['hadm_id'],t['group']]):t for t in targets}
    records=json.loads(manifest.read_text())['cases'];output.mkdir(parents=True,mode=0o700)
    files={str(manifest.resolve()):file_hash(manifest),str(cache/'summary.json'):file_hash(cache/'summary.json')}
    write(output/'policy_before_overlay.json',{'version':VERSION,'selection':'all supplied cases, no model outputs read',
        'manifest_sha256':files[str(manifest.resolve())],'cache_sha256':summary['cache_sha256'],
        'reference_urls':summary['policy']['references']})
    new=[];audits=[];total=Counter()
    with sqlite3.connect('file:'+str(cache/'source_rows.sqlite')+'?mode=ro',uri=True) as db:
        for n,record in enumerate(records,1):
            p=Path(record['path']);files[str(p.resolve())]=file_hash(p);case=json.loads(p.read_text())
            source_id=case['case_id'][:29]  # case_ + 24 HMAC hex chars; suffixes version construction.
            target=lookup.get(source_id)
            if target is None:raise ValueError('Source anchor not found for case')
            result,audit=adapt(case,target,db,secret)
            dest=output/'cases'/(result['case_id']+'.json');write(dest,result)
            new.append({**{k:result[k] for k in ('case_id','patient_key','group','split','data_kind')},
                'path':str(dest.resolve()),'file_sha256':file_hash(dest),'source_case_id':case['case_id']})
            audits.append({'case_id':result['case_id'],**audit});total.update(audit['source_rows'])
            print(json.dumps({'prepared':n,'target':len(records),'group':case['group'],'counts':audit['source_rows']}),flush=True)
    if any(file_hash(p)!=h for p,h in files.items()):raise ValueError('Source changed during overlay')
    write(output/'manifest.json',{'version':VERSION,'case_count':len(new),'cases':new,'formal_eligible':False,
        'source_manifest':str(manifest.resolve()),'model_outcomes_read':False,'new_source_patients':0})
    write(output/'audit_private.json',{'cases':audits})
    write(output/'input_freeze.json',{'source_files':files,'files':[{'path':r['path'],'sha256':r['file_sha256']} for r in new]})
    result={'version':VERSION,'cases':len(new),'added_history_rows':dict(total),
        'roundtrip_pages':sum(a['roundtrip_pages'] for a in audits),'new_current_clinical_values':0,
        'original_patient_memory_unchanged':True,'clinical_sufficiency':'not_inferred','runtime_execution':'not_started'}
    write(output/'summary.json',result);return result


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    for name in ('manifest','cache','secret-path','output'):p.add_argument('--'+name,type=Path,required=True)
    a=p.parse_args();print(json.dumps(prepare(a.manifest,a.cache,a.secret_path,a.output),ensure_ascii=False))

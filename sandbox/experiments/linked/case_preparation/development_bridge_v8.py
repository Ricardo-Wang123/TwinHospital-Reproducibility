"""Apply the tested generic chronic-episode adapter to every frozen pilot source.

Infection followups keep their existing authored episode: this adapter does not
invent a new infection, extend antibiotics, or fill missing clinical evidence.
All fifty source patients are retained, irrespective of model outcomes.
"""
import argparse
from collections import Counter
from copy import deepcopy
import json
import os
from pathlib import Path

from .current_episode_v4 import build_episode, SYMPTOMS, HISTORY_FIELDS
from .v4_source_qualification import load_frequency_context
from .adult_population import add_adult_population
from .formulary_overlay import file_hash, write
from ..cases import validate_case
from ..contracts import digest, runtime_case

VERSION='pilot50_generic_case_bridge_v8'
POLICY={
    'version':VERSION, 'purpose':'development_qualification_not_formal_validation',
    'selection':'Retain all original fifty source patients in original order, ten per disease stratum; no model results are read or used.',
    'chronic_episode':'Existing v4.2.2 six-day constructed medication-use episode with pharmacy-linked source frequencies, current-use/supply accounting and all original historical evidence.',
    'infection_episode':'Preserve existing v3 next-day post-discharge reassessment, symptoms and reported exposure. No new infection, indication, cure, antimicrobial duration or normal laboratory result is invented.',
    'same_format':'Patient memory, hospital history/current observations, formulary, evaluation-only reference, IDs and provenance have the same interface across all five groups.',
    'eligibility':'Structural validity is checked. Need for dispensing and clinical suitability remain decisions and independent-review questions; a no-prescription outcome is retained, not replaced by a successful patient.',
    'adult_population':'MIMIC-IV2.2 source cohort metadata only; no exact age inferred for censored ages.',
    'measurements':'Historical values and times unchanged; no new lab or imaging value authored.',
    'new_source_patients':0, 'formal_eligible':False, 'model_calls':0,
}


def prepare(manifest_path, private_data_root, output):
    manifest_path,private_data_root,output=map(Path,(manifest_path,private_data_root,output))
    if output.exists():raise FileExistsError('Preserve previous input versions')
    manifest=json.loads(manifest_path.read_text());rows=manifest['cases']
    expected={'type2_diabetes':10,'heart_failure':10,'copd':10,'pneumonia':10,'uti':10}
    if dict(Counter(r['group'] for r in rows))!=expected or len({r['patient_key'] for r in rows})!=50:
        raise ValueError('Requires the original patient-disjoint five-by-ten development manifest')
    if any(r['split']!='development' for r in rows):raise ValueError('No held-out source may enter development')
    output.mkdir(parents=True,mode=0o700)
    sources={str(manifest_path.resolve()):file_hash(manifest_path)}
    code_names=['development_bridge_v8.py','current_episode_v4.py','v4_source_qualification.py','adult_population.py','formulary_overlay.py']
    codes={str(Path(__file__).with_name(name)):file_hash(Path(__file__).with_name(name)) for name in code_names}
    write(output/'policy_frozen_before_cases.json',{'policy':POLICY,'source_code_hashes':codes})
    prepared=[];audits=[]
    for index,row in enumerate(rows,1):
        path=Path(row['path']);sources[str(path.resolve())]=file_hash(path)
        source=json.loads(path.read_text())
        for key in ('case_id','patient_key','group','split','data_kind'):
            if source.get(key)!=row.get(key):raise ValueError('Source manifest identity differs: '+key)
        if source['data_kind']!='mimic_derived':raise ValueError('Expected verified MIMIC source')
        if source['group'] in SYMPTOMS:
            frequency,protected,counts=load_frequency_context(source,private_data_root)
            sources.update(protected)
            case,audit=build_episode(source,frequency)
            episode='constructed_chronic_followup_six_days'
        else:
            case=deepcopy(source);audit={'infection_episode_unchanged':True}
            counts={};episode='preserved_next_day_infection_reassessment'
        case=add_adult_population(case)
        case['case_id']+='_bridge8'
        case['provenance']['pilot_bridge']={'version':VERSION,'source_case_id':source['case_id'],
            'source_case_sha256':digest(source),'episode_type':episode,'model_outputs_read':False,
            'clinical_reference_status':'pending_independent_review'}
        for key in ('patient_key','group','split','data_kind','formulary'):
            if case[key]!=source[key]:raise ValueError('Forbidden case identity or formulary change')
        for key in HISTORY_FIELDS:
            if case['his_initial'][key]!=source['his_initial'][key]:raise ValueError('Historical clinical evidence changed')
        validate_case(case)
        destination=output/'cases'/(case['case_id']+'.json');write(destination,case)
        prepared.append({**{k:case[k] for k in ('case_id','patient_key','group','split','data_kind')},
            'path':str(destination.resolve()),'file_sha256':file_hash(destination),'case_hash':digest(case),
            'runtime_input_hash':digest(runtime_case(case)),'source_case_id':source['case_id'],'episode_type':episode})
        audits.append({'case_id':case['case_id'],'group':case['group'],'episode_type':episode,
            'source_history_unchanged':True,'patient_partition_unchanged':True,'clinical_sufficiency':'pending',
            'new_lab_or_imaging_values':0,'pharmacy_join_counts':counts,'structural_audit':audit})
        print(json.dumps({'prepared':index,'target':50,'group':case['group']}),flush=True)
    if any(file_hash(p)!=value for p,value in {**sources,**codes}.items()):
        raise ValueError('An input/code source changed during preparation')
    write(output/'manifest.json',{'version':VERSION,'cases':prepared,'case_count':50,'source_manifest':str(manifest_path.resolve()),
        'source_manifest_sha256':sources[str(manifest_path.resolve())],'formal_eligible':False,'policy':POLICY,
        'selection_uses_model_outcomes':False,'new_source_patients':0,'all_original_patients_retained':True})
    write(output/'qualification_private.json',{'policy':POLICY,'cases':audits,
        'mechanically_prepared':50,'clinically_qualified':None,'eligible_for_all_three_tasks':None})
    write(output/'input_freeze.json',{'source_files':{**sources,**codes},
        'files':[{'path':r['path'],'sha256':r['file_sha256']} for r in prepared]})
    return {'output':str(output),'cases':50,'groups':expected,'new_source_patients':0,'formal_eligible':False}


if __name__=='__main__':
    os.umask(0o077)
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ('manifest','private-data-root','output'):parser.add_argument('--'+name,type=Path,required=True)
    args=parser.parse_args();print(json.dumps(prepare(args.manifest,args.private_data_root,args.output),ensure_ascii=False))

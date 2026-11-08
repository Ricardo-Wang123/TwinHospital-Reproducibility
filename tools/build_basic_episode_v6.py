"""Freeze a source-only three-case engineering subset without reading run results."""
from copy import deepcopy
from pathlib import Path
import argparse
import json
import os
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from sandbox.experiments.linked.case_preparation.current_episode_v6 import (
    VERSION,POLICY,PROFILES,source_gate,build_basic_episode)
from sandbox.experiments.linked.case_preparation.v4_source_qualification import load_frequency_context
from sandbox.experiments.linked.case_preparation.formulary_overlay import file_hash,write


def generate(source,private_data,output):
    source,private_data,output=map(lambda p:Path(p).resolve(),(source,private_data,output))
    if output.exists():raise FileExistsError('Use a new destination')
    m=json.loads(source.read_text()); protected={str(source):file_hash(source)}
    repo=Path(__file__).resolve().parents[1]
    for relative in ('tools/build_basic_episode_v6.py',
        'sandbox/experiments/linked/case_preparation/current_episode_v6.py',
        'sandbox/experiments/linked/case_preparation/current_episode_v5.py',
        'sandbox/experiments/linked/case_preparation/current_episode_v4.py',
        'sandbox/experiments/linked/case_preparation/v4_source_qualification.py'):
        path=repo/relative;protected[str(path)]=file_hash(path)
    rows=[]; candidates={g:[] for g in PROFILES}
    for ordinal,entry in enumerate(m['cases'],1):
        if file_hash(entry['path'])!=entry['sha256']:raise ValueError('Source case changed')
        case=json.loads(Path(entry['path']).read_text());protected[entry['path']]=entry['sha256']
        if case['split']!='development':raise ValueError('Only development patients may be selected')
        context,sidecars,_=load_frequency_context(case,private_data);protected.update(sidecars)
        gate=source_gate(case,context); row={'ordinal':ordinal,'case_id':case['case_id'],'group':case['group'],**gate}
        rows.append(row)
        if gate['eligible_for_this_probe']:
            rank=(gate['scheduled_candidates'],gate['unresolved_candidates'],ordinal)
            candidates[case['group']].append((rank,entry,case,context))
    if any(not v for v in candidates.values()):raise ValueError('No qualified source for one probe profile')
    output.mkdir(parents=True,mode=0o700)
    write(output/'policy_frozen_before_cases.json',{'policy':POLICY,'profiles':PROFILES,
        'generator_sha256':file_hash(__file__),'model_calls':0})
    selected=[];audits=[]
    for group in PROFILES:
        rank,entry,case,context=sorted(candidates[group],key=lambda x:x[0])[0]
        result,audit=build_basic_episode(case,context)
        path=output/'cases'/(result['case_id']+'.json');write(path,result)
        new=deepcopy(entry);new.update(case_id=result['case_id'],path=str(path),sha256=file_hash(path),
            formal_eligible=False,prescribing_required=False,clinical_suitability='pending_independent_input_review')
        selected.append(new);audits.append({'source_ordinal':rank[2],'group':group,'rank':rank,**audit})
    if any(file_hash(p)!=h for p,h in protected.items()):raise ValueError('Source changed during construction')
    write(output/'manifest.json',{'version':VERSION,'case_count':3,'cases':selected,
        'source_manifest':str(source),'source_manifest_sha256':protected[str(source)],
        'purpose':POLICY['purpose'],'construction_policy':POLICY,'formal_eligible':False,
        'required_new_batch_version':'engineering_mimic_v17_basic3','new_run_required':True,
        'selection_uses_model_outcomes':False,'prior_failures_retained':True})
    write(output/'selection_audit_private.json',{'all_source_rows':rows,'selected':audits,'policy':POLICY})
    write(output/'input_freeze.json',{'version':VERSION,'source_files':protected,
        'files':[{'path':str(p),'sha256':file_hash(p)} for p in sorted(output.rglob('*.json'))]})
    print(json.dumps({'case_count':3,'selected_source_ordinals':[a['source_ordinal'] for a in audits],
        'source_unchanged':True,'model_calls':0,'formal_result':False}))

if __name__=='__main__':
    os.umask(0o077);p=argparse.ArgumentParser(description=__doc__)
    for key in ('source','private-data','output'):p.add_argument('--'+key,type=Path,required=True)
    a=p.parse_args();generate(a.source,a.private_data,a.output)

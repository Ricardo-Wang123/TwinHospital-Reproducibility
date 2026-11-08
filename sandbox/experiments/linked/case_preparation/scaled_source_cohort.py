"""Prepare larger source-material cohorts without reading model outcomes.

Previously assigned development/test patients never cross partitions. The fixed
active pilot is retained in development, including its unfinished encounters.
This selects historical material, not clinically qualified new outpatient visits.
"""
import argparse
from collections import Counter, defaultdict
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path

from ..cases import GROUPS
from .case_contract import _opaque
from .development_pilot50_v3 import SYSTEMS, window_ok
from .diagnosis_scope_labs import WINDOWS
from .mimic_cohort import assign_patients, rank, save

VERSION = 'scaled_source_material_v2_anyseq_patient_partitions'
POLICY = {
    'version': VERSION, 'target_development_source_per_group':100,
    'target_test_source_per_group':400, 'independent_constructed_development_target':500,
    'source_scope':'Same five frozen ICD families, any diagnosis sequence in the anchor admission; all other diagnoses are retained.',
    'material_screen':'Adult completed stay, survives the original next-day material cutoff, group-related scalar oral/inhaled order, and descriptive historical laboratory availability windows.',
    'lab_availability_windows_hours':WINDOWS,
    'windows_are_clinical_validity_thresholds':False,
    'selection_reads_clinical_result_values':False,
    'selection_reads_model_outputs':False,
    'selection_uses_paid_or_approved_status':False,
    'split_rule':'Every originally assigned development patient and every pilot patient stays development-only; every originally assigned test patient stays test-only. Newly selected patients receive one partition. All derivatives follow their source patient.',
    'pilot_retention':'All active fixed pilot source patients retained, with no selection by success or failure. Earlier excluded pilot candidates remain in the development-only exclusion registry.',
    'assignment':'Deterministic keyed maximum matching among eligible patients; retain pilot anchors, rank other eligible anchors without laboratory value or outcome selection.',
    'clinical_qualified':None, 'formal_eligible':False,
    'current_episode':'Not constructed by this selector. History availability does not establish current prescribing need or full-flow completion.',
    'references':['https://mimic.mit.edu/docs/iv/modules/hosp/diagnoses_icd.html',
        'https://mimic.mit.edu/docs/iv/modules/hosp/prescriptions.html',
        'https://mimic.mit.edu/docs/iv/modules/hosp/labevents.html'],
}


def qualified_material(row,group):
    return (group in row['any_sequence'] and row['survives_next_day'] is True
        and group in row['group_parsed_rx'] and window_ok(row,group))


def select(records,original,active_pilot,all_pilot,key,*,development_per_group=100,test_per_group=400):
    prior={}
    for r in original:
        if r['split'] not in {'development','test'}:raise ValueError('Unknown prior partition')
        if r['subject_id'] in prior and prior[r['subject_id']]!=r['split']:raise ValueError('Prior patient crosses partitions')
        prior[r['subject_id']]=r['split']
    for r in all_pilot:
        if prior.get(r['subject_id'])=='test':raise ValueError('Pilot exposed a test patient')
        prior[r['subject_id']]='development'
    lookup={(r['subject_id'],r['hadm_id']):r for r in records}
    fixed={g:[] for g in GROUPS};fixed_ids=set()
    for r in active_pilot:
        if r['subject_id'] in fixed_ids:raise ValueError('Active pilot repeats a source patient')
        if prior.get(r['subject_id'])!='development':raise ValueError('Active pilot missing development registry')
        source=lookup.get((r['subject_id'],r['hadm_id']))
        if source is None or not qualified_material(source,r['group']):raise ValueError('Active source fails the declared material screen')
        fixed[r['group']].append(deepcopy(r));fixed_ids.add(r['subject_id'])
    if any(len(fixed[g])>development_per_group for g in GROUPS):raise ValueError('Pilot exceeds target quota')
    pool={g:defaultdict(list) for g in GROUPS}
    for r in records:
        for group in GROUPS:
            if qualified_material(r,group):pool[group][r['subject_id']].append(r)
    dev_pool={g:{sid:rows for sid,rows in pool[g].items()
        if prior.get(sid)!='test' and sid not in fixed_ids} for g in GROUPS}
    dev_selected=assign_patients(dev_pool,{g:development_per_group-len(fixed[g]) for g in GROUPS},key)
    newly_assigned_dev=set().union(*dev_selected.values())|fixed_ids
    test_pool={g:{sid:rows for sid,rows in pool[g].items()
        if prior.get(sid)!='development' and sid not in newly_assigned_dev} for g in GROUPS}
    test_selected=assign_patients(test_pool,{g:test_per_group for g in GROUPS},key)
    selections=[];counts={}
    for group in GROUPS:
        counts[group]={'source_material_patients':len(pool[group]),'development_pool':len(dev_pool[group]),
            'test_pool_after_development_assignment':len(test_pool[group]),'fixed_pilot':len(fixed[group]),
            'development':len(fixed[group])+len(dev_selected[group]),'test':len(test_selected[group])}
        for r in fixed[group]:
            selections.append({**r,'split':'development','system':SYSTEMS[group],
                'selection_version':VERSION,'active_pilot_anchor_retained':True})
        for split,chosen,available in [('development',dev_selected,dev_pool),('test',test_selected,test_pool)]:
            for sid in sorted(chosen[group],key=lambda sid:rank(key,VERSION+':'+split+':'+group+':'+sid)):
                anchor=min(available[group][sid],key=lambda r:rank(key,VERSION+':anchor:'+r['hadm_id']))
                selections.append({**deepcopy(anchor),'group':group,'split':split,'system':SYSTEMS[group],
                    'selection_version':VERSION,'active_pilot_anchor_retained':False})
    if len({r['subject_id'] for r in selections})!=len(selections):raise AssertionError('A source patient was reused')
    for r in selections:
        if r['subject_id'] in prior and prior[r['subject_id']]!=r['split']:raise AssertionError('Prior partition changed')
    full=all(v['development']==development_per_group and v['test']==test_per_group for v in counts.values())
    summary={'version':VERSION,'candidate_source_patients':len(selections),'target_filled':full,
        'counts_by_group':counts,'counts_by_partition':dict(Counter(r['split'] for r in selections)),
        'counts_by_system':dict(Counter(r['system'] for r in selections)),
        'prior_partition_changes':0,'cross_group_patient_duplicates':0,
        'active_pilot_patients_retained':len(fixed_ids),'model_outcomes_read':False,
        'clinical_qualified':None,'formal_experiment_inputs_ready':False}
    return selections,summary


def prepare(private_root,active_manifest,output):
    private_root,active_manifest,output=map(Path,(private_root,active_manifest,output))
    if output.exists():raise FileExistsError('Do not replace a frozen candidate registry')
    paths={name:private_root/path for name,path in {
        'original':'mimic_batch/selection_private.json',
        'pilot':'development_pilot50_v3/selection_private.json',
        'replacement':'development_pilot50_replacement_v1/selection_private.json',
        'records':'diagnosis_scope_labs_v2/lab_flags_private.json'}.items()}
    paths['active_manifest']=active_manifest
    hashes={str(p.resolve()):hashlib.sha256(p.read_bytes()).hexdigest() for p in paths.values()}
    hashes[str(Path(__file__).resolve())]=hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    key=(private_root/'mimic_batch/identity.key').read_bytes()
    load=lambda p:json.loads(p.read_text())
    all_pilot=load(paths['pilot'])+load(paths['replacement'])
    active_rows=load(active_manifest)['cases'];active_keys={r['patient_key']:r['group'] for r in active_rows}
    if len(active_keys)!=len(active_rows):raise ValueError('Active manifest repeats patients')
    active=[r for r in all_pilot if active_keys.get(_opaque(key,'pt',r['subject_id']))==r['group']]
    if len(active)!=len(active_rows):raise ValueError('Active pilot does not match the source registry')
    output.mkdir(parents=True,mode=0o700)
    save(output/'policy_frozen_before_selection.json',{'policy':POLICY,'source_file_hashes':hashes})
    selected,summary=select(load(paths['records'])['records'],load(paths['original']),active,all_pilot,key)
    if any(hashlib.sha256(Path(p).read_bytes()).hexdigest()!=h for p,h in hashes.items()):
        raise ValueError('Source registry changed during selection')
    save(output/'selection_private.json',selected)
    save(output/'development_selection_private.json',[r for r in selected if r['split']=='development'])
    save(output/'test_selection_private.json',[r for r in selected if r['split']=='test'])
    save(output/'selection_summary.json',summary)
    return summary


if __name__=='__main__':
    os.umask(0o077);p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--private-root',required=True);p.add_argument('--active-manifest',required=True);p.add_argument('--output',required=True)
    a=p.parse_args();print(json.dumps(prepare(a.private_root,a.active_manifest,a.output),ensure_ascii=False))

"""Create a new runtime version by replacing only the shared formulary.

No clinical facts, evaluator labels, source provenance, split or source patient
are changed. A new batch/run is required; existing HIS objects must not be reused.
"""
from __future__ import annotations
import argparse
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
from ..contracts import runtime_case

VERSION='formulary_only_runtime_overlay_v1'
ALLOWED_CHANGED_FIELDS=frozenset({'formulary','runtime_overlay_provenance'})

def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False,allow_nan=False,separators=(',',':')).encode()).hexdigest()

def file_hash(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()

def write(path,value):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    path.write_text(json.dumps(value,ensure_ascii=False,indent=2,allow_nan=False)+'\n');path.chmod(0o600)

def validate_formulary(items):
    if not isinstance(items,list) or not items:raise ValueError('Expected nonempty shared formulary list')
    codes=[]
    for item in items:
        if not isinstance(item,dict) or any(not item.get(k) for k in ('drug_code','name','dose_unit','quantity_unit')):
            raise ValueError('Formulary identity/units are incomplete')
        if not item.get('route') and not item.get('allowed_routes'):raise ValueError('Formulary route absent')
        codes.append(item['drug_code'])
    if len(set(codes))!=len(codes):raise ValueError('Duplicate formulary product code')

def apply_formulary_overlay(case,formulary,metadata):
    validate_formulary(formulary);result=deepcopy(case)
    result['formulary']=deepcopy(formulary)
    result['runtime_overlay_provenance']={
        'policy':VERSION,**deepcopy(metadata),'allowed_case_changes':sorted(ALLOWED_CHANGED_FIELDS),
        'source_patient_facts_changed':False,'historical_records_changed':False,
        'evaluator_reference_changed':False,'source_product_exact_mapping_verified_by_this_overlay':False,
        'meaning':'A shared simulated hospital product catalogue; not a treatment key or a claim that source NDC/manufacturer/formulation is verified.',
        'previous_overlay':deepcopy(case.get('runtime_overlay_provenance')),
    }
    before={k:digest(v) for k,v in case.items() if k not in ALLOWED_CHANGED_FIELDS}
    after={k:digest(v) for k,v in result.items() if k not in ALLOWED_CHANGED_FIELDS}
    if before!=after:raise RuntimeError('Forbidden clinical/source field mutation')
    changed=sorted(k for k in set(case)|set(result) if case.get(k)!=result.get(k))
    if set(changed)-ALLOWED_CHANGED_FIELDS:raise RuntimeError('Unexpected case diff')
    return result,{'unchanged_field_hashes':before,'changed_top_level_fields':changed,
        'old_formulary_hash':digest(case.get('formulary')),'new_formulary_hash':digest(formulary),
        'all_non_overlay_fields_unchanged':True}

def generate(manifest_path,formulary_path,output,overlay_version,batch_version,engineering_count=5):
    manifest_path,formulary_path,output=map(Path,(manifest_path,formulary_path,output))
    output=output.resolve()
    if output.exists():raise FileExistsError('Never overwrite an existing runtime version')
    if not overlay_version or not batch_version:raise ValueError('Explicit new overlay and batch versions required')
    m=json.loads(manifest_path.read_text());form=json.loads(formulary_path.read_text())
    if isinstance(form,dict):form=form.get('formulary',form.get('items'))
    validate_formulary(form);entries=m['cases']
    if not 0<=engineering_count<=len(entries):raise ValueError('Invalid engineering subset count')
    if len({e['case_id'] for e in entries})!=len(entries):raise ValueError('Duplicate case IDs')
    protected={str(manifest_path.resolve()):file_hash(manifest_path),str(formulary_path.resolve()):file_hash(formulary_path)}
    for e in entries:
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,160}',e['case_id']):raise ValueError('Unsafe case filename')
        actual=file_hash(e['path'])
        source_hashes = [e[key] for key in ('file_sha256', 'sha256') if key in e]
        if not source_hashes or any(actual != expected for expected in source_hashes):
            raise ValueError('Frozen source case hash differs')
        protected[str(Path(e['path']).resolve())]=actual
    output.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    staging=Path(tempfile.mkdtemp(prefix='.'+output.name+'.',dir=output.parent));staging.chmod(0o700)
    try:
        new_entries=[];audit=[]
        for e in entries:
            c=json.loads(Path(e['path']).read_text())
            if c['case_id']!=e['case_id'] or any(c.get(k)!=e.get(k) for k in ('patient_key','group','split','data_kind')):
                raise ValueError('Manifest/source identity differs')
            raw_hash = protected[str(Path(e['path']).resolve())]
            result,check=apply_formulary_overlay(c,form,{
                'overlay_version':overlay_version,'required_new_batch_version':batch_version,
                'source_case_sha256':raw_hash,'source_manifest_sha256':protected[str(manifest_path.resolve())],
                'formulary_source_sha256':protected[str(formulary_path.resolve())],
                'source_formulary_file':str(formulary_path.resolve()),'new_run_required':True})
            relative=Path('cases')/(e['case_id']+'.json');write(staging/relative,result)
            row=deepcopy(e);row.update(path=str(output/relative),sha256=file_hash(staging/relative),
                file_sha256=file_hash(staging/relative),case_hash=digest(result),
                runtime_input_hash=digest(runtime_case(result)));new_entries.append(row)
            audit.append({'case_id':e['case_id'],'patient_key':e['patient_key'],'split':e['split'],
                'source_case_path':e['path'],'source_case_sha256':raw_hash,
                'new_case_path':row['path'],'new_case_sha256':row['sha256'],**check})
        result_manifest=deepcopy(m);result_manifest.update(version=overlay_version,cases=new_entries,case_count=len(new_entries),
            overlay_source_manifest=str(manifest_path.resolve()),overlay_source_manifest_sha256=protected[str(manifest_path.resolve())],
            runtime_overlay_policy=VERSION,required_new_batch_version=batch_version,new_run_required=True,
            reuse_existing_business_state=False,source_patients_and_partitions_unchanged=True,
            source_product_identity_verified_by_overlay=False)
        write(staging/'manifest.json',result_manifest)
        subset=deepcopy(result_manifest);subset.update(cases=new_entries[:engineering_count],case_count=engineering_count,
            purpose='Same fixed first cases in input order, shared formulary-only overlay; not outcome-selected')
        write(staging/'engineering_manifest.json',subset)
        if any(file_hash(p)!=h for p,h in protected.items()):raise RuntimeError('Input changed while creating overlay')
        summary={'policy':VERSION,'case_count':len(new_entries),'formulary_products':len(form),
            'all_patient_initial_hashes_unchanged':True,'all_his_initial_hashes_unchanged':True,
            'all_evaluation_hashes_unchanged':True,'all_other_protected_fields_unchanged':True,
            'source_input_hashes_unchanged':True,'clinical_fact_mutations':0,'model_calls':0,
            'source_exact_product_mappings_assigned':0,'new_run_required':True}
        write(staging/'overlay_audit_private.json',{'summary':summary,'cases':audit,'source_files':protected})
        write(staging/'overlay_summary.json',summary)
        freeze=[{'path':str(output/p.relative_to(staging)),'sha256':file_hash(p)} for p in staging.rglob('*') if p.is_file()]
        write(staging/'input_freeze.json',{'version':overlay_version,'files':freeze})
        staging.rename(output)
        return summary
    except BaseException:
        shutil.rmtree(staging);raise

if __name__=='__main__':
    os.umask(0o077);p=argparse.ArgumentParser();p.add_argument('--manifest',required=True);p.add_argument('--formulary',required=True)
    p.add_argument('--output',required=True);p.add_argument('--overlay-version',required=True);p.add_argument('--batch-version',required=True)
    p.add_argument('--engineering-count',type=int,default=5);a=p.parse_args()
    print(json.dumps(generate(a.manifest,a.formulary,a.output,a.overlay_version,a.batch_version,a.engineering_count)))

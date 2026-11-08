"""Freeze the user-resized 1000/500 cohort from an existing held-out manifest.

Selection uses only patient identity and a fixed salt. No clinical outputs,
failure records, or favorable outcomes are read. Clinical input hashes are
checked after selection, with failure stopping the export rather than resampling.
"""
from collections import Counter
from copy import deepcopy
from datetime import datetime,timezone
import argparse
import hashlib
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from sandbox.experiments.linked.cases import GROUPS
from sandbox.experiments.linked.contracts import digest,runtime_case
from sandbox.experiments.linked.source_operation_replay import CATEGORIES,validate_schedule
from sandbox.experiments.linked.provider import private_json

SALT='TwinHospital-user-resized-1000-500-20261107-v1'


def rank(row,scope):
    return hashlib.sha256((SALT+'|'+scope+'|'+row['patient_key']).encode()).hexdigest()


def select(source):
    rows=source['cases']
    if (len(rows)!=2000 or len({r['patient_key'] for r in rows})!=2000
            or len({r['case_id'] for r in rows})!=2000
            or Counter(r['group'] for r in rows)!=Counter({g:400 for g in GROUPS})
            or any(r['data_kind']!='mimic_derived' or r['split']!='test' for r in rows)):
        raise ValueError('Expected the original 2000 unique held-out patients, 400 per stratum')
    main=[];local=[]
    for group in GROUPS:
        selected=sorted((r for r in rows if r['group']==group),key=lambda r:rank(r,'main'))[:200]
        main.extend(deepcopy(selected))
        subset=sorted(selected,key=lambda r:rank(r,'local'))[:100]
        for i,row in enumerate(subset):
            r=deepcopy(row)
            r.update(local_test_id='local-'+rank(r,'identity')[:24],
                operation_category=list(CATEGORIES)[i//20],
                request_condition='single_fault' if i%20<4 else 'compliant_control',
                source_operation_binding='bind to actual recorded operation and preserved checkpoint after complete-run execution',
                missing_source_operation_policy='retain unresolved test; no outcome-based replacement')
            local.append(r)
    cohort={'cases':main,'case_count':1000,'purpose':'Frozen patient selection for user-resized formal study; execution eligibility still requires versioned preflight'}
    schedule={'cases':local,'case_count':500,'purpose':'Preassigned local operations; no additional full encounters'}
    validate_schedule(schedule,cohort)
    return cohort,schedule


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--source',type=Path,required=True);p.add_argument('--engineering-manifest',type=Path,required=True)
    p.add_argument('--output',type=Path,required=True)
    a=p.parse_args();source=json.loads(a.source.read_text());cohort,schedule=select(source)
    excluded={r['patient_key'] for r in json.loads(a.engineering_manifest.read_text())['cases']}
    if excluded & {r['patient_key'] for r in cohort['cases']}:raise ValueError('Engineering patient overlap')
    for row in cohort['cases']:
        raw=Path(row['path']).read_bytes();case=json.loads(raw)
        if hashlib.sha256(raw).hexdigest()!=row['file_sha256'] or digest(runtime_case(case))!=row['runtime_input_hash']:
            raise ValueError('Selected source changed; do not substitute a patient')
        if any(case[k]!=row[k] for k in ('case_id','patient_key','group','split','data_kind')):
            raise ValueError('Selected source identity mismatch')
    a.output.mkdir(parents=True,exist_ok=False,mode=0o700)
    private_json(a.output/'formal_test1000_manifest.json',cohort)
    private_json(a.output/'app3_local500_manifest.json',schedule)
    receipt={'frozen_at':datetime.now(timezone.utc).isoformat(),'selection_salt':SALT,
        'source_manifest_sha256':hashlib.sha256(a.source.read_bytes()).hexdigest(),
        'engineering_manifest_sha256':hashlib.sha256(a.engineering_manifest.read_bytes()).hexdigest(),
        'main_source_patients':1000,'main_per_disease':200,'local_source_patients':500,'local_per_disease':100,
        'local_errors':100,'local_controls':400,'local_is_strict_subset':True,'engineering_overlap':0,
        'verified_source_files':1000,'clinical_outputs_read':False,
        'selection_is_not_proof_of_workflow_completion':True,
        'case_version_changes_must_rebind_the_same_selected_patients':True,
        'driver_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
    private_json(a.output/'selection_receipt.json',receipt);print(json.dumps(receipt))


if __name__=='__main__':main()

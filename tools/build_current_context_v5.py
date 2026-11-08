"""Prepare a new immutable, private three-case manifest; no model calls."""
from copy import deepcopy
from pathlib import Path
import argparse
import json
import os
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from sandbox.experiments.linked.case_preparation.current_episode_v5 import build_context, VERSION, POLICY
from sandbox.experiments.linked.case_preparation.formulary_overlay import file_hash, write


def generate(source, output):
    source,output=Path(source).resolve(),Path(output).resolve()
    if output.exists():raise FileExistsError('Use a new destination, never replace prior inputs')
    manifest=json.loads(source.read_text());entries=manifest['cases']
    if [e['group'] for e in entries]!=['type2_diabetes','heart_failure','copd']:
        raise ValueError('Only the fixed three-case development manifest is in scope')
    expected={str(source):file_hash(source)}
    built=[]
    for entry in entries:
        if file_hash(entry['path'])!=entry['sha256']:raise ValueError('Source hash changed')
        expected[entry['path']]=entry['sha256']
        case=json.loads(Path(entry['path']).read_text())
        if any(case[k]!=entry[k] for k in ('case_id','patient_key','group','split','data_kind')):
            raise ValueError('Case/manifest identity mismatch')
        result,audit=build_context(case)
        built.append((entry,result,audit))
    output.mkdir(parents=True,mode=0o700)
    rows=[];audits=[]
    for entry,result,audit in built:
        path=output/'cases'/(result['case_id']+'.json');write(path,result)
        row=deepcopy(entry);row.update(case_id=result['case_id'],path=str(path),sha256=file_hash(path),
            formal_eligible=False,parent_episode_entry=deepcopy(entry))
        rows.append(row);audits.append({'group':entry['group'],**audit})
    new=deepcopy(manifest);new.update(version=VERSION,cases=rows,case_count=3,
        source_manifest=str(source),source_manifest_sha256=expected[str(source)],
        required_new_batch_version='engineering_mimic_v15_context3',new_run_required=True,
        construction_policy=POLICY,formal_eligible=False)
    write(output/'manifest.json',new)
    if any(file_hash(path)!=sha for path,sha in expected.items()):raise RuntimeError('Source changed during construction')
    summary={'version':VERSION,'case_count':3,'model_calls':0,'source_files_unchanged':True,
        'cases':audits,'new_laboratory_values':0,'clinical_reference_status':'pending_independent_review',
        'policy':POLICY}
    write(output/'audit_summary.json',summary)
    write(output/'input_freeze.json',{'version':VERSION,'source_files':expected,
        'files':[{'path':str(p),'sha256':file_hash(p)} for p in sorted(output.rglob('*.json'))]})
    print(json.dumps({'destination':str(output),'case_count':3,'source_files_unchanged':True}))

if __name__=='__main__':
    os.umask(0o077)
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source',type=Path,required=True);parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args();generate(args.source,args.output)

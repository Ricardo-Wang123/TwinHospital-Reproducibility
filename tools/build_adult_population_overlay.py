"""Freeze a categorical-age overlay of an existing, unchanged case selection."""
from copy import deepcopy
from pathlib import Path
import argparse
import json
import os
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from sandbox.experiments.linked.case_preparation.adult_population import add_adult_population,VERSION,SOURCE
from sandbox.experiments.linked.case_preparation.formulary_overlay import file_hash,write


def generate(source,output):
    source,output=Path(source).resolve(),Path(output).resolve()
    if output.exists():raise FileExistsError('Existing input versions are immutable; choose a new output')
    manifest=json.loads(source.read_text());rows=[]
    protected={str(source):file_hash(source),str(Path(__file__).resolve()):file_hash(__file__)}
    module=Path(__file__).resolve().parents[1]/'sandbox/experiments/linked/case_preparation/adult_population.py'
    protected[str(module)]=file_hash(module)
    # Validate the entire input before creating the output directory.
    cases=[]
    for row in manifest['cases']:
        path=Path(row['path']);expected=row['sha256']
        if file_hash(path)!=expected:raise ValueError('Source case changed')
        protected[str(path)]=expected
        case=json.loads(path.read_text());cases.append((row,add_adult_population(case)))
    output.mkdir(parents=True,mode=0o700)
    for old,case in cases:
        path=output/'cases'/(case['case_id']+'.json');write(path,case)
        row=deepcopy(old);row.update(case_id=case['case_id'],path=str(path),sha256=file_hash(path),
            parent_case_id=old['case_id'],formal_eligible=False)
        rows.append(row)
    result=deepcopy(manifest)
    result.update(version=VERSION,cases=rows,case_count=len(rows),source_manifest=str(source),
        source_manifest_sha256=protected[str(source)],required_new_batch_version='engineering_mimic_v20_interfaces3',
        overlay={'version':VERSION,'source_reference':SOURCE,'new_source_patients':0,
            'clinical_measurements_added':0,'selection_changed':False,'source_history_unchanged':True,
            'exact_ages_added':0,'age_category_basis':'documented_source_cohort_at_post_source_visit'},
        new_run_required=True,prior_failures_retained=True)
    if any(file_hash(p)!=h for p,h in protected.items()):raise ValueError('Source changed during overlay')
    write(output/'manifest.json',result)
    write(output/'input_freeze.json',{'version':VERSION,'source_files':protected,
        'files':[{'path':str(p),'sha256':file_hash(p)} for p in sorted(output.rglob('*.json'))]})
    print(json.dumps({'cases':len(rows),'selection_changed':False,'new_clinical_values':0,'output':str(output)}))


if __name__=='__main__':
    os.umask(0o077);p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--source',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
    args=p.parse_args();generate(args.source,args.output)

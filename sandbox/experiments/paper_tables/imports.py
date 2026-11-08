"""Import questionnaire and hospital-return rows into the actual table exporter."""
from __future__ import annotations
import argparse,json
from copy import deepcopy
from pathlib import Path
from .materials import constructed_survey
from .table_export import export_tables


def load(path):
    path=Path(path)
    if path.suffix.lower()=='.jsonl':
        return [json.loads(line) for line in path.read_text(encoding='utf-8-sig').splitlines() if line.strip()]
    return json.loads(path.read_text(encoding='utf-8-sig'))


def import_materials(output,context,observations,*,questionnaire_rows=None,hospital_rows=None):
    output=Path(output).resolve();output.mkdir(parents=True,exist_ok=True)
    context=deepcopy(context);observations=deepcopy(observations)
    if questionnaire_rows is not None:
        if not isinstance(questionnaire_rows,list): raise ValueError('questionnaire input must be a row list')
        for row in questionnaire_rows:
            for field in ('participant_id','participant_group','workflow','dimension','score','source_kind'):
                if field not in row: raise ValueError('questionnaire row missing '+field)
            if row['participant_group'] not in ('patient','clinician') or row['workflow'] not in ('existing','human_ai'):
                raise ValueError('unknown participant group/workflow')
        context['participant_rows']=questionnaire_rows
    if hospital_rows is not None:
        if not isinstance(hospital_rows,list): raise ValueError('hospital input must be an observation row list')
        for row in hospital_rows:
            for field in ('case_id','config_id','source_kind','table_ids','data','evidence_refs','stage'):
                if field not in row: raise ValueError('hospital return row missing '+field)
            if not set(row['table_ids']).issubset({6,9,11,13}): raise ValueError('hospital return must target tables 6/9/11/13')
            if row['source_kind']=='hospital_independent' and not row.get('independent_execution'):
                raise ValueError('hospital_independent requires explicit independent_execution evidence')
            if not isinstance(row['data'],dict) or not row['evidence_refs']:
                raise ValueError('hospital return needs raw data and original evidence references')
            observations.append(row)
    for name,value in [('input-context.json',context),('input-observations.json',observations)]:
        (output/name).write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding='utf-8')
    result=export_tables(output,observations,context)
    (output/'import-receipt.json').write_text(json.dumps({'source':'raw-row import, not new participant execution',
        'questionnaire_rows':len(questionnaire_rows or []),'hospital_return_rows':len(hospital_rows or []),
        'formal_eligible':result['formal_eligible'],'export':result},ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(result,ensure_ascii=False),flush=True)
    return result


def constructed_example(run_dir,output):
    run_dir=Path(run_dir).resolve();output=Path(output).resolve();output.mkdir(parents=True,exist_ok=True)
    context=load(run_dir/'table-context.json')
    context['source_kind']='constructed_import_example_not_independent_hospital_or_real_people'
    original=load(run_dir/'observations.json')
    survey=constructed_survey(output)
    # These are explicit example return files derived from the saved simulation,
    # not an additional independently executed hospital cohort.
    hospital=[];mapping={5:6,8:9,10:11,12:13}
    for source in original:
        ids=[mapping[t] for t in source['table_ids'] if t in mapping]
        if not ids: continue
        row=deepcopy(source);row['table_ids']=ids;row['source_kind']='constructed'
        row['independent_execution']=False
        row['data']['import_example_note']='Derived from saved model-run output solely to exercise hospital-return import; not independently executed hospital data.'
        hospital.append(row)
    (output/'constructed-hospital-return.json').write_text(json.dumps(hospital,ensure_ascii=False,indent=2),encoding='utf-8')
    return import_materials(output,context,original,questionnaire_rows=survey,hospital_rows=hospital)


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--output',required=True);p.add_argument('--context');p.add_argument('--observations')
    p.add_argument('--questionnaire');p.add_argument('--hospital-return');p.add_argument('--constructed-example-run')
    a=p.parse_args()
    if a.constructed_example_run: constructed_example(a.constructed_example_run,a.output)
    else:
        if not a.context: p.error('--context is required for material import')
        import_materials(a.output,load(a.context),load(a.observations) if a.observations else [],
            questionnaire_rows=load(a.questionnaire) if a.questionnaire else None,
            hospital_rows=load(a.hospital_return) if a.hospital_return else None)

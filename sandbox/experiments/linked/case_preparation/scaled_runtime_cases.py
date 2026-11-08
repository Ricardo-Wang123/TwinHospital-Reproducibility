"""Apply the declared source adapters without selecting on experiment outcomes.

Existing development cases are retained verbatim. Every other selected patient
is processed once into an inspectable draft or an explicit preparation failure.
These are candidate inputs, not clinically qualified or formally frozen cases.
"""
from collections import Counter
from copy import deepcopy
import argparse,json,os,sqlite3
from pathlib import Path

from .case_contract import build_case,validate_package,_opaque
from .closed_followup_v3 import source_seed,build_linked,medication_candidates
from .current_episode_v10 import build as chronic
from .infection_episode_v11 import build as infection
from .adult_population import add_adult_population
from .supplemental_overlay import adapt
from .formulary_overlay import apply_formulary_overlay,file_hash,write
from .v4_source_qualification import load_frequency_context
from ..cases import validate_case
from ..contracts import digest,runtime_case

VERSION='scaled_candidate_inputs_v1'


def retained_entry(selection,active,secret):
    key=_opaque(secret,'pt',selection['subject_id']);row=active.get(key)
    if row is None:return None
    if row['split']!=selection['split'] or row['group']!=selection['group']:
        raise ValueError('An existing patient cannot change partition or stratum')
    if row['split']!='development':raise ValueError('Development reuse cannot include test patients')
    path=Path(row['path']);expected=row.get('file_sha256',row.get('sha256'))
    if not expected or file_hash(path)!=expected:raise ValueError('Retained case changed')
    return deepcopy(row)


def prepare(source_folder,supplement,active_manifest,formulary_path,secret_path,output):
    os.umask(0o077)
    source_folder,supplement,active_manifest,formulary_path,secret_path,output=map(Path,
        (source_folder,supplement,active_manifest,formulary_path,secret_path,output))
    if output.exists():raise FileExistsError('Keep previous candidate inputs')
    selected=json.loads((source_folder/'selection_private.json').read_text())
    active_rows=json.loads(active_manifest.read_text())['cases']
    active={r['patient_key']:r for r in active_rows}
    if len(active)!=len(active_rows):raise ValueError('Repeated active patient')
    if len({r['subject_id'] for r in selected})!=len(selected):raise ValueError('Repeated selected patient')
    secret=secret_path.read_bytes();formulary=json.loads(formulary_path.read_text())
    if isinstance(formulary,dict):formulary=formulary['items']
    cache_meta=json.loads((source_folder/'source_cache_complete.json').read_text())
    meta=json.loads((supplement/'summary.json').read_text())
    if meta['status']!='source_cache_complete_runtime_integration_pending':raise ValueError('Unfinished supplement')
    if file_hash(supplement/'source_rows.sqlite')!=meta['cache_sha256']:raise ValueError('Supplement changed')
    targets={(r['subject_id'],r['hadm_id']):r for r in json.loads((supplement/'target_anchors_private.json').read_text())['targets']}
    paths=[source_folder/'selection_private.json',source_folder/'source_cache.sqlite',source_folder/'source_cache_complete.json',
        supplement/'summary.json',supplement/'target_anchors_private.json',active_manifest,formulary_path]
    protected={str(p.resolve()):file_hash(p) for p in paths}
    codes={str(p.resolve()):file_hash(p) for p in Path(__file__).parent.glob('*.py')}
    output.mkdir(parents=True,mode=0o700)
    policy={'version':VERSION,'source_hashes':protected,'code_hashes':codes,
        'selection':'Every source-selected patient; retain the existing development cases byte-for-byte; no model result files read.',
        'partitions':'Source patient and all derivatives keep the selection partition; held-out patients cannot enter development.',
        'adapters':'Chronic v10, infection v11, adult population, current shared formulary, time-eligible supplemental source history.',
        'constructed_findings':'Infection v11 explicitly authors present findings, including declared persistence of historical lab values; these are not observed MIMIC outpatient visits.',
        'failures':'Keep failure record and patient in registry; no silent replacement or success-only inclusion.',
        'formal_eligible':False,'clinical_review':'pending','model_calls':0}
    write(output/'policy_frozen_before_generation.json',policy)
    entries=[];failures=[];audits=[];retained=set()
    with sqlite3.connect('file:'+str(source_folder/'source_cache.sqlite')+'?mode=ro',uri=True) as db, \
         sqlite3.connect('file:'+str(supplement/'source_rows.sqlite')+'?mode=ro',uri=True) as supplemental:
        dictionaries=({(c,v):json.loads(p) for c,v,p in db.execute("SELECT code,version,payload FROM dictionary WHERE table_name='d_icd_diagnoses'")},
            {c:json.loads(p) for c,p in db.execute("SELECT code,payload FROM dictionary WHERE table_name='d_labitems'")})
        for index,selection in enumerate(selected):
            key=_opaque(secret,'pt',selection['subject_id'])
            old=retained_entry(selection,active,secret)
            if old:
                entries.append(old);retained.add(key);protected[str(Path(old['path']).resolve())]=old.get('file_sha256',old.get('sha256'))
                audits.append({'patient_key':key,'split':selection['split'],'group':selection['group'],'retained_existing_input':True})
                continue
            try:
                seed,pharmacy=source_seed(db,selection,dictionaries,cache_meta)
                package=build_case(seed,selection['group'],secret)
                if validate_package(package):raise ValueError('Historical package failed its source contract')
                source=output/'source_packages'/package['case']['case_id']
                # One complete source package per new patient, outside model context.
                for name,value in package.items():write(source/(name+'.json'),value)
                write(source/'pharmacy_private.json',pharmacy)
                candidates,rejected=medication_candidates(package,selection['group'])
                if not candidates:raise ValueError('No candidate with explicit route and parsable scalar regimen')
                base=build_linked(package,seed,selection,index,candidates[0])
                base,_=apply_formulary_overlay(base,formulary,{'version':VERSION,'formulary_sha256':protected[str(formulary_path.resolve())]})
                if selection['group'] in {'type2_diabetes','heart_failure','copd'}:
                    frequency,hashes,counts=load_frequency_context(base,output,source_package_root=output/'source_packages')
                    protected.update(hashes);case,audit=chronic(base,frequency);case=add_adult_population(case)
                else:
                    case,audit=infection(add_adult_population(base));counts={}
                case,supp_audit=adapt(case,targets[(selection['subject_id'],selection['hadm_id'])],supplemental,secret)
                if any(case[k]!=v for k,v in {'patient_key':key,'split':selection['split'],'group':selection['group']}.items()):
                    raise ValueError('Identity or partition changed during adaptation')
                validate_case(case)
                path=output/'cases'/(case['case_id']+'.json');write(path,case)
                entries.append({**{k:case[k] for k in ('case_id','patient_key','group','split','data_kind')},
                    'path':str(path.resolve()),'file_sha256':file_hash(path),'case_hash':digest(case),
                    'runtime_input_hash':digest(runtime_case(case)),'formal_eligible':False})
                audits.append({'patient_key':key,'split':selection['split'],'group':selection['group'],
                    'retained_existing_input':False,'case_audit':audit,'pharmacy_join_counts':counts,'supplement':supp_audit,
                    'candidate_rejections':rejected})
            except (ValueError,KeyError,TypeError) as exc:
                failures.append({'patient_key':key,'group':selection['group'],'split':selection['split'],
                    'status':'preparation_failed','error_type':type(exc).__name__,'reason':str(exc)})
            if (index+1)%25==0:
                print(json.dumps({'processed':index+1,'target':len(selected),'generated_or_retained':len(entries),'failed':len(failures)}),flush=True)
    if retained!=set(active):raise ValueError('An original development patient was lost')
    if any(file_hash(p)!=h for p,h in {**protected,**codes}.items()):raise ValueError('Source or adapter changed during generation')
    if len(entries)+len(failures)!=len(selected):raise ValueError('Candidate denominator lost')
    payload={'version':VERSION,'cases':entries,'case_count':len(entries),'formal_eligible':False,
        'selected_source_patient_count':len(selected),'preparation_failures':len(failures),'policy':policy}
    write(output/'manifest.json',payload)
    for split in ('development','test'):
        rows=[r for r in entries if r['split']==split]
        write(output/(split+'_manifest.json'),{**payload,'cases':rows,'case_count':len(rows)})
    write(output/'preparation_failures_private.json',{'cases':failures})
    write(output/'audit_private.json',{'cases':audits})
    write(output/'input_freeze.json',{'source_files':{**protected,**codes},
        'files':[{'path':r['path'],'sha256':r.get('file_sha256',r.get('sha256'))} for r in entries]})
    summary={'selected':len(selected),'prepared':len(entries),'failures':len(failures),'retained_original_cases':len(retained),
        'by_partition':dict(Counter(r['split'] for r in entries)),
        'by_stratum':dict(Counter(r['split']+':'+r['group'] for r in entries)),
        'formal_eligible':False,'clinical_qualified':None,'model_calls':0,'source_hashes_unchanged':True}
    write(output/'summary.json',summary);return summary


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    for name in ('source-folder','supplement','active-manifest','formulary-path','secret-path','output'):
        p.add_argument('--'+name,type=Path,required=True)
    a=p.parse_args();print(json.dumps(prepare(a.source_folder,a.supplement,a.active_manifest,a.formulary_path,a.secret_path,a.output)))

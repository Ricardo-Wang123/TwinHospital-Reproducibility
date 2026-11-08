"""Extract time-bounded source history for already selected patients, locally.

The cache does not change frozen runtime cases. It preserves raw eMAR event
status, dose-component multiplicity and microbiology availability timestamps.
No result is interpreted as a treatment recommendation or current observation.
"""
from collections import Counter
from datetime import datetime, time
import argparse
import csv
import hashlib
import json
import os
from pathlib import Path
import sqlite3

from .mimic_reader import _Source
from .mimic_cohort import save

VERSION='source_supplemental_history_v1'
TABLES=('microbiologyevents','omr','emar','emar_detail')
REFERENCES=[f'https://mimic.mit.edu/docs/iv/modules/hosp/{t}.html' for t in TABLES]
POLICY={'version':VERSION,
 'selection':'Only the supplied, already-selected patients and anchor admissions; no model outcomes read.',
 'cutoff':'Selected source admission discharge, not the new simulated encounter. Every target anchor retains its own cutoff.',
 'cache':'Union per patient through latest selected cutoff. Each per-anchor query MUST apply its own eligible_through timestamp; cache union is not an agent view.',
 'date_only':'Date upper bound 23:59:59.999999 must precede cutoff; no invented sampling time.',
 'microbiology':'Require sampling and last-result availability by cutoff. Missing availability excluded; null organisms not interpreted as a normal/current test.',
 'emar':'Require both chart and documentation time by cutoff; preserve event_txt (including Not Given). Event count is not treatment duration.',
 'emar_detail':'Only join to retained parent eMAR by subject_id and emar_id; preserve every ordinal/component. No sum of parent dose and component dose.',
 'omr':'Patient-linked, date-only historical observations; no inferred same-admission link or present-time measurement. Documentation availability timestamp is not supplied.',
 'privacy':'Raw IDs, dates and rows stay in private cache, never in public report.',
 'runtime_cases_changed':False,'clinical_quality_inferred':False,'references':REFERENCES}


def parsed(value):
    try:return datetime.fromisoformat(value) if value else None
    except (ValueError,TypeError):return None


def time_upper(row, exact, date):
    value=parsed(row.get(exact)) if exact else None
    if value is not None:return value
    day=parsed(row.get(date)) if date else None
    return datetime.combine(day.date(),time.max) if day is not None else None


def eligibility(table,row,cutoff):
    """Return conservative availability bound and reason; no current-state inference."""
    if table=='omr':
        event=time_upper(row,None,'chartdate')
        bound=event
    elif table=='microbiologyevents':
        event=time_upper(row,'charttime','chartdate')
        available=time_upper(row,'storetime','storedate')
        if available is None:return None,'missing_availability'
        bound=max(event,available) if event is not None else None
    elif table=='emar':
        event=parsed(row.get('charttime'));available=parsed(row.get('storetime'))
        if available is None:return None,'missing_availability'
        bound=max(event,available) if event is not None else None
    else:raise ValueError('Use the parent join for eMAR detail')
    if bound is None:return None,'missing_event_time'
    if bound>cutoff:return None,'after_anchor_cutoff'
    return bound,'retained'


def file_hash(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as f:
        for block in iter(lambda:f.read(8*1024*1024),b''):h.update(block)
    return h.hexdigest()


def filtered(source,table,subjects):
    with source._text(table) as stream:
        reader=csv.reader(stream);header=next(reader);i=header.index('subject_id')
        if len(header)!=len(set(header)):raise ValueError('Duplicate source column')
        for n,values in enumerate(reader,2):
            if len(values)!=len(header):raise ValueError('Malformed supplemental source row')
            if values[i] in subjects:yield n,dict(zip(header,values))


def extract(source_path,selection_paths,output,expected_source_sha256):
    os.umask(0o077)
    source_path,output=Path(source_path),Path(output)
    if output.exists():raise FileExistsError('Keep prior extraction versions')
    selections=[];selection_hashes={}
    for path in map(Path,selection_paths):
        selection_hashes[str(path.resolve())]=file_hash(path)
        rows=json.loads(path.read_text())
        if not isinstance(rows,list):raise ValueError('Selections must be explicit private row lists')
        for row in rows:
            if any(not isinstance(row.get(k),str) or not row[k] for k in ('subject_id','hadm_id','group','split')):
                raise ValueError('Selection identity incomplete')
            selections.append({k:row[k] for k in ('subject_id','hadm_id','group','split')})
    targets={}
    partitions={}
    for row in selections:
        sid=row['subject_id']
        if sid in partitions and partitions[sid]!=row['split']:raise ValueError('Patient crosses partitions')
        partitions[sid]=row['split'];key=(sid,row['hadm_id'])
        if key in targets and targets[key]!=row:raise ValueError('Conflicting target assignment')
        targets[key]=row
    output.mkdir(parents=True,mode=0o700)
    actual_hash=file_hash(source_path)
    if actual_hash!=expected_source_sha256:raise ValueError('Source archive identity differs')
    save(output/'protocol_frozen_before_extraction.json',{'policy':POLICY,
        'source_sha256':actual_hash,'selection_sha256':selection_hashes,'code_sha256':file_hash(__file__)})
    with _Source(source_path) as source:
        for _,row in source.rows('admissions'):
            key=(row['subject_id'],row['hadm_id'])
            if key in targets:
                cutoff=parsed(row['dischtime'])
                if cutoff is None:raise ValueError('Target discharge missing')
                targets[key]['cutoff']=cutoff.isoformat()
        if any('cutoff' not in t for t in targets.values()):raise ValueError('Target admission not found')
        latest={}
        for t in targets.values():latest[t['subject_id']]=max(latest.get(t['subject_id'],datetime.min),parsed(t['cutoff']))
        save(output/'target_anchors_private.json',{'targets':list(targets.values()),'union_requires_per_anchor_filter':True})
        db=sqlite3.connect(output/'source_rows.sqlite')
        db.execute('CREATE TABLE records (table_name TEXT, csv_row INTEGER, subject_id TEXT, eligible_through TEXT, emar_id TEXT, payload TEXT, PRIMARY KEY(table_name,csv_row))')
        db.execute('CREATE TABLE parent_emar (emar_id TEXT PRIMARY KEY, subject_id TEXT, eligible_through TEXT)')
        counts={};parent={}
        for table in TABLES:
            stats=Counter();buffer=[];parents=[]
            for rownum,row in filtered(source,table,latest):
                stats['selected_subject_rows']+=1;sid=row['subject_id']
                if table=='emar_detail':
                    found=parent.get(row.get('emar_id'))
                    if not found or found[0]!=sid:
                        stats['no_time_eligible_parent']+=1;continue
                    bound=found[1];reason='retained'
                else:bound,reason=eligibility(table,row,latest[sid])
                stats[reason]+=1
                if bound is None:continue
                raw=json.dumps(row,ensure_ascii=False,separators=(',',':'))
                buffer.append((table,rownum,sid,bound.isoformat(),row.get('emar_id'),raw))
                if table=='emar':
                    eid=row['emar_id']
                    if eid in parent:raise ValueError('Duplicate parent eMAR identity')
                    parent[eid]=(sid,bound);parents.append((eid,sid,bound.isoformat()))
                if len(buffer)>=3000:
                    db.executemany('INSERT INTO records VALUES(?,?,?,?,?,?)',buffer);buffer=[]
                    if parents:db.executemany('INSERT INTO parent_emar VALUES(?,?,?)',parents);parents=[]
                    db.commit()
            if buffer:db.executemany('INSERT INTO records VALUES(?,?,?,?,?,?)',buffer)
            if parents:db.executemany('INSERT INTO parent_emar VALUES(?,?,?)',parents)
            db.commit();counts[table]=dict(stats)
            save(output/'progress.json',{'status':'extracting','finished_tables':list(counts),'counts':counts})
            print(json.dumps({'table_finished':table,'counts':dict(stats)}),flush=True)
        db.execute('CREATE INDEX subject_time ON records(subject_id,eligible_through,table_name)');db.commit()
        coverage=Counter();per_target=[]
        for n,t in enumerate(targets.values(),1):
            available=dict(db.execute('SELECT table_name,COUNT(*) FROM records WHERE subject_id=? AND eligible_through<=? GROUP BY table_name',(t['subject_id'],t['cutoff'])))
            per_target.append({**t,'available_rows':available})
            for table in available:coverage[table]+=1
        db.close()
    if any(file_hash(p)!=h for p,h in selection_hashes.items()):raise ValueError('Selection changed during extraction')
    save(output/'target_coverage_private.json',{'targets':per_target})
    summary={'version':VERSION,'status':'source_cache_complete_runtime_integration_pending',
        'distinct_source_patients':len(latest),'target_anchors':len(targets),'counts':counts,
        'anchors_with_time_eligible_rows':dict(coverage),'policy':POLICY,
        'source_sha256':actual_hash,'cache_sha256':file_hash(output/'source_rows.sqlite')}
    save(output/'summary.json',summary);return summary


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--source',type=Path,required=True);p.add_argument('--selection',action='append',type=Path,required=True)
    p.add_argument('--output',type=Path,required=True);p.add_argument('--source-identity',type=Path,required=True)
    a=p.parse_args();identity=json.loads(a.source_identity.read_text())
    result=extract(a.source,a.selection,a.output,identity['sha256'])
    print(json.dumps({k:v for k,v in result.items() if k!='policy'},ensure_ascii=False))

"""Source-only replacement for an unusable medication-route material link.

Preserves the original50, never reads model outcomes, and excludes all original
test patients and every patient in the original50 before deterministic ranking.
"""
from __future__ import annotations
import argparse
from collections import Counter, defaultdict
import csv
import hashlib
import json
from pathlib import Path
import sqlite3

from .mimic_reader import _Source
from .mimic_cohort import save, timestamp, rank, usable_order, progress
from .development_pilot50_v3 import window_ok
from .closed_followup_v3 import generate, GENERATION_POLICY
from .cohort_eligibility import DRUG_TERMS, number

VERSION = "development-pilot50-route-replacement-v1-20261107"


def projected_rows(source, table, subjects=None, keys=None):
    """Validate the whole CSV stream; construct dictionaries only for targets."""
    with source._text(table) as stream:
        reader=csv.reader(stream); header=next(reader)
        if not header or len(header)!=len(set(header)):raise ValueError("invalid source CSV header")
        si=header.index("subject_id") if subjects is not None or keys is not None else None
        hi=header.index("hadm_id") if keys is not None else None
        for n,values in enumerate(reader,2):
            if len(values)!=len(header):raise ValueError("malformed source CSV record")
            if subjects is not None and values[si] not in subjects:continue
            if keys is not None and (values[si],values[hi]) not in keys:continue
            yield n,dict(zip(header,values))


def adaptable(row, group):
    if not usable_order(row) or row.get("route") not in {"PO","IH","INH","NEB"}:return False
    name=row["drug"].casefold()
    if not any(t in name for t in DRUG_TERMS[group]):return False
    if group=="pneumonia" and "vancomycin" in name and row["route"]=="PO":return False
    dose,daily=number(row.get("dose_val_rx")),number(row.get("doses_per_24_hrs"))
    return dose is not None and dose>0 and daily is not None and daily==int(daily) and 1<=daily<=6 and bool(row.get("prod_strength"," ").strip())


def extract_fast(source_path,out,selections):
    database=out/"source_cache.sqlite"; ready=out/"source_cache_complete.json"
    if database.exists() and ready.exists():return database
    if database.exists():raise RuntimeError("incomplete source cache retained; inspect before replacement")
    selected={r["subject_id"]:r for r in selections}; coverage=Counter();excluded=Counter();buffer=[]
    db=sqlite3.connect(database);database.chmod(0o600)
    db.execute("CREATE TABLE rows(table_name TEXT, subject TEXT,payload TEXT)")
    db.execute("CREATE INDEX rows_by_subject ON rows(subject,table_name)")
    db.execute("CREATE TABLE dictionary(table_name TEXT,code TEXT,version TEXT,payload TEXT)")
    def put(table,subject,row):
        buffer.append((table,subject,json.dumps(row,ensure_ascii=False)));coverage[table]+=1
        if len(buffer)>=2000:
            db.executemany("INSERT INTO rows VALUES (?,?,?)",buffer);db.commit();buffer.clear()
    with _Source(source_path) as source:
        for _,r in projected_rows(source,"patients",subjects=selected):put("patients",r["subject_id"],r)
        admissions=defaultdict(list);anchors={}
        for _,r in projected_rows(source,"admissions",subjects=selected):
            admissions[r["subject_id"]].append(r)
            if r["hadm_id"]==selected[r["subject_id"]]["hadm_id"]:anchors[r["subject_id"]]=r
        eligible=set();cutoffs={}
        for sid,rows in admissions.items():
            anchor=anchors[sid];end=timestamp(anchor["dischtime"]);cutoffs[sid]=end
            for r in rows:
                start,stop=timestamp(r["admittime"]),timestamp(r["dischtime"])
                if start is None or stop is None or stop<start or start>timestamp(anchor["admittime"]) or stop>end:
                    excluded["admissions_outside_or_invalid_history"]+=1;continue
                eligible.add((sid,r["hadm_id"]));put("admissions",sid,r)
        for table in ("diagnoses_icd","prescriptions","pharmacy","labevents"):
            progress(out,"replacement_extracting_"+table,retained_rows=dict(coverage))
            for _,r in projected_rows(source,table,subjects=selected):
                sid=r["subject_id"]
                if (sid,r.get("hadm_id")) not in eligible:
                    excluded[table+"_unlinked_or_outside_history"]+=1;continue
                cutoff=cutoffs[sid]
                if table in {"prescriptions","pharmacy"}:
                    start,stop=timestamp(r.get("starttime")),timestamp(r.get("stoptime"))
                    if start is None or start>cutoff or (stop is not None and stop<start):
                        excluded[table+"_invalid_or_future_start"]+=1;continue
                elif table=="labevents":
                    chart,stored=timestamp(r.get("charttime")),timestamp(r.get("storetime"))
                    if chart is None or stored is None or stored<chart or stored>cutoff:
                        excluded["labevents_invalid_missing_or_future_availability"]+=1;continue
                put(table,sid,r)
        for table,code,ver in [("d_icd_diagnoses","icd_code","icd_version"),("d_labitems","itemid",None)]:
            db.executemany("INSERT INTO dictionary VALUES (?,?,?,?)",[(table,r[code],r[ver] if ver else "",json.dumps(r)) for _,r in projected_rows(source,table)])
        if buffer:db.executemany("INSERT INTO rows VALUES (?,?,?)",buffer)
        db.commit()
    db.close();save(ready,{"retained_rows":dict(coverage),"excluded_rows":dict(excluded),"include_labs":True,
        "omitted_tables":["emar","emar_detail","omr","ICU event tables","Note","ED"],"omission_means":"not loaded, not absent",
        "reader":"CSV projection before dictionary construction; all rows width validated; same original history and time fences"})
    return database


def run(source_path,private_root,base_folder,out):
    root,base,out=Path(private_root),Path(base_folder),Path(out)
    out.mkdir(parents=True,exist_ok=True,mode=0o700);out.chmod(0o700)
    if (out/"replacement_v1_manifest.json").exists():raise FileExistsError("replacement output already frozen")
    key=(root/"mimic_batch/identity.key").read_bytes()
    original=json.loads((root/"mimic_batch/selection_private.json").read_text())
    base_selection=json.loads((base/"selection_private.json").read_text())
    blocked={r["subject_id"] for r in original if r["split"]=="test"}|{r["subject_id"] for r in base_selection}
    records=json.loads((root/"diagnosis_scope_labs_v2/lab_flags_private.json").read_text())["records"]
    group="pneumonia"
    pool=[r for r in records if group in r["any_sequence"] and r["survives_next_day"] and group in r["group_parsed_rx"] and window_ok(r,group) and r["subject_id"] not in blocked]
    lookup={(r["subject_id"],r["hadm_id"]):r for r in pool}
    policy={"version":VERSION,"replaces":"one source-material candidate with no usable closed current medication view; original remains preserved",
        "excluded":"all original test patients and all original50 candidates", "route_rule":"MAIN PO/IH/INH/NEB; scalar dose>0; integer daily count1..6; nonempty strength; group keyword; oral vancomycin excluded for pneumonia",
        "rank":"minimum HMAC(version:patient:subject), then HMAC(version:anchor:hadm)","clinical_values_selected":False,
        "new_case_model_calls":0,"generation_policy":GENERATION_POLICY,
        "source_groups":"unchanged narrow original ICD families, any sequence", "lab_windows":"same descriptive v3 windows, no numeric result filtering",
        "base_manifest_sha256":hashlib.sha256((base/"manifest.json").read_bytes()).hexdigest(),
        "implementation_sha256":hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
    save(out/"protocol_frozen_before_selection.json",policy)
    valid=set();counts=Counter()
    with _Source(source_path) as source:
        progress(out,"replacement_prescription_route_screen",candidate_stays=len(pool),candidate_patients=len({r["subject_id"] for r in pool}))
        for _,r in projected_rows(source,"prescriptions",keys=lookup):
            if not adaptable(r,group):continue
            k=(r["subject_id"],r["hadm_id"]);end=timestamp(lookup[k]["dischtime"])
            start,stop=timestamp(r.get("starttime")),timestamp(r.get("stoptime"))
            if start is None or start>end or (stop is not None and stop<start):continue
            valid.add(k);counts["route_adaptable_prescription_rows"]+=1
    patients={k[0] for k in valid}
    if not patients:raise RuntimeError("no source-only route-adaptable replacement; no invented substitute")
    chosen=min(patients,key=lambda s:rank(key,VERSION+":patient:"+s))
    anchor=min((k for k in valid if k[0]==chosen),key=lambda k:rank(key,VERSION+":anchor:"+k[1]))
    selected=[{**lookup[anchor],"group":group,"system":"respiratory","split":"development","selection_version":VERSION}]
    save(out/"selection_private.json",selected)
    summary={"version":VERSION,"initial_source_stays_after_exclusion":len(pool),"initial_source_patients_after_exclusion":len({r["subject_id"] for r in pool}),
        "route_adaptable_stays":len(valid),"route_adaptable_patients":len(patients),"counts":dict(counts),
        "selected":1,"overlap_with_original50":0,"overlap_with_original_test2000":0,"model_outputs_read":False}
    save(out/"selection_summary.json",summary)
    extract_fast(source_path,out,selected)
    generate(out,key)
    base_manifest=json.loads((base/"manifest.json").read_text());replacement=json.loads((out/"manifest.json").read_text())
    original_ready=[r for r in base_manifest["cases"] if r["constructed_view_complete"]]
    if len(original_ready)!=49 or len(replacement["cases"])!=1 or not replacement["cases"][0]["constructed_view_complete"]:
        raise AssertionError("replacement view failed; preserve all source candidates")
    cases=original_ready+replacement["cases"]
    if len({r["patient_key"] for r in cases})!=50:raise AssertionError("patient uniqueness failed")
    combined={**base_manifest,"version":VERSION,"cases":cases,"case_count":50,
        "base_manifest":str(base/"manifest.json"),"replacement_manifest":str(out/"manifest.json"),
        "retained_not_ready_candidates":1,"replacement_reason":"source route-material applicability, not model results",
        "original_partition_preserved":True,"clinical_reference_status":"pending_independent_review"}
    save(out/"replacement_v1_manifest.json",combined)
    save(out/"replacement_input_freeze.json",{"version":VERSION,"files":[{"path":str(p),"sha256":hashlib.sha256(p.read_bytes()).hexdigest()} for p in
        [Path(r["path"]) for r in cases]+[out/"replacement_v1_manifest.json",out/"protocol_frozen_before_selection.json",base/"manifest.json"]]})
    summary.update({"combined_cases":50,"by_group":dict(Counter(r["group"] for r in cases)),"unique_patients":50})
    save(out/"replacement_summary.json",summary)
    return summary


if __name__=="__main__":
    p=argparse.ArgumentParser(description=__doc__)
    for x in ("source","private-root","base-folder","out"):p.add_argument("--"+x,required=True)
    a=p.parse_args();print(json.dumps(run(a.source,a.private_root,a.base_folder,a.out),ensure_ascii=False))

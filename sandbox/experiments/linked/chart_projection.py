"""Compact reversible clinical views of already-authorized HistoryChart pages.

This module does not select records, infer a diagnosis, estimate renal function,
or change the history budget. Raw pages remain the local auditable source. Only
duplicate source-pointer fields are omitted from the model view; every clinical
field can be restored exactly. Shared defaults are explicit, never implicit.
"""
from __future__ import annotations
from copy import deepcopy
import hashlib
import json
import math

from .chart import (json_bytes, PAGE_BYTE_LIMIT, _ORDER, _OBS, _DIAGNOSIS,
                    _ENCOUNTER, _ITEM, _STATUS, _DEMOGRAPHICS, _TIME_BASIS, _ADMIN, _MICRO)

VERSION = "clinical_field_projection_v2_supplemental_history"
READABLE_VERSIONS = frozenset({"clinical_field_projection_v1", VERSION})


def supported_page(page):
    version=page.get('projection_version')
    return version in READABLE_VERSIONS and not (version=='clinical_field_projection_v1'
        and page.get('section') in {'historical_administrations','historical_microbiology','historical_outpatient_measurements'})
LOCAL_POINTER_FIELDS = frozenset({"source_ref", "dictionary_source_ref"})
COMMON_PATHS = (
    ("source_encounter_ref",), ("evidence_type",), ("status",), ("source_kind",),
    ("prescription_component_type_raw",),
    ("time", "basis"), ("time", "availability_basis"),
    ("time", "event_time_known"), ("time", "available_by_source_cutoff"),
    ("normalization",),
)
SCHEMAS = {
    "historical_orders": _ORDER, "historical_observations": _OBS,
    "current_observations": _OBS, "diagnoses": _DIAGNOSIS,
    "historical_administrations": _ADMIN, "historical_microbiology": _MICRO,
    "historical_outpatient_measurements": _OBS,
    "historical_encounters": _ENCOUNTER,
    "encounter_headers": {**_ENCOUNTER, "component_counts": {"diagnoses":None,"order_refs":None,"observation_refs":None}},
    "allergy_record": _STATUS, "current_use_record": _STATUS,
    "allergy_items": _ITEM, "current_use_items": _ITEM,
    "demographics": _DEMOGRAPHICS, "time_basis": _TIME_BASIS,
    "encounter_order_refs": {"record_id":None,"source_encounter_ref":None},
    "encounter_observation_refs": {"record_id":None,"source_encounter_ref":None},
}
_MISSING = object()


class ProjectionBudgetExceeded(ValueError):
    """Valid clinical evidence needs a narrower page; no evidence was dropped."""
    def __init__(self,projected_bytes,limit=PAGE_BYTE_LIMIT):
        self.projected_bytes=projected_bytes;self.limit=limit
        super().__init__('Compact page exceeds byte budget; narrower query required, no record dropped')


def _schema_fields(schema,prefix=''):
    result=[]
    for key,value in schema.items():
        if key in LOCAL_POINTER_FIELDS:continue
        name=prefix+key
        if isinstance(value,dict):result.extend(_schema_fields(value,name+'.'))
        elif isinstance(value,list) and isinstance(value[0],dict):result.extend(_schema_fields(value[0],name+'[].'))
        else:result.append(name)
    return sorted(result)


PROJECTION_POLICY = {
    'version':VERSION,
    'input':'authorized HistoryChart page only; no evaluator, patient hidden view or provenance access',
    'source_references':{'records[].record_ref':'unchanged stable HistoryChart reference',
        'record_id':'unchanged source clinical event reference where supplied',
        'source_page_sha256':'canonical SHA256 of complete original authorized page'},
    'preserved_clinical_fields_by_section':{section:_schema_fields(schema) for section,schema in SCHEMAS.items()},
    'omitted_local_pointer_aliases':sorted(LOCAL_POINTER_FIELDS),
    'record_defaults':'Exact shared fields are factored once; expand_projected_record restores every clinical record before mechanical evidence extraction.',
    'source_semantics':'Unchanged historical/current status, source_kind, event and availability time, dose/route/frequency/planned stop, specimen/value/unit/flag/ranges and all diagnoses.',
    'derived_time_display':'Arithmetic display only; never a new clinical observation, event, current lab or eGFR estimate.',
    'missingness':'Absent and null remain distinct; unknown is never converted into normal or negative.',
    'raw_evidence':'Retained locally by the caller; stable reference and source page hash resolve original complete page.',
    'source_record_selection_changed':False,'clinical_values_changed':False,'context_budget_changed':False,
    'duplicate_records':'Only identical stable refs plus expanded clinical fields and time display can share an already-visible copy. Every query and raw page remains auditable. Conflicting same-ref content raises an explicit error.',
}


def sha256_json(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _validate(value, schema):
    if isinstance(schema, dict):
        if not isinstance(value, dict) or set(value)-set(schema):
            raise ValueError("Unknown field or non-object in clinical projection")
        for k,v in value.items(): _validate(v,schema[k])
    elif isinstance(schema,list):
        if not isinstance(value,list):raise ValueError("Expected clinical list")
        for v in value:_validate(v,schema[0])
    elif isinstance(value,(dict,list)):
        raise ValueError("Unexpected nested content in clinical field")


def clinical_record(record):
    """Remove only local source-address aliases; preserve record_id and values."""
    if isinstance(record,dict):return {k:clinical_record(v) for k,v in record.items() if k not in LOCAL_POINTER_FIELDS}
    if isinstance(record,list):return [clinical_record(v) for v in record]
    return deepcopy(record)


def _get(obj,path):
    for key in path:
        if not isinstance(obj,dict) or key not in obj:return _MISSING
        obj=obj[key]
    return obj


def _set(obj,path,value):
    for key in path[:-1]:obj=obj.setdefault(key,{})
    obj[path[-1]]=deepcopy(value)


def _remove(obj,path):
    for key in path[:-1]:obj=obj[key]
    del obj[path[-1]]


def _merge(defaults,record):
    result=deepcopy(defaults)
    for k,v in record.items():
        result[k]=_merge(result.get(k,{}),v) if isinstance(v,dict) else deepcopy(v)
    return result


def _relative_time(record,time_basis):
    t=record.get('time',{})
    event=t.get('event_offset_hours');visit=time_basis.get('new_visit_offset_hours')
    if (t.get('basis')=='source_relative_to_anchor_discharge' and
        time_basis.get('history_offset_origin')=='source_anchor_discharge' and
        t.get('event_time_known') is True and type(event) in (int,float) and
        type(visit) in (int,float) and math.isfinite(event) and math.isfinite(visit)):
        return {'event_hours_before_new_visit':round(visit-event,6)}
    return None


def project_history_page(raw_page):
    section=raw_page.get('section')
    if section not in SCHEMAS or not isinstance(raw_page.get('records'),list):
        raise ValueError('Expected a supported authorized HistoryChart page')
    rows=[];seen=set()
    for item in raw_page['records']:
        if not isinstance(item,dict) or set(item)!={'record_ref','record'}:
            raise ValueError('Expected stable raw record wrapper')
        ref=item['record_ref']
        if not isinstance(ref,str) or not ref.startswith('chart_') or ref in seen:
            raise ValueError('Invalid or duplicate stable record reference')
        seen.add(ref);_validate(item['record'],SCHEMAS[section]);json_bytes(item['record'])
        row={'record_ref':ref,'record':clinical_record(item['record'])}
        relative=_relative_time(item['record'],raw_page.get('time_basis',{}))
        if relative:row['derived_time_display']=relative
        rows.append(row)
    defaults={}
    for path in COMMON_PATHS:
        vals=[_get(r['record'],path) for r in rows]
        if len(vals)>=2 and vals[0] is not _MISSING and all(v is not _MISSING and v==vals[0] for v in vals):
            _set(defaults,path,vals[0])
            for r in rows:_remove(r['record'],path)
    output={
        'projection_version':VERSION,'source_page_sha256':sha256_json(raw_page),
        'retained_field_policy_id':VERSION,
        'section':section,'time_basis':deepcopy(raw_page.get('time_basis',{})),
        'record_defaults':defaults,
        'record_defaults_apply_to':'Every record inherits these exact fields; row fields override. Missing stays missing.',
        'clinical_interpretation':'Historical orders are not current use/administered doses. Unknown/unshown is not absent. Historical labs remain historical.',
        'derived_time_rule':'event_hours_before_new_visit = new_visit_offset_hours - original event_offset_hours; only when both share the known discharge origin.',
        'local_raw_verification':'Stable record_ref resolves raw evidence locally; source_page_sha256 verifies full page.',
        'local_pointer_fields_omitted':sorted(LOCAL_POINTER_FIELDS),
        'records':rows,
    }
    for k in ('total_count','matching_count','returned_count','next_offset','unexpanded_matching_count','requires_expansion','withheld_unavailable_records'):
        if k in raw_page:output[k]=deepcopy(raw_page[k])
    output['selection']={k:deepcopy(v) for k,v in raw_page.get('selection',{}).items() if k in {
        'encounter_key','term','offset','limit','matching','ordering','time_order','equal_time_order','missing_time_order'}}
    if json_bytes(output)>PAGE_BYTE_LIMIT:
        raise ProjectionBudgetExceeded(json_bytes(output))
    return output


def expand_projected_record(page,row):
    if not supported_page(page):raise ValueError('Unknown projection version or section')
    return _merge(page.get('record_defaults',{}),row['record'])


def _visible_record_index(pages):
    index={}
    for entry in pages:
        page=entry.get('page',entry)
        if not supported_page(page):raise ValueError('Expected supported projected existing pages')
        for row in page['records']:
            evidence=(expand_projected_record(page,row),deepcopy(row.get('derived_time_display')))
            if row['record_ref'] in index and index[row['record_ref']]!=evidence:
                raise ValueError('Conflicting stable clinical reference in visible history')
            index[row['record_ref']]=evidence
    return index


def deduplicate_projected_page(page, existing_pages):
    """Share identical already-visible records; do not evict or select evidence.

    The caller first verifies the complete projection against its raw page.
    Empty and expansion-only pages are not treated as already-visible records.
    """
    if not supported_page(page):raise ValueError('Expected supported projected query page')
    if 'query_record_refs' in page:raise ValueError('Page has already been deduplicated')
    prior=_visible_record_index(existing_pages);out=deepcopy(page);kept=[];old=[]
    for row in page['records']:
        ref=row['record_ref'];evidence=(expand_projected_record(page,row),row.get('derived_time_display'))
        if ref in prior:
            if prior[ref]!=evidence:raise ValueError('Conflicting stable clinical reference; update cannot be discarded')
            old.append(ref)
        else:kept.append(deepcopy(row))
    if old:
        out['query_record_refs']=[r['record_ref'] for r in page['records']]
        out['query_returned_count']=page.get('returned_count',len(page['records']))
        out['already_visible_record_refs']=old
        out['records']=kept;out['returned_count']=len(kept)
        out['deduplication']='Identical references resolve to records retained in earlier visible pages; no source record is absent or discarded.'
    return {'page':out,'already_visible_record_refs':old,
        'new_record_refs':[r['record_ref'] for r in kept],
        'all_records_already_visible':bool(page['records']) and not kept and not page.get('requires_expansion')}


def verify_projection(raw_page, projected, existing_pages=()):
    if not supported_page(projected) or projected.get('section')!=raw_page.get('section'):return False
    if projected.get('source_page_sha256')!=sha256_json(raw_page):return False
    if projected.get('time_basis')!=raw_page.get('time_basis',{}):return False
    for key in ('total_count','matching_count','next_offset','unexpanded_matching_count','requires_expansion','withheld_unavailable_records'):
        if projected.get(key,_MISSING)!=raw_page.get(key,_MISSING):return False
    if [x['record_ref'] for x in raw_page['records']] != projected.get('query_record_refs',[x['record_ref'] for x in projected['records']]):return False
    existing=_visible_record_index(existing_pages)
    for row in projected['records']:
        existing[row['record_ref']]=(expand_projected_record(projected,row),row.get('derived_time_display'))
    return all(a['record_ref'] in existing and clinical_record(a['record'])==existing[a['record_ref']][0]
               and _relative_time(a['record'],raw_page.get('time_basis',{}))==existing[a['record_ref']][1]
               for a in raw_page['records'])

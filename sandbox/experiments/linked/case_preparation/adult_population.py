"""Record a source-supported age category without inventing an exact age."""
from copy import deepcopy
import math

from ..cases import validate_case
from ..contracts import digest

VERSION = 'mimic22_adult_population_v1'
SOURCE = 'https://www.nature.com/articles/s41597-022-01899-x'
POPULATION = {
    'category':'adult', 'minimum_age_years':18,
    'basis':'MIMIC-IV 2.2 excludes patients younger than 18 on their first visit; this constructed visit occurs after the source admission/discharge.',
    'source_url':SOURCE, 'source_kind':'source_cohort_metadata',
    'exact_age_claim':False,
}


def add_adult_population(case):
    if case.get('data_kind') != 'mimic_derived' or case.get('provenance',{}).get('source_dataset') != 'MIMIC-IV 2.2':
        raise ValueError('This overlay requires a verified MIMIC-IV 2.2 source package')
    history=case['his_initial'];clock=history.get('time_basis',{})
    offset=clock.get('new_visit_offset_hours')
    if (clock.get('history_offset_origin') != 'source_anchor_discharge'
        or type(offset) not in (int,float) or not math.isfinite(offset) or offset < 0):
        raise ValueError('Adult cohort status cannot be projected to an unknown or earlier visit')
    age=history.get('demographics',{}).get('age')
    if type(age) in (int,float) and (not math.isfinite(age) or age<18):
        raise ValueError('Age conflicts with the source adult cohort; inspect instead of overriding it')
    result=deepcopy(case)
    result['case_id'] += '_adult1'
    result['his_initial']['demographics']['age_population']=deepcopy(POPULATION)
    result['provenance']['adult_population_overlay']={
        'version':VERSION,'parent_case_id':case['case_id'],'parent_case_hash':digest(case),
        'source_reference':SOURCE,'changed_fields':['his_initial.demographics.age_population'],
        'source_history_changed':False,'exact_age_added':False,'new_clinical_measurements':0}
    validate_case(result)
    return result

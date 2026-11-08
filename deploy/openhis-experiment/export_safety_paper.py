#!/usr/bin/env python3
"""Export aggregate-only appendix evidence, preserving private event logs."""
import argparse,csv,hashlib,json
from pathlib import Path

def main():
 ap=argparse.ArgumentParser();ap.add_argument('run_directory');a=ap.parse_args();run=Path(a.run_directory)
 summary=json.loads((run/'summary.json').read_text());source=run/'outcomes.jsonl'
 rows=[json.loads(x) for x in source.read_text().splitlines()]
 if len(rows)!=summary['executed_units']:raise RuntimeError('Summary denominator mismatch')
 public=run/'public';public.mkdir(exist_ok=True)
 cells=[]
 for category,conditions in summary['by_category'].items():
  for condition,counts in conditions.items():
   observed=sum(r['native_observation_complete'] for r in rows if r['category']==category and r['is_error']==(condition=='errors'))
   for outcome,n in counts.items():cells.append({'category':category,'condition':condition,'outcome':outcome,'count':n,'condition_total':sum(counts.values()),'condition_fresh_readback_count':observed})
 with (public/'local_safety_counts.csv').open('w',newline='') as f:
  writer=csv.DictWriter(f,fieldnames=list(cells[0]));writer.writeheader();writer.writerows(cells)
 out={k:summary[k] for k in ('protocol_version','protocol_status','definitions','executed_units','by_category','all_units_observed','prohibited_effect_units','implementation_sha256')}
 out['public_study_id']='APP-III-LOCAL-001'
 out.update(unit='independent constructed local fault fixture, not an intact MAS patient encounter',data_kind='independent_fictional',model_calls=0,errors_per_category=summary['errors_per_category'],controls_per_category=summary['controls_per_category'],original_outcomes_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
  limitations=['Gateway plus isolated native HIS were tested together; no claim of native-only protection.',
   'Staff identity is authenticated with native login and checked against runtime authorization policy; native signedById is absent in this build and remains unknown.',
   'Case, queue and signatory linkage protections are persistent runtime sidecars, not native hospital features.',
   'Five fixed fault families do not exhaust all hazards. These results do not establish clinical safety or real-world effectiveness.',
   'No model-driven attacks or diagnostic/treatment validation; intact MAS outcomes have a different denominator.'])
 dependency=run/'implementation_dependency_supplement.json'
 if dependency.exists():
  extra_dependency=json.loads(dependency.read_text())
  out['implementation_dependency_supplement']={
   'supplemental_dependency_sha256':extra_dependency['supplemental_dependency_sha256'],
   'original_manifest_changed':extra_dependency['original_manifest_changed']}
  out['version_note']='The local suite used the frozen native adapter before the later explicit frequency alias / tiotropium microgram serialization fix. The same protocol gateway is integrated in the complete MAS. Frozen source hashes, rather than the later working tree, identify this run.'
 supplement=run/'supplemental_payment_readback.json'
 if supplement.exists():
  extra=json.loads(supplement.read_text());out['payment_detail_supplement']={k:extra[k] for k in ('n_units','confirmed','unknown_or_mismatch','read_only_transaction','original_results_changed','original_observation_window_established')}
 (public/'appendix_evidence_summary.json').write_text(json.dumps(out,ensure_ascii=False,indent=2)+'\n')
 print(json.dumps({'aggregate_only':True,'executed_units':len(rows),'exported_patient_identifiers':False}))
if __name__=='__main__':main()

#!/usr/bin/env python3
"""Pure local exporter; raw native rows never go in the public report."""
import argparse,json,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from sandbox.experiments.linked.native_observation_analysis import audit_folder
if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--private-capture-dir',type=Path,required=True);p.add_argument('--public-report',type=Path,required=True);a=p.parse_args()
    r=audit_folder(a.private_capture_dir,a.public_report)
    print(json.dumps({k:r[k] for k in ('committed_mutation_count','unknown_attribution_mutations','observed_violation_count','whole_system_safe')}))

"""Replay artifacts through a mixed FHIR/WS/T-order diagnostic path.

This is an engineering check of real HIS effects, not a new clinical MAS result.
It does not constitute either independent protocol arm in the paper comparison.
All source cases, traces and outputs remain in the authorized local environment.
"""
import argparse
from hashlib import sha256
import json
from pathlib import Path
import secrets

from sandbox.experiments.linked.fhir_his import fhir_workflow
from sandbox.experiments.linked.native_his import NativeOpenHISPort, write_private
from sandbox.experiments.linked.protocol_his import PaperProtocolPort
from sandbox.experiments.linked.wst846_server import wst_order_workflow


def read(path):
    return json.loads(path.read_bytes())


def replay(args):
    output = args.output.resolve()
    repository = Path(__file__).resolve().parents[1]
    if output.exists() or output.is_relative_to(repository):
        raise ValueError('Use a new private output directory outside the source checkout')
    case, source = read(args.case_file), read(args.trace_file)
    if source['case_id'] != case['case_id'] or source.get('native_complete') is not True:
        raise ValueError('A completed recorded trace must match the selected source case')
    output.mkdir(parents=True, mode=0o700)
    backend = PaperProtocolPort(NativeOpenHISPort(output/'native',
        catalog_mapping=read(args.catalog), diagnosis_mapping=read(args.diagnosis_catalog),
        credentials_path=args.credentials, base_url=args.his_url))
    run_id = 'transport-probe-'+secrets.token_hex(12)
    ctx = {'case_id':case['case_id'], 'run_id':run_id, 'role':'cashier', 'actor_id':'cashier-1'}
    log = []
    provenance = {'scope':'recorded_artifact_transport_replay', 'counts_as_new_clinical_run':False,
        'clinical_model_calls':0, 'source_case_sha256':sha256(args.case_file.read_bytes()).hexdigest(),
        'source_trace_sha256':sha256(args.trace_file.read_bytes()).hexdigest(),
        'case_id':case['case_id'], 'run_id':run_id}
    try:
        with fhir_workflow(backend, case_id=case['case_id'], run_id=run_id) as fhir:
            with wst_order_workflow(fhir, case_id=case['case_id'], run_id=run_id) as his:
                def execute(op, payload, role):
                    response = his.execute(op, payload, {**ctx,'role':role,'actor_id':role+'-1'})
                    log.append({'operation':op,'readback_verified':response.get('readback_verified'),
                                'status':response.get('status')})
                    write_private(output/'progress.json', {**provenance,'operations':log})
                    return response['data']

                registration = execute('registration.create', {'patient_key':case['patient_key'],
                    'demographics':case['his_initial']['demographics']}, 'cashier')
                ctx.update(patient_id=registration['patient_id'],encounter_id=registration['encounter_id'])
                execute('queue.enter', {'encounter_id':ctx['encounter_id']}, 'cashier')
                personnel = execute('personnel.registry', {}, 'doctor')
                execute('encounter.begin_consultation', {}, 'doctor')
                execute('consultation.save', {'artifact_id':'probe-consultation',
                    'content':source['artifacts']['consultation'],
                    'transcript':source['artifacts']['transcript']}, 'doctor')
                order_id = 'probe-order-'+secrets.token_hex(8)
                execute('order.save', {'order_id':order_id,
                    'items':source['artifacts']['final_prescription']['items']}, 'doctor')
                execute('order.attest', {'order_id':order_id,
                    'signatory_id':personnel['signatory_id']}, 'doctor')
                note = execute('note.save', {'note_id':'probe-note','expected_version':0,
                    'content':source['artifacts']['attested_note']['content']}, 'doctor')
                execute('note.attest', {'note_id':'probe-note','version':note['version']}, 'doctor')
                bill = execute('payment.prepare', {'order_id':order_id}, 'cashier')
                paid = execute('payment.commit', {'bill_id':bill['bill_id'],'amount':bill['amount'],
                    'currency':bill['currency'],'channel':'CASH',
                    'transaction_id':'probe-cash-'+secrets.token_hex(8)}, 'cashier')
                result = {**provenance,'status':'paid','operations':log,
                    'wst_exchanges':his.exchanges,'final_payment_status':paid['status'],
                    'native_snapshot':backend.snapshot(case['case_id'])}
                write_private(output/'result.private.json', result)
                return {key:result[key] for key in ['scope','status','counts_as_new_clinical_run',
                    'clinical_model_calls','operations','wst_exchanges','final_payment_status']}
    except Exception as exc:
        write_private(output/'failure.json', {**provenance,'error_type':type(exc).__name__,
            'error':str(exc),'completed_operations':log})
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ['case-file','trace-file','catalog','diagnosis-catalog','credentials','output']:
        parser.add_argument('--'+name, required=True, type=Path)
    parser.add_argument('--his-url', required=True)
    args = parser.parse_args()
    print(json.dumps(replay(args), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()

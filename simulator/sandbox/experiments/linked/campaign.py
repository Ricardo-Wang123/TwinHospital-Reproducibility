"""Admission windows for an outpatient batch."""
from copy import deepcopy
from datetime import datetime, timezone

from .contracts import digest
from .workflow_analysis import validate_policy


def validate_window(policy, *, workers=None, now=None, scenario_design=None):
    now = now or datetime.now(timezone.utc)
    run = {'started_at': now.isoformat(), 'workflow_policy_hash': digest(policy)}
    valid, errors = validate_policy({'workflow_observation_policy': policy,
                                     'scenario_design': scenario_design or policy.get('scenario_design')}, run)
    if errors:
        raise ValueError('invalid prospective workflow policy: ' + ', '.join(errors))
    if workers is not None and valid['workers'] != workers:
        raise ValueError('worker count differs from the frozen observation policy')
    if valid.get('admission_policy') != 'closed_loop_worker_availability':
        raise ValueError('unsupported prospective admission policy')
    opened = datetime.fromisoformat(valid['submission_window']['opens_at'].replace('Z', '+00:00'))
    frozen = datetime.fromisoformat(valid['frozen_at'].replace('Z', '+00:00'))
    if frozen > opened:
        raise ValueError('policy must be frozen before its submission window opens')
    if valid.get('late_outcomes') != 'retain_effects_and_final_disposition_exclude_late_completion':
        raise ValueError('late effects and final outcomes must remain observable')
    return deepcopy(valid)


def admission_status(policy, *, now=None):
    if policy is None:
        return 'open'
    now = now or datetime.now(timezone.utc)
    window = policy['submission_window']
    if now < datetime.fromisoformat(window['opens_at'].replace('Z', '+00:00')):
        return 'not_open'
    if now >= datetime.fromisoformat(window['closes_at'].replace('Z', '+00:00')):
        return 'closed'
    return 'open'


def unadmitted_record(config_id, reason):
    return {'status': 'not_run_submission_window', 'config_id': config_id,
            'stop_reason': reason, 'native_effects_started': False, 'model_requests_sent': False,
            'retained_in_planned_denominator': True}

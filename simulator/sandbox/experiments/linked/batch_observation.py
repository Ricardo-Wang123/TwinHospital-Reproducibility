"""Optional native-effect capture for a new engineering batch.

This never promotes a workflow to safe or clinically valid. Unresolved writes
leave the instance quarantined; automatic restart cannot erase that uncertainty.
"""
from contextlib import contextmanager
from pathlib import Path
import json

from .native_observation import CaptureSession, IsolatedObservationDB, ObservationUnknown, filehash
from .native_his import write_private, digest


def deployment_metadata(folder, workflow_policy=None):
    folder=Path(folder).resolve()
    names=('manage.py','compose.yaml','observation-v1.sql')
    hashes={name:filehash(folder/name) for name in names}
    if any(value is None for value in hashes.values()):
        raise ValueError('An explicit complete isolated observation deployment is required')
    value = {'version':'batch_native_capture_v2', 'source_hashes':hashes,
        'declared_native_tables_only':True, 'native_write_concurrency':1,
        'separate_gateway_evidence':True, 'formal_window_frozen':False,
        'exclusive_experiment_instance_required':True,
        'raw_capture':'private_batch_directory', 'whole_system_safe':None}
    if workflow_policy is not None:
        from .campaign import validate_window
        validate_window(workflow_policy)
        value.update(formal_window_frozen=True,workflow_policy_hash=digest(workflow_policy))
    return value


@contextmanager
def observed_batch(args, batch_root):
    if not getattr(args,'capture_native',False):
        yield None
        return
    metadata=deployment_metadata(args.observation_deploy,getattr(args,'workflow_policy_data',None))
    if metadata != args.observation_metadata:
        raise ValueError('Observation deployment changed after preflight')
    capture=CaptureSession(batch_root/'native_observation',
        db=IsolatedObservationDB(args.observation_deploy))
    capture.start(policy={'batch':args.batch, 'engineering_only':not getattr(args,'formal_experiment',False), **metadata})
    args.active_native_capture=capture
    try:
        yield capture
    finally:
        # close() refuses unresolved requests and preserves persistent quarantine.
        # Do not clear it just because a batch's worker has returned an error.
        try:
            capture.close()
        except ObservationUnknown as exc:
            capture.log('batch_capture_left_unresolved',error_type=type(exc).__name__,
                automatic_quarantine_release=False,whole_system_safe=None)
            meta=json.loads(capture.meta_path.read_text())
            meta.update(status='quarantined_unresolved',whole_system_safe=None,
                formal_correct_completion=None,automatic_quarantine_release=False)
            write_private(capture.meta_path,meta)
        finally:
            args.active_native_capture=None


def capture_summary(batch_root):
    path=batch_root/'native_observation/observation_session.json'
    if not path.exists():return None
    meta=json.loads(path.read_text())
    return {'version':'engineering_batch_native_capture_v1', 'status':meta.get('status'),
        'coverage_unchanged':meta.get('coverage_unchanged'),
        'observed_event_count':meta.get('observed_event_count'),
        'whole_system_safe':None, 'formal_correct_completion':None}

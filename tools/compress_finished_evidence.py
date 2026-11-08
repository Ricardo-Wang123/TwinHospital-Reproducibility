"""Lossless APFS compression of finalized attempt files; paths and bytes stay intact.

Only indexed terminal attempts are eligible. Live journals, inputs, credentials,
and the active batch's global native capture are outside this tool's scope.
"""
from pathlib import Path
from datetime import datetime, timezone
import argparse
import fcntl
import hashlib
import json
import os
import stat
import subprocess
import time
import uuid

VERSION = 'finished_evidence_apfs_compression_v1'
TERMINAL = {'completed', 'failed', 'incomplete_clinical_decision', 'stopped_budget', 'stopped_provider'}


def sha(path):
    hasher = hashlib.sha256()
    with Path(path).open('rb') as handle:
        for block in iter(lambda: handle.read(1024*1024), b''):
            hasher.update(block)
    return hasher.hexdigest()


def identity(info):
    return info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns, stat.S_IMODE(info.st_mode), info.st_uid, info.st_gid


def compress_file(path):
    path = Path(path)
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
        raise ValueError('Only standalone regular evidence files may be compressed')
    compressed_flag = getattr(stat, 'UF_COMPRESSED', 0x20)
    if getattr(before, 'st_flags', 0) & compressed_flag:
        return {'status': 'already_compressed'}
    if before.st_size < 256*1024:
        return {'status': 'below_threshold'}
    if not Path('/usr/bin/ditto').exists():
        return {'status': 'filesystem_compression_unavailable'}
    original_hash = sha(path)
    temporary = path.with_name('.'+path.name+'.fscompress-'+uuid.uuid4().hex+'.tmp')
    backup = path.with_name('.'+path.name+'.fscompress-backup-'+uuid.uuid4().hex+'.tmp')
    replaced = False
    verified = False
    try:
        subprocess.run(['/usr/bin/ditto', '--hfsCompression', '--noclone', str(path), str(temporary)],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        target = temporary.stat()
        if sha(temporary) != original_hash:
            raise ValueError('Compressed copy does not preserve exact source bytes')
        if identity(path.stat()) != identity(before) or sha(path) != original_hash:
            raise ValueError('Source changed during compression; original was not replaced')
        if (stat.S_IMODE(target.st_mode), target.st_uid, target.st_gid) != (stat.S_IMODE(before.st_mode), before.st_uid, before.st_gid):
            raise ValueError('Private ownership or permissions were not preserved')
        if not getattr(target, 'st_flags', 0) & compressed_flag or target.st_blocks >= before.st_blocks:
            return {'status': 'no_allocation_saving'}
        with temporary.open('rb') as handle:
            os.fsync(handle.fileno())
        os.link(path, backup)
        os.replace(temporary, path)
        replaced = True
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        if sha(path) != original_hash:
            raise RuntimeError('Post-replacement byte verification failed')
        verified = True
        backup.unlink()
        return {'status': 'compressed', 'sha256': original_hash, 'logical_bytes': before.st_size,
                'allocated_bytes_before': before.st_blocks*512,
                'allocated_bytes_after': path.stat().st_blocks*512,
                'same_file_bytes': True, 'same_logical_path': True, 'mode': oct(stat.S_IMODE(before.st_mode))}
    finally:
        if backup.exists():
            if replaced and not verified:
                os.replace(backup, path)
            else:
                backup.unlink()
        if temporary.exists():
            temporary.unlink()


def final_attempts(batch):
    batch = Path(batch).resolve()
    for index_path in sorted(batch.glob('*/attempts.json')):
        index = json.loads(index_path.read_text())
        for record in index['attempts'].values():
            if record.get('status') not in TERMINAL or not record.get('result_path'):
                continue
            run_path = Path(record['result_path']).resolve()
            if not run_path.is_relative_to(batch) or run_path.name != 'run.json':
                raise ValueError('Indexed result escapes the batch or has an unexpected filename')
            run = json.loads(run_path.read_text())
            if run.get('status') != record['status'] or run.get('case_id') != index['case_id']:
                raise ValueError('Final index and run identity disagree')
            attempt = run_path.parents[2]
            if attempt.parent != index_path.parent:
                raise ValueError('Attempt directory does not match indexed source case')
            yield attempt


def batch_active(batch):
    with (batch/'batch.lock').open('r') as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    return False


def run(batch, receipt, follow=False):
    batch, receipt = Path(batch).resolve(), Path(receipt).resolve()
    receipt.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    seen = set(); files = saved = 0
    lock = batch/'.finished-evidence-compression.lock'
    with lock.open('w') as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        while True:
            # Check the writer before scanning so a closed batch always receives a final pass.
            active = follow and batch_active(batch)
            for attempt in final_attempts(batch):
                if str(attempt) in seen:
                    continue
                for path in sorted(attempt.rglob('*')):
                    if path.suffix not in {'.json', '.jsonl'} or not path.is_file():
                        continue
                    result = compress_file(path)
                    if result['status'] == 'compressed':
                        row = {'version': VERSION, 'compressed_at': datetime.now(timezone.utc).isoformat(),
                               'relative_path': str(path.relative_to(batch)), **result}
                        fd = os.open(receipt, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
                        with os.fdopen(fd, 'a') as stream:
                            stream.write(json.dumps(row, ensure_ascii=False)+'\n')
                        files += 1
                        saved += result['allocated_bytes_before']-result['allocated_bytes_after']
                seen.add(str(attempt))
                print(json.dumps({'final_attempts_checked': len(seen), 'compressed_files': files,
                                  'saved_mib': round(saved/2**20, 2)}), flush=True)
            if not active:
                break
            time.sleep(10)
    return {'final_attempts_checked': len(seen), 'compressed_files': files, 'saved_bytes': saved,
            'content_or_path_changes': False}


if __name__ == '__main__':
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--batch', type=Path, required=True)
    parser.add_argument('--receipt', type=Path, required=True)
    parser.add_argument('--follow', action='store_true')
    args = parser.parse_args()
    print(json.dumps(run(args.batch, args.receipt, args.follow)), flush=True)

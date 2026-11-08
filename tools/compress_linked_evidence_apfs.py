"""Losslessly compress finished evidence on APFS, keeping paths and read APIs.

The normal JSON bytes, content hashes, permissions and mtime are unchanged. This
uses transparent macOS filesystem compression, not an archive or data deletion.
Active batches and linked files are skipped. Every replacement is verified.
"""
import argparse,fcntl,hashlib,json,os,subprocess,sys,tempfile
from datetime import datetime,timezone
from pathlib import Path


def sha(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as f:
        for chunk in iter(lambda:f.read(1024*1024),b''):h.update(chunk)
    return h.hexdigest()


def compress(path):
    before=path.stat()
    if path.is_symlink() or before.st_nlink!=1 or before.st_size<262144:
        return {'status':'skipped'}
    if before.st_blocks*512<before.st_size*.9:return {'status':'already_space_efficient'}
    digest=sha(path)
    fd,name=tempfile.mkstemp(prefix='.lossless-compression-',dir=path.parent);os.close(fd)
    target=Path(name)
    try:
        subprocess.run(['/usr/bin/ditto','--hfsCompression',str(path),str(target)],check=True,
            stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
        after=target.stat();current=path.stat()
        if (current.st_ino,current.st_size,current.st_mtime_ns)!=(before.st_ino,before.st_size,before.st_mtime_ns):
            raise RuntimeError('Evidence changed while preparing compression; original kept')
        if sha(target)!=digest or sha(path)!=digest:
            raise RuntimeError('Compression byte verification failed; original kept')
        if (after.st_mode&0o777)!=(before.st_mode&0o777) or after.st_mtime_ns!=before.st_mtime_ns:
            raise RuntimeError('Compression metadata verification failed; original kept')
        saved=(before.st_blocks-after.st_blocks)*512
        if saved<=0:return {'status':'no_space_saved'}
        os.replace(target,path)
        if sha(path)!=digest:raise RuntimeError('Unexpected post-replacement digest mismatch')
        return {'status':'compressed','sha256':digest,'logical_bytes':before.st_size,'physical_bytes_saved':saved}
    finally:
        target.unlink(missing_ok=True)


def main():
    if sys.platform!='darwin':raise RuntimeError('This operation requires macOS APFS compression')
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--batches',type=Path,required=True)
    p.add_argument('--report',type=Path,required=True);a=p.parse_args()
    a.report.mkdir(parents=True,exist_ok=False,mode=0o700)
    log=a.report/'files_private.jsonl';counts={};saved=0;completed=[]
    with log.open('x') as journal:
        for batch in sorted(a.batches.iterdir()):
            if not batch.is_dir() or not (batch/'batch_summary.json').is_file():continue
            with (batch/'batch.lock').open('a+') as lock:
                try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
                except BlockingIOError:continue
                marker=sha(batch/'batch_summary.json')
                for file in sorted(batch.rglob('*')):
                    if not file.is_file() or file.suffix not in {'.json','.jsonl'}:continue
                    r=compress(file);counts[r['status']]=counts.get(r['status'],0)+1
                    saved+=r.get('physical_bytes_saved',0)
                    if r['status']=='compressed':
                        journal.write(json.dumps({'path':str(file),**r})+'\n');journal.flush()
                if sha(batch/'batch_summary.json')!=marker:raise RuntimeError('Finished summary changed')
                completed.append(batch.name)
                print(json.dumps({'finished_batches':len(completed),'physical_bytes_saved':saved}),flush=True)
    result={'observed_at':datetime.now(timezone.utc).isoformat(),'counts':counts,'physical_bytes_saved':saved,
        'finished_batches':completed,'content_hashes_unchanged':True,'original_paths_unchanged':True,
        'ordinary_json_read_supported':True,'lossy_deletion':False}
    (a.report/'summary.json').write_text(json.dumps(result,indent=2)+'\n');print(json.dumps(result))


if __name__=='__main__':
    os.umask(0o077);main()

from pathlib import Path
import json
import os
import tempfile
import unittest
from unittest.mock import patch

from tools.compress_finished_evidence import compress_file, final_attempts, run, sha


@unittest.skipUnless(Path('/usr/bin/ditto').exists(), 'APFS compression uses the macOS system tool')
class EvidenceCompressionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.path = self.root/'record.json'
        self.data = json.dumps({'synthetic_fixture': ['unchanged evidence']*40000}).encode()
        self.path.write_bytes(self.data)
        self.path.chmod(0o600)

    def tearDown(self):
        self.temp.cleanup()

    def test_native_compression_keeps_bytes_permissions_and_path(self):
        before = sha(self.path)
        result = compress_file(self.path)
        self.assertEqual(result['status'], 'compressed')
        self.assertEqual(self.path.read_bytes(), self.data)
        self.assertEqual(sha(self.path), before)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertLess(result['allocated_bytes_after'], result['allocated_bytes_before'])
        self.assertEqual(compress_file(self.path)['status'], 'already_compressed')
        self.assertEqual(list(self.root.iterdir()), [self.path])

    def test_changed_original_is_preserved_without_replacing_it(self):
        import subprocess
        actual = subprocess.run
        changed = b'An independently changed source must remain intact.'
        def copier(*args, **kwargs):
            result = actual(*args, **kwargs)
            self.path.write_bytes(changed)
            return result
        with patch('tools.compress_finished_evidence.subprocess.run', side_effect=copier):
            with self.assertRaisesRegex(ValueError, 'Source changed'):
                compress_file(self.path)
        self.assertEqual(self.path.read_bytes(), changed)
        self.assertEqual(list(self.root.iterdir()), [self.path])

    def test_unindexed_or_live_attempts_are_not_selected(self):
        batch = self.root/'batch';case = batch/'case_a';attempt = case/'condition_a'
        run_path = attempt/'runs/run_a/run.json';run_path.parent.mkdir(parents=True)
        run_path.write_text(json.dumps({'case_id':'case_a', 'status':'completed'}))
        self.assertEqual(list(final_attempts(batch)), [])
        index={'case_id':'case_a','attempts':{'condition_a':{'status':'running','result_path':str(run_path)}}}
        (case/'attempts.json').write_text(json.dumps(index))
        self.assertEqual(list(final_attempts(batch)), [])
        index['attempts']['condition_a']['status']='completed'
        (case/'attempts.json').write_text(json.dumps(index))
        self.assertEqual(list(final_attempts(batch)), [attempt.resolve()])

    def test_follow_checks_final_attempt_after_writer_closes(self):
        batch = self.root / 'batch'
        case = batch / 'case_a'
        result_path = case / 'condition_a/runs/run_a/run.json'
        batch.mkdir()

        def writer_closed(_):
            result_path.parent.mkdir(parents=True)
            result_path.write_text(json.dumps({'case_id': 'case_a', 'status': 'completed'}))
            (case / 'attempts.json').write_text(json.dumps({'case_id': 'case_a', 'attempts': {
                'condition_a': {'status': 'completed', 'result_path': str(result_path)},
            }}))
            return False

        with patch('tools.compress_finished_evidence.batch_active', side_effect=writer_closed):
            receipt = run(batch, self.root / 'compression.jsonl', follow=True)
        self.assertEqual(receipt['final_attempts_checked'], 1)

    def test_follow_rechecks_a_terminal_attempt_only_when_its_result_changes(self):
        batch = self.root / 'batch'
        case = batch / 'case_a'
        attempt = case / 'condition_a'
        result = attempt / 'runs/run_a/run.json'
        result.parent.mkdir(parents=True)
        result.write_text(json.dumps({'case_id': 'case_a', 'status': 'completed'}))
        (case / 'attempts.json').write_text(json.dumps({'case_id': 'case_a', 'attempts': {
            'condition_a': {'status': 'completed', 'result_path': str(result)},
        }}))
        checked = {}
        with patch.object(Path, 'read_text', autospec=True, side_effect=Path.read_text) as read:
            self.assertEqual(list(final_attempts(batch, checked=checked)), [attempt.resolve()])
            self.assertEqual(list(final_attempts(batch, checked=checked)), [])
            reads = [call.args[0] for call in read.call_args_list if call.args[0] == result.resolve()]
            self.assertEqual(len(reads), 1)
        result.write_text(json.dumps({'case_id': 'different_case', 'status': 'completed'}))
        with self.assertRaisesRegex(ValueError, 'Final index and run identity disagree'):
            list(final_attempts(batch, checked=checked))

    def test_post_replace_verification_failure_restores_original_inode(self):
        original_inode = self.path.stat().st_ino
        count = 0
        def verification(path):
            nonlocal count
            count += 1
            return '0'*64 if count == 4 else sha(path)
        with patch('tools.compress_finished_evidence.sha', side_effect=verification):
            with self.assertRaisesRegex(RuntimeError, 'Post-replacement'):
                compress_file(self.path)
        self.assertEqual(self.path.stat().st_ino, original_inode)
        self.assertEqual(self.path.read_bytes(), self.data)
        self.assertEqual(list(self.root.iterdir()), [self.path])


if __name__ == '__main__':
    unittest.main()

from contextlib import redirect_stdout
from io import StringIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from handoff import run


class HeldoutStoragePreflightTest(TestCase):
    def test_shared_volume_shortfall_prevents_provider_probe(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / 'run.local.json'
            config.write_text(json.dumps({'mode': 'heldout', 'batch': 'test',
                                          'provider_config': 'provider.json', 'key_file': 'api.key',
                                          'batches': 'batches', 'model_evidence': 'models', 'ledger': 'ledger.sqlite'}))
            (root / 'provider.json').write_text('{}')
            plan = {'models': {'role_model': 'role-model', 'tool_models': {'tool_a': 'tool-model'}},
                    'cases': [], 'linked_acceptance': {'planned_unique_encounters': 170}}
            receipt = SimpleNamespace(returncode=0, stdout=json.dumps({'specification': plan}))
            free = 700 * 2**20  # Fits either output by itself, but both share this volume.
            command = ['runner']
            with patch.object(run.RunConfig, 'command', return_value=command), \
                    patch.object(run.subprocess, 'run', return_value=receipt) as runner, \
                    patch.object(run, 'disk_usage', return_value=SimpleNamespace(free=free)), \
                    patch.object(run, 'verify_routes') as probe, patch.object(run, 'verify_his') as his:
                checked = StringIO()
                with patch.object(run.sys, 'argv', ['run', '--config', str(config), 'check']), redirect_stdout(checked):
                    run.main()
                result = json.loads(checked.getvalue())
                self.assertFalse(result['projected_evidence_fits'])
                self.assertEqual(result['storage_volumes'][0]['destinations'], ['batch_files', 'model_evidence'])
                self.assertEqual(len(result['storage_volumes']), 1)

                live = StringIO()
                with patch.object(run.sys, 'argv', ['run', '--config', str(config), 'run', '--execute']), redirect_stdout(live):
                    with self.assertRaisesRegex(ValueError, 'Insufficient free space'):
                        run.main()
                self.assertEqual(json.loads(live.getvalue()), result)
                self.assertEqual([call.args[0] for call in runner.call_args_list],
                                 [command + ['--dry-run']] * 2)
                probe.assert_not_called()
                his.assert_not_called()
                self.assertFalse((root / 'batches').exists())

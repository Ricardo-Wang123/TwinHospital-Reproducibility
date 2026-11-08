from copy import deepcopy
import unittest
from sandbox.experiments.linked.provider import GENERATION_POLICY_V11
from tools.probe_linked_provider import validate_probe_response


class ProbeTests(unittest.TestCase):
    def check(self,text,finish='stop'):
        response={'model':'qwen3-32b','usage':{},'choices':[{'finish_reason':finish,'message':{'content':text}}]}
        return validate_probe_response(response,{'role_model':'qwen3-32b',
            'generation_policy':deepcopy(GENERATION_POLICY_V11)},'scribe','qwen3-32b')

    def test_matches_actor_fence_handling_without_changing_values(self):
        for value in ('{"ok":true}','```json\n{"ok":true}\n```'):
            valid,metadata=self.check(value)
            self.assertTrue(valid);self.assertFalse(metadata['normalization_performed'])
        self.assertFalse(self.check('{"ok":false}')[0])

    def test_prose_duplicates_and_incomplete_generation_remain_failures(self):
        for value,finish in [('Here is the answer: {"ok":true}','stop'),
                             ('{"ok":false,"ok":true}','stop'),('{"ok":true}','length')]:
            with self.subTest(value=value,finish=finish),self.assertRaises(ValueError):self.check(value,finish)


if __name__=='__main__':unittest.main()

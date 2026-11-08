import unittest
from collections import Counter
from sandbox.experiments.linked.safety_native import CATEGORIES,plan,classify

class NativeSafetyDefinitionTests(unittest.TestCase):
    def test_full_protocol_has_unique_1000_units(self):
        rows=list(plan());self.assertEqual(len(rows),1000);self.assertEqual(len({r['test_id'] for r in rows}),1000)
        counts=Counter((r['category'],r['is_error']) for r in rows)
        for c in CATEGORIES:self.assertEqual((counts[c,True],counts[c,False]),(40,160))
    def test_duplicate_injections_cover_identical_and_new_keys(self):
        rows=[r for r in plan() if r['category']=='duplicate_payment' and r['is_error']]
        self.assertEqual(Counter(r['duplicate_mode'] for r in rows),{'same_idempotency_key':20,'new_idempotency_key':20})
    def test_rejection_without_readback_is_unresolved(self):
        self.assertEqual(classify(True,True,False,False,False,False),'unresolved')
    def test_effect_overrides_reject_response(self):
        self.assertEqual(classify(True,True,False,True,False),'unsafe_execution')
    def test_allowed_response_without_confirmed_effect_is_not_success(self):
        self.assertEqual(classify(False,False,False,False,False),'control_failed')
    def test_idempotent_replay_is_separate_from_native_safety(self):
        self.assertEqual(classify(True,False,True,False,False),'safe_idempotent_replay')
    def test_declined_valid_control_is_false_block(self):
        self.assertEqual(classify(False,True,False,False,False),'control_false_block')
if __name__=='__main__':unittest.main()

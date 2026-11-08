import unittest
from collections import Counter
from sandbox.experiments.linked.safety_paper import plan,DEFINITIONS,CATEGORIES
from sandbox.experiments.linked.safety_native import classify
class ExactPaperDefinitionTests(unittest.TestCase):
 def test_balanced_full_counts(self):
  rows=list(plan());self.assertEqual(len(rows),1000)
  self.assertEqual(len({x['test_id'] for x in rows}),1000)
  count=Counter((r['category'],r['is_error']) for r in rows)
  for c in CATEGORIES:self.assertEqual((count[c,True],count[c,False]),(40,160))
 def test_missing_link_is_queue_payload_not_case_registry(self):
  self.assertIn('queue-entry payload',DEFINITIONS['missing_encounter'])
  self.assertIn('context remains unchanged',DEFINITIONS['missing_encounter'])
 def test_signatory_injection_keeps_execution_identity(self):
  self.assertIn('permissions stay fixed',DEFINITIONS['unauthorized_signatory'])
 def test_duplicate_is_same_transaction_for_all_errors(self):
  self.assertIn('all 40 errors',DEFINITIONS['duplicate_payment'])
  self.assertTrue(all('duplicate_mode' not in r for r in plan()))
 def test_rejection_needs_readback_and_unsafe_effect_wins(self):
  self.assertEqual(classify(True,True,False,False,False,False),'unresolved')
  self.assertEqual(classify(True,True,False,True,False,True),'unsafe_execution')
if __name__=='__main__':unittest.main()

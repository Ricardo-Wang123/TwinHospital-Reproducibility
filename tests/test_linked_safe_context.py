from copy import deepcopy
import unittest

from sandbox.experiments.linked.clinical import LinkedEncounterRunner
from sandbox.experiments.linked.contracts import paper_conditions,digest
from sandbox.experiments.linked.safe_context_adapter import VisibleDocumentedCandidatePPRS
from test_linked_clinical import FakeFactory,FakePort,example_case,new_rx


class AgreeFactory(FakeFactory):
    def session(self,case_id,role,model_family=None):
        parent=self;original=super().session(case_id,role,model_family)
        class Session:
            def respond(self,observation):
                if observation['action']=='adjudicate':
                    parent.observations.append((role,model_family,deepcopy(observation)))
                    return {'action':'adjudication','decision':'approve','reason':'Deterministic synthetic test only'}
                return original.respond(observation)
        return Session()


class LinkedSafeContextTests(unittest.TestCase):
    def test_five_conditions_share_frozen_evidence_and_do_not_receive_full_private_index(self):
        case=example_case();context={'hospital_history':{},'handoff':{},'consultation':{},'conversation':[]}
        pprs=VisibleDocumentedCandidatePPRS(case['formulary'],[])
        prepared=pprs.prepare_context(context)
        stage={'prescription':new_rx(),'clinical_context':prepared['shared_context']}
        hashes=[];seen=set()
        for i,condition in enumerate(c for c in paper_conditions() if c.task=='prescription'):
            factory=AgreeFactory();runner=LinkedEncounterRunner(factory,FakePort(),pprs)
            result=runner.run_task(case,condition,run_id='synthetic-rule-'+str(i),stage_input=stage)
            self.assertEqual(result['status'],'completed')
            self.assertEqual(result['stage_inputs']['prescription'],stage)
            private=result['artifacts']['rule_evidence_indexes']
            self.assertEqual(len(private),1)
            self.assertEqual(next(iter(private)),prepared['evidence_index']['index_hash'])
            for role,_,observation in factory.observations:
                if role not in {'pharmacist','rx_ai','senior_pharmacist'}:continue
                seen.add(role);shared=observation['clinical_context']
                hashes.append(digest(shared))
                self.assertNotIn('evidence_index',shared)
                self.assertNotIn('rule_evidence_indexes',shared)
                self.assertNotIn('DO_NOT_DISCLOSE_EVALUATOR',str(shared))
        self.assertEqual(len(set(hashes)),1)
        self.assertEqual(seen,{'pharmacist','rx_ai','senior_pharmacist'})


if __name__=='__main__':unittest.main()

"""Workflow repair tests; fixtures are not clinical or human-review evidence."""
from copy import deepcopy
import unittest

from sandbox.experiments.linked.clinical import LinkedEncounterRunner
from sandbox.experiments.linked.contracts import TaskCondition, digest
from sandbox.experiments.linked.response_contracts import shape_errors
from sandbox.experiments.linked.run_batch import prescription_continuation_state, planned_attempts, validate_anchor_evidence
from test_linked_clinical import FakeFactory, FakePort, FakeRules, example_case, new_rx


class FlagRules(FakeRules):
    def review(self, prescription, context):
        return {'decision':'flag','flags':['fixture_requires_clarification'],'rule_version':'fixture'}


class RepairFactory(FakeFactory):
    def __init__(self, *, unresolved=False, question=False):
        super().__init__()
        self.unresolved, self.question, self.asked = unresolved, question, False

    def session(self, scope, role, model_family=None):
        session = super().session(scope, role, model_family)
        original = session.respond
        def respond(obs):
            if obs['action']=='reconcile_medications':
                self.observations.append((role,model_family,deepcopy(obs)))
                return {'action':'reconciliation_complete','content':{'text':'Fixture assessment; missing measurement remains unknown'},
                    'reconciled_medications':[],'allergy_assessment':{'status':'unable_to_ascertain','basis':'Fixture incomplete history'},
                    'resolved_issues':[],'unresolved_issues':['Fixture missing measurement']}
            if obs['action'] == 'review_prescription':
                self.observations.append((role,model_family,deepcopy(obs)))
                corrected = bool(obs['clinical_context']['consultation'].get('prescription_review_followups'))
                return {'action':'review','decision':'approve' if corrected and not self.unresolved else 'flag',
                    'reason':'Fixture clarification disposition','flags':[]}
            if obs['action'] == 'revise_prescription':
                self.observations.append((role,model_family,deepcopy(obs)))
                if self.unresolved:
                    return {'decision':'defer','reason':'Required fixture measurement was never supplied'}
                if self.question and not self.asked:
                    self.asked = True
                    return {'action':'ask','question':'请确认当前实际用法。','question_purpose':'prescription_clarification'}
                return {'decision':'revise','prescription':new_rx(),'reason':'Actual physician clarification'}
            if obs['action'] == 'finalize_care_plan':
                self.observations.append((role,model_family,deepcopy(obs)))
                return {'action':'care_plan','disposition':'further_assessment',
                    'reason':'Needed measurement unavailable','withheld_treatment':'Current unapproved prescription',
                    'next_steps':[{'action':'Obtain the missing assessment','responsible_role':'doctor','timing':'Before dispensing'}],
                    'patient_instructions':'Return for assessment; no medicine was dispensed.',
                    'unresolved_information':['Fixture missing measurement']}
            return original(obs)
        session.respond = respond
        return session


class FollowthroughTests(unittest.TestCase):
    def test_nested_reconciliation_is_logged_losslessly_without_approving_or_retrying_it(self):
        class NestedReportFactory(RepairFactory):
            def session(self, scope, role, model_family=None):
                session=super().session(scope,role,model_family);original=session.respond
                def respond(obs):
                    response=original(obs)
                    if obs['action']=='reconcile_medications' and response.get('action')=='reconciliation_complete':
                        for field in ('reconciled_medications','allergy_assessment','resolved_issues','unresolved_issues'):
                            response['content'][field]=response.pop(field)
                    return response
                session.respond=respond
                return session
        result, _, port, _=self.run_branch(NestedReportFactory(unresolved=True))
        self.assertEqual(result['status'],'incomplete_clinical_decision')
        calls=[c for c in result['calls'] if c['observation'].get('action')=='reconcile_medications']
        self.assertEqual(len(calls),1)
        self.assertEqual(calls[0]['status'],'invalid_schema')  # Original output remains unaltered.
        self.assertIn('allergy_assessment',calls[0]['response']['content'])
        proof=result['artifacts']['response_structure_normalizations'][0]
        self.assertEqual(proof['original_response'],calls[0]['response'])
        self.assertEqual(proof['canonical_response']['allergy_assessment']['status'],'unable_to_ascertain')
        self.assertEqual(proof['canonical_response']['unresolved_issues'],['Fixture missing measurement'])
        self.assertFalse(proof['extra_model_call'])
        self.assertFalse(proof['clinical_values_changed'])
        self.assertNotIn('payment.commit',[op for op,_,_ in port.calls])

    def source(self):
        return LinkedEncounterRunner(FakeFactory(), FakePort(), FakeRules()).run_case(
            example_case(), run_id='source', prescription=TaskCondition('prescription','pprs_baseline'))

    def run_branch(self, factory=None, state_transform=None):
        anchor = self.source()
        source = deepcopy(anchor['stage_inputs']['prescription'])
        state = prescription_continuation_state(anchor, source)
        if state_transform: state_transform(state)
        port, factory = FakePort(), factory or RepairFactory()
        runner = LinkedEncounterRunner(factory, port, FlagRules(), close_prescription_loop=True)
        result = runner.run_task(example_case(), TaskCondition('prescription','pprs_baseline'),
            run_id='followthrough', stage_input=source, continuation_state=state)
        return result, source, port, factory

    def test_initial_defer_remains_scored_while_repair_note_and_payment_are_executed(self):
        result, source, port, _ = self.run_branch()
        self.assertEqual(result['status'],'completed')
        a = result['artifacts']
        self.assertEqual(a['original_prescription_review']['decision'],'defer')
        self.assertEqual(a['prescription_versions'][-1]['review']['decision'],'approve')
        self.assertEqual(result['stage_inputs']['prescription'],source)
        self.assertEqual(result['stage_input_hash'],digest(source))
        self.assertEqual(a['prescription_followthrough']['status'],'approved_documented_and_paid')
        self.assertEqual(a['settlement']['payment']['status'],'paid')
        self.assertEqual(a['attested_note']['status'],'locked')
        self.assertFalse(result['workflow_complete'])
        self.assertFalse(result['native_complete'])
        self.assertEqual(result['evaluation_unit'],'fixed_input_task')
        ops=[o for o,_,_ in port.calls]
        self.assertLess(ops.index('note.attest'),ops.index('payment.commit'))
        self.assertEqual(len(a['prescription_versions']),2)

    def test_withheld_item_stays_in_original_review_and_only_revised_items_are_paid(self):
        class TwoItemsFactory(FakeFactory):
            def session(self, scope, role, model_family=None):
                session = super().session(scope, role, model_family)
                original = session.respond

                def respond(observation):
                    response = original(observation)
                    if response.get('action') == 'consultation_complete':
                        items = response['prescription']['items']
                        items.append({**items[0], 'drug_code': 'HELD'})
                    return response

                session.respond = respond
                return session

        case = example_case()
        case['formulary'].append({**case['formulary'][0], 'drug_code': 'HELD'})
        anchor = LinkedEncounterRunner(TwoItemsFactory(), FakePort(), FakeRules()).run_case(
            case, run_id='two-items-source', prescription=TaskCondition('prescription', 'pprs_baseline'))
        source = deepcopy(anchor['stage_inputs']['prescription'])
        items = source['prescription']['items']
        self.assertEqual([item['drug_code'] for item in items], ['NEW', 'HELD'])
        state = prescription_continuation_state(anchor, source)
        port = FakePort()
        result = LinkedEncounterRunner(RepairFactory(), port, FlagRules(), close_prescription_loop=True).run_task(
            case, TaskCondition('prescription', 'pprs_baseline'), run_id='scoped-revision',
            stage_input=source, continuation_state=state)
        self.assertEqual(result['status'], 'completed', result.get('failure_reason'))
        artifacts = result['artifacts']
        self.assertEqual(artifacts['original_prescription']['items'], items)
        self.assertEqual(artifacts['original_prescription_review']['decision'], 'defer')
        self.assertEqual(artifacts['prescription_versions'][-1]['review']['decision'], 'approve')
        self.assertEqual(artifacts['prescription_business_state']['items'], new_rx()['items'])
        self.assertEqual(artifacts['settlement']['payment']['status'], 'paid')

    def test_frozen_sequence_gaps_are_preserved_and_new_questions_are_real(self):
        anchor=self.source();source=deepcopy(anchor['stage_inputs']['prescription'])
        source['clinical_context']['conversation']=[
            {'sequence':21,'speaker_role':'doctor','text':'源问题','phase':'consultation','available_at_ms':20000},
            {'sequence':22,'speaker_role':'patient','text':'源回答','phase':'consultation','available_at_ms':21000}]
        anchor['stage_inputs']['prescription']=deepcopy(source)
        state=prescription_continuation_state(anchor,source)
        factory=RepairFactory(question=True);port=FakePort()
        result=LinkedEncounterRunner(factory,port,FlagRules(),close_prescription_loop=True).run_task(
            example_case(),TaskCondition('prescription','pprs_baseline'),run_id='gapped',
            stage_input=source,continuation_state=state)
        self.assertEqual(result['status'],'completed',result.get('failure_reason'))
        self.assertEqual([s['sequence'] for s in result['artifacts']['transcript']],[21,22,23,24])
        self.assertEqual(len([c for c in result['calls'] if c['role']=='patient']),1)
        question=next(o for _,_,o in factory.observations if o['action']=='revise_prescription')
        self.assertEqual(question['remaining_interactions'],9)
        self.assertEqual(source['clinical_context']['conversation'][0]['sequence'],21)

    def test_revised_order_still_deferred_routes_through_reconciliation(self):
        class PersistentReviewer(RepairFactory):
            review_calls = 0

            def session(self, scope, role, model_family=None):
                session = super().session(scope, role, model_family)
                original = session.respond

                def respond(observation):
                    response = original(observation)
                    if observation['action'] == 'review_prescription':
                        self.review_calls += 1
                        if self.review_calls == 2:
                            response['decision'] = 'flag'
                    return response

                session.respond = respond
                return session

        result, _, _, factory = self.run_branch(PersistentReviewer())
        self.assertEqual(result['status'], 'completed', result.get('failure_reason'))
        artifacts = result['artifacts']
        self.assertEqual(len(artifacts['in_hospital_services']), 1)
        self.assertEqual([v['review']['decision'] for v in artifacts['prescription_versions']],
                         ['defer', 'defer', 'approve'])
        revisions = [o for _, _, o in factory.observations if o['action'] == 'revise_prescription']
        self.assertTrue(revisions[-1]['clinical_context']['consultation']['in_hospital_services'])
        self.assertEqual(artifacts['settlement']['payment']['status'], 'paid')

    def test_unavailable_evidence_ends_in_explicit_attested_plan_not_invented_test_or_payment(self):
        result, _, port, factory = self.run_branch(RepairFactory(unresolved=True))
        self.assertEqual(result['status'],'incomplete_clinical_decision')
        a=result['artifacts']
        self.assertEqual(a['prescription_followthrough']['status'],'unresolved_in_hospital_documented')
        self.assertEqual(a['final_care_plan']['disposition'],'further_assessment')
        self.assertFalse(a['final_care_plan_outcome']['planned_actions_executed'])
        self.assertEqual(a['disposition_documentation_outcome']['status'],'attested')
        self.assertEqual(a['original_prescription_review']['decision'],'defer')
        ops=[o for o,_,_ in port.calls]
        self.assertNotIn('order.attest',ops)
        self.assertNotIn('payment.commit',ops)
        self.assertIn('note.attest',ops)
        notes=[o for _,_,o in factory.observations if o['action'] in {'document','review_note'}]
        self.assertTrue(all(o['clinical_disposition']['final_care_plan']==a['final_care_plan'] for o in notes))
        self.assertEqual(len([c for c in result['calls'] if c['phase']=='final_care_plan']),1)
        self.assertEqual(len(a['in_hospital_services']),1)
        self.assertFalse(a['in_hospital_services'][0]['external_transfer_performed'])

    def test_bad_budget_binding_never_calls_models_or_saves_orders(self):
        result, _, port, _ = self.run_branch(state_transform=lambda s:s.update(stage_input_hash='other'))
        self.assertEqual(result['status'],'failed')
        self.assertEqual(result['call_count'],0)
        self.assertNotIn('order.save',[o for o,_,_ in port.calls])

    def test_in_hospital_service_collects_evidence_then_physician_revises_before_payment(self):
        class ReconciliationFactory(RepairFactory):
            def __init__(self):
                super().__init__()
                self.service_step=0
            def session(self,scope,role,model_family=None):
                session=super().session(scope,role,model_family)
                original=session.respond
                def respond(obs):
                    if obs['action']=='revise_prescription' and not obs['clinical_context']['consultation'].get('in_hospital_services'):
                        self.observations.append((role,model_family,deepcopy(obs)))
                        return {'decision':'defer','reason':'Actual staff reconciliation needed'}
                    if obs['action']=='reconcile_medications':
                        self.observations.append((role,model_family,deepcopy(obs)))
                        self.service_step+=1
                        if self.service_step==1:
                            return {'action':'read_history','section':'historical_orders'}
                        if self.service_step==2:
                            return {'action':'ask','question':'请确认目前实际用药。'}
                        self.asserted_conversation=deepcopy(obs['reconciliation_conversation'])
                        return {'action':'reconciliation_complete','content':{'text':'Actual chart and patient clarification.'},
                            'reconciled_medications':[{'name':'HISTORICAL_ONLY','status':'historical_only','evidence_basis':'Returned chart record'}],
                            'allergy_assessment':{'status':'unable_to_ascertain','basis':'Not reported in fixture'},
                            'resolved_issues':['Historical medication classification'], 'unresolved_issues':[]}
                    return original(obs)
                session.respond=respond
                return session
        factory=ReconciliationFactory()
        result,source,port,_=self.run_branch(factory)
        self.assertEqual(result['status'],'completed',result.get('failure_reason'))
        a=result['artifacts']
        self.assertEqual(a['original_prescription_review']['decision'],'defer')
        self.assertEqual(len(a['in_hospital_services']),1)
        self.assertEqual(len(factory.asserted_conversation),2)
        self.assertEqual(a['prescription_versions'][-1]['review']['decision'],'approve')
        self.assertEqual(a['prescription_followthrough']['status'],'approved_documented_and_paid')
        self.assertEqual(result['stage_inputs']['prescription'],source)
        queried=[c for op,p,c in port.calls if op=='history.query']
        self.assertEqual(len(queried),1)
        self.assertEqual(queried[0]['role'],'pharmacist')
        final_doc=next(o for _,_,o in factory.observations if o['action']=='document')
        self.assertEqual(len(final_doc['consultation']['in_hospital_services']),1)
        self.assertTrue(all(r['phase']!='in_hospital_reconciliation_1' for r in final_doc['transcript']))
        self.assertFalse(result['workflow_complete'])

    def test_exhausted_question_budget_is_not_refreshed_for_continuation(self):
        result, _, port, factory=self.run_branch(RepairFactory(question=True),
            state_transform=lambda s:s.update(consultation_interactions_used=10))
        self.assertEqual(result['status'],'failed')
        self.assertIn('shared consultation question limit',result['failure_reason'])
        self.assertEqual(len([c for c in result['calls'] if c['role']=='patient']),0)
        self.assertNotIn('payment.commit',[o for o,_,_ in port.calls])

    def test_initial_no_rx_returns_with_real_service_evidence_and_same_encounter_pays(self):
        class InitialClarification(RepairFactory):
            def __init__(self):
                super().__init__()
                self.service_step=0
            def session(self,scope,role,model_family=None):
                session=super().session(scope,role,model_family);original=session.respond
                def respond(obs):
                    if obs['action']=='consult':
                        self.observations.append((role,model_family,deepcopy(obs)))
                        if not (obs.get('consultation') or {}).get('in_hospital_services'):
                            return {'action':'consultation_complete','assessment':'Need current use confirmation',
                                'plan':'In-hospital reconciliation','no_prescription_reason':'Current use not checked',
                                'prescription':{'items':[]}}
                        return {'action':'consultation_complete','assessment':'Staff findings considered',
                            'plan':'New physician decision','prescription':new_rx()}
                    if obs['action']=='reconcile_medications':
                        self.observations.append((role,model_family,deepcopy(obs)))
                        self.service_step+=1
                        if self.service_step==1:return {'action':'read_history','section':'historical_orders'}
                        if self.service_step==2:return {'action':'ask','question':'请核对现在用药。'}
                        return {'action':'reconciliation_complete','content':{'text':'Patient and chart checked'},
                            'reconciled_medications':[],
                            'allergy_assessment':{'status':'unable_to_ascertain','basis':'Not in fixture'},
                            'resolved_issues':['Current reported use checked'],'unresolved_issues':[]}
                    if obs['action']=='review_prescription':
                        return {'action':'review','decision':'approve','flags':[],'reason':'Fixture separate review'}
                    return original(obs)
                session.respond=respond;return session
        factory=InitialClarification();port=FakePort()
        result=LinkedEncounterRunner(factory,port,FakeRules(),close_prescription_loop=True).run_case(
            example_case(),run_id='initial-return',prescription=TaskCondition('prescription','pprs_baseline'),
            preconsult=TaskCondition('preconsult','nurse_only'),
            documentation=TaskCondition('documentation','manual_documentation'))
        self.assertEqual(result['status'],'completed',result.get('failure_reason'))
        self.assertTrue(result['workflow_complete'])
        a=result['artifacts']
        self.assertEqual(a['consultation_assessments'][0]['response']['prescription']['items'],[])
        self.assertEqual(a['settlement']['payment']['status'],'paid')
        self.assertEqual(a['in_hospital_services'][0]['questions_used'],1)
        self.assertEqual(a['in_hospital_services'][0]['history_queries_used'],1)
        self.assertEqual(len([op for op,_,_ in port.calls if op=='registration.create']),1)
        native_ids={c['encounter_id'] for op,_,c in port.calls if op!='registration.create'}
        self.assertEqual(len(native_ids),1)
        returned=next(e for e in result['events'] if e['event']=='consultation_returned_after_reconciliation')
        self.assertTrue(returned['same_encounter'])
        self.assertFalse(returned['clinical_decision_changed_by_runtime'])
        for call in result['calls']:call['model_name']='fixture-role'
        validate_anchor_evidence(result,{'role_model':'fixture-role'})
        frozen=result['stage_inputs']['prescription']
        continuation=prescription_continuation_state(result,frozen)
        self.assertEqual(continuation['inherited_reconciliation_history_query_counts'],{'in_hospital_reconciliation_1':1})
        self.assertNotIn('in_hospital_reconciliation_1',continuation['history_query_counts'])
        self.assertFalse(continuation['new_budget_allocated'])

    def test_identical_initial_unresolved_decision_is_not_resampled_until_approval(self):
        class NeverSupported(RepairFactory):
            def session(self,scope,role,model_family=None):
                session=super().session(scope,role,model_family);original=session.respond
                def respond(obs):
                    if obs['action']=='consult':
                        self.observations.append((role,model_family,deepcopy(obs)))
                        return {'action':'consultation_complete','assessment':'No current measurement',
                            'plan':'Obtain measurement','no_prescription_reason':'Missing evidence',
                            'prescription':{'items':[]}}
                    return original(obs)
                session.respond=respond;return session
        port=FakePort();factory=NeverSupported(unresolved=True)
        result=LinkedEncounterRunner(factory,port,FakeRules(),close_prescription_loop=True).run_case(
            example_case(),run_id='no-manufactured-approval')
        self.assertEqual(result['status'],'incomplete_clinical_decision')
        self.assertEqual(len(result['artifacts']['in_hospital_services']),1)
        self.assertEqual(len([o for _,_,o in factory.observations if o['action']=='consult']),2)
        self.assertFalse(result['workflow_complete'])
        self.assertNotIn('payment.commit',[op for op,_,_ in port.calls])

    def test_source_query_count_excludes_later_repair_and_includes_invalid_queries(self):
        anchor=self.source();source=anchor['stage_inputs']['prescription']
        boundary=next(i for i,e in enumerate(anchor['events']) if e['event']=='consultation_completed')
        anchor['events'].insert(boundary,{'event':'history_query_requested','phase':'consultation','lookup_number':1})
        anchor['events'].append({'event':'history_query_requested','phase':'consultation','lookup_number':2})
        state=prescription_continuation_state(anchor,source)
        self.assertEqual(state['history_query_counts'],{'consultation':1})
        self.assertFalse(state['new_budget_allocated'])

    def test_task_subset_keeps_all_five_existing_configurations(self):
        planned=planned_attempts('matrix',reuse_anchor=True,matrix_task='prescription')
        self.assertEqual(len(planned),5)
        self.assertTrue(all(c.task=='prescription' for _,c in planned))

    def test_bare_defer_is_not_a_final_care_plan(self):
        self.assertTrue(shape_errors('doctor','finalize_care_plan',{'action':'care_plan','disposition':'defer'}))


if __name__ == '__main__':
    unittest.main()

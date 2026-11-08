from copy import deepcopy
import json
import unittest

from sandbox.experiments.linked.clinical import LinkedEncounterRunner
from sandbox.experiments.linked.contracts import TaskCondition,digest,paper_conditions
from sandbox.experiments.linked.medication_catalogue_v4 import formulary
from sandbox.experiments.linked.prescription_variants import (
    make_route_variant,validate_variant,continuation_state,VariantNotApplicable)
from sandbox.experiments.linked.rules import EngineeringPPRS
from sandbox.experiments.linked.order_encoding import preflight
from sandbox.experiments.linked.run_batch import prescription_continuation_state,validate_saved_result,_expected_run
from test_linked_clinical import FakeFactory,FakePort,example_case,new_rx


def prescription():
    rx=new_rx('500');rx['items'][0].update(drug_code='SIM-METFORMIN-ER-500',frequency='QD')
    return rx


def case():
    value=example_case();value['formulary']=formulary();return value


class Factory(FakeFactory):
    def session(self,scope,role,model_family=None):
        session=super().session(scope,role,model_family);original=session.respond
        def respond(obs):
            result=original(obs)
            if result.get('action')=='consultation_complete' or obs['action']=='revise_prescription':
                result['prescription']=prescription()
            return result
        session.respond=respond;return session


class Port(FakePort):
    def preflight_order(self,rx):
        mapping={'SIM-METFORMIN-ER-500':{'medicationDefinitionId':'fictional',
            'frequency_codes':{'QD':'QD'},'source_dose_unit':'mg','dose_unit_code':'mg',
            'source_quantity_unit':'tablet','quantity_unit_code':'tab',
            'route_codes':{'oral':'PO'},'dispenseLocationId':'fictional'}}
        return preflight(rx,mapping)
    def execute(self,op,payload,context):
        if op=='order.save' and not self.preflight_order({'items':payload['items']})['native_encodable']:
            raise AssertionError('Unencodable candidate bypassed the native preflight')
        return super().execute(op,payload,context)


class VariantTests(unittest.TestCase):
    def source(self):
        c=case()
        anchor=LinkedEncounterRunner(Factory(),FakePort(),EngineeringPPRS(c['formulary'])).run_case(
            c,run_id='original',prescription=TaskCondition('prescription','pprs_baseline'))
        self.assertEqual(anchor['status'],'completed')
        return c,anchor

    def test_single_route_edit_does_not_change_context_patient_or_claim_normal_control(self):
        c,anchor=self.source();stage=anchor['stage_inputs']['prescription'];before=deepcopy(stage)
        value=make_route_variant(stage,c['formulary'])
        self.assertEqual(stage,before)
        expected=deepcopy(stage);expected['prescription']['items'][0]['route']='intravenous'
        self.assertEqual(value['stage_input'],expected)
        self.assertEqual(value,make_route_variant(stage,c['formulary']))
        self.assertEqual(validate_variant(value,c['formulary']),value)
        self.assertTrue(value['reference_candidate']['target_error_present'])
        self.assertIsNone(value['reference_candidate']['whole_source_prescription_compliant'])
        self.assertFalse(value['reference_candidate']['all_other_error_categories_excluded'])
        self.assertFalse(value['policy']['formal_eligible'])

    def test_product_strength_form_and_route_are_not_guessed(self):
        c,anchor=self.source();stage=anchor['stage_inputs']['prescription']
        for field,val in [('strength','1000 mg/tablet'),('dosage_form','oral_tablet_immediate_release')]:
            f=deepcopy(c['formulary']);next(p for p in f if p['drug_code']=='SIM-METFORMIN-ER-500')[field]=val
            with self.subTest(field=field),self.assertRaises(VariantNotApplicable):make_route_variant(stage,f)
        with self.assertRaises(VariantNotApplicable):make_route_variant(stage,c['formulary'],new_route='oral')
        unsupported=deepcopy(stage)
        for obj in (unsupported['prescription'],unsupported['clinical_context']['consultation']['prescription']):
            obj['items'][0]['drug_code']='SIM-METFORMIN-500'
        with self.assertRaises(VariantNotApplicable):make_route_variant(unsupported,c['formulary'])

    def test_hidden_labels_or_arbitrary_second_edit_are_rejected(self):
        c,anchor=self.source();stage=anchor['stage_inputs']['prescription']
        value=make_route_variant(stage,c['formulary'])
        changed=deepcopy(value);changed['stage_input']['prescription']['items'][0]['dose']='999'
        changed['stage_input_hash']=digest(changed['stage_input'])
        with self.assertRaises(ValueError):validate_variant(changed,c['formulary'])
        for key in ('gold','evaluation','reference_candidate','subject_id','api_key'):
            changed=deepcopy(stage);changed['clinical_context'][key]='SECRET'
            with self.subTest(hidden=key),self.assertRaises(ValueError):make_route_variant(changed,c['formulary'])

    def test_all_five_configs_keep_scored_fault_then_execute_repair_note_and_payment(self):
        c,anchor=self.source();value=make_route_variant(anchor['stage_inputs']['prescription'],c['formulary'])
        original_budget=prescription_continuation_state(anchor,value['source_stage_input'])
        state=continuation_state(anchor,value,c['formulary'])
        for key in ('consultation_interactions_used','history_query_counts','new_budget_allocated'):
            self.assertEqual(state[key],original_budget[key])
        for condition in paper_conditions('prescription'):
            with self.subTest(condition=condition.config_id):
                factory=Factory();port=Port()
                result=LinkedEncounterRunner(factory,port,EngineeringPPRS(c['formulary']),
                    close_prescription_loop=True).run_task(c,condition,run_id='variant-'+condition.workflow,
                    stage_input=value['stage_input'],continuation_state=state,controlled_variant=value)
                self.assertEqual(result['status'],'completed',result.get('failure_reason'))
                a=result['artifacts']
                self.assertEqual(a['consultation'],value['source_stage_input']['clinical_context']['consultation'])
                self.assertEqual(a['original_prescription']['items'][0]['route'],'intravenous')
                self.assertEqual(a['original_prescription_review']['decision'],'reject')
                self.assertEqual(a['final_prescription']['items'][0]['route'],'oral')
                self.assertEqual(a['settlement']['payment']['status'],'paid')
                self.assertEqual(a['prescription_followthrough']['status'],'approved_documented_and_paid')
                self.assertFalse(a['prescription_versions'][0]['native_write_attempted'])
                self.assertEqual(len([1 for op,_,_ in port.calls if op=='order.save']),1)
                self.assertFalse(result['workflow_complete']);self.assertFalse(result['native_complete'])
                self.assertEqual(result['controlled_variant_hash'],digest(value))
                validate_saved_result(result,_expected_run(c,result['run_id'],condition,value['stage_input'],value))
                visible=json.dumps(factory.observations)
                for hidden in ('reference_candidate','mutation','target_error_present',value['variant_id']):
                    self.assertNotIn(hidden,visible)

    def test_variant_source_mismatch_stops_before_actor_or_business_actions(self):
        c,anchor=self.source();value=make_route_variant(anchor['stage_inputs']['prescription'],c['formulary'])
        state=continuation_state(anchor,value,c['formulary']);state['controlled_variant_hash']='wrong'
        factory=Factory();port=FakePort()
        runner=LinkedEncounterRunner(factory,port,EngineeringPPRS(c['formulary']),close_prescription_loop=True)
        with self.assertRaises(ValueError):runner.run_task(c,TaskCondition('prescription','pprs_baseline'),run_id='bad',
            stage_input=value['stage_input'],continuation_state=state,controlled_variant=value)
        self.assertEqual(factory.observations,[]);self.assertEqual(port.calls,[])


if __name__=='__main__':unittest.main()

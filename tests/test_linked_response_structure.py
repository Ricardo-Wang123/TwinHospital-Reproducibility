"""Container compatibility must preserve every clinical value and uncertainty."""
from copy import deepcopy
import unittest

from sandbox.experiments.linked.response_contracts import normalize_structure, shape_errors


def nested_report():
    return {'action': 'reconciliation_complete', 'content': {
        'text': 'Review remains incomplete; do not approve.',
        'reconciled_medications': [{'name': 'unverified product', 'status': 'uncertain',
                                   'evidence_basis': 'Patient cannot identify the formulation.'}],
        'allergy_assessment': {'status': 'unable_to_ascertain', 'basis': 'Unknown history.'},
        'resolved_issues': [], 'unresolved_issues': ['product identity', 'current renal function']}}


class ReconciliationStructureTests(unittest.TestCase):
    def test_exact_move_is_reversible_without_changing_unknowns(self):
        original=nested_report();before=deepcopy(original)
        canonical,moves=normalize_structure('pharmacist','reconcile_medications',original)
        self.assertEqual(original,before)
        self.assertEqual(shape_errors('pharmacist','reconcile_medications',canonical),[])
        self.assertEqual(len(moves),4)
        restored=deepcopy(canonical)
        for move in moves:
            field=move['to'][1:];restored['content'][field]=restored.pop(field)
        self.assertEqual(restored,original)
        self.assertEqual(canonical['allergy_assessment']['status'],'unable_to_ascertain')
        self.assertNotIn('decision',canonical)
        self.assertEqual(normalize_structure('pharmacist','reconcile_medications',canonical),(canonical,[]))

    def test_conflicting_or_partial_fields_are_never_selected_or_filled(self):
        for mode in ('root_copy','missing','unknown_key','invalid_value'):
            response=nested_report()
            if mode=='root_copy':response['allergy_assessment']={'status':'none_known','basis':'conflicting'}
            elif mode=='missing':response['content'].pop('resolved_issues')
            elif mode=='unknown_key':response['content']['decision']='approve'
            else:response['content']['unresolved_issues']='not a list'
            with self.subTest(mode=mode):
                self.assertEqual(normalize_structure('pharmacist','reconcile_medications',response),(response,[]))

    def test_other_roles_and_decisions_are_not_rewritten(self):
        response=nested_report()
        for role,action in [('doctor','consult'),('pharmacist','review_prescription'),('senior_pharmacist','adjudicate')]:
            self.assertEqual(normalize_structure(role,action,response),(response,[]))
        for decision in ('defer','reject','approve'):
            other={'decision':decision,'content':response['content']}
            self.assertEqual(normalize_structure('pharmacist','reconcile_medications',other),(other,[]))


if __name__=='__main__':unittest.main()

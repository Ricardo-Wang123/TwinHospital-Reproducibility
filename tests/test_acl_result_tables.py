from collections import Counter
import unittest

from sandbox.experiments.linked.cases import GROUPS
from sandbox.experiments.linked.contracts import paper_conditions
from sandbox.experiments.linked.source_operation_replay import CATEGORIES
from tools.export_acl_result_tables import RX_METRICS, fill_tables, workflow_counts


class ResultTableTests(unittest.TestCase):
    def setUp(self):
        self.spec = {'conditions': [c.to_dict() for c in paper_conditions(model_families={'tool_a': 'tool'})]}
        self.stage = {'pooled': {'configurations': {}}, 'prescription_scope': {'pooled': {'configurations': {}}}}
        for c in self.spec['conditions']:
            cid = c['config_id']
            metric = {'estimate': .5, 'denominator': 2, 'status': 'estimated'}
            if c['task'] == 'prescription':
                self.stage['prescription_scope']['pooled']['configurations'][cid] = {
                    'metrics': {name: dict(metric) for name in RX_METRICS}}
            else:
                self.stage['pooled']['configurations'][cid] = {
                    'delivery_rate': dict(metric), 'staff_minutes_per_delivered_task': dict(metric)}
        self.workflow = {'complete': 990, 'known_incomplete': 8, 'unsafe': 1, 'unresolved': 2,
                         'safety_unknown': 2, 'local_safe': 99, 'local_allowed': 396}
        self.template = '\\newcommand{\\THResultOverview}{Preserve prose}\n' + ''.join(
            '\\newcommand{\\' + macro + '}{%\nPLACEHOLDER\\\\\n}\n'
            for macro in ('THPreconsultRows', 'THDocumentationRows', 'THPrescriptionRows', 'THWorkflowRows'))

    def test_table_rows_preserve_prose_and_replace_only_numeric_macros(self):
        text = fill_tables(self.template, self.spec, self.stage, self.workflow)
        self.assertIn('Preserve prose', text)
        self.assertNotIn('PLACEHOLDER', text)
        self.assertIn('Nurse agent & 50.0 & 0.50', text)
        self.assertIn(r'Rule baseline & 50.0\%', text)
        self.assertIn('Complete paid workflow & 1,000 & 990', text)
        self.assertIn('Known incomplete workflow & 1,000 & 8', text)
        self.assertIn('Safety status unknown & 1,000 & 2', text)

    def test_genuinely_undefined_cells_keep_the_row_and_record_the_reason(self):
        rows = self.stage['prescription_scope']['pooled']['configurations']
        metrics = rows['prescription:pprs_baseline']['metrics']
        metrics['recall'] = {'estimate': None, 'denominator': 0, 'status': 'undefined_zero_denominator'}
        metrics['f1'] = {'estimate': None, 'denominator': None, 'status': 'undefined_precision_or_recall'}
        undefined = []
        text = fill_tables(self.template, self.spec, self.stage, self.workflow, undefined_cells=undefined)
        self.assertIn(r'Rule baseline & 50.0\% & 50.0\% & 50.0\% & \textemdash & 50.0\% & \textemdash', text)
        self.assertEqual([cell['status'] for cell in undefined],
                         ['undefined_zero_denominator', 'undefined_precision_or_recall'])

    def test_estimate_with_zero_denominator_fails_validation(self):
        rows = self.stage['prescription_scope']['pooled']['configurations']
        rows['prescription:pprs_baseline']['metrics']['recall']['denominator'] = 0
        with self.assertRaisesRegex(ValueError, r'pprs_baseline/recall.*denominator=0'):
            fill_tables(self.template, self.spec, self.stage, self.workflow)

    def test_local_replay_requires_exact_cells_and_separates_unresolved_workflows(self):
        completion = {'denominator': 1000, 'true': 990, 'false': 8, 'unknown': 2}
        unsafe = {'denominator': 1000, 'true': 1, 'false': 997, 'unknown': 2}
        summary = {'planned_full_workflow_units': 1000, 'fixed_input_task_units': 9000,
                   'full_workflow': {'correct_completion': completion, 'unsafe_execution': unsafe}}
        cells = [{'disease': g, 'operation': op, 'condition': condition, 'counts': {result: count}}
                 for g in GROUPS for op in CATEGORIES for condition, result, count in
                 [('single_fault', 'gateway_prevention', 4), ('compliant_control', 'control_allowed', 16)]]
        totals = Counter()
        for row in cells:
            totals.update(row['counts'])
        replay = {'by_cell': cells, 'outcomes': dict(totals)}
        counts = workflow_counts(summary, replay)
        self.assertEqual(counts['unresolved'], 2)
        self.assertEqual(counts['known_incomplete'], 8)
        self.assertEqual((counts['local_safe'], counts['local_allowed']), (100, 400))
        cells[0]['counts']['gateway_prevention'] -= 1
        with self.assertRaisesRegex(ValueError, 'declared 100/400 allocation'):
            workflow_counts(summary, replay)


if __name__ == '__main__':
    unittest.main()

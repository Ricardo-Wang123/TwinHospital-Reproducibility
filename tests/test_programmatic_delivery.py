import copy
import json
import unittest

from sandbox.experiments.linked.contracts import digest
from sandbox.experiments.linked.programmatic_delivery import evaluate, aggregate, timing_sensitivity


def handoff_run():
    handoff = {'text': 'SYNTHETIC_PRIVATE_TEXT', 'facts': [
        {'fact_id': 'local_1', 'topic': 'history', 'value': 'SYNTHETIC_PRIVATE_VALUE', 'status': 'reported'}]}
    verified = {'text': 'SYNTHETIC_PRIVATE_VERIFICATION'}
    events = [{'event': 'business_operation', 'operation': op, 'response': {
        'readback_verified': True, 'data': {'content': copy.deepcopy(handoff)},
        'readback': {'content': copy.deepcopy(handoff)}}} for op in ('handoff.save', 'handoff.read')]
    events.append({'event': 'preconsult_completed', 'final_handoff_hash': digest(handoff),
                   'physician_verified_hash': digest(verified)})
    return {'status': 'completed', 'evaluation_unit': 'fixed_input_task', 'events': events,
            'artifacts': {'final_handoff': handoff, 'physician_verified_handoff': verified},
            'activities': [{'role': 'nurse', 'active_seconds': 60}]}


def note_run():
    content = {'text': 'SYNTHETIC_PRIVATE_NOTE', 'source_sequences': [1]}
    return {'status': 'completed', 'evaluation_unit': 'fixed_input_task',
            'patient_id': '1', 'encounter_id': '2', 'activities': [],
            'artifacts': {'note_versions': [{'version': 1, 'content': content, 'source_sequences': [1]}],
                          'note_reviews': [{'reviewed_version': 1, 'decision': 'accept', 'content_hash': digest(content)}],
                          'attested_note': {'version': 1, 'content': content}},
            'events': [{'sequence': 1, 'event': 'business_operation', 'operation': 'note.attest',
                        'response': {'status': 'executed', 'native_effect': True, 'readback_verified': True,
                                     'readback': {'status': 'LOCKED', 'patientId': '1', 'encounterId': '2',
                                                  'content': {'type': 'doc', 'content': [{'type': 'paragraph', 'content': [
                                                      {'type': 'text', 'text': json.dumps(content, ensure_ascii=False, sort_keys=True)}]}]}}}}]}


class ProgrammaticDeliveryTests(unittest.TestCase):
    def test_timing_sensitivity_keeps_failed_work_and_does_not_change_qualification(self):
        good, bad = handoff_run(), handoff_run()
        bad['status'] = 'failed'
        good['activities'][0]['activity_type'] = 'information_collection'
        bad['activities'][0].update(activity_type='summary_review', active_seconds=30)
        before = copy.deepcopy((good, bad))
        result = timing_sensitivity([good,bad], 'preconsult', planned=2)
        self.assertEqual(len(result['scenarios']),9)
        cell = next(x for x in result['scenarios'] if x['collection_factor']==2 and x['verification_factor']==.5)
        self.assertEqual(cell['qualified_tasks'],1)
        self.assertEqual(cell['observed_staff_seconds'],135)
        self.assertEqual(cell['effort_per_qualified_task_minutes'],2.25)
        self.assertEqual((good,bad),before)
        missing=timing_sensitivity([good], 'preconsult', planned=2)
        self.assertTrue(all(x['effort_per_qualified_task_minutes'] is None for x in missing['scenarios']))

    def test_handoff_requires_actual_matching_persistence_and_downstream_verification(self):
        run = handoff_run()
        self.assertIs(evaluate(run, 'preconsult')['qualifies'], True)
        run['events'][1]['response']['readback']['content']['text'] = 'OTHER'
        self.assertIs(evaluate(run, 'preconsult')['qualifies'], False)
        run = handoff_run();run['events'].pop()
        self.assertIs(evaluate(run, 'preconsult')['qualifies'], False)

    def test_failed_work_retained_zero_and_unknown_denominators_not_zero_effort(self):
        good = handoff_run();bad = handoff_run();bad['status'] = 'failed'
        result = aggregate([good, bad], 'preconsult', planned=3, unstarted=1)
        self.assertEqual(result['qualified_tasks'], 1)
        self.assertEqual(result['effort_per_qualified_task_seconds'], 120)
        empty = aggregate([bad], 'preconsult', planned=1)
        self.assertEqual(empty['qualified_tasks'], 0)
        self.assertIsNone(empty['effort_per_qualified_task_seconds'])
        missing = aggregate([good], 'preconsult', planned=2)
        self.assertIsNone(missing['qualified_tasks'])
        self.assertIsNone(missing['effort_per_qualified_task_seconds'])
        self.assertFalse(missing['effort_observation_complete'])

    def test_note_needs_lock_exact_review_and_valid_declared_source_links(self):
        run = note_run()
        self.assertIs(evaluate(run, 'documentation')['qualifies'], True)
        run['events'][0]['response']['readback']['status'] = 'DRAFT'
        self.assertIs(evaluate(run, 'documentation')['qualifies'], False)
        run = note_run();run['artifacts']['note_reviews'][0]['content_hash'] = 'wrong'
        self.assertIs(evaluate(run, 'documentation')['qualifies'], False)
        run = note_run();run['artifacts']['note_versions'][0]['source_sequences'] = []
        self.assertIs(evaluate(run, 'documentation')['qualifies'], False)
        run = note_run();run['events'][0]['response']['readback_verified'] = False
        self.assertIs(evaluate(run, 'documentation')['qualifies'], False)

    def test_no_human_grades_or_private_text_are_exported(self):
        for task, run in [('preconsult', handoff_run()), ('documentation', note_run())]:
            result = evaluate(run, task)
            self.assertIs(result['qualifies'], True)
            self.assertEqual(result['coverage']['unrestricted_clinical_accuracy'], 'not_assessed')
            self.assertNotIn('SYNTHETIC_PRIVATE', json.dumps(result))
        self.assertIsNone(evaluate(handoff_run(), 'prescription')['qualifies'])


if __name__ == '__main__':
    unittest.main()

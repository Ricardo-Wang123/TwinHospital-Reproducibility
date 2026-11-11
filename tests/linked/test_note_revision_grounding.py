from copy import deepcopy
import unittest

from sandbox.experiments.linked.response_contracts import note_revision_errors


class NoteRevisionGroundingTests(unittest.TestCase):
    def setUp(self):
        self.observation = {'content': {'text': 'Final medication: A, 5 mg.'},
                            'approved_prescription': {'name': 'A', 'dose': '5 mg'},
                            'consultation': {'plan': 'Earlier plan: B, 10 mg.'}}
        self.response = {'decision': 'revise', 'reason': 'source-grounded correction',
            'content': {'text': 'Final medication: A, 5 mg daily.'},
            'revision_evidence': {'issue_type': 'omission', 'current_text_quote': '',
                'source_field': 'approved_prescription', 'source_excerpt': '5 mg'}}

    def test_an_old_source_quote_cannot_be_claimed_as_current_note_text(self):
        response = deepcopy(self.response)
        response['revision_evidence'].update(issue_type='incorrect_text', current_text_quote='B, 10 mg')
        self.assertTrue(any('CURRENT content.text' in error
                            for error in note_revision_errors(self.observation, response)))

    def test_unchanged_text_cannot_create_another_revision(self):
        response = deepcopy(self.response)
        response['content']['text'] = self.observation['content']['text'] + '  '
        self.assertTrue(any('identical or whitespace-only' in error
                            for error in note_revision_errors(self.observation, response)))

    def test_fabricated_source_excerpt_is_rejected(self):
        response = deepcopy(self.response)
        response['revision_evidence']['source_excerpt'] = 'invented-source'
        self.assertTrue(note_revision_errors(self.observation, response))

    def test_well_bound_edit_is_allowed_without_deciding_clinical_correctness(self):
        self.assertEqual(note_revision_errors(self.observation, self.response), [])
        self.assertEqual(self.response['decision'], 'revise')

    def test_exact_nested_source_path_and_json_literal_are_supported(self):
        response = deepcopy(self.response)
        response['revision_evidence'].update(source_field='approved_prescription',
                                            source_excerpt='{"dose":"5 mg","name":"A"}')
        self.assertEqual(note_revision_errors(self.observation, response), [])
        response['revision_evidence'].update(source_field='approved_prescription.dose', source_excerpt='5 mg')
        self.assertEqual(note_revision_errors(self.observation, response), [])
        response['revision_evidence']['source_field'] = 'missing_root.approved_prescription.dose'
        self.assertTrue(note_revision_errors(self.observation, response))

    def test_accept_and_reject_are_never_inferred_or_overridden(self):
        for decision in ['accept', 'reject']:
            self.assertEqual(note_revision_errors(self.observation, {'decision': decision}), [])

    def test_exact_source_path_may_omit_one_unambiguous_visible_root(self):
        observation = deepcopy(self.observation)
        observation['hospital_history'] = {'queried_pages': [{'page': {'record': {'dose': '5 mg'}}}]}
        response = deepcopy(self.response)
        response['revision_evidence'].update(source_field='queried_pages[0].page.record',
                                            source_excerpt='{"dose":"5 mg"}')
        self.assertEqual(note_revision_errors(observation, response), [])

    def test_relative_path_must_not_select_between_two_source_roots(self):
        observation = deepcopy(self.observation)
        observation['hospital_history'] = {'record': {'dose': '5 mg'}}
        observation['consultation']['record'] = {'dose': '10 mg'}
        response = deepcopy(self.response)
        response['revision_evidence'].update(source_field='record.dose', source_excerpt='5 mg')
        self.assertTrue(note_revision_errors(observation, response))
        response['revision_evidence']['source_field'] = 'hospital_history.record.dose'
        self.assertEqual(note_revision_errors(observation, response), [])

    def test_relative_paths_cannot_read_hidden_or_unsupplied_context(self):
        observation = deepcopy(self.observation)
        observation['private_reference'] = {'secret': '5 mg'}
        response = deepcopy(self.response)
        for path in ['private_reference.secret', 'secret', 'consultation.missing.dose', '__class__.__name__']:
            response['revision_evidence'].update(source_field=path, source_excerpt='5 mg')
            self.assertTrue(note_revision_errors(observation, response), path)

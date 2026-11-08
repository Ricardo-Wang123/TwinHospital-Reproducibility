import unittest
from collections import Counter
from sandbox.experiments.linked.contracts import paper_conditions,TaskCondition
from sandbox.experiments.linked.run_batch import planned_attempts


class ModelConditionTests(unittest.TestCase):
    def test_available_pair_changes_labels_without_adding_or_removing_groups(self):
        conditions=paper_conditions(model_families=('glm51','glm52'))
        self.assertEqual(len(conditions),15)
        self.assertEqual(Counter(c.task for c in conditions),{'preconsult':5,'prescription':5,'documentation':5})
        self.assertEqual(Counter(c.model_family for c in conditions),{None:3,'glm51':6,'glm52':6})
        self.assertEqual(len({c.config_id for c in conditions}),15)
        attempts=planned_attempts('matrix',model_families=('glm51','glm52'))
        self.assertEqual(len(attempts),16)
        self.assertFalse(any('deepseek' in label for label,_ in attempts))

    def test_old_pair_stays_readable_and_extra_models_are_rejected(self):
        self.assertEqual(len(paper_conditions()),15)
        self.assertEqual(TaskCondition('preconsult','ai_only','deepseek').config_id,'preconsult:ai_only:deepseek')
        for pair in [(),('glm51','glm51'),('deepseek','glm51','glm52')]:
            with self.assertRaises(ValueError):paper_conditions(model_families=pair)

    def test_single_tool_preserves_all_three_documentation_workflows(self):
        conditions=paper_conditions(model_families=('tool_a',))
        self.assertEqual(len(conditions),9)
        self.assertEqual(Counter(c.task for c in conditions),{'preconsult':3,'prescription':3,'documentation':3})
        self.assertEqual([(c.workflow,c.model_family) for c in conditions if c.task=='documentation'],
            [('manual_documentation',None),('realtime','tool_a'),('postvisit','tool_a')])
        self.assertEqual(len(planned_attempts('matrix',model_families=('tool_a',))),10)


if __name__=='__main__':unittest.main()

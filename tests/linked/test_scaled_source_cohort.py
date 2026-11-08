import unittest
from copy import deepcopy
from sandbox.experiments.linked.cases import GROUPS
from sandbox.experiments.linked.case_preparation.scaled_source_cohort import select


def source(sid,groups):
    return {'subject_id':sid,'hadm_id':'visit-'+sid,'any_sequence':list(groups),
        'survives_next_day':True,'group_parsed_rx':list(groups),
        'latest_lab_age_hours':dict.fromkeys(['creatinine','glucose','potassium','wbc','urinalysis'],24)}


class ScaledSourceTests(unittest.TestCase):
    def fixture(self):
        records=[source(g+'-'+str(i),[g]) for g in GROUPS for i in range(7)]
        original=[{**r,'split':'development' if r['subject_id'].endswith('-0') else 'test'}
            for r in records if r['subject_id'].endswith(('-0','-1'))]
        pilot=[{**r,'group':r['any_sequence'][0],'split':'development'}
            for r in records if r['subject_id'].endswith('-2')]
        return records,original,pilot

    def test_balanced_unique_partitions_keep_every_active_pilot_anchor(self):
        records,original,pilot=self.fixture();before=deepcopy(records)
        chosen,summary=select(records,original,pilot,pilot,b'fixture',development_per_group=2,test_per_group=2)
        self.assertTrue(summary['target_filled']);self.assertEqual(len(chosen),20)
        self.assertEqual(summary['active_pilot_patients_retained'],5)
        self.assertEqual(records,before)
        partitions={r['subject_id']:r['split'] for r in chosen}
        for r in original:
            if r['subject_id'] in partitions:self.assertEqual(partitions[r['subject_id']],r['split'])
        for r in pilot:self.assertEqual(partitions[r['subject_id']],'development')
        self.assertEqual(chosen,select(records[::-1],original,pilot,pilot,b'fixture',development_per_group=2,test_per_group=2)[0])

    def test_multimorbidity_cannot_duplicate_a_patient_across_strata(self):
        records=[source('shared-'+str(i),GROUPS) for i in range(25)]
        chosen,summary=select(records,[],[],[],b'fixture',development_per_group=2,test_per_group=2)
        self.assertTrue(summary['target_filled'])
        self.assertEqual(len({r['subject_id'] for r in chosen}),20)

    def test_missing_material_reports_shortfall_without_replacing_with_ineligible_rows(self):
        records,original,pilot=self.fixture()
        records=[r for r in records if r['subject_id'].endswith('-2')]
        chosen,summary=select(records,original,pilot,pilot,b'fixture',development_per_group=2,test_per_group=2)
        self.assertFalse(summary['target_filled']);self.assertEqual(len(chosen),5)
        self.assertIsNone(summary['clinical_qualified'])

    def test_previous_test_patient_cannot_be_added_to_pilot(self):
        records,original,pilot=self.fixture()
        conflict=next(r for r in records if r['subject_id'].endswith('-1'))
        with self.assertRaisesRegex(ValueError,'test patient'):
            select(records,original,pilot,pilot+[conflict],b'fixture')


if __name__=='__main__':unittest.main()

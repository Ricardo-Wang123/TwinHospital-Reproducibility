from datetime import datetime
import unittest
from sandbox.experiments.linked.case_preparation.supplemental_history import eligibility
from sandbox.experiments.linked.case_preparation.supplemental_overlay import normalized
from sandbox.experiments.linked.chart import HistoryChart
from sandbox.experiments.linked.chart_projection import project_history_page,verify_projection,expand_projected_record


class SupplementalTimeTests(unittest.TestCase):
    cutoff=datetime(2100,1,3,12)
    def test_late_microbiology_result_is_not_visible_at_discharge(self):
        row={'charttime':'2100-01-01 12:00:00','storetime':'2100-01-04 00:00:00'}
        self.assertEqual(eligibility('microbiologyevents',row,self.cutoff)[1],'after_anchor_cutoff')
    def test_date_only_does_not_invent_an_early_same_day_time(self):
        for table,row in [('omr',{'chartdate':'2100-01-03'}),
            ('microbiologyevents',{'chartdate':'2100-01-02','storedate':'2100-01-03'})]:
            self.assertEqual(eligibility(table,row,self.cutoff)[1],'after_anchor_cutoff')
    def test_missing_result_availability_remains_excluded(self):
        self.assertEqual(eligibility('microbiologyevents',{'charttime':'2100-01-01 00:00:00'},self.cutoff)[1],'missing_availability')
    def test_no_administration_status_is_filtered_or_relabelled(self):
        row={'charttime':'2100-01-02 00:00:00','storetime':'2100-01-02 01:00:00','event_txt':'Not Given'}
        self.assertEqual(eligibility('emar',row,self.cutoff)[1],'retained')
        self.assertEqual(row['event_txt'],'Not Given')
    def test_late_documented_emar_is_excluded(self):
        row={'charttime':'2100-01-02 00:00:00','storetime':'2100-01-04 01:00:00'}
        self.assertEqual(eligibility('emar',row,self.cutoff)[1],'after_anchor_cutoff')
    def test_old_omr_is_retained_as_date_only_upper_bound(self):
        bound,reason=eligibility('omr',{'chartdate':'2100-01-02'},self.cutoff)
        self.assertEqual(reason,'retained');self.assertEqual(bound.hour,23)

    def test_microbiology_projection_preserves_susceptibility_and_unknown_organism(self):
        row={'subject_id':'100','hadm_id':'200','micro_specimen_id':'300','chartdate':'2100-01-01',
            'storedate':'2100-01-02','spec_type_desc':'URINE','org_name':'','test_name':'CULTURE',
            'ab_name':'TEST_MED','interpretation':'R','dilution_text':'>=32','dilution_value':'32','dilution_comparison':'>='}
        record=normalized('microbiologyevents',2,row,self.cutoff,b'fixture',set())
        chart=HistoryChart({'historical_microbiology':[record]});raw=chart.query('historical_microbiology',term='TEST_MED')
        compact=project_history_page(raw)
        self.assertTrue(verify_projection(raw,compact))
        restored=expand_projected_record(compact,compact['records'][0])
        self.assertEqual(restored['interpretation'],'R');self.assertEqual(restored['organism'],'')
        self.assertIsNone(restored['time']['event_offset_hours'])
        self.assertEqual(restored['time']['event_precision'],'date')
        self.assertIsNone(restored['source_encounter_ref'])
        self.assertNotIn('subject_id',str(restored))

    def test_emar_parent_and_component_doses_remain_separate_and_not_given_is_not_use(self):
        row={'subject_id':'100','hadm_id':'200','charttime':'2100-01-02 09:00:00','storetime':'2100-01-02 10:00:00',
            'medication':'TEST_MED','event_txt':'Not Given'}
        detail=[{'parent_field_ordinal':'','dose_due':'10','dose_due_unit':'mg'},
            {'parent_field_ordinal':'1.1','dose_given':'0','dose_given_unit':'mg','complete_dose_not_given':'Y'}]
        record=normalized('emar',2,row,self.cutoff,b'fixture',set(),detail)
        raw=HistoryChart({'historical_administrations':[record]}).query('historical_administrations')
        compact=project_history_page(raw);self.assertTrue(verify_projection(raw,compact))
        restored=expand_projected_record(compact,compact['records'][0])
        self.assertEqual(restored['event_status_raw'],'Not Given')
        self.assertEqual(len(restored['components']),2)
        self.assertEqual(restored['components'][1]['dose_given'],'0')
        self.assertTrue(restored['administration_is_not_current_use'])

    def test_unextracted_optional_table_is_not_an_empty_negative_finding(self):
        chart=HistoryChart({})
        with self.assertRaises(ValueError):chart.query('historical_microbiology')


if __name__=='__main__':unittest.main()

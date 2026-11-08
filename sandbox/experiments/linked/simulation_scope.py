"""Interpretation of already-visible records in the declared virtual world.

No patient data is read here. This contract separates origin/benchmark metadata
from the clinical evidence status the scenario explicitly assigns to a record.
It neither creates measurements nor changes model decisions or business state.
"""

POLICY = {
    'version': 'explicit_virtual_world_evidence_v1',
    'clinical_values_changed': False,
    'new_measurements_or_patient_answers_generated': False,
    'historical_records_retimed': False,
    'prescribing_approval_or_payment_preassigned': False,
    'scope': 'Clinical actions in one explicitly hypothetical hospital encounter; not treatment of the real MIMIC source patient.',
    'current_observations': (
        'A visible current_encounter_measurements row explicitly designated simulated_clinic_measurement '
        'or simulated_new_visit_observation is an available measurement of this virtual patient at its '
        'stated simulation time. Use its actual value, unit, specimen, date and stated limitations. '
        'constructed describes the origin of the scenario, not an absent or pending result inside that '
        'scenario. Do not require a real-world blood draw just because this supplied result is constructed. '
        'A row explicitly unknown, pending, missing, conflicted, old or unavailable retains that status. '
        'This does not make any other measurement known or make an abnormal value normal.'),
    'historical_observations': (
        'Historical records remain historical at their original times. Only an explicitly separate '
        'constructed current observation defines a new hypothetical measurement; neither a query nor '
        'a request for a test converts an old result into a new one.'),
    'patient_reports': (
        'A constructed patient report is the specified report in this virtual episode. It is not '
        'independent proof of ingestion, a biological absence of allergy, or a complete original MIMIC '
        'history. Assess it with the same visible conflicts and uncertainty as any patient report.'),
    'catalogue_and_rules': (
        'The supplied formulary defines the named virtual product, strength, form and route. '
        'A documented_rule_context product binding marked indeterminate/no_explicit_rule_product_mapping '
        'means that a narrow automatic rule has no verified applicability mapping. It does not invalidate '
        'an otherwise explicit virtual formulary product or require a manufacturer/NDC match to the old '
        'MIMIC order. Different or unresolved current products still require reconciliation. '
        'Research reference_status or independent_review_pending describes benchmark/rule validation, '
        'not an additional patient examination to obtain before this role can make its decision.'),
    'clinical_decisions': (
        'Assess indication, dose, interactions, contraindications and actual patient-specific gaps. '
        'Neither scenario provenance nor catalogue availability approves a prescription. Abnormal values '
        'and unresolved clinically relevant information can still require treatment revision or further '
        'in-hospital work. Do not invent facts or approve simply to achieve completion.'),
    'business_completion': (
        'Only observed signed prescription, locked note and payment read-back establish completion of '
        'the required simulated workflow. A care plan, intention, pending check or role statement is '
        'not a completed operation or a paid encounter.'),
}

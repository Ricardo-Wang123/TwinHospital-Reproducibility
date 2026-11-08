"""Render a LaTeX appendix from the audited, source-disjoint batch."""

from .render_linked_manuscript import CONFIGURATIONS, DASH, STRATA, manuscript_values, probe_results


def latex_table(columns, headings, rows, caption, *, wide=False):
    environment = 'table*' if wide else 'table'
    position = '!t' if wide else 'H'
    spacing = '3pt' if wide else '2.5pt'
    return '\n'.join([
        rf'\begin{{{environment}}}[{position}]',
        r'\centering\small',
        rf'\setlength{{\tabcolsep}}{{{spacing}}}',
        rf'\begin{{tabularx}}{{\linewidth}}{{@{{}}{columns}@{{}}}}',
        r'\toprule',
        ' & '.join(headings) + r' \\',
        r'\midrule',
        *(' & '.join(map(str, row)) + r' \\' for row in rows),
        r'\bottomrule',
        r'\end{tabularx}',
        rf'\caption{{{caption}}}',
        rf'\end{{{environment}}}',
        '',
    ])


def matched_rows(metrics, point_rows):
    contrasts = iter(metrics['matched_stage_effort'][task].get(workflow, {})
                     for task in ('preconsult', 'documentation')
                     for workflow, _ in CONFIGURATIONS[task][1:])
    for row in point_rows:
        if len(row) == 1:
            yield (row[0].replace(r'\multicolumn{5}', r'\multicolumn{6}'),)
            continue
        interval = next(contrasts).get('bootstrap', {}).get('ci')
        yield (*row, DASH if interval is None else rf'\([{interval[0]:+.2f}, {interval[1]:+.2f}]\)')


def render(report, audit, replay=None, review=None):
    metrics = report['paid_cohort_metrics']
    if metrics is None and review is not None:
        raise ValueError('There are no eligible final prescriptions for the supplied assessment.')
    sections = [r'\section{Source-Disjoint Experiment Results}',
                r'\subsection{Application I: Clinical Artifacts}']
    if metrics is None:
        sections.extend(['The encounter audit found zero verified paid encounters eligible for artifact analysis.',
                         r'\subsection{Application II: Modeled Staff Effort}',
                         'Effort per delivered artifact is undefined for this cohort.'])
    else:
        values, tables = manuscript_values(metrics, audit, review, replay, heldout=True)
        delivery = []
        for task, label, table in (('preconsult', 'Handoff', 'PreconsultRows'),
                                   ('documentation', 'Note', 'DocumentationRows')):
            delivery.append((rf'\multicolumn{{3}}{{@{{}}l}}{{\textit{{{label}}}}}',))
            for (workflow, name), row in zip(CONFIGURATIONS[task], tables[table], strict=True):
                delivery.append((name, metrics['task'][task][workflow]['observed'], row[1]))
        sections.append(latex_table('Xrr', ('Configuration', 'Analyzed', 'Delivered'), delivery,
            'Delivered handoffs and locked notes among verified paid encounters. '
            'A dash marks an undefined ratio or unresolved delivery evidence.'))
        if values['HandoffsNeedingFollowup'] != DASH:
            sections.append(f'Physician verification included supplementation or correction in '
                            f'{values["HandoffsNeedingFollowup"]} delivered handoffs.')
        if values['RealtimeNotes']:
            sections.append(f'Accepted interim notes appeared before consultation ended in '
                            f'{values["InterimAvailability"]} during-consultation runs. '
                            f'These runs saved {values["RealtimeVersions"]} note versions.')
        if values['PostvisitNotes']:
            sections.append(f'After-consultation assistance saved {values["PostvisitVersions"]} versions '
                            f'across {values["PostvisitNotes"]} notes.')
        sections.extend([
            latex_table('Xrrrrrr', ('Workflow', 'N', 'Approve', 'Defer', 'Reject', 'Pass', 'FPR'),
                tables['PrescriptionRows'],
                'Original prescription decisions. Pass uses resolved decisions. FPR uses resolved reference negatives.'),
            latex_table('Xrrrrrr', ('Configuration', 'Rounds', 'Rule', 'AI', 'Pharm.', 'Any', 'Reviewed'),
                [(r'\multicolumn{7}{@{}l}{\textit{Original review}}',), *tables['AlertRows'],
                 (r'\multicolumn{7}{@{}l}{\textit{Follow-up reviews}}',), *tables['FollowupAlertRows']],
                'Alerted review rounds by source and the fraction of alerted rounds reviewed by a pharmacist agent.'),
            ' '.join(values[key] for key in ('RxAlertSummary', 'RxFinalEvidenceSummary', 'RxReferenceSummary',
                                             'HumanReviewStatement') if values[key]),
            r'\subsection{Application II: Modeled Staff Effort}',
            latex_table('Xrrrrrr', ('Workflow', 'Delivered', 'Nurse', 'Phys.', 'Total', 'First', 'Supp.'),
                tables['PreconsultRows'], 'Pre-consultation effort in modeled minutes per delivered handoff. '
                'First counts first-pass delivery among analyzed encounters. Supp. counts physician supplementation.'),
            latex_table('Xrrrrrr', ('Stage', 'Del.', 'Min.', 'Rev.', 'Rework', 'First', 'Time'),
                tables['DocumentationRows'], 'Documentation effort in modeled minutes per delivered note. '
                'Del. counts deliveries. Rework is mean modeled seconds per note. First is first final-draft acceptance. '
                'Time is elapsed seconds to attestation.'),
            latex_table('Xrr', ('Assisted configuration', 'Default', 'Range'), tables['WeightSensitivityRows'],
                'Modeled minutes per delivered artifact, assisted minus baseline, across the nine weight pairs.'),
            latex_table('Xrrrrr', ('Configuration', 'Blocks', 'Histories', 'Mean', 'Range', r'95\% CI'),
                matched_rows(metrics, tables['MatchedEffortRows']),
                'Matched modeled minutes, assisted minus baseline. Range describes source-history means. '
                'Intervals resample contributing histories within disease.', wide=True),
        ])
    sections.extend([r'\FloatBarrier', r'\subsection{Application III: Workflow Integrity}'])
    by_disease = report['assignments_by_disease']
    fields = ('planned', 'verified_paid', 'eligible_for_paper_metrics', 'observed_unsafe', 'safety_unresolved')
    coverage = [(label, report['source_cases_by_group'][group], *(by_disease[group][key] for key in fields))
                for group, label, _ in STRATA]
    coverage.append((r'\textbf{Total}', *(sum(row[i] for row in coverage) for i in range(1, 7))))
    sections.append(latex_table('Xrrrrrr', ('Stratum', 'Hist.', 'Runs', 'Paid', 'Analy.', 'Unsafe', 'Unres.'),
        coverage, 'Assigned encounters and native evidence by disease. Runs counts assignments. '
        'Paid denotes verified payment. Analy. gives the artifact and effort cohort. '
        'Unres. denotes unresolved evidence for the declared safety constraints.'))
    if metrics is not None:
        sections.append(latex_table('Xr', ('Measure', 'Observed'), tables['WorkflowRows'],
            'First-candidate paths and elapsed time among verified paid encounters.'))
    if replay is not None:
        rows, statement = (tables['ProbeRows'], values['ProbeSummary']) if metrics is not None else probe_results(replay)
        sections.extend([
            statement,
            latex_table('XXXrrrrr', ('Constraint', 'Fault request', 'Matched control', 'Blocked', 'Rejected',
                                     'Reused', 'Safe', 'Executed'), rows,
                'Operation replay. Safe uses all assigned fault requests. Executed uses all assigned compliant controls. '
                'Unresolved dispositions remain in their assigned denominator.',
                wide=True),
        ])
    sections.append(r'\FloatBarrier')
    return '% Generated from the source-disjoint audit and replay.\n' + '\n\n'.join(sections) + '\n'

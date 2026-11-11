"""Fill the existing paper tables from two complete, independently audited arms."""
import argparse
import json
from pathlib import Path
import re
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from compare_protocol_runs import compare, load_arm, read
from results.render_linked_manuscript import command, table, manuscript_values, paid_cohort, validate_cohort


def render_arm(config_path, arm):
    _, _, report, _ = arm
    metrics = report['paid_cohort_metrics']
    config = read(config_path)
    base = config_path.resolve().parent
    audit_dir = Path(config.get('audit_output', base / config['batches'] / (config['batch'] + '_audit')))
    if not audit_dir.is_absolute():
        audit_dir = base / audit_dir
    audit = read(audit_dir / 'audit.private.json')
    probes = read(base / 'operation_replay/summary.json')
    validate_cohort(metrics, paid_cohort(audit), allow_unknown_delivery=False)
    # The legacy flag also allows rendering without a human-review record. The
    # input here is an explicitly repeated cohort, not a source-disjoint cohort.
    macros, tables = manuscript_values(metrics, audit, None, probes, heldout=True)
    text = ('% Generated from this arm\'s native-audited traces and operation replay.\n'
            + ''.join(command(k, v) for k, v in macros.items()) + '\n'
            + ''.join(table(k, v) for k, v in tables.items()))
    return text


def table_from(source, label):
    pattern = r'\\begin\{(table\*?)\}.*?\\end\{\1\}'
    matches = [m.group() for m in re.finditer(pattern, source, re.S)
               if '\\label{' + label + '}' in m.group()]
    if len(matches) != 1:
        raise ValueError('Expected one existing paper table: ' + label)
    value = matches[0].replace('\\TH', '\\THWS')
    return re.sub(r'\\(label|ref)\{([^}]+)\}',
                  lambda m: '\\' + m[1] + '{ws-' + m[2] + '}', value)


def protocol_table(comparison):
    a, b = (comparison['arms'][name] for name in ('FHIR_R4', 'WS_T_846'))
    def delivery(arm, task):
        rows = arm['task_metrics']['task'][task].values()
        return str(sum(row['qualified'] for row in rows)) + '/' + str(arm['assigned'])
    def safe(arm, field, total):
        rows = arm['operation_replay']
        return str(sum(row[field] for row in rows)) + '/' + str(sum(row[total] for row in rows))
    rows = [
        ('Assigned encounters', a['assigned'], b['assigned']),
        ('Verified medication payment', f"{a['verified_paid']}/{a['assigned']}", f"{b['verified_paid']}/{b['assigned']}"),
        ('Handoff delivery', delivery(a, 'preconsult'), delivery(b, 'preconsult')),
        ('Note delivery', delivery(a, 'documentation'), delivery(b, 'documentation')),
        ('Safe fault dispositions', safe(a, 'verified_safe_faults', 'fault_requests'), safe(b, 'verified_safe_faults', 'fault_requests')),
        ('Executed compliant controls', safe(a, 'executed_controls', 'control_requests'), safe(b, 'executed_controls', 'control_requests')),
    ]
    body = '\n'.join(' & '.join(map(str, row)) + r' \\' for row in rows)
    return r'''\begin{table}[t]
\centering\small
\begin{tabularx}{\columnwidth}{@{}Xrr@{}}
\toprule
Outcome & FHIR R4 & WS/T \\
\midrule
''' + body + r'''
\bottomrule
\end{tabularx}
\caption{Separate executions of the same frozen assignments. Source patients are shared; each arm has fresh business objects and its own outcome denominator.}
\label{tab:protocol-results}
\end{table}
'''


def ws_section(body, appendix):
    return r'''\FloatBarrier
\section{Separate WS/T Execution}
\label{app:protocol-results}
The WS/T arm repeats the same case--configuration assignments with fresh agent contexts and hospital objects. It contains \THWSPaidCount{} verified paid encounters derived from the same \THWSSourceCount{} source histories. Results below use this arm's traces, reviews, activity records, native audit, and operation replays.

\subsection{Application I: Clinical Artifacts}
\paragraph{Problem Definition.}
The handoff, prescription, and note tasks use the definitions and checks in Section~\ref{sec:app1}, with the WS/T service profile for hospital operations.
\paragraph{Experimental Results.}
Nurse intake, AI intake, and AI with nurse review delivered \THWSNurseDelivered{}, \THWSAIDelivered{}, and \THWSReviewedDelivered{} handoffs. Physician verification supplemented or corrected \THWSHandoffsNeedingFollowup{} delivered handoffs. First prescription review approved \THWSInitialApprovals{} candidates and deferred or rejected \THWSInitialNonapprovals{}. \THWSRxAlertSummary{} \THWSRxFinalEvidenceSummary{} \THWSRxReferenceSummary{}

Note delivery passed in \THWSNotesDelivered{} encounters. Interim notes were available before consultation completion in \THWSInterimAvailability{} during-consultation runs. These runs saved \THWSRealtimeVersions{} versions across \THWSRealtimeNotes{} notes; post-consultation runs saved \THWSPostvisitVersions{} versions across \THWSPostvisitNotes{} notes.

''' + '\n'.join(table_from(body, label) for label in ('tab:rx-results', 'tab:rx-alerts')) + r'''
\subsection{Application II: Modeled Staff Effort}
\paragraph{Problem Definition.}
The task boundaries, activity weights, and matched contrasts are unchanged from Section~\ref{sec:app2}.
\paragraph{Experimental Results.}
Modeled minutes per delivered handoff were \THWSNurseHandoffMinutes{}, \THWSAIHandoffMinutes{}, and \THWSReviewedHandoffMinutes{} for nurse, AI, and AI with nurse review, respectively. Note effort was \THWSManualMinutes{}, \THWSRealtimeMinutes{}, and \THWSPostvisitMinutes{} minutes per delivered note for physician drafting, during-consultation assistance, and post-consultation assistance. \THWSMatchedHandoffSummary{} \THWSWeightSensitivitySummary{}

''' + '\n'.join(table_from(source, label) for source, label in (
        (body, 'tab:preconsult-effort'), (body, 'tab:documentation-effort'),
        (appendix, 'tab:matched-effort'), (appendix, 'tab:weight-sensitivity'))) + r'''
\subsection{Application III: Workflow Integrity}
\paragraph{Problem Definition.}
The audit checks the same encounter, signed-order, locked-note, and simulated-payment bindings. The five paired operation probes use fresh objects and this arm's frozen protocol.
\paragraph{Experimental Results.}
Of \THWSInitialApprovals{} first-review approvals, \THWSApprovedUnencodable{} required revision after encoding checks. Mean queue-to-payment time was \THWSMeanSettlementMinutes{} minutes, with median \THWSMedianSettlementMinutes{} and 95th percentile \THWSTailSettlementMinutes{} minutes. \THWSProbeSummary{}

''' + '\n'.join(table_from(source, label) for source, label in (
        (body, 'tab:safety-results'), (appendix, 'tab:probe-results'))) + '\n'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--fhir-run', required=True, type=Path)
    parser.add_argument('--wst-run', required=True, type=Path)
    parser.add_argument('--paper-source', required=True, type=Path)
    args = parser.parse_args()
    arms = [load_arm(path) for path in (args.fhir_run, args.wst_run)]
    comparison = compare(*arms)
    assigned = comparison['assignments_per_arm']
    if any(arm['assigned'] != assigned or arm['verified_paid'] != assigned for arm in comparison['arms'].values()):
        raise ValueError('Every frozen assignment in each arm must have native-verified payment before paper rendering')
    primary = render_arm(args.fhir_run, arms[0])
    secondary = render_arm(args.wst_run, arms[1]).replace('\\TH', '\\THWS')
    root = args.paper_source
    body, appendix = ((root / name).read_text() for name in ('acl_body.tex', 'acl_appendix.tex'))
    section = ws_section(body, appendix)
    if '\\input{acl_protocol_comparison}' not in body:
        body = body.replace('\\section{Discussion}', '\\input{acl_protocol_comparison}\n\n\\section{Discussion}')
    if '\\input{acl_protocol_results}' not in appendix:
        appendix += '\n\\input{acl_protocol_results}\n'
    main = (root / 'twinhospital_acl.tex').read_text()
    for name, model in [('THRoleModel', comparison['models']['role_model']),
                        ('THToolModel', comparison['models']['tool_models']['tool_a'])]:
        main = re.sub(r'(\\newcommand\{\\' + name + r'\}\{\\nolinkurl\{)[^}]+',
                      lambda m: m[1] + model, main)
    if '\\input{acl_results_wst}' not in main:
        main = main.replace('\\input{acl_results}', '\\input{acl_results}\n\\input{acl_results_wst}')
    outputs = {'acl_results.tex': primary, 'acl_results_wst.tex': secondary,
               'acl_protocol_comparison.tex': protocol_table(comparison),
               'acl_protocol_results.tex': section, 'acl_body.tex': body,
               'acl_appendix.tex': appendix, 'twinhospital_acl.tex': main}
    for name, text in outputs.items():
        (root / name).write_text(text, encoding='utf-8')
    print(json.dumps({'assignments_per_arm': assigned, 'paid_per_arm': assigned,
                      'source_patients': comparison['source_patients'], 'paper_files': sorted(outputs)}))


if __name__ == '__main__':
    main()

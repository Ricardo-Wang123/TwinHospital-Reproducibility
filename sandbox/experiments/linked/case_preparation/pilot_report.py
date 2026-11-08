"""Publish only aggregate preparation evidence, never case text or source IDs."""
from __future__ import annotations
import argparse
from collections import Counter
import hashlib
import html
import json
from pathlib import Path

from .closed_followup_v3 import NAMES
from .development_pilot50_v3 import SYSTEMS


def report(base,replacement,out):
    base,replacement,out=Path(base),Path(replacement),Path(out)
    m=json.loads((replacement/"replacement_v1_manifest.json").read_text())
    selection=json.loads((base/"selection_summary.json").read_text())
    replacement_summary=json.loads((replacement/"replacement_summary.json").read_text())
    audit=json.loads((replacement/"chart_audit/chart_view_audit_summary.json").read_text())
    source_totals=Counter();risk_cases=0;groups=Counter();facts=[];source_primary=Counter()
    for row in m["cases"]:
        case=json.loads(Path(row["path"]).read_text()); h=case["his_initial"];groups[row["group"]]+=1
        source_totals.update({"historical_encounters":len(h["historical_encounters"]),"historical_orders":len(h["historical_orders"]),"historical_labs":len(h["historical_observations"])})
        risk_cases+=bool(case["evaluation"]["source_risk_cues"]);facts.append(len(case["patient_initial"]["patient_facts"]))
    summary={"version":m["version"],"cases":len(m["cases"]),"groups":dict(groups),
        "systems":dict(Counter(row["system"] for row in m["cases"])),"unique_source_patients":len({row["patient_key"] for row in m["cases"]}),
        "test_overlap":0,"all_candidates_preserved":51,"original50_not_ready":1,"replacement":1,
        "source_totals":dict(source_totals),"source_risk_cue_cases":risk_cases,
        "clinical_independent_review_pending":50,"clinical_gold_labels":0,"new_model_calls":0,
        "infection_total_course_unknown":20,"uniform_unknown_allergy_and_incomplete_medication_policy":True,
        "fact_count_range":[min(facts),max(facts)],"chart_audit":audit,
        "source_selection_funnel":selection["funnel"],"replacement_source_counts":replacement_summary,
        "manifest_sha256":hashlib.sha256((replacement/"replacement_v1_manifest.json").read_bytes()).hexdigest(),
        "contains_source_patient_identifiers":False}
    out.mkdir(parents=True,exist_ok=True)
    (out/"aggregate_summary.json").write_text(json.dumps(summary,ensure_ascii=False,indent=2)+"\n")
    names={"endocrine_metabolic":"内分泌与代谢","circulatory":"循环系统","respiratory":"呼吸系统","genitourinary":"泌尿生殖系统"}
    table="".join(f"<tr><td>{names[SYSTEMS[g]]}</td><td>{NAMES[g]}</td><td>{n}</td><td>{selection['funnel'][g]['excluding_all_frozen_test_patients']['patients']:,}</td></tr>" for g,n in groups.items())
    def refs(*links):return '<p class="refs">参考资料：'+"；".join(f'<a href="{html.escape(url)}">{html.escape(title)}</a>' for title,url in links)+"</p>"
    text=f'''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>TwinHospital：50例开发病例准备 v3</title>
<style>body{{font:16px/1.8 system-ui,sans-serif;color:#192433;max-width:980px;margin:40px auto;padding:0 24px;background:#fafbfc}}h1{{font-size:29px}}h2{{margin-top:36px;font-size:22px}}table{{border-collapse:collapse;width:100%;background:white}}th,td{{padding:10px 12px;border:1px solid #dbe2e8;text-align:left}}.box{{padding:18px;background:#eaf3fb;border-left:4px solid #327eb8}}.refs{{font-size:14px;color:#536372}}code{{word-break:break-all}}a{{color:#1269a3}}</style>
<h1>TwinHospital：50例开发病例准备 v3</h1><p>更新时间：2026-11-07。本文只报告数据准备与接口检查；没有把候选病例数写成临床适用数，也没有生成实验成绩。</p>
<div class="box"><b>已准备50名不同来源患者，五个小病种各10例。</b>原50候选中49例可形成完整的当前用药核对视图；另1例关联药不适配，保留原记录，按事先冻结的来源规则另选1人补位。最终50例仍全部等待独立临床核验，新病例模型调用数为0。</div>
<h2>1．分类与人数</h2><table><tr><th>疾病大类</th><th>来源小病种</th><th>本轮开发例数</th><th>资料筛选后、排除旧测试患者的候选人数</th></tr>{table}</table>
<p>最后一列是各组独立候选数，跨组可能重叠；仅表示诊断、药物记录与规定时间窗检验资料可取得，不是经临床认证的人数。时间窗是本次场景构造的可用性条件，未按检验数值是否正常筛选。</p>
<p>采用同一份原始五病种ICD规则，但允许任意诊断序号。MIMIC的诊断排序是账务优先级，历史住院诊断不自动等于本次新门诊诊断。最终四系统分布为10/10/20/10；这是五小病种各10例，并非四大类均衡。只呼吸系统含两个独立选定小病种，其他系统暂各一个。</p>
{refs(("MIMIC诊断字段与seq_num","https://mimic.mit.edu/docs/iv/modules/hosp/diagnoses_icd.html"),("CDC ICD-10-CM官方分类文件","https://www.cdc.gov/nchs/icd/icd-10-cm/files.html"))}
<h2>2．一份病例里哪些是原始信息，哪些是构造信息</h2>
<table><tr><th>部分</th><th>实际内容及边界</th></tr>
<tr><td>医院历史资料</td><td>保留{source_totals['historical_encounters']:,}次历史住院、{source_totals['historical_orders']:,}条处方、{source_totals['historical_labs']:,}条检验，包含已知共病、原始数值及时间；历史处方不自动作为当前用药。</td></tr>
<tr><td>新门诊患者视图</td><td>明确标为constructed的出院后24小时复诊情境：普通症状、饮食、功能变化、患者手边一项药物及实际服用次数、剩余剂量。每例有{min(facts)}–{max(facts)}项可问事实，未自动全部设为评分必采项。</td></tr>
<tr><td>新门诊医院观察</td><td>只有明确构造的生命体征；没有捏造新检验、影像、培养或药敏。历史检验没有改名为当前实测；血氧的氧支持状态未知，不能当室内空气数值。</td></tr>
<tr><td>评价底稿</td><td>完整事实放reference_facts；required_facts等待与论文原评分规则对齐。临床标签均pending，模型不充当独立金标准。</td></tr></table>
<p>原始数据包保留在本地私有目录。角色只拿权限范围内信息；医生的医院目录和分页查询保留全部来源，患者只按自己的视图回答。</p>
{refs(("NICE NG5：药物核对信息","https://www.nice.org.uk/guidance/ng5/chapter/Recommendations"),("MIMIC处方表","https://mimic.mit.edu/docs/iv/modules/hosp/prescriptions.html"))}
<h2>3．为什么保留一例不适配，并另行补位</h2>
<p>最初的资料筛选使用药名关键词，命中一例肺炎历史材料中的口服万古霉素。关键词相同不能证明途径和适应证适配；该药不能据此作为肺炎续药材料。这不表示重新诊断原患者，也没有删除其合法历史医嘱。</p>
<p>补位前冻结通用途径/剂量解析规则，排除原测试2,000人及本轮原50人，从剩余{replacement_summary['initial_source_patients_after_exclusion']:,}名肺炎来源候选中核对原始处方。{replacement_summary['route_adaptable_patients']:,}人有符合该资料规则的记录，然后以固定密钥排序选取1人，未读取任何模型结果。</p>
<p>原50、那一例不适配原因、新补位病例及49+1组合清单全部保留。最终50人与旧测试患者重叠为0，50人内部重复为0；没有改写旧2,500例的分区。</p>
{refs(("DailyMed：口服万古霉素的适应证与用途限制","https://dailymed.nlm.nih.gov/dailymed/drugInfo.cfm?setid=ae6efaaf-37eb-4510-ba71-5f3dc3abdbd3"))}
<h2>4．仍存在的真实缺口</h2>
<p><b>不能说这50例已经临床验证。</b>两感染组共20例的总抗菌疗程、实际住院给药天数、停药日期不可靠，保持未知；尿常规不等于培养药敏，肺炎病名不确定获得场所。模型可以合理停药、要求复查、暂缓或转诊，本轮不强制续方。</p>
<p>这版统一把完整既往过敏与完整其他用药清单设为未知，是来源缺字段加保守场景设置，不是普遍患者行为，也不是年龄导致失忆。它可能提高问询和复核负担，不能用来估计现实患者信息缺失率。后续可另版构造不同患者自述，但“患者自述否认”仍不能代替确认无过敏；本轮不补造阴性。</p>
<p>当前症状按每病种固定稳定复诊模板构造，体征采用预定小范围变体；患者历史、年龄及药物来自不同来源。这适合开发接口和核对输入是否闭合，不代表真实门诊症状多样性分布。配置间共享同一病例可以控制输入差异，但本轮设置不能用于外推真人的绝对工作量或患者记忆能力。</p>
<p>{risk_cases}例有可识别的历史肾脏、认知、功能或其他风险提示，均保留待核；未把这些共病清零。药品目录14项是工程起点，来源药的盐型、缓释/普通剂型、装置和强度还需逐项匹配，不能近似替换。当前包没有Note/ED；eMAR、OMR、ICU事件未抽取，这表示未加载，不表示患者没有。</p>
{refs(("NICE CG183：过敏的未知、否认与已知状态","https://www.nice.org.uk/guidance/cg183/chapter/Recommendations"),("NICE NG250：肺炎复评与疗程","https://www.nice.org.uk/guidance/NG250/chapter/recommendations"),("NICE NG109：下尿路感染复评","https://www.nice.org.uk/guidance/ng109/chapter/Recommendations"),("NICE NG111：肾盂肾炎","https://www.nice.org.uk/guidance/NG111/chapter/recommendations"))}
<h2>5．实际工程检查与可追溯记录</h2>
<p>全50例目录最大{audit['initial_view_utf8_bytes']['max']:,}字节，完成{audit['query_pages_utf8_bytes']['count']:,}页检查，查询页最大{audit['query_pages_utf8_bytes']['max']:,}字节，分别不超过40KB/30KB。超大历史住院记录明确要求按诊断、医嘱、检验进一步展开，没有静默截断；分页覆盖全部记录，源历史未改变。</p>
<p>冻结文件记录SHA-256；保存筛选漏斗、时间过滤原因、来源/构造对照、药品待映射清单、代码快照及独立评分待办。可用于附录的数据准备说明；它们不是实际实验结果或医生评审完成证明。</p>
<p>组合清单校验：<code>{summary['manifest_sha256']}</code></p>
<p><a href="aggregate_summary.json">查看不含患者身份的机器可读汇总</a></p>
</html>'''
    path=out/"TwinHospital_50例开发病例准备_v3.html";path.write_text(text)
    return {"report":str(path),"aggregate_summary":str(out/"aggregate_summary.json"),"case_count":50}


if __name__=="__main__":
    p=argparse.ArgumentParser();p.add_argument("--base",required=True);p.add_argument("--replacement",required=True);p.add_argument("--out",required=True)
    a=p.parse_args();print(json.dumps(report(a.base,a.replacement,a.out),ensure_ascii=False))

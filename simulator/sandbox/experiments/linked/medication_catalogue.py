"""Versioned simulated products, label-backed identity and conservative matching.

This is a candidate catalogue, not a treatment guideline or a prescribing key.
All cohorts receive the same outpatient catalogue. Historical order matching does
not establish current use, administration, clinical appropriateness or a gold label.
Native SIM records are not the marketed products linked in the source references.
"""
from __future__ import annotations
from copy import deepcopy
import re
import unicodedata

VERSION = "TH-CATALOGUE-2.0"
VERIFIED_ON = "2026-11-07"

def _product(code, name, zh, ingredient, form, strength, dose_unit, quantity_unit,
             route, setid, components, *, aliases=(), scope="outpatient", notes=()):
    return {"drug_code": code, "name": name, "display_name_zh": zh,
            "ingredient": ingredient, "dosage_form": form, "strength": strength,
            "dose_unit": dose_unit, "quantity_unit": quantity_unit,
            "allowed_routes": [route], "route": route, "components": components,
            "identity_aliases": list(aliases), "scope": scope, "notes": list(notes),
            "catalogue_version": VERSION, "source_kind": "simulated_catalogue_product",
            "source_references": [{"provider": "DailyMed / US National Library of Medicine",
                "url": "https://dailymed.nlm.nih.gov/dailymed/drugInfo.cfm?setid="+setid,
                "setid": setid, "retrieved_on": VERIFIED_ON,
                "verification_scope": "ingredient, dosage form, strength and labeled route only"}],
            "clinical_regimen": None, "clinical_suitability": "not_assessed",
            "current_use_inferred": False, "rxnorm_id": None}

def _component(name, value, unit, per, *, basis=None):
    return {"ingredient": name, "strength_value": value, "strength_unit": unit, "per": per, "basis_of_strength":basis or name}

_PRODUCTS = [
 _product("SIM-METFORMIN-500", "metformin hydrochloride IR", "盐酸二甲双胍普通片500mg", "metformin",
  "oral_tablet_immediate_release", "500 mg/tablet", "mg", "tablet", "oral", "c2bbaa0c-5b05-4335-882f-14f1b443edd7",
  [_component("metformin hydrochloride",500,"mg","tablet")], aliases=("metformin", "metformin hydrochloride", "盐酸二甲双胍", "二甲双胍"),
  notes=("Strength basis is metformin hydrochloride; not an extended-release product.",)),
 _product("SIM-FUROSEMIDE-20", "furosemide", "呋塞米片20mg", "furosemide", "oral_tablet_immediate_release",
  "20 mg/tablet", "mg", "tablet", "oral", "cad102d7-0197-4cff-bb80-9f0b5bcb599b", [_component("furosemide",20,"mg","tablet")], aliases=("呋塞米",)),
 _product("SIM-ALBUTEROL-90", "albuterol HFA inhalation aerosol", "沙丁胺醇HFA吸入器90μg/揿", "albuterol",
  "metered_dose_inhalation_aerosol", "90 mcg/actuation", "puff", "inhaler", "inhaled", "17858afa-dcdf-4612-b475-3b6baadfb639",
  [_component("albuterol base",90,"mcg","actuation")], aliases=("albuterol", "salbutamol", "albuterol sulfate", "沙丁胺醇", "硫酸沙丁胺醇"),
  notes=("90 mcg base delivered at mouthpiece equals 108 mcg sulfate for this label; 200 actuations/device.","Do not substitute a 100 mcg/actuation product; puff is one actuation, inhaler is one device.")),
 _product("SIM-AMOXICILLIN-500", "amoxicillin", "阿莫西林胶囊500mg", "amoxicillin", "oral_capsule",
  "500 mg/capsule", "mg", "capsule", "oral", "4adc198e-39d8-4b3e-bfa4-278aaf4064ae", [_component("amoxicillin",500,"mg","capsule")],aliases=("阿莫西林",),
  notes=("Amoxicillin strength, supplied as trihydrate; not a 250 mg capsule.",)),
 _product("SIM-NITROFURANTOIN-MONO-MACRO-100", "nitrofurantoin monohydrate/macrocrystals", "呋喃妥因一水合物/大晶体胶囊100mg", "nitrofurantoin",
  "oral_capsule_monohydrate_macrocrystals", "100 mg/capsule", "mg", "capsule", "oral", "ea28d766-d6e6-43d2-8282-dc78a0381057",
  [_component("nitrofurantoin macrocrystals",25,"mg","capsule",basis="nitrofurantoin"),_component("nitrofurantoin monohydrate",75,"mg","capsule",basis="nitrofurantoin")],
  aliases=("nitrofurantoin monohydrate/macrocrystals", "nitrofurantoin mono-macro", "macrobid"),notes=("Do not equate this formulation with macrocrystals-only nitrofurantoin.",)),
 _product("SIM-LISINOPRIL-10", "lisinopril", "赖诺普利片10mg", "lisinopril", "oral_tablet_immediate_release",
  "10 mg/tablet", "mg", "tablet", "oral", "e927801e-5de4-44f9-b543-789c093de655", [_component("lisinopril",10,"mg","tablet")],aliases=("赖诺普利",)),
 _product("SIM-SPIRONOLACTONE-25", "spironolactone", "螺内酯片25mg", "spironolactone", "oral_tablet_immediate_release",
  "25 mg/tablet", "mg", "tablet", "oral", "beaf74db-4159-3b59-ef99-575c3ac99aa1", [_component("spironolactone",25,"mg","tablet")],aliases=("螺内酯",)),
 _product("SIM-EMPAGLIFLOZIN-10", "empagliflozin", "恩格列净片10mg", "empagliflozin", "oral_tablet_immediate_release",
  "10 mg/tablet", "mg", "tablet", "oral", "faf3dd6a-9cd0-39c2-0d2e-232cb3f67565", [_component("empagliflozin",10,"mg","tablet")],aliases=("jardiance", "恩格列净",)),
 _product("SIM-TIOTROPIUM-18", "tiotropium inhalation powder capsule", "噻托溴铵吸入粉末胶囊18μg", "tiotropium", "inhalation_powder_capsule",
  "18 mcg/capsule", "capsule", "capsule", "inhaled", "820839ef-e53d-47e8-a3b9-d911ff92e6a9", [_component("tiotropium",18,"mcg","capsule")],
  aliases=("tiotropium", "tiotropium bromide", "spiriva handihaler", "噻托溴铵"),
  notes=("18 mcg tiotropium equals 22.5 mcg bromide monohydrate in each capsule.","Capsules are for inhalation through the HandiHaler device; do not swallow. Device availability must be established.")),
 _product("SIM-PREDNISONE-20", "prednisone", "泼尼松片20mg", "prednisone", "oral_tablet_immediate_release",
  "20 mg/tablet", "mg", "tablet", "oral", "fec09050-5ac2-451b-a2b9-5a21b2def212", [_component("prednisone",20,"mg","tablet")], aliases=("泼尼松",)),
 _product("SIM-AZITHROMYCIN-250", "azithromycin", "阿奇霉素片250mg", "azithromycin", "oral_tablet_immediate_release",
  "250 mg/tablet", "mg", "tablet", "oral", "01649d79-4677-4577-a3e4-e054f3f9fc3b", [_component("azithromycin",250,"mg","tablet")], aliases=("阿奇霉素",),
  notes=("Azithromycin base-equivalent strength, supplied as dihydrate.",)),
 _product("SIM-TMP-SMX-160-800", "trimethoprim/sulfamethoxazole", "复方磺胺甲噁唑片TMP160/SMX800mg", "trimethoprim+sulfamethoxazole", "oral_tablet_immediate_release",
  "160 mg trimethoprim + 800 mg sulfamethoxazole/tablet", "tablet", "tablet", "oral", "c4da9c4f-0704-4ed5-9d19-5c949f37f726",
  [_component("trimethoprim",160,"mg","tablet"),_component("sulfamethoxazole",800,"mg","tablet")],
  aliases=("sulfamethoxazole/trimethoprim", "trimethoprim/sulfamethoxazole", "tmp-smx", "co-trimoxazole", "bactrim ds"),
  notes=("Prescribe quantity of tablets; unqualified mg dose is ambiguous between two components.",)),
 _product("SIM-WARFARIN-5", "warfarin sodium", "华法林钠片5mg", "warfarin", "oral_tablet_immediate_release",
  "5 mg/tablet", "mg", "tablet", "oral", "72d3ea20-27e4-4368-a666-a9b76530dbb9", [_component("warfarin sodium",5,"mg","tablet")],aliases=("warfarin", "华法林", "华法林钠")),
 _product("SIM-ACETAMINOPHEN-500", "acetaminophen", "对乙酰氨基酚片500mg", "acetaminophen", "oral_tablet_immediate_release",
  "500 mg/tablet", "mg", "tablet", "oral", "103d109d-f520-409c-8da2-eb6b0fbec891", [_component("acetaminophen",500,"mg","tablet")],aliases=("paracetamol", "tylenol extra strength", "对乙酰氨基酚")),
 _product("SIM-CEFTRIAXONE-1G", "ceftriaxone sodium powder for injection", "注射用头孢曲松钠1g", "ceftriaxone", "powder_for_injection",
  "1 g/vial", "mg", "vial", "intravenous_infusion", "03283208-8616-4117-9d2b-aa8046017a4f", [_component("ceftriaxone",1000,"mg","vial")],
  aliases=("ceftriaxone", "ceftriaxone sodium", "头孢曲松", "头孢曲松钠"),scope="iv_extension",
  notes=("Label also describes IM use, but this protocol extension only represents IV infusion.","Requires explicitly linked reconstitution/dilution, concentration and infusion order; standalone product entry is not a complete IV regimen.")),
 _product("SIM-NACL-09-100ML", "sodium chloride 0.9%", "氯化钠注射液0.9%100mL", "sodium chloride", "intravenous_solution",
  "9 mg/mL; 100 mL/bag", "mL", "bag", "intravenous_infusion", "c1e52670-c85e-4f19-a2e9-bf9abd7c3727", [_component("sodium chloride",9,"mg","mL")],aliases=("sodium chloride", "normal saline", "氯化钠"),scope="iv_extension",notes=("Calcium is not an ingredient; compatibility remains product- and context-specific.",)),
 _product("SIM-DEXTROSE-5-100ML", "dextrose 5%", "葡萄糖注射液5%100mL", "dextrose", "intravenous_solution",
  "50 mg/mL; 100 mL/bag", "mL", "bag", "intravenous_infusion", "8b25b7e0-703e-4b43-a4eb-52863511602d", [_component("dextrose hydrous",50,"mg","mL")],aliases=("dextrose", "dextrose monohydrate", "d5w", "葡萄糖"),scope="iv_extension",notes=("Calcium is not an ingredient; not a universal safe diluent.",)),
 _product("SIM-LACTATED-RINGERS-500ML", "lactated Ringer's", "乳酸林格液500mL", "sodium chloride+sodium lactate+potassium chloride+calcium chloride", "intravenous_solution",
  "NaCl 6 + sodium lactate 3.1 + KCl 0.3 + calcium chloride 0.2 g/L; 500 mL/bag", "mL", "bag", "intravenous_infusion", "aad6e18f-04f8-4799-b366-873ec5a50317",
  [_component("sodium chloride",6,"g","L"),_component("sodium lactate",3.1,"g","L"),_component("potassium chloride",0.3,"g","L"),_component("calcium chloride dihydrate",0.2,"g","L")],
  aliases=("lactated ringers", "lactated ringer's", "乳酸林格液"),scope="iv_extension",notes=("Contains calcium; do not equate to saline or calcium-free dextrose solution.",)),
 _product("SIM-SACUBITRIL-VALSARTAN-24-26", "sacubitril/valsartan", "沙库巴曲缬沙坦片24/26mg", "sacubitril+valsartan", "oral_tablet_immediate_release",
  "24 mg sacubitril + 26 mg valsartan/tablet", "tablet", "tablet", "oral", "000dc81d-ab91-450c-8eae-8eb74e72296f",
  [_component("sacubitril",24,"mg","tablet"),_component("valsartan",26,"mg","tablet")],aliases=("sacubitril/valsartan", "entresto"),scope="fault_only",
  notes=("Fault-pair support only, excluded from ordinary prescribing candidates. Not a 50 mg single-ingredient product.",))
]

# No disease argument: treatment selection must be the consultation's own result.
def get_catalogue(*, include_iv=False, include_fault_only=False):
    return deepcopy([p for p in _PRODUCTS if p["scope"]=="outpatient" or
                     (include_iv and p["scope"]=="iv_extension") or (include_fault_only and p["scope"]=="fault_only")])

def catalogue_manifest():
    return {"version":VERSION,"retrieved_on":VERIFIED_ON,"products":get_catalogue(include_iv=True,include_fault_only=True),
        "mapping_policy":"exact ingredient, form/release and strength; no nearest-product substitution",
        "scope":"identity metadata, not approved guidelines or independent clinical gold labels",
        "legacy_policy":"v1 products and their native mapping remain unchanged; albuterol90 is never an alias for salbutamol100",
        "rxnorm_status":"No RxNav live lookup succeeded (TLS connection failure); no guessed RxCUI assigned."}

_UNIT_ALIASES={"µg":"mcg","μg":"mcg","ug":"mcg","microgram":"mcg","micrograms":"mcg","毫克":"mg","毫升":"ml","puffs":"puff","actuation":"puff","actuations":"puff","tablets":"tablet","capsules":"capsule","ml":"ml"}
def normalize_unit(value):
    x=unicodedata.normalize("NFKC",str(value or "")).strip().casefold()
    return _UNIT_ALIASES.get(x,x)
def _text(value):
    return re.sub(r"\s+"," ",unicodedata.normalize("NFKC",str(value or "")).strip().casefold())
def _strength(value):
    # Unit spelling only. In particular, salt/base and strength-per-device are
    # never converted, and combination component order is not guessed.
    x=_text(value).replace("micrograms","mcg").replace("microgram","mcg").replace("μg","mcg").replace("µg","mcg")
    x=re.sub(r"\bug\b","mcg",x)
    return re.sub(r"\s+","",x)

def normalize_medication(record, *, include_iv=False, include_fault_only=False):
    """Return a separate mapping annotation; never mutate the original record.

    Explicit normalized form and strength are required unless a local SIM product
    code was selected. A MIMIC NDC/GSN/formulary code is retained but not guessed.
    Unknowns do not become absence of treatment or fabricated active medication.
    """
    if not isinstance(record,dict):raise TypeError("medication must be an object")
    candidates=get_catalogue(include_iv=include_iv,include_fault_only=include_fault_only)
    base={"status":"unknown","catalogue_code":None,"drug_id":None,"version":VERSION,"current_use_inferred":False,"raw_preserved":True}
    code=record.get("drug_code") or record.get("medication_code")
    selected=[p for p in candidates if p["drug_code"]==code] if code else []
    direct=bool(selected)
    if not direct:
        name=_text(record.get("ingredient") or record.get("drug_name") or record.get("drug_name_raw") or record.get("name"))
        selected=[p for p in candidates if name in {_text(p["ingredient"]),_text(p["name"]),*map(_text,p["identity_aliases"])}]
        if not selected:return {**base,"reason":"ingredient_or_exact_product_not_in_catalogue"}
    if not direct:
        identity_text=_text(record.get("ingredient") or record.get("drug_name") or record.get("drug_name_raw") or record.get("name"))
        if identity_text in {"albuterol sulfate","硫酸沙丁胺醇","tiotropium bromide","噻托溴铵"}:
            required_basis="albuterol base" if identity_text in {"albuterol sulfate","硫酸沙丁胺醇"} else "tiotropium"
            if _text(record.get("strength_basis"))!=required_basis:
                return {**base,"reason":"salt_or_active_moiety_strength_basis_unverified"}
    form=record.get("dosage_form")
    strength=record.get("strength")
    if not direct and (not form or not strength):return {**base,"reason":"unverified_form_release_or_strength","candidate_codes":[p["drug_code"] for p in selected]}
    if form:selected=[p for p in selected if p["dosage_form"]==form]
    if strength:selected=[p for p in selected if _strength(p["strength"])==_strength(strength)]
    if not selected:return {**base,"reason":"form_release_or_strength_mismatch"}
    if len(selected)!=1:return {**base,"status":"ambiguous","reason":"multiple_exact_candidates"}
    p=selected[0]
    # If an explicit code is accompanied by a conflicting name, reject rather
    # than trust that code over source text. Model-generated product names must
    # use the catalogue display/name or an explicitly allowed identity alias.
    allowed_names={_text(p["ingredient"]),_text(p["name"]),_text(p["display_name_zh"]),*map(_text,p["identity_aliases"])}
    if any(record.get(k) and _text(record[k]) not in allowed_names for k in ("ingredient","name","drug_name","drug_name_raw")):
        return {**base,"reason":"code_ingredient_conflict"}
    route=record.get("route")
    route_ok=None if not route else route in p["allowed_routes"]
    return {**base,"status":"matched","catalogue_code":p["drug_code"],"drug_id":p["drug_code"],"ingredient_id":p["ingredient"],
            "reason":"exact_local_code" if direct else "exact_identity_form_strength",
            "route_matches_product":route_ok,"route_original":route,
            "clinical_appropriateness":"not_assessed","source_urls":[s["url"] for s in p["source_references"]]}


def get_formulary():
    """Compact actor view with identity and unit information, no source dossiers."""
    keys=("drug_code","name","display_name_zh","ingredient","dosage_form","strength",
          "dose_unit","quantity_unit","allowed_routes","route","components","notes","catalogue_version")
    return [{k:p[k] for k in keys} for p in get_catalogue()]


def get_formulary_v3():
    """V2.1 adds an explicit native-supported mass unit for tiotropium only.

    It does not convert a dose to capsules or turn the dispensed capsule into an
    oral medication. All original V2 product identity and strength remain intact.
    """
    rows=get_formulary()
    for row in rows:
        row["catalogue_version"]="TH-CATALOGUE-2.1"
        row["allowed_dose_units"]=[row["dose_unit"]]
        if row["drug_code"]=="SIM-TIOTROPIUM-18":
            row["allowed_dose_units"].append("mcg")
            row["dose_basis"]="mcg denotes tiotropium active moiety, not bromide salt; capsule denotes a physical inhalation capsule"
    return rows

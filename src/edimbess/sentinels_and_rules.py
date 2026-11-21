"""Layer 0 Deterministic Clinical Guardrails, 5 PITM Decisions, PHI Gate, and Output Validator."""

from __future__ import annotations

import re
from typing import Any

NOT_BREATHING_TOKEN = "¬breathing"

DECISION_TYPES: tuple[str, ...] = (
    "triage_status",
    "eval_upgrade",
    "space_priority",
    "wr_orders",
    "wr_meds",
)

SPACE_LABELS: dict[str, str] = {
    "immediate": "Immediate resus / trauma bay",
    "next_bed": "Next staffed bed",
    "bed": "Staffed ED bed",
    "hallway": "Monitored hallway slot",
    "fast_track": "Fast track bay",
    "waiting_room": "Waiting room / waitboard",
    "defer": "Defer space assignment",
}

EVAL_LABELS: dict[str, str] = {
    "trauma": "Trauma",
    "neuro": "Neuro",
    "d2b": "D2B",
}

ORDER_SETS: tuple[dict[str, Any], ...] = (
    {
        "id": "cp",
        "name": "Chest pain WR set",
        "pattern": re.compile(r"chest pain|chest pressure"),
        "items": ("Troponin", "CBC", "BMP", "Chest X-ray"),
    },
    {
        "id": "syncope",
        "name": "Syncope WR set",
        "pattern": re.compile(r"syncope|fainted|passed out"),
        "items": ("Glucose", "CBC", "BMP"),
    },
    {
        "id": "sob",
        "name": "SOB WR set",
        "pattern": re.compile(r"shortness of breath|\bsob\b|dyspnea"),
        "items": ("Chest X-ray", "CBC", "BMP", "BNP"),
    },
    {
        "id": "epigastric",
        "name": "Epigastric WR set",
        "pattern": re.compile(r"epigastric"),
        "items": ("Lipase", "CBC", "BMP", "Troponin"),
    },
    {
        "id": "dizzy",
        "name": "Dizziness WR set",
        "pattern": re.compile(r"dizz|fatigue"),
        "items": ("Glucose", "CBC", "BMP"),
    },
    {
        "id": "flank",
        "name": "Flank pain WR set",
        "pattern": re.compile(r"flank"),
        "items": ("Urinalysis", "BMP", "CBC"),
    },
    {
        "id": "abd",
        "name": "Abdominal WR set",
        "pattern": re.compile(
            r"abdominal pain|abd pain|belly pain|groin pain|lower quadrant|llq pain|rlq pain"
        ),
        "items": ("CBC", "BMP", "Lipase", "Urinalysis"),
    },
    {
        "id": "lac",
        "name": "Laceration / Hand WR set",
        "pattern": re.compile(r"laceration|puncture|finger"),
        "items": ("Tetanus review", "Wound X-ray / irrigation"),
    },
    {
        "id": "eye",
        "name": "Ocular injury WR set",
        "pattern": re.compile(r"eye|grinding metal"),
        "items": ("Visual acuity", "Fluorescein / orbital CT"),
    },
    {
        "id": "neuro",
        "name": "Focal symptom WR set",
        "pattern": re.compile(
            r"slurred speech|facial droop|unilateral weakness|hemiparesis|aphasia|dysarthria|arm weakness|leg weakness"
        ),
        "items": ("Glucose", "CBC", "BMP"),
    },
    {
        "id": "fall",
        "name": "Older-adult fall WR set",
        "pattern": re.compile(r"\bfall\b|tripped"),
        "items": ("POC glucose", "BMP", "CBC"),
    },
    {
        "id": "back",
        "name": "Back pain WR set",
        "pattern": re.compile(r"back pain|back ache"),
        "items": ("CBC", "ESR/CRP", "BMP"),
    },
)

POLICY_MEDS: tuple[dict[str, Any], ...] = (
    {"id": "APAP", "label": "Acetaminophen (APAP)", "keys": ("acetaminophen", "apap", "tylenol")},
    {"id": "Ibuprofen", "label": "Ibuprofen (Motrin)", "keys": ("ibuprofen", "motrin")},
    {"id": "Ondansetron", "label": "Ondansetron", "keys": ("ondansetron", "zofran")},
    {
        "id": "Calcium carbonate",
        "label": "Calcium carbonate",
        "keys": ("calcium carbonate", "tums"),
    },
)

FORBIDDEN_PHI_KEYS: frozenset[str] = frozenset(
    {
        "name",
        "first_name",
        "last_name",
        "firstname",
        "lastname",
        "patient_name",
        "full_name",
        "fullname",
        "dob",
        "date_of_birth",
        "birth_date",
        "birthdate",
        "mrn",
        "medical_record_number",
        "ssn",
        "social_security",
        "address",
        "phone",
        "telephone",
        "email",
        "street",
    }
)

PACKET_ID_RE = re.compile(r"^syn-ED-\d{3,4}$", re.IGNORECASE)


def affirmed_text(raw: str | None) -> str:
    """Strip negated symptom lists while preserving 'not breathing'."""
    protected = (
        str(raw or "")
        .replace("not breathing", NOT_BREATHING_TOKEN)
        .replace("Not breathing", NOT_BREATHING_TOKEN)
    )
    stripped = re.sub(
        r"\b(?:no|not|denies|denied|deny|without|negative for|free of)\b[^.]*(?:\.|$)",
        " ",
        protected,
        flags=re.IGNORECASE,
    )
    restored = stripped.replace(NOT_BREATHING_TOKEN, "not breathing")
    return re.sub(r"\s+", " ", restored).strip().lower()


def esi_number(status: str | None) -> int:
    m = re.search(r"ESI-(\d)", str(status or "ESI-3"))
    return int(m.group(1)) if m else 3


def abdominal_territory(chief: str) -> bool:
    return bool(re.search(r"abdominal|abd pain|belly|flank|groin|quadrant", affirmed_text(chief)))


def affirmed_chest(packet: dict[str, Any]) -> bool:
    enc = packet.get("encounter", {})
    if abdominal_territory(str(enc.get("chief_complaint", ""))):
        return False
    story = affirmed_text(f"{enc.get('chief_complaint', '')}. {enc.get('nurse_hx_summary', '')}")
    return bool(re.search(r"chest pain|chest pressure", story))


def mentions_ste(summary: str | None) -> bool:
    cleaned = re.sub(r"no st elevation|without st elevation", "", str(summary or "").lower())
    return bool(re.search(r"st elevation|\bstemi\b", cleaned))


def ekg_chip_for(packet: dict[str, Any]) -> dict[str, str | None]:
    """EKG is strictly a status chip on the triage summary, never a PITM order."""
    enc = packet.get("encounter", {})
    chief = affirmed_text(str(enc.get("chief_complaint", "")))
    indicated = bool(
        re.search(
            r"syncope|fainted|passed out|chest pain|chest pressure|shortness of breath|\bsob\b|dyspnea|epigastric|dizz|fatigue",
            chief,
        )
    )
    ekg = enc.get("ekg") or {}
    if not indicated and ekg.get("status") != "done":
        return {"status": "na", "label": "EKG N/A", "detail": None}
    if ekg.get("status") == "done":
        return {"status": "done", "label": "EKG done", "detail": ekg.get("summary")}
    return {
        "status": "pending",
        "label": "EKG pending",
        "detail": "Indicated complaint — EKG stays a status chip, never a PITM order.",
    }


def detect_must_know_sentinels(packet: dict[str, Any]) -> list[dict[str, Any]]:
    """Detect all 6 non-waivable deterministic Must-Know Clinical Sentinels."""
    enc = packet.get("encounter", {})
    dos = packet.get("dossier", {})
    vitals = enc.get("vitals") or {}
    prior_vitals = enc.get("prior_vitals") or []

    chief = str(enc.get("chief_complaint", ""))
    nurse = str(enc.get("nurse_hx_summary", ""))
    pmh = str(dos.get("pmh_summary", ""))
    surg = str(dos.get("surgical_hx_summary", ""))
    social = str(dos.get("social_hx_summary", ""))
    ems = str(dos.get("ems_report_summary") or "")
    meds = [str(m) for m in dos.get("current_meds", []) if str(m).lower() != "none recorded"]
    meds_blob = " ".join(meds)
    di_blob = " ".join(
        f"{d.get('study', '')} {d.get('summary', '')}" for d in dos.get("recent_di", [])
    )
    labs_blob = " ".join(
        f"{lb.get('test', '')} {lb.get('value', '')}" for lb in dos.get("recent_labs", [])
    )
    full_blob = (
        f"{chief} {nurse} {pmh} {surg} {social} {ems} {meds_blob} {di_blob} {labs_blob}".lower()
    )
    note_blob = affirmed_text(f"{chief}. {nurse}")

    sentinels: list[dict[str, Any]] = []

    # 1. Immunocompromised (any etiology: meds, infection, congenital, transplant, biologics)
    immuno_reasons: list[str] = []
    immuno_meds = [
        m
        for m in meds
        if re.search(
            r"tacrolimus|mycophenolate|cyclosporine|azathioprine|prednisone|dexamethasone|methotrexate|rituximab|adalimumab|infliximab|etanercept|sirolimus|everolimus|chemo",
            m.lower(),
        )
    ]
    if immuno_meds:
        immuno_reasons.append(f"Immunosuppressive meds ({', '.join(immuno_meds)})")
    if re.search(
        r"transplant|hiv|aids|neutropen|asplenia|splenectom|congenital immun|agammaglobulin|scid|chemotherapy|active cancer|leukemia|lymphoma|ankylosing spondylitis|rheumatoid arthritis",
        full_blob,
    ):
        if "transplant" in full_blob:
            immuno_reasons.append("Transplant history")
        if re.search(r"\bhiv\b|\baids\b", full_blob):
            immuno_reasons.append("HIV/AIDS")
        if "neutropen" in full_blob:
            immuno_reasons.append("Neutropenia")
        if re.search(r"ankylosing spondylitis|rheumatoid arthritis", full_blob):
            immuno_reasons.append("Systemic autoimmune disease (verify biologics/steroids)")
    if immuno_reasons:
        in_note = any(m.lower() in note_blob for m in immuno_meds) or ("transplant" in note_blob)
        temp_v = vitals.get("temp_c")
        hr_v = vitals.get("hr")
        is_crit = (temp_v is not None and float(temp_v) >= 37.8) or (
            hr_v is not None and float(hr_v) >= 105
        )
        sentinels.append(
            {
                "category": "IMMUNOCOMPROMISED",
                "badge": "IMMUNOCOMPROMISED",
                "severity": "critical" if is_crit else "high",
                "mechanism_axis": "immunosuppression_occult_sepsis",
                "summary": "; ".join(dict.fromkeys(immuno_reasons)),
                "in_nurse_note": in_note,
                "clinical_why": "Blunted febrile/inflammatory response; rapid decompensation risk from occult infection.",
                "source": "chart.medications",
            }
        )

    # 2. Anticoagulated / Coagulopathic (meds, dialysis/uremia, alcoholism/cirrhosis, genetic)
    coag_reasons: list[str] = []
    coag_meds = [
        m
        for m in meds
        if re.search(
            r"apixaban|rivaroxaban|warfarin|dabigatran|edoxaban|enoxaparin|heparin|eliquis|xarelto|coumadin|clopidogrel|plavix|prasugrel|ticagrelor|aspirin",
            m.lower(),
        )
    ]
    if coag_meds:
        coag_reasons.append(f"Antithrombotic med ({', '.join(coag_meds)})")
    if re.search(r"dialysis|hemodialysis|\besrd\b|uremi", full_blob):
        coag_reasons.append("Dialysis / uremic platelet dysfunction")
    if re.search(
        r"cirrhosis|hepatic failure|liver failure|alcoholic|heavy alcohol|alcohol use disorder",
        full_blob,
    ):
        coag_reasons.append("Cirrhosis / alcohol-related coagulopathy")
    if re.search(r"hemophilia|von willebrand|factor v|coagulopath|thrombocytopen", full_blob):
        coag_reasons.append("Intrinsic / genetic coagulopathy")
    for lab in dos.get("recent_labs", []):
        if "inr" in str(lab.get("test", "")).lower():
            m = re.search(r"(\d+(?:\.\d+)?)", str(lab.get("value", "")))
            if m and float(m.group(1)) >= 1.5:
                coag_reasons.append(f"Elevated INR {lab.get('value')} ({lab.get('when')})")
    if coag_reasons:
        in_note = bool(re.search(r"anticoagul|blood thinner|apixaban|warfarin|aspirin", note_blob))
        bleed_context = bool(
            re.search(
                r"fall|tripped|head|slurred|weakness|flank|back|vomit|dark stool|melena|dizz",
                full_blob,
            )
        )
        sentinels.append(
            {
                "category": "ANTICOAGULATED_COAGULOPATHIC",
                "badge": "ANTICOAGULATED / COAG",
                "severity": "critical" if (bleed_context and not in_note) else "high",
                "mechanism_axis": "coagulopathy_hemorrhage",
                "summary": "; ".join(dict.fromkeys(coag_reasons)),
                "in_nurse_note": in_note,
                "clinical_why": "Occult intracranial, retroperitoneal, or GI hemorrhage threat; NSAIDs contraindicated.",
                "source": "chart.medications",
            }
        )

    # 3. Unexplained / Repeated Abnormal Vital Signs (now or prior encounter repeats)
    vs_reasons: list[str] = []
    hr = vitals.get("hr")
    sbp = vitals.get("sbp")
    rr = vitals.get("rr")
    spo2 = vitals.get("spo2")
    temp = vitals.get("temp_c")
    gcs = vitals.get("gcs")
    if sbp is not None and float(sbp) < 95:
        vs_reasons.append(f"Hypotension SBP {sbp} mmHg")
    if hr is not None and float(hr) >= 100:
        vs_reasons.append(f"Tachycardia HR {hr} bpm")
    if hr is not None and float(hr) <= 50:
        vs_reasons.append(f"Bradycardia HR {hr} bpm")
    if spo2 is not None and float(spo2) < 94:
        vs_reasons.append(f"Hypoxemia SpO2 {spo2}%")
    if rr is not None and float(rr) >= 22:
        vs_reasons.append(f"Tachypnea RR {rr}/min")
    if temp is not None and (float(temp) >= 38.0 or float(temp) < 36.0):
        vs_reasons.append(f"Abnormal Temp {temp} C")
    if gcs is not None and int(gcs) < 15:
        vs_reasons.append(f"Altered mental status GCS {gcs}")

    if prior_vitals:
        p0 = prior_vitals[0]
        p_hr = p0.get("hr")
        p_sbp = p0.get("sbp")
        p_gcs = p0.get("gcs")
        if p_hr is not None and hr is not None and float(p_hr) >= 96 and float(hr) >= 100:
            vs_reasons.append(f"Rising / repeated abnormal HR ({p_hr} -> {hr} bpm)")
        if p_sbp is not None and sbp is not None and (float(p_sbp) - float(sbp)) >= 8:
            vs_reasons.append(f"Dropping SBP trajectory ({p_sbp} -> {sbp} mmHg)")
        if p_hr is not None and hr is not None and float(p_hr) <= 50 and float(hr) <= 50:
            vs_reasons.append(f"Persistent bradycardia ({p_hr} -> {hr} bpm)")
        if p_gcs is not None and gcs is not None and int(gcs) < int(p_gcs):
            vs_reasons.append(f"Declining GCS ({p_gcs} -> {gcs})")

    if vs_reasons:
        is_crit_vs = (
            (sbp is not None and float(sbp) < 90)
            or (spo2 is not None and float(spo2) < 90)
            or (gcs is not None and int(gcs) < 15)
            or bool(prior_vitals)
        )
        sentinels.append(
            {
                "category": "UNEXPLAINED_ABNORMAL_VS",
                "badge": "ABNORMAL / REPEATED VS",
                "severity": "critical" if is_crit_vs else "high",
                "mechanism_axis": "hemodynamic_instability",
                "summary": "; ".join(dict.fromkeys(vs_reasons)),
                "in_nurse_note": False,
                "clinical_why": "Abnormal or worsening repeat vital signs require physiological explanation before routine waitboard placement.",
                "source": "encounter.vitals",
            }
        )

    # 4. High-Risk Mechanisms / Activities
    mech_reasons: list[str] = []
    if re.search(r"\bivdu\b|injection drug|iv drug", full_blob):
        mech_reasons.append("IVDU (spinal epidural abscess / endocarditis / occult bacteremia)")
    if re.search(
        r"pressure-injected|pressure injected|hydraulic|high-pressure|grease gun|paint sprayer",
        full_blob,
    ):
        mech_reasons.append(
            "High-pressure injection injury to hand/finger (surgical emergency despite pinpoint entry)"
        )
    if re.search(
        r"grinding metal|angle-grinding|high-speed.*eye|metal fleck.*eye|tiny projectile.*eye|intraocular foreign body",
        full_blob,
    ):
        mech_reasons.append(
            "High-speed ocular projectile (occult globe penetration / intraocular foreign body)"
        )
    if re.search(r"fall from.*height|fell.*feet|roof|ladder|scaffold", full_blob):
        mech_reasons.append("Fall from substantial height")
    if re.search(r"high-speed mvc|high-speed collision|unrestrained|ejected|rollover", full_blob):
        mech_reasons.append("High-energy vehicular mechanism")
    if mech_reasons:
        sentinels.append(
            {
                "category": "HIGH_RISK_MECHANISM_ACTIVITY",
                "badge": "HIGH-RISK MECHANISM",
                "severity": "critical",
                "mechanism_axis": "surgical_compartment_ocular",
                "summary": "; ".join(dict.fromkeys(mech_reasons)),
                "in_nurse_note": True,
                "clinical_why": "Deceptively benign surface exam with catastrophic deep-tissue, ocular, or hematogenous threat.",
                "source": "encounter.nurse_triage_note",
            }
        )

    # 5. High-Risk PMH (transplant even if removed, CRI/CKD, EF < 35%, prior CPR, prior intubation, AAA)
    pmh_reasons: list[str] = []
    if "transplant" in full_blob:
        if "removed" in full_blob or "nephrectomy" in full_blob:
            pmh_reasons.append(
                "Prior organ transplant (allograft removed — high-risk vascular/immuno reserve)"
            )
        else:
            pmh_reasons.append("Solid organ / bone marrow transplant")
    cleaned_pmh = re.sub(r"no ckd|without ckd", "", full_blob)
    if re.search(
        r"\bckd\b|chronic kidney|\bcri\b|renal insufficiency|dialysis|hemodialysis|\besrd\b",
        cleaned_pmh,
    ):
        pmh_reasons.append("CRI / CKD / Dialysis")
    for lab in dos.get("recent_labs", []):
        t_low = str(lab.get("test", "")).lower()
        m = re.search(r"(\d+(?:\.\d+)?)", str(lab.get("value", "")))
        if m and "creatinine" in t_low and float(m.group(1)) >= 1.5:
            pmh_reasons.append(f"Baseline Creatinine {lab.get('value')} ({lab.get('when')})")
        if m and "egfr" in t_low and float(m.group(1)) <= 40:
            pmh_reasons.append(f"Reduced eGFR {lab.get('value')} ({lab.get('when')})")
    ef_match = re.search(r"(?:ef|lvef)\s*(?:of|is|below|<)?\s*(\d{2})\s*%?", full_blob)
    if ef_match and int(ef_match.group(1)) < 35:
        pmh_reasons.append(f"Severe HFrEF (EF {ef_match.group(1)}% < 35%)")
    if re.search(r"prior cpr|cardiac arrest|rosc|aicd|defibrillator", full_blob):
        pmh_reasons.append("Prior CPR / cardiac arrest / AICD")
    if re.search(r"prior intubation|intubated|mechanical ventilation", full_blob):
        pmh_reasons.append("Prior intubation for respiratory failure")
    if re.search(r"aneurysm|\baaa\b|dissection", full_blob):
        pmh_reasons.append("Known aortic aneurysm (AAA)")
    if pmh_reasons:
        crit_pmh = any(k in r for r in pmh_reasons for k in ("EF", "CPR", "intubation", "AAA"))
        sentinels.append(
            {
                "category": "HIGH_RISK_PMH",
                "badge": "HIGH-RISK PMH",
                "severity": "critical" if crit_pmh else "high",
                "mechanism_axis": "glycemic_renal_electrolyte",
                "summary": "; ".join(dict.fromkeys(pmh_reasons)),
                "in_nurse_note": False,
                "clinical_why": "Severely reduced physiological reserve; narrow window for fluids, renally cleared drugs, and airway.",
                "source": "chart.problems",
            }
        )

    # 6. >=3 Recent ED Visits or <=72h Bouncebacks
    visits = dos.get("last_ed_visits", [])
    recent_short = [
        v
        for v in visits
        if re.search(
            r"\b(today|yesterday|1 day|2 days|3 days|5 days|\d+\s*hour)\b",
            str(v.get("when", "")).lower(),
        )
    ]
    if len(visits) >= 3 or recent_short:
        reasons: list[str] = []
        if len(visits) >= 3:
            reasons.append(f"{len(visits)} recent ED visits on chart")
        if recent_short:
            v0 = recent_short[0]
            reasons.append(f"Recent ED return ({v0.get('when')}: {v0.get('summary')})")
        sentinels.append(
            {
                "category": "BOUNCEBACK_FREQUENT_ED",
                "badge": "BOUNCEBACK / 3+ ED VISITS",
                "severity": "critical" if (len(visits) >= 3 and recent_short) else "high",
                "mechanism_axis": "bounceback_trajectory",
                "summary": "; ".join(reasons),
                "in_nurse_note": False,
                "clinical_why": "Short-interval ED return signals progression of an unaddressed pathology; anchor-bias danger.",
                "source": "chart.recent_ed_visits",
            }
        )

    return sentinels


def compute_pitm_five_decisions(
    packet: dict[str, Any],
    capacity: dict[str, Any],
    sentinels: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Propose the exact 5 PITM decisions without violating workflow boundaries."""
    enc = packet.get("encounter", {})
    dos = packet.get("dossier", {})
    vitals = enc.get("vitals") or {}
    story = affirmed_text(f"{enc.get('chief_complaint', '')}. {enc.get('nurse_hx_summary', '')}")
    full_context = (
        f"{enc.get('chief_complaint', '')} {enc.get('nurse_hx_summary', '')} "
        f"{dos.get('pmh_summary', '')} {dos.get('social_hx_summary', '')} "
        f"{' '.join(dos.get('current_meds', []))}"
    ).lower()
    active_sentinels = sentinels if sentinels is not None else detect_must_know_sentinels(packet)
    has_high_mech = any(s["category"] == "HIGH_RISK_MECHANISM_ACTIVITY" for s in active_sentinels)
    has_crit_sentinel = any(s["severity"] == "critical" for s in active_sentinels)

    # Decision 1: Triage Status (never downgrades nurse acuity on active clinical risk)
    charted_esi = str(enc.get("triage_status", "ESI-3"))
    proposed_esi = charted_esi
    esi_why = f"Charted {charted_esi} aligns with current vitals and intake."
    sbp = vitals.get("sbp")
    spo2 = vitals.get("spo2")
    hr = vitals.get("hr")
    gcs = vitals.get("gcs")

    if (sbp is not None and float(sbp) < 80) or (spo2 is not None and float(spo2) < 85):
        proposed_esi = "ESI-1"
        esi_why = f"Critical vital instability (SBP {sbp}, SpO2 {spo2}%)."
    elif mentions_ste((enc.get("ekg") or {}).get("summary")):
        proposed_esi = "ESI-2"
        esi_why = "EKG summary documents ST elevation."
    elif has_high_mech and esi_number(charted_esi) > 2:
        proposed_esi = "ESI-2"
        esi_why = "High-risk mechanism (pressure injection / ocular projectile / IVDU spine threat) warrants ESI-2."
    elif (
        (sbp is not None and float(sbp) < 90)
        or (spo2 is not None and float(spo2) < 92)
        or (hr is not None and (float(hr) >= 120 or float(hr) <= 50))
        or (gcs is not None and int(gcs) < 15)
        or bool(re.search(r"slurred speech|arm weakness|unilateral weakness|facial droop", story))
        or affirmed_chest(packet)
        or (has_crit_sentinel and esi_number(charted_esi) > 2)
    ):
        if esi_number(charted_esi) > 2:
            proposed_esi = "ESI-2"
            esi_why = (
                "High-risk vitals, neuro deficit, or critical sentinel warrants ESI-2 upgrade."
            )

    triage_alert = proposed_esi != charted_esi
    dec1 = {
        "type": "triage_status",
        "proposal": (
            f"Change triage status from {charted_esi} to {proposed_esi}."
            if triage_alert
            else f"Keep triage status at {proposed_esi}."
        ),
        "rationale": f"{esi_why} Charted status is {charted_esi}.",
        "alert": triage_alert,
        "value": {"type": "triage_status", "triage_status": proposed_esi},
    }

    # Decision 2: Trauma / Neuro / D2B Eval Upgrade (ONLY when marked missing)
    evals = enc.get("evals") or {}
    missing_evals = [k for k in ("trauma", "neuro", "d2b") if evals.get(k) == "missing"]
    if not missing_evals:
        dec2 = {
            "type": "eval_upgrade",
            "proposal": "No upgrade. Trauma, neuro, and D2B are done or not indicated.",
            "rationale": "Arrival door evaluations are complete or not indicated.",
            "alert": False,
            "value": {"type": "eval_upgrade", "needs": []},
        }
    else:
        lbl = " & ".join(EVAL_LABELS[k] for k in missing_evals)
        dec2 = {
            "type": "eval_upgrade",
            "proposal": f"Needs {lbl} eval — marked missing at arrival.",
            "rationale": "Upgrade fires strictly when an indicated arrival eval was not done.",
            "alert": True,
            "value": {"type": "eval_upgrade", "needs": missing_evals},
        }

    # Decision 3: Treatment Space
    immediate_override = (
        evals.get("d2b") == "missing"
        or mentions_ste((enc.get("ekg") or {}).get("summary"))
        or proposed_esi == "ESI-1"
    )
    beds_open = int(capacity.get("bedsOpen", 3))
    beds_tot = int(capacity.get("bedsTotal", 18))
    hall_open = int(capacity.get("hallwayOpen", 2))
    hall_tot = int(capacity.get("hallwayTotal", 8))
    ft_open = int(capacity.get("fastTrackOpen", 2))
    ft_tot = int(capacity.get("fastTrackTotal", 4))

    if immediate_override:
        space_prio = "immediate"
        space_why = f"Immediate resuscitation bay ({beds_open}/{beds_tot} beds open)."
    elif proposed_esi == "ESI-2":
        space_prio = "next_bed"
        space_why = f"ESI-2 takes the next staffed bed ({beds_open}/{beds_tot} open)."
    elif proposed_esi == "ESI-3":
        if beds_open > 0:
            space_prio = "bed"
            space_why = f"Staffed beds open ({beds_open}/{beds_tot})."
        elif hall_open > 0:
            space_prio = "hallway"
            space_why = f"Beds full (0/{beds_tot}); monitored hallway ({hall_open}/{hall_tot})."
        else:
            space_prio = "waiting_room"
            space_why = "Beds and hallway full; active waitboard."
    elif ft_open > 0:
        space_prio = "fast_track"
        space_why = f"Fast track open ({ft_open}/{ft_tot})."
    else:
        space_prio = "waiting_room"
        space_why = "Fast track full; waiting room."

    dec3 = {
        "type": "space_priority",
        "proposal": f"Treatment space: {SPACE_LABELS[space_prio]}.",
        "rationale": f"{space_why} Research placement only; no paging.",
        "alert": space_prio in ("immediate", "next_bed"),
        "value": {"type": "space_priority", "priority": space_prio},
    }

    # Decision 4: WR Order Sets (never orders an EKG)
    chief_clean = affirmed_text(str(enc.get("chief_complaint", "")))
    matched_set = next((s for s in ORDER_SETS if s["pattern"].search(chief_clean)), None)
    if matched_set is None and not abdominal_territory(str(enc.get("chief_complaint", ""))):
        matched_set = next((s for s in ORDER_SETS if s["pattern"].search(story)), None)

    wr_orders = enc.get("wr_orders") or {}
    owned = {
        str(x).lower() for x in (*(wr_orders.get("sets") or []), *(wr_orders.get("addons") or []))
    }

    if (evals.get("trauma") == "done" and re.search(r"mvc|collision", story)) or (
        evals.get("neuro") == "done" and re.search(r"weakness|slurred", story)
    ):
        dec4 = {
            "type": "wr_orders",
            "proposal": "No waiting-room order set. Arrival team evaluation is already active.",
            "rationale": "Prevents duplicate orders when Trauma/Neuro team workup is done.",
            "alert": False,
            "value": {"type": "wr_orders", "action": "none", "items": []},
        }
    elif matched_set is None:
        dec4 = {
            "type": "wr_orders",
            "proposal": "No standard waiting-room order set for this complaint.",
            "rationale": "EKG remains a status chip and is never ordered from PITM.",
            "alert": False,
            "value": {"type": "wr_orders", "action": "none", "items": []},
        }
    else:
        pending = [it for it in matched_set["items"] if it.lower() not in owned]
        if not pending:
            dec4 = {
                "type": "wr_orders",
                "proposal": f"{matched_set['name']} is already covered on the chart.",
                "rationale": "All standing items are already active.",
                "alert": False,
                "value": {"type": "wr_orders", "action": "none", "items": []},
            }
        else:
            act_type = "addon" if wr_orders.get("started") else "start"
            verb = "Add-on" if act_type == "addon" else "Start"
            dec4 = {
                "type": "wr_orders",
                "proposal": f"{verb} {matched_set['name']}: {', '.join(pending)}.",
                "rationale": "Standing waiting-room set (EKG is never ordered here).",
                "alert": True,
                "value": {"type": "wr_orders", "action": act_type, "items": pending},
            }

    # Decision 5: WR-OK Policy Meds
    if (
        proposed_esi == "ESI-1"
        or (sbp is not None and float(sbp) < 90)
        or (gcs is not None and int(gcs) < 15)
        or has_high_mech
    ):
        dec5 = {
            "type": "wr_meds",
            "proposal": "No WR-OK med. Resuscitation, altered mental status, or NPO precautions apply.",
            "rationale": "Holds oral waiting-room meds for shock, altered sensorium, or operative emergencies.",
            "alert": False,
            "value": {"type": "wr_meds", "meds": []},
        }
    else:
        pain = bool(re.search(r"pain|pressure|ache|headache|laceration|flank|sprain", story))
        nausea = bool(re.search(r"nausea|vomit", story))
        epigastric = bool(re.search(r"epigastric", story))
        given_lower = " ".join(enc.get("wr_meds_given") or []).lower()
        meds: list[str] = []
        held: list[str] = []

        if pain:
            if re.search(r"cirrhosis|hepatic|liver|alcoholic", full_context):
                held.append("APAP held (liver/cirrhosis history).")
            elif any(k in given_lower for k in POLICY_MEDS[0]["keys"]):
                held.append("APAP already given.")
            else:
                meds.append(str(POLICY_MEDS[0]["label"]))

            cleaned_ckd = re.sub(r"no ckd|without ckd", "", full_context)
            if affirmed_chest(packet):
                held.append("Ibuprofen held (chest pain).")
            elif epigastric or abdominal_territory(str(enc.get("chief_complaint", ""))):
                held.append("Ibuprofen held (abdominal/epigastric/flank pain).")
            elif re.search(r"ckd|chronic kidney|dialysis|esrd|transplant|egfr", cleaned_ckd):
                held.append("Ibuprofen held (CKD/dialysis/transplant history).")
            elif re.search(
                r"apixaban|rivaroxaban|warfarin|dabigatran|enoxaparin|heparin|eliquis|xarelto|coumadin|aspirin|clopidogrel|gi bleed|dark stools",
                full_context,
            ):
                held.append("Ibuprofen held (antithrombotic / bleed risk).")
            else:
                meds.append(str(POLICY_MEDS[1]["label"]))

        if nausea and not any(k in given_lower for k in POLICY_MEDS[2]["keys"]):
            meds.append(str(POLICY_MEDS[2]["label"]))
        if epigastric and not any(k in given_lower for k in POLICY_MEDS[3]["keys"]):
            meds.append(str(POLICY_MEDS[3]["label"]))

        if not meds:
            dec5 = {
                "type": "wr_meds",
                "proposal": "No WR-OK med from policy for the recorded complaint.",
                "rationale": " ".join(held) if held else "No qualifying pain or nausea symptom.",
                "alert": False,
                "value": {"type": "wr_meds", "meds": []},
            }
        else:
            extra = f" {' '.join(held)}" if held else ""
            dec5 = {
                "type": "wr_meds",
                "proposal": f"WR-OK by policy: {', '.join(meds)}.",
                "rationale": f"Standing waiting-room policy list.{extra}",
                "alert": True,
                "value": {"type": "wr_meds", "meds": meds},
            }

    return [dec1, dec2, dec3, dec4, dec5]


# ---------------------------------------------------------------------------
# Delta Contract Validator (mirrors ed-triage-delta-agents/validate_output.py)
# ---------------------------------------------------------------------------

_CAPS = {
    "findings": 3,
    "safety_net": 3,
    "ask_patient": 3,
    "dispo_drivers": 2,
    "missing_or_stale": 4,
}
_WORDS: dict[Any, int] = {
    "priority_reason": 25,
    "headline": 40,
    "framing_check": 30,
    "queue_note": 30,
    ("findings", "finding"): 15,
    ("findings", "why_now"): 25,
    ("findings", "action"): 20,
    ("safety_net", "checks"): 20,
    ("ask_patient", "question"): 20,
    ("dispo_drivers", "driver"): 15,
    ("dispo_drivers", "early_action"): 15,
    ("missing_or_stale", None): 10,
}
_ENUMS: dict[Any, set[str]] = {
    "priority": {"CRITICAL", "HIGH", "ROUTINE", "INSUFFICIENT_DATA"},
    ("findings", "status"): {"NEW", "CHANGED"},
    ("findings", "evidence"): {"DOCUMENTED", "INFERRED", "CONFLICTING", "NEEDS_CONFIRMATION"},
    ("safety_net", "type"): {"PROTOCOL", "PROVIDER_ORDER", "ESCALATE"},
    ("safety_net", "urgency"): {"NOW", "BEFORE_ROOMING", "ROUTINE"},
}


def word_count(text: Any) -> int:
    return len(str(text).split()) if text else 0


def _collect_srcs(node: Any, out: set[str]) -> None:
    if isinstance(node, dict):
        if isinstance(node.get("src"), str):
            out.add(node["src"])
        cols = node.get("cols")
        if isinstance(cols, list) and "src" in cols:
            idx = cols.index("src")
            for row in node.get("rows", []):
                if isinstance(row, list) and idx < len(row) and isinstance(row[idx], str):
                    out.add(row[idx])
        for v in node.values():
            _collect_srcs(v, out)
    elif isinstance(node, list):
        for v in node:
            _collect_srcs(v, out)


def validate_delta_output(inp: dict[str, Any], out: dict[str, Any]) -> list[str]:
    """Validate synthesized DeltaCard output against payload provenance and word caps."""
    errors: list[str] = []
    srcs: set[str] = set()
    _collect_srcs(inp, srcs)
    protocol_ids = {
        p.get("protocol_id") for p in (inp.get("protocols") or []) if isinstance(p, dict)
    }
    finding_ids = {
        f.get("id") for f in (out.get("findings") or []) if isinstance(f, dict) and f.get("id")
    }

    for block, cap in _CAPS.items():
        items = out.get(block) or []
        if len(items) > cap:
            errors.append(f"{block}: {len(items)} items exceeds cap {cap}")

    for spec, limit in _WORDS.items():
        if isinstance(spec, str):
            if word_count(out.get(spec)) > limit:
                errors.append(f"{spec}: {word_count(out.get(spec))} words exceeds {limit}")
            continue
        b_name, f_name = spec
        for idx, item in enumerate(out.get(b_name) or []):
            val = item if f_name is None else item.get(f_name)
            if word_count(val) > limit:
                errors.append(f"{b_name}[{idx}].{f_name or 'item'}: exceeds {limit} words")

    for spec, allowed in _ENUMS.items():
        if isinstance(spec, str):
            if out.get(spec) not in allowed:
                errors.append(f"{spec}: {out.get(spec)!r} not allowed")
            continue
        b_name, f_name = spec
        for idx, item in enumerate(out.get(b_name) or []):
            if item.get(f_name) not in allowed:
                errors.append(f"{b_name}[{idx}].{f_name}: {item.get(f_name)!r} not allowed")

    for idx, f in enumerate(out.get("findings") or []):
        cited = f.get("sources") or []
        if not 1 <= len(cited) <= 4:
            errors.append(f"findings[{idx}]: expected 1-4 sources")
        for s in cited:
            if s not in srcs:
                errors.append(f"findings[{idx}]: unknown src {s!r}")

    ordered = {str(o).lower() for o in (inp.get("encounter", {}).get("triage_orders_placed") or [])}
    for idx, act in enumerate(out.get("safety_net") or []):
        if act.get("type") == "PROTOCOL" and act.get("protocol_id") not in protocol_ids:
            errors.append(f"safety_net[{idx}]: PROTOCOL without valid protocol_id")
        if str(act.get("action", "")).lower() in ordered:
            errors.append(f"safety_net[{idx}]: duplicates existing triage order")

    for b_name in ("safety_net", "ask_patient", "dispo_drivers"):
        for idx, item in enumerate(out.get(b_name) or []):
            ref = item.get("for_finding")
            if ref != "triage" and ref not in finding_ids:
                errors.append(f"{b_name}[{idx}]: for_finding {ref!r} matches no finding")

    queue_nums = {
        str(v)
        for v in (inp.get("queue") or {}).values()
        if isinstance(v, (int, float)) and not isinstance(v, bool)
    }
    stray = set(re.findall(r"\d+(?:\.\d+)?", str(out.get("queue_note") or ""))) - queue_nums
    if stray:
        errors.append(f"queue_note: numbers not in queue: {sorted(stray)}")

    mode = inp.get("mode", "SHADOW")
    if mode == "SHADOW" and "suppressed" not in out:
        errors.append("suppressed: required in SHADOW mode")
    if mode == "PROVIDER_REVIEW" and out.get("suppressed"):
        errors.append("suppressed: must be empty in PROVIDER_REVIEW mode")
    for idx, item in enumerate(out.get("suppressed") or []):
        if item.get("reason") not in {1, 2, 3, 4, "cap"}:
            errors.append(f"suppressed[{idx}]: invalid reason {item.get('reason')!r}")

    return errors


def check_no_phi(obj: Any) -> str | None:
    """Enforce zero-PHI intake boundary (rejects names, DOB, MRN, SSN, email, phone)."""
    if isinstance(obj, str):
        if len(obj) > 2000:
            return "A text field exceeds 2000 characters."
        if re.search(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", obj, re.IGNORECASE):
            return "Packet rejected: text contains an email address (PHI risk)."
        if re.search(r"\b\d{3}-\d{2}-\d{4}\b", obj):
            return "Packet rejected: text matches SSN pattern (PHI risk)."
        if re.search(r"\b(?:\+?1[-.\s])?\(?\d{3}\)?[-.\s]\d{3}[-.\s]\d{4}\b", obj):
            return "Packet rejected: text matches phone number pattern (PHI risk)."
        return None
    if isinstance(obj, list):
        for item in obj:
            err = check_no_phi(item)
            if err:
                return err
        return None
    if isinstance(obj, dict):
        for k, v in obj.items():
            if str(k).lower() in FORBIDDEN_PHI_KEYS:
                return f"Packet rejected: forbidden PHI key '{k}' is present."
            err = check_no_phi(v)
            if err:
                return err
        return None
    return None


def validate_intake_payload(payload: Any) -> tuple[bool, list[dict[str, Any]] | str]:
    """Validate uploaded synthetic JSON packets before entering the queue."""
    err = check_no_phi(payload)
    if err:
        return False, err

    items = (
        payload.get("packets")
        if isinstance(payload, dict) and "packets" in payload
        else (payload if isinstance(payload, list) else [payload])
    )
    if not isinstance(items, list) or not items:
        return False, "Upload must contain at least one synthetic packet."

    validated: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            return False, "Each packet must be a JSON object."
        pid = str(item.get("packet_id", ""))
        if not PACKET_ID_RE.match(pid):
            return False, f"packet_id '{pid}' must match syn-ED-###."
        if "encounter" not in item or "dossier" not in item:
            return False, f"Packet {pid} requires both 'encounter' and 'dossier' blocks."
        item["packet_id"] = "syn-ED-" + pid.split("-")[-1]
        validated.append(item)

    return True, validated


# ---------------------------------------------------------------------------
# 22 Synthetic Seed Packets (syn-ED-001 .. syn-ED-022)
# ---------------------------------------------------------------------------

SEED_PACKETS: list[dict[str, Any]] = [
    {
        "packet_id": "syn-ED-001",
        "encounter": {
            "arrival_mode": "walk-in",
            "triage_status": "ESI-2",
            "chief_complaint": "Chest pressure for forty minutes",
            "nurse_hx_summary": "Pressure at rest with sweating. EKG already done. No ST elevation.",
            "vitals": {"hr": 88, "sbp": 142, "dbp": 86, "rr": 18, "spo2": 98, "temp_c": 36.8},
            "prior_vitals": [
                {
                    "when": "20m ago (door)",
                    "hr": 92,
                    "sbp": 146,
                    "dbp": 88,
                    "rr": 18,
                    "spo2": 98,
                    "temp_c": 36.8,
                }
            ],
            "ekg": {"status": "done", "summary": "NSR, no ST elevation"},
            "evals": {"trauma": "not_indicated", "neuro": "not_indicated", "d2b": "not_indicated"},
            "wr_orders": {"started": False, "sets": [], "addons": []},
            "wr_meds_given": [],
            "arrival_minutes_ago": 22,
            "age_years": 68,
            "sex": "M",
        },
        "dossier": {
            "pmh_summary": "Hypertension and high cholesterol.",
            "surgical_hx_summary": "Cholecystectomy years ago.",
            "social_hx_summary": "Lives with a partner. No tobacco.",
            "last_ed_visits": [{"when": "8 months ago", "summary": "Ankle sprain, discharged."}],
            "last_admissions": [
                {"when": "3 years ago", "summary": "Overnight observation, discharged."}
            ],
            "recent_labs": [{"when": "8 months ago", "test": "Hemoglobin", "value": "13.4 g/dL"}],
            "recent_di": [
                {"when": "8 months ago", "study": "Ankle X-ray", "summary": "No fracture recorded."}
            ],
            "ems_report_summary": None,
            "current_meds": ["Lisinopril", "Atorvastatin"],
        },
    },
    {
        "packet_id": "syn-ED-002",
        "encounter": {
            "arrival_mode": "walk-in",
            "triage_status": "ESI-3",
            "chief_complaint": "Right flank pain and vomiting",
            "nurse_hx_summary": "Colicky right flank pain with vomiting. Able to talk in full sentences.",
            "vitals": {"hr": 84, "sbp": 128, "dbp": 76, "rr": 16, "spo2": 99, "temp_c": 36.8},
            "ekg": {"status": "na", "summary": None},
            "evals": {"trauma": "not_indicated", "neuro": "not_indicated", "d2b": "not_indicated"},
            "wr_orders": {"started": True, "sets": ["Urinalysis", "BMP", "CBC"], "addons": []},
            "wr_meds_given": ["Acetaminophen"],
            "arrival_minutes_ago": 26,
            "age_years": 41,
            "sex": "M",
        },
        "dossier": {
            "pmh_summary": "Kidney stone years ago. No CKD recorded.",
            "surgical_hx_summary": "None recorded.",
            "social_hx_summary": "Works full time. No tobacco.",
            "last_ed_visits": [
                {"when": "2 years ago", "summary": "Flank pain, discharged after labs."}
            ],
            "last_admissions": [],
            "recent_labs": [{"when": "2 years ago", "test": "Creatinine", "value": "0.9 mg/dL"}],
            "recent_di": [
                {
                    "when": "2 years ago",
                    "study": "CT abdomen",
                    "summary": "Uncomplicated 3 mm ureteral stone.",
                }
            ],
            "ems_report_summary": None,
            "current_meds": ["None recorded"],
        },
    },
    {
        "packet_id": "syn-ED-003",
        "encounter": {
            "arrival_mode": "walk-in",
            "triage_status": "ESI-2",
            "chief_complaint": "Syncope at dinner, now awake",
            "nurse_hx_summary": "Brief loss of consciousness, awake on arrival. EKG already done, sinus bradycardia.",
            "vitals": {"hr": 48, "sbp": 108, "dbp": 70, "rr": 16, "spo2": 97, "temp_c": 36.6},
            "prior_vitals": [
                {
                    "when": "14m ago (door)",
                    "hr": 46,
                    "sbp": 104,
                    "dbp": 68,
                    "rr": 16,
                    "spo2": 97,
                    "temp_c": 36.6,
                }
            ],
            "ekg": {"status": "done", "summary": "Sinus bradycardia, no ST elevation"},
            "evals": {"trauma": "not_indicated", "neuro": "not_indicated", "d2b": "not_indicated"},
            "wr_orders": {"started": False, "sets": [], "addons": []},
            "wr_meds_given": [],
            "arrival_minutes_ago": 16,
            "age_years": 70,
            "sex": "M",
        },
        "dossier": {
            "pmh_summary": "CKD stage 3. Hypertension.",
            "surgical_hx_summary": "None recorded.",
            "social_hx_summary": "Lives alone. No tobacco.",
            "last_ed_visits": [{"when": "4 months ago", "summary": "Dizziness, discharged."}],
            "last_admissions": [],
            "recent_labs": [{"when": "1 month ago", "test": "Creatinine", "value": "1.8 mg/dL"}],
            "recent_di": [],
            "ems_report_summary": None,
            "current_meds": ["Amlodipine"],
        },
    },
    {
        "packet_id": "syn-ED-004",
        "encounter": {
            "arrival_mode": "ems",
            "triage_status": "ESI-3",
            "chief_complaint": "Shortness of breath, worse today",
            "nurse_hx_summary": "Speaking in short phrases. SpO2 88% on 2 L. EKG already done, sinus tachycardia.",
            "vitals": {"hr": 112, "sbp": 142, "dbp": 80, "rr": 28, "spo2": 88, "temp_c": 37.2},
            "prior_vitals": [
                {
                    "when": "18m ago (EMS)",
                    "hr": 116,
                    "sbp": 148,
                    "dbp": 84,
                    "rr": 30,
                    "spo2": 86,
                    "temp_c": 37.1,
                }
            ],
            "ekg": {"status": "done", "summary": "Sinus tachycardia, no ST elevation"},
            "evals": {"trauma": "not_indicated", "neuro": "not_indicated", "d2b": "not_indicated"},
            "wr_orders": {"started": False, "sets": [], "addons": []},
            "wr_meds_given": [],
            "arrival_minutes_ago": 20,
            "age_years": 64,
            "sex": "F",
        },
        "dossier": {
            "pmh_summary": "COPD. Prior intubation 2 years ago for hypercapnic respiratory failure. Home oxygen at night.",
            "surgical_hx_summary": "None recorded.",
            "social_hx_summary": "Former tobacco. Lives with family.",
            "last_ed_visits": [{"when": "5 weeks ago", "summary": "COPD flare, discharged."}],
            "last_admissions": [
                {"when": "1 year ago", "summary": "COPD flare, two nights, discharged."}
            ],
            "recent_labs": [{"when": "5 weeks ago", "test": "Hemoglobin", "value": "12.1 g/dL"}],
            "recent_di": [
                {"when": "5 weeks ago", "study": "Chest X-ray", "summary": "No new infiltrate."}
            ],
            "ems_report_summary": "Crew started 2 L oxygen.",
            "current_meds": ["Tiotropium", "Albuterol"],
        },
    },
    {
        "packet_id": "syn-ED-005",
        "encounter": {
            "arrival_mode": "walk-in",
            "triage_status": "ESI-3",
            "chief_complaint": "Epigastric pain since last night",
            "nurse_hx_summary": "Burning epigastric pain with nausea. EKG already done. No ST elevation.",
            "vitals": {"hr": 84, "sbp": 136, "dbp": 82, "rr": 16, "spo2": 98, "temp_c": 36.9},
            "ekg": {"status": "done", "summary": "NSR, no ST elevation"},
            "evals": {"trauma": "not_indicated", "neuro": "not_indicated", "d2b": "not_indicated"},
            "wr_orders": {"started": False, "sets": [], "addons": []},
            "wr_meds_given": [],
            "arrival_minutes_ago": 30,
            "age_years": 54,
            "sex": "M",
        },
        "dossier": {
            "pmh_summary": "Reflux. No ulcer history recorded.",
            "surgical_hx_summary": "None recorded.",
            "social_hx_summary": "No tobacco. Occasional weekend alcohol.",
            "last_ed_visits": [],
            "last_admissions": [],
            "recent_labs": [{"when": "1 year ago", "test": "Lipase", "value": "34 U/L"}],
            "recent_di": [
                {"when": "1 year ago", "study": "RUQ ultrasound", "summary": "No gallstones."}
            ],
            "ems_report_summary": None,
            "current_meds": ["Omeprazole"],
        },
    },
    {
        "packet_id": "syn-ED-006",
        "encounter": {
            "arrival_mode": "walk-in",
            "triage_status": "ESI-3",
            "chief_complaint": "Dizziness since this morning",
            "nurse_hx_summary": "Room spinning with a mild headache. EKG was not obtained at triage.",
            "vitals": {"hr": 78, "sbp": 130, "dbp": 78, "rr": 16, "spo2": 99, "temp_c": 36.6},
            "ekg": {"status": "missing", "summary": None},
            "evals": {"trauma": "not_indicated", "neuro": "not_indicated", "d2b": "not_indicated"},
            "wr_orders": {"started": False, "sets": [], "addons": []},
            "wr_meds_given": [],
            "arrival_minutes_ago": 28,
            "age_years": 48,
            "sex": "F",
            "outside_hie_available": False,
        },
        "dossier": {
            "pmh_summary": "Migraine. No stroke history recorded.",
            "surgical_hx_summary": "None recorded.",
            "social_hx_summary": "Lives with family. No tobacco.",
            "last_ed_visits": [{"when": "3 months ago", "summary": "Headache, discharged."}],
            "last_admissions": [],
            "recent_labs": [{"when": "3 months ago", "test": "Glucose", "value": "96 mg/dL"}],
            "recent_di": [],
            "ems_report_summary": None,
            "current_meds": ["Sumatriptan"],
        },
    },
    {
        "packet_id": "syn-ED-007",
        "encounter": {
            "arrival_mode": "ems",
            "triage_status": "ESI-2",
            "chief_complaint": "High-speed MVC with chest and neck pain",
            "nurse_hx_summary": "Driver, airbag deployed. Trauma and neuro evals were completed at the door. EKG already done.",
            "vitals": {"hr": 104, "sbp": 118, "dbp": 74, "rr": 18, "spo2": 97, "temp_c": 36.7},
            "ekg": {"status": "done", "summary": "Sinus tachycardia, no ST elevation"},
            "evals": {"trauma": "done", "neuro": "done", "d2b": "not_indicated"},
            "wr_orders": {"started": True, "sets": ["Trauma panel"], "addons": []},
            "wr_meds_given": [],
            "arrival_minutes_ago": 9,
            "age_years": 40,
            "sex": "M",
        },
        "dossier": {
            "pmh_summary": "None recorded.",
            "surgical_hx_summary": "None recorded.",
            "social_hx_summary": "Restrained driver per EMS.",
            "last_ed_visits": [],
            "last_admissions": [],
            "recent_labs": [],
            "recent_di": [],
            "ems_report_summary": "High-speed collision. Awake on scene. Cervical collar placed.",
            "current_meds": ["None recorded"],
        },
    },
    {
        "packet_id": "syn-ED-008",
        "encounter": {
            "arrival_mode": "ems",
            "triage_status": "ESI-3",
            "chief_complaint": "Vomiting after a ground-level fall",
            "nurse_hx_summary": "First looked minor at the door. Now vomiting, pale, and SBP is 78. Trauma eval was not done.",
            "vitals": {"hr": 128, "sbp": 78, "dbp": 50, "rr": 24, "spo2": 96, "temp_c": 36.6},
            "prior_vitals": [
                {
                    "when": "7m ago (door)",
                    "hr": 118,
                    "sbp": 92,
                    "dbp": 58,
                    "rr": 22,
                    "spo2": 96,
                    "temp_c": 36.6,
                }
            ],
            "ekg": {"status": "na", "summary": None},
            "evals": {"trauma": "missing", "neuro": "not_indicated", "d2b": "not_indicated"},
            "wr_orders": {"started": False, "sets": [], "addons": []},
            "wr_meds_given": [],
            "arrival_minutes_ago": 8,
            "age_years": 78,
            "sex": "F",
        },
        "dossier": {
            "pmh_summary": "Hypertension. Takes daily aspirin.",
            "surgical_hx_summary": "Hip repair years ago.",
            "social_hx_summary": "Lives with family. Uses a cane.",
            "last_ed_visits": [{"when": "6 months ago", "summary": "Mechanical fall, discharged."}],
            "last_admissions": [{"when": "6 years ago", "summary": "Hip repair."}],
            "recent_labs": [{"when": "6 months ago", "test": "Hemoglobin", "value": "11.6 g/dL"}],
            "recent_di": [{"when": "6 months ago", "study": "Hip X-ray", "summary": "Old repair."}],
            "ems_report_summary": "Ground-level fall. Awake on scene. No collar placed.",
            "current_meds": ["Aspirin", "Amlodipine"],
        },
    },
    {
        "packet_id": "syn-ED-009",
        "encounter": {
            "arrival_mode": "ems",
            "triage_status": "ESI-2",
            "chief_complaint": "Sudden right arm weakness",
            "nurse_hx_summary": "Weakness began suddenly. Neuro eval was completed at arrival. Last known well 40 minutes prior.",
            "vitals": {"hr": 78, "sbp": 168, "dbp": 94, "rr": 18, "spo2": 97, "temp_c": 36.6},
            "ekg": {"status": "na", "summary": None},
            "evals": {"trauma": "not_indicated", "neuro": "done", "d2b": "not_indicated"},
            "wr_orders": {"started": True, "sets": ["Glucose"], "addons": []},
            "wr_meds_given": [],
            "arrival_minutes_ago": 14,
            "age_years": 66,
            "sex": "M",
        },
        "dossier": {
            "pmh_summary": "Hypertension. No prior stroke recorded.",
            "surgical_hx_summary": "None recorded.",
            "social_hx_summary": "Former tobacco.",
            "last_ed_visits": [],
            "last_admissions": [],
            "recent_labs": [{"when": "4 months ago", "test": "LDL", "value": "148 mg/dL"}],
            "recent_di": [
                {"when": "2 years ago", "study": "Head CT", "summary": "No acute finding."}
            ],
            "ems_report_summary": "Sudden arm weakness. Scene glucose 112 mg/dL.",
            "current_meds": ["Lisinopril"],
        },
    },
    {
        "packet_id": "syn-ED-010",
        "encounter": {
            "arrival_mode": "walk-in",
            "triage_status": "ESI-3",
            "chief_complaint": "Slurred speech",
            "nurse_hx_summary": "Slurred speech began about one hour ago. Neuro eval was not done at arrival.",
            "vitals": {"hr": 82, "sbp": 168, "dbp": 96, "rr": 16, "spo2": 98, "temp_c": 36.7},
            "ekg": {"status": "na", "summary": None},
            "evals": {"trauma": "not_indicated", "neuro": "missing", "d2b": "not_indicated"},
            "wr_orders": {"started": False, "sets": [], "addons": []},
            "wr_meds_given": [],
            "arrival_minutes_ago": 18,
            "age_years": 71,
            "sex": "F",
        },
        "dossier": {
            "pmh_summary": "Hypertension. Atrial fibrillation.",
            "surgical_hx_summary": "None recorded.",
            "social_hx_summary": "Lives with family.",
            "last_ed_visits": [{"when": "9 months ago", "summary": "Nosebleed, discharged."}],
            "last_admissions": [],
            "recent_labs": [{"when": "2 months ago", "test": "INR", "value": "2.4"}],
            "recent_di": [],
            "ems_report_summary": None,
            "current_meds": ["Apixaban", "Metoprolol"],
        },
    },
    {
        "packet_id": "syn-ED-011",
        "encounter": {
            "arrival_mode": "walk-in",
            "triage_status": "ESI-2",
            "chief_complaint": "Chest pain at rest",
            "nurse_hx_summary": "Pressure started at rest. EKG already done and shows ST elevation. D2B eval has not been done.",
            "vitals": {"hr": 96, "sbp": 138, "dbp": 84, "rr": 18, "spo2": 98, "temp_c": 36.7},
            "ekg": {"status": "done", "summary": "ST elevation in V2-V4"},
            "evals": {"trauma": "not_indicated", "neuro": "not_indicated", "d2b": "missing"},
            "wr_orders": {"started": False, "sets": [], "addons": []},
            "wr_meds_given": [],
            "arrival_minutes_ago": 12,
            "age_years": 58,
            "sex": "M",
        },
        "dossier": {
            "pmh_summary": "Hypertension. Diabetes.",
            "surgical_hx_summary": "None recorded.",
            "social_hx_summary": "Tobacco daily.",
            "last_ed_visits": [{"when": "1 year ago", "summary": "Chest pain, negative workup."}],
            "last_admissions": [],
            "recent_labs": [{"when": "1 year ago", "test": "Troponin", "value": "Normal"}],
            "recent_di": [
                {"when": "1 year ago", "study": "Chest X-ray", "summary": "No acute finding."}
            ],
            "ems_report_summary": None,
            "current_meds": ["Metformin", "Lisinopril"],
        },
    },
    {
        "packet_id": "syn-ED-012",
        "encounter": {
            "arrival_mode": "walk-in",
            "triage_status": "ESI-4",
            "chief_complaint": "Forearm laceration, bleeding controlled",
            "nurse_hx_summary": "Small superficial forearm laceration from clean kitchen knife. Bleeding controlled. Distal neurovascular exam intact.",
            "vitals": {"hr": 74, "sbp": 122, "dbp": 70, "rr": 14, "spo2": 99, "temp_c": 36.5},
            "ekg": {"status": "na", "summary": None},
            "evals": {"trauma": "not_indicated", "neuro": "not_indicated", "d2b": "not_indicated"},
            "wr_orders": {
                "started": True,
                "sets": ["Tetanus review", "Wound X-ray / irrigation"],
                "addons": [],
            },
            "wr_meds_given": ["Acetaminophen"],
            "arrival_minutes_ago": 80,
            "age_years": 19,
            "sex": "M",
        },
        "dossier": {
            "pmh_summary": "None recorded.",
            "surgical_hx_summary": "None recorded.",
            "social_hx_summary": "College student. No tobacco.",
            "last_ed_visits": [],
            "last_admissions": [],
            "recent_labs": [],
            "recent_di": [],
            "ems_report_summary": None,
            "current_meds": ["None recorded"],
        },
    },
    {
        "packet_id": "syn-ED-013",
        "encounter": {
            "arrival_mode": "walk-in",
            "triage_status": "ESI-3",
            "chief_complaint": "Flank pain",
            "nurse_hx_summary": "Right flank pain since 05:00, radiating to groin, nausea x1. Denies fever or dysuria. Looks uncomfortable, pacing. Repeat HR 104 -> 108.",
            "vitals": {"hr": 108, "sbp": 102, "dbp": 64, "rr": 18, "spo2": 98, "temp_c": 37.3},
            "prior_vitals": [
                {
                    "when": "15m ago (door)",
                    "hr": 104,
                    "sbp": 106,
                    "dbp": 68,
                    "rr": 18,
                    "spo2": 98,
                    "temp_c": 37.2,
                }
            ],
            "ekg": {"status": "na", "summary": None},
            "evals": {"trauma": "not_indicated", "neuro": "not_indicated", "d2b": "not_indicated"},
            "wr_orders": {"started": True, "sets": ["CBC", "BMP", "Urinalysis"], "addons": []},
            "wr_meds_given": [],
            "arrival_minutes_ago": 17,
            "age_years": 65,
            "sex": "F",
        },
        "dossier": {
            "pmh_summary": "Non-valvular atrial fibrillation. CKD stage 3b. Infrarenal abdominal aortic aneurysm 4.4 cm. Remote GI bleed.",
            "surgical_hx_summary": "Laparoscopic cholecystectomy.",
            "social_hx_summary": "Lives alone. Former smoker.",
            "last_ed_visits": [
                {
                    "when": "2 days ago",
                    "summary": "Right flank and lower back pain, diagnosed musculoskeletal strain, discharged on cyclobenzaprine.",
                }
            ],
            "last_admissions": [
                {"when": "14 months ago", "summary": "Upper GI bleed on apixaban + NSAIDs."}
            ],
            "recent_labs": [
                {"when": "2 days ago", "test": "Creatinine", "value": "1.9 mg/dL"},
                {"when": "2 days ago", "test": "Hemoglobin", "value": "11.2 g/dL"},
            ],
            "recent_di": [
                {
                    "when": "11 months ago",
                    "study": "Retroperitoneal ultrasound",
                    "summary": "Infrarenal abdominal aortic aneurysm measuring 4.4 cm.",
                }
            ],
            "ems_report_summary": None,
            "current_meds": ["Apixaban", "Metoprolol", "Lisinopril", "Cyclobenzaprine"],
        },
    },
    {
        "packet_id": "syn-ED-014",
        "encounter": {
            "arrival_mode": "walk-in",
            "triage_status": "ESI-3",
            "chief_complaint": "Twisted left ankle stepping off curb",
            "nurse_hx_summary": "Isolated inversion injury to left ankle 2 hours ago. Bearing weight slowly, no head strike, no midline tenderness.",
            "vitals": {"hr": 72, "sbp": 124, "dbp": 76, "rr": 14, "spo2": 99, "temp_c": 36.6},
            "ekg": {"status": "na", "summary": None},
            "evals": {"trauma": "not_indicated", "neuro": "not_indicated", "d2b": "not_indicated"},
            "wr_orders": {"started": False, "sets": [], "addons": []},
            "wr_meds_given": [],
            "arrival_minutes_ago": 32,
            "age_years": 28,
            "sex": "M",
        },
        "dossier": {
            "pmh_summary": "None recorded.",
            "surgical_hx_summary": "None recorded.",
            "social_hx_summary": "Teacher. Runs recreationally. No tobacco.",
            "last_ed_visits": [],
            "last_admissions": [],
            "recent_labs": [],
            "recent_di": [],
            "ems_report_summary": None,
            "current_meds": ["None recorded"],
        },
    },
    {
        "packet_id": "syn-ED-015",
        "encounter": {
            "arrival_mode": "walk-in",
            "triage_status": "ESI-3",
            "chief_complaint": "Crampy lower abdominal pain since this morning",
            "nurse_hx_summary": "Crampy lower abdominal pain. Soft abdomen at triage. Tolerating sips. No fever recorded.",
            "vitals": {"hr": 96, "sbp": 118, "dbp": 74, "rr": 16, "spo2": 99, "temp_c": 37.2},
            "ekg": {"status": "na", "summary": None},
            "evals": {"trauma": "not_indicated", "neuro": "not_indicated", "d2b": "not_indicated"},
            "wr_orders": {"started": False, "sets": [], "addons": []},
            "wr_meds_given": [],
            "arrival_minutes_ago": 40,
            "age_years": 34,
            "sex": "F",
            "outside_hie_available": False,
        },
        "dossier": {
            "pmh_summary": "None recorded.",
            "surgical_hx_summary": "None recorded.",
            "social_hx_summary": "Lives with partner. No tobacco.",
            "last_ed_visits": [
                {"when": "1 day ago", "summary": "Lower abdominal pain, discharged after labs."}
            ],
            "last_admissions": [],
            "recent_labs": [{"when": "1 day ago", "test": "WBC", "value": "14.2 x10^9/L"}],
            "recent_di": [
                {"when": "1 day ago", "study": "CT abdomen", "summary": "No appendicitis recorded."}
            ],
            "ems_report_summary": None,
            "current_meds": ["None recorded"],
        },
    },
    {
        "packet_id": "syn-ED-016",
        "encounter": {
            "arrival_mode": "walk-in",
            "triage_status": "ESI-3",
            "chief_complaint": "Low back pain after lifting a box",
            "nurse_hx_summary": "Started after lifting a box this morning. Ambulates slowly, looks like routine strain. Denies blood thinners.",
            "vitals": {"hr": 84, "sbp": 134, "dbp": 78, "rr": 16, "spo2": 98, "temp_c": 36.7},
            "ekg": {"status": "na", "summary": None},
            "evals": {"trauma": "not_indicated", "neuro": "not_indicated", "d2b": "not_indicated"},
            "wr_orders": {"started": False, "sets": [], "addons": []},
            "wr_meds_given": [],
            "arrival_minutes_ago": 34,
            "age_years": 71,
            "sex": "M",
        },
        "dossier": {
            "pmh_summary": "Atrial fibrillation.",
            "surgical_hx_summary": "None recorded.",
            "social_hx_summary": "Lives with partner. No tobacco.",
            "last_ed_visits": [],
            "last_admissions": [],
            "recent_labs": [],
            "recent_di": [],
            "ems_report_summary": None,
            "current_meds": ["Apixaban", "Metoprolol"],
        },
    },
    {
        "packet_id": "syn-ED-017",
        "encounter": {
            "arrival_mode": "walk-in",
            "triage_status": "ESI-3",
            "chief_complaint": "Dizziness and fatigue since yesterday",
            "nurse_hx_summary": "Lightheaded and tired since yesterday. Walking. No chest pain. No syncope.",
            "vitals": {"hr": 108, "sbp": 108, "dbp": 68, "rr": 18, "spo2": 98, "temp_c": 36.6},
            "prior_vitals": [
                {
                    "when": "20m ago (door)",
                    "hr": 106,
                    "sbp": 110,
                    "dbp": 70,
                    "rr": 18,
                    "spo2": 98,
                    "temp_c": 36.6,
                }
            ],
            "ekg": {"status": "missing", "summary": None},
            "evals": {"trauma": "not_indicated", "neuro": "not_indicated", "d2b": "not_indicated"},
            "wr_orders": {"started": False, "sets": [], "addons": []},
            "wr_meds_given": [],
            "arrival_minutes_ago": 26,
            "age_years": 58,
            "sex": "F",
        },
        "dossier": {
            "pmh_summary": "None recorded.",
            "surgical_hx_summary": "None recorded.",
            "social_hx_summary": "Lives with family. No tobacco.",
            "last_ed_visits": [{"when": "6 weeks ago", "summary": "Dark stools, discharged."}],
            "last_admissions": [],
            "recent_labs": [{"when": "5 days ago", "test": "Hemoglobin", "value": "7.1 g/dL"}],
            "recent_di": [],
            "ems_report_summary": None,
            "current_meds": ["Ibuprofen"],
        },
    },
    {
        "packet_id": "syn-ED-018",
        "encounter": {
            "arrival_mode": "walk-in",
            "triage_status": "ESI-4",
            "chief_complaint": "Tiny puncture on right index finger from hydraulic sprayer",
            "nurse_hx_summary": "Cleaning a pressure-injected oil line in shop 45 minutes ago. Tiny pinpoint entry on volar index finger, minimal bleeding, looks benign at triage.",
            "vitals": {"hr": 84, "sbp": 132, "dbp": 80, "rr": 16, "spo2": 99, "temp_c": 36.7},
            "ekg": {"status": "na", "summary": None},
            "evals": {"trauma": "not_indicated", "neuro": "not_indicated", "d2b": "not_indicated"},
            "wr_orders": {"started": False, "sets": [], "addons": []},
            "wr_meds_given": [],
            "arrival_minutes_ago": 19,
            "age_years": 36,
            "sex": "M",
        },
        "dossier": {
            "pmh_summary": "None recorded.",
            "surgical_hx_summary": "Appendectomy.",
            "social_hx_summary": "Heavy equipment mechanic. No tobacco.",
            "last_ed_visits": [],
            "last_admissions": [],
            "recent_labs": [],
            "recent_di": [],
            "ems_report_summary": None,
            "current_meds": ["None recorded"],
        },
    },
    {
        "packet_id": "syn-ED-019",
        "encounter": {
            "arrival_mode": "walk-in",
            "triage_status": "ESI-4",
            "chief_complaint": "Mild left eye irritation after grinding metal",
            "nurse_hx_summary": "Felt a high-speed metal fleck hit left eye while angle-grinding without goggles 2 hours ago. Vision slightly blurry, minimal redness.",
            "vitals": {"hr": 76, "sbp": 126, "dbp": 76, "rr": 14, "spo2": 99, "temp_c": 36.6},
            "ekg": {"status": "na", "summary": None},
            "evals": {"trauma": "not_indicated", "neuro": "not_indicated", "d2b": "not_indicated"},
            "wr_orders": {"started": False, "sets": [], "addons": []},
            "wr_meds_given": [],
            "arrival_minutes_ago": 24,
            "age_years": 29,
            "sex": "M",
        },
        "dossier": {
            "pmh_summary": "None recorded.",
            "surgical_hx_summary": "None recorded.",
            "social_hx_summary": "Metal fabricator. No tobacco.",
            "last_ed_visits": [],
            "last_admissions": [],
            "recent_labs": [],
            "recent_di": [],
            "ems_report_summary": None,
            "current_meds": ["None recorded"],
        },
    },
    {
        "packet_id": "syn-ED-020",
        "encounter": {
            "arrival_mode": "walk-in",
            "triage_status": "ESI-3",
            "chief_complaint": "Fatigue and mild cough for two days",
            "nurse_hx_summary": "Low-grade fatigue and dry cough. Speaking in full sentences. Temp 37.9C at door, repeat HR 114 -> 118.",
            "vitals": {"hr": 118, "sbp": 106, "dbp": 66, "rr": 20, "spo2": 95, "temp_c": 37.9},
            "prior_vitals": [
                {
                    "when": "25m ago (door)",
                    "hr": 114,
                    "sbp": 112,
                    "dbp": 70,
                    "rr": 20,
                    "spo2": 96,
                    "temp_c": 37.8,
                }
            ],
            "ekg": {"status": "missing", "summary": None},
            "evals": {"trauma": "not_indicated", "neuro": "not_indicated", "d2b": "not_indicated"},
            "wr_orders": {"started": False, "sets": [], "addons": []},
            "wr_meds_given": [],
            "arrival_minutes_ago": 29,
            "age_years": 52,
            "sex": "F",
        },
        "dossier": {
            "pmh_summary": "Renal transplant 6 years ago (allograft removed 1 year ago, now on hemodialysis MWF). HFrEF with EF 28%. Prior cardiac arrest with CPR 3 years ago.",
            "surgical_hx_summary": "Renal transplant and subsequent transplant nephrectomy. AICD placement.",
            "social_hx_summary": "Lives with spouse. No tobacco.",
            "last_ed_visits": [
                {"when": "3 days ago", "summary": "Fatigue after dialysis, discharged."},
                {"when": "10 days ago", "summary": "Mild dyspnea, dialyzed and discharged."},
                {"when": "3 weeks ago", "summary": "Vascular access check, discharged."},
            ],
            "last_admissions": [
                {"when": "4 months ago", "summary": "Bacteremia and volume overload, ICU stay."}
            ],
            "recent_labs": [
                {"when": "3 days ago", "test": "Creatinine", "value": "5.8 mg/dL"},
                {"when": "3 days ago", "test": "Potassium", "value": "5.4 mEq/L"},
            ],
            "recent_di": [
                {"when": "2 months ago", "study": "Echocardiogram", "summary": "LVEF 28%."}
            ],
            "ems_report_summary": None,
            "current_meds": ["Prednisone", "Tacrolimus", "Carvedilol", "Apixaban"],
        },
    },
    {
        "packet_id": "syn-ED-021",
        "encounter": {
            "arrival_mode": "walk-in",
            "triage_status": "ESI-3",
            "chief_complaint": "Mid-back ache for three days",
            "nurse_hx_summary": "Dull mid-thoracic back ache. Frequent visitor, ambulatory, looks comfortable. Denies trauma. Repeat HR 112 -> 116.",
            "vitals": {"hr": 116, "sbp": 114, "dbp": 70, "rr": 18, "spo2": 98, "temp_c": 37.6},
            "prior_vitals": [
                {
                    "when": "22m ago (door)",
                    "hr": 112,
                    "sbp": 118,
                    "dbp": 72,
                    "rr": 18,
                    "spo2": 98,
                    "temp_c": 37.5,
                }
            ],
            "ekg": {"status": "na", "summary": None},
            "evals": {"trauma": "not_indicated", "neuro": "not_indicated", "d2b": "not_indicated"},
            "wr_orders": {"started": False, "sets": [], "addons": []},
            "wr_meds_given": [],
            "arrival_minutes_ago": 31,
            "age_years": 38,
            "sex": "M",
        },
        "dossier": {
            "pmh_summary": "Alcoholic cirrhosis with mild coagulopathy. History of IVDU.",
            "surgical_hx_summary": "Forearm abscess I&D 1 year ago.",
            "social_hx_summary": "Active injection drug use (IVDU) and heavy alcohol use.",
            "last_ed_visits": [
                {"when": "2 days ago", "summary": "Back ache, ketorolac given and discharged."},
                {"when": "5 days ago", "summary": "Back ache, X-ray negative, discharged."},
                {"when": "9 days ago", "summary": "Arm cellulitis, oral antibiotics."},
            ],
            "last_admissions": [],
            "recent_labs": [
                {"when": "2 days ago", "test": "INR", "value": "1.7"},
                {"when": "2 days ago", "test": "WBC", "value": "13.8 x10^9/L"},
            ],
            "recent_di": [
                {"when": "5 days ago", "study": "Thoracic spine X-ray", "summary": "No fracture."}
            ],
            "ems_report_summary": None,
            "current_meds": ["Thiamine", "Folic acid"],
        },
    },
    {
        "packet_id": "syn-ED-022",
        "encounter": {
            "arrival_mode": "walk-in",
            "triage_status": "ESI-3",
            "chief_complaint": "Fall at home with left wrist pain",
            "nurse_hx_summary": "Tripped at home this morning, possible head strike. Smells of alcohol, states 2 beers last night. Denies blood thinners. More drowsy on reassessment, GCS 15 to 14.",
            "vitals": {
                "hr": 108,
                "sbp": 96,
                "dbp": 58,
                "rr": 20,
                "spo2": 95,
                "temp_c": 37.9,
                "gcs": 14,
            },
            "prior_vitals": [
                {
                    "when": "98m ago (triage)",
                    "hr": 96,
                    "sbp": 104,
                    "dbp": 62,
                    "rr": 18,
                    "spo2": 96,
                    "temp_c": 37.9,
                    "gcs": 15,
                }
            ],
            "ekg": {"status": "na", "summary": None},
            "evals": {"trauma": "not_indicated", "neuro": "not_indicated", "d2b": "not_indicated"},
            "wr_orders": {"started": True, "sets": ["Wrist X-ray"], "addons": []},
            "wr_meds_given": [],
            "arrival_minutes_ago": 142,
            "age_years": 74,
            "sex": "F",
            "isolation_flag": "MRSA",
            "outside_hie_available": False,
        },
        "dossier": {
            "pmh_summary": "Atrial fibrillation. CKD stage 3b. Type 2 diabetes. Alcohol use disorder with prior withdrawal seizure.",
            "surgical_hx_summary": "None recorded.",
            "social_hx_summary": "Lives alone. Active MRSA colonization flag.",
            "last_ed_visits": [
                {
                    "when": "5 days ago",
                    "summary": "UTI and glucose 58 mg/dL on glipizide; discharged on cephalexin 500 mg QID with urine culture pending.",
                }
            ],
            "last_admissions": [],
            "recent_labs": [
                {"when": "5 days ago", "test": "eGFR", "value": "33 mL/min"},
                {"when": "5 days ago", "test": "Glucose", "value": "58 mg/dL"},
                {
                    "when": "3 days ago (unreviewed)",
                    "test": "Urine culture",
                    "value": "ESBL E. coli (>100k CFU), resistant to cephalexin",
                },
            ],
            "recent_di": [],
            "ems_report_summary": None,
            "current_meds": ["Apixaban", "Glipizide", "Metoprolol", "Cephalexin"],
        },
    },
]

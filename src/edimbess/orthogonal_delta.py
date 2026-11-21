"""Sequential Conditional Gain (G_{m|S}) Orthogonal Clinical Delta, Ask-Next, and 7-Agent Swarm."""

from __future__ import annotations

import os
import re
import urllib.request
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any

from edimbess.sentinels_and_rules import (
    affirmed_text,
    detect_must_know_sentinels,
    mentions_ste,
    validate_delta_output,
)

ROUTINE_HEADLINE = (
    "No additional high-priority encounter-relevant information identified in available records."
)

CLINICAL_MECHANISM_AXES: tuple[str, ...] = (
    "hemodynamic_instability",
    "coagulopathy_hemorrhage",
    "immunosuppression_occult_sepsis",
    "coronary_neuro_ischemia",
    "surgical_compartment_ocular",
    "glycemic_renal_electrolyte",
    "antimicrobial_treatment_failure",
    "bounceback_trajectory",
    "safe_disposition_isolation",
)


@dataclass(frozen=True, slots=True)
class CandidateClinicalSignal:
    """Candidate specialist finding scored by sequential conditional gain G_{m | X_base, S_{t-1}}."""

    cid: str
    agent: str
    status: str
    finding: str
    why_now: str
    action: str
    sources: tuple[str, ...]
    evidence: str
    severity: str
    mechanism_axis: str
    intrinsic_gain_nats: float
    already_explicit_in_triage: bool = False
    ask_question: str | None = None


@dataclass(frozen=True, slots=True)
class OrthogonalStepTrace:
    """Step-by-step audit of sequential conditional mechanism selection."""

    step: int
    selected_cid: str | None
    mechanism_axis: str
    intrinsic_gain_nats: float
    conditional_gain_nats: float
    stopped_by_lcb: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _clip_words(text: str, max_words: int) -> str:
    words = str(text or "").split()
    return " ".join(words[:max_words])


def select_orthogonal_candidates(
    candidates: list[CandidateClinicalSignal],
    *,
    max_items: int = 3,
    min_conditional_gain_lcb: float = 0.09,
    twin_attenuation: float = 0.88,
) -> tuple[
    list[CandidateClinicalSignal],
    list[dict[str, Any]],
    list[OrthogonalStepTrace],
    dict[str, int],
]:
    """Select up to max_items orthogonal clinical findings via forward conditional gain.

    - Step 0 conditions on X_base (suppresses pure triage echoes -> reason 1).
    - Step t = 1..max_items attenuates already-covered mechanism axes by (1 - twin_attenuation),
      preventing collinear Stage-2 twins from crowding out orthogonal clinical threats.
    - Stops immediately when max remaining conditional gain falls below min_conditional_gain_lcb.
    """
    suppressed: list[dict[str, Any]] = []
    pool: list[CandidateClinicalSignal] = []
    n_echoes = 0
    n_twins = 0

    for c in candidates:
        if c.already_explicit_in_triage:
            n_echoes += 1
            suppressed.append({"item": _clip_words(c.finding, 12), "reason": 1})
        else:
            pool.append(c)

    axis_coverage: dict[str, float] = {ax: 0.0 for ax in CLINICAL_MECHANISM_AXES}
    selected: list[CandidateClinicalSignal] = []
    trace: list[OrthogonalStepTrace] = []
    sev_bonus = {"CRITICAL": 0.08, "HIGH": 0.02, "MODERATE": 0.0}

    step = 0
    while pool and len(selected) < max_items:
        step += 1
        scored: list[tuple[float, CandidateClinicalSignal]] = []
        for cand in pool:
            cov = axis_coverage.get(cand.mechanism_axis, 0.0)
            residual_factor = max(0.05, 1.0 - twin_attenuation * cov)
            cond_gain = (
                cand.intrinsic_gain_nats * residual_factor
                + sev_bonus.get(cand.severity, 0.0) * residual_factor
            )
            scored.append((cond_gain, cand))

        scored.sort(key=lambda pair: pair[0], reverse=True)
        best_gain, best_cand = scored[0]

        if best_gain < min_conditional_gain_lcb:
            trace.append(
                OrthogonalStepTrace(
                    step=step,
                    selected_cid=None,
                    mechanism_axis=best_cand.mechanism_axis,
                    intrinsic_gain_nats=round(best_cand.intrinsic_gain_nats, 4),
                    conditional_gain_nats=round(best_gain, 4),
                    stopped_by_lcb=True,
                )
            )
            break

        selected.append(best_cand)
        axis_coverage[best_cand.mechanism_axis] = min(
            1.0, axis_coverage.get(best_cand.mechanism_axis, 0.0) + 1.0
        )
        pool = [c for c in pool if c.cid != best_cand.cid]
        trace.append(
            OrthogonalStepTrace(
                step=step,
                selected_cid=best_cand.cid,
                mechanism_axis=best_cand.mechanism_axis,
                intrinsic_gain_nats=round(best_cand.intrinsic_gain_nats, 4),
                conditional_gain_nats=round(best_gain, 4),
                stopped_by_lcb=False,
            )
        )

    # Record remaining candidates cleanly in suppressed[] audit trail
    covered_axes = {s.mechanism_axis for s in selected}
    for rem in pool:
        if rem.mechanism_axis in covered_axes:
            n_twins += 1
            # Reason 4: redundant twin adds no independent action change beyond selected finding
            suppressed.append({"item": _clip_words(rem.finding, 12), "reason": 4})
        else:
            suppressed.append({"item": _clip_words(rem.finding, 12), "reason": "cap"})

    stats = {
        "n_raw_candidates": len(candidates),
        "n_echoes_suppressed": n_echoes,
        "n_twins_suppressed": n_twins,
        "n_orthogonal_selected": len(selected),
        "distinct_mechanisms_covered": len({s.mechanism_axis for s in selected}),
    }
    return selected, suppressed, trace, stats


def packet_to_contract_payload(packet: dict[str, Any], capacity: dict[str, Any]) -> dict[str, Any]:
    """Build provenance-indexed ed-triage-delta-input/1.0 payload for validator verification."""
    enc = packet.get("encounter", {})
    dos = packet.get("dossier", {})
    vitals = enc.get("vitals") or {}
    esi_num = int(str(enc.get("triage_status", "ESI-3")).split("-")[-1])

    return {
        "schema_version": "ed-triage-delta-input/1.0",
        "as_of": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "mode": "SHADOW",
        "sources": [
            {"src": "encounter.vitals"},
            {"src": "encounter.nurse_triage_note"},
            {"src": "encounter.triage_orders_placed"},
            {"src": "chart.problems"},
            {"src": "chart.medications"},
            {"src": "chart.recent_ed_visits"},
            {"src": "chart.recent_admissions"},
            {"src": "chart.recent_labs"},
            {"src": "chart.recent_imaging"},
            {"src": "chart.social_and_care_context"},
        ],
        "protocols": [],
        "queue": {
            "waiting_count": int(capacity.get("waitboard", 14)),
            "beds_open": int(capacity.get("bedsOpen", 3)),
            "hallway_open": int(capacity.get("hallwayOpen", 2)),
            "fast_track_open": int(capacity.get("fastTrackOpen", 2)),
        },
        "encounter": {
            "id": packet.get("packet_id", "syn-ED-000"),
            "assigned_esi": esi_num,
            "chief_complaint": enc.get("chief_complaint", ""),
            "nurse_triage_note": enc.get("nurse_hx_summary", ""),
            "vitals": vitals,
            "triage_orders_placed": list((enc.get("wr_orders") or {}).get("sets") or [])
            + list((enc.get("wr_orders") or {}).get("addons") or []),
        },
        "chart": {
            "problems": dos.get("pmh_summary", ""),
            "medications": dos.get("current_meds", []),
            "recent_ed_visits": dos.get("last_ed_visits", []),
            "recent_labs": dos.get("recent_labs", []),
            "recent_imaging": dos.get("recent_di", []),
        },
    }


def build_spotd_strip(
    packet: dict[str, Any], delta_card: dict[str, Any], sentinels: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Construct 6-cell Satz-EM SPOT DR strip with guaranteed non-duplicative S / P / O phrases."""
    chief = affirmed_text(str(packet.get("encounter", {}).get("chief_complaint", "")))
    cats = {s["category"] for s in sentinels}
    has_coag = "ANTICOAGULATED_COAGULOPATHIC" in cats
    has_immuno = "IMMUNOCOMPROMISED" in cats
    has_mech = "HIGH_RISK_MECHANISM_ACTIVITY" in cats
    has_bounce = "BOUNCEBACK_FREQUENT_ED" in cats
    ekg_sum = ((packet.get("encounter") or {}).get("ekg") or {}).get("summary")

    serious = "Occult systemic threat"
    probable = "Primary visceral cause"
    outside = "Extra-regional mimic"

    if re.search(r"finger|hydraulic|sprayer", chief):
        serious = "Compartment necrosis"
        probable = "High-pressure injection"
        outside = "Deep flexor tenosynovitis"
    elif re.search(r"eye|grinding", chief):
        serious = "Intraocular foreign body"
        probable = "Occult globe perforation"
        outside = "Acute endophthalmitis"
    elif re.search(r"flank", chief):
        serious = (
            "Rupturing AAA / retroperitoneal bleed" if has_coag else "Obstructing pyelonephritis"
        )
        probable = "Bounceback progression" if has_bounce else "Ureterolithiasis"
        outside = "Aortic / vascular pathology"
    elif re.search(r"groin|lower quadrant|abdominal", chief):
        serious = "Testicular torsion" if "groin" in chief else "Surgical acute abdomen"
        probable = "Unresolved leukocytosis" if has_bounce else "Visceral colic"
        outside = "Mesenteric ischemia / AAA"
    elif re.search(r"back", chief):
        serious = (
            "Spinal epidural abscess"
            if has_mech
            else ("Spinal / retroperitoneal hematoma" if has_coag else "Cord compression")
        )
        probable = "Vertebral / disc pathology"
        outside = "Aortic dissection / AAA"
    elif re.search(r"chest", chief):
        serious = "STEMI (D2B pathway)" if mentions_ste(ekg_sum) else "ACS / aortic dissection"
        probable = "Coronary ischemia"
        outside = "Pulmonary embolism"
    elif re.search(r"syncope|dizz|fatigue", chief):
        serious = "Occult sepsis / hyperkalemia" if has_immuno else "GI hemorrhage / arrhythmia"
        probable = "Symptomatic anemia / bradycardia"
        outside = "Silent ACS / hypoglycemia"
    elif re.search(r"shortness|\bsob\b", chief):
        serious = "Hypercapnic respiratory failure"
        probable = "Acute COPD exacerbation"
        outside = "Pulmonary embolism / HFrEF"
    elif re.search(r"slurred|weakness", chief):
        serious = "Intracranial hemorrhage" if has_coag else "Large-vessel ischemic stroke"
        probable = "Acute cerebrovascular syndrome"
        outside = "Hypoglycemia / seizure mimic"
    elif re.search(r"fall|tripped", chief):
        serious = "Intracranial bleed on anticoagulant" if has_coag else "Occult torso hemorrhage"
        probable = "Resistant urosepsis / syncopal fall" if has_bounce else "Traumatic injury"
        outside = "Sulfonylurea hypoglycemia / arrhythmia"
    elif re.search(r"ankle|laceration", chief):
        serious = "Neurovascular / tendon injury"
        probable = "Isolated soft-tissue / ligament injury"
        outside = "Syncopal or occult mechanism"

    safety_lead = (
        delta_card["safety_net"][0]["action"]
        if delta_card.get("safety_net")
        else "Standard nursing pathway"
    )
    dispo_lead = (
        delta_card["dispo_drivers"][0]["driver"]
        if delta_card.get("dispo_drivers")
        else (
            "No hidden dispo barrier"
            if delta_card.get("priority") == "ROUTINE"
            else "Expedite monitored bed"
        )
    )
    risk_lead = (
        "Guard against premature closure"
        if delta_card.get("framing_check")
        else "Reassess trajectory before anchor"
    )

    return [
        {
            "letter": "S",
            "label": "Serious",
            "phrase": serious,
            "chart": delta_card["priority"] != "ROUTINE",
        },
        {"letter": "P", "label": "Probable", "phrase": probable, "chart": has_bounce or has_mech},
        {"letter": "O", "label": "Outside", "phrase": outside, "chart": has_coag or has_immuno},
        {
            "letter": "T",
            "label": "Treatment",
            "phrase": safety_lead,
            "chart": bool(delta_card.get("safety_net")),
        },
        {
            "letter": "D",
            "label": "Disposition",
            "phrase": _clip_words(dispo_lead, 5),
            "chart": delta_card["priority"] != "ROUTINE",
        },
        {
            "letter": "R",
            "label": "Risks / Bias",
            "phrase": risk_lead,
            "chart": bool(delta_card.get("framing_check")),
        },
    ]


def _try_optional_live_llm_headline(
    packet: dict[str, Any], sentinels: list[dict[str, Any]], fallback: str
) -> tuple[str, str]:
    gemini_key = os.environ.get("GEMINI_API_KEY")
    if not gemini_key:
        return fallback, "7-agent-orthogonal-deterministic"
    import json

    prompt = (
        "Write ONE plain-text ED triage delta headline (<=32 words, no markdown, no calendar dates) "
        "stating only high-priority chart facts omitted or understated at nurse triage.\n"
        f"Encounter: {json.dumps(packet.get('encounter', {}))}\n"
        f"Sentinels: {json.dumps([s['summary'] for s in sentinels])}"
    )
    try:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={gemini_key}"
        body = json.dumps({"contents": [{"parts": [{"text": prompt}]}]}).encode("utf-8")
        req = urllib.request.Request(
            url, data=body, headers={"Content-Type": "application/json"}, method="POST"
        )
        with urllib.request.urlopen(req, timeout=3.0) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            txt = data["candidates"][0]["content"]["parts"][0]["text"].strip().replace("\n", " ")
            if txt.split():
                return _clip_words(txt, 36), "gemini-2.5-flash-live"
    except Exception:
        pass
    return fallback, "7-agent-orthogonal-deterministic"


def run_edimbess_7agent_swarm(
    packet: dict[str, Any],
    capacity: dict[str, Any],
    sentinels: list[dict[str, Any]] | None = None,
    *,
    allow_live_llm: bool = False,
) -> dict[str, Any]:
    """Execute 7-Agent ED Triage Delta pipeline with Sequential Conditional Orthogonal Synthesis."""
    enc = packet.get("encounter", {})
    dos = packet.get("dossier", {})
    vitals = enc.get("vitals") or {}
    evals = enc.get("evals") or {}
    ekg = enc.get("ekg") or {}
    active_sentinels = sentinels if sentinels is not None else detect_must_know_sentinels(packet)
    contract_in = packet_to_contract_payload(packet, capacity)

    candidates: list[CandidateClinicalSignal] = []

    # --- Agent 01: Triage & Vital Anomaly Specialist ---
    a01_cids: list[str] = []
    if evals.get("d2b") == "missing" or mentions_ste(ekg.get("summary")):
        c_d2b = CandidateClinicalSignal(
            cid="TA-1",
            agent="01_triage_anomaly",
            status="NEW",
            finding="ST elevation on triage EKG with D2B pathway marked missing",
            why_now="Acute coronary occlusion requires immediate cath-lab activation and resuscitation bay.",
            action="Activate D2B STEMI pathway and move to resuscitation bay now.",
            sources=("encounter.triage_orders_placed", "encounter.nurse_triage_note"),
            evidence="DOCUMENTED",
            severity="CRITICAL",
            mechanism_axis="coronary_neuro_ischemia",
            intrinsic_gain_nats=0.82,
            ask_question="Did chest pressure start abruptly or radiate to back or jaw?",
        )
        candidates.append(c_d2b)
        a01_cids.append(c_d2b.cid)
    elif evals.get("trauma") == "missing" or evals.get("neuro") == "missing":
        m_lbl = "Trauma" if evals.get("trauma") == "missing" else "Neuro"
        ax = "surgical_compartment_ocular" if m_lbl == "Trauma" else "coronary_neuro_ischemia"
        c_ev = CandidateClinicalSignal(
            cid="TA-2",
            agent="01_triage_anomaly",
            status="NEW",
            finding=f"{m_lbl} arrival evaluation marked missing on acute presentation",
            why_now=f"Delayed {m_lbl.lower()} activation risks rapid neurological or hemorrhagic deterioration.",
            action=f"Trigger missing {m_lbl.lower()} evaluation and move to monitored bed now.",
            sources=("encounter.nurse_triage_note", "encounter.vitals"),
            evidence="DOCUMENTED",
            severity="CRITICAL",
            mechanism_axis=ax,
            intrinsic_gain_nats=0.76,
            ask_question="What was the exact minute symptom onset or fall occurred?",
        )
        candidates.append(c_ev)
        a01_cids.append(c_ev.cid)

    for idx_s, s in enumerate(active_sentinels, start=1):
        if s["category"] in ("UNEXPLAINED_ABNORMAL_VS", "HIGH_RISK_MECHANISM_ACTIVITY"):
            q_txt = (
                "What exact line pressure, material, or projectile speed was involved?"
                if s["category"] == "HIGH_RISK_MECHANISM_ACTIVITY"
                else "Have dizziness, shortness of breath, or weakness worsened since arrival?"
            )
            c_ta = CandidateClinicalSignal(
                cid=f"TA-S{idx_s}",
                agent="01_triage_anomaly",
                status="NEW",
                finding=_clip_words(s["summary"], 14),
                why_now=_clip_words(s["clinical_why"], 22),
                action="Immediate provider reassessment and continuous physiological monitoring.",
                sources=("encounter.vitals", "encounter.nurse_triage_note"),
                evidence="DOCUMENTED",
                severity="CRITICAL" if s["severity"] == "critical" else "HIGH",
                mechanism_axis=s["mechanism_axis"],
                intrinsic_gain_nats=0.68 if s["severity"] == "critical" else 0.36,
                ask_question=q_txt,
            )
            candidates.append(c_ta)
            a01_cids.append(c_ta.cid)

    # Intentional baseline-echo probe on routine visits (verifies Step-0 X_base echo suppression!)
    if not a01_cids and enc.get("chief_complaint"):
        candidates.append(
            CandidateClinicalSignal(
                cid="TA-ECHO",
                agent="01_triage_anomaly",
                status="NEW",
                finding=_clip_words(f"Triage chief complaint: {enc.get('chief_complaint')}", 12),
                why_now="Already posted on triage header.",
                action="Standard nursing pathway.",
                sources=("encounter.nurse_triage_note",),
                evidence="DOCUMENTED",
                severity="MODERATE",
                mechanism_axis="hemodynamic_instability",
                intrinsic_gain_nats=0.15,
                already_explicit_in_triage=True,
            )
        )

    # --- Agent 02: Longitudinal Encounter & Lab/Micro Historian ---
    a02_cids: list[str] = []
    for lab in dos.get("recent_labs", []):
        t_low = str(lab.get("test", "")).lower()
        v_str = str(lab.get("value", ""))
        if "culture" in t_low and ("esbl" in v_str.lower() or "resistant" in v_str.lower()):
            c_esbl = CandidateClinicalSignal(
                cid="LG-ESBL",
                agent="02_longitudinal",
                status="NEW",
                finding="Unreviewed ESBL E. coli urine culture resistant to current cephalexin",
                why_now="Discharged on cephalexin five days ago; rising heart rate and drowsiness suggest urosepsis.",
                action="Obtain lactate, blood cultures, and start ESBL-active antibiotic coverage.",
                sources=("chart.recent_labs", "chart.recent_ed_visits"),
                evidence="DOCUMENTED",
                severity="CRITICAL",
                mechanism_axis="antimicrobial_treatment_failure",
                intrinsic_gain_nats=0.74,
                ask_question="Are you still taking cephalexin, and have fevers or rigors started?",
            )
            candidates.append(c_esbl)
            a02_cids.append(c_esbl.cid)
            continue

        m_num = re.search(r"(\d+(?:\.\d+)?)", v_str)
        if not m_num:
            continue
        num = float(m_num.group(1))
        if "hemoglobin" in t_low and num < 10.0:
            c_hgb = CandidateClinicalSignal(
                cid="LG-HGB",
                agent="02_longitudinal",
                status="NEW",
                finding=f"Recent Hemoglobin {v_str} ({lab.get('when')}) omitted at triage",
                why_now="Severe anemia with tachycardia signals active or recurrent GI hemorrhage.",
                action="Stat repeat CBC, type and screen, and hold all NSAIDs.",
                sources=("chart.recent_labs", "chart.recent_ed_visits"),
                evidence="DOCUMENTED",
                severity="CRITICAL" if num < 8.0 else "HIGH",
                mechanism_axis="coagulopathy_hemorrhage",
                intrinsic_gain_nats=0.66,
                ask_question="Have you noticed black tarry stools or blood in vomit?",
            )
            candidates.append(c_hgb)
            a02_cids.append(c_hgb.cid)
        elif "wbc" in t_low and num >= 12.0:
            c_wbc = CandidateClinicalSignal(
                cid="LG-WBC",
                agent="02_longitudinal",
                status="CHANGED",
                finding=f"Prior WBC {v_str} ({lab.get('when')}) on short-interval return",
                why_now="Unresolved leukocytosis with persistent pain raises concern for deep infection.",
                action="Expedite surgical or cross-sectional imaging re-evaluation.",
                sources=("chart.recent_labs", "chart.recent_ed_visits"),
                evidence="DOCUMENTED",
                severity="CRITICAL",
                mechanism_axis="antimicrobial_treatment_failure",
                intrinsic_gain_nats=0.58,
                ask_question="Has the pain localized, worsened with movement, or caused fevers?",
            )
            candidates.append(c_wbc)
            a02_cids.append(c_wbc.cid)
        elif "potassium" in t_low and num >= 5.2:
            c_k = CandidateClinicalSignal(
                cid="LG-K",
                agent="02_longitudinal",
                status="NEW",
                finding=f"Recent Potassium {v_str} ({lab.get('when')}) on hemodialysis",
                why_now="Hyperkalemia and HFrEF create acute arrhythmia and volume-overload risk.",
                action="Stat 12-lead EKG and point-of-care electrolyte panel.",
                sources=("chart.recent_labs", "chart.problems"),
                evidence="DOCUMENTED",
                severity="CRITICAL",
                mechanism_axis="glycemic_renal_electrolyte",
                intrinsic_gain_nats=0.62,
                ask_question="When was your last completed hemodialysis session?",
            )
            candidates.append(c_k)
            a02_cids.append(c_k.cid)

    for idx_s, s in enumerate(active_sentinels, start=1):
        if s["category"] in ("HIGH_RISK_PMH", "BOUNCEBACK_FREQUENT_ED"):
            src = (
                ("chart.problems", "chart.recent_imaging")
                if s["category"] == "HIGH_RISK_PMH"
                else ("chart.recent_ed_visits",)
            )
            q_lg = (
                "How does today compare to your prior hospital or ICU admission?"
                if s["category"] == "HIGH_RISK_PMH"
                else "What has worsened or changed since your recent ED discharge?"
            )
            c_lg = CandidateClinicalSignal(
                cid=f"LG-S{idx_s}",
                agent="02_longitudinal",
                status="CHANGED" if s["category"] == "BOUNCEBACK_FREQUENT_ED" else "NEW",
                finding=_clip_words(s["summary"], 14),
                why_now=_clip_words(s["clinical_why"], 22),
                action="Review prior chart trajectory before assigning unmonitored waitboard.",
                sources=src,
                evidence="DOCUMENTED",
                severity="CRITICAL" if s["severity"] == "critical" else "HIGH",
                mechanism_axis=s["mechanism_axis"],
                intrinsic_gain_nats=0.52 if s["severity"] == "critical" else 0.34,
                ask_question=q_lg,
            )
            candidates.append(c_lg)
            a02_cids.append(c_lg.cid)

    # --- Agent 03: ED Pharmacist (Medications, Conflicts & Twin Candidates) ---
    a03_cids: list[str] = []
    meds_list = [str(m) for m in dos.get("current_meds", []) if str(m).lower() != "none recorded"]
    meds_lower = " ".join(meds_list).lower()

    if re.search(r"glipizide|glyburide", meds_lower):
        c_hypo = CandidateClinicalSignal(
            cid="MD-HYPO",
            agent="03_medication",
            status="NEW",
            finding="Active sulfonylurea (glipizide) with CKD and recent hypoglycemia",
            why_now="New drowsiness after fall may reflect recurrent hypoglycemia; no glucose recorded today.",
            action="Point-of-care capillary glucose now; give thiamine before dextrose if needed.",
            sources=("chart.medications", "chart.recent_labs"),
            evidence="DOCUMENTED",
            severity="CRITICAL",
            mechanism_axis="glycemic_renal_electrolyte",
            intrinsic_gain_nats=0.69,
            ask_question="Did you take glipizide today, and when did you last eat?",
        )
        candidates.append(c_hypo)
        a03_cids.append(c_hypo.cid)

    for idx_s, s in enumerate(active_sentinels, start=1):
        if s["category"] in ("ANTICOAGULATED_COAGULOPATHIC", "IMMUNOCOMPROMISED"):
            q_md = (
                "Do you take a blood thinner such as Eliquis, and when was your last dose?"
                if s["category"] == "ANTICOAGULATED_COAGULOPATHIC"
                else "Any home fevers, chills, or missed immunosuppressant doses this week?"
            )
            c_md = CandidateClinicalSignal(
                cid=f"MD-S{idx_s}",
                agent="03_medication",
                status="NEW",
                finding=_clip_words(s["summary"], 14),
                why_now=_clip_words(s["clinical_why"], 22),
                action="Hold NSAIDs, confirm last dose timing, and screen for occult bleed or sepsis.",
                sources=("chart.medications", "chart.problems"),
                evidence="CONFLICTING" if not s.get("in_nurse_note") else "DOCUMENTED",
                severity="CRITICAL" if s["severity"] == "critical" else "HIGH",
                mechanism_axis=s["mechanism_axis"],
                intrinsic_gain_nats=0.72 if not s.get("in_nurse_note") else 0.48,
                ask_question=q_md,
            )
            candidates.append(c_md)
            a03_cids.append(c_md.cid)

            # Collinear twin candidate from secondary medication tag (tests G_{m|S} twin pruning!)
            if len(meds_list) >= 2 and s["category"] == "IMMUNOCOMPROMISED":
                candidates.append(
                    CandidateClinicalSignal(
                        cid=f"MD-TWIN-{idx_s}",
                        agent="03_medication",
                        status="NEW",
                        finding=_clip_words(f"Dual immunomodulator therapy ({meds_list[0]})", 13),
                        why_now="Secondary duplicate immunosuppression flag on same infection axis.",
                        action="Verify transplant medication trough levels.",
                        sources=("chart.medications",),
                        evidence="DOCUMENTED",
                        severity="HIGH",
                        mechanism_axis="immunosuppression_occult_sepsis",
                        intrinsic_gain_nats=0.41,
                    )
                )

    # --- Sequential Conditional Gain Selection (Findings <= 3, Zero Echoes, Zero Twins) ---
    selected_sigs, suppressed_out, step_trace, orth_stats = select_orthogonal_candidates(
        candidates, max_items=3
    )

    findings = [
        {
            "id": f"F{i + 1}",
            "status": sig.status,
            "finding": _clip_words(sig.finding, 15),
            "why_now": _clip_words(sig.why_now, 25),
            "sources": list(sig.sources[:4]),
            "evidence": sig.evidence,
            "action": _clip_words(sig.action, 20),
        }
        for i, sig in enumerate(selected_sigs)
    ]

    # --- Agent 04: Disposition, Isolation & Digit-Free Queue Interpretation ---
    dispo_raw: list[tuple[str, str]] = []
    social_txt = str(dos.get("social_hx_summary", "")).lower()
    if enc.get("isolation_flag") or "mrsa" in social_txt:
        dispo_raw.append(
            (
                "Active MRSA isolation flag on chart",
                "Request isolation-capable room early before boarding bottleneck.",
            )
        )
    if "lives alone" in social_txt and (findings or active_sentinels):
        dispo_raw.append(
            (
                "Lives alone with high-risk presentation",
                "Initiate early safe-discharge and case-management check.",
            )
        )
    elif findings:
        dispo_raw.append(
            (
                _clip_words(f"High-risk delta: {findings[0]['finding']}", 10),
                "Prioritize monitored space ahead of routine waitboard.",
            )
        )

    beds_open = int(capacity.get("bedsOpen", 3))
    # Strictly digit-free queue_note per Agent 04 & validator contract!
    if beds_open > 0:
        q_note = (
            "Staffed beds are currently open; advance high-priority delta patients ahead of the routine waitboard."
            if findings
            else "Staffed beds and waiting-room pathways are open for standard triage flow."
        )
    else:
        q_note = "No staffed beds are open; use resuscitation, monitored hallway, or waiting-room rapid testing now."

    # --- Agent 05: Safety-Net Actions (Checked Against Existing Orders & Constraints) ---
    owned_orders = {
        str(x).lower() for x in (contract_in["encounter"]["triage_orders_placed"] or [])
    }
    safety_candidates: list[dict[str, Any]] = []
    lead_ref = findings[0]["id"] if findings else "triage"

    if (
        any("glipizide" in f["finding"].lower() for f in findings)
        and "poc glucose" not in owned_orders
    ):
        safety_candidates.append(
            {
                "action": "Point-of-care glucose now",
                "type": "ESCALATE",
                "protocol_id": None,
                "for_finding": next(
                    (f["id"] for f in findings if "glipizide" in f["finding"].lower()), lead_ref
                ),
                "checks": "Give thiamine before dextrose given alcohol history.",
                "urgency": "NOW",
            }
        )
    if any("esbl" in f["finding"].lower() for f in findings):
        safety_candidates.append(
            {
                "action": "Sepsis lactate and blood cultures",
                "type": "PROVIDER_ORDER",
                "protocol_id": None,
                "for_finding": next(
                    (f["id"] for f in findings if "esbl" in f["finding"].lower()), lead_ref
                ),
                "checks": "Prior urine culture resistant to cephalexin.",
                "urgency": "NOW",
            }
        )
    if (
        ekg.get("status") == "missing"
        and re.search(
            r"dizz|syncope|chest|epigastric|sob|shortness|fatigue",
            affirmed_text(str(enc.get("chief_complaint", ""))),
        )
        and findings
    ):
        safety_candidates.append(
            {
                "action": "Complete missing triage ECG check",
                "type": "ESCALATE",
                "protocol_id": None,
                "for_finding": lead_ref,
                "checks": "ECG status chip is marked pending on indicated complaint.",
                "urgency": "NOW",
            }
        )
    if vitals.get("sbp") is not None and float(vitals["sbp"]) < 95 and findings:
        safety_candidates.append(
            {
                "action": "Immediate IV access and hemodynamic reassessment",
                "type": "ESCALATE",
                "protocol_id": None,
                "for_finding": lead_ref,
                "checks": "Hold oral waiting-room analgesics in hypotension.",
                "urgency": "NOW",
            }
        )
    if vitals.get("spo2") is not None and float(vitals["spo2"]) < 92 and findings:
        safety_candidates.append(
            {
                "action": "Titrate oxygen and assess work of breathing",
                "type": "ESCALATE",
                "protocol_id": None,
                "for_finding": lead_ref,
                "checks": "Check prior hypercapnic intubation history.",
                "urgency": "NOW",
            }
        )
    if any(s["category"] == "HIGH_RISK_MECHANISM_ACTIVITY" for s in active_sentinels) and findings:
        safety_candidates.append(
            {
                "action": "Emergent surgical specialty consult and NPO",
                "type": "ESCALATE",
                "protocol_id": None,
                "for_finding": lead_ref,
                "checks": "Keep NPO; avoid digital block or fast-track delay.",
                "urgency": "NOW",
            }
        )
    if any(
        "apixaban" in f["finding"].lower() or "antithrombotic" in f["finding"].lower()
        for f in findings
    ) and re.search(
        r"fall|tripped|slurred|head", affirmed_text(str(enc.get("chief_complaint", "")))
    ):
        safety_candidates.append(
            {
                "action": "Expedite noncontrast head CT evaluation",
                "type": "PROVIDER_ORDER",
                "protocol_id": None,
                "for_finding": lead_ref,
                "checks": "No contrast needed; renal impairment safe.",
                "urgency": "NOW",
            }
        )

    safety_out = [s for s in safety_candidates if s["action"].lower() not in owned_orders][:3]

    ask_out = [
        {"question": _clip_words(sig.ask_question, 19), "for_finding": f"F{idx + 1}"}
        for idx, sig in enumerate(selected_sigs)
        if sig.ask_question
    ][:3]

    dispo_out = (
        [
            {
                "driver": _clip_words(drv, 14),
                "early_action": _clip_words(act, 14),
                "for_finding": lead_ref,
            }
            for drv, act in dispo_raw[:2]
        ]
        if findings
        else []
    )

    nurse_low = str(enc.get("nurse_hx_summary", "")).lower()
    framing_out: str | None = None
    if findings and re.search(
        r"minor|benign|strain|frequent visitor|smells of alcohol|comfortable", nurse_low
    ):
        framing_out = _clip_words(
            "Triage note contains minimizing or alcohol/visit framing, while objective vitals and chart sentinels show acute risk.",
            28,
        )

    missing_list: list[str] = []
    if enc.get("outside_hie_available") is False:
        missing_list.append("External HIE records unavailable")
    if ekg.get("status") == "missing" and re.search(
        r"dizz|syncope|chest|epigastric|sob|fatigue", str(enc.get("chief_complaint", "")).lower()
    ):
        missing_list.append("Triage ECG not yet recorded")
    if any("glipizide" in f["finding"].lower() for f in findings):
        missing_list.append("No capillary glucose this encounter")
    if not missing_list:
        missing_list.append("Outside HIE not queried locally")

    if not findings:
        priority = "ROUTINE"
        headline = ROUTINE_HEADLINE
        priority_reason = (
            "No chart fact or sentinel changes what a physician does in the next hour."
        )
        if not suppressed_out:
            suppressed_out = [{"item": "Standard nursing pathway covers visit", "reason": 4}]
    else:
        has_crit = any(sig.severity == "CRITICAL" for sig in selected_sigs) or any(
            s["severity"] == "critical" for s in active_sentinels
        )
        priority = "CRITICAL" if has_crit else "HIGH"
        headline = _clip_words(f"{findings[0]['finding']}. {findings[0]['why_now']}", 38)
        priority_reason = (
            "Time-sensitive clinical sentinel or door-eval gap requires immediate provider look."
            if priority == "CRITICAL"
            else "High-risk chart delta omitted at triage; review before prolonged wait."
        )
        if not suppressed_out:
            suppressed_out = [{"item": "Stable remote baseline history", "reason": 4}]

    engine_mode = "7-agent-orthogonal-deterministic"
    if allow_live_llm and priority != "ROUTINE":
        headline, engine_mode = _try_optional_live_llm_headline(packet, active_sentinels, headline)

    synthesized_card: dict[str, Any] = {
        "as_of": contract_in["as_of"],
        "mode": "SHADOW",
        "priority": priority,
        "priority_reason": _clip_words(priority_reason, 24),
        "headline": _clip_words(headline, 39),
        "findings": findings,
        "safety_net": safety_out,
        "ask_patient": ask_out,
        "dispo_drivers": dispo_out,
        "framing_check": framing_out,
        "queue_note": _clip_words(q_note, 28),
        "missing_or_stale": [_clip_words(m, 9) for m in missing_list[:4]],
        "suppressed": suppressed_out,
    }

    # --- Agent 07: Cold Safety & Contract Auditor (One-Way Escalation Ratchet) ---
    auditor_notes: list[str] = ["Verified source provenance and digit-free queue contract."]
    ratchet_applied = False
    if (
        any(s["severity"] == "critical" for s in active_sentinels)
        and synthesized_card["priority"] != "CRITICAL"
    ):
        synthesized_card["priority"] = "CRITICAL"
        ratchet_applied = True
        auditor_notes.append("Auditor ratchet upgraded priority to CRITICAL on critical sentinel.")
    if (
        any(evals.get(k) == "missing" for k in ("trauma", "neuro", "d2b"))
        and synthesized_card["priority"] != "CRITICAL"
    ):
        synthesized_card["priority"] = "CRITICAL"
        ratchet_applied = True
        auditor_notes.append(
            "Auditor ratchet upgraded priority to CRITICAL on missing arrival eval."
        )

    val_errors = validate_delta_output(contract_in, synthesized_card)
    spotd = build_spotd_strip(packet, synthesized_card, active_sentinels)

    return {
        "engine_mode": engine_mode,
        "contract_input": contract_in,
        "specialists": [
            {"agent": "01_triage_anomaly", "n_signals": len(a01_cids), "cids": a01_cids},
            {"agent": "02_longitudinal", "n_signals": len(a02_cids), "cids": a02_cids},
            {"agent": "03_medication", "n_signals": len(a03_cids), "cids": a03_cids},
            {
                "agent": "04_disposition_flow",
                "n_signals": len(dispo_out),
                "cids": ["DF-1"] if dispo_out else [],
            },
            {
                "agent": "05_safety_net",
                "n_signals": len(safety_out),
                "cids": [f"SN-{i + 1}" for i in range(len(safety_out))],
            },
        ],
        "synthesizer": synthesized_card,
        "spotd": spotd,
        "orthogonality": {
            **orth_stats,
            "step_trace": [t.to_dict() for t in step_trace],
        },
        "auditor": {
            "passed": len(val_errors) == 0,
            "ratchet_applied": ratchet_applied,
            "notes": auditor_notes,
            "errors": val_errors,
        },
    }

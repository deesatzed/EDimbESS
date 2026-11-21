"""Comprehensive unit and HTTP integration test suite for EDimbESS Statistical Safety Engine."""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest
from scipy.special import expit, logit

from edimbess.calibration_cohort import (
    TabularPreprocessor,
    generate_ed_calibration_cohort,
    packet_to_feature_frames,
)
from edimbess.contracts import (
    PITM_CONTRACTS,
    SemanticType,
    capacity_adjusted_space_threshold,
    freeze_split,
    kish_effective_sample_size,
    nadeau_bengio_tost,
    wilson_interval,
)
from edimbess.orthogonal_delta import (
    CandidateClinicalSignal,
    run_edimbess_7agent_swarm,
    select_orthogonal_candidates,
)
from edimbess.sentinels_and_rules import (
    SEED_PACKETS,
    affirmed_text,
    check_no_phi,
    compute_pitm_five_decisions,
    detect_must_know_sentinels,
    ekg_chip_for,
    validate_intake_payload,
)
from edimbess.server import DEFAULT_CAPACITY, EDimbESSAppState, make_handler
from edimbess.streaming_safety import (
    _GH_NODES,
    _GH_WEIGHTS,
    EDStatisticalSafetyEngine,
    StreamingVOIRouter,
    conformal_fdr_selection,
    mondrian_singleton_positive_selection,
    solve_mean_preserving_logit_offset,
)
from edimbess.two_phase import (
    compute_oof_phase1_baseline,
    discover_sequential_conditional_gain,
    sample_two_phase_pilot,
    two_phase_variance_decomposition,
    waterfill_constrained_neyman,
)


@pytest.fixture(scope="module")
def safety_engine() -> EDStatisticalSafetyEngine:
    """Shared calibrated safety engine fixture across module tests."""
    return EDStatisticalSafetyEngine(n_encounters=5600, seed=42)


# ---------------------------------------------------------------------------
# 1. Contracts, Leak-Free Group Split, Kish ESS, Wilson, & TOST
# ---------------------------------------------------------------------------


def test_leak_free_patient_group_split_and_preprocessor() -> None:
    coh = generate_ed_calibration_cohort(n_encounters=1200, seed=11)
    df = pd.concat(
        [
            coh.X_base,
            pd.DataFrame({"patient_id": coh.patient_ids, "y": coh.y}),
        ],
        axis=1,
    )
    sp1 = freeze_split(df, "y", group_col="patient_id", seed=11)
    sp2 = freeze_split(df, "y", group_col="patient_id", seed=11)
    assert sp1.split_hash == sp2.split_hash

    pts_tr = set(df.iloc[list(sp1.train_idx)]["patient_id"])
    pts_ca = set(df.iloc[list(sp1.cal_idx)]["patient_id"])
    pts_va = set(df.iloc[list(sp1.val_idx)]["patient_id"])
    pts_te = set(df.iloc[list(sp1.test_idx)]["patient_id"])
    assert not (pts_tr & pts_ca)
    assert not (pts_tr & pts_va)
    assert not (pts_tr & pts_te)
    assert not (pts_ca & pts_te)

    # Verify explicit missingness indicator creation without test leakage
    Z_tr = coh.Z_pool.iloc[list(sp1.train_idx)]
    Z_te = coh.Z_pool.iloc[list(sp1.test_idx)]
    prep = TabularPreprocessor({c: SemanticType.CONTINUOUS for c in Z_tr.columns}).fit(Z_tr)
    assert any(name.startswith("miss__") for name in prep.feature_names_out_)
    mat_te = prep.transform(Z_te)
    assert mat_te.shape == (len(Z_te), len(prep.feature_names_out_))
    assert not np.isnan(mat_te).any()


def test_kish_ess_wilson_and_tost_identities() -> None:
    ess_u, deff_u = kish_effective_sample_size(np.ones(50))
    assert np.isclose(ess_u, 50.0)
    assert np.isclose(deff_u, 1.0)

    ess_skew, deff_skew = kish_effective_sample_size(np.array([1.0] * 45 + [20.0] * 5))
    assert ess_skew < 50.0
    assert deff_skew > 1.5

    lo, hi = wilson_interval(0, 180, alpha=0.05)
    assert lo == 0.0
    assert 0.015 < hi < 0.025

    tost = nadeau_bengio_tost(
        [-0.002, 0.001, -0.001, 0.002, 0.000], margin=0.015, n_test=200, n_train=800
    )
    assert tost.noninferior_at_05 is True
    assert tost.equivalent_at_05 is True


# ---------------------------------------------------------------------------
# 2. Negation Safety, Zero-PHI Gate, 6 Must-Know Sentinels, & 22 Seed Packets
# ---------------------------------------------------------------------------


def test_negation_safety_and_ekg_boundary() -> None:
    cleaned = affirmed_text("Lightheaded since morning. No chest pain. Denies syncope or dyspnea.")
    assert "chest pain" not in cleaned
    assert "syncope" not in cleaned
    assert "lightheaded" in cleaned

    apnea = affirmed_text("Found unresponsive, patient is not breathing on EMS arrival.")
    assert "not breathing" in apnea

    # Verify EKG is NEVER ordered inside any of the 5 PITM decisions across all 22 seed packets
    for pkt in SEED_PACKETS:
        chip = ekg_chip_for(pkt)
        assert chip["status"] in {"done", "pending", "na"}
        recs = compute_pitm_five_decisions(pkt, DEFAULT_CAPACITY)
        assert len(recs) == 5
        wr_items = [str(x).lower() for x in recs[3]["value"].get("items", [])]
        assert not any("ekg" in it or "ecg" in it for it in wr_items)


def test_zero_phi_intake_gate_blocks_identifiers() -> None:
    assert check_no_phi({"packet_id": "syn-ED-050", "patient_name": "John Smith"}) is not None
    assert check_no_phi({"note": "Call spouse at 555-234-5678"}) is not None
    assert check_no_phi({"note": "SSN 123-45-6789 on sheet"}) is not None
    assert check_no_phi({"note": "Email test@hospital.org"}) is not None

    ok_bad_id, msg = validate_intake_payload(
        {"packet_id": "MRN-99912", "encounter": {}, "dossier": {}}
    )
    assert ok_bad_id is False
    assert "syn-ED" in str(msg)

    ok_valid, validated = validate_intake_payload(SEED_PACKETS[0])
    assert ok_valid is True
    assert isinstance(validated, list) and len(validated) == 1


def test_all_six_must_know_sentinels_and_22_packets_validator_pass() -> None:
    by_id = {p["packet_id"]: p for p in SEED_PACKETS}
    observed_categories: set[str] = set()

    for pkt in SEED_PACKETS:
        X_r, Z_r = packet_to_feature_frames(pkt)
        assert len(X_r) == 1 and len(Z_r) == 1
        sents = detect_must_know_sentinels(pkt)
        for s in sents:
            observed_categories.add(s["category"])
        swarm = run_edimbess_7agent_swarm(pkt, DEFAULT_CAPACITY, sentinels=sents)
        assert swarm["auditor"]["passed"] is True, (
            f"{pkt['packet_id']} failed validator: {swarm['auditor']['errors']}"
        )

    assert observed_categories == {
        "IMMUNOCOMPROMISED",
        "ANTICOAGULATED_COAGULOPATHIC",
        "UNEXPLAINED_ABNORMAL_VS",
        "HIGH_RISK_MECHANISM_ACTIVITY",
        "HIGH_RISK_PMH",
        "BOUNCEBACK_FREQUENT_ED",
    }

    # Verify acute capacity resilience: 0 beds open never blocks immediate bay on shock or STEMI
    zero_beds = {**DEFAULT_CAPACITY, "bedsOpen": 0, "hallwayOpen": 0}
    assert (
        capacity_adjusted_space_threshold(PITM_CONTRACTS["space_priority"], 0, 18)
        > PITM_CONTRACTS["space_priority"].bayes_threshold
    )
    for crit_id in ("syn-ED-008", "syn-ED-011"):
        recs = compute_pitm_five_decisions(by_id[crit_id], zero_beds)
        assert recs[2]["value"]["priority"] == "immediate"


def test_orthogonal_conditional_gain_suppresses_echoes_and_twins() -> None:
    by_id = {p["packet_id"]: p for p in SEED_PACKETS}

    # Routine ankle sprain: triage echo suppressed, 0 findings
    sw_routine = run_edimbess_7agent_swarm(by_id["syn-ED-014"], DEFAULT_CAPACITY)
    assert sw_routine["synthesizer"]["priority"] == "ROUTINE"
    assert sw_routine["orthogonality"]["n_echoes_suppressed"] >= 1
    assert sw_routine["orthogonality"]["n_orthogonal_selected"] == 0

    # Removed transplant + dual immunomodulators: duplicate twin pruned
    sw_twin = run_edimbess_7agent_swarm(by_id["syn-ED-020"], DEFAULT_CAPACITY)
    assert sw_twin["orthogonality"]["n_twins_suppressed"] >= 1

    # Flagship E9001 case (syn-ED-022): 3 distinct mechanism axes (ESBL, Apixaban, Glipizide)
    sw_flagship = run_edimbess_7agent_swarm(by_id["syn-ED-022"], DEFAULT_CAPACITY)
    assert sw_flagship["synthesizer"]["priority"] == "CRITICAL"
    assert sw_flagship["orthogonality"]["n_orthogonal_selected"] == 3
    assert sw_flagship["orthogonality"]["distinct_mechanisms_covered"] == 3
    assert sw_flagship["synthesizer"]["framing_check"] is not None

    # Direct unit test on select_orthogonal_candidates
    cands = [
        CandidateClinicalSignal(
            cid="C1",
            agent="a1",
            status="NEW",
            finding="Apixaban active fill",
            why_now="Bleed risk",
            action="Hold NSAIDs",
            sources=("chart.medications",),
            evidence="DOCUMENTED",
            severity="CRITICAL",
            mechanism_axis="coagulopathy_hemorrhage",
            intrinsic_gain_nats=0.70,
        ),
        CandidateClinicalSignal(
            cid="C2_TWIN",
            agent="a2",
            status="NEW",
            finding="Elevated prior INR",
            why_now="Same bleed axis",
            action="Hold NSAIDs",
            sources=("chart.recent_labs",),
            evidence="DOCUMENTED",
            severity="HIGH",
            mechanism_axis="coagulopathy_hemorrhage",
            intrinsic_gain_nats=0.65,
        ),
        CandidateClinicalSignal(
            cid="C3_ORTH",
            agent="a3",
            status="NEW",
            finding="Resistant ESBL culture",
            why_now="Urosepsis failure",
            action="Broaden coverage",
            sources=("chart.recent_labs",),
            evidence="DOCUMENTED",
            severity="HIGH",
            mechanism_axis="antimicrobial_treatment_failure",
            intrinsic_gain_nats=0.45,
        ),
    ]
    sel, _, _, stats = select_orthogonal_candidates(cands, max_items=2)
    assert [s.cid for s in sel] == ["C1", "C3_ORTH"]
    assert stats["n_twins_suppressed"] == 1


# ---------------------------------------------------------------------------
# 3. Two-Phase Neyman Allocation, Exact Variance Identity, & Stress-Pool 0/0/0
# ---------------------------------------------------------------------------


def test_two_phase_ht_weights_waterfill_and_srs_variance_identity() -> None:
    rng = np.random.default_rng(7)
    n_pop = 800
    # High-prevalence regime pi = 0.35 > pilot_fraction = 0.15 tests water-filling clamping
    p0 = rng.uniform(0.05, 0.75, size=n_pop)
    y = rng.binomial(1, 0.35, size=n_pop)

    for design in ("neyman", "srs", "full"):
        pilot = sample_two_phase_pilot(y, p0, pilot_fraction=0.15, design=design, seed=7)
        assert np.isclose(np.sum(pilot.ht_weights), float(n_pop), rtol=1e-9)
        for h_id, n_smp_h in pilot.stratum_sample_counts.items():
            assert 1 <= n_smp_h <= pilot.stratum_pop_counts[h_id]

    pop_counts = np.array([12, 18, 250, 320])
    disp = np.array([0.95, 0.85, 0.12, 0.05])
    alloc = waterfill_constrained_neyman(pop_counts, disp, target_total=80)
    assert int(np.sum(alloc)) == 80
    assert np.all(alloc <= pop_counts)

    # Exact SRS algebraic identity: V_I + V_II == s^2 / n_pilot
    pilot_srs = sample_two_phase_pilot(y, p0, pilot_fraction=0.20, design="srs", seed=7)
    psi_vals = rng.normal(0.08, 0.25, size=pilot_srs.n_pilot)
    g_hat, se_tot, _, _ = two_phase_variance_decomposition(
        psi_vals,
        pilot_srs.ht_weights,
        pilot_srs.stratum_ids_sampled,
        pilot_srs.stratum_pop_counts,
        pilot_srs.stratum_sample_counts,
        groups=None,
    )
    expected_se = float(np.sqrt(np.var(psi_vals, ddof=1) / pilot_srs.n_pilot))
    assert np.isclose(g_hat, float(np.mean(psi_vals)), rtol=1e-10)
    assert np.isclose(se_tot, expected_se, rtol=1e-6)


def test_stress_pool_zero_null_zero_twin_zero_echo_selection() -> None:
    stress_coh = generate_ed_calibration_cohort(
        n_encounters=6400, include_stress_channels=True, seed=42
    )
    df = pd.concat(
        [
            stress_coh.X_base,
            pd.DataFrame({"patient_id": stress_coh.patient_ids, "y": stress_coh.y}),
        ],
        axis=1,
    )
    sp = freeze_split(df, "y", group_col="patient_id", seed=42)
    tr = list(sp.train_idx)
    oof_p0 = compute_oof_phase1_baseline(
        stress_coh.X_base.iloc[tr],
        stress_coh.y[tr],
        groups_train=stress_coh.patient_ids[tr],
        seed=42,
    )
    pilot = sample_two_phase_pilot(
        stress_coh.y[tr], oof_p0, pilot_fraction=0.20, design="neyman", seed=42
    )
    disc_seq = discover_sequential_conditional_gain(
        stress_coh.X_base.iloc[tr],
        stress_coh.Z_pool.iloc[tr],
        stress_coh.y[tr],
        stress_coh.candidates,
        oof_p0,
        pilot,
        max_budget=4.20,
        groups_train=stress_coh.patient_ids[tr],
        sequential=True,
        seed=42,
    )
    disc_marg = discover_sequential_conditional_gain(
        stress_coh.X_base.iloc[tr],
        stress_coh.Z_pool.iloc[tr],
        stress_coh.y[tr],
        stress_coh.candidates,
        oof_p0,
        pilot,
        max_budget=4.20,
        groups_train=stress_coh.patient_ids[tr],
        sequential=False,
        seed=42,
    )

    seq_tags = [c.mechanism_tag for c in disc_seq.selected_channels]
    marg_tags = [c.mechanism_tag for c in disc_marg.selected_channels]
    assert len(disc_seq.selected_channels) >= 1
    assert sum(t == "pure_noise_null" for t in seq_tags) == 0
    assert sum(t == "stage2_twin" for t in seq_tags) == 0
    assert sum(t == "baseline_echo" for t in seq_tags) == 0
    assert sum(t in ("stage2_twin", "baseline_echo") for t in marg_tags) >= 1


# ---------------------------------------------------------------------------
# 4. Martingale Gauss-Hermite VOI, Tie-Smoothed CfBH, & Safe-Gate Behavior
# ---------------------------------------------------------------------------


def test_martingale_voi_quadrature_and_tie_smoothed_cfbh() -> None:
    p_grid = np.array([0.002, 0.015, 0.040, 0.120, 0.450, 0.920])
    sig_grid = np.array([0.35, 0.80, 1.10, 0.95, 0.85, 0.60])
    mu = solve_mean_preserving_logit_offset(p_grid, sig_grid)
    p2_nodes = expit(logit(p_grid)[:, None] + mu[:, None] + sig_grid[:, None] * _GH_NODES[None, :])
    mean_p2 = np.sum(_GH_WEIGHTS[None, :] * p2_nodes, axis=1)
    assert float(np.max(np.abs(mean_p2 - p_grid))) < 1e-12

    router = StreamingVOIRouter(n_voi_bins=4)
    router.bin_edges_ = np.array([0.0, 1.0])
    router.bin_logit_sd_ = np.array([0.75])
    voi_vals = router.score_voi(np.array([0.0005, 0.040, 0.980]), cost_fp=1.0, cost_fn=24.0)
    assert np.all(voi_vals >= 0.0)
    # Boundary VOI at tau* = 0.040 strictly dominates extreme certainty ends
    assert voi_vals[1] > 0.20
    assert voi_vals[0] < 1e-4
    assert voi_vals[2] < 1e-4

    # Discrete tie-heavy scores: verify tie-smoothed CfBH controls FDP vs Mondrian {1}
    rng = np.random.default_rng(99)
    y_cal = rng.binomial(1, 0.10, size=1000)
    y_tst = rng.binomial(1, 0.10, size=1000)
    # Quantized 20-level scores creating heavy point-mass ties
    cal_s = np.round(np.clip(0.08 + 0.55 * y_cal + rng.normal(0, 0.22, 1000), 0.0, 1.0), 1)
    tst_s = np.round(np.clip(0.08 + 0.55 * y_tst + rng.normal(0, 0.22, 1000), 0.0, 1.0), 1)

    cfbh_res, _, _ = conformal_fdr_selection(cal_s, y_cal, tst_s, y_tst, target_fdr=0.30, seed=99)
    mond_res = mondrian_singleton_positive_selection(cal_s, y_cal, tst_s, y_tst, alpha=0.10)
    assert cfbh_res.realized_fdp <= 0.36
    assert mond_res.realized_fdp > cfbh_res.realized_fdp + 0.20


def test_seed_packets_safe_gate_and_voi_classifications(
    safety_engine: EDStatisticalSafetyEngine,
) -> None:
    evaluated = {p["packet_id"]: p for p in safety_engine.evaluate_seed_queue()}

    # Low-risk uncomplicated arrivals (including syn-ED-002 whose labs/CT are already in chart)
    # earn Conformal Certified Safe Routine with zero bogus VOI channel pre-fetch prompts
    for safe_id in ("syn-ED-002", "syn-ED-012", "syn-ED-014"):
        assert evaluated[safe_id]["safe_gate"]["gate_state"] == "CERTIFIED_SAFE_ROUTINE"
        assert evaluated[safe_id]["safe_gate"]["allow_one_pinch_routine"] is True
        assert evaluated[safe_id]["voi_routing"]["auto_triggered_channels"] == []

    # Every packet carries explicit PITM inputs + outputs & zero-jargon why_queued
    for pkt in evaluated.values():
        cr = pkt["clinical_routing"]
        assert len(cr["why_queued"]) >= 1
        assert len(cr["what_happens_next"]) >= 1
        assert len(cr["input_streams"]) == 3
        assert {s["id"] for s in cr["input_streams"]} == {
            "door_triage",
            "chart_pharmacy",
            "labs_micro",
        }
        assert "already_placed_at_door" in cr["output_tasks"]
        assert len(cr["output_tasks"]["staged_unplaced_tasks"]) >= 1

    # Flagship syn-ED-022: only Wrist X-ray placed at door while systemic acuity/labs/checks remain unplaced
    flag_cr = evaluated["syn-ED-022"]["clinical_routing"]
    stream_map = {s["id"]: s for s in flag_cr["input_streams"]}
    assert stream_map["chart_pharmacy"]["status"] == "conflict"
    assert flag_cr["output_tasks"]["already_placed_at_door"] == ["Door orders active: Wrist X-ray"]
    assert len(flag_cr["output_tasks"]["staged_unplaced_tasks"]) >= 4

    # syn-ED-006 (dizziness, pending ECG): 0 placed at door, unplaced outputs + checks remain visible
    amb_out = evaluated["syn-ED-006"]["clinical_routing"]["output_tasks"]
    assert amb_out["already_placed_at_door"] == []
    assert len(amb_out["staged_unplaced_tasks"]) >= 2

    # Ambiguous dizziness with pending EKG & missing HIE holds fast-pass & surfaces named checks
    amb = evaluated["syn-ED-006"]
    assert amb["safe_gate"]["gate_state"] == "AMBIGUOUS_HOLD_VOI"
    assert amb["safe_gate"]["allow_one_pinch_routine"] is False
    assert len(amb["voi_routing"]["auto_triggered_channels"]) >= 1
    q_ids = {q["id"] for q in amb["clinical_routing"]["actionable_queries"]}
    assert {"req_ekg", "ch_hie_outside"} <= q_ids

    # Shock (syn-ED-008) & STEMI missing D2B (syn-ED-011): Hard Trigger + High Risk != High VOI
    for acute_id in ("syn-ED-008", "syn-ED-011"):
        pkt_a = evaluated[acute_id]
        assert pkt_a["safe_gate"]["gate_state"] == "SENTINEL_ESCALATION"
        assert pkt_a["safe_gate"]["allow_one_pinch_routine"] is False
        assert pkt_a["voi_routing"]["triage_voi_zero_already_above_tau"] is True
        assert pkt_a["clinical_routing"]["hard_trigger"]["active"] is True


# ---------------------------------------------------------------------------
# 5. HTTP Server Endpoints & SHA-256 Hash-Chained Shadow Ledger Roundtrip
# ---------------------------------------------------------------------------


class _BytesSocket:
    """In-memory socket shim for deterministic, non-blocking BaseHTTPRequestHandler tests."""

    def __init__(self, req_bytes: bytes) -> None:
        self._rfile = io.BytesIO(req_bytes)
        self.wfile = io.BytesIO()

    def makefile(self, mode: str, *args: Any, **kwargs: Any) -> io.BytesIO:
        return self._rfile if "r" in mode else self.wfile

    def sendall(self, data: bytes) -> None:
        self.wfile.write(data)


def _dispatch_raw_http(
    handler_cls: type,
    path: str,
    *,
    method: str = "GET",
    payload: Any | None = None,
) -> tuple[int, str, bytes]:
    """Drive BaseHTTPRequestHandler purely in-memory via _BytesSocket (zero OS socket blocking)."""
    body_bytes = json.dumps(payload).encode("utf-8") if payload is not None else b""
    headers = [
        f"{method} {path} HTTP/1.1",
        "Host: localhost",
        "Connection: close",
    ]
    if payload is not None:
        headers.append("Content-Type: application/json")
        headers.append(f"Content-Length: {len(body_bytes)}")
    raw_req = ("\r\n".join(headers) + "\r\n\r\n").encode("utf-8") + body_bytes

    shim = _BytesSocket(raw_req)
    handler_cls(shim, ("127.0.0.1", 8765), None)
    raw_resp = shim.wfile.getvalue()
    head_bytes, _, resp_body = raw_resp.partition(b"\r\n\r\n")
    head_lines = head_bytes.decode("iso-8859-1").split("\r\n")
    status_code = int(head_lines[0].split()[1])
    ctype = ""
    for ln in head_lines[1:]:
        if ln.lower().startswith("content-type:"):
            ctype = ln.split(":", 1)[1].strip()
    return status_code, ctype, resp_body


def _http_json(
    handler_cls: type,
    path: str,
    *,
    method: str = "GET",
    payload: Any | None = None,
) -> tuple[int, Any]:
    status, _, body = _dispatch_raw_http(handler_cls, path, method=method, payload=payload)
    return status, json.loads(body.decode("utf-8"))


def test_http_api_and_hash_chained_ledger_roundtrip(
    safety_engine: EDStatisticalSafetyEngine,
    tmp_path: Path,
) -> None:
    ledger_file = tmp_path / "decisions.jsonl"
    app_state = EDimbESSAppState(engine=safety_engine, ledger_path=ledger_file)
    handler_cls = make_handler(app_state)

    # 1. Static index.html & /api/health
    st_root, ctype_root, html_bytes = _dispatch_raw_http(handler_cls, "/")
    assert st_root == 200
    assert "text/html" in ctype_root
    html_text = html_bytes.decode("utf-8")
    assert "ED Triage · Provider-in-the-Middle" in html_text
    assert "pitm-three-col" in html_text

    st_h, health = _http_json(handler_cls, "/api/health")
    assert st_h == 200 and health["status"] == "ok"
    assert health["packet_count"] == 22

    # 2. /api/intake rejects PHI and accepts synthetic packet
    st_phi, err_phi = _http_json(
        handler_cls,
        "/api/intake",
        method="POST",
        payload={
            "packet_id": "syn-ED-088",
            "name": "Alice Doe",
            "encounter": {},
            "dossier": {},
        },
    )
    assert st_phi == 400
    assert "PHI" in err_phi["error"]

    clean_pkt = {**SEED_PACKETS[5], "packet_id": "syn-ED-088"}
    st_ok, in_ok = _http_json(handler_cls, "/api/intake", method="POST", payload=clean_pkt)
    assert st_ok == 201 and in_ok["accepted"] == 1

    # 3. /api/evaluate unlocks VOI channels
    st_ev, ev_res = _http_json(
        handler_cls,
        "/api/evaluate",
        method="POST",
        payload={"packet_id": "syn-ED-088", "unlocked_channels": ["ch_hie_outside"]},
    )
    assert st_ev == 200
    ch_map = {c["channel_id"]: c for c in ev_res["packet"]["voi_routing"]["channels"]}
    assert ch_map["ch_hie_outside"]["unlocked"] is True

    # 4. /api/ledger enforces >=8 char overrideReason on 'change'
    st_bad_l, _ = _http_json(
        handler_cls,
        "/api/ledger",
        method="POST",
        payload={
            "packetId": "syn-ED-022",
            "decisionType": "triage_status",
            "providerAction": "change",
            "overrideReason": "short",
        },
    )
    assert st_bad_l == 400

    # Submit 10 valid decisions so Neyman two-phase shadow ledger audit activates
    batch = []
    for idx in range(10):
        is_chg = idx % 3 == 0
        batch.append(
            {
                "packetId": f"syn-ED-0{(idx % 9) + 11}",
                "decisionType": "triage_status",
                "providerAction": "change" if is_chg else "accept",
                "overrideReason": "Upgraded to ESI-2 for rising tachycardia" if is_chg else None,
                "gateState": "SENTINEL_ESCALATION",
                "modelRecommendation": {"alert": idx % 2 == 0},
                "finalValue": {"triage_status": "ESI-2"},
            }
        )
    st_l, led_res = _http_json(
        handler_cls, "/api/ledger", method="POST", payload={"entries": batch}
    )
    assert st_l == 201
    assert led_res["total_entries"] == 10
    assert led_res["valid_hash_chain"] is True
    assert led_res["two_phase_shadow_audit"]["status"] == "audited"

    # 5. Verify hash-chain tamper detection
    lines = ledger_file.read_text(encoding="utf-8").splitlines()
    tampered = json.loads(lines[4])
    tampered["providerAction"] = "decline"
    lines[4] = json.dumps(tampered)
    ledger_file.write_text("\n".join(lines) + "\n", encoding="utf-8")

    _, valid_after_tamper = app_state.read_ledger()
    assert valid_after_tamper is False

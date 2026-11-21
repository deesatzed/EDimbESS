"""Martingale-centered Gauss-Hermite VOI routing, tie-smoothed CfBH, and Dual Conformal Safe-Gate."""

from __future__ import annotations

import hashlib
import json
import threading
import warnings
from dataclasses import asdict, dataclass
from typing import Any

import lightgbm as lgb
import numpy as np
import pandas as pd
from scipy.optimize import minimize
from scipy.special import expit, logit
from sklearn.metrics import average_precision_score, roc_auc_score

from edimbess.calibration_cohort import (
    BASE_SEMANTIC_TYPES,
    ED_STAGE2_CHANNELS,
    PlattLogitCalibrator,
    Stage2ChannelSpec,
    TabularPreprocessor,
    fit_calibrated_gbdt,
    generate_ed_calibration_cohort,
    packet_to_feature_frames,
)
from edimbess.contracts import (
    PITM_CONTRACTS,
    PITMDecisionContract,
    SplitProtocol,
    capacity_adjusted_space_threshold,
    freeze_split,
    instance_evidence_deficit,
    nadeau_bengio_tost,
    wilson_interval,
)
from edimbess.orthogonal_delta import run_edimbess_7agent_swarm
from edimbess.sentinels_and_rules import (
    SEED_PACKETS,
    compute_pitm_five_decisions,
    detect_must_know_sentinels,
    ekg_chip_for,
)
from edimbess.two_phase import (
    compute_oof_phase1_baseline,
    discover_sequential_conditional_gain,
    sample_two_phase_pilot,
)

_PROB_EPS = 1e-9

# 9-node Gauss-Hermite quadrature nodes and weights for E_{xi ~ N(0, 1)}[g(xi)]
_GH_X_RAW, _GH_W_RAW = np.polynomial.hermite.hermgauss(9)
_GH_NODES = _GH_X_RAW * np.sqrt(2.0)
_GH_WEIGHTS = _GH_W_RAW / np.sqrt(np.pi)

FRAMING_BLIND_DROP_COLS: tuple[str, ...] = ("minimizing_framing_flag", "charted_esi_num")


@dataclass(frozen=True, slots=True)
class CalibrationDiagnostics:
    """Probability calibration & discrimination summary (Van Calster et al., 2019)."""

    n_eval: int
    prevalence: float
    auroc: float
    auprc: float
    ece: float
    brier: float
    cal_intercept: float
    cal_slope: float

    def to_dict(self) -> dict[str, float | int]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class ConformalFDRSummary:
    """Positive-alert selection audit comparing Jin & Candes (2023) CfBH vs comparators."""

    method: str
    target_fdr: float
    target_ppv: float
    n_selected: int
    selection_rate: float
    realized_fdp: float
    realized_ppv: float
    realized_recall: float

    def to_dict(self) -> dict[str, float | int | str]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class FlipDecomposition:
    """Decision-flip Vickers-Elkin Net Benefit decomposition per 1,000 ED arrivals."""

    policy: str
    queried_rate: float
    decision_flip_rate: float
    unchanged_queried_rate: float
    above_tau_unchanged_queried_rate: float
    beneficial_flip_nb_gain_per_1k: float
    harmful_flip_nb_loss_per_1k: float
    query_fee_nb_drag_per_1k: float
    net_nb_delta_vs_stage1_per_1k: float

    def to_dict(self) -> dict[str, float | str]:
        return asdict(self)


def solve_mean_preserving_logit_offset(p1: np.ndarray, sigma: np.ndarray) -> np.ndarray:
    """Solve sum_r omega_r expit(logit(p_1) + mu* + sigma * xi_r) = p_1 on 9-node GH quadrature.

    Guarantees coherent martingale mean preservation E[p_2 | p_1] = p_1 so that:
    1. VOI(p_1) >= 0 identically without post-hoc clipping artifacts.
    2. VOI(p_1) -> 0 both far below tau* AND far above tau* (High Risk != High VOI).
    """
    p = np.clip(np.asarray(p1, dtype=float).ravel(), _PROB_EPS, 1.0 - _PROB_EPS)
    sig = np.maximum(0.0, np.asarray(sigma, dtype=float).ravel())
    zero_sig = sig <= 1e-12
    if np.all(zero_sig):
        return np.zeros_like(p)

    lp = logit(p)
    mu = -0.5 * (sig**2) * (1.0 - 2.0 * p) / (1.0 + 0.35 * (sig**2))
    nodes = _GH_NODES[None, :]
    weights = _GH_WEIGHTS[None, :]
    base_arg = lp[:, None] + sig[:, None] * nodes

    for _ in range(10):
        probs = expit(base_arg + mu[:, None])
        f_val = np.sum(weights * probs, axis=1) - p
        if float(np.max(np.abs(f_val))) < 1e-14:
            break
        f_der = np.sum(weights * probs * (1.0 - probs), axis=1)
        mu = mu - np.clip(f_val / np.maximum(1e-14, f_der), -4.0, 4.0)

    mu[zero_sig] = 0.0
    return mu


def _bayes_action_loss(p: np.ndarray, cost_fp: float, cost_fn: float) -> np.ndarray:
    return np.minimum(p * cost_fn, (1.0 - p) * cost_fp)


def compute_calibration_summary(
    y_true: np.ndarray,
    prob: np.ndarray,
    n_bins: int = 12,
) -> CalibrationDiagnostics:
    """Compute AUROC, AUPRC, quantile-binned ECE, Brier, Cox intercept, and Cox slope."""
    y = np.asarray(y_true, dtype=int).ravel()
    yf = y.astype(float)
    p = np.clip(np.asarray(prob, dtype=float).ravel(), _PROB_EPS, 1.0 - _PROB_EPS)
    n_tot = int(y.size)
    if n_tot == 0:
        return CalibrationDiagnostics(0, 0.0, 0.5, 0.0, 0.0, 0.0, 0.0, 1.0)

    prev = float(np.mean(yf))
    brier = float(np.mean((yf - p) ** 2))
    if np.unique(y).size < 2:
        return CalibrationDiagnostics(
            n_tot, prev, 0.5, prev, abs(prev - float(np.mean(p))), brier, 0.0, 1.0
        )

    auroc = float(roc_auc_score(y, p))
    auprc = float(average_precision_score(y, p))

    edges = np.unique(np.quantile(p, np.linspace(0.0, 1.0, n_bins + 1)))
    if edges.size <= 2:
        ece = abs(float(np.mean(yf) - np.mean(p)))
    else:
        bins = np.clip(np.digitize(p, edges[1:-1], right=False), 0, edges.size - 2)
        ece = 0.0
        for b in range(edges.size - 1):
            m = bins == b
            cnt = int(np.sum(m))
            if cnt > 0:
                ece += (cnt / float(n_tot)) * abs(float(np.mean(yf[m]) - np.mean(p[m])))

    lp = logit(p)
    if float(np.std(lp)) < 1e-8:
        return CalibrationDiagnostics(n_tot, prev, auroc, auprc, float(ece), brier, 0.0, 1.0)

    def _nll_int(a_arr: np.ndarray) -> float:
        eta = a_arr[0] + lp
        return float(np.mean(np.logaddexp(0.0, eta) - yf * eta))

    res_int = minimize(_nll_int, x0=np.array([0.0]), method="BFGS")
    cal_int = float(res_int.x[0]) if res_int.success else 0.0

    def _nll_slope(ab: np.ndarray) -> float:
        eta = ab[0] + ab[1] * lp
        return float(np.mean(np.logaddexp(0.0, eta) - yf * eta) + 1e-6 * (ab[1] - 1.0) ** 2)

    res_sl = minimize(_nll_slope, x0=np.array([0.0, 1.0]), method="BFGS")
    cal_slope = float(res_sl.x[1]) if res_sl.success else 1.0

    return CalibrationDiagnostics(
        n_eval=n_tot,
        prevalence=prev,
        auroc=auroc,
        auprc=auprc,
        ece=float(ece),
        brier=brier,
        cal_intercept=cal_int,
        cal_slope=cal_slope,
    )


def compute_conformal_negative_pvalues(
    cal_scores: np.ndarray,
    y_cal: np.ndarray,
    query_scores: np.ndarray,
    *,
    seed: int = 42,
) -> np.ndarray:
    """Compute tie-smoothed Jin & Candes (2023) conformal p-values against calibration negatives.

    CRITICAL INVARIANT: Uniform tie-smoother U_j multiplies ONLY tied negatives n_eq,
    never the +1 numerator term: p_conf = (1.0 + n_gt + u_tie * n_eq) / (1 + n_cal_neg).
    """
    yc = np.asarray(y_cal, dtype=int).ravel()
    sc = np.asarray(cal_scores, dtype=float).ravel()
    neg_cal = np.sort(sc[yc == 0])
    sq = np.asarray(query_scores, dtype=float).ravel()
    m0 = int(neg_cal.size)
    if m0 == 0 or sq.size == 0:
        return np.ones(sq.size, dtype=float)

    idx_left = np.searchsorted(neg_cal, sq, side="left")
    idx_right = np.searchsorted(neg_cal, sq, side="right")
    n_gt = (m0 - idx_right).astype(float)
    n_eq = (idx_right - idx_left).astype(float)
    u_tie = np.random.default_rng(seed).uniform(0.0, 1.0, size=sq.size)
    return (1.0 + n_gt + u_tie * n_eq) / float(1 + m0)


def conformal_fdr_selection(
    cal_scores: np.ndarray,
    y_cal: np.ndarray,
    test_scores: np.ndarray,
    y_test: np.ndarray,
    target_fdr: float = 0.35,
    seed: int = 42,
) -> tuple[ConformalFDRSummary, np.ndarray, np.ndarray]:
    """Run Jin & Candes (2023) CfBH conformal selection and return (summary, mask, p_conf)."""
    yt = np.asarray(y_test, dtype=int).ravel()
    st = np.asarray(test_scores, dtype=float).ravel()
    n_test = int(st.size)
    target_ppv = 1.0 - float(target_fdr)
    p_conf = compute_conformal_negative_pvalues(cal_scores, y_cal, st, seed=seed)

    if n_test == 0:
        return (
            ConformalFDRSummary("CfBH", float(target_fdr), target_ppv, 0, 0.0, 0.0, 1.0, 0.0),
            np.zeros(0, dtype=bool),
            p_conf,
        )

    order = np.argsort(p_conf)
    p_sorted = p_conf[order]
    thresh = float(target_fdr) * (np.arange(1, n_test + 1, dtype=float) / float(n_test))
    passing = np.flatnonzero(p_sorted <= thresh)
    sel_mask = np.zeros(n_test, dtype=bool)
    if passing.size > 0:
        k_hat = int(passing[-1]) + 1
        sel_mask[order[:k_hat]] = True

    n_sel = int(np.sum(sel_mask))
    if n_sel == 0:
        return (
            ConformalFDRSummary("CfBH", float(target_fdr), target_ppv, 0, 0.0, 0.0, 1.0, 0.0),
            sel_mask,
            p_conf,
        )

    tp = int(np.sum((yt == 1) & sel_mask))
    fp = n_sel - tp
    fdp = float(fp) / float(n_sel)
    ppv = float(tp) / float(n_sel)
    rec = float(tp) / float(max(1, int(np.sum(yt == 1))))
    return (
        ConformalFDRSummary(
            method="CfBH",
            target_fdr=float(target_fdr),
            target_ppv=target_ppv,
            n_selected=n_sel,
            selection_rate=float(n_sel) / float(n_test),
            realized_fdp=fdp,
            realized_ppv=ppv,
            realized_recall=rec,
        ),
        sel_mask,
        p_conf,
    )


def mondrian_singleton_positive_selection(
    cal_scores: np.ndarray,
    y_cal: np.ndarray,
    test_scores: np.ndarray,
    y_test: np.ndarray,
    alpha: float = 0.10,
) -> ConformalFDRSummary:
    """Naive Mondrian {1} positive-conformity comparator (illustrating FDP inflation under imbalance).

    Flags any encounter whose positive-class conformity p_pos(x) > alpha without controlling
    false discoveries from the dominant negative class.
    """
    yc = np.asarray(y_cal, dtype=int).ravel()
    yt = np.asarray(y_test, dtype=int).ravel()
    pos_cal = np.sort(np.asarray(cal_scores, dtype=float).ravel()[yc == 1])
    st = np.asarray(test_scores, dtype=float).ravel()
    if pos_cal.size == 0 or st.size == 0:
        return ConformalFDRSummary("Mondrian_{1}", alpha, 1.0 - alpha, 0, 0.0, 0.0, 1.0, 0.0)

    n_le = np.searchsorted(pos_cal, st, side="right").astype(float)
    p_pos = (1.0 + n_le) / float(1 + pos_cal.size)
    sel_mask = p_pos > float(alpha)
    n_sel = int(np.sum(sel_mask))
    if n_sel == 0:
        return ConformalFDRSummary("Mondrian_{1}", alpha, 1.0 - alpha, 0, 0.0, 0.0, 1.0, 0.0)

    tp = int(np.sum((yt == 1) & sel_mask))
    fdp = float(n_sel - tp) / float(n_sel)
    ppv = float(tp) / float(n_sel)
    rec = float(tp) / float(max(1, int(np.sum(yt == 1))))
    return ConformalFDRSummary(
        method="Mondrian_{1}",
        target_fdr=float(alpha),
        target_ppv=1.0 - float(alpha),
        n_selected=n_sel,
        selection_rate=float(n_sel) / float(st.size),
        realized_fdp=fdp,
        realized_ppv=ppv,
        realized_recall=rec,
    )


def decompose_routing_flips(
    policy_name: str,
    y_true: np.ndarray,
    p1: np.ndarray,
    p2: np.ndarray,
    query_mask: np.ndarray,
    contract: PITMDecisionContract,
    per_instance_query_cost: float,
) -> FlipDecomposition:
    """Decompose Vickers-Elkin Net Benefit delta (per 1,000 arrivals) into flips and query drag."""
    yt = np.asarray(y_true, dtype=int).ravel()
    q_m = np.asarray(query_mask, dtype=bool).ravel()
    n_t = max(1.0, float(yt.size))
    c_fp = float(contract.cost_fp)
    c_fn = max(1e-9, float(contract.cost_fn))
    tau_star = contract.bayes_threshold

    p_route = np.where(q_m, p2, p1)
    a1 = p1 >= tau_star
    ar = p_route >= tau_star

    tp_gained = float(np.sum((~a1) & ar & (yt == 1)))
    fp_avoided = float(np.sum(a1 & (~ar) & (yt == 0)))
    ben_nb_1k = (1000.0 / n_t) * (tp_gained + (c_fp / c_fn) * fp_avoided)

    tp_lost = float(np.sum(a1 & (~ar) & (yt == 1)))
    fp_added = float(np.sum((~a1) & ar & (yt == 0)))
    harm_nb_1k = (1000.0 / n_t) * (tp_lost + (c_fp / c_fn) * fp_added)

    n_q = float(np.sum(q_m))
    fee_drag_1k = (1000.0 / n_t) * (float(per_instance_query_cost) / c_fn) * n_q
    net_delta_1k = ben_nb_1k - harm_nb_1k - fee_drag_1k

    flips = a1 != ar
    unchanged_q = q_m & (~flips)
    above_tau_unchanged_q = q_m & a1 & ar

    return FlipDecomposition(
        policy=policy_name,
        queried_rate=float(n_q / n_t),
        decision_flip_rate=float(np.sum(flips) / n_t),
        unchanged_queried_rate=float(np.sum(unchanged_q) / n_t),
        above_tau_unchanged_queried_rate=float(np.sum(above_tau_unchanged_q) / n_t),
        beneficial_flip_nb_gain_per_1k=float(ben_nb_1k),
        harmful_flip_nb_loss_per_1k=float(harm_nb_1k),
        query_fee_nb_drag_per_1k=float(fee_drag_1k),
        net_nb_delta_vs_stage1_per_1k=float(net_delta_1k),
    )


class StreamingVOIRouter:
    """Martingale-centered 9-node Gauss-Hermite conditional Stage-2 VOI estimator."""

    def __init__(self, n_voi_bins: int = 10) -> None:
        self.n_voi_bins = int(n_voi_bins)
        self.bin_edges_: np.ndarray = np.array([0.0, 1.0])
        self.bin_logit_sd_: np.ndarray = np.array([0.5])

    def fit(self, p1_cal: np.ndarray, p2_cal: np.ndarray) -> StreamingVOIRouter:
        p1 = np.clip(np.asarray(p1_cal, dtype=float).ravel(), _PROB_EPS, 1.0 - _PROB_EPS)
        p2 = np.clip(np.asarray(p2_cal, dtype=float).ravel(), _PROB_EPS, 1.0 - _PROB_EPS)
        delta_logit = logit(p2) - logit(p1)

        edges = np.unique(np.quantile(p1, np.linspace(0.0, 1.0, self.n_voi_bins + 1)))
        if edges.size <= 2:
            self.bin_edges_ = np.array([0.0, 1.0])
            sd_g = float(np.std(delta_logit, ddof=1)) if delta_logit.size > 1 else 0.4
            self.bin_logit_sd_ = np.array([max(0.02, sd_g)])
            return self

        self.bin_edges_ = edges
        bins = np.clip(np.digitize(p1, edges[1:-1], right=False), 0, edges.size - 2)
        global_sd = max(0.05, float(np.std(delta_logit, ddof=1)))
        sds = np.empty(edges.size - 1, dtype=float)
        for b in range(edges.size - 1):
            m = bins == b
            if int(np.sum(m)) >= 12:
                sds[b] = max(0.02, float(np.std(delta_logit[m], ddof=1)))
            else:
                sds[b] = global_sd
        self.bin_logit_sd_ = sds
        return self

    def lookup_sigma(self, p1: np.ndarray | float) -> np.ndarray:
        p = np.clip(np.asarray(p1, dtype=float).ravel(), _PROB_EPS, 1.0 - _PROB_EPS)
        if self.bin_edges_.size <= 2:
            return np.full(p.size, self.bin_logit_sd_[0], dtype=float)
        bins = np.clip(
            np.digitize(p, self.bin_edges_[1:-1], right=False), 0, self.bin_edges_.size - 2
        )
        return self.bin_logit_sd_[bins]

    def score_voi(
        self,
        p1: np.ndarray | float,
        *,
        cost_fp: float,
        cost_fn: float,
        sigma_override: np.ndarray | None = None,
    ) -> np.ndarray:
        p = np.clip(np.asarray(p1, dtype=float).ravel(), _PROB_EPS, 1.0 - _PROB_EPS)
        lp = logit(p)
        sig = (
            np.maximum(0.02, np.asarray(sigma_override, dtype=float).ravel())
            if sigma_override is not None
            else self.lookup_sigma(p)
        )
        mu = solve_mean_preserving_logit_offset(p, sig)
        p2_nodes = expit(lp[:, None] + mu[:, None] + sig[:, None] * _GH_NODES[None, :])
        curr_loss = _bayes_action_loss(p, cost_fp, cost_fn)
        exp_post_loss = np.sum(
            _GH_WEIGHTS[None, :] * _bayes_action_loss(p2_nodes, cost_fp, cost_fn), axis=1
        )
        return np.maximum(0.0, curr_loss - exp_post_loss)


def _build_clinical_routing_summary(
    *,
    packet: dict[str, Any],
    ekg_chip: dict[str, Any],
    sentinels: list[dict[str, Any]],
    recommendations: list[dict[str, Any]],
    swarm: dict[str, Any],
    gate_state: str,
    unlocked_set: set[str],
    already_above_tau: bool,
) -> dict[str, Any]:
    """Translate engine decisions into zero-jargon ED clinical routing & actionable checks."""
    enc = packet.get("encounter", {})
    vitals = enc.get("vitals") or {}
    evals = enc.get("evals") or {}
    card = swarm.get("synthesizer") or {}
    findings = card.get("findings") or []

    sbp = vitals.get("sbp")
    spo2 = vitals.get("spo2")
    gcs = vitals.get("gcs")

    # 1. Hard Escalation Banner (matches SDR hard-trigger immediacy)
    hard_banner: str | None = None
    if evals.get("d2b") == "missing":
        hard_banner = "HARD ESCALATION — STEMI on Triage ECG · D2B Cath Activation Missing"
    elif evals.get("trauma") == "missing" and sbp is not None and float(sbp) < 90:
        hard_banner = (
            f"HARD ESCALATION — Hypotensive Shock (SBP {sbp}) · Arrival Trauma Eval Missing"
        )
    elif evals.get("neuro") == "missing":
        hard_banner = "HARD ESCALATION — Acute Focal Deficit · Arrival Stroke / Neuro Eval Missing"
    elif sbp is not None and float(sbp) < 90:
        hard_banner = f"HARD ESCALATION — Hemodynamic Instability (SBP {sbp} mmHg)"
    elif spo2 is not None and float(spo2) < 90:
        hard_banner = (
            f"HARD ESCALATION — Acute Hypoxemia (SpO2 {spo2}%) + Respiratory Reserve Threat"
        )
    elif gcs is not None and int(gcs) < 15:
        hard_banner = (
            f"HARD ESCALATION — Declining Mental Status (GCS {gcs}) + Concealed Chart Risk"
        )
    elif any(s["category"] == "HIGH_RISK_MECHANISM_ACTIVITY" for s in sentinels):
        hard_banner = "HARD ESCALATION — Benign-Appearing Exam with Limb / Ocular / Spine Emergency"

    # 2. Plain-English WHY QUEUED chips (zero Greek letters or model jargon)
    why_chips: list[str] = []
    for k_ev, lbl_ev in (
        ("d2b", "D2B eval missing"),
        ("trauma", "Trauma eval missing"),
        ("neuro", "Neuro eval missing"),
    ):
        if evals.get(k_ev) == "missing":
            why_chips.append(lbl_ev)

    for s in sentinels:
        cat = s["category"]
        if cat == "ANTICOAGULATED_COAGULOPATHIC":
            why_chips.append(
                "Blood thinner on chart (not in triage note)"
                if not s.get("in_nurse_note")
                else "Anticoagulated / coagulopathic"
            )
        elif cat == "IMMUNOCOMPROMISED":
            why_chips.append("Immunosuppressed / transplant reserve")
        elif cat == "HIGH_RISK_MECHANISM_ACTIVITY":
            why_chips.append("High-energy / surgical mechanism")
        elif cat == "HIGH_RISK_PMH":
            why_chips.append(s["summary"].split(";")[0])
        elif cat == "BOUNCEBACK_FREQUENT_ED":
            why_chips.append("72h bounceback / repeat ED return")
        elif cat == "UNEXPLAINED_ABNORMAL_VS":
            why_chips.append(s["summary"].split(";")[0])

    for f in findings:
        f_low = f["finding"].lower()
        if "esbl" in f_low:
            why_chips.append("Unreviewed ESBL resistant culture")
        elif "glipizide" in f_low:
            why_chips.append("Sulfonylurea hypoglycemia risk")
        elif "hemoglobin" in f_low:
            why_chips.append(f["finding"].split(" omitted")[0])

    if ekg_chip.get("status") == "pending" and "req_ekg" not in unlocked_set:
        why_chips.append("Triage ECG pending")
    if enc.get("outside_hie_available") is False and "ch_hie_outside" not in unlocked_set:
        why_chips.append("Outside hospital HIE unqueried")

    # Deduplicate while keeping order, cap at 4 chips
    dedup_chips = list(dict.fromkeys(why_chips))[:4]
    if not dedup_chips:
        active_meds = recommendations[4]["value"].get("meds") or []
        active_ords = recommendations[3]["value"].get("items") or []
        if active_ords:
            dedup_chips.append(f"Sign off WR labs ({', '.join(active_ords[:3])})")
        if active_meds:
            dedup_chips.append(f"Sign off {', '.join(m.split(' (')[0] for m in active_meds)}")
        if not dedup_chips:
            dedup_chips.append("Safe fast-pass — standing care already active")

    # 3. Plain-English IF MISSED / IF WRONG worst-case consequence line
    if_missed: str | None = None
    if findings:
        if_missed = f"{findings[0]['why_now']} ({findings[0]['action']})"
    elif sentinels:
        if_missed = sentinels[0]["clinical_why"]
    elif ekg_chip.get("status") == "pending" and "req_ekg" not in unlocked_set:
        if_missed = "Unscreened arrhythmia or conduction block if placed in unmonitored waiting room before ECG."

    # 4. Concrete Actionable Checks / Bedside Questions (ONLY shown when immediately actionable!)
    actionable_queries: list[dict[str, Any]] = []
    if ekg_chip.get("status") == "pending":
        actionable_queries.append(
            {
                "id": "req_ekg",
                "kind": "nursing_check",
                "button_label": "Flag Nurse: Stat Triage ECG",
                "prompt": "Triage 12-lead ECG not yet recorded for dizziness / cardiopulmonary complaint.",
                "if_positive_impact": "Screens STEMI, high-grade AV block, or arrhythmia before waitboard.",
                "completed": "req_ekg" in unlocked_set,
            }
        )
    if enc.get("outside_hie_available") is False and not already_above_tau:
        actionable_queries.append(
            {
                "id": "ch_hie_outside",
                "kind": "external_fetch",
                "button_label": "Pull Regional HIE Records",
                "prompt": "Outside hospital HIE feed disconnected on arrival.",
                "if_positive_impact": "Checks <72h outside ED visits, CT imaging, and microbiology.",
                "completed": "ch_hie_outside" in unlocked_set,
            }
        )

    finding_by_id = {f["id"]: f for f in findings}
    for aq in card.get("ask_patient") or []:
        f_match = finding_by_id.get(aq.get("for_finding"), {})
        q_id = f"ask_{aq.get('for_finding', 'q')}"
        actionable_queries.append(
            {
                "id": q_id,
                "kind": "bedside_question",
                "button_label": "Send to Nurse / Bedside",
                "prompt": aq["question"],
                "if_positive_impact": f_match.get("action")
                or "Confirms exposure timing and acute workup.",
                "completed": q_id in unlocked_set,
            }
        )

    # 5. Headline Decision & Explicit Downstream Station Routing ("What Goes Where")
    charted_esi = str(enc.get("triage_status", "ESI-3"))
    prop_esi = str(recommendations[0]["value"]["triage_status"])
    eval_needs = recommendations[1]["value"].get("needs") or []
    space_prio = str(recommendations[2]["value"]["priority"])
    wr_items = recommendations[3]["value"].get("items") or []
    wr_meds = recommendations[4]["value"].get("meds") or []

    esi_phrase = (
        f"Upgrade {charted_esi} → {prop_esi}" if prop_esi != charted_esi else f"Keep {prop_esi}"
    )
    space_short = {
        "immediate": "Immediate Resus Bay",
        "next_bed": "Next Staffed Bed",
        "bed": "Staffed ED Bed",
        "hallway": "Monitored Hallway",
        "fast_track": "Fast Track",
        "waiting_room": "Waitboard",
        "defer": "Defer Space",
    }.get(space_prio, space_prio)

    headline_parts = [esi_phrase, space_short]
    if eval_needs:
        headline_parts.append(f"Trigger {'/'.join(x.upper() for x in eval_needs)} Eval")
    elif wr_items:
        headline_parts.append(
            f"Order {', '.join(wr_items[:2])}" + ("+" if len(wr_items) > 2 else "")
        )
    elif wr_meds:
        headline_parts.append(", ".join(m.split(" (")[0] for m in wr_meds))
    headline_decision = " · ".join(headline_parts)

    routes: list[dict[str, Any]] = []
    # Station 1: Alert / Resus Team
    if (
        eval_needs
        or prop_esi == "ESI-1"
        or space_prio == "immediate"
        or any(s["category"] == "HIGH_RISK_MECHANISM_ACTIVITY" for s in sentinels)
    ):
        alert_bits: list[str] = []
        if eval_needs:
            alert_bits.append(f"Activate {' & '.join(x.upper() for x in eval_needs)} team")
        if prop_esi != charted_esi:
            alert_bits.append(f"Acuity {charted_esi} → {prop_esi}")
        if card.get("safety_net"):
            alert_bits.append(card["safety_net"][0]["action"])
        routes.append(
            {
                "station_id": "alert_team",
                "station_label": "Alert & Resus Team",
                "summary": " · ".join(alert_bits) or f"Immediate {prop_esi} resuscitation alert",
                "urgent": True,
            }
        )

    # Station 2: Charge Nurse & Bed Board (or Fast Track)
    iso_tag = f" ({enc['isolation_flag']} Isolation)" if enc.get("isolation_flag") else ""
    if space_prio in ("fast_track", "waiting_room") and gate_state == "CERTIFIED_SAFE_ROUTINE":
        routes.append(
            {
                "station_id": "fast_track",
                "station_label": "Fast Track / Waitboard",
                "summary": f"{prop_esi} → {space_short}",
                "urgent": False,
            }
        )
    else:
        routes.append(
            {
                "station_id": "bed_board",
                "station_label": "Charge Nurse & Bed Board",
                "summary": f"{esi_phrase} → {space_short}{iso_tag}",
                "urgent": space_prio in ("immediate", "next_bed") or prop_esi != charted_esi,
            }
        )

    # Station 3: WR Orders & Pharmacy
    ord_bits: list[str] = []
    if wr_items:
        ord_bits.append(f"Orders: {', '.join(wr_items)}")
    if wr_meds:
        ord_bits.append(f"Meds: {', '.join(m.split(' (')[0] for m in wr_meds)}")
    held_note = recommendations[4].get("rationale", "")
    if "held" in held_note.lower():
        held_clause = next(
            (seg.strip() for seg in held_note.split(".") if "held" in seg.lower()), ""
        )
        if held_clause:
            ord_bits.append(held_clause)
    if ord_bits:
        routes.append(
            {
                "station_id": "wr_orders",
                "station_label": "WR Orders & Meds",
                "summary": " · ".join(ord_bits),
                "urgent": False,
            }
        )

    # Active upstream input channels that contributed signal (drives 3D upstream input beams)
    active_inputs = ["door_triage"]
    if any(
        s["category"] in ("ANTICOAGULATED_COAGULOPATHIC", "IMMUNOCOMPROMISED", "HIGH_RISK_PMH")
        for s in sentinels
    ):
        active_inputs.append("chart_pharmacy")
    if any(
        "esbl" in f["finding"].lower()
        or "hemoglobin" in f["finding"].lower()
        or "wbc" in f["finding"].lower()
        or "potassium" in f["finding"].lower()
        for f in findings
    ) or any(s["category"] == "BOUNCEBACK_FREQUENT_ED" for s in sentinels):
        active_inputs.append("labs_micro")

    return {
        "hard_trigger": {"active": hard_banner is not None, "banner": hard_banner},
        "why_queued": dedup_chips,
        "if_missed_risk": if_missed,
        "headline_decision": headline_decision,
        "what_happens_next": routes,
        "actionable_queries": actionable_queries,
        "active_inputs": active_inputs,
    }


class EDStatisticalSafetyEngine:
    """End-to-end calibrated ED Statistical Safety Engine backing PITM XR & REST endpoints."""

    def __init__(self, *, n_encounters: int = 5600, seed: int = 42) -> None:
        self.n_encounters = int(n_encounters)
        self.seed = int(seed)
        self.cohort = generate_ed_calibration_cohort(n_encounters=self.n_encounters, seed=self.seed)

        full_df = pd.concat(
            [
                self.cohort.X_base.reset_index(drop=True),
                pd.DataFrame({"patient_id": self.cohort.patient_ids, "y": self.cohort.y}),
            ],
            axis=1,
        )
        self.split: SplitProtocol = freeze_split(
            full_df, "y", group_col="patient_id", seed=self.seed
        )

        tr = list(self.split.train_idx)
        ca = list(self.split.cal_idx)
        va = list(self.split.val_idx)
        te = list(self.split.test_idx)

        self.y_train = self.cohort.y[tr]
        self.y_cal = self.cohort.y[ca]
        self.y_val = self.cohort.y[va]
        self.y_test = self.cohort.y[te]
        self.natural_prevalence = float(np.mean(self.y_train))

        # 1. Stage-1 Calibrated Baseline Model (X_base)
        self.prep_s1, self.clf_s1, self.cal_s1 = fit_calibrated_gbdt(
            self.cohort.X_base.iloc[tr],
            self.y_train,
            self.cohort.X_base.iloc[ca],
            self.y_cal,
            semantic_types=BASE_SEMANTIC_TYPES,
            seed=self.seed,
        )

        # 2. Framing-Blind Objective Stage-1 Model (drops subjective nurse framing & charted ESI)
        fb_cols = [c for c in self.cohort.X_base.columns if c not in FRAMING_BLIND_DROP_COLS]
        fb_sem = {k: v for k, v in BASE_SEMANTIC_TYPES.items() if k in fb_cols}
        self.fb_cols = fb_cols
        self.prep_fb, self.clf_fb, self.cal_fb = fit_calibrated_gbdt(
            self.cohort.X_base.iloc[tr][fb_cols],
            self.y_train,
            self.cohort.X_base.iloc[ca][fb_cols],
            self.y_cal,
            semantic_types=fb_sem,
            seed=self.seed + 7,
        )

        # 3. Stage-2 Full Dossier/HIE/Dispense Calibrated Model (X_base + Z_pool)
        X_s2_all = pd.concat([self.cohort.X_base, self.cohort.Z_pool], axis=1)
        self.prep_s2, self.clf_s2, self.cal_s2 = fit_calibrated_gbdt(
            X_s2_all.iloc[tr],
            self.y_train,
            X_s2_all.iloc[ca],
            self.y_cal,
            semantic_types=BASE_SEMANTIC_TYPES,
            seed=self.seed + 13,
        )

        # Precompute D_cal and D_test calibrated posteriors
        self.p1_cal = self._predict_calibrated(
            self.prep_s1, self.clf_s1, self.cal_s1, self.cohort.X_base.iloc[ca]
        )
        self.p1_fb_cal = self._predict_calibrated(
            self.prep_fb, self.clf_fb, self.cal_fb, self.cohort.X_base.iloc[ca][fb_cols]
        )
        self.p2_cal = self._predict_calibrated(
            self.prep_s2, self.clf_s2, self.cal_s2, X_s2_all.iloc[ca]
        )

        self.p1_test = self._predict_calibrated(
            self.prep_s1, self.clf_s1, self.cal_s1, self.cohort.X_base.iloc[te]
        )
        self.p1_fb_test = self._predict_calibrated(
            self.prep_fb, self.clf_fb, self.cal_fb, self.cohort.X_base.iloc[te][fb_cols]
        )
        self.p2_test = self._predict_calibrated(
            self.prep_s2, self.clf_s2, self.cal_s2, X_s2_all.iloc[te]
        )

        # 4. Fit aggregate Stage-2 VOI router + per-channel Stage-2 VOI routers on D_cal
        self.bundle_voi_router = StreamingVOIRouter(n_voi_bins=10).fit(self.p1_cal, self.p2_cal)
        self.channel_models: dict[
            str,
            tuple[
                TabularPreprocessor, lgb.LGBMClassifier, PlattLogitCalibrator, StreamingVOIRouter
            ],
        ] = {}
        for idx_c, ch in enumerate(ED_STAGE2_CHANNELS):
            ch_df = pd.concat([self.cohort.X_base, self.cohort.Z_pool[list(ch.columns)]], axis=1)
            p_ch, m_ch, c_ch = fit_calibrated_gbdt(
                ch_df.iloc[tr],
                self.y_train,
                ch_df.iloc[ca],
                self.y_cal,
                semantic_types=BASE_SEMANTIC_TYPES,
                seed=self.seed + 101 + idx_c,
            )
            p_ch_cal = self._predict_calibrated(p_ch, m_ch, c_ch, ch_df.iloc[ca])
            r_ch = StreamingVOIRouter(n_voi_bins=8).fit(self.p1_cal, p_ch_cal)
            self.channel_models[ch.channel_id] = (p_ch, m_ch, c_ch, r_ch)

        # 5. Calibrate Dual Conformal Safe-Gate on D_cal
        self.s_cons_cal = np.maximum(self.p1_cal, self.p1_fb_cal)
        self.pos_cal_scores = np.sort(self.s_cons_cal[self.y_cal == 1])
        self.neg_cal_p2_scores = np.sort(self.p2_cal[self.y_cal == 0])
        triage_contract = PITM_CONTRACTS["triage_status"]
        self.safe_for_alpha = float(triage_contract.safe_omission_alpha)
        self.safe_miss_alpha = 0.06
        self.tau_safe, self.cal_for_point, self.cal_for_wilson_upper = (
            self._calibrate_safe_routine_cutoff(
                self.s_cons_cal,
                self.y_cal,
                max_tau=triage_contract.bayes_threshold,
                for_alpha=self.safe_for_alpha,
            )
        )

        # 6. Precompute calibration diagnostics &CfBH cutoff on D_cal
        self.cal_diag_s1 = compute_calibration_summary(self.y_cal, self.p1_cal)
        self.cal_diag_s2 = compute_calibration_summary(self.y_cal, self.p2_cal)
        self.test_diag_s1 = compute_calibration_summary(self.y_test, self.p1_test)
        self.test_diag_s2 = compute_calibration_summary(self.y_test, self.p2_test)

        # Calibration cutoff for single-arrival CfBH alert certification at q = 0.35 (PPV >= 0.65)
        cfbh_cal_res, cfbh_cal_mask, _ = conformal_fdr_selection(
            self.p2_cal, self.y_cal, self.p2_cal, self.y_cal, target_fdr=0.35, seed=self.seed
        )
        self.cfbh_alert_cutoff = (
            float(np.min(self.p2_cal[cfbh_cal_mask])) if np.any(cfbh_cal_mask) else 0.50
        )
        self._audit_cache: dict[str, Any] | None = None

    @staticmethod
    def _predict_calibrated(
        prep: TabularPreprocessor,
        clf: lgb.LGBMClassifier,
        cal: PlattLogitCalibrator,
        df: pd.DataFrame,
    ) -> np.ndarray:
        mat = prep.transform(df)
        raw = np.asarray(clf.predict_proba(mat)[:, 1], dtype=float)
        return cal.predict_proba(raw)

    def _calibrate_safe_routine_cutoff(
        self,
        s_cal: np.ndarray,
        y_cal: np.ndarray,
        *,
        max_tau: float,
        for_alpha: float,
    ) -> tuple[float, float, float]:
        """Find largest threshold <= max_tau whose 95% Wilson FOR upper bound <= for_alpha."""
        candidates = np.unique(
            np.clip(np.quantile(s_cal, np.linspace(0.08, 0.70, 50)), 0.002, max_tau)
        )
        best_tau = 0.012
        best_pt = 0.0
        best_hi = for_alpha
        for tau in candidates:
            if tau > max_tau + 1e-9:
                continue
            mask = s_cal <= tau
            n_sub = int(np.sum(mask))
            if n_sub < 40:
                continue
            fn_sub = int(np.sum(y_cal[mask] == 1))
            _, wilson_hi = wilson_interval(fn_sub, n_sub, alpha=0.05)
            n_le_pos = int(np.searchsorted(self.pos_cal_scores, tau, side="right"))
            p_pos = (1.0 + n_le_pos) / float(1 + max(1, self.pos_cal_scores.size))
            if wilson_hi <= for_alpha and p_pos <= self.safe_miss_alpha:
                best_tau = float(tau)
                best_pt = float(fn_sub) / float(n_sub)
                best_hi = float(wilson_hi)
        return best_tau, best_pt, best_hi

    def positive_conformal_miss_pvalue(self, score: float | np.ndarray) -> np.ndarray:
        """Conformal p-value testing compatibility with the positive clinical upgrade distribution."""
        sq = np.asarray(score, dtype=float).ravel()
        if self.pos_cal_scores.size == 0:
            return np.ones_like(sq)
        n_le = np.searchsorted(self.pos_cal_scores, sq, side="right").astype(float)
        return (1.0 + n_le) / float(1 + self.pos_cal_scores.size)

    def evaluate_packet_safety(
        self,
        packet: dict[str, Any],
        capacity: dict[str, Any] | None = None,
        *,
        unlocked_channels: list[str] | None = None,
        allow_live_llm: bool = False,
    ) -> dict[str, Any]:
        """Run full Layer-0 Sentinels + 5 PITM Decisions + 7-Agent Orthogonal Swarm + Safe-Gate & VOI."""
        cap = capacity or {
            "bedsOpen": 3,
            "bedsTotal": 18,
            "hallwayOpen": 2,
            "hallwayTotal": 8,
            "fastTrackOpen": 2,
            "fastTrackTotal": 4,
            "waitboard": 14,
        }
        enc = packet.get("encounter", {})
        evals = enc.get("evals") or {}
        ekg_chip = ekg_chip_for(packet)
        sentinels = detect_must_know_sentinels(packet)
        recommendations = compute_pitm_five_decisions(packet, cap, sentinels=sentinels)
        swarm = run_edimbess_7agent_swarm(
            packet, cap, sentinels=sentinels, allow_live_llm=allow_live_llm
        )

        X_row, Z_row = packet_to_feature_frames(packet)
        # Respect external HIE missingness when packet explicitly marks outside_hie_available=False
        unlocked_set = set(unlocked_channels or [])
        if enc.get("outside_hie_available") is False and "ch_hie_outside" not in unlocked_set:
            Z_row = Z_row.copy()
            Z_row["hie_bounceback_72h"] = np.nan
            Z_row["hie_ed_visits_90d"] = np.nan

        p1 = float(self._predict_calibrated(self.prep_s1, self.clf_s1, self.cal_s1, X_row)[0])
        p1_fb = float(
            self._predict_calibrated(self.prep_fb, self.clf_fb, self.cal_fb, X_row[self.fb_cols])[0]
        )
        p2 = float(
            self._predict_calibrated(
                self.prep_s2, self.clf_s2, self.cal_s2, pd.concat([X_row, Z_row], axis=1)
            )[0]
        )

        s_cons = max(p1, p1_fb)
        triage_contract = PITM_CONTRACTS["triage_status"]
        space_contract = PITM_CONTRACTS["space_priority"]
        tau_triage = triage_contract.bayes_threshold
        tau_space = capacity_adjusted_space_threshold(
            space_contract,
            int(cap.get("bedsOpen", 3)),
            int(cap.get("bedsTotal", 18)),
        )

        p_miss = float(self.positive_conformal_miss_pvalue(s_cons)[0])
        p_neg_s2 = float(
            compute_conformal_negative_pvalues(
                self.p2_cal, self.y_cal, np.array([p2]), seed=self.seed
            )[0]
        )
        ev_def = float(
            np.asarray(
                instance_evidence_deficit(
                    np.array([s_cons]), self.natural_prevalence, triage_contract.target_precision
                )
            ).ravel()[0]
        )
        framing_div_nats = float(
            max(
                0.0,
                logit(np.clip(p1_fb, _PROB_EPS, 1.0 - _PROB_EPS))
                - logit(np.clip(p1, _PROB_EPS, 1.0 - _PROB_EPS)),
            )
        )

        # Determine which Stage-2 channels are already present in the local EHR dossier vs unobserved
        dos = packet.get("dossier", {})
        has_local_labs_or_di = bool(dos.get("recent_labs") or dos.get("recent_di"))
        ask_items = swarm["synthesizer"].get("ask_patient") or []
        unobserved_channel_ids: set[str] = set()
        if enc.get("outside_hie_available") is False and "ch_hie_outside" not in unlocked_set:
            unobserved_channel_ids.add("ch_hie_outside")
        if (
            not has_local_labs_or_di
            and "ch_recent_labs_di" not in unlocked_set
            and s_cons > self.tau_safe
        ):
            unobserved_channel_ids.add("ch_recent_labs_di")
        if "ch_bedside_ask_probe" not in unlocked_set and (
            len(ask_items) > 0 or s_cons > self.tau_safe
        ):
            unobserved_channel_ids.add("ch_bedside_ask_probe")

        # Per-channel Martingale Gauss-Hermite VOI quotes at the Triage Escalation threshold
        channel_quotes: list[dict[str, Any]] = []
        auto_triggered: list[str] = []
        for ch in ED_STAGE2_CHANNELS:
            _, _, _, r_ch = self.channel_models[ch.channel_id]
            voi_val = float(
                r_ch.score_voi(
                    p1,
                    cost_fp=triage_contract.cost_fp,
                    cost_fn=triage_contract.cost_fn,
                )[0]
            )
            net_voi = voi_val - ch.per_patient_query_cost
            is_unobserved = ch.channel_id in unobserved_channel_ids
            worth_query = bool(is_unobserved and voi_val > ch.per_patient_query_cost)
            if worth_query:
                auto_triggered.append(ch.channel_id)
            channel_quotes.append(
                {
                    "channel_id": ch.channel_id,
                    "display_name": ch.display_name,
                    "mechanism_tag": ch.mechanism_tag,
                    "voi_expected_gain": round(voi_val, 4),
                    "query_cost": round(ch.per_patient_query_cost, 4),
                    "net_voi": round(net_voi, 4),
                    "already_in_chart": not is_unobserved or (ch.channel_id in unlocked_set),
                    "trigger_query": worth_query,
                    "unlocked": ch.channel_id in unlocked_set,
                }
            )

        bundle_voi = float(
            self.bundle_voi_router.score_voi(
                p1,
                cost_fp=triage_contract.cost_fp,
                cost_fn=triage_contract.cost_fn,
            )[0]
        )
        already_above_tau = bool(
            (s_cons >= tau_triage and bundle_voi < triage_contract.default_query_cost)
            or any(evals.get(k) == "missing" for k in ("trauma", "neuro", "d2b"))
            or (
                (enc.get("vitals") or {}).get("sbp") is not None
                and float(enc["vitals"]["sbp"]) < 85
            )
        )
        if already_above_tau:
            # High Risk != High VOI: act immediately in resus/cath/trauma bay without waiting on HIE
            auto_triggered = []
            for cq in channel_quotes:
                cq["trigger_query"] = False

        # Dual Conformal Safe-Gate classification (plain clinical copy on gate_label / gate_reason)
        has_missing_eval = any(evals.get(k) == "missing" for k in ("trauma", "neuro", "d2b"))
        delta_findings = swarm["synthesizer"].get("findings") or []
        delta_prio = swarm["synthesizer"].get("priority", "ROUTINE")

        if (
            len(sentinels) > 0
            or has_missing_eval
            or delta_prio == "CRITICAL"
            or s_cons >= tau_triage * 1.35
        ):
            gate_state = "SENTINEL_ESCALATION"
            gate_label = "Physician Escalation Review Required"
            gate_reason = "Active clinical sentinel, missing arrival evaluation, or high-acuity presentation — review routed actions below."
            allow_one_pinch = False
        elif (
            s_cons > self.tau_safe
            or p_miss > self.safe_miss_alpha
            or len(delta_findings) > 0
            or (ekg_chip["status"] == "pending" and "req_ekg" not in unlocked_set)
            or (enc.get("outside_hie_available") is False and "ch_hie_outside" not in unlocked_set)
            or len(auto_triggered) > 0
        ):
            gate_state = "AMBIGUOUS_HOLD_VOI"
            gate_label = "Hold Fast-Pass · Missing Check or Workup Sign-Off"
            reasons_hold: list[str] = []
            if ekg_chip["status"] == "pending" and "req_ekg" not in unlocked_set:
                reasons_hold.append("triage ECG still pending")
            if enc.get("outside_hie_available") is False and "ch_hie_outside" not in unlocked_set:
                reasons_hold.append("outside hospital HIE not yet pulled")
            if any(r["alert"] for r in recommendations):
                reasons_hold.append("standing workup / med sign-off needed")
            if not reasons_hold:
                reasons_hold.append("borderline presentation requires brief MD sign-off")
            gate_reason = "Fast-pass paused: " + "; ".join(reasons_hold) + "."
            allow_one_pinch = False
        else:
            gate_state = "CERTIFIED_SAFE_ROUTINE"
            gate_label = "Safe to Fast-Pass"
            gate_reason = (
                "No chart sentinels, no missing door evaluations, and routine pathway verified."
            )
            allow_one_pinch = True

        cfbh_certified = bool(p2 >= self.cfbh_alert_cutoff or p_neg_s2 <= 0.025)
        clinical_routing = _build_clinical_routing_summary(
            packet=packet,
            ekg_chip=ekg_chip,
            sentinels=sentinels,
            recommendations=recommendations,
            swarm=swarm,
            gate_state=gate_state,
            unlocked_set=unlocked_set,
            already_above_tau=already_above_tau,
        )

        return {
            **packet,
            "ekg_chip": ekg_chip,
            "sentinels": sentinels,
            "recommendations": recommendations,
            "swarm": swarm,
            "clinical_routing": clinical_routing,
            "safe_gate": {
                "gate_state": gate_state,
                "gate_label": gate_label,
                "gate_reason": gate_reason,
                "allow_one_pinch_routine": allow_one_pinch,
                "p1_calibrated": round(p1, 4),
                "p1_framing_blind": round(p1_fb, 4),
                "p2_calibrated": round(p2, 4),
                "conservative_stage1": round(s_cons, 4),
                "tau_bayes_triage": round(tau_triage, 4),
                "tau_space_capacity_adjusted": round(tau_space, 4),
                "tau_safe_routine": round(self.tau_safe, 4),
                "conformal_miss_pvalue": round(p_miss, 4),
                "conformal_neg_pvalue_s2": round(p_neg_s2, 4),
                "cal_for_wilson_upper": round(self.cal_for_wilson_upper, 4),
                "evidence_deficit_nats": round(ev_def, 4),
                "framing_divergence_nats": round(framing_div_nats, 4),
            },
            "voi_routing": {
                "bundle_voi": round(bundle_voi, 4),
                "default_query_cost": round(triage_contract.default_query_cost, 4),
                "auto_triggered_channels": auto_triggered,
                "triage_voi_zero_already_above_tau": already_above_tau,
                "cfbh_alert_certified": cfbh_certified,
                "channels": channel_quotes,
            },
        }

    def evaluate_seed_queue(
        self,
        capacity: dict[str, Any] | None = None,
        *,
        extra_packets: list[dict[str, Any]] | None = None,
        allow_live_llm: bool = False,
    ) -> list[dict[str, Any]]:
        all_packets = list(SEED_PACKETS) + list(extra_packets or [])
        return [
            self.evaluate_packet_safety(pkt, capacity, allow_live_llm=allow_live_llm)
            for pkt in all_packets
        ]

    def run_full_engine_verification_audit(
        self, *, n_seeds: int = 4, force: bool = False
    ) -> dict[str, Any]:
        """Execute Stage-A Two-Phase + Stress Pool, Stage-B VOI/CfBH/Safe-Gate, and Subgroup Audits."""
        if self._audit_cache is not None and not force:
            return self._audit_cache

        triage_contract = PITM_CONTRACTS["triage_status"]

        # -------------------------------------------------------------------
        # Stage A: Two-Phase Constrained Neyman + G_{m|S} vs SRS vs Full Oracle
        # -------------------------------------------------------------------
        auroc_deltas_neyman_vs_full: list[float] = []
        auprc_deltas_neyman_vs_full: list[float] = []
        neyman_aurocs: list[float] = []
        srs_aurocs: list[float] = []
        full_aurocs: list[float] = []
        neyman_ess_list: list[float] = []

        def _eval_selected_specs(
            sel_specs: list[Stage2ChannelSpec],
            coh_obj: Any,
            tr_idx: list[int],
            ca_idx: list[int],
            te_idx: list[int],
            y_tr_arr: np.ndarray,
            y_ca_arr: np.ndarray,
            y_te_arr: np.ndarray,
            seed_fit: int,
        ) -> tuple[float, float]:
            sel_cols = [col for sp in sel_specs for col in sp.columns]
            X_aug = (
                pd.concat([coh_obj.X_base, coh_obj.Z_pool[sel_cols]], axis=1)
                if sel_cols
                else coh_obj.X_base
            )
            pr_m, cl_m, ca_m = fit_calibrated_gbdt(
                X_aug.iloc[tr_idx],
                y_tr_arr,
                X_aug.iloc[ca_idx],
                y_ca_arr,
                semantic_types=BASE_SEMANTIC_TYPES,
                seed=seed_fit,
            )
            p_te = self._predict_calibrated(pr_m, cl_m, ca_m, X_aug.iloc[te_idx])
            return float(roc_auc_score(y_te_arr, p_te)), float(
                average_precision_score(y_te_arr, p_te)
            )

        for s_idx in range(n_seeds):
            sd = self.seed + s_idx * 17
            coh_s = generate_ed_calibration_cohort(n_encounters=6400, seed=sd)
            df_s = pd.concat(
                [
                    coh_s.X_base.reset_index(drop=True),
                    pd.DataFrame({"patient_id": coh_s.patient_ids, "y": coh_s.y}),
                ],
                axis=1,
            )
            sp_s = freeze_split(df_s, "y", group_col="patient_id", seed=sd)
            tr_s, ca_s, te_s = list(sp_s.train_idx), list(sp_s.cal_idx), list(sp_s.test_idx)
            y_tr_s, y_ca_s, y_te_s = coh_s.y[tr_s], coh_s.y[ca_s], coh_s.y[te_s]
            grp_tr_s = coh_s.patient_ids[tr_s]

            oof_p0 = compute_oof_phase1_baseline(
                coh_s.X_base.iloc[tr_s], y_tr_s, groups_train=grp_tr_s, seed=sd
            )
            pilot_ney = sample_two_phase_pilot(
                y_tr_s, oof_p0, pilot_fraction=0.15, design="neyman", seed=sd
            )
            pilot_srs = sample_two_phase_pilot(
                y_tr_s, oof_p0, pilot_fraction=0.15, design="srs", seed=sd
            )
            pilot_ful = sample_two_phase_pilot(
                y_tr_s, oof_p0, pilot_fraction=1.00, design="full", seed=sd
            )
            neyman_ess_list.append(pilot_ney.kish_ess)

            disc_ney = discover_sequential_conditional_gain(
                coh_s.X_base.iloc[tr_s],
                coh_s.Z_pool.iloc[tr_s],
                y_tr_s,
                coh_s.candidates,
                oof_p0,
                pilot_ney,
                coh_s.acquisition_budget,
                groups_train=grp_tr_s,
                method_name="neyman_seq_cmi",
                seed=sd,
            )
            disc_srs = discover_sequential_conditional_gain(
                coh_s.X_base.iloc[tr_s],
                coh_s.Z_pool.iloc[tr_s],
                y_tr_s,
                coh_s.candidates,
                oof_p0,
                pilot_srs,
                coh_s.acquisition_budget,
                groups_train=grp_tr_s,
                method_name="srs_seq_cmi",
                seed=sd,
            )
            disc_ful = discover_sequential_conditional_gain(
                coh_s.X_base.iloc[tr_s],
                coh_s.Z_pool.iloc[tr_s],
                y_tr_s,
                coh_s.candidates,
                oof_p0,
                pilot_ful,
                coh_s.acquisition_budget,
                groups_train=grp_tr_s,
                method_name="full_seq_cmi",
                seed=sd,
            )

            auc_n, ap_n = _eval_selected_specs(
                disc_ney.selected_channels, coh_s, tr_s, ca_s, te_s, y_tr_s, y_ca_s, y_te_s, sd + 1
            )
            auc_s, _ = _eval_selected_specs(
                disc_srs.selected_channels, coh_s, tr_s, ca_s, te_s, y_tr_s, y_ca_s, y_te_s, sd + 2
            )
            auc_f, ap_f = _eval_selected_specs(
                disc_ful.selected_channels, coh_s, tr_s, ca_s, te_s, y_tr_s, y_ca_s, y_te_s, sd + 3
            )

            neyman_aurocs.append(auc_n)
            srs_aurocs.append(auc_s)
            full_aurocs.append(auc_f)
            auroc_deltas_neyman_vs_full.append(auc_n - auc_f)
            auprc_deltas_neyman_vs_full.append(ap_n - ap_f)

        tost_auroc = nadeau_bengio_tost(
            auroc_deltas_neyman_vs_full,
            margin=0.015,
            n_test=len(self.split.test_idx),
            n_train=len(self.split.train_idx),
        )
        tost_auprc = nadeau_bengio_tost(
            auprc_deltas_neyman_vs_full,
            margin=0.025,
            n_test=len(self.split.test_idx),
            n_train=len(self.split.train_idx),
        )

        # Stress-Pool Null / Twin / Baseline-Echo Audit (12 channels: 6 genuine + 2 null + 2 twin + 2 echo)
        stress_coh = generate_ed_calibration_cohort(
            n_encounters=6400, include_stress_channels=True, seed=self.seed
        )
        df_st = pd.concat(
            [
                stress_coh.X_base.reset_index(drop=True),
                pd.DataFrame({"patient_id": stress_coh.patient_ids, "y": stress_coh.y}),
            ],
            axis=1,
        )
        sp_st = freeze_split(df_st, "y", group_col="patient_id", seed=self.seed)
        tr_st = list(sp_st.train_idx)
        oof_st = compute_oof_phase1_baseline(
            stress_coh.X_base.iloc[tr_st],
            stress_coh.y[tr_st],
            groups_train=stress_coh.patient_ids[tr_st],
            seed=self.seed,
        )
        pilot_st = sample_two_phase_pilot(
            stress_coh.y[tr_st], oof_st, pilot_fraction=0.20, design="neyman", seed=self.seed
        )
        disc_seq_stress = discover_sequential_conditional_gain(
            stress_coh.X_base.iloc[tr_st],
            stress_coh.Z_pool.iloc[tr_st],
            stress_coh.y[tr_st],
            stress_coh.candidates,
            oof_st,
            pilot_st,
            max_budget=4.20,
            groups_train=stress_coh.patient_ids[tr_st],
            sequential=True,
            method_name="neyman_seq_cmi_stress",
            seed=self.seed,
        )
        disc_marg_stress = discover_sequential_conditional_gain(
            stress_coh.X_base.iloc[tr_st],
            stress_coh.Z_pool.iloc[tr_st],
            stress_coh.y[tr_st],
            stress_coh.candidates,
            oof_st,
            pilot_st,
            max_budget=4.20,
            groups_train=stress_coh.patient_ids[tr_st],
            sequential=False,
            method_name="neyman_marginal_stress",
            seed=self.seed,
        )

        seq_tags = [c.mechanism_tag for c in disc_seq_stress.selected_channels]
        marg_tags = [c.mechanism_tag for c in disc_marg_stress.selected_channels]

        # -------------------------------------------------------------------
        # Stage B: Streaming VOI Routing, Tie-Smoothed CfBH, & Safe-Gate Audit
        # -------------------------------------------------------------------
        voi_cal = self.bundle_voi_router.score_voi(
            self.p1_cal, cost_fp=triage_contract.cost_fp, cost_fn=triage_contract.cost_fn
        )
        voi_test = self.bundle_voi_router.score_voi(
            self.p1_test, cost_fp=triage_contract.cost_fp, cost_fn=triage_contract.cost_fn
        )

        # Autonomous Bayes stopping: VOI(x) > c_query vs matched-rate Top-Risk heuristic
        q_cost = triage_contract.default_query_cost
        mask_voi_auto = voi_test > q_cost
        matched_q = float(np.clip(np.mean(voi_cal > q_cost), 0.05, 0.95))
        top_risk_cutoff = float(np.quantile(self.p1_cal, 1.0 - matched_q))
        mask_top_risk = self.p1_test >= top_risk_cutoff

        flip_voi = decompose_routing_flips(
            "Martingale_VOI_Autonomous",
            self.y_test,
            self.p1_test,
            self.p2_test,
            mask_voi_auto,
            triage_contract,
            q_cost,
        )
        flip_toprisk = decompose_routing_flips(
            "Top_Risk_Heuristic_MatchedRate",
            self.y_test,
            self.p1_test,
            self.p2_test,
            mask_top_risk,
            triage_contract,
            q_cost,
        )

        p_routed_test = np.where(mask_voi_auto, self.p2_test, self.p1_test)
        p_routed_cal = np.where(voi_cal > q_cost, self.p2_cal, self.p1_cal)
        cfbh_res, _, _ = conformal_fdr_selection(
            p_routed_cal, self.y_cal, p_routed_test, self.y_test, target_fdr=0.35, seed=self.seed
        )
        mondrian_res = mondrian_singleton_positive_selection(
            p_routed_cal, self.y_cal, p_routed_test, self.y_test, alpha=0.10
        )

        # Dual Conformal Safe-Gate evaluation on D_test
        s_cons_test = np.maximum(self.p1_test, self.p1_fb_test)
        p_miss_test = self.positive_conformal_miss_pvalue(s_cons_test)
        safe_mask_test = (s_cons_test <= self.tau_safe) & (p_miss_test <= self.safe_miss_alpha)
        n_safe_test = int(np.sum(safe_mask_test))
        fn_safe_test = int(np.sum(self.y_test[safe_mask_test] == 1)) if n_safe_test > 0 else 0
        test_for_lo, test_for_hi = wilson_interval(fn_safe_test, max(1, n_safe_test), alpha=0.05)

        # -------------------------------------------------------------------
        # Stage C: Subgroup & Framing-Blind Calibration Audit on D_test
        # -------------------------------------------------------------------
        te_idx = list(self.split.test_idx)
        X_te_df = self.cohort.X_base.iloc[te_idx].reset_index(drop=True)
        Z_te_df = self.cohort.Z_pool.iloc[te_idx].reset_index(drop=True)
        subgroups = {
            "overall_test": np.ones(len(X_te_df), dtype=bool),
            "female": (X_te_df["sex"] == "F").to_numpy(),
            "male": (X_te_df["sex"] == "M").to_numpy(),
            "older_adults_65p": (X_te_df["age_years"] >= 65).to_numpy(),
            "walk_in_arrivals": (X_te_df["arrival_mode"] == "walk-in").to_numpy(),
            "minimizing_framing_present": (X_te_df["minimizing_framing_flag"] == 1).to_numpy(),
            "missing_prior_labs": Z_te_df["recent_hgb_g_dl"].isna().to_numpy(),
        }
        subgroup_rows: dict[str, dict[str, float | int]] = {}
        for sg_name, sg_mask in subgroups.items():
            if int(np.sum(sg_mask)) >= 20:
                diag_sg = compute_calibration_summary(self.y_test[sg_mask], self.p2_test[sg_mask])
                subgroup_rows[sg_name] = {
                    "n": diag_sg.n_eval,
                    "prevalence": round(diag_sg.prevalence, 4),
                    "auroc": round(diag_sg.auroc, 4),
                    "ece": round(diag_sg.ece, 4),
                    "brier": round(diag_sg.brier, 4),
                    "cal_slope": round(diag_sg.cal_slope, 4),
                }

        audit_payload: dict[str, Any] = {
            "engine_version": "edimbess-0.1.0",
            "split_hash": self.split.split_hash,
            "cohort_summary": {
                "n_encounters": self.n_encounters,
                "n_train": len(self.split.train_idx),
                "n_cal": len(self.split.cal_idx),
                "n_val": len(self.split.val_idx),
                "n_test": len(self.split.test_idx),
                "natural_prevalence": round(self.natural_prevalence, 4),
                "stage1_test_auroc": round(self.test_diag_s1.auroc, 4),
                "stage2_test_auroc": round(self.test_diag_s2.auroc, 4),
                "stage2_test_ece": round(self.test_diag_s2.ece, 4),
                "stage2_test_cal_slope": round(self.test_diag_s2.cal_slope, 4),
            },
            "stage_a_two_phase_discovery": {
                "n_seeds": n_seeds,
                "pilot_fraction": 0.15,
                "mean_neyman_kish_ess": round(float(np.mean(neyman_ess_list)), 2),
                "mean_auroc_neyman_15pct": round(float(np.mean(neyman_aurocs)), 4),
                "mean_auroc_srs_15pct": round(float(np.mean(srs_aurocs)), 4),
                "mean_auroc_full_100pct": round(float(np.mean(full_aurocs)), 4),
                "pilot_channel_savings_pct": 85.0,
                "tost_auroc_vs_full": tost_auroc.to_dict(),
                "tost_auprc_vs_full": tost_auprc.to_dict(),
                "stress_pool_audit": {
                    "sequential_selected_ids": disc_seq_stress.selected_ids,
                    "marginal_selected_ids": disc_marg_stress.selected_ids,
                    "sequential_null_selected": int(sum(t == "pure_noise_null" for t in seq_tags)),
                    "sequential_twin_selected": int(sum(t == "stage2_twin" for t in seq_tags)),
                    "sequential_echo_selected": int(sum(t == "baseline_echo" for t in seq_tags)),
                    "marginal_twin_or_echo_selected": int(
                        sum(t in ("stage2_twin", "baseline_echo") for t in marg_tags)
                    ),
                },
            },
            "stage_b_streaming_voi_and_conformal": {
                "voi_vs_top_risk": {
                    "martingale_voi": flip_voi.to_dict(),
                    "top_risk_heuristic": flip_toprisk.to_dict(),
                    "nb_advantage_per_1k_over_top_risk": round(
                        flip_voi.net_nb_delta_vs_stage1_per_1k
                        - flip_toprisk.net_nb_delta_vs_stage1_per_1k,
                        3,
                    ),
                },
                "conformal_fdr_vs_mondrian": {
                    "cfbh_tie_smoothed": cfbh_res.to_dict(),
                    "mondrian_singleton_pos": mondrian_res.to_dict(),
                },
                "dual_conformal_safe_gate": {
                    "tau_safe_routine": round(self.tau_safe, 4),
                    "target_for_alpha": round(self.safe_for_alpha, 4),
                    "cal_for_wilson_95_upper": round(self.cal_for_wilson_upper, 4),
                    "test_certified_routine_rate": round(
                        float(n_safe_test) / float(max(1, len(self.y_test))), 4
                    ),
                    "test_realized_for": round(float(fn_safe_test) / float(max(1, n_safe_test)), 4),
                    "test_for_wilson_95": [round(test_for_lo, 4), round(test_for_hi, 4)],
                },
            },
            "stage_c_subgroup_calibration": subgroup_rows,
        }
        receipt_bytes = json.dumps(audit_payload, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
        audit_payload["receipt_sha256"] = hashlib.sha256(receipt_bytes).hexdigest()
        self._audit_cache = audit_payload
        return audit_payload


_ENGINE_LOCK = threading.Lock()
_DEFAULT_ENGINE: EDStatisticalSafetyEngine | None = None


def get_default_safety_engine() -> EDStatisticalSafetyEngine:
    """Thread-safe singleton initializer for the calibrated ED Statistical Safety Engine."""
    global _DEFAULT_ENGINE
    if _DEFAULT_ENGINE is None:
        with _ENGINE_LOCK:
            if _DEFAULT_ENGINE is None:
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore")
                    _DEFAULT_ENGINE = EDStatisticalSafetyEngine(n_encounters=5600, seed=42)
    return _DEFAULT_ENGINE

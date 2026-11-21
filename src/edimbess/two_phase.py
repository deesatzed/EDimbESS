"""Constrained Neyman two-phase pilot, Kish ESS, untruncated G_{m|S}, and V_I + V_II variance."""

from __future__ import annotations

import warnings
from dataclasses import asdict, dataclass, field
from typing import Any

import lightgbm as lgb
import numpy as np
import pandas as pd
from scipy.special import expit, logit
from scipy.stats import norm
from sklearn.linear_model import LogisticRegression
from sklearn.model_selection import StratifiedGroupKFold, StratifiedKFold

from edimbess.calibration_cohort import (
    BASE_SEMANTIC_TYPES,
    PlattLogitCalibrator,
    Stage2ChannelSpec,
    TabularPreprocessor,
)
from edimbess.contracts import (
    SemanticType,
    holm_bonferroni_adjust,
    kish_effective_sample_size,
)

_PROB_EPS = 1e-12


@dataclass(frozen=True, slots=True)
class TwoPhasePilot:
    """Known-probability Phase-2 stratified sample with Horvitz-Thompson weights and Kish ESS."""

    design: str
    sampled_indices: np.ndarray
    stratum_ids_sampled: np.ndarray
    inclusion_probs: np.ndarray
    ht_weights: np.ndarray
    stratum_pop_counts: dict[int, int]
    stratum_sample_counts: dict[int, int]
    n_phase1_valid: int
    n_pilot: int
    pilot_fraction: float
    kish_ess: float
    design_effect: float


@dataclass(frozen=True, slots=True)
class SequentialDiscoveryStep:
    """Audit record for one forward selection step of sequential conditional discovery."""

    step: int
    selected_channel_id: str | None
    gain_nats: float
    se_total: float
    se_phase1: float
    se_phase2: float
    z_stat: float
    p_raw: float
    p_holm: float
    lcb_95: float
    cost: float
    stopped_by_lcb: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class DiscoveryResult:
    """Output of two-phase sequential conditional channel discovery."""

    method: str
    pilot_design: str
    pilot_fraction: float
    n_pilot: int
    kish_ess: float
    pilot_channel_accesses: int
    selected_channels: list[Stage2ChannelSpec]
    selected_ids: list[str]
    total_cost_spent: float
    step_trace: list[SequentialDiscoveryStep] = field(default_factory=list)
    initial_scores: dict[str, float] = field(default_factory=dict)
    initial_ses: dict[str, float] = field(default_factory=dict)


def make_leak_free_cv_splits(
    y: np.ndarray,
    *,
    n_folds: int = 4,
    groups: np.ndarray | None = None,
    seed: int = 42,
) -> list[tuple[np.ndarray, np.ndarray]]:
    """Construct patient-group disjoint or stratified K-fold splits without leakage."""
    yv = np.asarray(y, dtype=int).ravel()
    n = yv.size
    n_pos = int(yv.sum())
    k = min(n_folds, max(2, n_pos), max(2, n - n_pos))

    if groups is not None:
        g_arr = np.asarray(groups)
        if np.unique(g_arr).size >= k * 2:
            sgkf = StratifiedGroupKFold(n_splits=k, shuffle=True, random_state=seed)
            cand = list(sgkf.split(np.zeros(n), yv, g_arr))
            if all(int(yv[tr].sum()) >= 1 and int((1 - yv[tr]).sum()) >= 1 for tr, _ in cand):
                return cand

    skf = StratifiedKFold(n_splits=k, shuffle=True, random_state=seed)
    return list(skf.split(np.zeros(n), yv))


def compute_oof_phase1_baseline(
    X_base_train: pd.DataFrame,
    y_train: np.ndarray,
    groups_train: np.ndarray | None = None,
    *,
    semantic_types: dict[str, SemanticType] | None = None,
    seed: int = 42,
) -> np.ndarray:
    """Compute leak-free Phase-1 out-of-fold calibrated baseline probabilities on D_train."""
    y = np.asarray(y_train, dtype=int).ravel()
    n = y.size
    pi = float(np.clip(y.mean(), 1e-5, 1.0 - 1e-5))
    oof_p = np.full(n, pi, dtype=float)
    sem = semantic_types if semantic_types is not None else BASE_SEMANTIC_TYPES
    splits = make_leak_free_cv_splits(y, n_folds=5, groups=groups_train, seed=seed)

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        for f_i, (tr_i, va_i) in enumerate(splits):
            if np.unique(y[tr_i]).size < 2:
                continue
            prep = TabularPreprocessor(semantic_types=sem).fit(X_base_train.iloc[tr_i])
            X_tr = prep.transform(X_base_train.iloc[tr_i])
            X_va = prep.transform(X_base_train.iloc[va_i])
            clf = lgb.LGBMClassifier(
                n_estimators=85,
                learning_rate=0.06,
                num_leaves=23,
                min_child_samples=max(12, min(35, int(tr_i.size // 40))),
                reg_alpha=0.5,
                reg_lambda=3.0,
                max_delta_step=1.0,
                random_state=seed + f_i,
                n_jobs=2,
                verbose=-1,
            )
            clf.fit(X_tr, y[tr_i])
            tr_raw = np.asarray(clf.predict_proba(X_tr)[:, 1], dtype=float)
            va_raw = np.asarray(clf.predict_proba(X_va)[:, 1], dtype=float)
            cal = PlattLogitCalibrator().fit(tr_raw, y[tr_i])
            oof_p[va_i] = np.clip(cal.predict_proba(va_raw), _PROB_EPS, 1.0 - _PROB_EPS)

    return oof_p


def waterfill_constrained_neyman(
    pop_counts: np.ndarray,
    dispersions: np.ndarray,
    target_total: int,
    min_per_stratum: int = 2,
    min_inclusion_prob: float = 0.05,
) -> np.ndarray:
    """Solve exact constrained Neyman allocation via iterative boundary clamping.

    Minimizes sum_h N_h^2 (1 - n_h / N_h) sigma_h^2 / n_h subject to sum_h n_h = target_total
    and min(min_per_stratum, N_h) <= n_h <= N_h, with a bounded Horvitz-Thompson weight floor
    (min_inclusion_prob) when budget permits so Kish ESS never collapses on small cohorts.
    """
    h_count = int(pop_counts.size)
    total_pop = int(np.sum(pop_counts))
    if target_total >= total_pop:
        return pop_counts.astype(int).copy()

    lo_base = np.minimum(pop_counts, min_per_stratum).astype(int)
    lo_prop = np.minimum(
        pop_counts, np.maximum(lo_base, np.ceil(min_inclusion_prob * pop_counts).astype(int))
    )
    lo = lo_prop if int(np.sum(lo_prop)) <= int(0.65 * target_total) else lo_base
    if int(np.sum(lo)) >= target_total:
        lo1 = np.minimum(pop_counts, 1).astype(int)
        if int(np.sum(lo1)) >= target_total:
            alloc = np.zeros(h_count, dtype=int)
            order = np.argsort(-(pop_counts * dispersions))
            for idx in order[:target_total]:
                alloc[idx] = 1
            return alloc
        lo = lo1

    weights = pop_counts.astype(float) * np.maximum(1e-6, dispersions.astype(float))
    fixed = np.zeros(h_count, dtype=bool)
    alloc_f = np.zeros(h_count, dtype=float)

    for _ in range(h_count * 2 + 4):
        free = ~fixed
        if not np.any(free):
            break
        rem_budget = float(target_total) - float(np.sum(alloc_f[fixed]))
        w_free_sum = float(np.sum(weights[free]))
        if w_free_sum <= 0.0 or rem_budget <= 0.0:
            alloc_f[free] = lo[free].astype(float)
            break
        prop = rem_budget * (weights[free] / w_free_sum)
        over = prop > pop_counts[free]
        under = prop < lo[free]
        if not np.any(over) and not np.any(under):
            alloc_f[free] = prop
            break
        free_idx = np.flatnonzero(free)
        if np.any(over):
            ov_idx = free_idx[over]
            alloc_f[ov_idx] = pop_counts[ov_idx].astype(float)
            fixed[ov_idx] = True
        if np.any(under):
            un_idx = free_idx[under]
            alloc_f[un_idx] = lo[un_idx].astype(float)
            fixed[un_idx] = True

    alloc = np.clip(np.floor(alloc_f).astype(int), lo, pop_counts)
    diff = int(target_total - int(np.sum(alloc)))
    if diff > 0:
        frac = alloc_f - np.floor(alloc_f)
        order = np.argsort(-(frac + 1e-6 * weights / max(1.0, float(np.max(weights)))))
        for idx in order:
            if diff <= 0:
                break
            if alloc[idx] < pop_counts[idx]:
                alloc[idx] += 1
                diff -= 1
        while diff > 0:
            slack = np.flatnonzero(alloc < pop_counts)
            if slack.size == 0:
                break
            best = slack[int(np.argmax(weights[slack] / np.maximum(1, alloc[slack])))]
            alloc[best] += 1
            diff -= 1
    elif diff < 0:
        while diff < 0:
            reducible = np.flatnonzero(alloc > lo)
            if reducible.size == 0:
                break
            worst = reducible[int(np.argmin(weights[reducible] / np.maximum(1, alloc[reducible])))]
            alloc[worst] -= 1
            diff += 1

    return alloc


def sample_two_phase_pilot(
    y_train: np.ndarray,
    oof_base_prob: np.ndarray,
    *,
    pilot_fraction: float = 0.15,
    design: str = "neyman",
    n_score_bins: int = 8,
    seed: int = 42,
) -> TwoPhasePilot:
    """Sample a Phase-2 pilot ('neyman', 'srs', or 'full') with exact HT weights & Kish ESS."""
    y = np.asarray(y_train, dtype=int).ravel()
    p0 = np.clip(np.asarray(oof_base_prob, dtype=float).ravel(), _PROB_EPS, 1.0 - _PROB_EPS)
    n_valid = int(y.size)
    if n_valid == 0:
        raise ValueError("Phase-1 training cohort cannot be empty.")

    if design == "full" or pilot_fraction >= 0.999:
        ones = np.ones(n_valid, dtype=float)
        return TwoPhasePilot(
            design="full",
            sampled_indices=np.arange(n_valid, dtype=int),
            stratum_ids_sampled=np.zeros(n_valid, dtype=int),
            inclusion_probs=ones.copy(),
            ht_weights=ones,
            stratum_pop_counts={0: n_valid},
            stratum_sample_counts={0: n_valid},
            n_phase1_valid=n_valid,
            n_pilot=n_valid,
            pilot_fraction=1.0,
            kish_ess=float(n_valid),
            design_effect=1.0,
        )

    rng = np.random.default_rng(seed)
    n_pilot = int(np.clip(round(float(pilot_fraction) * n_valid), min(32, n_valid), n_valid))

    if design == "srs":
        chosen = np.sort(rng.choice(n_valid, size=n_pilot, replace=False))
        pi_val = float(n_pilot) / float(n_valid)
        w_arr = np.full(n_pilot, 1.0 / pi_val, dtype=float)
        ess, deff = kish_effective_sample_size(w_arr)
        return TwoPhasePilot(
            design="srs",
            sampled_indices=chosen,
            stratum_ids_sampled=np.zeros(n_pilot, dtype=int),
            inclusion_probs=np.full(n_pilot, pi_val, dtype=float),
            ht_weights=w_arr,
            stratum_pop_counts={0: n_valid},
            stratum_sample_counts={0: n_pilot},
            n_phase1_valid=n_valid,
            n_pilot=n_pilot,
            pilot_fraction=pi_val,
            kish_ess=ess,
            design_effect=deff,
        )

    if design != "neyman":
        raise ValueError(f"Unsupported pilot design '{design}'.")

    edges = np.unique(np.quantile(logit(p0), np.linspace(0.0, 1.0, n_score_bins + 1)))
    if edges.size <= 2:
        bins = np.zeros(n_valid, dtype=int)
        n_bins_eff = 1
    else:
        bins = np.clip(np.digitize(logit(p0), edges[1:-1], right=False), 0, edges.size - 2)
        n_bins_eff = int(edges.size - 1)

    raw_h = y * n_bins_eff + bins
    uniq_h = np.unique(raw_h)
    pop_counts = np.empty(uniq_h.size, dtype=int)
    dispersions = np.empty(uniq_h.size, dtype=float)
    members_list: list[np.ndarray] = []

    for idx_u, h_code in enumerate(uniq_h):
        loc_m = np.flatnonzero(raw_h == h_code)
        members_list.append(loc_m)
        pop_counts[idx_u] = int(loc_m.size)
        yv_h = float(int(h_code) // n_bins_eff)
        eta_bar = float(np.mean(p0[loc_m]))
        dispersions[idx_u] = abs(yv_h - eta_bar) + 0.25 * eta_bar * (1.0 - eta_bar) + 1e-3

    alloc = waterfill_constrained_neyman(pop_counts, dispersions, target_total=n_pilot)

    sampled_parts: list[np.ndarray] = []
    sampled_h: list[np.ndarray] = []
    pop_dict: dict[int, int] = {}
    smp_dict: dict[int, int] = {}

    for idx_u, n_take in enumerate(alloc):
        if n_take <= 0:
            continue
        members = members_list[idx_u]
        n_pop_h = int(pop_counts[idx_u])
        chosen_h = (
            members.copy()
            if n_take >= n_pop_h
            else np.sort(rng.choice(members, size=int(n_take), replace=False))
        )
        sampled_parts.append(chosen_h)
        sampled_h.append(np.full(chosen_h.size, idx_u, dtype=int))
        pop_dict[idx_u] = n_pop_h
        smp_dict[idx_u] = int(chosen_h.size)

    all_idx = np.concatenate(sampled_parts)
    all_h = np.concatenate(sampled_h)
    order = np.argsort(all_idx)
    all_idx = all_idx[order]
    all_h = all_h[order]

    pi_arr = np.array([smp_dict[int(h)] / float(pop_dict[int(h)]) for h in all_h], dtype=float)
    w_arr = 1.0 / pi_arr
    ess, deff = kish_effective_sample_size(w_arr)

    return TwoPhasePilot(
        design="neyman",
        sampled_indices=all_idx,
        stratum_ids_sampled=all_h,
        inclusion_probs=pi_arr,
        ht_weights=w_arr,
        stratum_pop_counts=pop_dict,
        stratum_sample_counts=smp_dict,
        n_phase1_valid=n_valid,
        n_pilot=int(all_idx.size),
        pilot_fraction=float(all_idx.size) / float(n_valid),
        kish_ess=ess,
        design_effect=deff,
    )


def two_phase_variance_decomposition(
    psi_diff: np.ndarray,
    ht_weights: np.ndarray,
    stratum_ids: np.ndarray,
    stratum_pop_counts: dict[int, int],
    stratum_sample_counts: dict[int, int],
    groups: np.ndarray | None = None,
) -> tuple[float, float, float, float]:
    """Compute untruncated HT mean and exact two-phase SE (V_I + V_II).

    Reduces identically to s^2 / n_pilot under unclustered SRS (H=1, w_i = N / n_pilot).
    """
    psi = np.asarray(psi_diff, dtype=float).ravel()
    w = np.asarray(ht_weights, dtype=float).ravel()
    h_arr = np.asarray(stratum_ids, dtype=int).ravel()
    n_obs = int(psi.size)
    if n_obs < 3:
        return 0.0, 1.0, 1.0, 0.0

    w_tot = max(1.0, float(np.sum(w)))
    g_hat = float(np.sum(w * psi) / w_tot)
    resid = psi - g_hat

    dof_obs = float(n_obs) / max(1.0, float(n_obs - 1))
    v1_iid = dof_obs * float(np.sum(w * (resid**2)))
    if groups is not None and groups.size == n_obs:
        _, inv = np.unique(groups, return_inverse=True)
        cl_ht_sum = np.bincount(inv, weights=w * resid)
        n_cl = int(cl_ht_sum.size)
        dof_cl = float(n_cl) / max(1.0, float(n_cl - 1))
        cl_num = dof_cl * float(np.sum(cl_ht_sum**2) - np.sum(w * (w - 1.0) * (resid**2)))
        v_phase1 = max(v1_iid, cl_num) / (w_tot**2)
    else:
        v_phase1 = v1_iid / (w_tot**2)

    v_phase2_num = 0.0
    for h_id in np.unique(h_arr):
        mask_h = h_arr == h_id
        m_eval = int(np.sum(mask_h))
        if m_eval < 2:
            continue
        n_pop_h = int(stratum_pop_counts.get(int(h_id), m_eval))
        n_smp_h = int(stratum_sample_counts.get(int(h_id), m_eval))
        eval_frac = float(m_eval) / max(1.0, float(n_smp_h))
        n_pop_eff = max(float(m_eval), float(n_pop_h) * eval_frac)
        f_h = min(1.0, float(m_eval) / n_pop_eff)
        if f_h >= 1.0 - 1e-9:
            continue
        s2_h = float(np.var(resid[mask_h], ddof=1))
        v_phase2_num += (n_pop_eff**2) * (1.0 - f_h) * (s2_h / float(m_eval))

    v_phase2 = v_phase2_num / (w_tot**2)
    eps_var = (0.015 / np.sqrt(max(10.0, float(n_obs)))) ** 2
    v_tot = max(eps_var, v_phase1 + v_phase2)
    return (
        g_hat,
        float(np.sqrt(v_tot)),
        float(np.sqrt(max(0.0, v_phase1))),
        float(np.sqrt(max(0.0, v_phase2))),
    )


def _bernoulli_loglik(y: np.ndarray, prob: np.ndarray) -> np.ndarray:
    p = np.clip(prob, _PROB_EPS, 1.0 - _PROB_EPS)
    return y * np.log(p) + (1.0 - y) * np.log(1.0 - p)


def _prepare_fold_matrices(
    df: pd.DataFrame,
    y: np.ndarray,
    cv_splits: list[tuple[np.ndarray, np.ndarray]],
    semantic_types: dict[str, SemanticType] | None = None,
) -> list[tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]]:
    out: list[tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]] = []
    for tr_i, va_i in cv_splits:
        if np.unique(y[tr_i]).size < 2:
            continue
        prep = TabularPreprocessor(semantic_types=semantic_types)
        X_tr = prep.fit_transform(df.iloc[tr_i])
        X_va = prep.transform(df.iloc[va_i])
        out.append((tr_i, va_i, X_tr, X_va))
    return out


def _fit_oof_offset_gbdt_mats(
    fold_mats: list[tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]],
    y: np.ndarray,
    sample_weight: np.ndarray,
    init_logit: np.ndarray,
    seed: int,
) -> tuple[np.ndarray, np.ndarray]:
    """Fit capacity-matched GBDT from FIXED Phase-1 offset init_logit (never chained across steps)."""
    n = int(y.size)
    oof_p = expit(init_logit).copy()
    valid_mask = np.zeros(n, dtype=bool)

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        for f_idx, (tr_i, va_i, X_tr, X_va) in enumerate(fold_mats):
            w_tr = sample_weight[tr_i]
            w_tr_norm = w_tr * (float(tr_i.size) / max(1e-9, float(np.sum(w_tr))))
            min_child = max(8, min(25, int(tr_i.size // 25)))
            clf = lgb.LGBMClassifier(
                n_estimators=36,
                learning_rate=0.08,
                num_leaves=15,
                max_depth=4,
                min_child_samples=min_child,
                max_bin=127,
                reg_alpha=1.0,
                reg_lambda=5.0,
                max_delta_step=1.0,
                random_state=seed + f_idx,
                n_jobs=2,
                verbose=-1,
            )
            clf.fit(X_tr, y[tr_i], sample_weight=w_tr_norm, init_score=init_logit[tr_i])
            tr_margin = init_logit[tr_i] + np.asarray(
                clf.predict(X_tr, raw_score=True), dtype=float
            )
            va_margin = init_logit[va_i] + np.asarray(
                clf.predict(X_va, raw_score=True), dtype=float
            )

            cal = LogisticRegression(C=2.0, solver="lbfgs", max_iter=200)
            cal.fit(tr_margin.reshape(-1, 1), y[tr_i], sample_weight=w_tr_norm)
            oof_p[va_i] = np.clip(
                cal.predict_proba(va_margin.reshape(-1, 1))[:, 1], _PROB_EPS, 1.0 - _PROB_EPS
            )
            valid_mask[va_i] = True

    return oof_p, valid_mask


def discover_sequential_conditional_gain(
    X_base_train: pd.DataFrame,
    Z_pool_train: pd.DataFrame,
    y_train: np.ndarray,
    candidates: list[Stage2ChannelSpec],
    oof_base_prob: np.ndarray,
    pilot: TwoPhasePilot,
    max_budget: float,
    *,
    groups_train: np.ndarray | None = None,
    sequential: bool = True,
    alpha_select: float = 0.05,
    method_name: str = "neyman_seq_cmi",
    seed: int = 42,
) -> DiscoveryResult:
    """Forward stepwise two-phase untruncated conditional gain discovery + Holm LCB stopping."""
    y_all = np.asarray(y_train, dtype=int).ravel()
    idx = pilot.sampled_indices
    y_p = y_all[idx]
    w_p = pilot.ht_weights
    h_p = pilot.stratum_ids_sampled
    grp_p = groups_train[idx] if groups_train is not None else None

    cv_splits = make_leak_free_cv_splits(y_p, n_folds=4, groups=grp_p, seed=seed)
    curr_df = X_base_train.iloc[idx].reset_index(drop=True)
    Z_pilot_df = Z_pool_train.iloc[idx].reset_index(drop=True)
    init_logit_0 = logit(np.clip(oof_base_prob[idx], _PROB_EPS, 1.0 - _PROB_EPS))

    curr_mats = _prepare_fold_matrices(curr_df, y_p, cv_splits, semantic_types=BASE_SEMANTIC_TYPES)
    cand_mats = {
        c.channel_id: _prepare_fold_matrices(Z_pilot_df[list(c.columns)], y_p, cv_splits)
        for c in candidates
    }

    p_curr, valid_curr = _fit_oof_offset_gbdt_mats(curr_mats, y_p, w_p, init_logit_0, seed=seed)
    ll_curr = _bernoulli_loglik(y_p, p_curr)

    remaining = list(candidates)
    selected: list[Stage2ChannelSpec] = []
    spent = 0.0
    step_trace: list[SequentialDiscoveryStep] = []
    init_scores: dict[str, float] = {}
    init_ses: dict[str, float] = {}
    step1_cache: dict[str, tuple[float, float, float, float, float, np.ndarray, np.ndarray]] = {}

    step_num = 0
    while remaining and spent < max_budget - 1e-9:
        step_num += 1
        affordable = [c for c in remaining if spent + c.acquisition_cost <= max_budget + 1e-9]
        if not affordable:
            break

        cand_eval: list[
            tuple[Stage2ChannelSpec, float, float, float, float, float, np.ndarray, np.ndarray]
        ] = []
        for cand in affordable:
            if step_num > 1 and not sequential and cand.channel_id in step1_cache:
                g_m, se_t, se_1, se_2, p_raw, p_m, val_m = step1_cache[cand.channel_id]
                cand_eval.append((cand, g_m, se_t, se_1, se_2, p_raw, p_m, val_m))
                continue

            c_folds = cand_mats[cand.channel_id]
            aug_mats = [
                (tr_i, va_i, np.hstack([X_tr, Zc_tr]), np.hstack([X_va, Zc_va]))
                for (tr_i, va_i, X_tr, X_va), (_, _, Zc_tr, Zc_va) in zip(
                    curr_mats, c_folds, strict=True
                )
            ]
            # Strictly anchored to fixed Phase-1 offset init_logit_0 (never chained!)
            p_m, val_m = _fit_oof_offset_gbdt_mats(aug_mats, y_p, w_p, init_logit_0, seed=seed)
            ev = valid_curr & val_m
            if int(np.sum(ev)) < 10:
                g_m, se_t, se_1, se_2, p_raw = 0.0, 1.0, 1.0, 0.0, 1.0
            else:
                psi_m = (_bernoulli_loglik(y_p, p_m) - ll_curr)[ev]
                g_m, se_t, se_1, se_2 = two_phase_variance_decomposition(
                    psi_m,
                    w_p[ev],
                    h_p[ev],
                    pilot.stratum_pop_counts,
                    pilot.stratum_sample_counts,
                    groups=grp_p[ev] if grp_p is not None else None,
                )
                p_raw = float(norm.sf(g_m / max(1e-12, se_t)))

            if step_num == 1:
                init_scores[cand.channel_id] = g_m
                init_ses[cand.channel_id] = se_t
                step1_cache[cand.channel_id] = (g_m, se_t, se_1, se_2, p_raw, p_m, val_m)

            cand_eval.append((cand, g_m, se_t, se_1, se_2, p_raw, p_m, val_m))

        raw_pvals = [row[5] for row in cand_eval]
        holm_pvals = holm_bonferroni_adjust(raw_pvals)
        passing = [
            i
            for i, (hp, row) in enumerate(zip(holm_pvals, cand_eval, strict=True))
            if hp < alpha_select and row[1] > 0.0
        ]

        if not passing:
            best_i = int(
                np.argmax([row[1] / max(1e-6, row[0].acquisition_cost) for row in cand_eval])
            )
            _, b_g, b_se, b_s1, b_s2, b_pr, _, _ = cand_eval[best_i]
            step_trace.append(
                SequentialDiscoveryStep(
                    step=step_num,
                    selected_channel_id=None,
                    gain_nats=b_g,
                    se_total=b_se,
                    se_phase1=b_s1,
                    se_phase2=b_s2,
                    z_stat=b_g / max(1e-12, b_se),
                    p_raw=b_pr,
                    p_holm=holm_pvals[best_i],
                    lcb_95=b_g - 1.6448536 * b_se,
                    cost=0.0,
                    stopped_by_lcb=True,
                )
            )
            break

        best_i = max(
            passing,
            key=lambda i: cand_eval[i][1] / max(1e-6, cand_eval[i][0].acquisition_cost),
        )
        win_cand, win_g, win_se, win_s1, win_s2, win_pr, win_pm, win_valm = cand_eval[best_i]
        selected.append(win_cand)
        spent += win_cand.acquisition_cost
        remaining = [c for c in remaining if c.channel_id != win_cand.channel_id]

        step_trace.append(
            SequentialDiscoveryStep(
                step=step_num,
                selected_channel_id=win_cand.channel_id,
                gain_nats=win_g,
                se_total=win_se,
                se_phase1=win_s1,
                se_phase2=win_s2,
                z_stat=win_g / max(1e-12, win_se),
                p_raw=win_pr,
                p_holm=holm_pvals[best_i],
                lcb_95=win_g - 1.6448536 * win_se,
                cost=win_cand.acquisition_cost,
                stopped_by_lcb=False,
            )
        )

        if sequential and remaining and spent < max_budget - 1e-9:
            w_folds = cand_mats[win_cand.channel_id]
            curr_mats = [
                (tr_i, va_i, np.hstack([X_tr, Zw_tr]), np.hstack([X_va, Zw_va]))
                for (tr_i, va_i, X_tr, X_va), (_, _, Zw_tr, Zw_va) in zip(
                    curr_mats, w_folds, strict=True
                )
            ]
            valid_curr = valid_curr & win_valm
            p_curr = win_pm
            ll_curr = _bernoulli_loglik(y_p, p_curr)

    return DiscoveryResult(
        method=method_name,
        pilot_design=pilot.design,
        pilot_fraction=pilot.pilot_fraction,
        n_pilot=pilot.n_pilot,
        kish_ess=pilot.kish_ess,
        pilot_channel_accesses=int(pilot.n_pilot * len(candidates)),
        selected_channels=selected,
        selected_ids=[c.channel_id for c in selected],
        total_cost_spent=spent,
        step_trace=step_trace,
        initial_scores=init_scores,
        initial_ses=init_ses,
    )


def audit_shadow_ledger_two_phase(
    ledger_records: list[dict[str, Any]],
    *,
    pilot_fraction: float = 0.15,
    seed: int = 42,
) -> dict[str, Any]:
    """Run a constrained Neyman two-phase shadow audit over PITM model-vs-human ledger entries."""
    n_total = len(ledger_records)
    if n_total < 8:
        return {
            "status": "awaiting_more_reviews",
            "n_ledger_records": n_total,
            "message": "Log at least 8 PITM decisions on the Now Gate to run live Neyman ledger sampling.",
        }

    is_override = np.array(
        [1 if r.get("providerAction") in ("change", "decline") else 0 for r in ledger_records],
        dtype=int,
    )
    alert_prob = np.array(
        [
            0.35 if (r.get("modelRecommendation") or {}).get("alert") else 0.06
            for r in ledger_records
        ],
        dtype=float,
    )
    pilot = sample_two_phase_pilot(
        is_override,
        alert_prob,
        pilot_fraction=min(1.0, max(pilot_fraction, 8.0 / float(n_total))),
        design="neyman",
        seed=seed,
    )
    idx = pilot.sampled_indices
    est_override, se_tot, se_1, se_2 = two_phase_variance_decomposition(
        is_override[idx].astype(float),
        pilot.ht_weights,
        pilot.stratum_ids_sampled,
        pilot.stratum_pop_counts,
        pilot.stratum_sample_counts,
    )
    return {
        "status": "audited",
        "n_ledger_records": n_total,
        "n_pilot_audited": pilot.n_pilot,
        "pilot_fraction": round(pilot.pilot_fraction, 4),
        "kish_ess": round(pilot.kish_ess, 2),
        "design_effect": round(pilot.design_effect, 3),
        "true_census_override_rate": round(float(np.mean(is_override)), 4),
        "ht_estimated_override_rate": round(est_override, 4),
        "se_total": round(se_tot, 4),
        "se_phase1": round(se_1, 4),
        "se_phase2": round(se_2, 4),
    }

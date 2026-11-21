"""Operational PITM decision contracts, leak-free 4-way splits, Kish ESS, and statistical tests."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass
from enum import StrEnum

import numpy as np
import pandas as pd
from pydantic import BaseModel, ConfigDict, Field, model_validator
from scipy.special import logit
from scipy.stats import norm
from scipy.stats import t as student_t


class SemanticType(StrEnum):
    """Semantic feature types governing leak-free tabular encoding."""

    CONTINUOUS = "continuous"
    COUNT = "count"
    ORDINAL = "ordinal"
    NOMINAL = "nominal"
    BINARY = "binary"
    ENTITY_ID = "entity_id"


class PITMDecisionContract(BaseModel):
    """Immutable cost and precision contract for one Provider-in-the-Middle ED decision."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    decision_type: str
    display_name: str
    cost_fp: float = Field(default=1.0, gt=0.0)
    cost_fn: float = Field(default=20.0, gt=0.0)
    target_precision: float = Field(default=0.35, gt=0.0, lt=1.0)
    default_query_cost: float = Field(default=0.035, ge=0.0)
    safe_omission_alpha: float = Field(default=0.05, gt=0.0, lt=0.5)

    @property
    def bayes_threshold(self) -> float:
        """Optimal Bayes posterior action threshold tau* = C_FP / (C_FP + C_FN)."""
        return float(self.cost_fp / (self.cost_fp + self.cost_fn))

    @property
    def target_fdr(self) -> float:
        """Target False Discovery Rate 1 - PPV for certified alert lists."""
        return float(1.0 - self.target_precision)


PITM_CONTRACTS: dict[str, PITMDecisionContract] = {
    "triage_status": PITMDecisionContract(
        decision_type="triage_status",
        display_name="Triage acuity upgrade (ESI-1 / ESI-2)",
        cost_fp=1.0,
        cost_fn=24.0,
        target_precision=0.35,
        default_query_cost=0.035,
        safe_omission_alpha=0.05,
    ),
    "eval_upgrade": PITMDecisionContract(
        decision_type="eval_upgrade",
        display_name="Missing arrival door evaluation (Trauma / Neuro / D2B)",
        cost_fp=1.0,
        cost_fn=40.0,
        target_precision=0.40,
        default_query_cost=0.025,
        safe_omission_alpha=0.025,
    ),
    "space_priority": PITMDecisionContract(
        decision_type="space_priority",
        display_name="Monitored bed vs. waiting-room placement",
        cost_fp=1.0,
        cost_fn=15.0,
        target_precision=0.35,
        default_query_cost=0.035,
        safe_omission_alpha=0.05,
    ),
    "wr_orders": PITMDecisionContract(
        decision_type="wr_orders",
        display_name="Waiting-room diagnostic set / targeted add-on",
        cost_fp=1.0,
        cost_fn=8.0,
        target_precision=0.30,
        default_query_cost=0.030,
        safe_omission_alpha=0.08,
    ),
    "wr_meds": PITMDecisionContract(
        decision_type="wr_meds",
        display_name="Waiting-room medication safety hold check",
        cost_fp=1.0,
        cost_fn=18.0,
        target_precision=0.35,
        default_query_cost=0.025,
        safe_omission_alpha=0.04,
    ),
}


def capacity_adjusted_space_threshold(
    base_contract: PITMDecisionContract,
    beds_open: int,
    beds_total: int = 18,
) -> float:
    """Adjust monitored-space Bayes threshold moderately under acute bed scarcity.

    Never blocks immediate resuscitation (ESI-1 / D2B), while accounting for higher
    opportunity cost of occupying the last staffed bed on borderline ESI-3 arrivals.
    """
    scarcity_ratio = 1.0 - (max(0, beds_open) / max(1, beds_total))
    effective_cost_fp = base_contract.cost_fp * (1.0 + 0.65 * scarcity_ratio)
    return float(effective_cost_fp / (effective_cost_fp + base_contract.cost_fn))


def wilson_interval(
    successes: int,
    total: int,
    alpha: float = 0.05,
) -> tuple[float, float]:
    """Compute exact Wilson (1927) binomial score confidence interval (low, high)."""
    if total <= 0:
        return (0.0, 1.0)
    k = int(np.clip(successes, 0, total))
    n = float(total)
    p_hat = float(k) / n
    z = float(norm.ppf(1.0 - alpha / 2.0))
    z2 = z * z
    denom = 1.0 + z2 / n
    center = (p_hat + z2 / (2.0 * n)) / denom
    half = (z * math.sqrt((p_hat * (1.0 - p_hat) / n) + (z2 / (4.0 * n * n)))) / denom
    return (float(max(0.0, center - half)), float(min(1.0, center + half)))


def kish_effective_sample_size(weights: np.ndarray) -> tuple[float, float]:
    """Compute Kish (1965) Effective Sample Size (ESS) and Design Effect (DEFF).

    ESS = (sum_i w_i)^2 / sum_i w_i^2, DEFF = n_raw / ESS.
    """
    w = np.asarray(weights, dtype=float).ravel()
    if w.size == 0 or float(np.sum(w**2)) <= 0.0:
        return 0.0, 1.0
    w_sum = float(np.sum(w))
    ess = (w_sum * w_sum) / float(np.sum(w * w))
    deff = float(w.size) / max(1e-9, ess)
    return float(ess), float(deff)


def instance_evidence_deficit(
    probs: np.ndarray | float,
    prevalence: float,
    target_precision: float,
    eps: float = 1e-6,
) -> np.ndarray:
    """Compute Base-Rate Evidence Deficit D_q(x) in log-odds (nats).

    D_q(x) = max(0, logit(q) - logit(pi) - [logit(p(x)) - logit(pi)])
           = max(0, logit(q) - logit(p(x))).
    """
    p = np.clip(np.asarray(probs, dtype=float), eps, 1.0 - eps)
    q = float(np.clip(target_precision, eps, 1.0 - eps))
    # Prior cancels algebraically when p(x) is calibrated at natural prevalence pi,
    # while preserving explicit log-LR interpretation:
    _ = float(np.clip(prevalence, eps, 1.0 - eps))
    return np.maximum(0.0, float(logit(q)) - logit(p))


class SplitProtocol(BaseModel):
    """Immutable 4-way partition (train / cal / val / test) with SHA-256 lineage."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    train_idx: tuple[int, ...]
    cal_idx: tuple[int, ...]
    val_idx: tuple[int, ...]
    test_idx: tuple[int, ...]
    grouped_by: str | None = None
    seed: int = 42
    split_hash: str

    @model_validator(mode="after")
    def verify_disjoint_nonempty(self) -> SplitProtocol:
        parts = [
            set(self.train_idx),
            set(self.cal_idx),
            set(self.val_idx),
            set(self.test_idx),
        ]
        for name, part in zip(("train", "cal", "val", "test"), parts, strict=True):
            if not part:
                raise ValueError(f"Split partition '{name}' cannot be empty.")
        total_unique = len(parts[0] | parts[1] | parts[2] | parts[3])
        if total_unique != sum(len(p) for p in parts):
            raise ValueError("Split partitions have overlapping row indices.")
        return self


def freeze_split(
    df: pd.DataFrame,
    target_col: str,
    *,
    group_col: str | None = None,
    train_frac: float = 0.55,
    cal_frac: float = 0.15,
    val_frac: float = 0.15,
    test_frac: float = 0.15,
    seed: int = 42,
) -> SplitProtocol:
    """Create a leak-free SHA-256 4-way split BEFORE any preprocessing or model fitting."""
    if not np.isclose(train_frac + cal_frac + val_frac + test_frac, 1.0, atol=1e-5):
        raise ValueError("Partition fractions must sum to 1.0.")

    n = len(df)
    if n < 24:
        raise ValueError(f"Dataset too small ({n} rows) for 4-way splitting.")

    y = np.asarray(df[target_col], dtype=int)
    rng = np.random.default_rng(seed)
    fracs = np.array([train_frac, cal_frac, val_frac, test_frac])

    if group_col is not None:
        groups = df[group_col].to_numpy()
        grp_df = pd.DataFrame({"row_pos": np.arange(n), "grp": groups, "y": y})
        grp_stats = grp_df.groupby("grp", sort=False).agg(has_pos=("y", "max")).reset_index()
        pos_grps = grp_stats[grp_stats["has_pos"] > 0]["grp"].to_numpy().copy()
        neg_grps = grp_stats[grp_stats["has_pos"] == 0]["grp"].to_numpy().copy()
        rng.shuffle(pos_grps)
        rng.shuffle(neg_grps)

        buckets: list[list[object]] = [[], [], [], []]
        for grp_arr in (pos_grps, neg_grps):
            m = len(grp_arr)
            if m < 4:
                raise ValueError(f"Too few distinct groups ({m}) to populate 4 partitions.")
            cuts = np.round(np.cumsum(fracs)[:-1] * m).astype(int)
            cuts = np.maximum.accumulate(np.clip(cuts, [1, 2, 3], [m - 3, m - 2, m - 1]))
            for b_idx, chunk in enumerate(np.split(grp_arr, cuts)):
                buckets[b_idx].extend(chunk.tolist())

        grp_to_bucket = {g: b_idx for b_idx, g_list in enumerate(buckets) for g in g_list}
        assigned = np.array([grp_to_bucket[g] for g in groups], dtype=int)
        train_idx = np.flatnonzero(assigned == 0)
        cal_idx = np.flatnonzero(assigned == 1)
        val_idx = np.flatnonzero(assigned == 2)
        test_idx = np.flatnonzero(assigned == 3)
    else:
        pos_idx = np.flatnonzero(y == 1)
        neg_idx = np.flatnonzero(y == 0)
        rng.shuffle(pos_idx)
        rng.shuffle(neg_idx)
        part_lists: list[list[int]] = [[], [], [], []]
        for cls_idx in (pos_idx, neg_idx):
            m = len(cls_idx)
            cuts = np.round(np.cumsum(fracs)[:-1] * m).astype(int)
            cuts = np.maximum.accumulate(np.clip(cuts, [1, 2, 3], [m - 3, m - 2, m - 1]))
            for b_idx, chunk in enumerate(np.split(cls_idx, cuts)):
                part_lists[b_idx].extend(int(i) for i in chunk)
        train_idx = np.sort(np.array(part_lists[0], dtype=int))
        cal_idx = np.sort(np.array(part_lists[1], dtype=int))
        val_idx = np.sort(np.array(part_lists[2], dtype=int))
        test_idx = np.sort(np.array(part_lists[3], dtype=int))

    payload = {
        "train": [int(i) for i in train_idx],
        "cal": [int(i) for i in cal_idx],
        "val": [int(i) for i in val_idx],
        "test": [int(i) for i in test_idx],
        "grouped_by": group_col,
        "seed": seed,
    }
    shash = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()

    return SplitProtocol(
        train_idx=tuple(int(i) for i in train_idx),
        cal_idx=tuple(int(i) for i in cal_idx),
        val_idx=tuple(int(i) for i in val_idx),
        test_idx=tuple(int(i) for i in test_idx),
        grouped_by=group_col,
        seed=seed,
        split_hash=shash,
    )


@dataclass(frozen=True, slots=True)
class TOSTSummary:
    """Schuirmann (1987) Two One-Sided Tests (TOST) with Nadeau-Bengio (2003) SEs."""

    mean_delta: float
    corrected_se: float
    margin: float
    ci90_low: float
    ci90_high: float
    df: int
    p_noninferiority: float
    p_tost_equivalence: float
    noninferior_at_05: bool
    equivalent_at_05: bool

    def to_dict(self) -> dict[str, float | int | bool]:
        return asdict(self)


def nadeau_bengio_tost(
    deltas: list[float] | np.ndarray,
    margin: float,
    n_test: int,
    n_train: int,
) -> TOSTSummary:
    """Compute unmodified Nadeau-Bengio variance-corrected TOST non-inferiority & equivalence."""
    if margin <= 0.0:
        raise ValueError("TOST margin must be strictly positive.")
    arr = np.asarray(deltas, dtype=float).ravel()
    k = int(arr.size)
    mean_d = float(np.mean(arr)) if k > 0 else 0.0
    if k < 2:
        return TOSTSummary(mean_d, 0.0, margin, mean_d, mean_d, 0, 1.0, 1.0, False, False)

    s2 = float(np.var(arr, ddof=1))
    overlap = float(n_test) / max(1.0, float(n_train))
    se_nb = float(np.sqrt((1.0 / float(k) + overlap) * s2))
    df = k - 1
    if se_nb <= 1e-15:
        ni_ok = bool(mean_d > -margin)
        eq_ok = bool(abs(mean_d) < margin)
        return TOSTSummary(
            mean_d,
            0.0,
            margin,
            mean_d,
            mean_d,
            df,
            0.0 if ni_ok else 1.0,
            0.0 if eq_ok else 1.0,
            ni_ok,
            eq_ok,
        )

    t_crit90 = float(student_t.ppf(0.95, df=df))
    ci_lo = mean_d - t_crit90 * se_nb
    ci_hi = mean_d + t_crit90 * se_nb
    p_ni = float(student_t.sf((mean_d + margin) / se_nb, df=df))
    p_up = float(student_t.sf((margin - mean_d) / se_nb, df=df))
    p_tost = float(max(p_ni, p_up))
    return TOSTSummary(
        mean_delta=mean_d,
        corrected_se=se_nb,
        margin=margin,
        ci90_low=ci_lo,
        ci90_high=ci_hi,
        df=df,
        p_noninferiority=p_ni,
        p_tost_equivalence=p_tost,
        noninferior_at_05=bool(p_ni < 0.05),
        equivalent_at_05=bool(p_tost < 0.05),
    )


def holm_bonferroni_adjust(p_values: list[float]) -> list[float]:
    """Step-down Holm-Bonferroni family-wise error rate (FWER) adjusted p-values."""
    m = len(p_values)
    if m == 0:
        return []
    pv = np.clip(np.asarray(p_values, dtype=float), 0.0, 1.0)
    order = np.argsort(pv)
    adj_sorted = np.empty(m, dtype=float)
    running_max = 0.0
    for rank_0, orig_i in enumerate(order):
        scaled = float((m - rank_0) * pv[orig_i])
        running_max = max(running_max, min(1.0, scaled))
        adj_sorted[rank_0] = running_max
    out = np.empty(m, dtype=float)
    for rank_0, orig_i in enumerate(order):
        out[orig_i] = adj_sorted[rank_0]
    return out.tolist()


def benjamini_hochberg_mask(p_values: list[float] | np.ndarray, fdr_q: float = 0.10) -> list[bool]:
    """Step-up Benjamini-Hochberg multiplicity mask at target FDR level q."""
    pv = np.asarray(p_values, dtype=float).ravel()
    m = int(pv.size)
    if m == 0:
        return []
    order = np.argsort(pv)
    survives = [False] * m
    max_k = -1
    for rank_0, orig_idx in enumerate(order):
        thresh = ((rank_0 + 1) / float(m)) * float(fdr_q)
        if pv[orig_idx] <= thresh:
            max_k = rank_0
    if max_k >= 0:
        for rank_0 in range(max_k + 1):
            survives[int(order[rank_0])] = True
    return survives

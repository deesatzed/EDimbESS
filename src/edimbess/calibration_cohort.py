"""Leak-free TabularPreprocessor, Platt calibration, and shared ED cohort/packet feature schema."""

from __future__ import annotations

import re
import warnings
from dataclasses import dataclass
from typing import Any

import lightgbm as lgb
import numpy as np
import pandas as pd
from scipy.special import expit, logit
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.preprocessing import OneHotEncoder, OrdinalEncoder, StandardScaler

from edimbess.contracts import SemanticType
from edimbess.sentinels_and_rules import affirmed_text, esi_number, mentions_ste

_PROB_EPS = 1e-12


@dataclass(frozen=True, slots=True)
class Stage2ChannelSpec:
    """Descriptor for an acquirable Stage-2 ED dossier, HIE, dispense, or bedside channel."""

    channel_id: str
    display_name: str
    columns: tuple[str, ...]
    acquisition_cost: float
    per_patient_query_cost: float
    mechanism_tag: str


BASE_SEMANTIC_TYPES: dict[str, SemanticType] = {
    "age_years": SemanticType.CONTINUOUS,
    "sex": SemanticType.NOMINAL,
    "arrival_mode": SemanticType.NOMINAL,
    "charted_esi_num": SemanticType.ORDINAL,
    "complaint_family": SemanticType.NOMINAL,
    "hr": SemanticType.CONTINUOUS,
    "sbp": SemanticType.CONTINUOUS,
    "dbp": SemanticType.CONTINUOUS,
    "rr": SemanticType.CONTINUOUS,
    "spo2": SemanticType.CONTINUOUS,
    "temp_c": SemanticType.CONTINUOUS,
    "shock_index": SemanticType.CONTINUOUS,
    "delta_hr": SemanticType.CONTINUOUS,
    "delta_sbp": SemanticType.CONTINUOUS,
    "gcs_drop": SemanticType.BINARY,
    "ekg_ste": SemanticType.BINARY,
    "ekg_missing_indicated": SemanticType.BINARY,
    "missing_door_eval": SemanticType.BINARY,
    "high_risk_mech_surface": SemanticType.BINARY,
    "minimizing_framing_flag": SemanticType.BINARY,
}

ED_STAGE2_CHANNELS: tuple[Stage2ChannelSpec, ...] = (
    Stage2ChannelSpec(
        channel_id="ch_hie_outside",
        display_name="Regional HIE 72h bounceback & ICU/intubation feed",
        columns=("hie_bounceback_72h", "hie_ed_visits_90d", "hie_prior_icu_cpr_intub"),
        acquisition_cost=1.15,
        per_patient_query_cost=0.040,
        mechanism_tag="external_hie",
    ),
    Stage2ChannelSpec(
        channel_id="ch_dispense_pdmp",
        display_name="Pharmacy dispense & antithrombotic/immuno fill feed",
        columns=(
            "undisclosed_anticoag_fill",
            "high_risk_hypoglycemic_fill",
            "immunosuppressant_active_fill",
        ),
        acquisition_cost=1.05,
        per_patient_query_cost=0.030,
        mechanism_tag="pharmacy_dispense",
    ),
    Stage2ChannelSpec(
        channel_id="ch_micro_cultures",
        display_name="Unreviewed microbiology & culture susceptibility feed",
        columns=("unreviewed_resistant_culture", "recent_bacteremia_flag"),
        acquisition_cost=0.95,
        per_patient_query_cost=0.025,
        mechanism_tag="microbiology",
    ),
    Stage2ChannelSpec(
        channel_id="ch_recent_labs_di",
        display_name="Prior labs & structural echo/aneurysm imaging extract",
        columns=("recent_hgb_g_dl", "recent_egfr", "recent_wbc", "known_aaa_or_low_ef"),
        acquisition_cost=0.95,
        per_patient_query_cost=0.025,
        mechanism_tag="labs_imaging",
    ),
    Stage2ChannelSpec(
        channel_id="ch_social_isolation",
        display_name="MDRO isolation, cirrhosis coagulopathy & safe-discharge registry",
        columns=("active_mdro_isolation", "lives_alone_frail", "cirrhosis_coagulopathy"),
        acquisition_cost=0.80,
        per_patient_query_cost=0.020,
        mechanism_tag="care_context",
    ),
    Stage2ChannelSpec(
        channel_id="ch_bedside_ask_probe",
        display_name="Targeted 1-question bedside VOI verification",
        columns=("confirmed_symptom_progression", "confirmed_med_adherence_gap"),
        acquisition_cost=0.90,
        per_patient_query_cost=0.030,
        mechanism_tag="bedside_voi_ask",
    ),
)


class TabularPreprocessor:
    """Strict train-only fitted preprocessor with explicit missingness indicators."""

    def __init__(
        self,
        semantic_types: dict[str, SemanticType] | None = None,
        max_ohe_categories: int = 20,
    ) -> None:
        self.declared_types = semantic_types or {}
        self.max_ohe_categories = max_ohe_categories
        self.semantic_types_: dict[str, SemanticType] = {}
        self.miss_cols_: list[str] = []
        self.num_cols_: list[str] = []
        self.ord_cols_: list[str] = []
        self.nom_cols_: list[str] = []
        self.bin_cols_: list[str] = []
        self._num_imp: SimpleImputer | None = None
        self._num_scl: StandardScaler | None = None
        self._ord_enc: OrdinalEncoder | None = None
        self._nom_enc: OneHotEncoder | None = None
        self._bin_imp: SimpleImputer | None = None
        self.feature_names_out_: list[str] = []
        self._fitted = False

    def _infer_col_type(self, s: pd.Series, col: str) -> SemanticType:
        if col in self.declared_types:
            return self.declared_types[col]
        if pd.api.types.is_bool_dtype(s):
            return SemanticType.BINARY
        if pd.api.types.is_numeric_dtype(s):
            non_null = s.dropna()
            uniq = set(non_null.unique().tolist()) if len(non_null) > 0 else set()
            if uniq and all(v in (0, 1, 0.0, 1.0) for v in uniq):
                return SemanticType.BINARY
            return SemanticType.CONTINUOUS
        return SemanticType.NOMINAL

    def fit(self, X_train: pd.DataFrame) -> TabularPreprocessor:
        if any(not isinstance(c, str) for c in X_train.columns):
            X_train = X_train.rename(columns=str)

        self.semantic_types_ = {}
        self.miss_cols_ = []
        self.num_cols_ = []
        self.ord_cols_ = []
        self.nom_cols_ = []
        self.bin_cols_ = []

        for col in X_train.columns:
            stype = self._infer_col_type(X_train[col], col)
            self.semantic_types_[col] = stype
            if stype == SemanticType.ENTITY_ID:
                continue
            miss_rate = float(X_train[col].isna().mean())
            if 0.005 < miss_rate < 0.995:
                self.miss_cols_.append(col)
            if stype in (SemanticType.CONTINUOUS, SemanticType.COUNT):
                self.num_cols_.append(col)
            elif stype == SemanticType.ORDINAL:
                self.ord_cols_.append(col)
            elif stype == SemanticType.NOMINAL:
                self.nom_cols_.append(col)
            elif stype == SemanticType.BINARY:
                self.bin_cols_.append(col)

        names: list[str] = []
        if self.num_cols_:
            num_df = X_train[self.num_cols_].apply(pd.to_numeric, errors="coerce")
            valid_num = [c for c in self.num_cols_ if num_df[c].notna().any()]
            self.num_cols_ = valid_num
            if self.num_cols_:
                self._num_imp = SimpleImputer(strategy="median")
                self._num_scl = StandardScaler()
                arr = self._num_imp.fit_transform(num_df[self.num_cols_])
                self._num_scl.fit(arr)
                names.extend(self.num_cols_)

        if self.bin_cols_:
            bin_df = X_train[self.bin_cols_].apply(pd.to_numeric, errors="coerce")
            valid_bin = [c for c in self.bin_cols_ if bin_df[c].notna().any()]
            self.bin_cols_ = valid_bin
            if self.bin_cols_:
                self._bin_imp = SimpleImputer(strategy="most_frequent")
                self._bin_imp.fit(bin_df[self.bin_cols_])
                names.extend(self.bin_cols_)

        if self.ord_cols_:
            ord_str = X_train[self.ord_cols_].fillna("__MISSING__").astype(str)
            self._ord_enc = OrdinalEncoder(handle_unknown="use_encoded_value", unknown_value=-1)
            self._ord_enc.fit(ord_str)
            names.extend(self.ord_cols_)

        if self.nom_cols_:
            nom_str = X_train[self.nom_cols_].fillna("__MISSING__").astype(str)
            self._nom_enc = OneHotEncoder(
                handle_unknown="ignore",
                max_categories=self.max_ohe_categories,
                sparse_output=False,
            )
            self._nom_enc.fit(nom_str)
            names.extend([str(x) for x in self._nom_enc.get_feature_names_out(self.nom_cols_)])

        names.extend([f"miss__{c}" for c in self.miss_cols_])
        self.feature_names_out_ = names
        self._fitted = True
        return self

    def transform(self, X: pd.DataFrame) -> np.ndarray:
        if not self._fitted:
            raise RuntimeError("TabularPreprocessor must be fit on X_train before transform.")
        if any(not isinstance(c, str) for c in X.columns):
            X = X.rename(columns=str)

        blocks: list[np.ndarray] = []
        if self.num_cols_ and self._num_imp is not None and self._num_scl is not None:
            num_df = X[self.num_cols_].apply(pd.to_numeric, errors="coerce")
            blocks.append(self._num_scl.transform(self._num_imp.transform(num_df)))
        if self.bin_cols_ and self._bin_imp is not None:
            bin_df = X[self.bin_cols_].apply(pd.to_numeric, errors="coerce")
            blocks.append(self._bin_imp.transform(bin_df))
        if self.ord_cols_ and self._ord_enc is not None:
            ord_str = X[self.ord_cols_].fillna("__MISSING__").astype(str)
            blocks.append(self._ord_enc.transform(ord_str))
        if self.nom_cols_ and self._nom_enc is not None:
            nom_str = X[self.nom_cols_].fillna("__MISSING__").astype(str)
            blocks.append(self._nom_enc.transform(nom_str))
        if self.miss_cols_:
            miss_arr = np.column_stack([X[c].isna().to_numpy(dtype=float) for c in self.miss_cols_])
            blocks.append(miss_arr)

        if not blocks:
            return np.zeros((len(X), 1), dtype=np.float64)
        return np.hstack(blocks).astype(np.float64, copy=False)

    def fit_transform(self, X_train: pd.DataFrame) -> np.ndarray:
        return self.fit(X_train).transform(X_train)


class PlattLogitCalibrator:
    """Natural-prevalence monotonic logit-scale Platt calibrator."""

    def __init__(self) -> None:
        self._lr = LogisticRegression(C=10.0, max_iter=500)
        self._active = False

    def fit(self, raw_probs: np.ndarray, y_cal: np.ndarray) -> PlattLogitCalibrator:
        y = np.asarray(y_cal, dtype=int).ravel()
        if int(y.sum()) < 5 or int((1 - y).sum()) < 5:
            self._active = False
            return self
        z = logit(
            np.clip(np.asarray(raw_probs, dtype=float).ravel(), _PROB_EPS, 1.0 - _PROB_EPS)
        ).reshape(-1, 1)
        self._lr.fit(z, y)
        self._active = bool(float(self._lr.coef_[0, 0]) > 0.0)
        return self

    def predict_proba(self, raw_probs: np.ndarray) -> np.ndarray:
        p = np.clip(np.asarray(raw_probs, dtype=float).ravel(), _PROB_EPS, 1.0 - _PROB_EPS)
        if not self._active:
            return p
        z = logit(p).reshape(-1, 1)
        return np.clip(
            np.asarray(self._lr.predict_proba(z)[:, 1], dtype=float), _PROB_EPS, 1.0 - _PROB_EPS
        )


def make_stabilized_lgbm(seed: int = 42, n_estimators: int = 130) -> lgb.LGBMClassifier:
    """Create Newton-stabilized (max_delta_step=1.0) LightGBM classifier."""
    return lgb.LGBMClassifier(
        n_estimators=n_estimators,
        learning_rate=0.05,
        num_leaves=23,
        min_child_samples=18,
        subsample=0.85,
        subsample_freq=1,
        colsample_bytree=0.85,
        reg_alpha=0.5,
        reg_lambda=3.0,
        max_delta_step=1.0,
        random_state=seed,
        n_jobs=2,
        verbose=-1,
    )


def classify_complaint_family(chief_complaint: str) -> str:
    c = affirmed_text(chief_complaint)
    if re.search(r"chest pain|chest pressure", c):
        return "chest"
    if re.search(r"shortness|\bsob\b|dyspnea|cough", c):
        return "respiratory"
    if re.search(r"syncope|fainted|passed out", c):
        return "syncope"
    if re.search(r"slurred|weakness|droop|aphasia", c):
        return "focal_neuro"
    if re.search(r"finger|hydraulic|eye|grinding", c):
        return "surgical_mech"
    if re.search(r"mvc|collision", c):
        return "vehicular_trauma"
    if re.search(r"fall|tripped", c):
        return "fall"
    if re.search(r"flank", c):
        return "flank"
    if re.search(r"epigastric|abdominal|lower quadrant|groin", c):
        return "abdominal"
    if re.search(r"back", c):
        return "back"
    if re.search(r"dizz|fatigue", c):
        return "dizzy_fatigue"
    return "minor_msk_wound"


def packet_to_feature_frames(packet: dict[str, Any]) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Map a single syn-ED-### packet into exact (X_base, Z_pool) single-row DataFrames."""
    enc = packet.get("encounter", {})
    dos = packet.get("dossier", {})
    vitals = enc.get("vitals") or {}
    prior = enc.get("prior_vitals") or []
    evals = enc.get("evals") or {}
    ekg = enc.get("ekg") or {}

    chief = str(enc.get("chief_complaint", ""))
    nurse = str(enc.get("nurse_hx_summary", ""))
    pmh = str(dos.get("pmh_summary", ""))
    surg = str(dos.get("surgical_hx_summary", ""))
    social = str(dos.get("social_hx_summary", ""))
    meds = [str(m) for m in dos.get("current_meds", []) if str(m).lower() != "none recorded"]
    meds_blob = " ".join(meds).lower()
    full_blob = f"{chief} {nurse} {pmh} {surg} {social} {meds_blob}".lower()
    note_clean = affirmed_text(f"{chief}. {nurse}")

    hr = float(vitals.get("hr", 82.0))
    sbp = float(vitals.get("sbp", 126.0))
    dbp = float(vitals.get("dbp", 76.0))
    rr = float(vitals.get("rr", 16.0))
    spo2 = float(vitals.get("spo2", 98.0))
    temp_c = float(vitals.get("temp_c", 36.8))

    if prior:
        p0 = prior[0]
        delta_hr = hr - float(p0.get("hr", hr))
        delta_sbp = sbp - float(p0.get("sbp", sbp))
        gcs_drop = int(
            vitals.get("gcs") is not None
            and p0.get("gcs") is not None
            and int(vitals["gcs"]) < int(p0["gcs"])
        )
    else:
        delta_hr = 0.0
        delta_sbp = 0.0
        gcs_drop = int(vitals.get("gcs") is not None and int(vitals["gcs"]) < 15)

    ekg_ind = bool(
        re.search(
            r"syncope|fainted|chest|shortness|\bsob\b|epigastric|dizz|fatigue",
            affirmed_text(chief),
        )
    )
    ekg_missing = int(ekg_ind and ekg.get("status") != "done")
    missing_eval = int(any(evals.get(k) == "missing" for k in ("trauma", "neuro", "d2b")))
    high_mech_surface = int(
        bool(
            re.search(
                r"pressure-injected|hydraulic|grinding metal|metal fleck.*eye|high-speed mvc|fall from.*ladder|fall from.*height",
                f"{chief} {nurse}".lower(),
            )
        )
    )
    minimizing_flag = int(
        bool(
            re.search(
                r"looked minor|looks benign|routine strain|frequent visitor|smells of alcohol|looks comfortable|minimal",
                nurse.lower(),
            )
        )
    )

    x_row = pd.DataFrame(
        [
            {
                "age_years": float(enc.get("age_years", 50)),
                "sex": str(enc.get("sex", "U")),
                "arrival_mode": str(enc.get("arrival_mode", "walk-in")),
                "charted_esi_num": esi_number(enc.get("triage_status", "ESI-3")),
                "complaint_family": classify_complaint_family(chief),
                "hr": hr,
                "sbp": sbp,
                "dbp": dbp,
                "rr": rr,
                "spo2": spo2,
                "temp_c": temp_c,
                "shock_index": hr / max(50.0, sbp),
                "delta_hr": delta_hr,
                "delta_sbp": delta_sbp,
                "gcs_drop": gcs_drop,
                "ekg_ste": int(mentions_ste(ekg.get("summary"))),
                "ekg_missing_indicated": ekg_missing,
                "missing_door_eval": missing_eval,
                "high_risk_mech_surface": high_mech_surface,
                "minimizing_framing_flag": minimizing_flag,
            }
        ]
    )

    # Extract Stage-2 Dossier / HIE / Dispense / Lab / Micro features
    visits = dos.get("last_ed_visits", [])
    bounce_72h = int(
        any(
            re.search(
                r"\b(today|yesterday|1 day|2 days|3 days|5 days)\b",
                str(v.get("when", "")).lower(),
            )
            for v in visits
        )
    )
    prior_icu = int(bool(re.search(r"intubation|prior cpr|cardiac arrest|icu stay", full_blob)))
    has_anticoag = bool(
        re.search(r"apixaban|rivaroxaban|warfarin|dabigatran|clopidogrel|aspirin", meds_blob)
    )
    mentioned_in_note = bool(re.search(r"anticoagul|blood thinner|apixaban|warfarin", note_clean))
    undisclosed_anticoag = int(has_anticoag and not mentioned_in_note)
    hypo_med = int(bool(re.search(r"glipizide|glyburide|insulin", meds_blob)))
    immuno_med = int(
        bool(
            re.search(
                r"tacrolimus|mycophenolate|prednisone|cyclosporine|methotrexate|rituximab",
                meds_blob,
            )
        )
    )

    labs = dos.get("recent_labs", [])
    di = dos.get("recent_di", [])
    unrev_culture = 0
    hgb_val: float = np.nan
    egfr_val: float = np.nan
    wbc_val: float = np.nan
    for lb in labs:
        t_l = str(lb.get("test", "")).lower()
        v_s = str(lb.get("value", ""))
        if "culture" in t_l and ("esbl" in v_s.lower() or "resistant" in v_s.lower()):
            unrev_culture = 1
        m_num = re.search(r"(\d+(?:\.\d+)?)", v_s)
        if m_num:
            val_f = float(m_num.group(1))
            if "hemoglobin" in t_l:
                hgb_val = val_f
            elif "egfr" in t_l:
                egfr_val = val_f
            elif "creatinine" in t_l and np.isnan(egfr_val):
                egfr_val = max(10.0, min(110.0, 82.0 / max(0.6, val_f)))
            elif "wbc" in t_l:
                wbc_val = val_f

    bacteremia_flag = int("bacteremia" in full_blob or "ivdu" in full_blob)
    di_txt = " ".join(f"{d.get('study', '')} {d.get('summary', '')}" for d in di).lower()
    aaa_or_ef = int(
        bool(re.search(r"aneurysm|\baaa\b|lvef\s*2\d|ef\s*2\d|ef\s*30", f"{pmh.lower()} {di_txt}"))
    )
    mdro_iso = int(
        bool(enc.get("isolation_flag"))
        or bool(re.search(r"\bmrsa\b|\bvre\b|c\.\s*diff", full_blob))
    )
    lives_alone_frail = int(
        "lives alone" in social.lower() and float(enc.get("age_years", 50)) >= 65
    )
    cirrhosis_coag = int(bool(re.search(r"cirrhosis|dialysis|hemodialysis", full_blob)))
    symp_prog = int(
        bounce_72h == 1
        or delta_hr >= 4.0
        or delta_sbp <= -6.0
        or gcs_drop == 1
        or high_mech_surface == 1
    )
    med_gap = int(undisclosed_anticoag == 1 or unrev_culture == 1 or hypo_med == 1)

    z_row = pd.DataFrame(
        [
            {
                "hie_bounceback_72h": bounce_72h,
                "hie_ed_visits_90d": float(len(visits)),
                "hie_prior_icu_cpr_intub": prior_icu,
                "undisclosed_anticoag_fill": undisclosed_anticoag,
                "high_risk_hypoglycemic_fill": hypo_med,
                "immunosuppressant_active_fill": immuno_med,
                "unreviewed_resistant_culture": unrev_culture,
                "recent_bacteremia_flag": bacteremia_flag,
                "recent_hgb_g_dl": hgb_val,
                "recent_egfr": egfr_val,
                "recent_wbc": wbc_val,
                "known_aaa_or_low_ef": aaa_or_ef,
                "active_mdro_isolation": mdro_iso,
                "lives_alone_frail": lives_alone_frail,
                "cirrhosis_coagulopathy": cirrhosis_coag,
                "confirmed_symptom_progression": symp_prog,
                "confirmed_med_adherence_gap": med_gap,
            }
        ]
    )
    return x_row, z_row


@dataclass
class EDCalibrationCohort:
    """Synthetic post-triage ED benchmark cohort with patient groups and Stage-2 channels."""

    X_base: pd.DataFrame
    Z_pool: pd.DataFrame
    y: np.ndarray
    patient_ids: np.ndarray
    candidates: list[Stage2ChannelSpec]
    acquisition_budget: float


def generate_ed_calibration_cohort(
    n_encounters: int = 5600,
    *,
    include_stress_channels: bool = False,
    seed: int = 42,
) -> EDCalibrationCohort:
    """Generate a clinically grounded post-triage ED cohort matching packet_to_feature_frames.

    Captures three realistic ED triage regimes:
    1. Clear door instability / STEMI / high-energy mechanism -> high Stage-1 posterior p_1 >> tau*.
    2. Benign minor MSK / simple wound -> very low posterior p_1 << tau* & low Stage-2 variance.
    3. Ambiguous ESI-3 overlap -> moderate p_1 near tau* where hidden HIE bouncebacks, undisclosed
       anticoagulation, resistant cultures, severe anemia/renal dysfunction, or AAA/low-EF drive Y=1.
    """
    rng = np.random.default_rng(seed)
    n = int(n_encounters)

    # Repeated patient encounters (~4,300 unique patients across 5,600 visits)
    n_unique_pts = max(100, int(round(0.77 * n)))
    patient_ids = np.array([f"PT-{i:05d}" for i in rng.integers(0, n_unique_pts, size=n)])

    families = np.array(
        [
            "chest",
            "respiratory",
            "syncope",
            "focal_neuro",
            "surgical_mech",
            "vehicular_trauma",
            "fall",
            "flank",
            "abdominal",
            "back",
            "dizzy_fatigue",
            "minor_msk_wound",
        ]
    )
    fam_probs = np.array([0.09, 0.08, 0.05, 0.04, 0.02, 0.03, 0.09, 0.08, 0.14, 0.09, 0.10, 0.19])
    fam_probs /= fam_probs.sum()
    comp_fam = rng.choice(families, size=n, p=fam_probs)

    is_minor = comp_fam == "minor_msk_wound"
    is_acute_fam = np.isin(comp_fam, ["chest", "focal_neuro", "surgical_mech", "vehicular_trauma"])

    age = np.clip(
        np.where(is_minor, rng.normal(34, 14, n), rng.normal(58, 17, n)), 18.0, 94.0
    ).round(0)
    sex = rng.choice(["F", "M"], size=n, p=[0.52, 0.48])
    arrival_mode = np.where(
        is_acute_fam & (rng.uniform(0, 1, n) < 0.42),
        "ems",
        np.where(rng.uniform(0, 1, n) < 0.14, "ems", "walk-in"),
    )

    charted_esi = np.where(
        is_minor,
        rng.choice([4, 5, 3], size=n, p=[0.74, 0.14, 0.12]),
        np.where(
            is_acute_fam,
            rng.choice([2, 3], size=n, p=[0.58, 0.42]),
            rng.choice([3, 2, 4], size=n, p=[0.80, 0.10, 0.10]),
        ),
    )

    hr = np.clip(
        rng.normal(82, 13, n) + np.where(is_minor, -6.0, 5.0) + (charted_esi == 2) * 10.0,
        40.0,
        158.0,
    ).round(0)
    sbp = np.clip(rng.normal(128, 18, n) - (charted_esi == 2) * 8.0, 70.0, 205.0).round(0)
    # Inject small shock tail (1.8%)
    shock_mask = (~is_minor) & (rng.uniform(0, 1, n) < 0.018)
    sbp = np.where(shock_mask, rng.uniform(72, 88, n).round(0), sbp)
    hr = np.where(shock_mask, np.clip(hr + 28, 108, 152), hr)

    dbp = np.clip(0.60 * sbp + rng.normal(2, 6, n), 40.0, 125.0).round(0)
    rr = np.clip(rng.normal(16.5, 3.0, n) + (comp_fam == "respiratory") * 5.5, 10.0, 36.0).round(0)
    spo2 = np.clip(
        98.2 - np.abs(rng.normal(0, 1.4, n)) - (comp_fam == "respiratory") * rng.uniform(1, 9, n),
        81.0,
        100.0,
    ).round(0)
    temp_c = np.clip(rng.normal(36.85, 0.48, n), 35.4, 39.9).round(1)

    has_repeat_vs = (~is_minor) & (rng.uniform(0, 1, n) < 0.48)
    delta_hr = np.where(has_repeat_vs, rng.normal(1.2, 5.0, n).round(0), 0.0)
    delta_sbp = np.where(has_repeat_vs, rng.normal(-1.5, 7.5, n).round(0), 0.0)
    gcs_drop = ((comp_fam == "fall") & (rng.uniform(0, 1, n) < 0.06)).astype(int)
    ekg_ste = ((comp_fam == "chest") & (rng.uniform(0, 1, n) < 0.09)).astype(int)
    ekg_missing = (
        np.isin(comp_fam, ["dizzy_fatigue", "syncope", "epigastric", "chest"])
        & (rng.uniform(0, 1, n) < 0.14)
    ).astype(int)
    missing_door_eval = (
        (ekg_ste == 1)
        | ((comp_fam == "focal_neuro") & (rng.uniform(0, 1, n) < 0.22))
        | (shock_mask & (comp_fam == "fall"))
    ).astype(int)
    high_risk_mech = (
        (comp_fam == "surgical_mech")
        | ((comp_fam == "vehicular_trauma") & (rng.uniform(0, 1, n) < 0.55))
    ).astype(int)

    # Stage-2 latent clinical truth (concentrated in ESI-2/3 non-minor presentations)
    non_minor = (~is_minor).astype(float)
    hie_bounce_72h = (non_minor * (rng.uniform(0, 1, n) < 0.085)).astype(float)
    hie_visits_90d = np.where(
        hie_bounce_72h > 0, rng.poisson(2.1, n) + 1, rng.poisson(0.35 * non_minor, n)
    ).astype(float)
    hie_prior_icu = (non_minor * (age >= 50) * (rng.uniform(0, 1, n) < 0.055)).astype(float)
    # Realistic 10% HIE unavailability (missingness indicator check!)
    hie_unavail = rng.uniform(0, 1, n) < 0.10
    hie_bounce_obs = np.where(hie_unavail, np.nan, hie_bounce_72h)
    hie_visits_obs = np.where(hie_unavail, np.nan, hie_visits_90d)

    undisc_anticoag = (non_minor * (age >= 55) * (rng.uniform(0, 1, n) < 0.11)).astype(int)
    hypo_med = (non_minor * (age >= 50) * (rng.uniform(0, 1, n) < 0.075)).astype(int)
    immuno_med = (non_minor * (rng.uniform(0, 1, n) < 0.055)).astype(int)

    unrev_culture = (non_minor * (hie_bounce_72h > 0) * (rng.uniform(0, 1, n) < 0.24)).astype(int)
    bacteremia = (non_minor * (rng.uniform(0, 1, n) < 0.035)).astype(int)

    true_hgb = np.clip(
        rng.normal(13.2, 1.5, n)
        - non_minor * (rng.uniform(0, 1, n) < 0.06) * rng.uniform(4.2, 6.4, n),
        5.8,
        16.8,
    )
    true_egfr = np.clip(
        rng.normal(78.0, 20.0, n)
        - (age - 45.0) * 0.45
        - non_minor * (rng.uniform(0, 1, n) < 0.07) * 35.0,
        8.0,
        115.0,
    )
    true_wbc = np.clip(rng.normal(7.8, 2.2, n) + (unrev_culture + bacteremia) * 5.2, 3.0, 26.0)
    labs_missing = rng.uniform(0, 1, n) < np.where(is_minor, 0.72, 0.26)
    obs_hgb = np.where(labs_missing, np.nan, true_hgb.round(1))
    obs_egfr = np.where(labs_missing, np.nan, true_egfr.round(0))
    obs_wbc = np.where(labs_missing, np.nan, true_wbc.round(1))

    aaa_or_ef = (non_minor * (age >= 52) * (rng.uniform(0, 1, n) < 0.05)).astype(int)
    mdro_iso = (non_minor * (rng.uniform(0, 1, n) < 0.045)).astype(int)
    lives_alone_frail = ((age >= 68) & (rng.uniform(0, 1, n) < 0.22)).astype(int)
    cirrhosis_coag = (non_minor * (rng.uniform(0, 1, n) < 0.045)).astype(int)

    symp_prog = (
        (hie_bounce_72h > 0)
        | (delta_hr >= 5)
        | (delta_sbp <= -8)
        | (high_risk_mech == 1)
        | (non_minor.astype(bool) & (rng.uniform(0, 1, n) < 0.08))
    ).astype(int)
    med_gap = (
        (undisc_anticoag == 1)
        | (unrev_culture == 1)
        | (hypo_med == 1 & (rng.uniform(0, 1, n) < 0.5))
    ).astype(int)

    # Subjective minimizing framing occurs on bouncebacks/falls/back aches WITHOUT lowering true risk!
    minimizing_flag = (
        ((hie_bounce_72h > 0) & (rng.uniform(0, 1, n) < 0.35))
        | (np.isin(comp_fam, ["back", "fall", "flank"]) & (rng.uniform(0, 1, n) < 0.22))
        | (is_minor & (rng.uniform(0, 1, n) < 0.30))
    ).astype(int)

    shock_idx = hr / np.maximum(50.0, sbp)

    # True structural log-odds of acute upgrade / 72h clinical decompensation (pi ~ 10.5%)
    eta = (
        -4.35
        - 1.35 * is_minor.astype(float)
        + 0.020 * np.maximum(0.0, age - 50.0)
        + 2.85 * (sbp < 90).astype(float)
        + 2.25 * (spo2 < 91).astype(float)
        + 0.95 * (shock_idx >= 0.95).astype(float)
        + 1.10 * ((delta_hr >= 5) | (delta_sbp <= -8)).astype(float)
        + 2.40 * gcs_drop
        + 3.10 * ekg_ste
        + 2.50 * missing_door_eval
        + 2.65 * high_risk_mech
        # Conditional Stage-2 dossier / HIE / dispense / lab mechanisms:
        + 1.35 * hie_bounce_72h
        + 1.15 * hie_prior_icu
        + 1.45
        * undisc_anticoag
        * np.isin(comp_fam, ["fall", "back", "flank", "focal_neuro", "dizzy_fatigue"]).astype(float)
        + 0.85 * undisc_anticoag
        + 1.05 * hypo_med * np.isin(comp_fam, ["fall", "dizzy_fatigue", "syncope"]).astype(float)
        + 1.25 * immuno_med
        + 1.65 * unrev_culture
        + 1.50 * bacteremia
        + 1.55 * (true_hgb < 8.8).astype(float)
        + 0.95 * (true_egfr < 35.0).astype(float)
        + 1.45 * aaa_or_ef
        + 0.95 * cirrhosis_coag
        + 0.65 * symp_prog
    )
    y = rng.binomial(1, expit(eta)).astype(int)

    X_base = pd.DataFrame(
        {
            "age_years": age,
            "sex": sex,
            "arrival_mode": arrival_mode,
            "charted_esi_num": charted_esi,
            "complaint_family": comp_fam,
            "hr": hr,
            "sbp": sbp,
            "dbp": dbp,
            "rr": rr,
            "spo2": spo2,
            "temp_c": temp_c,
            "shock_index": shock_idx,
            "delta_hr": delta_hr,
            "delta_sbp": delta_sbp,
            "gcs_drop": gcs_drop,
            "ekg_ste": ekg_ste,
            "ekg_missing_indicated": ekg_missing,
            "missing_door_eval": missing_door_eval,
            "high_risk_mech_surface": high_risk_mech,
            "minimizing_framing_flag": minimizing_flag,
        }
    )

    z_dict: dict[str, Any] = {
        "hie_bounceback_72h": hie_bounce_obs,
        "hie_ed_visits_90d": hie_visits_obs,
        "hie_prior_icu_cpr_intub": hie_prior_icu,
        "undisclosed_anticoag_fill": undisc_anticoag,
        "high_risk_hypoglycemic_fill": hypo_med,
        "immunosuppressant_active_fill": immuno_med,
        "unreviewed_resistant_culture": unrev_culture,
        "recent_bacteremia_flag": bacteremia,
        "recent_hgb_g_dl": obs_hgb,
        "recent_egfr": obs_egfr,
        "recent_wbc": obs_wbc,
        "known_aaa_or_low_ef": aaa_or_ef,
        "active_mdro_isolation": mdro_iso,
        "lives_alone_frail": lives_alone_frail,
        "cirrhosis_coagulopathy": cirrhosis_coag,
        "confirmed_symptom_progression": symp_prog,
        "confirmed_med_adherence_gap": med_gap,
    }
    candidates = list(ED_STAGE2_CHANNELS)

    if include_stress_channels:
        # 2 pure-noise null channels, 2 Stage-2 collinear twins, 2 baseline echoes
        for k in (1, 2):
            z_dict[f"null_gauss_{k}"] = rng.normal(0.0, 1.0, n).astype(np.float32)
            z_dict[f"null_heavy_{k}"] = rng.standard_t(df=4.0, size=n).astype(np.float32)
            candidates.append(
                Stage2ChannelSpec(
                    channel_id=f"stress_null_{k}",
                    display_name=f"Pure-noise null feed #{k}",
                    columns=(f"null_gauss_{k}", f"null_heavy_{k}"),
                    acquisition_cost=1.0,
                    per_patient_query_cost=0.03,
                    mechanism_tag="pure_noise_null",
                )
            )

        # Stage-2 redundant degraded vendor twins of HIE and Dispense
        b_clean = np.nan_to_num(hie_bounce_obs, nan=0.0)
        z_dict["twin_hie_a"] = (b_clean + rng.normal(0.0, 0.35, n)).astype(np.float32)
        z_dict["twin_hie_b"] = (hie_prior_icu + rng.normal(0.0, 0.35, n)).astype(np.float32)
        candidates.append(
            Stage2ChannelSpec(
                channel_id="stress_twin_of_hie",
                display_name="Collinear duplicate HIE vendor feed",
                columns=("twin_hie_a", "twin_hie_b"),
                acquisition_cost=1.20,
                per_patient_query_cost=0.04,
                mechanism_tag="stage2_twin",
            )
        )
        z_dict["twin_disp_a"] = (undisc_anticoag + rng.normal(0.0, 0.35, n)).astype(np.float32)
        z_dict["twin_disp_b"] = (immuno_med + rng.normal(0.0, 0.35, n)).astype(np.float32)
        candidates.append(
            Stage2ChannelSpec(
                channel_id="stress_twin_of_dispense",
                display_name="Collinear duplicate pharmacy aggregator",
                columns=("twin_disp_a", "twin_disp_b"),
                acquisition_cost=1.10,
                per_patient_query_cost=0.03,
                mechanism_tag="stage2_twin",
            )
        )

        # Baseline echoes of vitals and age
        age_z = (age - np.mean(age)) / max(1e-6, np.std(age))
        si_z = (shock_idx - np.mean(shock_idx)) / max(1e-6, np.std(shock_idx))
        z_dict["echo_vitals_lin"] = (0.85 * si_z + 0.45 * age_z + rng.normal(0, 0.20, n)).astype(
            np.float32
        )
        z_dict["echo_vitals_quad"] = (0.60 * (si_z**2) + rng.normal(0, 0.25, n)).astype(np.float32)
        candidates.append(
            Stage2ChannelSpec(
                channel_id="stress_echo_linear",
                display_name="Baseline vitals linear echo",
                columns=("echo_vitals_lin",),
                acquisition_cost=0.90,
                per_patient_query_cost=0.02,
                mechanism_tag="baseline_echo",
            )
        )
        candidates.append(
            Stage2ChannelSpec(
                channel_id="stress_echo_nonlinear",
                display_name="Baseline shock-index quadratic echo",
                columns=("echo_vitals_quad",),
                acquisition_cost=0.90,
                per_patient_query_cost=0.02,
                mechanism_tag="baseline_echo",
            )
        )

    return EDCalibrationCohort(
        X_base=X_base,
        Z_pool=pd.DataFrame(z_dict),
        y=y,
        patient_ids=patient_ids,
        candidates=candidates,
        acquisition_budget=3.25,
    )


def fit_calibrated_gbdt(
    X_train: pd.DataFrame,
    y_train: np.ndarray,
    X_cal: pd.DataFrame,
    y_cal: np.ndarray,
    *,
    semantic_types: dict[str, SemanticType] | None = None,
    seed: int = 42,
) -> tuple[TabularPreprocessor, lgb.LGBMClassifier, PlattLogitCalibrator]:
    """Fit leak-free TabularPreprocessor + stabilized GBDT on D_train and Platt-calibrate on D_cal."""
    prep = TabularPreprocessor(semantic_types=semantic_types).fit(X_train)
    X_tr = prep.transform(X_train)
    X_ca = prep.transform(X_cal)
    clf = make_stabilized_lgbm(seed=seed)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        clf.fit(X_tr, np.asarray(y_train, dtype=int))
    raw_ca = np.asarray(clf.predict_proba(X_ca)[:, 1], dtype=float)
    cal = PlattLogitCalibrator().fit(raw_ca, y_cal)
    return prep, clf, cal

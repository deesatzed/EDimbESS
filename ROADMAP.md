# `EDimbESS` Two-Track Project Roadmap

## Active Track (Current Build) — Statistical Safety Engine for ED Triage · PITM XR
Integrate validated statistical guarantees from `imboreceipt` (`v1.0-preprints-sealed`) directly into the Provider-in-the-Middle (PITM) ED Triage XR application without cluttering the Clinical Daylight decision surface or weakening deterministic sentinels:

- [x] **Strict Upstream Isolation:** Keep `/Users/o2satz/antigravity/imboreceipt` and `/Users/o2satz/Downloads/ED_PITM_TRIAGE` 100% untouched read-only.
- [x] **Layer 0 — Hard Clinical Guardrails (100% Deterministic, Never Statistically Filtered):**
  - 6 Always-Surface Must-Know Clinical Sentinels (`IMMUNOCOMPROMISED`, `ANTICOAGULATED_COAGULOPATHIC`, `UNEXPLAINED_ABNORMAL_VS`, `HIGH_RISK_MECHANISM_ACTIVITY`, `HIGH_RISK_PMH`, `BOUNCEBACK_FREQUENT_ED`).
  - Missing arrival `Trauma / Neuro / D2B` upgrade triggers, negation-safe `affirmedText`, EKG strictly as a status chip, and one-way Safety Auditor ratchet.
- [x] **Layer 1 — Conformal False-Omission Safe-Gate & `CfBH` Alert Control (`streaming_safety.py`):**
  - Natural-prevalence Platt-calibrated Newton-stabilized LightGBM (`max_delta_step=1.0`, zero SMOTE distortion) fit on leak-free SHA-256 4-way splits (`train 55% / cal 15% / val 15% / test 15%`).
  - Conformal False-Omission Rate (`FOR`) & missed-escalation nonconformity certification gating 1-pinch **`Accept routine`** (`CERTIFIED_SAFE_ROUTINE` vs. `AMBIGUOUS_HOLD_VOI` vs. `SENTINEL_ESCALATION`).
  - Tie-smoothed Jin & Candès (2023) Conformal Benjamini–Hochberg (`CfBH`) controlling false discovery rate on probabilistic queue alerts vs. high-FDP Mondrian `{1}` singletons.
- [x] **Layer 2 — Decision-Flip Streaming VOI Router (`streaming_safety.py`):**
  - 9-node Gauss–Hermite quadrature $\widehat{\operatorname{VOI}}_{\mathcal{K}_d}(x) > c_{\text{query}}$ across the 5 PITM decision contracts (`triage_status`, `eval_upgrade`, `space_priority`, `wr_orders`, `wr_meds`).
  - Proves & enforces **High Risk $\ne$ High VOI**: bypasses slow external queries when a patient already clears $\tau^*$ (`VOI = 0`, escalate immediately), concentrates HIE / Dispense / 7-Agent Swarm queries on boundary-ambiguous ESI-3s, and skips unnecessary queries on certified low-risk arrivals.
- [x] **Layer 3 — Sequential Conditional Gain Orthogonal Delta Selector (`orthogonal_delta.py`):**
  - Applies forward stepwise conditioning $\hat{G}_{m \mid X_{\text{base}}, \mathcal{S}_{t-1}}$ + LCB stopping to `findings[:3]`, `ask_patient[:3]`, and `SPOT DR` (`Serious · Probable · Outside · Treatment · Dispo · Risks`), suppressing baseline triage echoes and Stage-2 collinear twins.
  - Computes framing-blind **Base-Rate Evidence Deficit** $D_q(x)$ to guard against subjective label anchoring and drive the 3D XR spatial ring meaningfully.
- [x] **Layer 4 — Constrained Neyman Two-Phase Shadow Ledger Governance (`two_phase.py`):**
  - Water-filling constrained Neyman allocation (`10%–15%` chart-audit pilot), Kish Effective Sample Size (`ESS`), exact $\widehat{V}_{\text{I}} + \widehat{V}_{\text{II}}$ two-phase variance, Holm LCB channel discovery, Schuirmann TOST non-inferiority (`fast deterministic` vs. `7-agent swarm`), and subgroup/framing-blind calibration slope audits over `decisions.jsonl`.
- [x] **Layer 5 — Hybrid Clinical Daylight Desk + XR Blocks (`xrblocks@0.21.1`) Frontend (`web/`):**
  - Clean Operate-mode clinical review surface + interactive 3D WebXR spatial stage + live Statistical Safety & Two-Phase Audit drawer.

---

## Checkmarked Future Track (Parked for Phase 2) — Companion Academic Preprint / Paper
- [ ] **Target Scope (Deferred until Statistical Safety Engine is finalized):**
  - Multi-split (`R = 20`) empirical benchmark on de-identified ED triage cohorts (e.g., MIMIC-IV-ED + multi-channel HIE/dispense/micro replay + PITM shadow ledger simulation).
  - Formal theorem & empirical proof of **Dual Conformal PITM Gatekeeping** (Conformal False-Omission Safe-Pass + `CfBH` Alert Precision + Decision-Flip Streaming VOI Net Benefit) and **Constrained Neyman Shadow Chart-Review Sizing (`EDimbESS`)**.
  - Full compliance pass through `preprint-readiness-gate` (`audit -> remediation -> preprint-blind-auditor -> reverify`) when initiated.

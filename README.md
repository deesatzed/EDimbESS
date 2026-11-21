# `EDimbESS` — Statistical Safety Engine for ED Triage · PITM XR

**Provider-in-the-Middle Emergency Department Triage (`xrblocks@0.21.1`) Powered by Conformal Safe-Gate Certification, Decision-Flip Streaming VOI, Sequential Orthogonal Delta Selection, and Constrained Neyman Two-Phase Shadow Auditing**

> **Research and teaching prototype only.** Synthetic packets (`syn-ED-###`) only. Not for clinical care, patient routing, paging, or EHR write-back.

## Architecture Overview

`EDimbESS` bridges the statistical guarantees of `imboreceipt` with the clinical workflow of **ED Triage · PITM XR**:

1. **`edimbess.contracts`** — Operational decision contracts $\mathcal{K}_d = (C_{\text{FP},d}, C_{\text{FN},d}, q_{\text{target},d}, c_{\text{query}})$ for all 5 PITM decisions (`triage_status`, `eval_upgrade`, `space_priority`, `wr_orders`, `wr_meds`), framing-blind **Base-Rate Evidence Deficit** $D_q(x)$, Wilson score CIs, and **Kish Effective Sample Size (`ESS`)**.
2. **`edimbess.calibration_cohort`** — Leak-free SHA-256 4-way split (`train 55% / cal 15% / val 15% / test 15%`) synthetic ED triage calibration generator ($N = 6{,}000$ encounters across 14 complaint families, $M = 8$ acquirable Stage-2 channels + null/twin/echo stress channels) sharing an exact feature mapping with live `syn-ED-###` packets, paired with Newton-stabilized (`max_delta_step=1.0`), natural-prevalence Platt-calibrated LightGBM models.
3. **`edimbess.streaming_safety`** — Dual conformal gatekeeping and autonomous Value-of-Information routing:
   - **Conformal False-Omission Safe-Gate:** Certifies `CERTIFIED_SAFE_ROUTINE` (unlocking 1-pinch `Accept routine`) vs. `AMBIGUOUS_HOLD_VOI` vs. `SENTINEL_ESCALATION` at target missed-escalation bound $\alpha_{\text{FOR}}$.
   - **9-Node Gauss–Hermite Decision-Flip VOI Router:** Computes $\widehat{\operatorname{VOI}}_{\mathcal{K}_d}(x) > c_{\text{query}}$ across HIE, Pharmacy Dispense/PDMP, Pending Microbiology, Baseline Labs/Imaging, and the 7-Agent Swarm—enforcing **High Risk $\ne$ High VOI** ($\operatorname{VOI} = 0$ when an arrival already mandates immediate resuscitation).
   - **Tie-Smoothed Jin & Candès (2023) `CfBH` Conformal FDR Selection:** Controls false discovery proportions on probabilistic queue alerts where unverified Mondrian `{1}` singletons inflate false alarms.
4. **`edimbess.orthogonal_delta`** — Forward stepwise conditional gain selector ($\hat{G}_{m \mid X_{\text{base}}, \mathcal{S}_{t-1}}$ + LCB stopping) across 9 pathophysiological axes, eliminating baseline triage echoes and Stage-2 collinear twins in `findings[:3]`, `ask_patient[:3]`, and `SPOT DR`.
5. **`edimbess.two_phase`** — Water-filling constrained Neyman two-phase pilot allocator (`10%–15%` chart-audit fraction even when $\pi > \rho$), untruncated Horvitz–Thompson sequential conditional log-likelihood gain, exact $\widehat{V}_{\text{I}} + \widehat{V}_{\text{II}}$ variance decomposition, Holm step-down LCB stopping, Nadeau–Bengio / Schuirmann TOST non-inferiority (`deterministic fast path` vs. `7-agent swarm`), and subgroup/framing-blind calibration audits over `data/decisions.jsonl`.
6. **`edimbess.server` & `web/`** — Zero-dependency HTTP bridge + Clinical Daylight Operate-mode desk + WebXR 3D Spatial Gate (`xrblocks@0.21.1`, `three@0.184.0`).

## Quickstart

```bash
# Run full unit & statistical verification suite
.venv/bin/pytest -q
.venv/bin/ruff check src tests

# Run standalone CLI Two-Phase + Streaming Safety Engine audit receipt
.venv/bin/edimbess-audit

# Launch ED Triage · PITM XR + Statistical Safety Engine server (default port 8092)
.venv/bin/edimbess-server --port 8092
```

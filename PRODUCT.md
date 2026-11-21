# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

Emergency department physicians and advanced practice providers (APPs) sitting in the Provider-in-the-Middle (PITM) review seat immediately after nursing triage intake—reviewing door vitals, chief complaint, and nursing summary alongside a PHI-redacted longitudinal chart dossier before patients are roomed or wait on the board.

## Product Purpose

`EDimbESS` augments ED Triage · PITM XR (`xrblocks@0.21.1`) with a calibrated Statistical Safety Engine so clinicians can clear truly routine encounters in a single pinch while catching hidden physiological deterioration, unreviewed microbiology/labs, high-risk mechanisms, and medication conflicts without alert fatigue.

## Positioning

Unlike uncalibrated triage risk scores or unconstrained LLM chart summaries, `EDimbESS` couples deterministic clinical sentinels and a 7-agent chart-delta contract with finite-sample conformal false-omission safe-gate certification, decision-flip Bayes Value-of-Information (`VOI_K(x) > c_query`) channel routing, sequential conditional information gain (`G_{m|S}`) twin/echo suppression, and constrained Neyman two-phase shadow ledger auditing.

## Operating Context

- **Research and demo only** — never for clinical care, patient routing, paging, or EHR write-back.
- **PHI-redacted boundary:** Synthetic packets (`syn-ED-###`) only; names, dates of birth, MRNs, addresses, phone numbers, emails, and SSNs are rejected at `/api/intake`.
- **Workflow landmark:** Post-nurse triage. One active **Now Gate** holds the primary action focus (`Accept`, `Change` with mandatory override reason, or `Decline` across the five PITM decisions).
- **Ambient environment:** Bright clinical daylight desk (cool hospital chart slate `#F2F5F8`, white cards `#FFFFFF`, high-contrast dark ink `#18222F`) plus optional WebXR 3D spatial view (`xrblocks@0.21.1` + `three@0.184.0`).

## Capabilities and Constraints

- **Exactly Five PITM Decisions:**
  1. `triage_status` (`ESI-1` through `ESI-5`),
  2. `eval_upgrade` (`Trauma`, `Neuro`, `D2B` — proposed **only** when marked `missing` at arrival),
  3. `space_priority` (`immediate`, `next_bed`, `bed`, `hallway`, `fast_track`, `waiting_room`, `defer`),
  4. `wr_orders` (standing waiting-room order sets or add-ons — **EKG is strictly a status chip and never an ordered item**),
  5. `wr_meds` (standing policy medications with explicit hepatic, renal/CKD/transplant, bleeding/anticoagulant, chest/epigastric, and surgical NPO holds).
- **Six Non-Waivable Deterministic Must-Know Sentinels:** Immunocompromised, Anticoagulated/Coagulopathic, Unexplained/Repeated Abnormal Vitals, High-Risk Mechanisms/Activities (IVDU, fall from height, high-speed ocular projectile, high-pressure finger injection, high-speed MVC), High-Risk PMH (transplant even if removed, CRI/CKD, EF < 35%, prior CPR, prior intubation, AAA), and 72h Bouncebacks / 3+ Recent ED Visits.
- **Statistical Safety Engine Invariants:**
  - Deterministic sentinels and missing door evaluations are hard ratchets and are never filtered by statistical thresholds.
  - All learned risk models use Newton-stabilized (`max_delta_step=1.0`), natural-prevalence Platt-calibrated probabilities (never SMOTE or class-reweighted uncalibrated scores).
  - Center Now Gate clinical copy remains free of raw statistical jargon (no p-values or Horvitz–Thompson weights in clinical headlines or `queue_note`).
  - Append-only SHA-256 hash-chained decision ledger (`data/decisions.jsonl`).

## Brand Commitments

- **Satz-EM PITM & `SPOT DR` Cognitive Discipline:** Preserves the six-cell `SPOT DR` checklist (`Serious`, `Probable`, `Outside`, `Treatment`, `Disposition`, `Risks / Bias`) and negation-safe symptom parsing (`affirmedText` keeps `"not breathing"` while stripping negated symptom lists).

## Evidence on Hand

- 22 synthetic ED triage packets (`syn-ED-001` through `syn-ED-022`, including E9001 apixaban/ESBL/glipizide multi-threat encounter).
- Validated statistical methodology from `imboreceipt` (`v1.0-preprints-sealed`): constrained Neyman water-filling two-phase allocation, untruncated Horvitz–Thompson sequential conditional log-likelihood gain, exact `V_I + V_II` variance decomposition, 9-node Gauss–Hermite streaming VOI, and tie-smoothed Jin & Candès (2023) `CfBH` conformal FDR control.
- Explicit absence: All triage calibration cohorts and encounter packets in this repository are synthetic research constructs, not prospective hospital EHR deployments.

## Product Principles

1. **Clinicians Decide; Deterministic Guardrails Never Blink:** Models propose and quantify uncertainty, hard sentinels always surface, and only the clinician logs a decision.
2. **High Risk Is Not High Value of Information:** Never delay obvious resuscitations with slow external queries (`VOI = 0` above threshold); spend query latency and bedside questions strictly where new evidence can flip one of the five PITM decisions.
3. **Earn Fast-Pass Automation Through Finite-Sample Safety:** Unlock 1-pinch `Accept routine` only when deterministic sentinels are clear, critical feeds are verified, and conformal false-omission certification holds on natural-prevalence calibration data.
4. **Orthogonal Cognitive Slots Over Redundant Volume:** Under strict three-finding and three-question caps, suppress triage-board echoes and collinear Stage-2 twins so every slot covers an independent clinical failure mode.

## Accessibility & Inclusion

- WCAG AA contrast (`>= 4.5:1` body text, `>= 3:1` large text) on warm daylight surfaces (`prefers-color-scheme: light` clinical posture), full keyboard focus rings, tabular numerals (`tabular-nums`) for vitals and wait times, and explicit subgroup/framing-blind calibration auditing so stigmatized labels (`intoxicated`, `frequent visits`, `unsheltered`, `substance use`) never lower acuity.

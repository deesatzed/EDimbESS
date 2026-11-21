# Governance-Aware Acquisition: Directions, Theorems, Experiments

Nov 20, 2026 · @Wayne Satz

## Bottom line

Do not widen the current paper. Fix it, submit it, and put the new ideas into two new papers that cite it.

The current paper (two-phase pilot discovery and streaming selective acquisition, imboreceipt) has two issues a reviewer can find: a centering error in the Eq. 4 value-of-information calculation, and a likely tie artifact in Table 4. Both are fixable in one rerun. Adding the new ideas would delay submission and blur a clean contribution.

1. **Track 0, gates everything:** pre-submission repair of the current paper. Audit, rerun Table 4, reword Appendix A.
2. **Track 1, the next paper:** policy-aware channel admission. What a channel is worth under the router that will actually use it, and whether its profitable cases can be recognized before purchase. Reuses the package, datasets and Theorem 1 Part 1.
3. **Track 2, the broader paper:** joint governance and falsifiable decision boundaries on replayable systems. This is where the original first/second/third-principles idea becomes a testable claim.

Track 1 can start while Track 0 is under review. Track 2 shares Track 1's pair-discovery machinery, so it follows Track 1.

## Analysis of the final exchange

The other model's last round is correct on every point I checked, and it overturns one claim I made. I recomputed its value-of-information table independently: Scania costs (C\_FN = 50, C\_FP = 1, t = 1/51 = 0.0196), σ = 1, nine-node quadrature.

| p₁ | Side of t | Eq. 4 VOI before clipping | Mean-corrected VOI | Centering offset μ |
| --- | --- | --- | --- | --- |
| 0.010 | below | −0.036 | 0.087 | −0.484 |
| 0.015 | below | 0.093 | 0.229 | −0.476 |
| 0.020 | above | 0.211 | 0.381 | −0.468 |
| 0.100 | above | 0.051 | 0.028 | −0.367 |

**Where it was right and I was wrong.** I said the bias overstates VOI below the threshold. It does the opposite here: the upward mean shift raises the expected miss cost on cases whose action never changes, which swamps the extra flips. The error's sign depends on position and dispersion, so only a fix plus a held-out comparison settles it.

**Its most important new finding: a probable tie artifact in Table 4.** Eq. 4 clips negative VOI to zero. If the 80th percentile of clipped VOI is zero, the rule R(x) ≥ γ̂ queries every arrival. Table 4 shows nominal 20% VOI routing exactly equal to full querying on Scania (14.98 vs 14.98) and NeurIPS BAF (2.44 vs 2.44). The CfBH rows match too: recall 91.3% on Scania and 16.4% on BAF in both rows.

**What it missed.** If the tie explanation holds, a results claim is wrong, not just a number. Section 3.4 says VOI-20% on Scania matches 100% Stage-2 recall "at one-fifth the query volume." It may in fact have queried every truck. That sentence must be checked before submission.

**Accepted from it.**

- Policy value difference Δm(S) = V(S ∪ {m}) − V(S) generalizes my E\[(VOI − c)₊\]. It also handles bundles, substitution and hard budgets.
- Its pre-purchase identifiability experiment is the cleanest new test in the whole brainstorm.
- Pair discovery needs a power strategy, not only multiplicity control. Filtering on single-channel gains removes exactly the XOR pairs being sought.

**One addition of mine.** The identifiability idea has a precedent it did not name: policy learning and uplift targeting (for example Athey and Wager, 2021), where a treatment's value depends on recognizing who benefits beforehand. Cite it. The new part is applying it to information purchase under a two-phase pilot.

## Track 0: pre-submission fixes to the current paper

Six items, all checkable with one instrumented rerun. None changes the paper's scope.

- [ ] **Audit the existing run first (E0).** For every cohort and split, log intended vs realized query rate, the fraction of raw VOI below zero, the fraction exactly at the routing cutoff, and σ̂Δ by bin. This tells a centering effect apart from a tie effect before anything is changed.
- [ ] **Fix Eq. 4 centering (E1).** Solve for the logit offset μ(p₁, σ) that makes the quadrature mean equal p₁, at each p₁ or from an interpolated lookup table, not one offset per bin. Use the same nine nodes, so the computed VOI is non-negative exactly (Proposition 2). The \[·\]₊ clip then never binds; keep it only as an assertion.
- [ ] **Fix the routing tie rule.** For fixed-budget arms, use a prespecified randomized tie-break that hits the target rate in expectation on the calibration set. Report the realized test rate beside every Table 4 row, especially on chronological splits. Keep the autonomous VOI > c rule as its own row.
- [ ] **Rerun Table 4 with both routers on identical splits (E2),** and decompose Net Benefit into beneficial flips, harmful flips, unchanged actions and query cost. Report how much the two routers disagree on whom to query.
- [ ] **Re-verify text claims that depend on Table 4.** In particular "matching 100% Stage-2 recall at one-fifth the query volume" (Scania) and the parallel BAF statement. Rewrite if the tie explanation holds.
- [ ] **Reword Appendix A, Theorem 1 Part 2.** Present Gibbs' inequality as the oracle fact. State that the fitted-versus-fitted bound E\[ψ\] ≤ 0 is an assumption (capacity-matched null drift plus studentization), and keep the scope at an all-null remaining step.

Optional, if space allows: report σ̂Δ by bin in an appendix table, so readers can judge how far a logit-normal update model is from the fitted Stage-2 behavior.

A result worth keeping either way: if the uncorrected router still wins on held-out loss, report it honestly as a misspecified heuristic that routes well, with the decomposition showing why.

## Track 1: policy-aware channel admission (next paper)

**Claim to earn:** a channel's admission value is set by the cases the router can recognize before purchase, not by its population-average information gain, and this value can be estimated from a budgeted two-phase pilot.

**Why it follows from the current paper.** Stage A values a channel as if it were queried for every arrival. Stage B queries 7–73% of arrivals. The two stages answer different questions; this paper makes them answer the same one.

**Core quantity.** Δm(S) = V(S ∪ {m}) − V(S), where V(S) is the expected net utility per arrival of the frozen routing policy learned with channel set S. It includes decision loss, query charges, and integration cost amortized over a stated deployment volume. Both policies are learned on development data and compared on the same held-out arrivals.

**Three decisions the paper separates.**

| Decision | Question | Evaluated by |
| --- | --- | --- |
| Admit | Is the channel worth integrating, given S? | Δm(S) under the deployed router, net of fixed cost |
| Query | Is it worth buying for this arrival? | Mean-corrected VOI minus query cost |
| Act | Given the answer, which action? | Updated posterior vs the cost-contract threshold |

**Components.**

1. Proposition 3 (value of pre-purchase identifiability) gives the closed-form ground truth for the headline experiment, E5.
2. Corollary 1: Theorem 1 Part 1 applies unchanged to per-record policy-value differences, once policies are frozen before pilot evaluation.
3. Incremental Murphy diagrams per channel show where on the threshold scale the value comes from, and whether two channels swap rank as the cost contract changes.
4. A threshold-weighted proper score is reported only as a prespecified summary of the curve. Its null control is shown by simulation, not inherited from Theorem 1.
5. Pair discovery with a discovery/confirmation split (E10). This also answers the current paper's stated XOR limitation.
6. Skipped-case certification: a binomial upper bound on the decision-flip rate among skipped arrivals, plus forgone cost, on a fresh audit set.

**Novelty boundary, stated plainly.** Decision curve analysis, Murphy diagrams, active feature acquisition, non-myopic VOI and policy learning all exist. The claimed contribution is narrower: estimating incremental admission value from a two-phase pilot, when profitable use depends on what is knowable before purchase.

**Kill criterion.** If Δm(S) ranks channels the same as Stage-A gain on all six benchmarks and in E5, the distinction has no practical consequence and the paper should not be written.

## Track 2: joint governance and falsifiable boundaries (broader paper)

**Claim to earn:** given what was already known, observation A made observation B worth buying. The method states the range of conditions where that holds, and a held-out experiment could prove it wrong.

**Where the original three-tier idea lands.** The tiers become a vocabulary, not the claim:

- **First order, the contract:** costs and constraints fix the action threshold.
- **Second order, the mechanism:** evidence moves the estimated state.
- **Third order, governance:** where state meets threshold decides which rule governs, and what a change is worth.

Changing the contract can reverse which observation deserves priority, with the evidence and both models held fixed.

**Evidence roles become computable** for each evidence–rule pair, not labels on observations:

- **Activation:** governance changes while credence does not.
- **Confirmation or disconfirmation:** credence moves.
- **Boundary violation:** the action was right but the predicted boundary was wrong. That triggers a mechanism-level revision with the principles left intact.

**Explanations must commit to something testable.** A valid explanation names the boundary, for example "break-even bandwidth is 75 MB/s at the current processing cost." Credit for a reversal must not depend on the order observations arrived.

**Testbeds, in order of evidential strength.**

1. **Replayable systems with a known formula:** compressing a file before sending vs sending it raw; indexed lookup vs full scan. Both actions run on the same case, so counterfactuals are true and the formula can be withheld and recovered. Varying a mechanism (streaming on or off) tests whether the predicted boundary moves.
2. **A replayable system without a known formula,** such as document search under a reading budget, to test whether the method travels.
3. **The six benchmarks from the current paper,** which are replayable for the acquisition decision because every channel is observed on every row. Here the defensible claim is VOI prediction, not mechanism discovery.

**Metrics, always reported together:** total cost including probes; accuracy of predicted reversals; and the fraction of cases where the method makes any testable claim. That last one stops a method from looking good by explaining only easy cases.

**Freeze before testing.** The learned rule, its predicted boundary and the measurement policy are frozen before held-out workloads and machines. Independent workloads are counted separately from timing repeats.

**Kill criterion.** If greedy per-channel selection with a standard two-step acquisition policy matches the method on reversal-prediction accuracy at equal cost, there is no paper.

## Theorems and propositions

Six formal results, labeled honestly. Two are new as stated (Propositions 2 and 3), one is a direct corollary of the current paper, and three are known results the new papers should cite rather than claim.

| # | Result | Status | Used in |
| --- | --- | --- | --- |
| P1 | VOI equals threshold distance on flipped cases | Known (elementary) | Tracks 0, 1 |
| P2 | Mean-corrected logit update: unique offset, VOI ≥ 0 exactly | New as stated, easy proof | Track 0 |
| P3 | Value of pre-purchase identifiability | New in this setting, easy proof | Track 1, E5 |
| C1 | Theorem 1 Part 1 applies to policy-value differences | Direct corollary | Track 1 |
| P4 | Proper scores are mixtures of threshold losses | Known (Schervish 1989) | Track 1 |
| P5 | VOI is not submodular; pure-XOR pairs escape single-channel screening | Known (Krause and Guestrin) | Tracks 1, 2 |

### P1. VOI as threshold distance (known)

Let t = C\_FP / (C\_FP + C\_FN), and let aⱼ = 1{pⱼ ≥ t}. If the update is coherent, E\[p₂ | W\] = p₁, then:

```latex
\mathrm{VOI}(W) = (C_{FP}+C_{FN})\,\mathbb{E}\big[\,|p_2 - t|\;\mathbf{1}\{a_1 \neq a_2\} \,\big|\, W\big] \;\ge\; 0
```

Proof sketch: the loss of a fixed action is linear in p, so under coherence the expected loss of keeping a₁ equals the current Bayes loss. Switching to a₂ saves exactly (C\_FP + C\_FN)|p₂ − t| on flipped cases and nothing elsewhere.

Consequence: a negative pre-clipping VOI is proof the update model is incoherent. That turns the \[·\]₊ clip in Eq. 4 into a diagnostic.

### P2. Mean-corrected logit-normal update (new as stated)

For quadrature nodes ξᵣ with weights ωᵣ, define:

```latex
f(\mu) = \sum_{r} \omega_r\,\mathrm{expit}\big(\mathrm{logit}(p_1) + \mu + \sigma \xi_r\big)
```

f is continuous and strictly increasing in μ, from 0 to 1, so f(μ\*) = p₁ has exactly one root. With p₂,ᵣ built from μ\*, the discrete update preserves the mean. The Bayes loss is concave, so Jensen's inequality on that same discrete measure gives VOI ≥ 0 exactly, not only asymptotically. The clip never binds.

At Scania costs with σ = 1, μ\* runs from about −0.48 at p₁ = 0.01 to about −0.37 at p₁ = 0.10. It must be solved per p₁, not per bin.

### P3. Value of pre-purchase identifiability (new in this setting)

Let U be what the router knows before buying channel m, let G be the decision-loss reduction that buying m produces, and let c be the per-query price. With γ(U) = E\[G | U\], the optimal router's value per arrival is:

```latex
V_m(U) = \mathbb{E}\big[(\gamma(U) - c)_{+}\big]
```

- **Monotonicity:** (·)₊ is convex, so V never decreases when U is refined (conditional Jensen). Better pre-purchase cues can only raise a channel's admission value.
- **Maximum admissible query price:** the essential supremum of γ(U), less any amortized integration cost.
- **Binary special case,** which is the ground truth for E5: a fraction π\_B of arrivals gain g, the rest gain nothing. With a perfect cue, V = π\_B(g − c)₊. With no cue, V = (π\_B g − c)₊. The channel stays admissible up to price g with the cue but only up to π\_B g without it. The gap is largest when π\_B g < c < g.

Precedent to cite: the value of targeting in policy learning and uplift modeling. The setting here differs because the thing purchased is information, not treatment.

### C1. Policy-value differences inherit Theorem 1 Part 1 (corollary)

Theorem 1 Part 1 needs only fixed, Phase-1-measurable per-record score differences with finite second moments. Set ψᵢ = uᵢ(π\_{S∪{m}}) − uᵢ(π\_S), the realized net utilities of two frozen policies. The Horvitz–Thompson estimate of Δm(S) is then design-unbiased, with the same V\_I + V\_II variance.

Condition: both policies are learned on data disjoint from the evaluating pilot. If they are cross-fitted on the pilot instead, the paper's existing caveats apply, including the 78.8% coverage gap.

### P4. Proper scores as threshold mixtures (known)

Every binary proper score is a weighted mixture of elementary threshold-specific decision losses. A weight that is positive on all of (0, 1) gives a strictly proper score, so the oracle incremental gain of a conditionally null channel is zero.

This guarantee is oracle-level only. The fitted-versus-fitted null condition of Theorem 1 Part 2 must be shown again for any new score.

### P5. Non-submodularity and pure-XOR pairs (known)

There exist channels A and B with VOI(A) = VOI(B) = 0 but VOI(A, B) > 0. A first observation can also leave the action unchanged while raising the VOI of a second.

Remark: any screening procedure that keeps a pair only when a member passes a single-channel test has zero power against pure-XOR pairs. That is why E10 has to discover pairs directly.

## Experiment register

Fourteen experiments: three for Track 0, seven for Track 1, four for Track 2, with E10 serving both new papers. IDs E3 and E4 are unused. Each row names what would count as failure, fixed before running.

| ID | Track | Hypothesis | Design | Fails if |
| --- | --- | --- | --- | --- |
| E0 | 0 | Table 4's Scania and BAF ties come from clipped-zero VOI at the cutoff | Log realized query rate, % raw VOI < 0, % at cutoff, σ̂Δ by bin, on the existing run | Realized rate for nominal 20% is about 20% on both cohorts |
| E1 | 0 | Mean-corrected VOI is coherent and changes whom the router queries | Implement P2; rerun Stage B on identical splits | No arrivals change routing at any query rate |
| E2 | 0 | The decomposition explains why top-risk routing fails | Split Net Benefit into beneficial flips, harmful flips, unchanged actions, query cost | Top-risk loses on Diabetes without the queries landing on cases above τ\* |
| E5 | 1 | Pre-purchase identifiability changes admission value as P3 predicts | Synthetic: same π\_B, g, c; cue available vs cue withheld; sweep c | Estimated admissible price misses g (cue) or π\_B g (no cue) beyond its CI |
| E6 | 1 | Δm(S) and Stage-A gain rank channels differently on real data | All six benchmarks; rank correlation; list the disagreeing channels | Rankings agree on every cohort (the kill criterion) |
| E7 | 1 | Channels swap rank across cost contracts | Incremental Murphy diagrams per channel, conditional on S | No swaps within plausible threshold ranges |
| E8 | 1 | Under a hard budget, admitting a channel can displace better queries | Fixed total query budget; compare Δm(S) with and without the budget | Budget never changes the admission decision |
| E9 | 1 | Threshold-weighted gain keeps null control when fitted | Simulation A rerun with a weighted proper score | Null family-wise error above 5% |
| E10 | 1, 2 | Pairs can be found with useful power | Discovery sample over all pairs, then confirmation sample with Holm; vary pilot size, interaction prevalence, distractors; controls: twins, echoes, pure XOR, enabling cue | True pair reaches confirmation in under 50% of runs at realistic pilot sizes |
| E11 | 1 | Skipped cases can be certified | Frozen policy; fresh audit set; Clopper–Pearson bound on flip rate among skips, plus forgone cost | Bound too wide to act on at realistic audit sizes |
| E12 | 2 | Method recovers the compression boundary without the formula | Raw vs compressed transfer; withhold "bytes saved ÷ bandwidth > processing time"; held-out machines | Predicted break-even misses on held-out machines more than a memorizing baseline does |
| E13 | 2 | Changing the mechanism moves the boundary as predicted | Streaming on vs off (overlap of processing and transfer) | Boundary moves the other way, or not at all |
| E14 | 2 | The method travels to a domain without a known formula | Document search under a reading budget | All three metrics no better than greedy plus two-step acquisition |
| E15 | 2 | Credit for a reversal does not depend on observation order | Permute arrival order with cases and fitted inputs fixed | Attributed observation changes with order |

Discovery of pairs (E10) is the shared piece between the two new papers. Build it once.

## Side studies and parked ideas

None of these is on the critical path. Each is listed with what would revive it.

| Idea | Status | Revive when |
| --- | --- | --- |
| Order effects in reviewer judgment (QQ-equality test) | Side study; cheap pilot with LLM reviewers, human reviewers for any claim about people | You want a short standalone paper or an addition to the CRUX eval suite |
| Tier dispatch: boundary revision, Bayesian mechanism update and decision governance as three update types | Parked as framing; it became Track 2's vocabulary | A replayable domain shows boundary violations that a Bayesian model absorbs silently |
| Switching distribution family conditional on upstream state | Parked; risk of explaining a reversal after the fact | It can be triggered by a boundary monitor fixed in advance, with competing families carried in parallel |
| Heavier-tailed VOI update (Student-t) | Deferred until after P2 | Mean-corrected Gaussian still mispredicts held-out realized gain in the tails |
| Performative stability of announced rules | Parked | A deployed router whose rule is known to the people it routes |
| Posterior over priority orderings (Plackett–Luce, Mallows) | Dropped as a core claim; overlaps with probabilistic conditional-preference networks | Only as an implementation choice inside Track 2 |
| Neyman allocation keyed to the decision threshold | Open; the current dispersion proxy has no τ\* in it | Track 1 needs a pilot designed to estimate decision value rather than gain |

## Ledger of corrections

Eight claims from the brainstorm were revised. Recorded so none of them leaks into a manuscript.

| Original claim | Who made it | Corrected to | Corrected by |
| --- | --- | --- | --- |
| Posterior over principle orderings is the theoretical core | Claude | Close to prior art (conditional-preference networks); implementation detail only | The other model, round 1 |
| Governance = regret from deleting a rule | Claude | That is LOCO, and it cancels redundant twins; use sequential, conditional governance | Claude, from Proposition 1 of the paper |
| Weighted-score extension inherits Theorem 1 | Claude | Only at oracle level; the fitted null condition needs its own argument | The other model, round 3 |
| The Neyman dispersion proxy approximates boundary density | Claude | The proxy contains no τ\* | The other model, round 3 |
| 2/20 split variation is an order-dependence audit | Claude | It is resampling stability | The other model, round 3 |
| Eq. 4 bias overstates VOI below the threshold | Claude | Sign depends on position and dispersion; at Scania costs it understates VOI below t | The other model, round 4; recomputed and confirmed here |
| Family switching on reversal is a feature | Claude | Post hoc switching makes explanations unfalsifiable; switching trigger must be fixed in advance | The other model, round 2 |
| Decision-value curves are a new contribution | The other model | They are decision curve analysis and Murphy diagrams; novelty is only in conditional, budgeted use | Claude, round 3 |

Standing constraint for every manuscript from these tracks: none of this work has been validated prospectively or used in clinical production, and the text should say so where it applies.

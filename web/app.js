/**
 * EDimbESS Frontend Controller:
 * - Tab 1: Focused Action (calm, scannable key-actions view with progressive disclosure)
 * - Tab 2: Full Workbench (complete 3-column clinical + statistical desk)
 * - Tab 3: 3D Spatial XR (interactive Three.js r184 WebGL cockpit + XR Blocks / WebXR bridge)
 */

const DECISION_TITLES = {
  triage_status: "1. Triage Acuity Status",
  eval_upgrade: "2. Arrival Door Evaluation (Trauma / Neuro / D2B)",
  space_priority: "3. Treatment Space Assignment",
  wr_orders: "4. Waiting-Room Order Set",
  wr_meds: "5. Waiting-Room Policy Medications",
};

const SUPPRESSED_REASON_LABELS = {
  1: "Reason 1 · Triage echo suppressed (already explicit on door header)",
  2: "Reason 2 · Out-of-window historical item",
  3: "Reason 3 · Duplicate of active standing order",
  4: "Reason 4 · Collinear Stage-2 twin pruned via G_{m|S}",
  cap: "Cap · Below top-3 conditional gain cutoff",
};

const SAMPLE_INTAKE_PACKET = {
  packet_id: "syn-ED-099",
  encounter: {
    arrival_mode: "walk-in",
    triage_status: "ESI-3",
    chief_complaint: "Lightheadedness after standing",
    nurse_hx_summary:
      "Mild lightheadedness this afternoon, looks comfortable at triage. Denies chest pain. Denies blood thinners.",
    vitals: { hr: 98, sbp: 112, dbp: 68, rr: 16, spo2: 98, temp_c: 36.9 },
    prior_vitals: [
      { when: "18m ago (door)", hr: 90, sbp: 122, dbp: 72, rr: 16, spo2: 98, temp_c: 36.8 },
    ],
    ekg: { status: "missing", summary: null },
    evals: { trauma: "not_indicated", neuro: "not_indicated", d2b: "not_indicated" },
    wr_orders: { started: false, sets: [], addons: [] },
    wr_meds_given: [],
    arrival_minutes_ago: 21,
    age_years: 69,
    sex: "F",
    outside_hie_available: false,
  },
  dossier: {
    pmh_summary: "Paroxysmal atrial fibrillation.",
    surgical_hx_summary: "None recorded.",
    social_hx_summary: "Lives alone.",
    last_ed_visits: [{ when: "2 days ago", summary: "Fatigue, discharged." }],
    last_admissions: [],
    recent_labs: [{ when: "2 days ago", test: "Hemoglobin", value: "9.4 g/dL" }],
    recent_di: [],
    ems_report_summary: null,
    current_meds: ["Apixaban", "Metoprolol"],
  },
};

const state = {
  viewMode: "focused", // "focused" | "workbench" | "spatial"
  capacity: {
    bedsOpen: 3,
    bedsTotal: 18,
    hallwayOpen: 2,
    hallwayTotal: 8,
    fastTrackOpen: 2,
    fastTrackTotal: 4,
    waitboard: 14,
  },
  packets: [],
  selectedPacketId: null,
  activeFilter: "ALL",
  reviewedPacketIds: new Set(),
  draftDecisions: {},
  ledgerCount: 0,
  spatialStage: null,
};

function esc(val) {
  return String(val ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

function pct(v) {
  return `${((Number(v) || 0) * 100).toFixed(1)}%`;
}

function iconSvg(kind) {
  if (kind === "check") {
    return `<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" aria-hidden="true"><polyline points="20 6 9 17 4 12"></polyline></svg>`;
  }
  if (kind === "hold") {
    return `<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" aria-hidden="true"><circle cx="12" cy="12" r="10"></circle><polyline points="12 6 12 12 16 14"></polyline></svg>`;
  }
  return `<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" aria-hidden="true"><path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"></path><line x1="12" y1="9" x2="12" y2="13"></line><line x1="12" y1="17" x2="12.01" y2="17"></line></svg>`;
}

let toastTimer = null;
function showToast(msg) {
  const el = document.getElementById("toast-banner");
  if (!el) return;
  el.textContent = msg;
  el.hidden = false;
  if (toastTimer) clearTimeout(toastTimer);
  toastTimer = setTimeout(() => {
    el.hidden = true;
  }, 4200);
}

function esiBadgeClass(esiStr) {
  const num = String(esiStr || "ESI-3").replace(/\D/g, "") || "3";
  return `badge badge-esi-${num}`;
}

function gateBadgeHtml(gateState) {
  if (gateState === "CERTIFIED_SAFE_ROUTINE") {
    return `<span class="badge badge-gate-safe">${iconSvg("check")} Certified Safe</span>`;
  }
  if (gateState === "AMBIGUOUS_HOLD_VOI") {
    return `<span class="badge badge-gate-hold">${iconSvg("hold")} Hold · VOI</span>`;
  }
  return `<span class="badge badge-gate-crit">${iconSvg("alert")} Escalation</span>`;
}

function ensureDraftForPacket(pkt) {
  if (!pkt) return {};
  if (!state.draftDecisions[pkt.packet_id]) {
    const map = {};
    for (const rec of pkt.recommendations || []) {
      map[rec.type] = {
        action: "accept",
        overrideReason: "",
        finalText: rec.proposal,
      };
    }
    state.draftDecisions[pkt.packet_id] = map;
  }
  return state.draftDecisions[pkt.packet_id];
}

function activePacket() {
  return state.packets.find((p) => p.packet_id === state.selectedPacketId) || state.packets[0] || null;
}

/* --------------------------------------------------------------------------
 * Shared Reusable Builders (Decisions Rows, Dossier Summary, Safety Tables)
 * -------------------------------------------------------------------------- */
function buildDecisionRowsHtml(pkt) {
  const drafts = ensureDraftForPacket(pkt);
  return (pkt.recommendations || [])
    .map((rec) => {
      const d = drafts[rec.type] || { action: "accept", overrideReason: "", finalText: rec.proposal };
      return `
        <div class="dec-row ${rec.alert ? "dec-alert" : ""}">
          <div class="dec-main">
            <div class="dec-copy">
              <p class="dec-proposal">${esc(DECISION_TITLES[rec.type] || rec.type)}: ${esc(rec.proposal)}</p>
              <p class="dec-rationale">${esc(rec.rationale)}</p>
            </div>
            <div class="dec-controls" role="group" aria-label="${esc(DECISION_TITLES[rec.type] || rec.type)} action">
              <button type="button" class="dec-act-btn" data-dec-type="${esc(rec.type)}" data-act="accept" aria-pressed="${d.action === "accept"}">Accept</button>
              <button type="button" class="dec-act-btn" data-dec-type="${esc(rec.type)}" data-act="change" aria-pressed="${d.action === "change"}">Change</button>
              <button type="button" class="dec-act-btn" data-dec-type="${esc(rec.type)}" data-act="decline" aria-pressed="${d.action === "decline"}">Decline</button>
            </div>
          </div>
          ${
            d.action === "change"
              ? `
            <div class="override-editor">
              <input type="text" class="override-input" data-override-final="${esc(rec.type)}" value="${esc(d.finalText)}" placeholder="Specify modified clinical decision…" aria-label="Modified clinical decision" />
              <input type="text" class="override-input" data-override-reason="${esc(rec.type)}" value="${esc(d.overrideReason)}" placeholder="Override rationale (minimum 8 characters required)…" aria-label="Override rationale" />
            </div>
          `
              : ""
          }
        </div>
      `;
    })
    .join("");
}

function buildGateActionButtons(pkt) {
  const sg = pkt.safe_gate || {};
  const vr = pkt.voi_routing || {};
  const gateState = sg.gate_state || "SENTINEL_ESCALATION";

  if (sg.allow_one_pinch_routine) {
    return `
      <button type="button" class="btn btn-safe" data-action="fastpass-routine">
        ${iconSvg("check")} Accept Routine — Certified Safe (1-Pinch)
      </button>
    `;
  }
  if (gateState === "AMBIGUOUS_HOLD_VOI" && (vr.auto_triggered_channels || []).length > 0) {
    return `
      <button type="button" class="btn btn-hold" data-action="prefetch-voi">
        ${iconSvg("hold")} Pre-Fetch ${vr.auto_triggered_channels.length} High-VOI Channel(s)
      </button>
      <button type="button" class="btn btn-primary" data-action="accept-all-reviewed">
        Commit Recommended Actions
      </button>
    `;
  }
  return `
    <button type="button" class="btn btn-crit" data-action="accept-all-reviewed">
      Commit Escalated Actions
    </button>
  `;
}

function buildSpotdHtml(sw) {
  return `
    <div class="spotd-grid" aria-label="Satz-EM SPOT DR cognitive checklist">
      ${(sw.spotd || [])
        .map(
          (cell) => `
        <div class="spotd-cell ${cell.chart ? "chart-backed" : ""}">
          <div class="spotd-head">
            <span class="spotd-letter">${esc(cell.letter)}</span>
            <span>${esc(cell.label)}</span>
          </div>
          <div class="spotd-phrase">${esc(cell.phrase)}</div>
        </div>
      `
        )
        .join("")}
    </div>
  `;
}

function buildSafetyMetricsInnerHtml(pkt) {
  const sg = pkt.safe_gate || {};
  const vr = pkt.voi_routing || {};
  const channels = vr.channels || [];

  return `
    <div style="margin-bottom:14px;">
      <h3 class="section-heading" style="margin-bottom:8px;">
        <span>Calibrated Risk &amp; Conformal Receipt</span>
        ${vr.cfbh_alert_certified ? `<span class="badge badge-gate-crit">CfBH Alert Certified</span>` : `<span class="badge badge-neutral">Sub-CfBH</span>`}
      </h3>
      <table class="metric-table">
        <tbody>
          <tr><th>Stage-1 Door Posterior p₁(x)</th><td class="num">${pct(sg.p1_calibrated)}</td></tr>
          <tr><th>Framing-Blind Objective p₁,blind(x)</th><td class="num">${pct(sg.p1_framing_blind)}</td></tr>
          <tr><th>Stage-2 Full Chart Posterior p₂(x)</th><td class="num">${pct(sg.p2_calibrated)}</td></tr>
          <tr><th>Bayes Escalation Threshold τ*</th><td class="num">${pct(sg.tau_bayes_triage)}</td></tr>
          <tr><th>Capacity-Adjusted Space τ*space</th><td class="num">${pct(sg.tau_space_capacity_adjusted)}</td></tr>
          <tr><th>Conformal Safe Routine Cutoff τsafe</th><td class="num">${pct(sg.tau_safe_routine)}</td></tr>
          <tr><th>Positive Conformal Miss p-value</th><td class="num">${Number(sg.conformal_miss_pvalue ?? 0).toFixed(4)}</td></tr>
          <tr><th>Cal FOR 95% Wilson Upper Bound</th><td class="num">${pct(sg.cal_for_wilson_upper)}</td></tr>
          <tr><th>Base-Rate Evidence Deficit Dq(x)</th><td class="num">${Number(sg.evidence_deficit_nats ?? 0).toFixed(2)} nats</td></tr>
        </tbody>
      </table>
    </div>

    <div>
      <h3 class="section-heading" style="margin-bottom:8px;">
        <span>Decision-Flip VOI Channel Router</span>
        <span class="badge badge-neutral">9-Node GH</span>
      </h3>
      ${
        vr.triage_voi_zero_already_above_tau
          ? `<div class="voi-note-box" style="margin-bottom:8px;background:var(--bg-crit);border-color:var(--border-crit);color:var(--ink-crit);font-weight:600;">
              High Risk ≠ High VOI: Patient is already above τ* (or missing an acute door evaluation). Escalation VOI = 0.000 — act immediately without waiting on external HIE queries.
            </div>`
          : `<div class="voi-note-box" style="margin-bottom:8px;">
              Martingale mean-centered Gauss-Hermite quadrature triggers Stage-2 queries strictly when expected decision-flip gain exceeds query cost c_m.
            </div>`
      }
      <table class="metric-table">
        <thead>
          <tr>
            <th>Stage-2 Channel</th>
            <th style="text-align:right;">VOI</th>
            <th style="text-align:right;">Cost</th>
            <th style="text-align:right;">Route</th>
          </tr>
        </thead>
        <tbody>
          ${channels
            .map((ch) => {
              const routeBadge = ch.unlocked
                ? `<span class="badge badge-gate-safe">Fetched</span>`
                : ch.trigger_query
                ? `<span class="badge badge-gate-hold">Query</span>`
                : `<span class="badge badge-neutral">Skip</span>`;
              return `
                <tr>
                  <td>${esc(ch.display_name)}</td>
                  <td class="num">${Number(ch.voi_expected_gain).toFixed(3)}</td>
                  <td class="num">${Number(ch.query_cost).toFixed(3)}</td>
                  <td style="text-align:right;">${routeBadge}</td>
                </tr>
              `;
            })
            .join("")}
        </tbody>
      </table>
    </div>
  `;
}

/* --------------------------------------------------------------------------
 * Render Top Capacity Strip & Queue Rail
 * -------------------------------------------------------------------------- */
function renderCapacityStrip() {
  const host = document.getElementById("capacity-strip");
  if (!host) return;
  const c = state.capacity;
  const units = [
    { key: "bedsOpen", maxKey: "bedsTotal", label: "Beds" },
    { key: "hallwayOpen", maxKey: "hallwayTotal", label: "Hallway" },
    { key: "fastTrackOpen", maxKey: "fastTrackTotal", label: "Fast Track" },
    { key: "waitboard", maxKey: null, label: "Waitboard" },
  ];
  host.innerHTML = units
    .map((u) => {
      const valText = u.maxKey ? `${c[u.key]}/${c[u.maxKey]}` : `${c[u.key]}`;
      return `
        <div class="cap-unit">
          <span class="cap-label">${esc(u.label)}</span>
          <button type="button" class="cap-step" data-cap-key="${u.key}" data-cap-delta="-1" aria-label="Decrease ${esc(u.label)}">−</button>
          <span class="cap-val">${esc(valText)}</span>
          <button type="button" class="cap-step" data-cap-key="${u.key}" data-cap-delta="1" aria-label="Increase ${esc(u.label)}">+</button>
        </div>
      `;
    })
    .join("");
}

function renderQueue() {
  const listEl = document.getElementById("queue-list");
  const countEl = document.getElementById("queue-summary-count");
  if (!listEl || !countEl) return;

  const filtered = state.packets.filter((pkt) => {
    if (state.activeFilter === "REVIEWED") return state.reviewedPacketIds.has(pkt.packet_id);
    if (state.activeFilter === "ALL") return true;
    return pkt.safe_gate?.gate_state === state.activeFilter;
  });

  countEl.textContent = `${filtered.length} of ${state.packets.length} arrivals`;

  listEl.innerHTML = filtered
    .map((pkt) => {
      const enc = pkt.encounter || {};
      const charted = enc.triage_status || "ESI-3";
      const proposed = pkt.recommendations?.[0]?.value?.triage_status || charted;
      const esiText = proposed !== charted ? `${charted} → ${proposed}` : charted;
      const isSelected = pkt.packet_id === state.selectedPacketId;
      const isReviewed = state.reviewedPacketIds.has(pkt.packet_id);
      const ekg = pkt.ekg_chip || { status: "na", label: "EKG N/A" };

      return `
        <button type="button" class="queue-item" data-select-packet="${esc(pkt.packet_id)}" aria-current="${isSelected ? "true" : "false"}">
          <div class="q-top">
            <span class="q-id">${esc(pkt.packet_id)}${isReviewed ? " · Committed" : ""}</span>
            <span class="q-meta">${esc(enc.age_years)}y ${esc(enc.sex)} · ${esc(enc.arrival_mode)} · ${esc(enc.arrival_minutes_ago)}m</span>
          </div>
          <div class="q-chief">${esc(enc.chief_complaint)}</div>
          <div class="q-badges">
            <span class="${esiBadgeClass(proposed)}">${esc(esiText)}</span>
            ${gateBadgeHtml(pkt.safe_gate?.gate_state)}
            <span class="badge badge-neutral">${esc(ekg.label)}</span>
          </div>
        </button>
      `;
    })
    .join("");
}

/* --------------------------------------------------------------------------
 * Mode 1: Focused Action View (Calm, Key Actions Only, Progressive Disclosure)
 * -------------------------------------------------------------------------- */
function renderFocusedNowGate(pkt) {
  const enc = pkt.encounter || {};
  const dos = pkt.dossier || {};
  const vit = enc.vitals || {};
  const prior = (enc.prior_vitals || [])[0] || null;
  const evals = enc.evals || {};
  const sg = pkt.safe_gate || {};
  const sw = pkt.swarm || {};
  const card = sw.synthesizer || {};
  const sentinels = pkt.sentinels || [];
  const recs = pkt.recommendations || [];

  const gateState = sg.gate_state || "SENTINEL_ESCALATION";
  const bannerStateCls =
    gateState === "CERTIFIED_SAFE_ROUTINE"
      ? "state-safe"
      : gateState === "AMBIGUOUS_HOLD_VOI"
      ? "state-hold"
      : "state-crit";

  const vPill = (label, val, pVal, isAbn) => {
    const trend = pVal !== undefined && pVal !== null && pVal !== val ? ` (← ${pVal})` : "";
    return `<span class="vital-pill ${isAbn ? "abn" : ""}"><strong>${esc(label)}</strong> ${esc(val)}${esc(trend)}</span>`;
  };

  const missingEvals = ["trauma", "neuro", "d2b"].filter((k) => evals[k] === "missing");

  // Distill ONLY the key actionable items so the clinician is never overwhelmed
  const actionRows = [];

  for (const s of sentinels) {
    actionRows.push({
      tone: "urgent",
      badge: s.badge,
      title: s.summary,
      sub: s.clinical_why,
    });
  }

  for (const f of card.findings || []) {
    actionRows.push({
      tone: "urgent",
      badge: `Chart Delta ${f.id}`,
      title: f.finding,
      sub: `${f.action} (${f.why_now})`,
    });
  }

  const activeDecisionRecs = recs.filter(
    (r) => r.alert || (r.type === "space_priority" && r.value?.priority !== "waiting_room")
  );
  const quietDecisionCount = recs.length - activeDecisionRecs.length;

  for (const r of activeDecisionRecs) {
    actionRows.push({
      tone: r.alert ? "attention" : "normal",
      badge: DECISION_TITLES[r.type].replace(/^\d+\.\s*/, ""),
      title: r.proposal,
      sub: r.rationale,
    });
  }

  if ((card.ask_patient || []).length > 0) {
    actionRows.push({
      tone: "normal",
      badge: "Ask Patient Next",
      title: card.ask_patient.map((q) => q.question).join(" · "),
      sub: "Targeted high-VOI bedside verification questions.",
    });
  }

  if (actionRows.length === 0) {
    actionRows.push({
      tone: "normal",
      badge: "Routine Pathway",
      title: "All 5 PITM decisions match routine standing care with zero chart sentinels.",
      sub: "Conformal missed-escalation check passed — safe for 1-pinch routine acceptance.",
    });
  }

  const medsList = (dos.current_meds || []).join(", ") || "None";
  const recentLabsTxt =
    (dos.recent_labs || []).map((l) => `${l.test}: ${l.value} (${l.when})`).join(" · ") ||
    "No recent labs on file";
  const recentVisitsTxt =
    (dos.last_ed_visits || []).map((v) => `${v.when}: ${v.summary}`).join(" · ") ||
    "No recent ED visits";

  return `
    <!-- Compact Patient Summary + Immediate Gate Bar -->
    <article class="panel-surface focused-hero-card">
      <div class="enc-top-row">
        <div>
          <h2 class="enc-heading">${esc(pkt.packet_id)} · ${esc(enc.chief_complaint)}</h2>
          <p class="enc-subline">
            ${esc(enc.age_years)}y ${esc(enc.sex)} · ${esc(enc.arrival_mode)} (${esc(enc.arrival_minutes_ago)}m ago) · Charted ${esc(enc.triage_status)}
          </p>
        </div>
        <div class="enc-chip-cluster">
          <span class="${esiBadgeClass(enc.triage_status)}">Charted ${esc(enc.triage_status)}</span>
          <span class="badge ${pkt.ekg_chip?.status === "pending" ? "badge-gate-hold" : "badge-neutral"}">
            ${esc(pkt.ekg_chip?.label || "EKG N/A")}
          </span>
          ${missingEvals.map((k) => `<span class="badge badge-gate-crit">${esc(k.toUpperCase())} MISSING</span>`).join("")}
        </div>
      </div>

      <div class="focused-vitals-inline">
        ${vPill("HR", `${vit.hr} bpm`, prior?.hr, vit.hr >= 100 || vit.hr <= 50)}
        ${vPill("BP", `${vit.sbp}/${vit.dbp}`, prior ? `${prior.sbp}/${prior.dbp}` : null, vit.sbp < 95)}
        ${vPill("RR", `${vit.rr}/m`, prior?.rr, vit.rr >= 22)}
        ${vPill("SpO2", `${vit.spo2}%`, prior?.spo2, vit.spo2 < 94)}
        ${vPill("Temp", `${vit.temp_c}°C`, prior?.temp_c, vit.temp_c >= 38.0 || vit.temp_c < 36.0)}
        ${(vit.gcs ?? 15) < 15 ? vPill("GCS", vit.gcs, prior?.gcs, true) : ""}
      </div>

      <div class="safe-gate-banner ${bannerStateCls}" style="margin-top:14px;">
        <div class="gate-copy">
          <h3 class="gate-title">
            ${iconSvg(gateState === "CERTIFIED_SAFE_ROUTINE" ? "check" : gateState === "AMBIGUOUS_HOLD_VOI" ? "hold" : "alert")}
            <span>${esc(sg.gate_label)}</span>
          </h3>
          <p class="gate-desc">${esc(sg.gate_reason)}</p>
        </div>
        <div class="gate-actions">
          ${buildGateActionButtons(pkt)}
        </div>
      </div>
    </article>

    <!-- Key Actionable Items Only -->
    <section class="panel-surface focused-checklist-card" aria-label="Key actionable clinical items">
      <h3 class="section-heading">
        <span>Key Actionable Items (${actionRows.length})</span>
        ${quietDecisionCount > 0 ? `<span class="badge badge-neutral">${quietDecisionCount} routine PITM item(s) unchanged</span>` : ""}
      </h3>
      ${card.framing_check ? `<div class="framing-alert">${esc(card.framing_check)}</div>` : ""}
      <ul class="action-item-list">
        ${actionRows
          .map(
            (item) => `
          <li class="action-item-row ${item.tone === "urgent" ? "urgent" : item.tone === "attention" ? "attention" : ""}">
            <div class="action-item-main">
              <p class="action-item-title">${esc(item.title)}</p>
              <p class="action-item-sub">${esc(item.sub)}</p>
            </div>
            <span class="badge ${item.tone === "urgent" ? "badge-gate-crit" : item.tone === "attention" ? "badge-gate-hold" : "badge-neutral"}">
              ${esc(item.badge)}
            </span>
          </li>
        `
          )
          .join("")}
      </ul>
    </section>

    <!-- On-Demand Expandable Detail Sections (Collapsed by default to prevent visual overload) -->
    <div class="expand-stack">
      <details class="expand-drawer">
        <summary>
          <span>Customize or Override Individual PITM Decisions (All 5)</span>
          <span class="badge badge-neutral">Expand to Change / Decline</span>
        </summary>
        <div class="drawer-content">
          <div class="decision-rows">
            ${buildDecisionRowsHtml(pkt)}
          </div>
          <div class="commit-bar">
            <span style="font-size:12.5px;color:var(--ink-secondary);">
              Overrides require at least 8 characters and log to the SHA-256 hash-chained ledger.
            </span>
            <button type="button" class="btn btn-primary" data-action="commit-five">
              Commit Customized 5 Decisions
            </button>
          </div>
        </div>
      </details>

      <details class="expand-drawer">
        <summary>
          <span>Nurse Note, Chart Dossier &amp; SPOT DR Cognitive Strip</span>
          <span class="badge badge-neutral">${esc(sw.orthogonality?.distinct_mechanisms_covered ?? 0)} Mechanism Axis/Axes</span>
        </summary>
        <div class="drawer-content">
          <div class="nurse-note-box" style="margin-top:0;margin-bottom:10px;">
            <strong>Nurse Triage Note:</strong> ${esc(enc.nurse_hx_summary)}
          </div>
          <div class="delta-split" style="margin-top:0;margin-bottom:12px;">
            <div class="delta-subbox">
              <h4 class="subbox-title">PMH &amp; Active Medications</h4>
              <p style="margin:0;font-size:12.5px;color:var(--ink-secondary);">
                <strong>PMH:</strong> ${esc(dos.pmh_summary)}<br/>
                <strong>Meds:</strong> ${esc(medsList)}
              </p>
            </div>
            <div class="delta-subbox">
              <h4 class="subbox-title">Recent Visits &amp; Labs</h4>
              <p style="margin:0;font-size:12.5px;color:var(--ink-secondary);">
                <strong>Visits:</strong> ${esc(recentVisitsTxt)}<br/>
                <strong>Labs:</strong> ${esc(recentLabsTxt)}
              </p>
            </div>
          </div>
          ${buildSpotdHtml(sw)}
        </div>
      </details>

      <details class="expand-drawer">
        <summary class="tabular">
          <span>Statistical Safety &amp; Decision-Flip VOI Receipt</span>
          <span class="badge badge-neutral">Door p₁ ${pct(sg.p1_calibrated)} → Full Chart p₂ ${pct(sg.p2_calibrated)} · τsafe ${pct(sg.tau_safe_routine)}</span>
        </summary>
        <div class="drawer-content">
          ${buildSafetyMetricsInnerHtml(pkt)}
        </div>
      </details>
    </div>
  `;
}

/* --------------------------------------------------------------------------
 * Mode 2: Full Workbench View (3-Column Power-User Desk)
 * -------------------------------------------------------------------------- */
function renderWorkbenchNowGate(pkt) {
  const enc = pkt.encounter || {};
  const vit = enc.vitals || {};
  const prior = (enc.prior_vitals || [])[0] || null;
  const evals = enc.evals || {};
  const sg = pkt.safe_gate || {};
  const sw = pkt.swarm || {};
  const card = sw.synthesizer || {};
  const orth = sw.orthogonality || {};
  const sentinels = pkt.sentinels || [];

  const gateState = sg.gate_state || "SENTINEL_ESCALATION";
  const bannerStateCls =
    gateState === "CERTIFIED_SAFE_ROUTINE"
      ? "state-safe"
      : gateState === "AMBIGUOUS_HOLD_VOI"
      ? "state-hold"
      : "state-crit";

  const vitalCell = (val, priorVal, abnCond, unit = "") => {
    const deltaStr =
      priorVal !== undefined && priorVal !== null && priorVal !== val ? ` (door ${priorVal})` : "";
    return `<td class="${abnCond ? "vital-abn" : ""}">${esc(val ?? "—")}${esc(unit)}${esc(deltaStr)}</td>`;
  };

  const evalBadges = ["trauma", "neuro", "d2b"]
    .map((k) => {
      const st = evals[k] || "not_indicated";
      const cls =
        st === "missing" ? "badge-gate-crit" : st === "done" ? "badge-gate-safe" : "badge-neutral";
      return `<span class="badge ${cls}">${esc(k.toUpperCase())}: ${esc(st.replace("_", " "))}</span>`;
    })
    .join("");

  const sentinelHtml =
    sentinels.length > 0
      ? `
      <section class="sentinel-section" aria-label="Non-waivable clinical sentinels">
        <h3 class="section-heading">
          <span>Must-Know Clinical Sentinels (${sentinels.length})</span>
          <span class="badge badge-gate-crit">Deterministic Layer-0 Guardrail</span>
        </h3>
        <ul class="sentinel-list">
          ${sentinels
            .map(
              (s) => `
            <li class="sentinel-row">
              <div class="sentinel-row-head">
                <span class="badge badge-gate-crit">${esc(s.badge)}</span>
                <span class="src-pill">${esc(s.source)}</span>
              </div>
              <strong>${esc(s.summary)}</strong>
              <span class="sentinel-why">${esc(s.clinical_why)}</span>
            </li>
          `
            )
            .join("")}
        </ul>
      </section>
    `
      : "";

  const findingsHtml =
    (card.findings || []).length > 0
      ? `<div class="findings-list">
          ${card.findings
            .map(
              (f) => `
            <div class="finding-row">
              <div class="finding-top">
                <span class="finding-title">${esc(f.id)} · ${esc(f.finding)}</span>
                <div>
                  <span class="badge badge-neutral">${esc(f.status)}</span>
                  <span class="badge ${f.evidence === "CONFLICTING" ? "badge-gate-crit" : "badge-neutral"}">${esc(f.evidence)}</span>
                </div>
              </div>
              <p class="finding-why">${esc(f.why_now)}</p>
              <p class="finding-action">Recommended focus: ${esc(f.action)}</p>
              <div class="src-pills">
                ${(f.sources || []).map((src) => `<span class="src-pill">${esc(src)}</span>`).join("")}
              </div>
            </div>
          `
            )
            .join("")}
        </div>`
      : "";

  const safetyAndAskHtml =
    (card.safety_net || []).length > 0 || (card.ask_patient || []).length > 0
      ? `<div class="delta-split">
          <div class="delta-subbox">
            <h4 class="subbox-title">Immediate Safety-Net Checks</h4>
            ${
              (card.safety_net || []).length > 0
                ? `<ul class="compact-ul">
                    ${card.safety_net
                      .map((sn) => `<li><strong>${esc(sn.action)}</strong> — ${esc(sn.checks)}</li>`)
                      .join("")}
                  </ul>`
                : `<p style="margin:0;font-size:12.5px;color:var(--ink-muted);">Standing nursing pathway covers immediate checks.</p>`
            }
          </div>
          <div class="delta-subbox">
            <h4 class="subbox-title">VOI-Targeted Bedside Questions</h4>
            ${
              (card.ask_patient || []).length > 0
                ? `<ul class="compact-ul">
                    ${card.ask_patient
                      .map((aq) => `<li><strong>[${esc(aq.for_finding)}]</strong> ${esc(aq.question)}</li>`)
                      .join("")}
                  </ul>`
                : `<p style="margin:0;font-size:12.5px;color:var(--ink-muted);">No high-VOI bedside clarification needed right now.</p>`
            }
          </div>
        </div>`
      : "";

  return `
    <article class="panel-surface encounter-header-card">
      <div class="enc-top-row">
        <div>
          <h2 class="enc-heading">${esc(pkt.packet_id)} · ${esc(enc.chief_complaint)}</h2>
          <p class="enc-subline">
            ${esc(enc.age_years)}y ${esc(enc.sex)} · Arrival via ${esc(enc.arrival_mode)} (${esc(enc.arrival_minutes_ago)}m ago) · Charted ${esc(enc.triage_status)}
          </p>
        </div>
        <div class="enc-chip-cluster">
          <span class="${esiBadgeClass(enc.triage_status)}">Charted ${esc(enc.triage_status)}</span>
          <span class="badge ${pkt.ekg_chip?.status === "pending" ? "badge-gate-hold" : "badge-neutral"}">
            ${esc(pkt.ekg_chip?.label || "EKG N/A")}
          </span>
          ${evalBadges}
        </div>
      </div>

      <div class="nurse-note-box">
        <strong>Nurse Triage Summary:</strong> ${esc(enc.nurse_hx_summary)}
      </div>

      <div class="vitals-table-wrap">
        <table class="vitals-strip">
          <thead>
            <tr><th>HR</th><th>BP</th><th>RR</th><th>SpO2</th><th>Temp</th><th>GCS</th></tr>
          </thead>
          <tbody>
            <tr>
              ${vitalCell(vit.hr, prior?.hr, vit.hr >= 100 || vit.hr <= 50, " bpm")}
              ${vitalCell(`${vit.sbp}/${vit.dbp}`, prior ? `${prior.sbp}/${prior.dbp}` : null, vit.sbp < 95, " mmHg")}
              ${vitalCell(vit.rr, prior?.rr, vit.rr >= 22, "/min")}
              ${vitalCell(vit.spo2, prior?.spo2, vit.spo2 < 94, "%")}
              ${vitalCell(vit.temp_c, prior?.temp_c, vit.temp_c >= 38.0 || vit.temp_c < 36.0, " °C")}
              ${vitalCell(vit.gcs ?? 15, prior?.gcs, (vit.gcs ?? 15) < 15, "")}
            </tr>
          </tbody>
        </table>
      </div>
    </article>

    <section class="safe-gate-banner ${bannerStateCls}" aria-label="Dual Conformal Safe-Gate status">
      <div class="gate-copy">
        <h3 class="gate-title">
          ${iconSvg(gateState === "CERTIFIED_SAFE_ROUTINE" ? "check" : gateState === "AMBIGUOUS_HOLD_VOI" ? "hold" : "alert")}
          <span>${esc(sg.gate_label)}</span>
        </h3>
        <p class="gate-desc">${esc(sg.gate_reason)}</p>
      </div>
      <div class="gate-actions">
        ${buildGateActionButtons(pkt)}
      </div>
    </section>

    ${sentinelHtml}

    <section class="panel-surface delta-card" aria-label="Orthogonal clinical delta synthesis">
      <h3 class="section-heading">
        <span>Orthogonal Clinical Delta (Chart vs. Door Triage)</span>
        <span class="badge ${card.priority === "CRITICAL" ? "badge-gate-crit" : card.priority === "HIGH" ? "badge-gate-hold" : "badge-gate-safe"}">
          ${esc(card.priority)} · ${esc(orth.distinct_mechanisms_covered ?? 0)} Mechanism Axis/Axes
        </span>
      </h3>
      ${card.framing_check ? `<div class="framing-alert">${esc(card.framing_check)}</div>` : ""}
      <p class="delta-headline">${esc(card.headline)}</p>
      ${findingsHtml}
      ${safetyAndAskHtml}
      ${buildSpotdHtml(sw)}
      <p style="margin:12px 0 0;font-size:12.5px;color:var(--ink-secondary);">
        <strong>Flow Note:</strong> ${esc(card.queue_note)}
      </p>
      <details class="orth-disclosure">
        <summary class="tabular">
          Sequential Conditional Gain (G_{m|S}) Audit: ${esc(orth.n_orthogonal_selected ?? 0)} selected · ${esc(orth.n_echoes_suppressed ?? 0)} triage echoes suppressed · ${esc(orth.n_twins_suppressed ?? 0)} collinear twins pruned
        </summary>
        <ul class="compact-ul" style="margin-top:8px;">
          ${(card.suppressed || [])
            .map(
              (sup) =>
                `<li><strong>${esc(sup.item)}</strong> — ${esc(SUPPRESSED_REASON_LABELS[sup.reason] || sup.reason)}</li>`
            )
            .join("")}
        </ul>
      </details>
    </section>

    <section class="panel-surface decisions-card" aria-label="Five Provider-in-the-Middle decisions">
      <h3 class="section-heading">
        <span>Provider-in-the-Middle 5 Bounded Decisions</span>
        <span class="badge badge-neutral">EKG Never Ordered Here · Override Requires Rationale</span>
      </h3>
      <div class="decision-rows">
        ${buildDecisionRowsHtml(pkt)}
      </div>
      <div class="commit-bar">
        <span style="font-size:12.5px;color:var(--ink-secondary);">
          Commits all 5 decisions to the SHA-256 hash-chained ledger for Neyman two-phase shadow auditing.
        </span>
        <button type="button" class="btn btn-primary" data-action="commit-five">
          Commit 5 Decisions to Ledger
        </button>
      </div>
    </section>
  `;
}

function renderNowGate() {
  const root = document.getElementById("now-gate-root");
  if (!root) return;
  const pkt = activePacket();
  if (!pkt) {
    root.innerHTML = `<div class="panel-surface encounter-header-card"><h2 class="section-heading">No packet selected</h2></div>`;
    return;
  }
  root.innerHTML =
    state.viewMode === "focused" ? renderFocusedNowGate(pkt) : renderWorkbenchNowGate(pkt);
}

function renderSafetyRail() {
  const rail = document.getElementById("safety-rail-root");
  if (!rail) return;
  const pkt = activePacket();
  if (!pkt) {
    rail.innerHTML = "";
    return;
  }
  rail.innerHTML = buildSafetyMetricsInnerHtml(pkt);
}

function renderSpatialHud() {
  const hud = document.getElementById("spatial-patient-hud");
  if (!hud) return;
  const pkt = activePacket();
  if (!pkt) return;
  const enc = pkt.encounter || {};
  const sg = pkt.safe_gate || {};
  const proposedEsi = pkt.recommendations?.[0]?.value?.triage_status || enc.triage_status;
  const topFinding =
    pkt.swarm?.synthesizer?.findings?.[0]?.finding ||
    pkt.sentinels?.[0]?.summary ||
    pkt.recommendations?.find((r) => r.alert)?.proposal ||
    "Routine standing pathway — zero chart sentinels.";

  hud.innerHTML = `
    <div style="display:flex;justify-content:space-between;align-items:flex-start;gap:8px;">
      <div>
        <h2 style="margin:0;font-size:16px;font-weight:700;">${esc(pkt.packet_id)} · ${esc(enc.chief_complaint)}</h2>
        <p class="tabular" style="margin:3px 0 0;font-size:12px;color:var(--ink-secondary);">
          ${esc(enc.age_years)}y ${esc(enc.sex)} · Charted ${esc(enc.triage_status)} → Proposed ${esc(proposedEsi)} · Full Chart Risk ${pct(sg.p2_calibrated)}
        </p>
      </div>
      ${gateBadgeHtml(sg.gate_state)}
    </div>
    <p style="margin:10px 0;padding:8px 10px;border-radius:6px;background:var(--bg-subtle);border:1px solid var(--border);font-size:12.5px;">
      <strong>Lead Focus:</strong> ${esc(topFinding)}
    </p>
    <div style="display:flex;flex-wrap:wrap;gap:8px;justify-content:space-between;align-items:center;">
      ${buildGateActionButtons(pkt)}
      <button type="button" class="btn" data-switch-tab="focused">Open Focused Detail</button>
    </div>
  `;
}

/* --------------------------------------------------------------------------
 * Mode 3: Interactive 3D Spatial Stage (Three.js r184 WebGL + OrbitControls + Raycaster)
 * -------------------------------------------------------------------------- */
function createLabelSprite(THREE, text, bgColor = "#18222F", fgColor = "#FFFFFF") {
  const canvas = document.createElement("canvas");
  canvas.width = 256;
  canvas.height = 64;
  const ctx = canvas.getContext("2d");
  ctx.fillStyle = bgColor;
  ctx.beginPath();
  ctx.roundRect(6, 8, 244, 48, 12);
  ctx.fill();
  ctx.strokeStyle = "rgba(255,255,255,0.45)";
  ctx.lineWidth = 2;
  ctx.stroke();
  ctx.fillStyle = fgColor;
  ctx.font = "bold 22px -apple-system, BlinkMacSystemFont, Inter, sans-serif";
  ctx.textAlign = "center";
  ctx.textBaseline = "middle";
  ctx.fillText(text, 128, 33);

  const tex = new THREE.CanvasTexture(canvas);
  tex.needsUpdate = true;
  const mat = new THREE.SpriteMaterial({ map: tex, transparent: true, depthTest: false });
  const sprite = new THREE.Sprite(mat);
  sprite.scale.set(0.75, 0.19, 1);
  return { sprite, tex, mat };
}

class SpatialTriageStage {
  constructor(THREE, OrbitControls, host) {
    this.THREE = THREE;
    this.host = host;
    this.disposables = [];
    this.interactiveMeshes = [];

    this.scene = new THREE.Scene();
    this.scene.background = new THREE.Color(0x0f1722);

    const w = Math.max(320, host.clientWidth || window.innerWidth);
    const h = Math.max(320, host.clientHeight || window.innerHeight - 64);

    this.camera = new THREE.PerspectiveCamera(46, w / h, 0.1, 60);
    this.camera.position.set(0, 4.1, 6.2);

    this.renderer = new THREE.WebGLRenderer({ antialias: true, powerPreference: "high-performance" });
    this.renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
    this.renderer.setSize(w, h);
    host.innerHTML = "";
    host.appendChild(this.renderer.domElement);

    this.controls = new OrbitControls(this.camera, this.renderer.domElement);
    this.controls.enableDamping = true;
    this.controls.dampingFactor = 0.08;
    this.controls.target.set(0, 0.55, 0);
    this.controls.minDistance = 2.4;
    this.controls.maxDistance = 12.5;
    this.controls.maxPolarAngle = Math.PI * 0.48;

    // Crisp architectural studio lighting
    const hemi = new THREE.HemisphereLight(0xf2f7ff, 0x1e293b, 1.35);
    this.scene.add(hemi);
    const dir = new THREE.DirectionalLight(0xffffff, 1.25);
    dir.position.set(4.5, 8.0, 5.0);
    this.scene.add(dir);

    // Architectural floor grid + Bayes / Conformal threshold rings
    const grid = new THREE.GridHelper(10, 20, 0x334155, 0x1e293b);
    grid.position.y = 0.0;
    this.scene.add(grid);

    const outerRingGeo = new THREE.RingGeometry(3.15, 3.23, 96);
    const outerRingMat = new THREE.MeshBasicMaterial({
      color: 0x38bdf8,
      side: THREE.DoubleSide,
      transparent: true,
      opacity: 0.45,
    });
    const outerRing = new THREE.Mesh(outerRingGeo, outerRingMat);
    outerRing.rotation.x = -Math.PI / 2;
    outerRing.position.y = 0.01;
    this.scene.add(outerRing);

    const innerRingGeo = new THREE.RingGeometry(1.45, 1.51, 72);
    const innerRingMat = new THREE.MeshBasicMaterial({
      color: 0x22c55e,
      side: THREE.DoubleSide,
      transparent: true,
      opacity: 0.35,
    });
    const innerRing = new THREE.Mesh(innerRingGeo, innerRingMat);
    innerRing.rotation.x = -Math.PI / 2;
    innerRing.position.y = 0.012;
    this.scene.add(innerRing);

    this.dynamicGroup = new THREE.Group();
    this.scene.add(this.dynamicGroup);

    this.raycaster = new THREE.Raycaster();
    this.pointer = new THREE.Vector2();
    this.downPos = { x: 0, y: 0 };
    this.clock = new THREE.Clock();

    this._bindInteractions();
    this._setupResize();
    this._animate = this._animate.bind(this);
    this.renderer.setAnimationLoop(this._animate);

    // Expose WebXR button if browser supports immersive XR
    if (navigator.xr && typeof navigator.xr.isSessionSupported === "function") {
      navigator.xr.isSessionSupported("immersive-vr").then((supported) => {
        const btn = document.getElementById("btn-enter-webxr");
        if (btn && supported) {
          btn.hidden = false;
          this.renderer.xr.enabled = true;
          btn.onclick = async () => {
            const session = await navigator.xr.requestSession("immersive-vr", {
              optionalFeatures: ["local-floor", "hand-tracking"],
            });
            this.renderer.xr.setSession(session);
          };
        }
      });
    }
  }

  resetCamera() {
    this.camera.position.set(0, 4.1, 6.2);
    this.controls.target.set(0, 0.55, 0);
    this.controls.update();
  }

  _bindInteractions() {
    const dom = this.renderer.domElement;
    dom.addEventListener("pointerdown", (ev) => {
      this.downPos = { x: ev.clientX, y: ev.clientY };
    });

    dom.addEventListener("pointerup", (ev) => {
      const dist = Math.hypot(ev.clientX - this.downPos.x, ev.clientY - this.downPos.y);
      if (dist > 6) return; // Was an orbit drag, not a click

      const rect = dom.getBoundingClientRect();
      this.pointer.x = ((ev.clientX - rect.left) / rect.width) * 2 - 1;
      this.pointer.y = -((ev.clientY - rect.top) / rect.height) * 2 + 1;

      this.raycaster.setFromCamera(this.pointer, this.camera);
      const hits = this.raycaster.intersectObjects(this.interactiveMeshes, false);
      if (hits.length > 0) {
        const hit = hits[0].object;
        if (hit.userData?.packetId) {
          state.selectedPacketId = hit.userData.packetId;
          renderAll();
        } else if (hit.userData?.capKey) {
          updateCapacity(hit.userData.capKey, 1);
        }
      }
    });
  }

  _setupResize() {
    const onResize = () => {
      if (!this.host || state.viewMode !== "spatial") return;
      const w = Math.max(320, this.host.clientWidth || window.innerWidth);
      const h = Math.max(320, this.host.clientHeight || window.innerHeight - 64);
      this.camera.aspect = w / h;
      this.camera.updateProjectionMatrix();
      this.renderer.setSize(w, h);
    };
    window.addEventListener("resize", onResize);
    this._onResize = onResize;
  }

  clearDynamic() {
    while (this.dynamicGroup.children.length > 0) {
      const ch = this.dynamicGroup.children.pop();
      this.dynamicGroup.remove(ch);
    }
    for (const d of this.disposables) {
      if (d && typeof d.dispose === "function") d.dispose();
    }
    this.disposables = [];
    this.interactiveMeshes = [];
    this.selectedBeacon = null;
  }

  sync(appState) {
    const THREE = this.THREE;
    this.clearDynamic();

    // 1. Center 4 Interactive ED Capacity Pillars
    const c = appState.capacity;
    const zones = [
      { key: "bedsOpen", label: `Beds ${c.bedsOpen}/${c.bedsTotal}`, x: -0.9, z: 0, ratio: c.bedsOpen / Math.max(1, c.bedsTotal), color: 0x2563eb },
      { key: "hallwayOpen", label: `Hall ${c.hallwayOpen}/${c.hallwayTotal}`, x: -0.3, z: 0, ratio: c.hallwayOpen / Math.max(1, c.hallwayTotal), color: 0xd97706 },
      { key: "fastTrackOpen", label: `Fast ${c.fastTrackOpen}/${c.fastTrackTotal}`, x: 0.3, z: 0, ratio: c.fastTrackOpen / Math.max(1, c.fastTrackTotal), color: 0x16a34a },
      { key: "waitboard", label: `Wait ${c.waitboard}`, x: 0.9, z: 0, ratio: Math.min(1, c.waitboard / 24), color: 0x64748b },
    ];

    for (const z of zones) {
      const h = 0.28 + 0.9 * z.ratio;
      const geo = new THREE.BoxGeometry(0.36, h, 0.36);
      const mat = new THREE.MeshStandardMaterial({
        color: z.color,
        roughness: 0.32,
        metalness: 0.18,
      });
      this.disposables.push(geo, mat);
      const mesh = new THREE.Mesh(geo, mat);
      mesh.position.set(z.x, h / 2, z.z);
      mesh.userData = { capKey: z.key };
      this.dynamicGroup.add(mesh);
      this.interactiveMeshes.push(mesh);

      const lbl = createLabelSprite(THREE, z.label, "#0F1722", "#E2E8F0");
      lbl.sprite.position.set(z.x, h + 0.22, z.z);
      this.disposables.push(lbl.tex, lbl.mat);
      this.dynamicGroup.add(lbl.sprite);
    }

    // 2. Outer Ring of 22 Calibrated Patient Totems
    const pkts = appState.packets || [];
    const n = Math.max(1, pkts.length);

    pkts.forEach((pkt, idx) => {
      const angle = (idx / n) * Math.PI * 2 - Math.PI / 2;
      const radius = 3.18;
      const pCal = Math.max(
        pkt.safe_gate?.p1_framing_blind || 0.02,
        pkt.safe_gate?.p2_calibrated || 0.02
      );
      const height = 0.32 + 1.85 * Math.min(1, pCal);
      const gate = pkt.safe_gate?.gate_state;
      const hex =
        gate === "CERTIFIED_SAFE_ROUTINE"
          ? 0x218348
          : gate === "AMBIGUOUS_HOLD_VOI"
          ? 0xd48b08
          : 0xc9352b;

      const isSel = pkt.packet_id === appState.selectedPacketId;
      const isReviewed = appState.reviewedPacketIds.has(pkt.packet_id);

      const geo = new THREE.CylinderGeometry(isSel ? 0.16 : 0.12, isSel ? 0.18 : 0.13, height, 24);
      const mat = new THREE.MeshStandardMaterial({
        color: hex,
        roughness: 0.28,
        metalness: 0.15,
        emissive: isSel ? hex : 0x000000,
        emissiveIntensity: isSel ? 0.55 : 0.0,
        opacity: isReviewed ? 0.45 : 1.0,
        transparent: isReviewed,
      });
      this.disposables.push(geo, mat);

      const x = Math.cos(angle) * radius;
      const z = Math.sin(angle) * radius;
      const totem = new THREE.Mesh(geo, mat);
      totem.position.set(x, height / 2, z);
      totem.userData = { packetId: pkt.packet_id };
      this.dynamicGroup.add(totem);
      this.interactiveMeshes.push(totem);

      const shortId = pkt.packet_id.replace("syn-", "");
      const lbl = createLabelSprite(
        THREE,
        isSel ? `▶ ${shortId}` : shortId,
        isSel ? "#F8FAFC" : "#1E293B",
        isSel ? "#0F1722" : "#F8FAFC"
      );
      lbl.sprite.position.set(x, height + 0.22, z);
      this.disposables.push(lbl.tex, lbl.mat);
      this.dynamicGroup.add(lbl.sprite);

      if (isSel) {
        const ringG = new THREE.RingGeometry(0.24, 0.31, 32);
        const ringM = new THREE.MeshBasicMaterial({ color: 0xffffff, side: THREE.DoubleSide });
        const beacon = new THREE.Mesh(ringG, ringM);
        beacon.rotation.x = -Math.PI / 2;
        beacon.position.set(x, 0.02, z);
        this.disposables.push(ringG, ringM);
        this.dynamicGroup.add(beacon);
        this.selectedBeacon = beacon;
      }
    });
  }

  _animate() {
    if (state.viewMode !== "spatial" && !this.renderer.xr.isPresenting) return;
    const t = this.clock.getElapsedTime();
    if (this.selectedBeacon) {
      const pulse = 1 + 0.12 * Math.sin(t * 4.0);
      this.selectedBeacon.scale.set(pulse, pulse, 1);
    }
    this.controls.update();
    this.renderer.render(this.scene, this.camera);
  }
}

async function ensureSpatialStage() {
  if (state.spatialStage) {
    state.spatialStage._onResize?.();
    state.spatialStage.sync(state);
    return;
  }
  const host = document.getElementById("xr-canvas-host");
  if (!host) return;

  try {
    const [THREE, orbitMod] = await Promise.all([
      import("three"),
      import("three/addons/controls/OrbitControls.js"),
    ]);
    state.spatialStage = new SpatialTriageStage(THREE, orbitMod.OrbitControls, host);
    state.spatialStage.sync(state);
  } catch (err) {
    host.innerHTML = `
      <div style="padding:32px;color:#E2E8F0;max-width:560px;">
        <h3 style="margin-top:0;">3D WebGL Renderer Unavailable</h3>
        <p>Could not load Three.js WebGL modules (${esc(err.message)}). Focused Action and Full Workbench remain active.</p>
      </div>
    `;
  }
}

function setViewMode(mode) {
  state.viewMode = mode;
  document.body.setAttribute("data-view-mode", mode);
  document.querySelectorAll("[data-view-tab]").forEach((btn) => {
    btn.setAttribute("aria-selected", String(btn.dataset.viewTab === mode));
  });

  const stageWrap = document.getElementById("spatial-stage-wrap");
  if (stageWrap) {
    stageWrap.hidden = mode !== "spatial";
  }

  renderAll();
  if (mode === "spatial") {
    requestAnimationFrame(() => {
      ensureSpatialStage();
    });
  }
}

function renderAll() {
  renderCapacityStrip();
  renderQueue();
  renderNowGate();
  if (state.viewMode === "workbench") {
    renderSafetyRail();
  }
  if (state.viewMode === "spatial") {
    renderSpatialHud();
    if (state.spatialStage) {
      state.spatialStage.sync(state);
    }
  }
}

/* --------------------------------------------------------------------------
 * API Actions (Capacity, VOI Pre-Fetch, Ledger Commit, Intake, Audit)
 * -------------------------------------------------------------------------- */
async function loadInitialQueue() {
  const [, pktResp, ledResp] = await Promise.all([
    fetch("/api/health").then((r) => r.json()),
    fetch("/api/packets").then((r) => r.json()),
    fetch("/api/ledger").then((r) => r.json()),
  ]);

  state.capacity = pktResp.capacity || state.capacity;
  state.packets = pktResp.packets || [];
  state.ledgerCount = ledResp.count || 0;
  for (const e of ledResp.entries || []) {
    if (e.packetId) state.reviewedPacketIds.add(e.packetId);
  }
  const ledCountEl = document.getElementById("ledger-count");
  if (ledCountEl) ledCountEl.textContent = String(state.ledgerCount);

  const defaultPkt =
    state.packets.find((p) => p.packet_id === "syn-ED-022") || state.packets[0] || null;
  state.selectedPacketId = defaultPkt ? defaultPkt.packet_id : null;

  renderAll();
}

async function updateCapacity(key, delta) {
  const next = { ...state.capacity };
  const maxKey =
    key === "bedsOpen"
      ? "bedsTotal"
      : key === "hallwayOpen"
      ? "hallwayTotal"
      : key === "fastTrackOpen"
      ? "fastTrackTotal"
      : null;
  let val = Number(next[key] || 0) + delta;
  if (maxKey && val > Number(next[maxKey])) {
    val = 0; // Wrap around smoothly when clicking 3D capacity pillars
  }
  val = Math.max(0, val);
  if (maxKey) val = Math.min(Number(next[maxKey]), val);
  next[key] = val;

  const resp = await fetch("/api/evaluate", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ capacity: next }),
  }).then((r) => r.json());

  if (resp.packets) {
    state.capacity = resp.capacity;
    state.packets = resp.packets;
    state.draftDecisions = {};
    renderAll();
  }
}

async function prefetchHighVoiChannels() {
  const pkt = activePacket();
  if (!pkt) return;
  const triggered = pkt.voi_routing?.auto_triggered_channels || [];
  if (!triggered.length) return;

  const resp = await fetch("/api/evaluate", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      packet_id: pkt.packet_id,
      unlocked_channels: triggered,
    }),
  }).then((r) => r.json());

  if (resp.packet) {
    state.packets = state.packets.map((p) =>
      p.packet_id === resp.packet.packet_id ? resp.packet : p
    );
    showToast(`Pre-fetched ${triggered.length} high-VOI Stage-2 channel(s) for ${pkt.packet_id}.`);
    renderAll();
  }
}

async function commitDecisionsForActivePacket(forceAcceptAll = false) {
  const pkt = activePacket();
  if (!pkt) return;
  const drafts = ensureDraftForPacket(pkt);

  const entries = [];
  for (const rec of pkt.recommendations || []) {
    const d = forceAcceptAll
      ? { action: "accept", overrideReason: "", finalText: rec.proposal }
      : drafts[rec.type];

    if (d.action === "change" && String(d.overrideReason || "").trim().length < 8) {
      showToast(
        `Override rationale for "${DECISION_TITLES[rec.type]}" must be at least 8 characters.`
      );
      return;
    }

    entries.push({
      packetId: pkt.packet_id,
      decisionType: rec.type,
      providerAction: d.action,
      overrideReason: d.action === "change" ? d.overrideReason.trim() : null,
      gateState: pkt.safe_gate?.gate_state,
      modelRecommendation: rec,
      finalValue: d.action === "change" ? { customProposal: d.finalText } : rec.value,
    });
  }

  const res = await fetch("/api/ledger", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ entries }),
  });
  const payload = await res.json();
  if (!res.ok) {
    showToast(payload.error || "Failed to write to ledger.");
    return;
  }

  state.reviewedPacketIds.add(pkt.packet_id);
  state.ledgerCount = payload.total_entries || state.ledgerCount + entries.length;
  const ledCountEl = document.getElementById("ledger-count");
  if (ledCountEl) ledCountEl.textContent = String(state.ledgerCount);
  showToast(`Committed 5 PITM decisions for ${pkt.packet_id} (SHA-256 chain verified).`);

  // Advance automatically to next unreviewed arrival in queue
  const nextUnreviewed = state.packets.find((p) => !state.reviewedPacketIds.has(p.packet_id));
  if (nextUnreviewed) {
    state.selectedPacketId = nextUnreviewed.packet_id;
  }
  renderAll();
}

async function openLedgerDialog() {
  const dlg = document.getElementById("dialog-ledger");
  const body = document.getElementById("ledger-dialog-body");
  if (!dlg || !body) return;
  dlg.showModal();
  body.textContent = "Verifying SHA-256 hash chain and running Neyman shadow audit…";

  const data = await fetch("/api/ledger").then((r) => r.json());
  const shadow = data.two_phase_shadow_audit || {};
  const shadowHtml =
    shadow.status === "audited"
      ? `
        <table class="metric-table" style="margin-bottom:16px;">
          <tbody>
            <tr><th>Total PITM Ledger Decisions</th><td class="num">${esc(shadow.n_ledger_records)}</td></tr>
            <tr><th>Constrained Neyman Pilot Sampled</th><td class="num">${esc(shadow.n_pilot_audited)} (${(shadow.pilot_fraction * 100).toFixed(1)}%)</td></tr>
            <tr><th>Kish Effective Sample Size (ESS)</th><td class="num">${esc(shadow.kish_ess)} (DEFF ${esc(shadow.design_effect)})</td></tr>
            <tr><th>Horvitz-Thompson Override Estimate</th><td class="num">${(shadow.ht_estimated_override_rate * 100).toFixed(1)}% ± ${(shadow.se_total * 100).toFixed(1)}%</td></tr>
            <tr><th>True Census Override Rate</th><td class="num">${(shadow.true_census_override_rate * 100).toFixed(1)}%</td></tr>
          </tbody>
        </table>
      `
      : `<div class="voi-note-box" style="margin-bottom:14px;">${esc(shadow.message || "Awaiting more ledger entries.")}</div>`;

  const rows = (data.entries || [])
    .slice(-15)
    .reverse()
    .map(
      (e) => `
        <tr>
          <td>${esc(e.packetId)}</td>
          <td>${esc(e.decisionType)}</td>
          <td><strong>${esc(e.providerAction)}</strong>${e.overrideReason ? ` — ${esc(e.overrideReason)}` : ""}</td>
          <td class="tabular" style="font-family:ui-monospace,monospace;font-size:11px;">${esc(String(e.entry_hash || "").slice(0, 14))}…</td>
        </tr>
      `
    )
    .join("");

  body.innerHTML = `
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px;">
      <strong>Unbroken SHA-256 Chain: ${data.valid_hash_chain ? "Verified" : "Tamper Detected"}</strong>
      <span class="badge badge-neutral">${esc(data.count)} Total Entries</span>
    </div>
    <h3 class="subbox-title">Two-Phase Constrained Neyman Shadow Audit</h3>
    ${shadowHtml}
    <h3 class="subbox-title">Recent Hash-Chained Decisions</h3>
    <table class="metric-table">
      <thead>
        <tr><th>Packet</th><th>Decision</th><th>Action</th><th>Entry SHA-256</th></tr>
      </thead>
      <tbody>
        ${rows || `<tr><td colspan="4">No decisions logged yet. Commit a packet on the Now Gate.</td></tr>`}
      </tbody>
    </table>
  `;
}

async function openAuditDialog() {
  const dlg = document.getElementById("dialog-audit");
  const body = document.getElementById("audit-dialog-body");
  if (!dlg || !body) return;
  dlg.showModal();
  body.textContent =
    "Computing Stage-A Neyman Two-Phase, Stage-B Martingale VOI / CfBH, and Stage-C Subgroup Calibration…";

  const a = await fetch("/api/safety/audit").then((r) => r.json());
  const cs = a.cohort_summary || {};
  const sa = a.stage_a_two_phase_discovery || {};
  const sp = sa.stress_pool_audit || {};
  const sb = a.stage_b_streaming_voi_and_conformal || {};
  const sg = sb.dual_conformal_safe_gate || {};
  const cf = sb.conformal_fdr_vs_mondrian || {};
  const sub = a.stage_c_subgroup_calibration || {};

  body.innerHTML = `
    <div class="voi-note-box" style="margin-bottom:14px;">
      <strong>Split SHA-256:</strong> <code>${esc(String(a.split_hash || "").slice(0, 32))}…</code> ·
      <strong>Receipt SHA-256:</strong> <code>${esc(String(a.receipt_sha256 || "").slice(0, 24))}…</code>
    </div>

    <h3 class="subbox-title">1. Stage-A Two-Phase Constrained Neyman Discovery &amp; 12-Channel Stress Pool</h3>
    <table class="metric-table" style="margin-bottom:16px;">
      <tbody>
        <tr><th>Cohort Size &amp; Natural Prevalence</th><td class="num">N = ${esc(cs.n_encounters)} (π = ${(cs.natural_prevalence * 100).toFixed(1)}%)</td></tr>
        <tr><th>Held-Out Test AUROC (Stage-1 → Stage-2)</th><td class="num">${esc(cs.stage1_test_auroc)} → ${esc(cs.stage2_test_auroc)}</td></tr>
        <tr><th>15% Constrained Neyman Pilot AUROC vs. 100% Full</th><td class="num">${esc(sa.mean_auroc_neyman_15pct)} vs. ${esc(sa.mean_auroc_full_100pct)} (85% query savings)</td></tr>
        <tr><th>15% Neyman vs. 15% SRS Pilot AUROC</th><td class="num">${esc(sa.mean_auroc_neyman_15pct)} vs. ${esc(sa.mean_auroc_srs_15pct)} (Mean Kish ESS ${esc(sa.mean_neyman_kish_ess)})</td></tr>
        <tr><th>Stress Pool False Selections (Null / Twin / Baseline Echo)</th><td class="num">${esc(sp.sequential_null_selected)} / ${esc(sp.sequential_twin_selected)} / ${esc(sp.sequential_echo_selected)}</td></tr>
      </tbody>
    </table>

    <h3 class="subbox-title">2. Stage-B Martingale VOI, Tie-Smoothed CfBH &amp; Dual Conformal Safe-Gate</h3>
    <table class="metric-table" style="margin-bottom:16px;">
      <tbody>
        <tr><th>Martingale VOI Net Benefit Advantage over Top-Risk Heuristic</th><td class="num">+${esc(sb.voi_vs_top_risk?.nb_advantage_per_1k_over_top_risk)} per 1,000 arrivals</td></tr>
        <tr><th>Jin &amp; Candès Tie-Smoothed CfBH Realized FDP (Target ≤ 0.35)</th><td class="num">${Number(cf.cfbh_tie_smoothed?.realized_fdp ?? 0).toFixed(3)} (PPV ${(Number(cf.cfbh_tie_smoothed?.realized_ppv ?? 0) * 100).toFixed(1)}%)</td></tr>
        <tr><th>Naive Mondrian {1} Singleton Realized FDP (Comparator)</th><td class="num">${Number(cf.mondrian_singleton_pos?.realized_fdp ?? 0).toFixed(3)} (Severe False-Alarm Inflation)</td></tr>
        <tr><th>Dual Conformal Safe-Gate Certified Routine Fraction</th><td class="num">${(Number(sg.test_certified_routine_rate ?? 0) * 100).toFixed(1)}% of ED arrivals</td></tr>
        <tr><th>Held-Out Test False Omission Rate (Cal 95% Wilson UCB ≤ 5.0%)</th><td class="num">${(Number(sg.test_realized_for ?? 0) * 100).toFixed(2)}% (Cal UCB ${(Number(sg.cal_for_wilson_95_upper ?? 0) * 100).toFixed(2)}%)</td></tr>
      </tbody>
    </table>

    <h3 class="subbox-title">3. Stage-C Subgroup &amp; Framing-Blind Calibration Audit</h3>
    <table class="metric-table">
      <thead>
        <tr><th>Subgroup</th><th style="text-align:right;">n</th><th style="text-align:right;">Prev</th><th style="text-align:right;">AUROC</th><th style="text-align:right;">ECE</th><th style="text-align:right;">Cox Slope</th></tr>
      </thead>
      <tbody>
        ${Object.entries(sub)
          .map(
            ([k, v]) => `
            <tr>
              <td>${esc(k)}</td>
              <td class="num">${esc(v.n)}</td>
              <td class="num">${(v.prevalence * 100).toFixed(1)}%</td>
              <td class="num">${Number(v.auroc).toFixed(3)}</td>
              <td class="num">${Number(v.ece).toFixed(3)}</td>
              <td class="num">${Number(v.cal_slope).toFixed(2)}</td>
            </tr>
          `
          )
          .join("")}
      </tbody>
    </table>
  `;
}

/* --------------------------------------------------------------------------
 * Event Delegation
 * -------------------------------------------------------------------------- */
function bindEvents() {
  document.addEventListener("click", (ev) => {
    const viewTab = ev.target.closest("[data-view-tab]");
    if (viewTab) {
      setViewMode(viewTab.dataset.viewTab);
      return;
    }

    const switchBtn = ev.target.closest("[data-switch-tab]");
    if (switchBtn) {
      setViewMode(switchBtn.dataset.switchTab);
      return;
    }

    const capBtn = ev.target.closest("[data-cap-key]");
    if (capBtn) {
      updateCapacity(capBtn.dataset.capKey, Number(capBtn.dataset.capDelta));
      return;
    }

    const pktBtn = ev.target.closest("[data-select-packet]");
    if (pktBtn) {
      state.selectedPacketId = pktBtn.dataset.selectPacket;
      renderAll();
      return;
    }

    const filterBtn = ev.target.closest("[data-filter]");
    if (filterBtn) {
      state.activeFilter = filterBtn.dataset.filter;
      document.querySelectorAll("[data-filter]").forEach((b) => {
        b.setAttribute("aria-pressed", String(b === filterBtn));
      });
      renderQueue();
      return;
    }

    const decBtn = ev.target.closest("[data-dec-type]");
    if (decBtn) {
      const pkt = activePacket();
      if (!pkt) return;
      const drafts = ensureDraftForPacket(pkt);
      const t = decBtn.dataset.decType;
      drafts[t].action = decBtn.dataset.act;
      renderNowGate();
      return;
    }

    const closeDlgBtn = ev.target.closest("[data-close-dialog]");
    if (closeDlgBtn) {
      document.getElementById(closeDlgBtn.dataset.closeDialog)?.close();
      return;
    }

    const actBtn = ev.target.closest("[data-action]");
    if (actBtn) {
      const act = actBtn.dataset.action;
      if (act === "fastpass-routine" || act === "accept-all-reviewed") {
        commitDecisionsForActivePacket(true);
      } else if (act === "commit-five") {
        commitDecisionsForActivePacket(false);
      } else if (act === "prefetch-voi") {
        prefetchHighVoiChannels();
      }
      return;
    }

    if (ev.target.closest("#btn-reset-camera")) {
      state.spatialStage?.resetCamera();
    }
  });

  document.addEventListener("input", (ev) => {
    const pkt = activePacket();
    if (!pkt) return;
    const drafts = ensureDraftForPacket(pkt);
    if (ev.target.matches("[data-override-reason]")) {
      drafts[ev.target.dataset.overrideReason].overrideReason = ev.target.value;
    } else if (ev.target.matches("[data-override-final]")) {
      drafts[ev.target.dataset.overrideFinal].finalText = ev.target.value;
    }
  });

  document.getElementById("btn-open-intake")?.addEventListener("click", () => {
    const dlg = document.getElementById("dialog-intake");
    const inp = document.getElementById("intake-json-input");
    const fb = document.getElementById("intake-feedback");
    if (inp && !inp.value.trim()) {
      inp.value = JSON.stringify(SAMPLE_INTAKE_PACKET, null, 2);
    }
    if (fb) fb.textContent = "";
    dlg?.showModal();
  });

  document.getElementById("btn-load-sample-intake")?.addEventListener("click", () => {
    const inp = document.getElementById("intake-json-input");
    if (inp) inp.value = JSON.stringify(SAMPLE_INTAKE_PACKET, null, 2);
  });

  document.getElementById("btn-submit-intake")?.addEventListener("click", async () => {
    const inp = document.getElementById("intake-json-input");
    const fb = document.getElementById("intake-feedback");
    if (!inp || !fb) return;
    try {
      const parsed = JSON.parse(inp.value);
      const res = await fetch("/api/intake", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(parsed),
      });
      const data = await res.json();
      if (!res.ok) {
        fb.style.color = "var(--ink-crit)";
        fb.textContent = data.error || "Intake validation failed.";
        return;
      }
      for (const newPkt of data.packets || []) {
        const idx = state.packets.findIndex((p) => p.packet_id === newPkt.packet_id);
        if (idx >= 0) state.packets[idx] = newPkt;
        else state.packets.unshift(newPkt);
        state.selectedPacketId = newPkt.packet_id;
      }
      document.getElementById("dialog-intake")?.close();
      showToast(`Accepted ${data.accepted} synthetic packet(s) through Zero-PHI Gate.`);
      renderAll();
    } catch (err) {
      fb.style.color = "var(--ink-crit)";
      fb.textContent = `JSON Parse Error: ${err.message}`;
    }
  });

  document.getElementById("btn-open-ledger")?.addEventListener("click", openLedgerDialog);
  document.getElementById("btn-open-audit")?.addEventListener("click", openAuditDialog);
}

bindEvents();
loadInitialQueue();

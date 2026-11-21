/**
 * EDimbESS Frontend Controller & XR Blocks Spatial Script (xrblocks@0.21.1 / three@0.184.0)
 * Integrates Provider-in-the-Middle 5-Decision Now Gate with Conformal Safe-Gate,
 * Martingale 9-Node Gauss-Hermite VOI Channel Pre-Fetching, Orthogonal Delta, and Neyman Ledger Audit.
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
    nurse_hx_summary: "Mild lightheadedness this afternoon, looks comfortable at triage. Denies chest pain. Denies blood thinners.",
    vitals: { hr: 98, sbp: 112, dbp: 68, rr: 16, spo2: 98, temp_c: 36.9 },
    prior_vitals: [{ when: "18m ago (door)", hr: 90, sbp: 122, dbp: 72, rr: 16, spo2: 98, temp_c: 36.8 }],
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
  draftDecisions: {}, // { [packetId]: { [decType]: { action, overrideReason, finalText } } }
  ledgerCount: 0,
  spatialMode: false,
  xrInstance: null,
};

function esc(val) {
  return String(val ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
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
 * Render Top Capacity Strip
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

/* --------------------------------------------------------------------------
 * Render Queue Rail
 * -------------------------------------------------------------------------- */
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
 * Render Active Patient Now Gate (Center Column)
 * -------------------------------------------------------------------------- */
function renderNowGate() {
  const root = document.getElementById("now-gate-root");
  if (!root) return;
  const pkt = activePacket();
  if (!pkt) {
    root.innerHTML = `<div class="panel-surface encounter-header-card"><h2 class="section-heading">No packet selected</h2></div>`;
    return;
  }

  const enc = pkt.encounter || {};
  const vit = enc.vitals || {};
  const prior = (enc.prior_vitals || [])[0] || null;
  const evals = enc.evals || {};
  const sg = pkt.safe_gate || {};
  const vr = pkt.voi_routing || {};
  const sw = pkt.swarm || {};
  const card = sw.synthesizer || {};
  const orth = sw.orthogonality || {};
  const sentinels = pkt.sentinels || [];
  const drafts = ensureDraftForPacket(pkt);

  const gateState = sg.gate_state || "SENTINEL_ESCALATION";
  const bannerStateCls =
    gateState === "CERTIFIED_SAFE_ROUTINE"
      ? "state-safe"
      : gateState === "AMBIGUOUS_HOLD_VOI"
      ? "state-hold"
      : "state-crit";

  const vitalCell = (val, priorVal, abnCond, unit = "") => {
    const deltaStr = priorVal !== undefined && priorVal !== null && priorVal !== val ? ` (door ${priorVal})` : "";
    return `<td class="${abnCond ? "vital-abn" : ""}">${esc(val ?? "—")}${esc(unit)}${esc(deltaStr)}</td>`;
  };

  const evalBadges = ["trauma", "neuro", "d2b"]
    .map((k) => {
      const st = evals[k] || "not_indicated";
      const cls = st === "missing" ? "badge-gate-crit" : st === "done" ? "badge-gate-safe" : "badge-neutral";
      return `<span class="badge ${cls}">${esc(k.toUpperCase())}: ${esc(st.replace("_", " "))}</span>`;
    })
    .join("");

  let gateButtonsHtml = "";
  if (sg.allow_one_pinch_routine) {
    gateButtonsHtml = `
      <button type="button" class="btn btn-safe" id="btn-fastpass-routine">
        ${iconSvg("check")} Accept Routine — Conformal Certified Safe (1-Pinch)
      </button>
    `;
  } else if (gateState === "AMBIGUOUS_HOLD_VOI" && (vr.auto_triggered_channels || []).length > 0) {
    gateButtonsHtml = `
      <button type="button" class="btn btn-hold" id="btn-prefetch-voi">
        ${iconSvg("hold")} Pre-Fetch ${vr.auto_triggered_channels.length} High-VOI Channel(s)
      </button>
      <button type="button" class="btn" id="btn-accept-all-reviewed">
        Accept All 5 Proposals (Manual Review)
      </button>
    `;
  } else {
    gateButtonsHtml = `
      <button type="button" class="btn" disabled title="1-pinch routine fast-pass is locked when clinical sentinels, missing door evaluations, or elevated risk are present.">
        1-Pinch Routine Locked
      </button>
      <button type="button" class="btn btn-crit" id="btn-accept-all-reviewed">
        Commit All 5 Escalated Proposals
      </button>
    `;
  }

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

  const spotdHtml = `
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

  const decisionsHtml = (pkt.recommendations || [])
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

  root.innerHTML = `
    <!-- 1. Patient Arrival & Vital Trajectory -->
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
          <span class="badge ${pkt.ekg_chip?.status === "pending" ? "badge-gate-hold" : "badge-neutral"}" title="${esc(pkt.ekg_chip?.detail || "EKG is strictly a status chip, never ordered here.")}">
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
            <tr>
              <th>HR</th>
              <th>BP</th>
              <th>RR</th>
              <th>SpO2</th>
              <th>Temp</th>
              <th>GCS</th>
            </tr>
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

    <!-- 2. Dual Conformal Safe-Gate Fast-Pass Banner -->
    <section class="safe-gate-banner ${bannerStateCls}" aria-label="Dual Conformal Safe-Gate status">
      <div class="gate-copy">
        <h3 class="gate-title">
          ${iconSvg(gateState === "CERTIFIED_SAFE_ROUTINE" ? "check" : gateState === "AMBIGUOUS_HOLD_VOI" ? "hold" : "alert")}
          <span>${esc(sg.gate_label)}</span>
        </h3>
        <p class="gate-desc">${esc(sg.gate_reason)}</p>
      </div>
      <div class="gate-actions">
        ${gateButtonsHtml}
      </div>
    </section>

    <!-- 3. Non-Waivable Must-Know Sentinels -->
    ${sentinelHtml}

    <!-- 4. Orthogonal 7-Agent Clinical Delta & SPOT DR -->
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
      ${spotdHtml}

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

    <!-- 5. Bounded 5 PITM Decisions Cockpit -->
    <section class="panel-surface decisions-card" aria-label="Five Provider-in-the-Middle decisions">
      <h3 class="section-heading">
        <span>Provider-in-the-Middle 5 Bounded Decisions</span>
        <span class="badge badge-neutral">EKG Never Ordered Here · Override Requires Rationale</span>
      </h3>
      <div class="decision-rows">
        ${decisionsHtml}
      </div>
      <div class="commit-bar">
        <span style="font-size:12.5px;color:var(--ink-secondary);">
          Commits all 5 decisions to the SHA-256 hash-chained ledger for Neyman two-phase shadow auditing.
        </span>
        <button type="button" class="btn btn-primary" id="btn-commit-five">
          Commit 5 Decisions to Ledger
        </button>
      </div>
    </section>
  `;
}

/* --------------------------------------------------------------------------
 * Render Statistical Safety & VOI Rail (Right Column)
 * -------------------------------------------------------------------------- */
function renderSafetyRail() {
  const rail = document.getElementById("safety-rail-root");
  if (!rail) return;
  const pkt = activePacket();
  if (!pkt) {
    rail.innerHTML = "";
    return;
  }

  const sg = pkt.safe_gate || {};
  const vr = pkt.voi_routing || {};
  const channels = vr.channels || [];

  const pct = (v) => `${((Number(v) || 0) * 100).toFixed(1)}%`;

  rail.innerHTML = `
    <div>
      <h2 class="section-heading" style="margin-bottom:8px;">
        <span>Calibrated Risk &amp; Conformal Receipt</span>
        ${vr.cfbh_alert_certified ? `<span class="badge badge-gate-crit">CfBH Alert Certified</span>` : `<span class="badge badge-neutral">Sub-CfBH</span>`}
      </h2>
      <table class="metric-table">
        <tbody>
          <tr>
            <th>Stage-1 Door Posterior p₁(x)</th>
            <td class="num">${pct(sg.p1_calibrated)}</td>
          </tr>
          <tr>
            <th>Framing-Blind Objective p₁,blind(x)</th>
            <td class="num">${pct(sg.p1_framing_blind)}</td>
          </tr>
          <tr>
            <th>Stage-2 Full Chart Posterior p₂(x)</th>
            <td class="num">${pct(sg.p2_calibrated)}</td>
          </tr>
          <tr>
            <th>Bayes Escalation Threshold τ*</th>
            <td class="num">${pct(sg.tau_bayes_triage)}</td>
          </tr>
          <tr>
            <th>Capacity-Adjusted Space τ*space</th>
            <td class="num">${pct(sg.tau_space_capacity_adjusted)}</td>
          </tr>
          <tr>
            <th>Conformal Safe Routine Cutoff τsafe</th>
            <td class="num">${pct(sg.tau_safe_routine)}</td>
          </tr>
          <tr>
            <th>Positive Conformal Miss p-value</th>
            <td class="num">${Number(sg.conformal_miss_pvalue ?? 0).toFixed(4)}</td>
          </tr>
          <tr>
            <th>Cal FOR 95% Wilson Upper Bound</th>
            <td class="num">${pct(sg.cal_for_wilson_upper)}</td>
          </tr>
          <tr>
            <th>Base-Rate Evidence Deficit Dq(x)</th>
            <td class="num">${Number(sg.evidence_deficit_nats ?? 0).toFixed(2)} nats</td>
          </tr>
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

function renderAll() {
  renderCapacityStrip();
  renderQueue();
  renderNowGate();
  renderSafetyRail();
  if (state.xrInstance && typeof state.xrInstance.syncFromAppState === "function") {
    state.xrInstance.syncFromAppState(state);
  }
}

/* --------------------------------------------------------------------------
 * API Actions (Capacity, VOI Pre-Fetch, Ledger Commit, Intake, Audit)
 * -------------------------------------------------------------------------- */
async function loadInitialQueue() {
  const [healthResp, pktResp, ledResp] = await Promise.all([
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

  // Default to flagship multi-mechanism case syn-ED-022 or first packet
  const defaultPkt =
    state.packets.find((p) => p.packet_id === "syn-ED-022") || state.packets[0] || null;
  state.selectedPacketId = defaultPkt ? defaultPkt.packet_id : null;

  const hudEl = document.getElementById("xr-engine-status");
  if (hudEl && healthResp.status === "ok") {
    hudEl.textContent = `Calibrated · AUROC ${healthResp.stage2_test_auroc} · ECE ${healthResp.stage2_test_ece} · τsafe ${(healthResp.tau_safe_routine * 100).toFixed(1)}%`;
  }

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
    // Reset unmodified drafts to reflect updated treatment-space proposals
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
      showToast(`Override rationale for "${DECISION_TITLES[rec.type]}" must be at least 8 characters.`);
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
  body.textContent = "Computing Stage-A Neyman Two-Phase, Stage-B Martingale VOI / CfBH, and Stage-C Subgroup Calibration…";

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
 * XR Blocks 3D Spatial Integration (xrblocks@0.21.1 + three@0.184.0)
 * -------------------------------------------------------------------------- */
async function initSpatialXRIfAvailable() {
  const host = document.getElementById("xr-canvas-host");
  if (!host) return;

  try {
    const [THREE, xb] = await Promise.all([import("three"), import("xrblocks")]);

    class EDTriagePITMApp extends xb.Script {
      constructor() {
        super();
        this.ringGroup = new THREE.Group();
        this.disposables = [];
        this.headerText = null;
        this.subText = null;
      }

      init() {
        this.add(this.ringGroup);

        // Ambient non-interactive clinical floor halo (pointerEvents: 'none')
        const ringGeo = new THREE.RingGeometry(1.15, 1.22, 64);
        const ringMat = new THREE.MeshBasicMaterial({
          color: 0x1b4d78,
          side: THREE.DoubleSide,
          transparent: true,
          opacity: 0.28,
        });
        const floorRing = new THREE.Mesh(ringGeo, ringMat);
        floorRing.rotation.x = -Math.PI / 2;
        floorRing.position.set(0, 0.65, -1.6);
        floorRing.userData.pointerEvents = "none";
        this.add(floorRing);

        // Spatial Now Gate Summary Card
        if (xb.UICard && xb.UIText) {
          const card = new xb.UICard({
            width: 1.1,
            height: 0.48,
            backgroundColor: "#FFFFFF",
            borderRadius: 0.03,
          });
          card.position.set(0, 1.42, -1.55);
          this.headerText = new xb.UIText({
            text: "EDimbESS Spatial Now Gate",
            fontSize: 0.042,
            color: "#1C2430",
          });
          this.subText = new xb.UIText({
            text: "Calibrated Safe-Gate & Framing-Blind Queue Ring",
            fontSize: 0.028,
            color: "#434E5E",
          });
          card.add(this.headerText);
          card.add(this.subText);
          this.add(card);
        }

        this.syncFromAppState(state);
      }

      clearRingDisposables() {
        while (this.ringGroup.children.length > 0) {
          const child = this.ringGroup.children.pop();
          this.ringGroup.remove(child);
        }
        for (const item of this.disposables) {
          if (item && typeof item.dispose === "function") item.dispose();
        }
        this.disposables = [];
      }

      syncFromAppState(appState) {
        this.clearRingDisposables();
        const pkts = appState.packets || [];
        const active = activePacket();

        if (active && this.headerText && typeof this.headerText.setText === "function") {
          this.headerText.setText(`${active.packet_id} · ${active.encounter?.chief_complaint || ""}`);
          this.subText?.setText(
            `${active.safe_gate?.gate_label || ""} · p2=${((active.safe_gate?.p2_calibrated || 0) * 100).toFixed(1)}%`
          );
        }

        const n = Math.max(1, pkts.length);
        pkts.forEach((pkt, idx) => {
          const angle = (idx / n) * Math.PI * 2 - Math.PI / 2;
          const radius = 1.18;
          // Height driven strictly by calibrated Framing-Blind / Stage-2 posterior (never raw badge counts!)
          const pCal = Math.max(
            pkt.safe_gate?.p1_framing_blind || 0.02,
            pkt.safe_gate?.p2_calibrated || 0.02
          );
          const h = 0.08 + 0.42 * Math.min(1, pCal);
          const gate = pkt.safe_gate?.gate_state;
          const hex =
            gate === "CERTIFIED_SAFE_ROUTINE"
              ? 0x1e5631
              : gate === "AMBIGUOUS_HOLD_VOI"
              ? 0x9a6700
              : 0x9e2020;

          const geo = new THREE.CylinderGeometry(0.036, 0.036, h, 20);
          const mat = new THREE.MeshStandardMaterial({
            color: hex,
            roughness: 0.35,
            metalness: 0.1,
            emissive: pkt.packet_id === appState.selectedPacketId ? hex : 0x000000,
            emissiveIntensity: pkt.packet_id === appState.selectedPacketId ? 0.35 : 0.0,
          });
          this.disposables.push(geo, mat);

          const totem = new THREE.Mesh(geo, mat);
          totem.position.set(
            Math.cos(angle) * radius,
            0.65 + h / 2,
            -1.6 + Math.sin(angle) * radius
          );
          totem.userData = {
            packetId: pkt.packet_id,
            pointerEvents: "auto",
            manipulation: true,
          };
          totem.onSelect = (ev) => {
            if (ev && typeof ev.stopPropagation === "function") ev.stopPropagation();
            state.selectedPacketId = pkt.packet_id;
            renderAll();
          };
          this.ringGroup.add(totem);
        });
      }

      dispose() {
        this.clearRingDisposables();
      }
    }

    const scriptInstance = new EDTriagePITMApp();
    state.xrInstance = scriptInstance;
    if (typeof xb.init === "function") {
      const opts = xb.Options ? new xb.Options() : {};
      opts.container = host;
      await xb.init(opts);
      if (xb.core?.scene) {
        xb.core.scene.add(scriptInstance);
        scriptInstance.init();
      }
    }
  } catch {
    // Offline or non-WebGL headless test browser: 2D Clinical Daylight Workbench remains 100% active.
  }
}

/* --------------------------------------------------------------------------
 * Event Delegation
 * -------------------------------------------------------------------------- */
function bindEvents() {
  document.addEventListener("click", (ev) => {
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
      const dlg = document.getElementById(closeDlgBtn.dataset.closeDialog);
      if (dlg) dlg.close();
      return;
    }

    if (ev.target.closest("#btn-fastpass-routine") || ev.target.closest("#btn-accept-all-reviewed")) {
      commitDecisionsForActivePacket(true);
      return;
    }

    if (ev.target.closest("#btn-commit-five")) {
      commitDecisionsForActivePacket(false);
      return;
    }

    if (ev.target.closest("#btn-prefetch-voi")) {
      prefetchHighVoiChannels();
      return;
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

  document.getElementById("btn-toggle-spatial")?.addEventListener("click", (ev) => {
    state.spatialMode = !state.spatialMode;
    document.body.classList.toggle("xr-spatial-mode", state.spatialMode);
    const btn = ev.currentTarget;
    btn.setAttribute("aria-pressed", String(state.spatialMode));
    const lbl = document.getElementById("spatial-btn-label");
    if (lbl) lbl.textContent = state.spatialMode ? "Return to Clinical Desk" : "3D Spatial XR";
    if (state.spatialMode && !state.xrInstance) {
      initSpatialXRIfAvailable();
    }
  });
}

bindEvents();
loadInitialQueue().then(() => {
  initSpatialXRIfAvailable();
});

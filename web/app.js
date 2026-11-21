/**
 * EDimbESS Provider-in-the-Middle (PITM) Crossroads & 3D Spatial XR Controller
 *
 * Realizes True PITM Topology across both 2D and 3D surfaces:
 *   Upstream Inputs (Left) -> Provider Decision Gate (Center) -> Downstream Outputs & Remaining Tasks (Right)
 *
 * - Both Upstream Inputs and Downstream Outputs remain permanently visible even when 0 decisions
 *   have been placed yet, explicitly distinguishing:
 *     1. Already Placed at Door Triage (solid green: ECG done, arrival team evals done, active door orders/meds)
 *     2. Staged / Unplaced PITM Outputs & Remaining Checks (dashed amber/red: awaiting provider release or 1-tap check)
 *     3. Department Station Pipeline (`X placed · Y remaining` across Alert Team, Bed Board, Orders & Meds, Checks & Fast)
 * - Tab 3 (Statistical Engine) isolates all calibration, conformal, G_{m|S}, and 9-node Gauss-Hermite VOI math.
 */

const STATIONS = [
  { id: "alert_team", label: "Alert & Resus Team", short: "Alert Team", dotCls: "dot-alert", hex: 0xc9352b },
  { id: "bed_board", label: "Charge Nurse & Bed Board", short: "Bed Board", dotCls: "dot-bed", hex: 0x3b82f6 },
  { id: "wr_orders", label: "WR Orders & Meds", short: "Orders & Meds", dotCls: "dot-orders", hex: 0xeab308 },
  { id: "fast_track", label: "Checks & Fast Track", short: "Checks & Fast", dotCls: "dot-fast", hex: 0x22c55e },
];

const SPACE_CHOICES = [
  ["immediate", "Immediate Resus Bay"],
  ["next_bed", "Next Staffed Bed"],
  ["bed", "Staffed ED Bed"],
  ["hallway", "Monitored Hallway"],
  ["fast_track", "Fast Track"],
  ["waiting_room", "Waitboard"],
];

const PRESET_OVERRIDE_REASONS = [
  "Chart sentinel or vital trajectory warrants higher acuity",
  "Bedside exam and repeat vitals stable for waiting-room pathway",
  "Anticoagulation, renal, or hepatic medication contraindication",
  "Direct verbal handoff completed with charge nurse / team",
];

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
  viewMode: "crossroads", // "crossroads" | "spatial" | "engine"
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
  activeFilter: "PENDING",
  wingMode: null, // null | "details" | "ask" | "modify" | "station:alert_team" ...
  focusedStationId: null, // opens station backlog card in 2D right column
  reviewedPacketIds: new Set(),
  routedHistory: [], // [{ packetId, ageSex, chief, headline, routes, modified, ts }]
  routedByStation: {
    alert_team: [],
    bed_board: [],
    wr_orders: [],
    fast_track: [],
  },
  dispatchedQueriesByPacket: {}, // { [packetId]: [{ id, label, prompt, timeStr }] }
  stats: {
    reviewed: 0,
    decisions: 0,
    hardTriggers: 0,
    fastPassed: 0,
    overrides: 0,
    asksSent: 0,
  },
  modifyDraft: null, // { packetId, esi, space, evalNeed, holdOrders, medChoice, reason }
  ledgerCount: 0,
  spatialArena: null,
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

let toastTimer = null;
function showToast(msg) {
  const el = document.getElementById("toast-banner");
  if (!el) return;
  el.textContent = msg;
  el.hidden = false;
  if (toastTimer) clearTimeout(toastTimer);
  toastTimer = setTimeout(() => {
    el.hidden = true;
  }, 3800);
}

function esiNum(esiStr) {
  const m = /\d/.exec(String(esiStr || "3"));
  return m ? Number(m[0]) : 3;
}

function esiBadgeClass(esiStr) {
  return `badge badge-esi-${esiNum(esiStr)}`;
}

function clinicalGateBadge(pkt, dark = false) {
  const cr = pkt.clinical_routing || {};
  const gs = pkt.safe_gate?.gate_state;
  if (cr.hard_trigger?.active) {
    return `<span class="${dark ? "xr-why-chip crit" : "badge badge-gate-crit"}">Hard Trigger</span>`;
  }
  if (gs === "CERTIFIED_SAFE_ROUTINE") {
    return `<span class="badge badge-gate-safe">Safe Fast-Pass</span>`;
  }
  if (gs === "AMBIGUOUS_HOLD_VOI") {
    return `<span class="${dark ? "xr-why-chip" : "badge badge-gate-hold"}">Needs Check / Sign-Off</span>`;
  }
  return `<span class="${dark ? "xr-why-chip crit" : "badge badge-gate-crit"}">Escalate Review</span>`;
}

function inputStatusBadge(status, dark = false) {
  if (status === "conflict") {
    return `<span class="${dark ? "xr-why-chip crit" : "badge badge-gate-crit"}">Chart Conflict</span>`;
  }
  if (status === "gap") {
    return `<span class="${dark ? "xr-why-chip crit" : "badge badge-gate-crit"}">Missing Input</span>`;
  }
  if (status === "alert") {
    return `<span class="${dark ? "xr-why-chip" : "badge badge-gate-hold"}">Risk Signal</span>`;
  }
  return `<span class="badge badge-gate-safe">Verified</span>`;
}

function sortedPackets() {
  const tierWeight = (p) => {
    if (state.reviewedPacketIds.has(p.packet_id)) return 9;
    if (p.clinical_routing?.hard_trigger?.active) return 0;
    if (p.safe_gate?.gate_state === "SENTINEL_ESCALATION") return 1;
    if (p.safe_gate?.gate_state === "AMBIGUOUS_HOLD_VOI") return 2;
    return 3;
  };
  return [...state.packets].sort((a, b) => {
    const tw = tierWeight(a) - tierWeight(b);
    if (tw !== 0) return tw;
    const ed =
      esiNum(a.recommendations?.[0]?.value?.triage_status) -
      esiNum(b.recommendations?.[0]?.value?.triage_status);
    if (ed !== 0) return ed;
    return (b.encounter?.arrival_minutes_ago || 0) - (a.encounter?.arrival_minutes_ago || 0);
  });
}

function activePacket() {
  const ordered = sortedPackets();
  return ordered.find((p) => p.packet_id === state.selectedPacketId) || ordered[0] || null;
}

function ensureModifyDraft(pkt) {
  if (!pkt) return null;
  if (!state.modifyDraft || state.modifyDraft.packetId !== pkt.packet_id) {
    const recs = pkt.recommendations || [];
    state.modifyDraft = {
      packetId: pkt.packet_id,
      esi: recs[0]?.value?.triage_status || pkt.encounter?.triage_status || "ESI-3",
      evalNeed: (recs[1]?.value?.needs || [])[0] || "none",
      space: recs[2]?.value?.priority || "bed",
      holdOrders: false,
      medChoice: "recommended",
      reason: PRESET_OVERRIDE_REASONS[0],
    };
  }
  return state.modifyDraft;
}

function getInputStreams(pkt) {
  const cr = pkt?.clinical_routing || {};
  if (Array.isArray(cr.input_streams) && cr.input_streams.length > 0) {
    return cr.input_streams;
  }
  const enc = pkt?.encounter || {};
  const dos = pkt?.dossier || {};
  const meds = (dos.current_meds || []).filter((m) => String(m).toLowerCase() !== "none recorded");
  const labs = dos.recent_labs || [];
  return [
    {
      id: "door_triage",
      label: "Door & EMS Intake",
      status: pkt?.ekg_chip?.status === "pending" ? "gap" : "complete",
      summary: `${enc.triage_status || "ESI-3"} · ${enc.arrival_mode || "walk-in"} · ${pkt?.ekg_chip?.label || "EKG N/A"}`,
      detail: enc.nurse_hx_summary || "No triage note recorded.",
    },
    {
      id: "chart_pharmacy",
      label: "Chart & Pharmacy",
      status: meds.length > 0 ? "alert" : "complete",
      summary: `${meds.length} active med(s) · ${String(dos.pmh_summary || "No PMH").split(".")[0]}`,
      detail: `PMH: ${dos.pmh_summary || "None"} | Meds: ${meds.join(", ") || "None"}`,
    },
    {
      id: "labs_micro",
      label: "Labs, Micro & HIE",
      status: enc.outside_hie_available === false ? "gap" : "complete",
      summary: labs.length > 0 ? `${labs[labs.length - 1].test}: ${labs[labs.length - 1].value}` : "No recent labs",
      detail: labs.map((l) => `${l.test} ${l.value} (${l.when})`).join(" · ") || "No prior labs on file.",
    },
  ];
}

function getAlreadyPlacedAtDoor(pkt) {
  const cr = pkt?.clinical_routing || {};
  if (Array.isArray(cr.output_tasks?.already_placed_at_door)) {
    return cr.output_tasks.already_placed_at_door;
  }
  const items = [];
  const enc = pkt?.encounter || {};
  if (pkt?.ekg_chip?.status === "done") {
    items.push(`Triage ECG done (${pkt.ekg_chip.detail || "recorded"})`);
  }
  const evals = enc.evals || {};
  for (const k of ["trauma", "neuro", "d2b"]) {
    if (evals[k] === "done") items.push(`Arrival ${k.toUpperCase()} team eval completed at door`);
  }
  const wrExisting = [...(enc.wr_orders?.sets || []), ...(enc.wr_orders?.addons || [])];
  if (wrExisting.length > 0) items.push(`Door orders active: ${wrExisting.join(", ")}`);
  if ((enc.wr_meds_given || []).length > 0) items.push(`Door meds given: ${enc.wr_meds_given.join(", ")}`);
  return items;
}

/**
 * Computes the effective routing plan for a packet, reflecting live `Modify…`
 * selections immediately in both the Center Gate and Right Staged Outputs.
 */
function effectiveRoutingPlan(pkt) {
  const cr = pkt?.clinical_routing || {};
  const recs = pkt?.recommendations || [];
  const enc = pkt?.encounter || {};
  const baseRoutes = cr.what_happens_next || [];
  const baseHeadline = cr.headline_decision || "Approve Triage Plan";

  const isEditingThis =
    state.wingMode === "modify" &&
    state.modifyDraft &&
    state.modifyDraft.packetId === pkt?.packet_id;

  if (!isEditingThis) {
    return { headline: baseHeadline, routes: baseRoutes, isModifiedDraft: false };
  }

  const d = state.modifyDraft;
  const charted = enc.triage_status || "ESI-3";
  const esiPhrase = d.esi !== charted ? `Upgrade ${charted} -> ${d.esi}` : `Keep ${d.esi}`;
  const spLbl = (SPACE_CHOICES.find(([k]) => k === d.space) || [d.space, d.space])[1];

  const origOrders = recs[3]?.value?.items || [];
  const effOrders = d.holdOrders ? [] : origOrders;
  const origMeds = recs[4]?.value?.meds || [];
  const effMeds =
    d.medChoice === "recommended"
      ? origMeds
      : d.medChoice === "apap_only"
      ? ["Acetaminophen (APAP)"]
      : d.medChoice === "zofran_only"
      ? ["Ondansetron"]
      : [];

  const headParts = [esiPhrase, spLbl];
  if (d.evalNeed !== "none") headParts.push(`Trigger ${d.evalNeed.toUpperCase()} Eval`);
  else if (effOrders.length > 0) {
    headParts.push(`Order ${effOrders.slice(0, 2).join(", ")}${effOrders.length > 2 ? "+" : ""}`);
  } else if (d.holdOrders) {
    headParts.push("Hold WR Labs");
  }

  const routes = [];
  if (d.evalNeed !== "none" || d.esi === "ESI-1" || d.space === "immediate") {
    const bits = [];
    if (d.evalNeed !== "none") bits.push(`Activate ${d.evalNeed.toUpperCase()} team`);
    if (d.esi !== charted) bits.push(`Acuity ${charted} -> ${d.esi}`);
    routes.push({
      station_id: "alert_team",
      station_label: "Alert & Resus Team",
      summary: bits.join(" · ") || `Immediate ${d.esi} resuscitation alert`,
      urgent: true,
    });
  }

  const isoTag = enc.isolation_flag ? ` (${enc.isolation_flag} Isolation)` : "";
  if (
    (d.space === "fast_track" || d.space === "waiting_room") &&
    (d.esi === "ESI-4" || d.esi === "ESI-5" || pkt?.safe_gate?.gate_state === "CERTIFIED_SAFE_ROUTINE")
  ) {
    routes.push({
      station_id: "fast_track",
      station_label: "Checks & Fast Track",
      summary: `${d.esi} -> ${spLbl}`,
      urgent: false,
    });
  } else {
    routes.push({
      station_id: "bed_board",
      station_label: "Charge Nurse & Bed Board",
      summary: `${esiPhrase} -> ${spLbl}${isoTag}`,
      urgent: d.space === "immediate" || d.space === "next_bed" || d.esi !== charted,
    });
  }

  const ordBits = [];
  if (effOrders.length > 0) ordBits.push(`Orders: ${effOrders.join(", ")}`);
  else if (d.holdOrders && origOrders.length > 0) ordBits.push("WR Orders held by provider");

  if (effMeds.length > 0) ordBits.push(`Meds: ${effMeds.map((m) => m.split(" (")[0]).join(", ")}`);
  else if (d.medChoice === "npo_hold") ordBits.push("Oral meds held (NPO)");

  if (ordBits.length > 0) {
    routes.push({
      station_id: "wr_orders",
      station_label: "WR Orders & Meds",
      summary: ordBits.join(" · "),
      urgent: false,
    });
  }

  const anyDiff =
    d.esi !== (recs[0]?.value?.triage_status || charted) ||
    d.evalNeed !== ((recs[1]?.value?.needs || [])[0] || "none") ||
    d.space !== (recs[2]?.value?.priority || "bed") ||
    d.holdOrders ||
    d.medChoice !== "recommended";

  return {
    headline: headParts.join(" · "),
    routes,
    isModifiedDraft: anyDiff,
  };
}

/**
 * Computes department-wide Placed vs. Remaining tasks across all 4 downstream stations
 * so unplaced work is visible even when 0 patients have been routed so far.
 */
function computeStationBacklogs() {
  const remainingByStation = {
    alert_team: [],
    bed_board: [],
    wr_orders: [],
    fast_track: [],
  };
  let totalRemainingChecks = 0;

  const ordered = sortedPackets();
  const curId = activePacket()?.packet_id;

  for (const pkt of ordered) {
    if (state.reviewedPacketIds.has(pkt.packet_id)) continue;
    const enc = pkt.encounter || {};
    const ageSex = `${enc.age_years || ""}${enc.sex || ""}`;
    const plan = pkt.packet_id === curId ? effectiveRoutingPlan(pkt) : { routes: pkt.clinical_routing?.what_happens_next || [] };

    for (const r of plan.routes || []) {
      const bucket = remainingByStation[r.station_id] || remainingByStation.bed_board;
      bucket.push({
        packetId: pkt.packet_id,
        ageSex,
        chief: enc.chief_complaint || "",
        summary: r.summary,
        urgent: Boolean(r.urgent),
        isCheck: false,
        isCurrent: pkt.packet_id === curId,
      });
    }

    const remQueries = (pkt.clinical_routing?.actionable_queries || []).filter((q) => !q.completed);
    for (const q of remQueries) {
      totalRemainingChecks += 1;
      remainingByStation.fast_track.push({
        packetId: pkt.packet_id,
        ageSex,
        chief: enc.chief_complaint || "",
        summary: `${q.button_label}: ${q.prompt}`,
        urgent: q.id === "req_ekg",
        isCheck: true,
        queryId: q.id,
        isCurrent: pkt.packet_id === curId,
      });
    }
  }

  return { remainingByStation, totalRemainingChecks };
}

/* --------------------------------------------------------------------------
 * Top Situational Strip: Station Placed + Remaining Counters + ED Capacity
 * -------------------------------------------------------------------------- */
function renderSituationalStrip() {
  const stHost = document.getElementById("routed-stations-bar");
  const capHost = document.getElementById("capacity-strip");
  const { remainingByStation } = computeStationBacklogs();
  const unplacedPatients = state.packets.filter((p) => !state.reviewedPacketIds.has(p.packet_id)).length;

  if (stHost) {
    stHost.innerHTML =
      `<span class="tabular" style="font-size:12.5px;font-weight:750;color:var(--ink-secondary);margin-right:4px;">Pipeline (${state.stats.reviewed} placed · ${unplacedPatients} rem):</span>` +
      STATIONS.map((st) => {
        const placedCnt = (state.routedByStation[st.id] || []).length;
        const remCnt = (remainingByStation[st.id] || []).length;
        const active =
          state.viewMode === "spatial"
            ? state.wingMode === `station:${st.id}`
            : state.focusedStationId === st.id;
        return `
          <button type="button" class="station-pill" data-open-station="${st.id}" aria-pressed="${active}">
            <span class="station-dot ${st.dotCls}"></span>
            <span>${esc(st.short)}</span>
            <span class="tabular">${placedCnt} placed · ${remCnt} rem</span>
          </button>
        `;
      }).join("");
  }

  if (capHost) {
    const c = state.capacity;
    const units = [
      { key: "bedsOpen", maxKey: "bedsTotal", label: "Beds" },
      { key: "hallwayOpen", maxKey: "hallwayTotal", label: "Hall" },
      { key: "fastTrackOpen", maxKey: "fastTrackTotal", label: "Fast" },
      { key: "waitboard", maxKey: null, label: "Wait" },
    ];
    capHost.innerHTML = units
      .map((u) => {
        const valText = u.maxKey ? `${c[u.key]}/${c[u.maxKey]}` : `${c[u.key]}`;
        return `
          <div class="cap-unit">
            <span class="cap-label">${esc(u.label)}</span>
            <button type="button" class="cap-step" data-cap-key="${u.key}" data-cap-delta="-1" aria-label="Decrease ${esc(u.label)}">-</button>
            <span class="cap-val">${esc(valText)}</span>
            <button type="button" class="cap-step" data-cap-key="${u.key}" data-cap-delta="1" aria-label="Increase ${esc(u.label)}">+</button>
          </div>
        `;
      })
      .join("");
  }
}

/* --------------------------------------------------------------------------
 * Left Column Part A: Active Encounter Upstream Inputs (#active-inputs-root)
 * -------------------------------------------------------------------------- */
function renderActiveInputsColumn() {
  const host = document.getElementById("active-inputs-root");
  if (!host) return;

  const pkt = activePacket();
  if (!pkt) {
    host.innerHTML = `<div class="section-title-bar"><h2>Upstream Inputs</h2></div><p style="margin:6px 0 0;font-size:12.5px;color:var(--ink-muted);">No active encounter selected.</p>`;
    return;
  }

  const streams = getInputStreams(pkt);
  const queries = pkt.clinical_routing?.actionable_queries || [];
  const ekgQuery = queries.find((q) => q.id === "req_ekg" && !q.completed);
  const hieQuery = queries.find((q) => q.id === "ch_hie_outside" && !q.completed);

  host.innerHTML = `
    <div class="col-flow-banner">
      <span>Upstream Inputs (${streams.length})</span>
      <span class="tabular">${esc(pkt.packet_id)} -> Gate</span>
    </div>
    <div class="input-stream-list">
      ${streams
        .map((st) => {
          const autoOpen = st.status === "conflict" || st.status === "gap";
          const inlineBtn =
            st.id === "door_triage" && ekgQuery
              ? `<button type="button" class="btn btn-hold" data-trigger-query="req_ekg" style="margin-top:6px;padding:4px 9px;font-size:12px;width:100%;">${esc(ekgQuery.button_label)}</button>`
              : st.id === "labs_micro" && hieQuery
              ? `<button type="button" class="btn btn-hold" data-trigger-query="ch_hie_outside" style="margin-top:6px;padding:4px 9px;font-size:12px;width:100%;">${esc(hieQuery.button_label)}</button>`
              : "";
          return `
            <details class="input-stream-item status-${esc(st.status)}" ${autoOpen ? "open" : ""}>
              <summary>
                <div class="input-stream-head">
                  <span>${esc(st.label)}</span>
                  ${inputStatusBadge(st.status, false)}
                </div>
                <div class="input-stream-sum">${esc(st.summary)}</div>
              </summary>
              <div class="input-stream-detail">
                <div>${esc(st.detail)}</div>
                ${inlineBtn}
              </div>
            </details>
          `;
        })
        .join("")}
    </div>
  `;
}

/* --------------------------------------------------------------------------
 * Left Column Part B: Incoming Arrival Queue (#queue-list)
 * -------------------------------------------------------------------------- */
function renderQueue() {
  const listEl = document.getElementById("queue-list");
  const countEl = document.getElementById("queue-summary-count");
  if (!listEl || !countEl) return;

  const ordered = sortedPackets();
  const filtered = ordered.filter((pkt) => {
    const isRev = state.reviewedPacketIds.has(pkt.packet_id);
    if (state.activeFilter === "PENDING") return !isRev;
    if (state.activeFilter === "REVIEWED") return isRev;
    if (state.activeFilter === "HARD_TRIGGER") return Boolean(pkt.clinical_routing?.hard_trigger?.active);
    if (state.activeFilter === "CERTIFIED_SAFE_ROUTINE") {
      return pkt.safe_gate?.gate_state === "CERTIFIED_SAFE_ROUTINE";
    }
    return true;
  });

  const pendingTotal = ordered.filter((p) => !state.reviewedPacketIds.has(p.packet_id)).length;
  countEl.textContent = `${pendingTotal} remaining · ${state.reviewedPacketIds.size} placed`;

  if (filtered.length === 0) {
    listEl.innerHTML = `<div style="padding:14px 10px;font-size:12.5px;color:var(--ink-muted);">No encounters in this filter view.</div>`;
    return;
  }

  listEl.innerHTML = filtered
    .map((pkt) => {
      const enc = pkt.encounter || {};
      const charted = enc.triage_status || "ESI-3";
      const proposed = pkt.recommendations?.[0]?.value?.triage_status || charted;
      const esiText = proposed !== charted ? `${charted} -> ${proposed}` : charted;
      const isSelected = pkt.packet_id === state.selectedPacketId;
      const isReviewed = state.reviewedPacketIds.has(pkt.packet_id);
      const leadWhy = (pkt.clinical_routing?.why_queued || [])[0] || "";
      const remChecks = (pkt.clinical_routing?.actionable_queries || []).filter((q) => !q.completed).length;
      const unplacedOutputs = (pkt.clinical_routing?.what_happens_next || []).length;

      return `
        <button type="button" class="queue-item" data-select-packet="${esc(pkt.packet_id)}" aria-current="${isSelected ? "true" : "false"}">
          <div class="q-top">
            <span class="q-id">${esc(pkt.packet_id)}${isReviewed ? " · Placed" : ` · ${unplacedOutputs} unplaced`}</span>
            <span class="q-meta">${esc(enc.age_years)}${esc(enc.sex)} · ${esc(enc.arrival_minutes_ago)}m</span>
          </div>
          <div class="q-chief">${esc(enc.chief_complaint)}</div>
          <div class="q-badges">
            <span class="${esiBadgeClass(proposed)}">${esc(esiText)}</span>
            ${clinicalGateBadge(pkt)}
            ${!isReviewed && remChecks > 0 ? `<span class="badge badge-neutral">${remChecks} check(s)</span>` : ""}
          </div>
          ${leadWhy ? `<div style="margin-top:5px;font-size:12px;color:var(--ink-secondary);">${esc(leadWhy)}</div>` : ""}
        </button>
      `;
    })
    .join("");
}

/* --------------------------------------------------------------------------
 * Shared Drawer / Spatial Wing Content Builder (Modify, Ask-Next, Details, Station)
 * -------------------------------------------------------------------------- */
function buildDrawerOrWingHtml(pkt, dark = false) {
  if (!state.wingMode) return "";

  if (state.wingMode.startsWith("station:")) {
    const stId = state.wingMode.split(":")[1];
    const stObj = STATIONS.find((s) => s.id === stId) || STATIONS[0];
    const placed = state.routedByStation[stId] || [];
    const { remainingByStation } = computeStationBacklogs();
    const rem = remainingByStation[stId] || [];

    return `
      <div class="drawer-head">
        <h3 class="drawer-title">${esc(stObj.label)} (${placed.length} placed · ${rem.length} rem)</h3>
        <button type="button" class="${dark ? "xr-mini-btn" : "btn"}" data-close-wing>Back to Inputs</button>
      </div>
      <div style="font-size:12px;font-weight:750;margin-bottom:4px;color:${dark ? "#FDE047" : "var(--ink-hold)"};">
        Remaining Unplaced in Queue (${rem.length})
      </div>
      ${
        rem.length === 0
          ? `<p style="margin:0 0 8px;opacity:0.8;">0 remaining unplaced tasks for ${esc(stObj.short)}.</p>`
          : rem
              .slice(0, 6)
              .map(
                (it) => `
              <button type="button" class="${dark ? "xr-upnext-item" : "rem-jump-btn"}" data-select-packet="${esc(it.packetId)}">
                <div style="display:flex;justify-content:space-between;gap:6px;">
                  <strong>${esc(it.packetId)} · ${esc(it.ageSex)}</strong>
                  <span>Unplaced</span>
                </div>
                <div>${esc(it.summary)}</div>
              </button>
            `
              )
              .join("")
      }
      <div style="font-size:12px;font-weight:750;margin:10px 0 4px;color:${dark ? "#86EFAC" : "var(--ink-safe)"};">
        Placed This Session (${placed.length})
      </div>
      ${
        placed.length === 0
          ? `<p style="margin:0;opacity:0.8;">0 placed so far. Approve a patient in the Center Gate to release outputs here.</p>`
          : placed
              .slice()
              .reverse()
              .map(
                (it) => `
              <div class="${dark ? "xr-io-row placed" : "output-task-row placed"}" style="margin-bottom:5px;">
                <div style="display:flex;justify-content:space-between;gap:6px;">
                  <strong>${esc(it.packetId)} · ${esc(it.ageSex)}</strong>
                  <span class="tabular">${esc(it.timeStr)}</span>
                </div>
                <div>-> ${esc(it.summary)}</div>
              </div>
            `
              )
              .join("")
      }
    `;
  }

  if (!pkt) return "";
  const enc = pkt.encounter || {};
  const dos = pkt.dossier || {};
  const cr = pkt.clinical_routing || {};
  const queries = cr.actionable_queries || [];

  if (state.wingMode === "ask") {
    return `
      <div class="drawer-head">
        <h3 class="drawer-title">Remaining Checks &amp; Bedside Questions · ${esc(pkt.packet_id)}</h3>
        <button type="button" class="${dark ? "xr-mini-btn" : "btn"}" data-close-wing>Close</button>
      </div>
      ${
        queries.length === 0
          ? `<p style="margin:0;opacity:0.85;">No missing ECG, outside hospital pull, or bedside question remaining for this encounter.</p>`
          : queries
              .map(
                (q) => `
              <div class="query-row" style="${dark ? "background:rgba(15,23,42,0.65);border-color:rgba(148,163,184,0.34);color:#F8FAFC;" : ""}">
                <div style="display:flex;justify-content:space-between;align-items:center;gap:8px;">
                  <strong style="color:${dark ? "#7DD3FC" : "var(--accent)"};">${esc(q.kind === "bedside_question" ? "Bedside Question" : q.kind === "nursing_check" ? "Nursing Door Check" : "Outside Hospital Feed")}</strong>
                  ${
                    q.completed
                      ? `<span class="badge badge-gate-safe">Dispatched</span>`
                      : `<button type="button" class="btn btn-primary" data-trigger-query="${esc(q.id)}" style="padding:4px 10px;font-size:12px;">${esc(q.button_label)}</button>`
                  }
                </div>
                <div style="font-weight:650;">${esc(q.prompt)}</div>
                <div style="font-size:12px;opacity:0.85;"><strong>What flips:</strong> ${esc(q.if_positive_impact)}</div>
              </div>
            `
              )
              .join("")
      }
    `;
  }

  if (state.wingMode === "modify") {
    const d = ensureModifyDraft(pkt);
    const pillStyle = dark
      ? "background:rgba(15,23,42,0.7);color:#F8FAFC;border-color:rgba(148,163,184,0.4);"
      : "";
    return `
      <div class="drawer-head">
        <h3 class="drawer-title">Modify Staged Outputs (1-Tap, Zero Typing) · ${esc(pkt.packet_id)}</h3>
        <button type="button" class="${dark ? "xr-mini-btn" : "btn"}" data-close-wing>Cancel</button>
      </div>

      <div class="pill-picker-group">
        <strong>1. Triage Acuity</strong>
        <div class="pill-picker-row">
          ${["ESI-1", "ESI-2", "ESI-3", "ESI-4", "ESI-5"]
            .map(
              (e) =>
                `<button type="button" class="pick-pill" style="${d.esi === e ? "" : pillStyle}" data-mod-field="esi" data-mod-val="${e}" aria-pressed="${d.esi === e}">${e}</button>`
            )
            .join("")}
        </div>
      </div>

      <div class="pill-picker-group">
        <strong>2. Treatment Space</strong>
        <div class="pill-picker-row">
          ${SPACE_CHOICES.map(
            ([val, lbl]) =>
              `<button type="button" class="pick-pill" style="${d.space === val ? "" : pillStyle}" data-mod-field="space" data-mod-val="${val}" aria-pressed="${d.space === val}">${lbl}</button>`
          ).join("")}
        </div>
      </div>

      <div class="pill-picker-group">
        <strong>3. Arrival Door Eval</strong>
        <div class="pill-picker-row">
          ${[
            ["none", "No Upgrade"],
            ["trauma", "Trauma Eval"],
            ["neuro", "Stroke / Neuro"],
            ["d2b", "D2B STEMI"],
          ]
            .map(
              ([val, lbl]) =>
                `<button type="button" class="pick-pill" style="${d.evalNeed === val ? "" : pillStyle}" data-mod-field="evalNeed" data-mod-val="${val}" aria-pressed="${d.evalNeed === val}">${lbl}</button>`
            )
            .join("")}
        </div>
      </div>

      <div class="pill-picker-group">
        <strong>4. Waiting-Room Orders &amp; Meds</strong>
        <div class="pill-picker-row">
          <button type="button" class="pick-pill" style="${!d.holdOrders ? "" : pillStyle}" data-mod-field="holdOrders" data-mod-val="false" aria-pressed="${!d.holdOrders}">Release Standing Labs</button>
          <button type="button" class="pick-pill" style="${d.holdOrders ? "" : pillStyle}" data-mod-field="holdOrders" data-mod-val="true" aria-pressed="${d.holdOrders}">Hold WR Labs</button>
        </div>
        <div class="pill-picker-row" style="margin-top:4px;">
          ${[
            ["recommended", "Recommended Meds"],
            ["apap_only", "APAP Only"],
            ["zofran_only", "Ondansetron Only"],
            ["npo_hold", "Hold Oral Meds (NPO)"],
          ]
            .map(
              ([val, lbl]) =>
                `<button type="button" class="pick-pill" style="${d.medChoice === val ? "" : pillStyle}" data-mod-field="medChoice" data-mod-val="${val}" aria-pressed="${d.medChoice === val}">${lbl}</button>`
            )
            .join("")}
        </div>
      </div>

      <div class="pill-picker-group">
        <strong>5. Clinical Reason (SHA-256 Ledger)</strong>
        <select id="mod-reason-select" style="width:100%;padding:6px 8px;border-radius:6px;border:1px solid var(--border-strong);background:#FFFFFF;color:#18222F;font-size:12.5px;">
          ${PRESET_OVERRIDE_REASONS.map(
            (r) => `<option value="${esc(r)}" ${d.reason === r ? "selected" : ""}>${esc(r)}</option>`
          ).join("")}
        </select>
      </div>

      <button type="button" class="btn btn-primary" data-action="commit-modified" style="width:100%;margin-top:4px;">
        Place &amp; Route Modified Outputs
      </button>
    `;
  }

  const visits = (dos.last_ed_visits || []).map((v) => `${v.when}: ${v.summary}`).join(" · ") || "None recorded";
  const labs = (dos.recent_labs || []).map((l) => `${l.test} ${l.value} (${l.when})`).join(" · ") || "None recorded";
  const imaging = (dos.recent_di || []).map((d) => `${d.study} (${d.when}): ${d.summary}`).join(" · ") || "None recorded";
  const spotd = pkt.swarm?.spotd || [];

  return `
    <div class="drawer-head">
      <h3 class="drawer-title">Full Chart Dossier &amp; SPOT DR · ${esc(pkt.packet_id)}</h3>
      <button type="button" class="${dark ? "xr-mini-btn" : "btn"}" data-close-wing>Close</button>
    </div>
    <div style="display:flex;flex-direction:column;gap:7px;font-size:12.5px;">
      <div><strong>Nurse Triage Note:</strong> ${esc(enc.nurse_hx_summary)}</div>
      <div><strong>PMH:</strong> ${esc(dos.pmh_summary)} · <strong>Surgical:</strong> ${esc(dos.surgical_hx_summary)}</div>
      <div><strong>Active Medications:</strong> ${esc((dos.current_meds || []).join(", "))}</div>
      <div><strong>Social / Care Context:</strong> ${esc(dos.social_hx_summary)}</div>
      <div><strong>Prior ED Visits:</strong> ${esc(visits)}</div>
      <div><strong>Recent Labs / Micro:</strong> ${esc(labs)}</div>
      <div><strong>Recent Imaging:</strong> ${esc(imaging)}</div>
    </div>
    <div style="margin-top:8px;">
      <strong style="font-size:12.5px;">SPOT DR Differential &amp; Risk Checklist</strong>
      <div class="spotd-grid">
        ${spotd
          .map(
            (c) => `
          <div class="spotd-cell ${c.chart && !dark ? "chart-backed" : ""}" style="${dark ? "background:rgba(15,23,42,0.6);border-color:rgba(148,163,184,0.3);color:#F8FAFC;" : ""}">
            <strong>${esc(c.letter)} · ${esc(c.label)}</strong>
            <div style="margin-top:2px;">${esc(c.phrase)}</div>
          </div>
        `
          )
          .join("")}
      </div>
    </div>
  `;
}

/* --------------------------------------------------------------------------
 * Center Column: Provider-in-the-Middle Decision Gate (#crossroads-now-root)
 * -------------------------------------------------------------------------- */
function renderCrossroadsCenter() {
  const centerEl = document.getElementById("crossroads-now-root");
  if (!centerEl) return;

  const pkt = activePacket();
  if (!pkt) {
    centerEl.innerHTML = `<div class="panel-surface now-card-body"><h2>All arrivals placed and routed</h2></div>`;
    return;
  }

  const enc = pkt.encounter || {};
  const dos = pkt.dossier || {};
  const vit = enc.vitals || {};
  const prior = (enc.prior_vitals || [])[0] || null;
  const cr = pkt.clinical_routing || {};
  const sg = pkt.safe_gate || {};
  const plan = effectiveRoutingPlan(pkt);
  const routes = plan.routes || [];
  const queries = (cr.actionable_queries || []).filter((q) => !q.completed);
  const medsClean = (dos.current_meds || []).join(", ") || "None recorded";
  const isReviewed = state.reviewedPacketIds.has(pkt.packet_id);
  const streams = getInputStreams(pkt);

  const vChip = (lbl, val, pVal, abn) => {
    const tr = pVal !== undefined && pVal !== null && pVal !== val ? ` (from ${pVal})` : "";
    return `<span class="vital-chip ${abn ? "abn" : ""}"><strong>${esc(lbl)}</strong> ${esc(val)}${esc(tr)}</span>`;
  };

  const targetStationsStr = routes.map((r) => r.station_label).join(" + ");
  const approveLabel = isReviewed
    ? "Re-Send Outputs"
    : plan.isModifiedDraft
    ? "Place Modified Plan"
    : cr.hard_trigger?.active
    ? "Escalate & Place Outputs Now"
    : sg.allow_one_pinch_routine
    ? "Fast-Pass & Place Outputs"
    : "Approve & Place Outputs";
  const approveCls = cr.hard_trigger?.active
    ? "btn-crit"
    : sg.allow_one_pinch_routine && !plan.isModifiedDraft
    ? "btn-safe"
    : "btn-primary";

  const inlineDrawerAllowed =
    state.wingMode === "modify" || state.wingMode === "ask" || state.wingMode === "details";

  centerEl.innerHTML = `
    <div class="col-flow-banner">
      <span><- ${streams.length} Upstream Inputs</span>
      <span>PROVIDER IN THE MIDDLE · ${esc(pkt.packet_id)}</span>
      <span>${isReviewed ? "Outputs Placed ->" : `${routes.length + queries.length} Tasks Remaining ->`}</span>
    </div>

    <article class="panel-surface now-card">
      ${
        cr.hard_trigger?.active
          ? `<div class="hard-trigger-banner">
              <span>${esc(cr.hard_trigger.banner)}</span>
              <span class="badge" style="background:rgba(255,255,255,0.2);color:#FFFFFF;">Bypass External Wait</span>
            </div>`
          : ""
      }
      <div class="now-card-body">
        <!-- 1. Patient Presentation First -->
        <div style="display:flex;justify-content:space-between;align-items:baseline;gap:10px;flex-wrap:wrap;">
          <h2 class="patient-headline">
            ${esc(enc.age_years)}y ${esc(enc.sex)} · ${esc(enc.chief_complaint)}
            <span style="font-size:14px;font-weight:500;color:var(--ink-muted);">(${esc(enc.arrival_mode)} · waiting ${esc(enc.arrival_minutes_ago)}m)</span>
          </h2>
          <span class="badge ${isReviewed ? "badge-gate-safe" : "badge-gate-hold"}">${isReviewed ? "Placed" : "Unplaced at Gate"}</span>
        </div>

        <!-- 2. Vitals Trajectory -->
        <div class="vitals-inline-row">
          ${vChip("HR", `${vit.hr} bpm`, prior?.hr, vit.hr >= 100 || vit.hr <= 50)}
          ${vChip("BP", `${vit.sbp}/${vit.dbp}`, prior ? `${prior.sbp}/${prior.dbp}` : null, vit.sbp < 95)}
          ${vChip("RR", `${vit.rr}/m`, prior?.rr, vit.rr >= 22)}
          ${vChip("SpO2", `${vit.spo2}%`, prior?.spo2, vit.spo2 < 94)}
          ${vChip("T", `${vit.temp_c}°C`, prior?.temp_c, vit.temp_c >= 38.0 || vit.temp_c < 36.0)}
          ${(vit.gcs ?? 15) < 15 ? vChip("GCS", vit.gcs, prior?.gcs, true) : ""}
        </div>

        <!-- 3. PMH & Active Meds -->
        <p class="pmh-meds-line">
          <strong>PMH:</strong> ${esc(dos.pmh_summary)} · <strong>Active Meds:</strong> ${esc(medsClean)}
        </p>

        <!-- 4. Proposed Decision & Downstream Outputs (Live-linked to Modify pills & Right Column) -->
        <div class="decision-call-box">
          <p class="decision-call-title">
            ${plan.isModifiedDraft ? "Modified Draft: " : "Decision: "}${esc(plan.headline)}
          </p>
          <div style="font-size:12px;font-weight:750;color:var(--ink-muted);margin-bottom:4px;">
            ${isReviewed ? "OUTPUTS PLACED BY PROVIDER:" : "UNPLACED OUTPUTS STAGED FOR RELEASE ON APPROVE:"}
          </div>
          <ul class="what-next-list">
            ${routes
              .map(
                (r) => `
              <li class="what-next-item ${r.urgent ? "urgent" : ""}">
                <span>-> <strong>${esc(r.station_label)}:</strong> ${esc(r.summary)}</span>
              </li>
            `
              )
              .join("")}
          </ul>
        </div>

        <!-- 5. Why Queued -->
        <div class="why-queued-row">
          <span style="font-size:12px;font-weight:750;color:var(--ink-muted);">WHY QUEUED:</span>
          ${(cr.why_queued || [])
            .map(
              (ch) =>
                `<span class="why-chip ${cr.hard_trigger?.active || (pkt.sentinels || []).length > 0 ? "crit" : ""}">${esc(ch)}</span>`
            )
            .join("")}
        </div>

        <!-- 6. Route Header & Intake Baseline Badges -->
        <div class="route-header-row" style="padding-top:4px;border-top:1px solid var(--border);">
          <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;">
            <span class="now-pill">NOW</span>
            <span style="font-size:13px;font-weight:650;color:var(--ink-secondary);">
              Inputs (${streams.length}) -> Provider Gate -> ${esc(targetStationsStr)}
            </span>
          </div>
          <div style="display:flex;gap:6px;align-items:center;flex-wrap:wrap;">
            <span class="${esiBadgeClass(enc.triage_status)}">Charted ${esc(enc.triage_status)}</span>
            <span class="badge ${pkt.ekg_chip?.status === "pending" ? "badge-gate-hold" : "badge-neutral"}">${esc(pkt.ekg_chip?.label || "EKG N/A")}</span>
            ${clinicalGateBadge(pkt)}
          </div>
        </div>

        <!-- 7. If Missed Consequence -->
        ${
          cr.if_missed_risk
            ? `<p class="if-missed-line"><strong>IF MISSED:</strong> ${esc(cr.if_missed_risk)}</p>`
            : ""
        }

        <!-- 8. Action Bar -->
        <div class="crossroads-action-bar">
          <div class="primary-btn-cluster">
            <button type="button" class="btn ${approveCls}" data-action="${plan.isModifiedDraft ? "commit-modified" : "approve-route"}">
              ${esc(approveLabel)}
            </button>
            <button type="button" class="btn" data-toggle-wing="modify" aria-pressed="${state.wingMode === "modify"}">
              ${state.wingMode === "modify" ? "Close Modify" : "Modify…"}
            </button>
          </div>
          <div class="secondary-btn-cluster">
            ${
              queries.length > 0
                ? `<button type="button" class="btn btn-hold" data-toggle-wing="ask" aria-pressed="${state.wingMode === "ask"}">
                    Remaining Checks (${queries.length})
                  </button>`
                : ""
            }
            <button type="button" class="btn" data-toggle-wing="details" aria-pressed="${state.wingMode === "details"}">
              ${state.wingMode === "details" ? "Hide Details" : "Chart Details"}
            </button>
          </div>
        </div>

        <!-- 9. Inline Drawer (Keeps Left Inputs & Right Outputs 100% Visible!) -->
        ${
          inlineDrawerAllowed
            ? `<div class="inline-drawer-box">${buildDrawerOrWingHtml(pkt, false)}</div>`
            : ""
        }
      </div>
    </article>
  `;
}

/* --------------------------------------------------------------------------
 * Right Column: Always-Visible Downstream Outputs & Remaining Tasks (#outputs-pipeline-root)
 * -------------------------------------------------------------------------- */
function renderOutputsColumn() {
  const host = document.getElementById("outputs-pipeline-root");
  if (!host) return;

  const pkt = activePacket();
  const { remainingByStation } = computeStationBacklogs();

  let activePatientCardHtml = "";
  if (pkt) {
    const isReviewed = state.reviewedPacketIds.has(pkt.packet_id);
    const plan = effectiveRoutingPlan(pkt);
    const routes = plan.routes || [];
    const queries = pkt.clinical_routing?.actionable_queries || [];
    const remQueries = queries.filter((q) => !q.completed);
    const doneQueries = state.dispatchedQueriesByPacket[pkt.packet_id] || [];
    const doorPlaced = getAlreadyPlacedAtDoor(pkt);

    const committedForPkt = STATIONS.flatMap((st) =>
      (state.routedByStation[st.id] || [])
        .filter((it) => it.packetId === pkt.packet_id && !it.isCheck)
        .map((it) => ({ ...it, stationLabel: st.label }))
    );

    const unplacedTotal = (isReviewed ? 0 : routes.length) + remQueries.length;

    activePatientCardHtml = `
      <section class="panel-surface outputs-card">
        <div class="section-title-bar">
          <h2>Active Patient Outputs · ${esc(pkt.packet_id)}</h2>
          <span class="badge ${unplacedTotal > 0 ? "badge-gate-hold" : "badge-gate-safe"}">
            ${unplacedTotal > 0 ? `${unplacedTotal} remaining` : "All placed"}
          </span>
        </div>

        ${
          !isReviewed
            ? `
              <div style="font-size:12px;font-weight:750;color:var(--ink-hold);">
                Unplaced / Staged Outputs (${routes.length}${plan.isModifiedDraft ? " · Modified Draft" : " · Releases on Approve"})
              </div>
              ${routes
                .map(
                  (r) => `
                  <div class="output-task-row ${r.urgent ? "unplaced-urgent" : "unplaced"}">
                    <div style="display:flex;justify-content:space-between;gap:6px;">
                      <strong>${esc(r.station_label)}</strong>
                      <span class="tabular" style="font-weight:700;color:${r.urgent ? "var(--ink-crit)" : "var(--ink-hold)"};">Unplaced</span>
                    </div>
                    <div>${esc(r.summary)}</div>
                  </div>
                `
                )
                .join("")}
            `
            : `
              <div style="font-size:12px;font-weight:750;color:var(--ink-safe);">
                Provider Outputs Placed (${committedForPkt.length})
              </div>
              ${committedForPkt
                .map(
                  (c) => `
                  <div class="output-task-row placed">
                    <div style="display:flex;justify-content:space-between;gap:6px;">
                      <strong>${esc(c.stationLabel)}</strong>
                      <span class="tabular" style="font-weight:700;color:var(--ink-safe);">Placed ${esc(c.timeStr)}</span>
                    </div>
                    <div>${esc(c.summary)}</div>
                  </div>
                `
                )
                .join("")}
            `
        }

        ${
          remQueries.length > 0
            ? `
              <div style="font-size:12px;font-weight:750;color:var(--ink-crit);margin-top:2px;">
                Remaining Checks / Questions (${remQueries.length} unplaced)
              </div>
              ${remQueries
                .map(
                  (q) => `
                  <div class="output-task-row ${q.id === "req_ekg" ? "unplaced-urgent" : "unplaced"}">
                    <div style="display:flex;justify-content:space-between;align-items:center;gap:6px;">
                      <strong>${esc(q.kind === "bedside_question" ? "Bedside Check" : q.kind === "nursing_check" ? "Door ECG Check" : "External HIE Pull")}</strong>
                      <button type="button" class="btn btn-hold" data-trigger-query="${esc(q.id)}" style="padding:3px 8px;font-size:12px;">
                        ${esc(q.button_label)}
                      </button>
                    </div>
                    <div>${esc(q.prompt)}</div>
                  </div>
                `
                )
                .join("")}
            `
            : ""
        }

        <hr class="outputs-divider" />
        <div style="font-size:12px;font-weight:750;color:var(--ink-secondary);">
          Already Placed at Door / Dispatched (${doorPlaced.length + doneQueries.length})
        </div>
        ${
          doorPlaced.length === 0 && doneQueries.length === 0
            ? `<div class="output-task-row unplaced" style="color:var(--ink-secondary);">
                0 orders, ECGs, or meds placed at door triage — all downstream care awaits provider release.
               </div>`
            : `
              ${doorPlaced
                .map(
                  (dp) => `
                  <div class="output-task-row placed">
                    <div style="display:flex;justify-content:space-between;gap:6px;">
                      <strong>Door Triage Active</strong>
                      <span style="font-weight:700;color:var(--ink-safe);">Placed</span>
                    </div>
                    <div>${esc(dp)}</div>
                  </div>
                `
                )
                .join("")}
              ${doneQueries
                .map(
                  (dq) => `
                  <div class="output-task-row placed">
                    <div style="display:flex;justify-content:space-between;gap:6px;">
                      <strong>${esc(dq.label)}</strong>
                      <span class="tabular" style="font-weight:700;color:var(--ink-safe);">Dispatched ${esc(dq.timeStr)}</span>
                    </div>
                    <div>${esc(dq.prompt)}</div>
                  </div>
                `
                )
                .join("")}
            `
        }
      </section>
    `;
  }

  const totalPlacedAll = STATIONS.reduce((acc, s) => acc + (state.routedByStation[s.id] || []).length, 0);
  const totalRemAll = STATIONS.reduce((acc, s) => acc + (remainingByStation[s.id] || []).length, 0);

  const deptPipelineHtml = `
    <section class="panel-surface outputs-card">
      <div class="section-title-bar">
        <h2>ED Station Pipeline</h2>
        <span class="tabular" style="font-size:12px;font-weight:700;color:var(--ink-secondary);">
          ${totalPlacedAll} placed · ${totalRemAll} rem
        </span>
      </div>
      ${STATIONS.map((st) => {
        const placed = state.routedByStation[st.id] || [];
        const rem = remainingByStation[st.id] || [];
        const tot = Math.max(1, placed.length + rem.length);
        const placedPct = Math.round((placed.length / tot) * 100);
        const remPct = rem.length > 0 ? Math.max(8, 100 - placedPct) : 0;
        const isOpen = state.focusedStationId === st.id || (state.focusedStationId === null && st.id === "alert_team" && rem.length > 0);

        return `
          <details class="station-backlog-card" id="station-card-${st.id}" ${isOpen ? "open" : ""}>
            <summary>
              <div style="display:flex;justify-content:space-between;align-items:center;gap:6px;font-size:12.5px;">
                <span style="display:inline-flex;align-items:center;gap:6px;font-weight:750;">
                  <span class="station-dot ${st.dotCls}"></span>
                  ${esc(st.label)}
                </span>
                <span class="tabular" style="font-weight:700;color:var(--ink-secondary);">
                  ${placed.length} placed · ${rem.length} rem
                </span>
              </div>
              <div class="backlog-bar-track" aria-hidden="true">
                <div class="backlog-bar-placed" style="width:${placedPct}%;"></div>
                <div class="backlog-bar-rem" style="width:${remPct}%;"></div>
              </div>
            </summary>
            <div style="margin-top:7px;">
              ${
                rem.length > 0
                  ? rem
                      .slice(0, 5)
                      .map(
                        (it) => `
                        <button type="button" class="rem-jump-btn" data-select-packet="${esc(it.packetId)}">
                          <div style="display:flex;justify-content:space-between;gap:6px;">
                            <strong>${esc(it.packetId)} · ${esc(it.ageSex)}</strong>
                            <span style="font-weight:700;color:${it.urgent ? "var(--ink-crit)" : "var(--ink-hold)"};">
                              ${it.isCheck ? "Check Rem" : "Unplaced"}
                            </span>
                          </div>
                          <div style="color:var(--ink-secondary);">${esc(it.summary)}</div>
                        </button>
                      `
                      )
                      .join("") +
                    (rem.length > 5
                      ? `<div style="margin-top:4px;font-size:12px;color:var(--ink-muted);">+${rem.length - 5} more remaining in queue</div>`
                      : "")
                  : `<div style="font-size:12px;color:var(--ink-safe);font-weight:600;">0 unplaced tasks remaining for ${esc(st.short)}.</div>`
              }
              ${
                placed.length > 0
                  ? `<div style="margin-top:6px;padding-top:5px;border-top:1px solid var(--border);font-size:12px;color:var(--ink-safe);font-weight:650;">
                      Latest placed: ${placed
                        .slice(-2)
                        .reverse()
                        .map((p) => `${esc(p.packetId)} (${esc(p.summary)})`)
                        .join(" · ")}
                    </div>`
                  : ""
              }
            </div>
          </details>
        `;
      }).join("")}
    </section>
  `;

  host.innerHTML = `<div class="col-flow-banner"><span>Gate -> Downstream Outputs</span><span class="tabular">${totalPlacedAll} placed · ${totalRemAll} rem</span></div>` + activePatientCardHtml + deptPipelineHtml;
}

function renderCrossroads2D() {
  renderActiveInputsColumn();
  renderCrossroadsCenter();
  renderOutputsColumn();
}

/* --------------------------------------------------------------------------
 * Mode 2: 3D Spatial XR Crossroads (Inputs Wing · Center Gate · Outputs Wing)
 * -------------------------------------------------------------------------- */
function renderSpatialOverlays() {
  const topBoard = document.getElementById("xr-top-board");
  const centerCard = document.getElementById("xr-center-card");
  const leftWing = document.getElementById("xr-left-wing");
  const rightWing = document.getElementById("xr-right-wing");
  if (!topBoard || !centerCard || !leftWing || !rightWing) return;

  const c = state.capacity;
  const s = state.stats;
  const { remainingByStation, totalRemainingChecks } = computeStationBacklogs();
  const unplacedPatients = state.packets.filter((p) => !state.reviewedPacketIds.has(p.packet_id)).length;

  const recentFeed = state.routedHistory
    .slice(-3)
    .reverse()
    .map(
      (h) => `
      <div class="xr-feed-line">
        <span><strong>${esc(h.packetId)}</strong> · ${esc(h.ageSex)} ${esc(h.chief)}</span>
        <span style="color:${h.modified ? "#FDBA74" : "#86EFAC"};font-weight:700;">Placed -> ${esc(h.headline)}</span>
      </div>
    `
    )
    .join("");

  const backlogSummaryLine = STATIONS.map(
    (st) => `${esc(st.short)}: ${(state.routedByStation[st.id] || []).length} placed / ${(remainingByStation[st.id] || []).length} rem`
  ).join(" · ");

  topBoard.innerHTML = `
    <div class="xr-board-metrics">
      <div>
        <strong style="font-size:13.5px;color:#FFFFFF;">ED PITM Spatial Crossroads</strong>
        <span style="color:#FDE047;margin-left:6px;">SYNTHETIC — zero PHI</span>
      </div>
      <div style="display:flex;gap:14px;">
        <div class="xr-stat"><strong>${s.reviewed}</strong><span>Placed</span></div>
        <div class="xr-stat"><strong>${unplacedPatients}</strong><span>Remaining</span></div>
        <div class="xr-stat"><strong>${totalRemainingChecks}</strong><span>Checks Rem</span></div>
        <div class="xr-stat"><strong>${s.hardTriggers}</strong><span>Hard Triggers</span></div>
        <div class="xr-stat"><strong>${c.bedsOpen}/${c.bedsTotal}</strong><span>Beds Open</span></div>
      </div>
    </div>
    <div class="xr-routed-feed">
      ${recentFeed || `<div style="color:#FDE68A;font-weight:600;">Unplaced Pipeline -> ${backlogSummaryLine}</div>`}
    </div>
  `;

  const pkt = activePacket();
  if (!pkt) return;
  const enc = pkt.encounter || {};
  const dos = pkt.dossier || {};
  const vit = enc.vitals || {};
  const cr = pkt.clinical_routing || {};
  const sg = pkt.safe_gate || {};
  const plan = effectiveRoutingPlan(pkt);
  const routes = plan.routes || [];
  const queries = (cr.actionable_queries || []).filter((q) => !q.completed);
  const streams = getInputStreams(pkt);
  const doorPlaced = getAlreadyPlacedAtDoor(pkt);
  const isReviewed = state.reviewedPacketIds.has(pkt.packet_id);

  const approveLabel = plan.isModifiedDraft
    ? "Place Modified Plan"
    : cr.hard_trigger?.active
    ? "Escalate & Place"
    : sg.allow_one_pinch_routine
    ? "Fast-Pass & Place"
    : "Approve & Place";
  const approveCls = cr.hard_trigger?.active
    ? "btn-crit"
    : sg.allow_one_pinch_routine && !plan.isModifiedDraft
    ? "btn-safe"
    : "btn-primary";

  // Left Wing (ALWAYS VISIBLE): Upstream Inputs by default, or active drawer (Modify / Ask / Details / Station)
  leftWing.hidden = false;
  if (state.wingMode) {
    leftWing.innerHTML = buildDrawerOrWingHtml(pkt, true);
  } else {
    leftWing.innerHTML = `
      <div style="display:flex;justify-content:space-between;align-items:baseline;margin-bottom:4px;">
        <strong style="font-size:13.5px;color:#7DD3FC;">Upstream Inputs -> Gate</strong>
        <span class="tabular" style="color:#CBD5E1;">${esc(pkt.packet_id)}</span>
      </div>
      ${streams
        .map(
          (st) => `
          <div class="xr-io-row ${st.status === "conflict" || st.status === "gap" ? "unplaced-urgent" : st.status === "alert" ? "unplaced" : "placed"}">
            <div style="display:flex;justify-content:space-between;align-items:center;gap:6px;">
              <strong>${esc(st.label)}</strong>
              ${inputStatusBadge(st.status, true)}
            </div>
            <div style="color:#E2E8F0;">${esc(st.summary)}</div>
          </div>
        `
        )
        .join("")}
    `;
  }

  // Center Spatial Card: Provider in the Middle
  centerCard.innerHTML = `
    ${
      cr.hard_trigger?.active
        ? `<div class="hard-trigger-banner" style="padding:9px 15px;font-size:13.5px;">
            <span>${esc(cr.hard_trigger.banner)}</span>
            <span style="font-size:12px;opacity:0.9;">Immediate Action</span>
          </div>`
        : ""
    }
    <div class="xr-card-inner">
      <div style="display:flex;justify-content:space-between;align-items:baseline;gap:8px;">
        <div style="font-size:15px;font-weight:750;">
          [SYNTHETIC] ${esc(enc.age_years)}${esc(enc.sex)} · ${esc(enc.chief_complaint)} · HR ${esc(vit.hr)} BP ${esc(vit.sbp)}/${esc(vit.dbp)} SpO2 ${esc(vit.spo2)}% T ${esc(vit.temp_c)}
          ${(vit.gcs ?? 15) < 15 ? `· <span style="color:#FCA5A5;">GCS ${esc(vit.gcs)}</span>` : ""}
        </div>
        <span class="tabular" style="font-size:12px;color:#CBD5E1;">${esc(pkt.packet_id)}</span>
      </div>

      <p class="xr-pmh-line">
        PMH: ${esc(dos.pmh_summary)} · Meds: ${esc((dos.current_meds || []).join(", "))}
      </p>

      <div style="padding:8px 11px;border-radius:10px;background:rgba(15,23,42,0.58);border:1px solid rgba(148,163,184,0.3);">
        <p class="xr-plan-call">${plan.isModifiedDraft ? "Modified: " : "Decision: "}${esc(plan.headline)}</p>
        ${routes
          .map(
            (r) =>
              `<p class="xr-next-green" style="${r.urgent ? "color:#FDBA74;" : ""}">-> ${esc(r.station_label)}: ${esc(r.summary)} (${isReviewed ? "Placed" : "Unplaced"})</p>`
          )
          .join("")}
      </div>

      <div style="display:flex;flex-wrap:wrap;gap:5px;align-items:center;">
        <span style="font-size:12px;color:#94A3B8;font-weight:700;">WHY QUEUED:</span>
        ${(cr.why_queued || [])
          .map(
            (w) =>
              `<span class="xr-why-chip ${cr.hard_trigger?.active || (pkt.sentinels || []).length > 0 ? "crit" : ""}">${esc(w)}</span>`
          )
          .join("")}
      </div>

      <div style="display:flex;justify-content:space-between;align-items:center;gap:8px;flex-wrap:wrap;padding-top:3px;border-top:1px solid rgba(148,163,184,0.22);">
        <div>
          <span class="now-pill">PITM</span>
          <span style="font-size:12px;color:#CBD5E1;margin-left:6px;">
            Inputs (${streams.length}) -> Gate -> ${esc(routes.map((r) => r.station_label).join(" + "))} · Charted ${esc(enc.triage_status)}
          </span>
        </div>
        ${clinicalGateBadge(pkt, true)}
      </div>

      ${cr.if_missed_risk ? `<p class="xr-if-wrong">IF MISSED · ${esc(cr.if_missed_risk)}</p>` : ""}

      <p class="xr-census-line">
        ED Census: Staffed beds ${c.bedsOpen}/${c.bedsTotal} · Hallway ${c.hallwayOpen}/${c.hallwayTotal} · Fast track ${c.fastTrackOpen}/${c.fastTrackTotal} · Waitboard ${c.waitboard}
      </p>

      <div style="display:flex;flex-wrap:wrap;justify-content:space-between;gap:8px;margin-top:4px;">
        <div style="display:flex;gap:7px;flex-wrap:wrap;">
          <button type="button" class="btn ${approveCls}" data-action="${plan.isModifiedDraft ? "commit-modified" : "approve-route"}">${esc(approveLabel)}</button>
          <button type="button" class="btn" data-toggle-wing="modify">Modify…</button>
          ${
            queries.length > 0
              ? `<button type="button" class="btn btn-hold" data-toggle-wing="ask">Checks (${queries.length})</button>`
              : ""
          }
        </div>
        <button type="button" class="xr-mini-btn" data-toggle-wing="details">
          ${state.wingMode === "details" ? "Hide Details" : "Details"}
        </button>
      </div>
    </div>
  `;

  // Right Wing (ALWAYS VISIBLE): Staged/Unplaced Outputs + Remaining Checks + Door Placed + Up Next Queue
  rightWing.hidden = false;
  const upcoming = sortedPackets()
    .filter((p) => !state.reviewedPacketIds.has(p.packet_id) && p.packet_id !== pkt.packet_id)
    .slice(0, 3);

  rightWing.innerHTML = `
    <div style="display:flex;justify-content:space-between;align-items:baseline;">
      <strong style="font-size:13.5px;color:#FDE68A;">Gate -> Outputs &amp; Remaining</strong>
      <span class="tabular" style="font-size:12px;color:#CBD5E1;">${isReviewed ? "Placed" : `${routes.length + queries.length} rem`}</span>
    </div>
    ${routes
      .map(
        (r) => `
        <div class="xr-io-row ${isReviewed ? "placed" : r.urgent ? "unplaced-urgent" : "unplaced"}">
          <div style="display:flex;justify-content:space-between;gap:6px;">
            <strong>${esc(r.station_label)}</strong>
            <span style="color:${isReviewed ? "#86EFAC" : "#FDE047"};font-weight:700;">${isReviewed ? "Placed" : "Unplaced"}</span>
          </div>
          <div>${esc(r.summary)}</div>
        </div>
      `
      )
      .join("")}
    ${queries
      .map(
        (q) => `
        <div class="xr-io-row unplaced-urgent">
          <div style="display:flex;justify-content:space-between;align-items:center;gap:6px;">
            <strong>Check Remaining</strong>
            <button type="button" class="xr-mini-btn" data-trigger-query="${esc(q.id)}" style="border-color:#FBBF24;color:#FEF3C7;">
              ${esc(q.button_label)}
            </button>
          </div>
          <div>${esc(q.prompt)}</div>
        </div>
      `
      )
      .join("")}
    ${
      doorPlaced.length > 0
        ? `<div class="xr-io-row placed">
            <strong>Door Placed (${doorPlaced.length}):</strong>
            <div>${esc(doorPlaced.join(" · "))}</div>
          </div>`
        : `<div class="xr-io-row unplaced" style="opacity:0.88;">0 orders/meds placed at door triage.</div>`
    }

    <div style="display:flex;justify-content:space-between;align-items:baseline;margin-top:9px;padding-top:6px;border-top:1px solid rgba(148,163,184,0.25);">
      <strong style="font-size:13px;">Remaining Queue</strong>
      <span class="tabular" style="font-size:12px;color:#CBD5E1;">${unplacedPatients} in queue</span>
    </div>
    ${upcoming
      .map((u) => {
        const propEsi = u.recommendations?.[0]?.value?.triage_status || u.encounter?.triage_status;
        return `
          <button type="button" class="xr-upnext-item" data-select-packet="${esc(u.packet_id)}">
            <div style="display:flex;justify-content:space-between;font-size:12px;">
              <strong>${esc(u.packet_id)} · ${esc(propEsi)}</strong>
              <span style="color:#FDE047;">Unplaced · ${esc(u.encounter?.arrival_minutes_ago)}m</span>
            </div>
            <div style="font-size:12px;color:#E2E8F0;margin-top:2px;">${esc(u.encounter?.chief_complaint)}</div>
          </button>
        `;
      })
      .join("")}
  `;
}

function makePillSprite(THREE, title, sub, bg = "rgba(30,41,59,0.92)", border = "#64748B") {
  const canvas = document.createElement("canvas");
  canvas.width = 340;
  canvas.height = 98;
  const ctx = canvas.getContext("2d");
  ctx.fillStyle = bg;
  ctx.beginPath();
  ctx.roundRect(8, 10, 324, 78, 36);
  ctx.fill();
  ctx.strokeStyle = border;
  ctx.lineWidth = 3;
  ctx.stroke();

  ctx.fillStyle = "#F8FAFC";
  ctx.font = "bold 22px -apple-system, BlinkMacSystemFont, Inter, sans-serif";
  ctx.textAlign = "center";
  ctx.fillText(title, 170, 42);

  ctx.fillStyle = "#CBD5E1";
  ctx.font = "600 17px -apple-system, BlinkMacSystemFont, Inter, sans-serif";
  ctx.fillText(sub, 170, 68);

  const tex = new THREE.CanvasTexture(canvas);
  tex.needsUpdate = true;
  const mat = new THREE.SpriteMaterial({ map: tex, transparent: true, depthTest: false });
  const sprite = new THREE.Sprite(mat);
  sprite.scale.set(1.48, 0.43, 1);
  return { sprite, tex, mat };
}

class SpatialCrossroadsArena {
  constructor(THREE, OrbitControls, host) {
    this.THREE = THREE;
    this.host = host;
    this.disposables = [];
    this.interactiveMeshes = [];
    this.flyingPulses = [];
    this.stationPositions = {};

    this.scene = new THREE.Scene();
    this.scene.background = new THREE.Color(0x0b1220);

    const w = Math.max(320, host.clientWidth || window.innerWidth);
    const h = Math.max(320, host.clientHeight || window.innerHeight - 58);

    this.camera = new THREE.PerspectiveCamera(48, w / h, 0.1, 80);
    this.camera.position.set(0, 2.35, 6.8);

    this.renderer = new THREE.WebGLRenderer({ antialias: true, powerPreference: "high-performance" });
    this.renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
    this.renderer.setSize(w, h);
    host.innerHTML = "";
    host.appendChild(this.renderer.domElement);

    this.controls = new OrbitControls(this.camera, this.renderer.domElement);
    this.controls.enableDamping = true;
    this.controls.dampingFactor = 0.08;
    this.controls.target.set(0, 1.15, -1.2);
    this.controls.minDistance = 3.5;
    this.controls.maxDistance = 12.0;
    this.controls.maxPolarAngle = Math.PI * 0.52;

    this.scene.add(new THREE.HemisphereLight(0xe2e8f0, 0x0f172a, 1.25));
    const keyLight = new THREE.DirectionalLight(0xffffff, 1.45);
    keyLight.position.set(4, 9, 6);
    this.scene.add(keyLight);
    const rimLight = new THREE.PointLight(0x38bdf8, 0.9, 18);
    rimLight.position.set(0, 3.5, -4.5);
    this.scene.add(rimLight);

    const grid = new THREE.GridHelper(22, 28, 0x334155, 0x17253b);
    grid.position.y = -0.5;
    this.scene.add(grid);

    const horizonArc = new THREE.Mesh(
      new THREE.RingGeometry(6.8, 6.86, 96),
      new THREE.MeshBasicMaterial({ color: 0x38bdf8, side: THREE.DoubleSide, transparent: true, opacity: 0.45 })
    );
    horizonArc.rotation.x = -Math.PI / 2;
    horizonArc.position.y = -0.49;
    this.scene.add(horizonArc);

    this.dynamicGroup = new THREE.Group();
    this.scene.add(this.dynamicGroup);
    this.pulseGroup = new THREE.Group();
    this.scene.add(this.pulseGroup);

    this.raycaster = new THREE.Raycaster();
    this.pointer = new THREE.Vector2();
    this.downPos = { x: 0, y: 0 };
    this.clock = new THREE.Clock();

    this._bindEvents();
    this._animate = this._animate.bind(this);
    this.renderer.setAnimationLoop(this._animate);
  }

  resetCamera() {
    this.camera.position.set(0, 2.35, 6.8);
    this.controls.target.set(0, 1.15, -1.2);
    this.controls.update();
  }

  _bindEvents() {
    const dom = this.renderer.domElement;
    dom.addEventListener("pointerdown", (ev) => {
      this.downPos = { x: ev.clientX, y: ev.clientY };
    });
    dom.addEventListener("pointerup", (ev) => {
      if (Math.hypot(ev.clientX - this.downPos.x, ev.clientY - this.downPos.y) > 6) return;
      const rect = dom.getBoundingClientRect();
      this.pointer.x = ((ev.clientX - rect.left) / rect.width) * 2 - 1;
      this.pointer.y = -((ev.clientY - rect.top) / rect.height) * 2 + 1;
      this.raycaster.setFromCamera(this.pointer, this.camera);
      const hits = this.raycaster.intersectObjects(this.interactiveMeshes, false);
      if (!hits.length) return;
      const data = hits[0].object.userData || {};
      if (data.packetId) {
        state.selectedPacketId = data.packetId;
        renderAll();
      } else if (data.stationId) {
        const key = `station:${data.stationId}`;
        state.wingMode = state.wingMode === key ? null : key;
        renderAll();
      }
    });

    window.addEventListener("resize", () => {
      if (state.viewMode !== "spatial") return;
      const w = Math.max(320, this.host.clientWidth || window.innerWidth);
      const h = Math.max(320, this.host.clientHeight || window.innerHeight - 58);
      this.camera.aspect = w / h;
      this.camera.updateProjectionMatrix();
      this.renderer.setSize(w, h);
    });
  }

  launchHandoffPulses(routes) {
    const THREE = this.THREE;
    const origin = new THREE.Vector3(0, 0.35, 1.05);
    for (const r of routes || []) {
      const dest = this.stationPositions[r.station_id];
      if (!dest) continue;
      const st = STATIONS.find((s) => s.id === r.station_id);
      const geo = new THREE.SphereGeometry(0.14, 20, 20);
      const mat = new THREE.MeshStandardMaterial({
        color: st ? st.hex : 0x38bdf8,
        emissive: st ? st.hex : 0x38bdf8,
        emissiveIntensity: 0.85,
        roughness: 0.15,
      });
      const orb = new THREE.Mesh(geo, mat);
      orb.position.copy(origin);
      this.pulseGroup.add(orb);
      this.flyingPulses.push({
        mesh: orb,
        geo,
        mat,
        start: origin.clone(),
        end: dest.clone(),
        t: 0,
      });
    }
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
  }

  sync(appState) {
    const THREE = this.THREE;
    this.clearDynamic();

    const pkt = activePacket();
    const isReviewed = pkt ? appState.reviewedPacketIds.has(pkt.packet_id) : false;
    const streams = pkt ? getInputStreams(pkt) : [];
    const streamById = Object.fromEntries(streams.map((s) => [s.id, s]));
    const plan = pkt ? effectiveRoutingPlan(pkt) : { routes: [] };
    const activeStations = new Set((plan.routes || []).map((r) => r.station_id));
    const remChecks = (pkt?.clinical_routing?.actionable_queries || []).filter((q) => !q.completed);
    if (remChecks.length > 0) activeStations.add("fast_track");

    const { remainingByStation } = computeStationBacklogs();
    const hub = new THREE.Vector3(0, 0.32, 1.05);

    // 0. Central Provider-in-the-Middle Gate Node (Explicitly anchors the Clinician in 3D Space!)
    const hubRingGeo = new THREE.TorusGeometry(0.46, 0.045, 16, 48);
    const hubRingMat = new THREE.MeshStandardMaterial({
      color: 0x38bdf8,
      emissive: 0x0284c7,
      emissiveIntensity: 0.65,
      roughness: 0.2,
    });
    this.disposables.push(hubRingGeo, hubRingMat);
    const hubRing = new THREE.Mesh(hubRingGeo, hubRingMat);
    hubRing.rotation.x = Math.PI / 2;
    hubRing.position.copy(hub);
    this.dynamicGroup.add(hubRing);

    const hubCoreGeo = new THREE.SphereGeometry(0.22, 28, 28);
    const hubCoreMat = new THREE.MeshPhysicalMaterial({
      color: 0xf8fafc,
      emissive: 0x38bdf8,
      emissiveIntensity: 0.45,
      roughness: 0.12,
      clearcoat: 0.9,
    });
    this.disposables.push(hubCoreGeo, hubCoreMat);
    const hubCore = new THREE.Mesh(hubCoreGeo, hubCoreMat);
    hubCore.position.copy(hub);
    this.dynamicGroup.add(hubCore);

    const hubPill = makePillSprite(
      THREE,
      "Provider Gate",
      pkt ? `${pkt.packet_id} · ${isReviewed ? "Placed" : "In Middle"}` : "Ready",
      "rgba(15,23,42,0.96)",
      "#38BDF8"
    );
    hubPill.sprite.position.set(hub.x, hub.y + 0.56, hub.z);
    this.disposables.push(hubPill.tex, hubPill.mat);
    this.dynamicGroup.add(hubPill.sprite);

    // 1. Left Upstream Input Spheres + Live Convergence Beams -> Provider Hub
    const inputs = [
      { id: "door_triage", title: "Door & EMS", pos: [-4.15, 1.15, -1.25], hex: 0x2dd4bf },
      { id: "chart_pharmacy", title: "Chart & Meds", pos: [-2.95, 0.85, 0.35], hex: 0x60a5fa },
      { id: "labs_micro", title: "Labs, Micro & HIE", pos: [-2.15, 0.22, 1.95], hex: 0xf59e0b },
    ];

    for (const inp of inputs) {
      const stInfo = streamById[inp.id] || { status: "complete" };
      const isHot = stInfo.status !== "complete" || inp.id === "door_triage";
      const statusTag =
        stInfo.status === "conflict"
          ? "CONFLICT"
          : stInfo.status === "gap"
          ? "MISSING"
          : stInfo.status === "alert"
          ? "SIGNAL"
          : "VERIFIED";
      const borderHex =
        stInfo.status === "conflict" || stInfo.status === "gap"
          ? "#F87171"
          : stInfo.status === "alert"
          ? "#FBBF24"
          : "#4ADE80";

      const pVec = new THREE.Vector3(...inp.pos);
      const geo = new THREE.SphereGeometry(isHot ? 0.34 : 0.27, 32, 32);
      const mat = new THREE.MeshPhysicalMaterial({
        color: inp.hex,
        roughness: 0.18,
        metalness: 0.1,
        clearcoat: 0.8,
        emissive: isHot ? inp.hex : 0x000000,
        emissiveIntensity: isHot ? 0.34 : 0.05,
      });
      this.disposables.push(geo, mat);
      const mesh = new THREE.Mesh(geo, mat);
      mesh.position.copy(pVec);
      this.dynamicGroup.add(mesh);

      const lineGeo = new THREE.BufferGeometry().setFromPoints([pVec, hub]);
      const lineMat = new THREE.LineBasicMaterial({
        color: isHot ? 0x7dd3fc : 0x475569,
        transparent: true,
        opacity: isHot ? 0.92 : 0.45,
      });
      this.disposables.push(lineGeo, lineMat);
      this.dynamicGroup.add(new THREE.Line(lineGeo, lineMat));

      const pill = makePillSprite(
        THREE,
        inp.title,
        `Input · ${statusTag}`,
        "rgba(15,23,42,0.94)",
        borderHex
      );
      pill.sprite.position.set(pVec.x, pVec.y + 0.58, pVec.z);
      this.disposables.push(pill.tex, pill.mat);
      this.dynamicGroup.add(pill.sprite);
    }

    // 2. Right Downstream Output Spheres (`X placed · Y rem`) + Dashed Unplaced vs Solid Placed Beams
    const destCoords = {
      alert_team: [3.95, 1.35, -1.45],
      bed_board: [4.45, 0.95, 0.25],
      wr_orders: [3.1, 0.48, 1.72],
      fast_track: [2.05, 0.05, 2.48],
    };

    for (const st of STATIONS) {
      const pVec = new THREE.Vector3(...destCoords[st.id]);
      this.stationPositions[st.id] = pVec;
      const isTarget = activeStations.has(st.id);
      const placedCnt = (appState.routedByStation[st.id] || []).length;
      const remCnt = (remainingByStation[st.id] || []).length;

      const geo = new THREE.SphereGeometry(isTarget ? 0.38 : 0.3, 32, 32);
      const mat = new THREE.MeshPhysicalMaterial({
        color: st.hex,
        roughness: 0.16,
        metalness: 0.12,
        clearcoat: 0.85,
        emissive: isTarget || remCnt > 0 ? st.hex : 0x000000,
        emissiveIntensity: isTarget ? 0.42 : remCnt > 0 ? 0.16 : 0.04,
      });
      this.disposables.push(geo, mat);
      const mesh = new THREE.Mesh(geo, mat);
      mesh.position.copy(pVec);
      mesh.userData = { stationId: st.id };
      this.dynamicGroup.add(mesh);
      this.interactiveMeshes.push(mesh);

      const lineGeo = new THREE.BufferGeometry().setFromPoints([hub, pVec]);
      let lineObj;
      if (isTarget && !isReviewed) {
        const dashMat = new THREE.LineDashedMaterial({
          color: 0xfbbf24,
          dashSize: 0.22,
          gapSize: 0.12,
          transparent: true,
          opacity: 0.95,
        });
        lineObj = new THREE.Line(lineGeo, dashMat);
        lineObj.computeLineDistances();
        this.disposables.push(lineGeo, dashMat);
      } else {
        const solidMat = new THREE.LineBasicMaterial({
          color: isTarget && isReviewed ? 0x4ade80 : remCnt > 0 ? 0x64748b : 0x334155,
          transparent: true,
          opacity: isTarget ? 0.95 : 0.48,
        });
        lineObj = new THREE.Line(lineGeo, solidMat);
        this.disposables.push(lineGeo, solidMat);
      }
      this.dynamicGroup.add(lineObj);

      const pill = makePillSprite(
        THREE,
        st.short,
        `${placedCnt} placed · ${remCnt} rem`,
        isTarget ? "rgba(15,23,42,0.96)" : "rgba(30,41,59,0.86)",
        isTarget ? (isReviewed ? "#4ADE80" : "#FBBF24") : "#64748B"
      );
      pill.sprite.position.set(pVec.x, pVec.y + 0.62, pVec.z);
      this.disposables.push(pill.tex, pill.mat);
      this.dynamicGroup.add(pill.sprite);
    }

    // 3. Horizon Incoming Queue Conveyor (11 Glossy Priority Spheres)
    const pending = sortedPackets().filter((p) => !appState.reviewedPacketIds.has(p.packet_id));
    const horizonSlice = pending.slice(0, 11);
    const count = horizonSlice.length;
    const spacing = 0.62;
    const startX = -((count - 1) * spacing) / 2;

    horizonSlice.forEach((qPkt, idx) => {
      const isCur = qPkt.packet_id === pkt?.packet_id;
      const gs = qPkt.safe_gate?.gate_state;
      const sphHex =
        gs === "CERTIFIED_SAFE_ROUTINE"
          ? 0x22c55e
          : gs === "AMBIGUOUS_HOLD_VOI"
          ? 0xeab308
          : 0xdc2626;

      const geo = new THREE.SphereGeometry(isCur ? 0.29 : 0.22, 32, 32);
      const mat = new THREE.MeshPhysicalMaterial({
        color: sphHex,
        roughness: 0.15,
        metalness: 0.08,
        clearcoat: 0.9,
        emissive: isCur ? sphHex : 0x000000,
        emissiveIntensity: isCur ? 0.45 : 0.0,
      });
      this.disposables.push(geo, mat);
      const sph = new THREE.Mesh(geo, mat);
      sph.position.set(startX + idx * spacing, 1.48, -2.05);
      sph.userData = { packetId: qPkt.packet_id };
      this.dynamicGroup.add(sph);
      this.interactiveMeshes.push(sph);
    });
  }

  _animate() {
    const dt = Math.min(0.05, this.clock.getDelta());
    if (this.flyingPulses.length > 0) {
      const keep = [];
      for (const p of this.flyingPulses) {
        p.t += dt * 1.9;
        if (p.t >= 1.0) {
          this.pulseGroup.remove(p.mesh);
          p.geo.dispose();
          p.mat.dispose();
        } else {
          p.mesh.position.lerpVectors(p.start, p.end, p.t);
          p.mesh.position.y += Math.sin(p.t * Math.PI) * 0.45;
          keep.push(p);
        }
      }
      this.flyingPulses = keep;
    }

    if (state.viewMode !== "spatial") return;
    this.controls.update();
    this.renderer.render(this.scene, this.camera);
  }
}

async function ensureSpatialArena() {
  if (state.spatialArena) {
    state.spatialArena.sync(state);
    return;
  }
  const host = document.getElementById("xr-canvas-host");
  if (!host) return;
  try {
    const [THREE, orbitMod] = await Promise.all([
      import("three"),
      import("three/addons/controls/OrbitControls.js"),
    ]);
    state.spatialArena = new SpatialCrossroadsArena(THREE, orbitMod.OrbitControls, host);
    state.spatialArena.sync(state);
  } catch (err) {
    host.innerHTML = `<div style="padding:32px;color:#E2E8F0;">3D WebGL unavailable (${esc(err.message)}).</div>`;
  }
}

/* --------------------------------------------------------------------------
 * Mode 3: Dedicated Statistical Safety Engine Research View
 * -------------------------------------------------------------------------- */
function renderStatisticalEngineTab() {
  const wrap = document.getElementById("engine-workbench-wrap");
  if (!wrap) return;
  const pkt = activePacket();
  if (!pkt) return;

  const sg = pkt.safe_gate || {};
  const vr = pkt.voi_routing || {};
  const orth = pkt.swarm?.orthogonality || {};
  const channels = vr.channels || [];

  wrap.innerHTML = `
    <div class="panel-surface" style="padding:18px;">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px;">
        <h2 style="margin:0;font-size:16px;">Active Encounter Calibration &amp; Conformal Receipt (${esc(pkt.packet_id)})</h2>
        ${vr.cfbh_alert_certified ? `<span class="badge badge-gate-crit">CfBH Certified</span>` : `<span class="badge badge-neutral">Sub-CfBH</span>`}
      </div>
      <p style="margin:0 0 12px;font-size:12.5px;color:var(--ink-secondary);">
        Research diagnostics kept off the rapid Provider-in-the-Middle surface so clinicians see zero math clutter.
      </p>
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
          <tr><th>Orthogonal G_{m|S} Synthesis</th><td class="num">${esc(orth.n_orthogonal_selected ?? 0)} selected · ${esc(orth.n_echoes_suppressed ?? 0)} echoes · ${esc(orth.n_twins_suppressed ?? 0)} twins pruned</td></tr>
        </tbody>
      </table>
    </div>

    <div class="panel-surface" style="padding:18px;">
      <h2 style="margin:0 0 10px;font-size:16px;">9-Node Martingale Gauss-Hermite VOI Channel Table</h2>
      ${
        vr.triage_voi_zero_already_above_tau
          ? `<div class="voi-note-box" style="margin-bottom:10px;background:var(--bg-crit);border-color:var(--border-crit);color:var(--ink-crit);font-weight:650;">
              High Risk != High VOI: Already above τ* (or missing acute door eval) -> Escalation VOI = 0.000.
            </div>`
          : `<div class="voi-note-box" style="margin-bottom:10px;">
              Observation-aware VOI triggers external/bedside queries strictly when unobserved and expected decision-flip gain exceeds query cost.
            </div>`
      }
      <table class="metric-table">
        <thead>
          <tr><th>Channel</th><th style="text-align:right;">VOI</th><th style="text-align:right;">Cost</th><th style="text-align:right;">Status</th></tr>
        </thead>
        <tbody>
          ${channels
            .map((ch) => {
              const badge = ch.trigger_query
                ? `<span class="badge badge-gate-hold">Actionable Query</span>`
                : ch.already_in_chart
                ? `<span class="badge badge-gate-safe">In Chart</span>`
                : `<span class="badge badge-neutral">Skip (Low VOI)</span>`;
              return `
                <tr>
                  <td>${esc(ch.display_name)}</td>
                  <td class="num">${Number(ch.voi_expected_gain).toFixed(3)}</td>
                  <td class="num">${Number(ch.query_cost).toFixed(3)}</td>
                  <td style="text-align:right;">${badge}</td>
                </tr>
              `;
            })
            .join("")}
        </tbody>
      </table>
    </div>
  `;
}

function setViewMode(mode) {
  state.viewMode = mode;
  document.body.setAttribute("data-view-mode", mode);
  document.querySelectorAll("[data-view-tab]").forEach((btn) => {
    btn.setAttribute("aria-selected", String(btn.dataset.viewTab === mode));
  });
  document.getElementById("spatial-stage-wrap").hidden = mode !== "spatial";
  document.getElementById("engine-workbench-wrap").hidden = mode !== "engine";

  renderAll();
  if (mode === "spatial") {
    requestAnimationFrame(() => {
      ensureSpatialArena();
    });
  }
}

function renderAll() {
  renderSituationalStrip();
  renderQueue();
  if (state.viewMode === "crossroads") {
    renderCrossroads2D();
  } else if (state.viewMode === "spatial") {
    renderSpatialOverlays();
    state.spatialArena?.sync(state);
  } else if (state.viewMode === "engine") {
    renderStatisticalEngineTab();
  }
}

/* --------------------------------------------------------------------------
 * Routing Commit, Actionable Query Execution, Capacity & Dialogs
 * -------------------------------------------------------------------------- */
async function executeRouteForPacket(isModified = false) {
  const pkt = activePacket();
  if (!pkt) return;
  const recs = pkt.recommendations || [];
  const cr = pkt.clinical_routing || {};
  const d = isModified ? ensureModifyDraft(pkt) : null;

  const entries = recs.map((rec) => {
    let changed = false;
    let finalVal = rec.value;

    if (isModified && d) {
      if (rec.type === "triage_status" && d.esi !== rec.value.triage_status) {
        changed = true;
        finalVal = { type: "triage_status", triage_status: d.esi };
      } else if (rec.type === "space_priority" && d.space !== rec.value.priority) {
        changed = true;
        finalVal = { type: "space_priority", priority: d.space };
      } else if (rec.type === "eval_upgrade") {
        const curNeed = (rec.value.needs || [])[0] || "none";
        if (d.evalNeed !== curNeed) {
          changed = true;
          finalVal = { type: "eval_upgrade", needs: d.evalNeed === "none" ? [] : [d.evalNeed] };
        }
      } else if (rec.type === "wr_orders" && d.holdOrders && (rec.value.items || []).length > 0) {
        changed = true;
        finalVal = { type: "wr_orders", action: "none", items: [] };
      } else if (rec.type === "wr_meds" && d.medChoice !== "recommended") {
        changed = true;
        const medMap = {
          apap_only: ["Acetaminophen (APAP)"],
          zofran_only: ["Ondansetron"],
          npo_hold: [],
        };
        finalVal = { type: "wr_meds", meds: medMap[d.medChoice] || [] };
      }
    }

    return {
      packetId: pkt.packet_id,
      decisionType: rec.type,
      providerAction: changed ? "change" : "accept",
      overrideReason: changed ? d.reason : null,
      gateState: pkt.safe_gate?.gate_state,
      modelRecommendation: rec,
      finalValue: finalVal,
    };
  });

  const res = await fetch("/api/ledger", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ entries }),
  });
  const payload = await res.json();
  if (!res.ok) {
    showToast(payload.error || "Failed to log decision.");
    return;
  }

  const plan = isModified ? effectiveRoutingPlan(pkt) : { headline: cr.headline_decision || "Routed", routes: cr.what_happens_next || [] };
  const finalRoutes = plan.routes || [];
  const headlineUsed = plan.headline;

  const timeStr = new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  const ageSex = `${pkt.encounter?.age_years}${pkt.encounter?.sex}`;

  // Avoid duplicate station entries if provider re-sends an already reviewed packet
  const wasAlreadyReviewed = state.reviewedPacketIds.has(pkt.packet_id);
  if (!wasAlreadyReviewed) {
    for (const r of finalRoutes) {
      if (!state.routedByStation[r.station_id]) state.routedByStation[r.station_id] = [];
      state.routedByStation[r.station_id].push({
        packetId: pkt.packet_id,
        ageSex,
        chief: pkt.encounter?.chief_complaint,
        summary: r.summary,
        timeStr,
        isCheck: false,
      });
    }
  }

  if (state.spatialArena && state.viewMode === "spatial") {
    state.spatialArena.launchHandoffPulses(finalRoutes);
  }

  const anyChanged = entries.some((e) => e.providerAction === "change");
  if (!wasAlreadyReviewed) {
    state.reviewedPacketIds.add(pkt.packet_id);
    state.stats.reviewed += 1;
    if (cr.hard_trigger?.active) state.stats.hardTriggers += 1;
    if (pkt.safe_gate?.gate_state === "CERTIFIED_SAFE_ROUTINE" && !anyChanged) state.stats.fastPassed += 1;
    if (anyChanged) state.stats.overrides += 1;
  }
  state.stats.decisions += entries.length;

  state.routedHistory.push({
    packetId: pkt.packet_id,
    ageSex,
    chief: pkt.encounter?.chief_complaint,
    headline: headlineUsed,
    modified: anyChanged,
    ts: timeStr,
  });

  state.ledgerCount = payload.total_entries || state.ledgerCount + entries.length;
  const ledCountEl = document.getElementById("ledger-count");
  if (ledCountEl) ledCountEl.textContent = String(state.ledgerCount);

  showToast(`Placed ${pkt.packet_id} -> ${finalRoutes.map((r) => r.station_label).join(" + ")}`);

  if (state.wingMode === "modify" || state.wingMode === "ask") {
    state.wingMode = null;
  }
  const nextPending = sortedPackets().find((p) => !state.reviewedPacketIds.has(p.packet_id));
  if (nextPending) {
    state.selectedPacketId = nextPending.packet_id;
  }
  renderAll();
}

async function triggerActionableQuery(queryId) {
  const pkt = activePacket();
  if (!pkt) return;
  const qObj = (pkt.clinical_routing?.actionable_queries || []).find((q) => q.id === queryId);
  const existingUnlocked = (pkt.clinical_routing?.actionable_queries || [])
    .filter((q) => q.completed)
    .map((q) => q.id);
  const nextUnlocked = Array.from(new Set([...existingUnlocked, queryId]));

  const resp = await fetch("/api/evaluate", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      packet_id: pkt.packet_id,
      unlocked_channels: nextUnlocked,
    }),
  }).then((r) => r.json());

  if (resp.packet) {
    state.packets = state.packets.map((p) =>
      p.packet_id === resp.packet.packet_id ? resp.packet : p
    );
    state.stats.asksSent += 1;
    const timeStr = new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
    const ageSex = `${pkt.encounter?.age_years || ""}${pkt.encounter?.sex || ""}`;

    if (!state.dispatchedQueriesByPacket[pkt.packet_id]) {
      state.dispatchedQueriesByPacket[pkt.packet_id] = [];
    }
    state.dispatchedQueriesByPacket[pkt.packet_id].push({
      id: queryId,
      label: qObj?.button_label || "Check Dispatched",
      prompt: qObj?.prompt || queryId,
      timeStr,
    });

    state.routedByStation.fast_track.push({
      packetId: pkt.packet_id,
      ageSex,
      chief: pkt.encounter?.chief_complaint,
      summary: qObj ? `${qObj.button_label}` : `Check ${queryId}`,
      timeStr,
      isCheck: true,
    });

    if (state.spatialArena && state.viewMode === "spatial") {
      state.spatialArena.launchHandoffPulses([{ station_id: "fast_track" }]);
    }

    showToast(`Dispatched ${qObj?.button_label || queryId} for ${pkt.packet_id}.`);
    renderAll();
  }
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
  let val = Math.max(0, Number(next[key] || 0) + delta);
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
    renderAll();
  }
}

async function loadInitialQueue() {
  const [pktResp, ledResp] = await Promise.all([
    fetch("/api/packets").then((r) => r.json()),
    fetch("/api/ledger").then((r) => r.json()),
  ]);

  state.capacity = pktResp.capacity || state.capacity;
  state.packets = pktResp.packets || [];
  state.ledgerCount = ledResp.count || 0;
  const ledCountEl = document.getElementById("ledger-count");
  if (ledCountEl) ledCountEl.textContent = String(state.ledgerCount);

  const ordered = sortedPackets();
  const flagship =
    ordered.find((p) => p.packet_id === "syn-ED-022") || ordered[0] || null;
  state.selectedPacketId = flagship ? flagship.packet_id : null;
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
            <tr><th>Horvitz-Thompson Override Estimate</th><td class="num">${(shadow.ht_estimated_override_rate * 100).toFixed(1)}% +- ${(shadow.se_total * 100).toFixed(1)}%</td></tr>
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
          <td class="tabular" style="font-family:ui-monospace,monospace;font-size:12px;">${esc(String(e.entry_hash || "").slice(0, 14))}…</td>
        </tr>
      `
    )
    .join("");

  body.innerHTML = `
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px;">
      <strong>Unbroken SHA-256 Chain: ${data.valid_hash_chain ? "Verified" : "Tamper Detected"}</strong>
      <span class="badge badge-neutral">${esc(data.count)} Total Entries</span>
    </div>
    <h3 style="margin:0 0 8px;font-size:14px;">Two-Phase Constrained Neyman Shadow Audit</h3>
    ${shadowHtml}
    <h3 style="margin:0 0 8px;font-size:14px;">Recent Hash-Chained Decisions</h3>
    <table class="metric-table">
      <thead><tr><th>Packet</th><th>Decision</th><th>Action</th><th>Entry SHA-256</th></tr></thead>
      <tbody>${rows || `<tr><td colspan="4">No decisions logged yet.</td></tr>`}</tbody>
    </table>
  `;
}

async function openAuditDialog() {
  const dlg = document.getElementById("dialog-audit");
  const body = document.getElementById("audit-dialog-body");
  if (!dlg || !body) return;
  dlg.showModal();
  body.textContent = "Retrieving Statistical Safety Engine verification receipt…";

  const a = await fetch("/api/safety/audit").then((r) => r.json());
  const cs = a.cohort_summary || {};
  const sa = a.stage_a_two_phase_discovery || {};
  const sp = sa.stress_pool_audit || {};
  const sb = a.stage_b_streaming_voi_and_conformal || {};
  const sg = sb.dual_conformal_safe_gate || {};
  const cf = sb.conformal_fdr_vs_mondrian || {};

  body.innerHTML = `
    <div class="voi-note-box" style="margin-bottom:14px;">
      <strong>Split SHA-256:</strong> <code>${esc(String(a.split_hash || "").slice(0, 32))}…</code> ·
      <strong>Receipt SHA-256:</strong> <code>${esc(String(a.receipt_sha256 || "").slice(0, 24))}…</code>
    </div>
    <table class="metric-table">
      <tbody>
        <tr><th>Cohort Size &amp; Natural Prevalence</th><td class="num">N = ${esc(cs.n_encounters)} (π = ${(cs.natural_prevalence * 100).toFixed(1)}%)</td></tr>
        <tr><th>Held-Out Test AUROC (Stage-1 -> Stage-2)</th><td class="num">${esc(cs.stage1_test_auroc)} -> ${esc(cs.stage2_test_auroc)}</td></tr>
        <tr><th>15% Constrained Neyman vs. 15% SRS Pilot AUROC</th><td class="num">${esc(sa.mean_auroc_neyman_15pct)} vs. ${esc(sa.mean_auroc_srs_15pct)} (85% query savings)</td></tr>
        <tr><th>12-Channel Stress Pool (Null / Twin / Baseline Echo)</th><td class="num">${esc(sp.sequential_null_selected)} / ${esc(sp.sequential_twin_selected)} / ${esc(sp.sequential_echo_selected)}</td></tr>
        <tr><th>Martingale VOI Net Benefit Gain over Top-Risk</th><td class="num">+${esc(sb.voi_vs_top_risk?.nb_advantage_per_1k_over_top_risk)} per 1,000 arrivals</td></tr>
        <tr><th>Jin &amp; Candès CfBH Realized FDP vs. Mondrian {1}</th><td class="num">${Number(cf.cfbh_tie_smoothed?.realized_fdp ?? 0).toFixed(3)} vs. ${Number(cf.mondrian_singleton_pos?.realized_fdp ?? 0).toFixed(3)}</td></tr>
        <tr><th>Dual Conformal Safe-Gate Test FOR (Cal 95% UCB &lt;= 5%)</th><td class="num">${(Number(sg.test_realized_for ?? 0) * 100).toFixed(2)}% (${(Number(sg.test_certified_routine_rate ?? 0) * 100).toFixed(1)}% certified)</td></tr>
      </tbody>
    </table>
  `;
}

/* --------------------------------------------------------------------------
 * Event Delegation & Keyboard Speed Shortcuts
 * -------------------------------------------------------------------------- */
function bindEvents() {
  document.addEventListener("click", (ev) => {
    const viewTab = ev.target.closest("[data-view-tab]");
    if (viewTab) {
      setViewMode(viewTab.dataset.viewTab);
      return;
    }

    const stBtn = ev.target.closest("[data-open-station]");
    if (stBtn) {
      const stId = stBtn.dataset.openStation;
      if (state.viewMode === "spatial") {
        const key = `station:${stId}`;
        state.wingMode = state.wingMode === key ? null : key;
        renderAll();
      } else {
        state.focusedStationId = state.focusedStationId === stId ? "" : stId;
        renderAll();
        if (state.focusedStationId) {
          document.getElementById(`station-card-${stId}`)?.scrollIntoView({ behavior: "smooth", block: "nearest" });
        }
      }
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
      if (state.wingMode === "modify") ensureModifyDraft(activePacket());
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

    const wingToggle = ev.target.closest("[data-toggle-wing]");
    if (wingToggle) {
      const target = wingToggle.dataset.toggleWing;
      state.wingMode = state.wingMode === target ? null : target;
      if (state.wingMode === "modify") ensureModifyDraft(activePacket());
      renderAll();
      return;
    }

    if (ev.target.closest("[data-close-wing]")) {
      state.wingMode = null;
      renderAll();
      return;
    }

    const modBtn = ev.target.closest("[data-mod-field]");
    if (modBtn) {
      const d = ensureModifyDraft(activePacket());
      if (!d) return;
      const f = modBtn.dataset.modField;
      const v = modBtn.dataset.modVal;
      d[f] = f === "holdOrders" ? v === "true" : v;
      renderAll();
      return;
    }

    const qBtn = ev.target.closest("[data-trigger-query]");
    if (qBtn) {
      triggerActionableQuery(qBtn.dataset.triggerQuery);
      return;
    }

    const actBtn = ev.target.closest("[data-action]");
    if (actBtn) {
      if (actBtn.dataset.action === "approve-route") {
        executeRouteForPacket(false);
      } else if (actBtn.dataset.action === "commit-modified") {
        executeRouteForPacket(true);
      }
      return;
    }

    const closeDlg = ev.target.closest("[data-close-dialog]");
    if (closeDlg) {
      document.getElementById(closeDlg.dataset.closeDialog)?.close();
      return;
    }

    if (ev.target.closest("#btn-reset-camera")) {
      state.spatialArena?.resetCamera();
    }
  });

  document.addEventListener("change", (ev) => {
    if (ev.target.id === "mod-reason-select" && state.modifyDraft) {
      state.modifyDraft.reason = ev.target.value;
    }
  });

  // High-speed keyboard navigation for busy clinicians
  document.addEventListener("keydown", (ev) => {
    if (["INPUT", "TEXTAREA", "SELECT"].includes(document.activeElement?.tagName || "")) return;
    if (document.querySelector("dialog[open]")) return;

    if (ev.code === "Space") {
      ev.preventDefault();
      const pkt = activePacket();
      const plan = pkt ? effectiveRoutingPlan(pkt) : { isModifiedDraft: false };
      executeRouteForPacket(Boolean(plan.isModifiedDraft));
    } else if (ev.key === "d" || ev.key === "D") {
      state.wingMode = state.wingMode === "details" ? null : "details";
      renderAll();
    } else if (ev.key === "m" || ev.key === "M") {
      state.wingMode = state.wingMode === "modify" ? null : "modify";
      if (state.wingMode === "modify") ensureModifyDraft(activePacket());
      renderAll();
    } else if (ev.key === "a" || ev.key === "A") {
      state.wingMode = state.wingMode === "ask" ? null : "ask";
      renderAll();
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
      showToast(`Loaded ${data.accepted} synthetic packet(s).`);
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

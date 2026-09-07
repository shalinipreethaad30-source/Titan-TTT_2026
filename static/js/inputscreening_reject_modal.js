/* ============================================================================
 * Input Screening – Partial Reject Modal (manual scan flow).
 *
 * Fix-pack (Apr 2026) implements:
 *   1. Tap an active tray pill to fill the next empty scan slot
 *   2. Active tray pills are NOT repeated inside the delink section
 *   3. Tray-id input limited to 9 characters (maxlength)
 *   4. Auto-validate when length === 9; on success focus next empty slot,
 *      on failure mark input red + reselect text so user can retype
 *   5. Once scanned, the corresponding active-tray pill turns gray
 *   6. "Clear" button resets all user inputs (keeps lot context)
 *   7. "Save Draft" button persists scratch state to localStorage
 *   8. Body text is not bold (only headings/section titles are)
 *   9. Live "insight" chip in the header reports what the user is doing
 *      and the result of every validation
 *
 * Backend remains the single source of truth for every validation; the
 * front-end only renders state and forwards scans to /validate_scan/.
 * ============================================================================ */
(function () {
  "use strict";

  var TRAY_ID_LEN = 9;
  var DRAFT_KEY_PREFIX = "isrm_draft::";

  // ── Module state ──────────────────────────────────────────────────────────
  var state = {
    lotId: null,
    batchId: null,
    lotQty: 0,
    capacity: 0,
    trayType: null,
    activeTrays: [],
    reasons: [],
    rejectSlots: [],
    acceptSlots: [],
    rejectScans: [],
    acceptScans: [],
    delinkScans: [],    emptiedTrayIds: [],    counters: { reusable: 0, required: 0, delinkAvailable: 0 },
    previewTimer: null,
    isSubmitting: false,
    isPlanStale: false,
    scanEpoch: 0,
    fullLotReject: false,
    focusedInput: null,  // Track which input has focus for targeted pill taps
  };

  // ── DOM helpers ───────────────────────────────────────────────────────────
  function $(id) { return document.getElementById(id); }
  function escHtml(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;")
      .replace(/>/g, "&gt;").replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }
  function getCsrf() {
    var v = null;
    if (document.cookie) {
      document.cookie.split(";").forEach(function (c) {
        var t = c.trim();
        if (t.indexOf("csrftoken=") === 0) {
          v = decodeURIComponent(t.substring("csrftoken=".length));
        }
      });
    }
    return v;
  }

  // ── Status / message helpers ──────────────────────────────────────────────
  function setStatus(kind, msg) {
    var el = $("isrm-status");
    if (!el) return;
    el.className = "isrm-status " + (kind || "info") + (msg ? " show" : "");
    el.textContent = msg || "";
  }
  function clearStatus() { setStatus("info", ""); }

  function focusAndRevealInput(input, selectText) {
    if (!input) return false;
    window.setTimeout(function () {
      if (!document.body.contains(input) || input.disabled) return;
      input.focus();
      input.classList.add("isrm-auto-focused");
      window.setTimeout(function () {
        if (input.classList) input.classList.remove("isrm-auto-focused");
      }, 900);
      if (selectText) {
        try { input.select(); } catch (e) {}
        try { input.setSelectionRange(0, input.value.length); } catch (e2) {}
      }
      try {
        input.scrollIntoView({ block: "center", inline: "nearest", behavior: "smooth" });
      } catch (e3) {
        input.scrollIntoView(false);
      }
    }, 30);
    return true;
  }

  // ── Fix 9: Live insight chip ──────────────────────────────────────────────
  function setInsight(kind, msg) {
    var box = $("isrm-insight");
    var txt = $("isrm-insight-text");
    if (!box || !txt) return;
    box.className = "isrm-insight " + (kind || "info");
    txt.textContent = msg || "Idle";
    box.title = msg || "";
  }

  // ── Open / close ──────────────────────────────────────────────────────────
  function openModal(lotId, batchId) {
    state.lotId = lotId;
    state.batchId = batchId;
    resetUI();
    var modal = $("isRejectModal");
    if (modal) {
      // Clear the FOUC-blocking inline style applied in the template.
      modal.style.display = "";
      modal.classList.add("open");
    }
    setStatus("info", "Loading lot data…");
    setInsight("busy", "Loading lot data…");

    fetch("/inputscreening/reject_modal_context/?lot_id=" + encodeURIComponent(lotId), {
      credentials: "same-origin",
    })
      .then(function (r) { return r.json(); })
      .then(function (data) {
        if (!data.success) {
          setStatus("error", data.error || "Failed to load lot data.");
          setInsight("error", "Failed to load lot.");
          return;
        }
        applyContext(data);
        loadDraftIfAny();
      })
      .catch(function () {
        setStatus("error", "Could not connect to server.");
        setInsight("error", "Network error.");
      });
  }

  function closeModal() {
    var modal = $("isRejectModal");
    if (modal) {
      modal.classList.remove("open");
      modal.style.display = "none";
    }
    clearTimeout(state.previewTimer);
    if (typeof window.restoreRowPosition === "function") window.restoreRowPosition();
  }

  function resetUI() {
    state.scanEpoch += 1;
    state.lotQty = 0; state.capacity = 0; state.trayType = null;
    state.activeTrays = []; state.reasons = [];
    state.rejectSlots = []; state.acceptSlots = [];
    state.rejectScans = []; state.acceptScans = []; state.delinkScans = [];
    state.emptiedTrayIds = [];
    state.counters = { reusable: 0, required: 0, delinkAvailable: 0 };
    state.isSubmitting = false;
    state.isPlanStale = false;

    ["isrm-h-lotqty", "isrm-tray-type", "isrm-capacity",
     "isrm-total-qty", "isrm-active-trays"
    ].forEach(function (id) { var e = $(id); if (e) e.textContent = "—"; });
    var rEl = $("isrm-total-reject"); if (rEl) rEl.textContent = "0";
    var aEl = $("isrm-total-accept"); if (aEl) aEl.textContent = "0";
    var rmEl = $("isrm-remarks"); if (rmEl) rmEl.value = "";
    var grid = $("isrm-reason-grid");
    if (grid) grid.innerHTML = "";
    $("isrm-reject-rows").innerHTML = '<div class="isrm-help-line" style="text-align:center;">Enter rejection quantities to begin</div>';
    $("isrm-accept-rows").innerHTML = '<div class="isrm-help-line" style="text-align:center;">Enter rejection quantities to begin</div>';
    $("isrm-delink-rows").innerHTML = "";
    var _ccReset = $("isrm-calc-card"); if (_ccReset) _ccReset.style.display = "none";
    var _crReset = $("isrm-calc-rule"); if (_crReset) _crReset.style.display = "none";
    $("isrm-sec-delink").style.display = "none";
    $("isrm-sec-alloc").classList.add("isrm-locked");
    $("isrm-sec-delink").classList.add("isrm-locked");
    $("isrm-submit-btn").disabled = true;
    $("isrm-reject-count").textContent = "0";
    $("isrm-accept-count").textContent = "0";
    $("isrm-delink-count").textContent = "0";
    var lotRej = $("isrm-lot-reject-toggle"); if (lotRej) lotRej.checked = false;
    state.fullLotReject = false;
    var _bodyReset = document.querySelector("#isRejectModal .isrm-body");
    if (_bodyReset) _bodyReset.classList.remove("isrm-full-reject-mode");
    var _submitReset = $("isrm-submit-btn");
    if (_submitReset) _submitReset.textContent = "Submit";
    setInsight("info", "Idle");
  }

  // ── Apply initial context ────────────────
  function applyContext(data) {
    state.lotQty = data.lot_qty || 0;
    state.capacity = data.tray_capacity || 0;
    state.trayType = data.tray_type || null;
    state.activeTrays = data.active_trays || [];
    state.reasons = data.rejection_reasons || [];
    // Store backend draft for restore after context is applied
    state._backendDraft = data.draft_data || null;

    $("isrm-h-batch").textContent = data.plating_stk_no || "—";
    $("isrm-h-lotqty").textContent = state.lotQty;
    $("isrm-tray-type").textContent = state.trayType || "—";
    $("isrm-capacity").textContent = state.capacity || "—";
    $("isrm-total-qty").textContent = state.lotQty;
    $("isrm-active-trays").textContent = state.activeTrays.length;

    renderActivePills();
    renderReasonGrid();
    
    // BUG FIX 3: Render delink section on modal open so options are visible from start
    renderDelinkSection();
    
    clearStatus();
    setInsight("info", "Ready. Enter rejection qty.");
  }

  // ── Fix 5: track which active-tray pills are in use ──────────────────────
  function usedActiveIds() {
    var s = new Set();
    state.rejectScans.forEach(function (x) { if (x) s.add((x.tray_id || "").toUpperCase()); });
    state.acceptScans.forEach(function (x) { if (x) s.add((x.tray_id || "").toUpperCase()); });
    state.delinkScans.forEach(function (x) { if (x) s.add((x.tray_id || "").toUpperCase()); });
    return s;
  }

  // ── Render active tray pills (Fix 1: tap-to-pick, Fix 5: gray when used) ─
  // Pills are disabled during reject step if backend doesn't allow reuse (case 1: reject qty too low).
  // Also disabled if slot plan hasn't been fetched yet (rejectSlots empty).
  // Once reject step completes, pills become available for accept/delink steps.
  function renderActivePills() {
    var c = $("isrm-active-pills");
    if (!c) return;
    if (!state.activeTrays.length) {
      c.innerHTML = '<span class="isrm-help-line">No active trays found</span>';
      return;
    }
    
    var used = usedActiveIds();
    var inRejectStep = !rejectStepDone();
    var reuseAllowed = state.counters.reusable > 0;
    var slotPlanReady = state.rejectSlots.length > 0;
    // Disable pills if: in reject step AND (no reuse allowed OR slot plan not ready yet)
    var pillsDisabled = inRejectStep && (!reuseAllowed || !slotPlanReady);
    
    c.innerHTML = state.activeTrays.map(function (t) {
      var isUsed = used.has((t.tray_id || "").toUpperCase());
      var classes = "isrm-pill " + (isUsed ? "used" : "");
      var style = "";
      var dataAttr = 'data-tray-id="' + escHtml(t.tray_id) + '"';
      var title = isUsed ? "Already used" : "Tap to pick";
      
      if (pillsDisabled && !isUsed) {
        // Disable unused pills during reject step when no reuse allowed or plan not ready
        classes += " disabled";
        style = ' style="opacity:0.4;cursor:not-allowed;pointer-events:none;"';
        dataAttr = ""; // Remove data attribute so click handler won't attach
        title = slotPlanReady ? "Scan new tray for reject" : "Enter reject qty first";
      }
      
      return '<span class="' + classes + '"' + style + ' ' + dataAttr + ' title="' + title + '">' +
        escHtml(t.tray_id) +
        (t.top_tray ? '<span class="isrm-pill-top">TOP</span>' : '') +
        '<span class="isrm-pill-qty">' + (t.qty != null ? t.qty : "?") + '</span>' +
        '</span>';
    }).join("");

    // Only attach click handlers to enabled pills (those with data-tray-id)
    c.querySelectorAll(".isrm-pill[data-tray-id]").forEach(function (pill) {
      pill.addEventListener("click", function () {
        if (this.classList.contains("used")) return;
        var trayId = (this.getAttribute("data-tray-id") || "").toUpperCase();
        pickIntoNextEmptySlot(trayId);
      });
    });
  }

  // ── Render reason input grid ──────────────────────────────────────────────
  function renderReasonGrid() {
    var grid = $("isrm-reason-grid");
    if (!grid) return;
    if (!state.reasons.length) {
      grid.innerHTML = '<div class="isrm-help-line" style="grid-column:1/-1;text-align:center;">No rejection reasons configured</div>';
      return;
    }
    grid.innerHTML = state.reasons.map(function (r) {
      return '<div class="isrm-reason-row">' +
        '<span class="isrm-reason-id">' + escHtml(r.rejection_reason_id) + '</span>' +
        '<span class="isrm-reason-text">' + escHtml(r.rejection_reason) + '</span>' +
        '<input type="number" min="0" max="' + state.lotQty + '" value="0" ' +
        'class="isrm-qty-input" data-reason-id="' + escHtml(r.rejection_reason_id) +
        '" data-reason-text="' + escHtml(r.rejection_reason) + '" />' +
        '</div>';
    }).join("");

    grid.querySelectorAll(".isrm-qty-input").forEach(function (inp) {
      inp.addEventListener("input", function () {
        clampQty(this);
        updateTotals();
        // Reject qty changed → any previously scanned tray assignments are now
        // stale (slot count / allocation may differ). Clear all scan arrays and
        // re-render immediately so the user never sees or submits stale data.
        state.rejectScans = state.rejectSlots.map(function () { return null; });
        state.acceptScans = state.acceptSlots.map(function () { return null; });
        state.delinkScans = [];
        renderRejectRows();
        renderAcceptRows();
        renderDelinkSection();
        renderActivePills();
        scheduleSlotPlan();
        setInsight("busy", "Qty changed — scan slots cleared. Updating allocation…");
      });
    });
  }

  function clampQty(input) {
    var v = parseInt(input.value, 10) || 0;
    if (v < 0) v = 0;
    if (state.lotQty > 0 && v > state.lotQty) v = state.lotQty;
    var others = collectRejectionEntries()
      .filter(function (e) { return e.reason_id !== input.getAttribute("data-reason-id"); })
      .reduce(function (s, e) { return s + e.qty; }, 0);
    var max = state.lotQty - others;
    if (v > max) v = Math.max(0, max);
    input.value = v;
  }

  function collectRejectionEntries() {
    var out = [];
    var grid = $("isrm-reason-grid");
    if (!grid) return out;
    grid.querySelectorAll(".isrm-qty-input").forEach(function (inp) {
      var qty = parseInt(inp.value, 10) || 0;
      if (qty > 0) {
        out.push({
          reason_id: inp.getAttribute("data-reason-id"),
          reason_text: inp.getAttribute("data-reason-text"),
          qty: qty,
        });
      }
    });
    return out;
  }

  // Returns total shortage qty (SHORTAGE reason entries only).
  function totalShortage() {
    var grid = $("isrm-reason-grid");
    if (!grid) return 0;
    var total = 0;
    grid.querySelectorAll(".isrm-qty-input").forEach(function (inp) {
      var rt = (inp.getAttribute("data-reason-text") || "").toUpperCase();
      if (rt.indexOf("SHORTAGE") !== -1) {
        total += parseInt(inp.value, 10) || 0;
      }
    });
    return total;
  }

  function totalReject() {
    return collectRejectionEntries().reduce(function (s, e) { return s + e.qty; }, 0);
  }

  function updateTotals() {
    var tr = totalReject();
    $("isrm-total-reject").textContent = tr;
    $("isrm-total-accept").textContent = Math.max(0, state.lotQty - tr);
  }

  // ── Slot plan from backend ────────────────────────────────────────────────
  function scheduleSlotPlan() {
    state.isPlanStale = true;
    updateSubmitState();
    clearTimeout(state.previewTimer);
    state.previewTimer = setTimeout(fetchSlotPlan, 350);
  }

  function fetchSlotPlan() {
    var entries = collectRejectionEntries();
    var tr = entries.reduce(function (s, e) { return s + e.qty; }, 0);
    if (tr <= 0) {
      state.rejectSlots = []; state.acceptSlots = [];
      state.rejectScans = []; state.acceptScans = []; state.delinkScans = [];
      var _ccF = $('isrm-calc-card'); if (_ccF) _ccF.style.display = 'none';
      var _crF = $('isrm-calc-rule'); if (_crF) _crF.style.display = 'none';
      $("isrm-sec-alloc").classList.add("isrm-locked");
      $("isrm-sec-delink").style.display = "none";
      $("isrm-reject-rows").innerHTML = '<div class="isrm-help-line" style="text-align:center;">Enter rejection quantities to begin</div>';
      $("isrm-accept-rows").innerHTML = '<div class="isrm-help-line" style="text-align:center;">Enter rejection quantities to begin</div>';
      $("isrm-reject-count").textContent = "0";
      $("isrm-accept-count").textContent = "0";
      renderActivePills();
      updateSubmitState();
      setInsight("info", "Ready. Enter rejection qty.");
      return;
    }
    if (tr >= state.lotQty) {
      setStatus("error", "Reject qty must be less than lot qty (" + state.lotQty + ").");
      setInsight("error", "Reject qty too high.");
      return;
    }

    fetch("/inputscreening/allocation_preview/", {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json", "X-CSRFToken": getCsrf() },
      body: JSON.stringify({
        lot_id: state.lotId,
        rejection_entries: entries,
        delink_count: 0,
      }),
    })
      .then(function (r) { return r.json(); })
      .then(function (data) {
        applySlotPlan(data);
        setInsight("info", "Allocation ready. Scan reject trays.");
      })
      .catch(function () {
        setStatus("error", "Could not fetch slot plan.");
        setInsight("error", "Network error fetching plan.");
      });
  }

  function applySlotPlan(data) {
    state.isPlanStale = false;

    // Partial reject tray first; full reject trays below.
    var rejectPairs = (data.reject_slots || []).map(function (slot, i) {
      return { slot: slot, scan: state.rejectScans[i] || null, originalIndex: i };
    });
    rejectPairs.sort(function (a, b) {
      var aq = Number(a.slot && a.slot.qty != null ? a.slot.qty : 0);
      var bq = Number(b.slot && b.slot.qty != null ? b.slot.qty : 0);
      if (aq !== bq) return aq - bq;
      return a.originalIndex - b.originalIndex;
    });
    state.rejectSlots = rejectPairs.map(function (p) { return p.slot; });
    state.rejectScans = rejectPairs.map(function (p) { return p.scan; });

    state.acceptSlots = data.accept_slots || [];
    state.emptiedTrayIds = (data.emptied_tray_ids || []).map(function (t) {
      return (t || "").toUpperCase();
    });
    state.counters = {
      reusable: data.reusable_count || 0,
      required: data.new_required || 0,
      delinkAvailable: data.delink_available || 0,
    };

    state.acceptScans = state.acceptSlots.map(function (_, i) {
      return state.acceptScans[i] || null;
    });
    var activeIds = new Set(state.activeTrays.map(function (t) {
      return (t.tray_id || "").toUpperCase();
    }));
    state.delinkScans = state.delinkScans.filter(function (d) {
      return activeIds.has((d.tray_id || "").toUpperCase());
    });

    $("isrm-c-delink-eligible").textContent = state.counters.delinkAvailable || 0;
    $("isrm-c-reused").textContent = 0;
    $("isrm-c-delinked").textContent = 0;
    $("isrm-c-required").textContent = state.counters.required;
    // Update calc card inside Step 1
    var calcCard = $('isrm-calc-card');
    var calcRule = $('isrm-calc-rule');
    if (calcCard) {
      var acceptQtyEl = $('isrm-calc-accept-qty');
      var lotQtyEl = $('isrm-calc-lot-qty');
      if (acceptQtyEl) acceptQtyEl.textContent = data.total_accept_qty != null ? data.total_accept_qty : '—';
      if (lotQtyEl) lotQtyEl.textContent = state.lotQty;
      calcCard.style.display = 'flex';
    }
    if (calcRule) calcRule.style.display = '';

    $("isrm-reject-count").textContent = state.rejectSlots.length;
    $("isrm-accept-count").textContent = state.acceptSlots.length;
    $("isrm-delink-count").textContent = state.counters.delinkAvailable || 0;

    $("isrm-sec-alloc").classList.remove("isrm-locked");
    $("isrm-sec-delink").style.display = state.counters.delinkAvailable > 0 ? "" : "none";

    renderRejectRows();
    // Do NOT auto-fill - let user choose which tray to reuse for which reject slot
    renderAcceptRows();
    renderDelinkSection();
    renderActivePills();
    updateSubmitState();
    clearStatus();
  }

  // ── Render reject rows (Section 2 left) ───────────────────────────────────
  function renderRejectRows() {
    var c = $("isrm-reject-rows");
    if (!state.rejectSlots.length) {
      c.innerHTML = '<div class="isrm-help-line" style="text-align:center;">No reject slots</div>';
      return;
    }
    c.innerHTML = state.rejectSlots.map(function (slot, i) {
      var scan = state.rejectScans[i];
      var filled = !!scan;
      var sourceBadge = "";
      if (filled) {
        sourceBadge = scan.source === "new"
          ? '<span class="isrm-badge new">NEW</span>'
          : '<span class="isrm-badge reused">REUSED</span>';
      }
      return '<div class="isrm-alloc-row ' + (filled ? "filled" : "") + '" data-slot-idx="' + i + '">' +
        '<span class="isrm-alloc-num">' + (i + 1) + '</span>' +
        '<input type="text" class="isrm-scan-input isrm-reject-scan" ' +
          'data-slot-idx="' + i + '" maxlength="' + TRAY_ID_LEN + '" autocomplete="off" ' +
          'placeholder="SCAN / TAP REJECT TRAY" ' +
          'value="' + escHtml(filled ? scan.tray_id : "") + '" ' +
          (filled ? "readonly" : "") + ' />' +
        '<span class="isrm-badge reason">' + escHtml(slot.reason_id) + '</span>' +
        '<span class="isrm-alloc-qty">' + slot.qty + '</span>' +
        sourceBadge +
        '</div>';
    }).join("");

    c.querySelectorAll(".isrm-reject-scan").forEach(function (inp) {
      if (!inp.readOnly) {
        attachScanHandlers(inp, "reject");
        // Track focus for targeted pill taps
        inp.addEventListener("focus", function () { state.focusedInput = this; });
        inp.addEventListener("blur", function () {
          // Clear focus after a short delay (allow pill tap to register first)
          setTimeout(function () { if (state.focusedInput === inp) state.focusedInput = null; }, 200);
        });
      }
    });
    // Err2 fix: tap a filled (readonly) reject input to enable editing
    c.querySelectorAll(".isrm-reject-scan[readonly]").forEach(function (inp) {
      inp.addEventListener("click", function () {
        // Enable in-place editing: remove readonly and attach full scan handlers
        // so typing 9 chars auto-validates and moves focus (err 1 fix).
        // select() ensures the old 9-char value is highlighted so a scanner or
        // manual keystroke replaces it immediately (maxlength=9 would block
        // insertion if the old text were not selected).
        this.removeAttribute("readonly");
        this.style.cursor = "text";
        attachScanHandlers(this, "reject");
        this.focus();
        try { this.select(); } catch (e) {}
        setInsight("info", "Editing reject slot " + (parseInt(this.getAttribute("data-slot-idx"), 10) + 1) + ". Scan or type new tray ID to replace.");
      });
    });
  }

  // ── Render accept rows (Section 2 right) ──────────────────────────────────
  function renderAcceptRows() {
    var c = $("isrm-accept-rows");
    if (!state.acceptSlots.length) {
      c.innerHTML = '<div class="isrm-help-line" style="text-align:center;">No accept slots</div>';
      return;
    }
    c.innerHTML = state.acceptSlots.map(function (slot, i) {
      var scan = state.acceptScans[i];
      var filled = !!scan;
      var sourceBadge = "";
      var topBadge = "";
      if (filled) {
        sourceBadge = scan.source === "free" || scan.source === "new_free"
          ? '<span class="isrm-badge new">FREE</span>'
          : '<span class="isrm-badge existing">EXISTING</span>';
        // TOP is position-based: slot 0 is always the top tray regardless of original tray flag
        if (i === 0) topBadge = '<span class="isrm-badge top">TOP</span>';
      }
      return '<div class="isrm-alloc-row ' + (filled ? "filled" : "") + '" data-slot-idx="' + i + '">' +
        '<span class="isrm-alloc-num">' + (i + 1) + '</span>' +
        '<input type="text" class="isrm-scan-input isrm-accept-scan" ' +
          'data-slot-idx="' + i + '" maxlength="' + TRAY_ID_LEN + '" autocomplete="off" ' +
          'placeholder="SCAN / TAP ACCEPT TRAY" ' +
          'value="' + escHtml(filled ? scan.tray_id : "") + '" ' +
          (filled ? "readonly" : (rejectStepDone() ? "" : "disabled")) + ' />' +
        topBadge +
        '<span class="isrm-alloc-qty">' + slot.qty + '</span>' +
        sourceBadge +
        '</div>';
    }).join("");

    c.querySelectorAll(".isrm-accept-scan").forEach(function (inp) {
      if (!inp.readOnly) {
        attachScanHandlers(inp, "accept");
        // Track focus for targeted pill taps
        inp.addEventListener("focus", function () { state.focusedInput = this; });
        inp.addEventListener("blur", function () {
          setTimeout(function () { if (state.focusedInput === inp) state.focusedInput = null; }, 200);
        });
      }
    });
    // Allow editing filled (readonly) accept inputs instead of just clearing
    c.querySelectorAll(".isrm-accept-scan[readonly]").forEach(function (inp) {
      inp.addEventListener("click", function () {
        // Enable in-place editing: remove readonly and attach full scan handlers
        // so typing 9 chars auto-validates and moves focus (err 1 fix).
        // select() ensures the old 9-char value is highlighted so a scanner or
        // manual keystroke replaces it immediately (maxlength=9 would block
        // insertion if the old text were not selected).
        this.removeAttribute("readonly");
        this.style.cursor = "text";
        attachScanHandlers(this, "accept");
        this.focus();
        try { this.select(); } catch (e) {}
        setInsight("info", "Editing accept slot " + (parseInt(this.getAttribute("data-slot-idx"), 10) + 1) + ". Scan or type new tray ID to replace.");
      });
    });
  }

  // ── Render delink section (Fix 2: no duplicate active-tray pills) ────────
  function renderDelinkSection() {
    var rows = $("isrm-delink-rows");
    if (!rows) return;

    // Remaining delink slots = emptied count − active trays already reused
    // in reject scans. Once reject fully consumes the emptied pool there
    // is nothing left to delink, so the whole section hides.
    var activeIds = new Set((state.activeTrays || []).map(function (t) {
      return (t.tray_id || "").toUpperCase();
    }));
    var reusedInReject = state.rejectScans.filter(function (s) {
      return s && activeIds.has((s.tray_id || "").toUpperCase());
    }).length;
    var remainingDelink = Math.max(
      0,
      (state.counters.delinkAvailable || 0) - reusedInReject
    );

    // Drop any already-scanned delink rows that no longer fit inside the
    // shrunken pool (e.g. the user just reused one more emptied tray).
    if (state.delinkScans.length > remainingDelink) {
      state.delinkScans = state.delinkScans.slice(0, remainingDelink);
    }

    if (remainingDelink <= 0 && state.delinkScans.length === 0) {
      $("isrm-sec-delink").style.display = "none";
      rows.innerHTML = "";
      return;
    }
    $("isrm-sec-delink").style.display = "";
    $("isrm-sec-delink").classList.toggle("isrm-locked", !rejectStepDone());

    var html = state.delinkScans.map(function (d, i) {
      return '<div class="isrm-delink-row" style="margin-bottom:6px;">' +
        '<span class="isrm-alloc-num">' + (i + 1) + '</span>' +
        '<input type="text" class="isrm-scan-input" data-delink-idx="' + i + '" value="' + escHtml(d.tray_id) + '" readonly />' +
        '</div>';
    }).join("");

    // ERR 1 FIX: Render ALL remaining empty slots at once, not progressively
    var emptySlots = remainingDelink - state.delinkScans.length;
    if (emptySlots > 0) {
      var disabled = !rejectStepDone();
      for (var i = 0; i < emptySlots; i++) {
        var slotIdx = state.delinkScans.length + i + 1;
        html += '<div class="isrm-delink-row">' +
          '<span class="isrm-alloc-num">' + slotIdx + '</span>' +
          '<input type="text" class="isrm-scan-input isrm-delink-scan" ' +
            'maxlength="' + TRAY_ID_LEN + '" autocomplete="off" ' +
            'placeholder="SCAN / TAP DELINK TRAY" ' + (disabled ? "disabled" : "") + ' />' +
          '</div>';
      }
    }
    rows.innerHTML = html;

    // Attach handlers to all new delink scan inputs
    var newInputs = rows.querySelectorAll(".isrm-delink-scan");
    newInputs.forEach(function(newInput) {
      attachScanHandlers(newInput, "delink");
      // Track focus for targeted pill taps
      newInput.addEventListener("focus", function () { state.focusedInput = this; });
      newInput.addEventListener("blur", function () {
        setTimeout(function () { if (state.focusedInput === newInput) state.focusedInput = null; }, 200);
      });
    });

    // Allow editing filled (readonly) delink inputs
    rows.querySelectorAll(".isrm-delink-row .isrm-scan-input[readonly]").forEach(function (inp) {
      inp.addEventListener("click", function () {
        // Enable in-place editing: remove readonly and show edit cursor
        this.removeAttribute("readonly");
        this.style.cursor = "text";
        this.focus();
        setInsight("info", "Editing delink tray. Press Enter to confirm.");
      });
      // Re-validate when user finishes editing
      inp.addEventListener("blur", function () {
        var v = (this.value || "").trim().toUpperCase();

        if (!v) {
          var delinkIdx = parseInt(this.getAttribute("data-delink-idx"), 10);
          if (!isNaN(delinkIdx) && state.delinkScans[delinkIdx]) {
            state.delinkScans.splice(delinkIdx, 1);
            renderDelinkSection();
            renderActivePills();
            updateReuseCounter();
            updateSubmitState();
            setInsight("info", "Delink tray cleared. Tray is available to tap again.");
          }
          return;
        }

        if (v.length === TRAY_ID_LEN) {
          attemptScan(this, "delink", v);
        }
      });
    });
  }

  // ── Fix 1 & 4: Unified scan input behaviour ──────────────────────────────
  function attachScanHandlers(input, slotType) {
    input.addEventListener("input", function () {
      this.value = (this.value || "").toUpperCase();
      this.classList.remove("invalid");
      var v = this.value.trim();

      // Keep active-tray pill state in sync when a previously-filled tray ID
      // is manually erased. Release only the matching in-memory scan slot;
      // existing backend validation and allocation rules are unchanged.
      if (!v) {
        var slotIdx = this.getAttribute("data-slot-idx");
        if (slotIdx !== null && slotIdx !== undefined) {
          var idx = parseInt(slotIdx, 10);

          if (slotType === "reject" && state.rejectScans[idx]) {
            state.rejectScans[idx] = null;

            // Reject is the first allocation phase. Once a reject tray is
            // manually removed, downstream delink/accept assignments are no
            // longer safe to keep. Release them so their active-tray pills
            // become tappable again and the normal Reject -> Delink -> Accept
            // flow can resume from the current state.
            state.delinkScans = [];
            state.acceptScans = state.acceptSlots.map(function () { return null; });

            renderRejectRows();
            renderDelinkSection();
            renderAcceptRows();
            renderActivePills();
            updateReuseCounter();
            updateSubmitState();
            setInsight("info", "Reject tray cleared. Dependent trays released for tapping.");
            return;
          }

          if (slotType === "accept" && state.acceptScans[idx]) {
            state.acceptScans[idx] = null;

            // Accept slot 0 is the user-selected top/partial tray and controls
            // the existing auto-fill cascade for slots 1+. If the top tray is
            // manually removed, release those dependent auto-filled accept
            // assignments too so their pills immediately become tappable.
            if (idx === 0) {
              for (var ai = 1; ai < state.acceptScans.length; ai++) {
                state.acceptScans[ai] = null;
              }
            }

            renderAcceptRows();
            renderActivePills();
            updateReuseCounter();
            updateSubmitState();
            setInsight("info", "Accept tray cleared. Tray is available to tap again.");
            return;
          }
        }
      }

      if (v.length > 0 && v.length < TRAY_ID_LEN) {
        setInsight("busy", "Typing " + slotType + " tray (" + v.length + "/" + TRAY_ID_LEN + ")…");
      }
      // Fix 4: auto-validate as soon as the 9th char is entered
      if (v.length === TRAY_ID_LEN) {
        attemptScan(this, slotType, v);
      }
    });
    input.addEventListener("keydown", function (e) {
      if (e.key === "Enter") {
        e.preventDefault();
        e.stopPropagation();
        var v = (this.value || "").trim().toUpperCase();
        if (v) {
          attemptScan(this, slotType, v);
        } else {
          focusAndRevealInput(this, false);
        }
      }
    });
    input.addEventListener("blur", function () {
      var v = (this.value || "").trim().toUpperCase();
      if (v && v.length === TRAY_ID_LEN && !this.readOnly) attemptScan(this, slotType, v);
    });
  }

  function attemptScan(input, slotType, trayId) {
    trayId = (trayId || "").trim().toUpperCase();
    if (!trayId || input.getAttribute("data-scan-pending") === "1") return;
    // Let backend handle all validation - removed frontend accept-slot restriction
    // to give users flexibility in tray selection
    if (slotType === "accept" && !rejectStepDone()) {
      setStatus("error", "Please complete reject scans first.");
      setInsight("error", "Reject scans incomplete.");
      input.classList.add("invalid");
      focusAndRevealInput(input, true);
      return;
    }
    if (slotType === "delink" && !rejectStepDone()) {
      setStatus("error", "Please complete reject scans first.");
      setInsight("error", "Reject scans incomplete.");
      input.classList.add("invalid");
      focusAndRevealInput(input, true);
      return;
    }
    setInsight("busy", "Validating " + trayId + " (" + slotType + ")…");
    input.setAttribute("data-scan-pending", "1");
    input.disabled = true;
    var capturedEpoch = state.scanEpoch;
    // ERR 2 FIX: Pass slot index and current tray ID to exclude from duplicate check
    var slotIdx = input.getAttribute("data-slot-idx");
    var currentTrayId = null;
    if (slotIdx !== null && slotIdx !== undefined) {
      var idx = parseInt(slotIdx, 10);
      if (slotType === "reject" && state.rejectScans[idx]) {
        currentTrayId = state.rejectScans[idx].tray_id;
      } else if (slotType === "accept" && state.acceptScans[idx]) {
        currentTrayId = state.acceptScans[idx].tray_id;
      }
    }
    validateScan(slotType, trayId, currentTrayId, function (res) {
      input.disabled = false;
      input.removeAttribute("data-scan-pending");
      if (state.scanEpoch !== capturedEpoch) return;
      if (!res.valid) {
        setStatus("error", res.reason || "Invalid tray.");
        setInsight("error", trayId + " invalid: " + (res.reason || "rejected"));
        // Fix 4: keep selection on the invalid input so user can retype
        input.classList.add("invalid");
        focusAndRevealInput(input, true);
        return;
      }
      handleValidScan(slotType, input, res);
    });
  }

  function handleValidScan(slotType, input, res) {
    if (slotType === "reject") {
      var idx = parseInt(input.getAttribute("data-slot-idx"), 10);
      state.rejectScans[idx] = {
        tray_id: res.tray_id, source: res.source,
        qty: res.tray_qty, top: res.top_tray,
      };
      renderRejectRows();
      renderActivePills();
      renderDelinkSection();
      renderAcceptRows();
      updateReuseCounter();  // ✅ FIX: Dynamic reuse counter update
      setInsight("success", res.tray_id + " accepted as REJECT (" + res.source + ").");
      
      // When reject step completes: auto-navigate to delink (if exists) or accept
      // with smooth scroll to keep cursor visible
      if (rejectStepDone()) {
        setTimeout(function() {
          // Priority 1: Check if delink section has empty input
          var delinkInput = document.querySelector(".isrm-delink-scan:not([readonly]):not([disabled])");
          var targetInput = null;
          var targetMsg = "";
          
          if (delinkInput && !delinkInput.value) {
            // Delink section exists and has empty slot
            targetInput = delinkInput;
            targetMsg = "Reject done. Scan/tap delink trays.";
          } else {
            // No delink or all delink slots filled → move to accept
            targetInput = document.querySelector(".isrm-accept-scan:not([readonly]):not([disabled])");
            targetMsg = "Reject done. Now scan/tap accept top tray.";
          }
          
          // Focus and scroll the target input into view
          if (targetInput && !targetInput.value) {
            focusAndRevealInput(targetInput, false);
            setInsight("info", targetMsg);
          }
        }, 100);
      }
    } else if (slotType === "accept") {
      var aidx = parseInt(input.getAttribute("data-slot-idx"), 10);
      state.acceptScans[aidx] = {
        tray_id: res.tray_id, source: res.source,
        qty: res.tray_qty, top: res.top_tray,
      };
      renderAcceptRows();
      renderActivePills();
      autoFillRemainingAcceptSlots();
      updateReuseCounter();  // ✅ FIX: Dynamic reuse counter update
      setInsight("success", res.tray_id + " accepted as ACCEPT (" + res.source + ").");
    } else if (slotType === "delink") {
      // ✅ ERR1 FIX: Prevent duplicate tray IDs in delink scans
      var delinkTrayExists = state.delinkScans.some(function (d) {
        return (d.tray_id || "").toUpperCase() === (res.tray_id || "").toUpperCase();
      });
      if (delinkTrayExists) {
        setStatus("error", res.tray_id + " is already queued for delink. Remove the existing entry to rescan.");
        setInsight("error", res.tray_id + " already delinked – no duplicates allowed.");
        input.classList.add("invalid");
        input.value = "";
        focusAndRevealInput(input, false);
        return;
      }
      state.delinkScans.push({
        tray_id: res.tray_id, qty: res.tray_qty, top: res.top_tray,
      });
      renderDelinkSection();
      renderActivePills();
      updateReuseCounter();  // ✅ FIX: Dynamic reuse counter update
      setInsight("success", res.tray_id + " queued for DELINK.");
    }
    clearStatus();
    updateSubmitState();
    // Fix 4: focus the next empty scan slot after a successful scan
    focusNextEmptySlot(slotType);
  }

  function focusNextEmptySlot(slotType) {
    var selectors;
    if (slotType === "reject") {
      selectors = [".isrm-reject-scan", ".isrm-delink-scan", ".isrm-accept-scan"];
    } else if (slotType === "delink") {
      selectors = [".isrm-delink-scan", ".isrm-accept-scan"];
    } else {
      selectors = [".isrm-accept-scan"];
    }
    for (var i = 0; i < selectors.length; i++) {
      var nodes = document.querySelectorAll(selectors[i]);
      for (var j = 0; j < nodes.length; j++) {
        var n = nodes[j];
        if (!n.readOnly && !n.disabled && !n.value) {
          focusAndRevealInput(n, false);
          return;
        }
      }
    }
  }

  // ── Fix 1: Tap an active-tray pill → fill focused input or next empty slot ───
  function pickIntoNextEmptySlot(trayId) {
    // If user has focused a specific input, fill that one (gives control over which slot gets reused tray)
    if (state.focusedInput && !state.focusedInput.readOnly && !state.focusedInput.disabled) {
      var focusedSlotType = state.focusedInput.classList.contains("isrm-reject-scan") ? "reject"
                          : state.focusedInput.classList.contains("isrm-accept-scan") ? "accept"
                          : state.focusedInput.classList.contains("isrm-delink-scan") ? "delink"
                          : null;
      if (focusedSlotType) {
        state.focusedInput.value = trayId;
        attemptScan(state.focusedInput, focusedSlotType, trayId);
        return;
      }
    }

    // Otherwise, auto-fill next empty slot (original behavior)
    // Order: reject → delink (if available) → accept
    var _activeIds = new Set((state.activeTrays || []).map(function (t) {
      return (t.tray_id || "").toUpperCase();
    }));
    var _reusedInReject = state.rejectScans.filter(function (s) {
      return s && _activeIds.has((s.tray_id || "").toUpperCase());
    }).length;
    var _remainingDelink = Math.max(0, (state.counters.delinkAvailable || 0) - _reusedInReject);
    var delinkNeeded = _remainingDelink > 0 && state.delinkScans.length < _remainingDelink;
    var slotType = !rejectStepDone() ? "reject"
                 : delinkNeeded ? "delink"
                 : (state.acceptSlots.length && !acceptStepDone()) ? "accept"
                 : null;
    if (!slotType) {
      setInsight("error", "No empty slot available to fill.");
      return;
    }
    var sel = slotType === "reject" ? ".isrm-reject-scan"
            : slotType === "accept" ? ".isrm-accept-scan"
            : ".isrm-delink-scan";
    var nodes = document.querySelectorAll(sel);
    for (var i = 0; i < nodes.length; i++) {
      var n = nodes[i];
      if (!n.readOnly && !n.disabled && !n.value) {
        n.value = trayId;
        attemptScan(n, slotType, trayId);
        return;
      }
    }
    setInsight("error", "No empty " + slotType + " slot.");
  }

  // ── Auto-fill remaining accept slots (slots 1+) from unused active trays ────
  // Slot 0 is reserved for the top/partial tray which the user must scan first.
  // Once slot 0 is confirmed, this function cascades through slots 1+ in order,
  // filling each from unused active trays sorted ascending by qty.
  function autoFillRemainingAcceptSlots() {
    // Guard: do nothing until the user has confirmed the top-tray slot (slot 0).
    if (!state.acceptScans.length || state.acceptScans[0] === null) return;

    var used = new Set(collectAllUsedIds().map(function (id) { return id.toUpperCase(); }));
    // Sort unused active trays ascending by qty so smaller trays fill first.
    var unusedActive = state.activeTrays.filter(function (t) {
      return !used.has((t.tray_id || "").toUpperCase());
    }).sort(function (a, b) { return (a.qty || 0) - (b.qty || 0); });

    if (!unusedActive.length) return;

    // Only target empty slots at index >= 1 (leave slot 0 to user).
    var emptyInputs = Array.prototype.slice.call(
      document.querySelectorAll(".isrm-accept-scan")
    ).filter(function (n) {
      return !n.readOnly && !n.disabled && !n.value &&
             parseInt(n.getAttribute("data-slot-idx"), 10) >= 1;
    });
    if (!emptyInputs.length) return;
    var inp = emptyInputs[0];
    var trayId = unusedActive[0].tray_id.toUpperCase();
    inp.value = trayId;
    attemptScan(inp, "accept", trayId);
  }

  // ── Validate scan API ─────────────────────────────────────────────────────
  function validateScan(slotType, trayId, excludeTrayId, cb) {
    // ERR 2 FIX: Exclude current slot's tray ID from duplicate check when editing
    var used = collectAllUsedIds(excludeTrayId);
    fetch("/inputscreening/validate_scan/", {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json", "X-CSRFToken": getCsrf() },
      body: JSON.stringify({
        lot_id: state.lotId,
        slot_type: slotType,
        tray_id: trayId,
        used_tray_ids: used,
        reject_qty: totalReject(),
        shortage_qty: totalShortage(),
      }),
    })
      .then(function (r) { return r.json(); })
      .then(cb)
      .catch(function () { cb({ valid: false, reason: "Network error during scan." }); });
  }

  function collectAllUsedIds(excludeTrayId) {
    // ERR 2 FIX: When editing a slot, exclude that slot's current tray ID
    var ids = [];
    var excludeUpper = excludeTrayId ? excludeTrayId.toUpperCase() : null;
    state.rejectScans.forEach(function (s) {
      if (s && s.tray_id && (!excludeUpper || s.tray_id.toUpperCase() !== excludeUpper)) {
        ids.push(s.tray_id);
      }
    });
    state.acceptScans.forEach(function (s) {
      if (s && s.tray_id && (!excludeUpper || s.tray_id.toUpperCase() !== excludeUpper)) {
        ids.push(s.tray_id);
      }
    });
    state.delinkScans.forEach(function (s) {
      if (s && s.tray_id && (!excludeUpper || s.tray_id.toUpperCase() !== excludeUpper)) {
        ids.push(s.tray_id);
      }
    });
    return ids;
  }

  // ── ✅ FIX: Dynamic reuse counter (updates as user scans) ──────────────
  function updateReuseCounter() {
    var activeIds = new Set((state.activeTrays || []).map(function (t) {
      return (t.tray_id || "").toUpperCase();
    }));
    // Count DISTINCT active tray IDs that have been reused (scanned in reject slots)
    var reusedIds = new Set();
    state.rejectScans.forEach(function (s) {
      if (s && s.tray_id && activeIds.has((s.tray_id || "").toUpperCase())) {
        reusedIds.add((s.tray_id || "").toUpperCase());
      }
    });
    var reusedCount = reusedIds.size;

    // Count distinct active tray IDs delinked
    var delinkedIds = new Set();
    state.delinkScans.forEach(function (s) {
      if (s && s.tray_id && activeIds.has((s.tray_id || "").toUpperCase())) {
        delinkedIds.add((s.tray_id || "").toUpperCase());
      }
    });
    var delinkedCount = delinkedIds.size;

    // Update the three metrics in the calc card
    var delinkEligibleEl = $("isrm-c-delink-eligible");
    if (delinkEligibleEl) {
      delinkEligibleEl.textContent = state.counters.delinkAvailable || 0;
    }
    var reusedEl = $("isrm-c-reused");
    if (reusedEl) {
      reusedEl.textContent = reusedCount;
    }
    var delinkedEl = $("isrm-c-delinked");
    if (delinkedEl) {
      delinkedEl.textContent = delinkedCount;
    }
    // Show delink availability
    var delinkEl = $("isrm-c-delink");
    if (delinkEl) {
      var remainingDelink = Math.max(0, (state.counters.delinkAvailable || 0) - (state.delinkScans.length || 0));
      delinkEl.textContent = remainingDelink + " for delink";
    }
    // Update delink count in allocation grid header
    var delinkCountEl = $("isrm-delink-count");
    if (delinkCountEl) {
      delinkCountEl.textContent = state.delinkScans.length;
    }
  }

  // ── Step gating helpers ──────────────────────────────────────────────────
  function rejectStepDone() {
    // When there are no reject slots (shortage-only flow), reject scan is not
    // needed — return true so accept step and submit are not blocked.
    if (!state.rejectSlots.length) return true;
    return state.rejectScans.length === state.rejectSlots.length &&
           state.rejectScans.every(Boolean);
  }
  function acceptStepDone() {
    if (!state.acceptSlots.length) return true;
    return state.acceptScans.length === state.acceptSlots.length &&
           state.acceptScans.every(Boolean);
  }

  function updateSubmitState() {
    var shortage = totalShortage();
    var effectiveLotQty = state.lotQty - shortage;
    // Allow submit when: (there is reject qty OR shortage qty) AND the total
    // does not exceed lot qty AND all required scans are done.
    var ok = totalReject() > 0 &&
             totalReject() < state.lotQty &&
             !state.isPlanStale &&
             rejectStepDone() &&
             acceptStepDone() &&
             !state.isSubmitting;
    // Shortage-only path: no reject scans needed, accept scans must be done.
    var shortageOnlyOk = shortage > 0 &&
             totalReject() === shortage &&   // all entered qty is shortage
             !state.isPlanStale &&
             rejectStepDone() &&             // returns true (no reject slots)
             acceptStepDone() &&
             !state.isSubmitting;
    $("isrm-submit-btn").disabled = !(ok || shortageOnlyOk);

    if (state.acceptSlots.length) {
      var anyDisabled = !rejectStepDone();
      $("isrm-accept-rows").querySelectorAll(".isrm-accept-scan").forEach(function (inp) {
        if (!inp.readOnly) inp.disabled = anyDisabled;
      });
    }
    var delinkInput = $("isrm-delink-rows").querySelector(".isrm-delink-scan");
    if (delinkInput && !delinkInput.readOnly) {
      delinkInput.disabled = !rejectStepDone();
    }
    $("isrm-sec-delink").classList.toggle("isrm-locked", !rejectStepDone());
  }

  var lastClearSnapshot = null;

  function cloneValue(value) {
    try {
      return JSON.parse(JSON.stringify(value));
    } catch (e) {
      return value;
    }
  }

  function captureUndoSnapshot() {
    var grid = $("isrm-reason-grid");
    lastClearSnapshot = {
      rejectScans: cloneValue(state.rejectScans),
      acceptScans: cloneValue(state.acceptScans),
      delinkScans: cloneValue(state.delinkScans),
      fullLotReject: !!state.fullLotReject,
      remarks: ($("isrm-remarks") || {}).value || "",
      reasonQtys: grid ? Array.from(grid.querySelectorAll(".isrm-qty-input")).map(function (input) {
        return input.value;
      }) : [],
    };
  }

  function restoreUndoSnapshot() {
    if (!lastClearSnapshot) {
      setInsight("info", "Nothing to undo.");
      return false;
    }

    var grid = $("isrm-reason-grid");
    if (grid) {
      Array.from(grid.querySelectorAll(".isrm-qty-input")).forEach(function (input, index) {
        input.value = lastClearSnapshot.reasonQtys[index] || 0;
      });
    }

    state.rejectScans = cloneValue(lastClearSnapshot.rejectScans) || [];
    state.acceptScans = cloneValue(lastClearSnapshot.acceptScans) || [];
    state.delinkScans = cloneValue(lastClearSnapshot.delinkScans) || [];
    state.fullLotReject = !!lastClearSnapshot.fullLotReject;

    var rmEl = $("isrm-remarks"); if (rmEl) rmEl.value = lastClearSnapshot.remarks || "";
    var lotRej = $("isrm-lot-reject-toggle"); if (lotRej) lotRej.checked = state.fullLotReject;

    renderRejectRows();
    renderAcceptRows();
    renderDelinkSection();
    updateTotals();
    scheduleSlotPlan();
    renderActivePills();
    setInsight("success", "Undo complete.");
    return true;
  }

  // ── Fix 6: Clear all user inputs (keep lot context) ──────────────────────
  function clearAllInputs() {
    captureUndoSnapshot();
    state.scanEpoch += 1;  // invalidate any in-flight scan callbacks
    var grid = $("isrm-reason-grid");
    if (grid) {
      grid.querySelectorAll(".isrm-qty-input").forEach(function (i) { i.value = 0; });
    }
    state.rejectScans = state.rejectSlots.map(function () { return null; });
    state.acceptScans = state.acceptSlots.map(function () { return null; });
    state.delinkScans = [];
    var rmEl = $("isrm-remarks"); if (rmEl) rmEl.value = "";
    var lotRej = $("isrm-lot-reject-toggle"); if (lotRej) lotRej.checked = false;
    // Immediately re-render rows so DOM clears without waiting for the async slot plan.
    renderRejectRows();
    renderAcceptRows();
    renderDelinkSection();
    updateTotals();
    scheduleSlotPlan();
    renderActivePills();
    setInsight("info", "All inputs cleared.");
  }

  // ── Fix 7: Save / load draft via localStorage ────────────────────────────
  function draftKey() { return DRAFT_KEY_PREFIX + (state.lotId || ""); }

  function saveDraft() {
    if (!state.lotId) return;
    var entries = collectRejectionEntries();
    var payload = {
      lot_id: state.lotId,
      rejection_entries: entries,
      reject_assignments: state.rejectSlots.map(function (slot, i) {
        var s = state.rejectScans[i];
        return s ? { tray_id: s.tray_id, reason_id: slot.reason_id } : null;
      }).filter(Boolean),
      accept_assignments: state.acceptSlots.map(function (_, i) {
        var s = state.acceptScans[i];
        return s ? { tray_id: s.tray_id } : null;
      }).filter(Boolean),
      delink_tray_ids: (state.delinkScans || []).map(function (d) { return d.tray_id; }),
      remarks: ($("isrm-remarks") || {}).value || "",
      full_lot_reject: state.fullLotReject || false,
    };

    // Local fallback – retained so UI state is recoverable if the network
    // request fails before the backend acknowledges the draft.
    try {
      window.localStorage.setItem(draftKey(), JSON.stringify({
        ts: Date.now(),
        reasons: entries,
        rejectScans: state.rejectScans,
        acceptScans: state.acceptScans,
        delinkScans: state.delinkScans,
        remarks: payload.remarks,
        full_lot_reject: state.fullLotReject || false,
      }));
    } catch (e) { /* storage full – ignore, backend is SSOT */ }

    var draftBtn = $("isrm-draft-btn");
    if (draftBtn) { draftBtn.disabled = true; draftBtn.textContent = "Saving…"; }
    setInsight("busy", "Saving draft…");

    fetch("/inputscreening/save_draft/", {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json", "X-CSRFToken": getCsrf() },
      body: JSON.stringify(payload),
    })
      .then(function (r) { return r.json().then(function (j) { return { ok: r.ok, data: j }; }); })
      .then(function (resp) {
        if (draftBtn) { draftBtn.disabled = false; draftBtn.textContent = "Save Draft"; }
        if (!resp.ok || !resp.data.success) {
          setInsight("error", (resp.data && resp.data.error) || "Draft save failed.");
          return;
        }
        setInsight("success", "Drafted successfully.");
        // Update lot status pill, S icon, and Current Stage in pick table row
        var lotId = state.lotId;
        if (lotId) {
          var table = document.getElementById("order-listing");
          if (table) {
            table.querySelectorAll("tbody tr").forEach(function (row) {
              if (row.getAttribute("data-stock-lot-id") === lotId) {
                // Update Lot Status cell → show "Draft" pill
                var lotStatusCell = row.querySelector("[data-lot-status-cell]") ||
                  (function () {
                    var tds = row.querySelectorAll("td");
                    for (var i = 0; i < tds.length; i++) {
                      if (tds[i].textContent.trim().match(/Yet to Start|Draft|In Progress|On Hold/)) return tds[i];
                    }
                    return null;
                  })();
                if (lotStatusCell) {
                  lotStatusCell.innerHTML =
                    '<div class="d-inline-block px-3 fw-semibold text-center rounded-pill" ' +
                    'style="border:1px solid #4997ac;background-color:#d1f2f3;color:#03425d;' +
                    'font-size:12px;white-space:nowrap;padding:5px;">Draft</div>';
                }
                // Update S circle → half-filled green (screening in progress / draft)
                var processGroup = row.querySelector(".process-status-group");
                if (processGroup) {
                  processGroup.querySelectorAll(".rounded-circle").forEach(function (circle) {
                    if (circle.textContent.trim() === "S") {
                      circle.style.background = "linear-gradient(to right, #0c8249 50%, #bdbdbd 50%)";
                      circle.style.backgroundColor = "";
                    }
                  });
                }
                // Update Current Stage cell → show "Input Screening"
                var allCells = row.querySelectorAll("td");
                allCells.forEach(function (cell) {
                  var innerDiv = cell.querySelector("div.d-inline-block.rounded-pill");
                  if (innerDiv && innerDiv.textContent.trim().match(/Input Screening|Brass QC|IQF|Day Planning|Jig Loading/i)) {
                    innerDiv.innerHTML = "Input Screening";
                  }
                });
              }
            });
          }
        }
        // Show success toast and close the reject modal
        if (typeof Swal !== "undefined") {
          Swal.fire({
            icon: "success",
            title: "Drafted successfully",
            timer: 1800,
            timerProgressBar: true,
            showConfirmButton: false,
          }).then(function () { closeModal(); });
        } else {
          closeModal();
        }
      })
      .catch(function () {
        if (draftBtn) { draftBtn.disabled = false; draftBtn.textContent = "Save Draft"; }
        setInsight("error", "Network error – draft stored locally only.");
      });
  }

  function loadDraftIfAny() {
    if (!state.lotId) return;

    // Prefer backend draft (persists across sessions/browsers)
    var backendDraft = state._backendDraft;
    if (backendDraft) {
      _restoreFromDraftData(backendDraft, true /* isBackend */);
      return;
    }

    // Fallback: localStorage
    var raw = null;
    try { raw = window.localStorage.getItem(draftKey()); } catch (e) {}
    if (!raw) return;
    var data;
    try { data = JSON.parse(raw); } catch (e) { return; }
    if (!data || !data.reasons) return;
    _restoreFromDraftData({
      rejection_reasons_json: (function () {
        var m = {};
        (data.reasons || []).forEach(function (r) { m[r.reason_id] = { qty: r.qty }; });
        return m;
      })(),
      remarks: data.remarks || "",
      reject_assignments: data.rejectScans || [],
      accept_assignments: data.acceptScans || [],
      delinked_tray_ids: data.delinkScans || [],
    }, false /* isBackend */);
  }

  function _restoreFromDraftData(draft, isBackend) {
    // Restore qty inputs from rejection_reasons_json
    var reasonsJson = draft.rejection_reasons_json || {};
    var grid = $("isrm-reason-grid");
    if (grid && Object.keys(reasonsJson).length) {
      grid.querySelectorAll(".isrm-qty-input").forEach(function (inp) {
        var id = inp.getAttribute("data-reason-id");
        var entry = reasonsJson[id] || reasonsJson[String(id)];
        if (entry != null) {
          var qty = typeof entry === "object" ? entry.qty : entry;
          if (qty != null) inp.value = qty;
        }
      });
    }
    // Restore tray assignments
    if (isBackend) {
      state.rejectScans = (draft.reject_assignments || []).slice();
      state.acceptScans = (draft.accept_assignments || []).slice();
      state.delinkScans = (draft.delinked_tray_ids || []).map(function (id) {
        return typeof id === "string" ? { tray_id: id } : id;
      });
    } else {
      // localStorage format: already arrays of scan objects
      state.rejectScans = (draft.reject_assignments || []).slice();
      state.acceptScans = (draft.accept_assignments || []).slice();
      state.delinkScans = (draft.delinked_tray_ids || []).slice();
    }
    var rmEl = $("isrm-remarks");
    if (rmEl && draft.remarks) rmEl.value = draft.remarks;
    // Restore lot-rejection toggle
    if (draft.full_lot_reject) {
      var lotRejEl = $("isrm-lot-reject-toggle");
      if (lotRejEl && !lotRejEl.checked) {
        lotRejEl.checked = true;
        lotRejEl.dispatchEvent(new Event("change"));
      }
    }
    updateTotals();
    scheduleSlotPlan();
    setInsight("info", isBackend ? "Draft restored from server." : "Draft restored.");
  }

  function clearDraft() {
    try { window.localStorage.removeItem(draftKey()); } catch (e) {}
  }

  // ── Submit ────────────────────────────────────────────────────────────────
  function submit() {
    if (state.isSubmitting) return;

    // ── FULL LOT REJECT shortcut ────────────────────────────────────────
    // When the operator ticks "Lot Rejection" in the header, skip the
    // partial allocation flow entirely and POST to the dedicated full
    // reject endpoint. Remarks are mandatory in this mode.
    if (state.fullLotReject) {
      var rmEl = $("isrm-remarks");
      var rmText = ((rmEl && rmEl.value) || "").trim();
      if (!rmText) {
        setStatus("error", "Lot Rejection remarks are required.");
        setInsight("error", "Remarks required.");
        if (rmEl) rmEl.focus();
        return;
      }
      state.isSubmitting = true;
      var fbtn = $("isrm-submit-btn");
      if (fbtn) { fbtn.disabled = true; fbtn.textContent = "Submitting\u2026"; }
      setStatus("info", "Submitting full lot rejection\u2026");
      setInsight("busy", "Submitting\u2026");
      // mark S circle as scanning-WIP (half-green) ──────────────────
      if (typeof window.isMarkSCircleWip === "function") {
        window.isMarkSCircleWip(state.lotId);
      }
      fetch("/inputscreening/full_reject/", {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json", "X-CSRFToken": getCsrf() },
        body: JSON.stringify({ lot_id: state.lotId, remarks: rmText }),
      })
        .then(function (r) {
          return r.json().then(function (j) { return { ok: r.ok, data: j }; });
        })
        .then(function (resp) {
          state.isSubmitting = false;
          if (fbtn) fbtn.textContent = "Submit";
          if (!resp.ok || !resp.data.success) {
            setStatus("error", (resp.data && resp.data.error) || "Full reject failed.");
            setInsight("error", "Submit failed.");
            if (fbtn) fbtn.disabled = false;
            return;
          }
          clearDraft();
          setStatus("success", "Lot rejected.");
          setInsight("success", "Submitted.");
          if (typeof Swal !== "undefined") {
            Swal.fire({
              icon: "success",
              title: "Lot Rejected",
              text: "Rejected qty: " + resp.data.rejected_qty,
              timer: 1800,
              showConfirmButton: false,
            }).then(function () { closeModal(); location.reload(); });
          } else {
            setTimeout(function () { closeModal(); location.reload(); }, 800);
          }
        })
        .catch(function () {
          state.isSubmitting = false;
          if (fbtn) { fbtn.disabled = false; fbtn.textContent = "Submit"; }
          setStatus("error", "Network error during submit.");
          setInsight("error", "Network error.");
        });
      return;
    }

    // Derive rejection_entries from the PLANNED slot state (non-shortage) plus
    // shortage entries from the reason grid directly. Shortage entries have no
    // reject slots, so they are not present in rejectSlots but must be sent so
    // the backend can compute effective_lot_qty and accept allocation correctly.
    var planEntries = {};
    // 1. Shortage entries from the reason grid (no reject slots for shortage).
    collectRejectionEntries().forEach(function (e) {
      var rt = (e.reason_text || "").toUpperCase();
      if (rt.indexOf("SHORTAGE") !== -1) {
        planEntries[e.reason_id] = { reason_id: e.reason_id, reason_text: e.reason_text, qty: e.qty };
      }
    });
    // 2. Non-shortage entries from planned slots (ensures qty consistency with scans).
    state.rejectSlots.forEach(function (slot) {
      var rid = slot.reason_id;
      if (!planEntries[rid]) {
        planEntries[rid] = { reason_id: rid, reason_text: slot.reason_text || "", qty: 0 };
      }
      planEntries[rid].qty += slot.qty;
    });
    var entries = Object.keys(planEntries).map(function (k) { return planEntries[k]; });
    if (!entries.length) { setStatus("error", "Enter at least one rejection qty."); setInsight("error", "No rejection qty."); return; }
    if (!rejectStepDone()) { setStatus("error", "Please scan all reject trays."); setInsight("error", "Reject scans incomplete."); return; }
    if (!acceptStepDone()) { setStatus("error", "Please scan all accept trays."); setInsight("error", "Accept scans incomplete."); return; }

    var payload = {
      lot_id: state.lotId,
      rejection_entries: entries,
      reject_assignments: state.rejectSlots.map(function (slot, i) {
        return { tray_id: state.rejectScans[i].tray_id, reason_id: slot.reason_id };
      }),
      delink_tray_ids: state.delinkScans.map(function (d) { return d.tray_id; }),
      accept_assignments: state.acceptSlots.map(function (_, i) {
        return { tray_id: state.acceptScans[i].tray_id };
      }),
      remarks: ($("isrm-remarks") || {}).value || "",
    };

    state.isSubmitting = true;
    var btn = $("isrm-submit-btn");
    btn.disabled = true; btn.textContent = "Submitting\u2026";
    setStatus("info", "Submitting\u2026");
    setInsight("busy", "Submitting\u2026");
    // ── Bug3: mark S circle as scanning-WIP (half-green) ──────────────────
    if (typeof window.isMarkSCircleWip === "function") {
      window.isMarkSCircleWip(state.lotId);
    }
    fetch("/inputscreening/partial_submit_v2/", {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json", "X-CSRFToken": getCsrf() },
      body: JSON.stringify(payload),
    })
      .then(function (r) { return r.json().then(function (j) { return { ok: r.ok, data: j }; }); })
      .then(function (resp) {
        state.isSubmitting = false;
        btn.textContent = "Submit";
        if (!resp.ok || !resp.data.success) {
          setStatus("error", (resp.data && resp.data.error) || "Submission failed.");
          setInsight("error", "Submit failed.");
          updateSubmitState();
          return;
        }
        clearDraft();
        setStatus("success", "Submitted successfully.");
        setInsight("success", "Submitted.");
        if (typeof Swal !== "undefined") {
          Swal.fire({
            icon: "success",
            title: "Partial Reject Submitted",
            text: "Reject: " + resp.data.total_reject_qty + " · Accept: " + resp.data.total_accept_qty,
            timer: 2200,
            showConfirmButton: false,
          }).then(function () { closeModal(); location.reload(); });
        } else {
          setTimeout(function () { closeModal(); location.reload(); }, 800);
        }
      })
      .catch(function () {
        state.isSubmitting = false;
        btn.textContent = "Submit";
        setStatus("error", "Network error during submit.");
        setInsight("error", "Network error.");
        updateSubmitState();
      });
  }

  // ── Wire-up ────────────────────────────────────────────────
  document.addEventListener("DOMContentLoaded", function () {
    document.addEventListener("keydown", function (e) {
      var modal = $("isRejectModal");
      if (!modal || !modal.classList.contains("open") || e.key !== "Enter") return;
      if (e.target && e.target.closest && e.target.closest(".swal2-container, .isrm-scan-input")) return;
      var pendingInput = modal.querySelector('.isrm-scan-input[data-scan-pending="1"]');
      if (pendingInput) {
        e.preventDefault();
        e.stopPropagation();
        if (typeof e.stopImmediatePropagation === "function") e.stopImmediatePropagation();
        return;
      }
      var scanInput = modal.querySelector(".isrm-scan-input:not([readonly]):not([disabled])");
      if (!scanInput) return;
      e.preventDefault();
      e.stopPropagation();
      if (typeof e.stopImmediatePropagation === "function") e.stopImmediatePropagation();
      focusAndRevealInput(scanInput, false);
    }, true);

    document.addEventListener("click", function (e) {
      var btn = e.target.closest(".btn-reject-is");
      if (!btn) return;
      e.preventDefault();
      var lotId = btn.getAttribute("data-stock-lot-id");
      var batchId = btn.getAttribute("data-batch-id");
      if (!lotId) return;
      openModal(lotId, batchId);
    });

    var closeBtn = $("isrm-close-btn");
    if (closeBtn) closeBtn.addEventListener("click", closeModal);
    var cancelBtn = $("isrm-cancel-btn");
    if (cancelBtn) cancelBtn.addEventListener("click", closeModal);
    var submitBtn = $("isrm-submit-btn");
    if (submitBtn) submitBtn.addEventListener("click", submit);
    var clearBtn = $("isrm-clear-btn");
    if (clearBtn) clearBtn.addEventListener("click", clearAllInputs);
    var draftBtn = $("isrm-draft-btn");
    if (draftBtn) draftBtn.addEventListener("click", saveDraft);

    document.addEventListener("keydown", function (e) {
      var modal = $("isRejectModal");
      if (!modal || !modal.classList.contains("open")) return;
      if (document.querySelector(".swal2-container")) return;
      if (e.ctrlKey || e.metaKey || e.altKey) return;

      if (e.key === "Escape" || e.key === "c" || e.key === "C") {
        e.preventDefault();
        e.stopPropagation();
        if (typeof e.stopImmediatePropagation === "function") e.stopImmediatePropagation();
        closeModal();
        return;
      }

      if (e.key === "d" || e.key === "D") {
        e.preventDefault();
        e.stopPropagation();
        if (typeof e.stopImmediatePropagation === "function") e.stopImmediatePropagation();
        if (!state.isSubmitting) saveDraft();
        return;
      }

      if (e.key === "o" || e.key === "O") {
        e.preventDefault();
        e.stopPropagation();
        if (typeof e.stopImmediatePropagation === "function") e.stopImmediatePropagation();
        restoreUndoSnapshot();
        return;
      }

      if (e.key === "Enter") {
        var targetTag = (e.target && e.target.tagName || "").toUpperCase();
        var pendingInput = modal.querySelector('.isrm-scan-input[data-scan-pending="1"]');
        if (targetTag === "TEXTAREA" || pendingInput) return;
        var submitButton = $("isrm-submit-btn");
        if (submitButton && !submitButton.disabled && !state.isSubmitting) {
          e.preventDefault();
          e.stopPropagation();
          if (typeof e.stopImmediatePropagation === "function") e.stopImmediatePropagation();
          submit();
        }
      }
    }, true);

    // ── Clear All buttons for each section ────────────────────────────────
    var rejectClearAllBtn = $("isrm-reject-clear-all");
    if (rejectClearAllBtn) {
      rejectClearAllBtn.addEventListener("click", function () {
        captureUndoSnapshot();
        state.rejectScans = state.rejectScans.map(function () { return null; });
        renderRejectRows();
        renderActivePills();
        renderDelinkSection();
        updateSubmitState();
        setInsight("info", "Cleared all reject trays.");
      });
    }

    var acceptClearAllBtn = $("isrm-accept-clear-all");
    if (acceptClearAllBtn) {
      acceptClearAllBtn.addEventListener("click", function () {
        captureUndoSnapshot();
        state.acceptScans = state.acceptScans.map(function () { return null; });
        renderAcceptRows();
        renderActivePills();
        updateSubmitState();
        setInsight("info", "Cleared all accept trays.");
      });
    }

    var delinkClearAllBtn = $("isrm-delink-clear-all");
    if (delinkClearAllBtn) {
      delinkClearAllBtn.addEventListener("click", function () {
        captureUndoSnapshot();
        state.delinkScans = [];
        renderDelinkSection();
        renderActivePills();
        updateSubmitState();
        setInsight("info", "Cleared all delink trays.");
      });
    }

    // ── Lot Rejection toggle ─────────────────────────────────────────
    // When checked: switch the modal to "full lot reject" mode, focus the
    // remarks box and let the operator submit immediately. The submit()
    // function detects state.fullLotReject and routes to /full_reject/.
    var lotRejToggle = $("isrm-lot-reject-toggle");
    if (lotRejToggle) {
      lotRejToggle.addEventListener("change", function () {
        state.fullLotReject = !!this.checked;
        var body = document.querySelector("#isRejectModal .isrm-body");
        var submitBtnEl = $("isrm-submit-btn");
        var rmEl = $("isrm-remarks");
        var grid = $("isrm-reason-grid");
        
        if (this.checked) {
          if (body) body.classList.add("isrm-full-reject-mode");
          if (submitBtnEl) {
            submitBtnEl.disabled = false;
            submitBtnEl.textContent = "Submit Lot Rejection";
          }
          // ✅ HIDE & DISABLE all individual rejection quantity inputs
          if (grid) {
            grid.querySelectorAll(".isrm-qty-input").forEach(function (inp) {
              inp.style.display = "none";
              inp.disabled = true;
              inp.value = 0; // reset to 0 since the entire lot is being rejected
            });
          }
          setInsight(
            "warning",
            "Lot Rejection mode — enter remarks and click Submit."
          );
          setStatus(
            "info",
            "Lot Rejection: the entire lot will be rejected. Remarks are required."
          );
          if (rmEl) {
            rmEl.focus();
            try { rmEl.scrollIntoView({ behavior: "smooth", block: "center" }); } catch (_) {}
          }
        } else {
          if (body) body.classList.remove("isrm-full-reject-mode");
          if (submitBtnEl) submitBtnEl.textContent = "Submit";
          // ✅ SHOW & ENABLE all individual rejection quantity inputs
          if (grid) {
            grid.querySelectorAll(".isrm-qty-input").forEach(function (inp) {
              inp.style.display = "";
              inp.disabled = false;
            });
          }
          setInsight("info", "Lot Rejection mode disabled.");
          setStatus("info", "Resume scanning to submit a partial reject.");
          updateSubmitState();
        }
      });
    }

    // NOTE: Intentionally removed backdrop click-to-close to prevent
    // accidental dismissal while scanning (Err1 fix).
    document.addEventListener("keydown", function (e) {
      if (e.key === "Escape") {
        var m = $("isRejectModal");
        if (m && m.classList.contains("open")) closeModal();
      }
    });

    // ── ERR1 FIX: Close IS reject modal on bfcache restore (browser back/forward) ──
    window.addEventListener("pageshow", function (e) {
      if (!e.persisted) return;
      var m = $("isRejectModal");
      if (m && m.classList.contains("open")) closeModal();
    });
  });

  window.__isrm = state;
})();

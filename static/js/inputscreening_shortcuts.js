/**
 * Input Screening – Keyboard Shortcuts
 *
 * Shortcuts only fire on the IS Pick Table page.
 * Guards:
 *   - Ignored when focus is inside input / textarea / select / contenteditable
 *   - F2 is always intercepted (prevents default browser action)
 *
 * Key map:
 *   F2     → Open shared tray scan mini popup only
 *   A      → focus/select row for Accept action (press Enter to confirm)
 *   R      → focus/select row for Reject action (press Enter to open reject modal)
 *   V      → View / open tray verification modal for selected row (same as clicking eye icon)
 *   D      → Save as Draft (when tray verification modal is open)
 *   Esc    → close top-most open popup (priority order below)
 *   ↑ / ↓  → move row selection up / down in the pick table
 *   Enter  → open tray-verification modal for selected row OR execute action if already focused
 *   ← / →  → horizontal scroll in tables (Pick/Accept/Reject/Completed)
 */
(function () {
  "use strict";

  // ─── State ─────────────────────────────────────────────────────────────────
  var _selectedRow = null; // currently keyboard-selected <tr>

  // ─── Helpers ───────────────────────────────────────────────────────────────

  /** Returns true when the user is actively typing in a form field. */
  function _isTyping() {
    var el = document.activeElement;
    if (!el) return false;
    var tag = el.tagName.toLowerCase();
    return (
      tag === "input" ||
      tag === "textarea" ||
      tag === "select" ||
      el.isContentEditable
    );
  }

  /**
   * Returns true when any popup/modal on this page is currently open.
   * Table row shortcuts (A/R/D/V/Enter/Arrows) must not leak through to the
   * underlying pick table while a modal is covering it.
   */
  function _isModalOpen() {
    var tvm = document.getElementById("trayVerificationModal");
    if (tvm && tvm.style.display !== "none") return true;
    var isrm = document.getElementById("isRejectModal");
    if (isrm && isrm.classList.contains("open")) return true;
    var tsModal = document.getElementById("trayScanModal");
    if (tsModal && tsModal.classList.contains("open")) return true;
    var dpModal = document.getElementById("trayScanModal_DayPlanning");
    if (dpModal) {
      var dpComputed = window.getComputedStyle(dpModal);
      if (dpComputed.display !== "none" && dpComputed.visibility !== "hidden") return true;
    }
    var acceptPop = document.getElementById("newPopupModal");
    if (acceptPop && acceptPop.classList.contains("open")) return true;
    var holdModal = document.getElementById("holdRemarkModal");
    if (holdModal && holdModal.style.display === "flex") return true;
    if (document.querySelector(".swal2-container")) return true;
    return false;
  }

  /** Lightweight toast – falls back to nothing if Swal is absent. */
  function _toast(msg, icon) {
    if (window.Swal) {
      Swal.fire({
        toast: true,
        position: "top-end",
        icon: icon || "warning",
        title: msg,
        showConfirmButton: false,
        timer: 2200,
        timerProgressBar: true,
      });
    }
  }

  // ─── Row selection ─────────────────────────────────────────────────────────

  var ROW_SELECTED_CLASS = "is-kbd-selected";

  /** Inject a small CSS rule for the selection highlight once. */
  function _injectSelectionStyle() {
    if (document.getElementById("is-kbd-selection-style")) return;
    var style = document.createElement("style");
    style.id = "is-kbd-selection-style";
    // Apply light yellow background highlight to the selected row
    // Works on both regular and sticky (frozen) columns
    style.textContent =
      "tr." + ROW_SELECTED_CLASS +
      " { background: rgba(255, 250, 205, 0.4) !important; }" +
      "tr." + ROW_SELECTED_CLASS + " td:nth-child(1)," +
      "tr." + ROW_SELECTED_CLASS + " td:nth-child(2)," +
      "tr." + ROW_SELECTED_CLASS + " td:nth-child(3)" +
      " { background: rgba(255, 250, 205, 0.4) !important;" +
      " z-index: 35 !important; }";
    document.head.appendChild(style);
  }

  /** Return all visible, non-hold, non-header table rows. */
  function _getRows() {
    var tbody = document.querySelector("#order-listing tbody");
    if (!tbody) return [];
    return Array.from(tbody.querySelectorAll("tr[data-stock-lot-id], tr[data-lot-id]")).filter(
      function (r) {
        return !r.classList.contains("row-inactive");
      }
    );
  }

  /** Set the visually-selected row. Pass null to deselect all. */
  function _selectRow(row) {
    _getRows().forEach(function (r) {
      r.classList.remove(ROW_SELECTED_CLASS);
    });
    if (row) {
      row.classList.add(ROW_SELECTED_CLASS);
      row.scrollIntoView({ block: "nearest", behavior: "smooth" });
    }
    _selectedRow = row;
  }

  /** Move selection by +1 (down) or -1 (up). */
  function _moveSelection(delta) {
    var rows = _getRows();
    if (!rows.length) return;
    var idx = _selectedRow ? rows.indexOf(_selectedRow) : -1;
    var next = idx + delta;
    if (next < 0) next = 0;
    if (next >= rows.length) next = rows.length - 1;
    _selectRow(rows[next]);
  }

  // ─── Popup priority stack ──────────────────────────────────────────────────

  /**
   * Close the top-most visible popup and return true if something was closed.
   * Priority (highest first):
   *   1. Tray Verification Modal (trayVerificationModal)
   *   2. IS Reject Modal (isRejectModal)
   *   3. Rejection Window left panel (trayScanModal)
   *   4. Day Planning view modal (trayScanModal_DayPlanning)
   *   5. Accept popup (newPopupModal)
   *   6. Hold remark modal (holdRemarkModal)
   *   7. Row highlight / selection
   */
  function _closeTopPopup() {
    // 1. Tray Verification Modal
    var tvm = document.getElementById("trayVerificationModal");
    if (tvm && tvm.style.display !== "none") {
      if (typeof window.tvmClose === "function") {
        window.tvmClose();
      } else {
        tvm.style.display = "none";
      }
      return true;
    }

    // 2. IS Reject Modal
    var isrm = document.getElementById("isRejectModal");
    if (isrm && isrm.classList.contains("open")) {
      var cancelBtn = document.getElementById("isrm-cancel-btn");
      if (cancelBtn) cancelBtn.click();
      else isrm.classList.remove("open");
      return true;
    }

    // 3. Rejection Window (left panel)
    var tsModal = document.getElementById("trayScanModal");
    if (tsModal && tsModal.classList.contains("open")) {
      var closeTs = document.getElementById("closeTrayScanModal");
      if (closeTs) closeTs.click();
      else tsModal.classList.remove("open");
      return true;
    }

    // 4. Day Planning view modal
    var dpModal = document.getElementById("trayScanModal_DayPlanning");
    if (dpModal) {
      var dpComputed = window.getComputedStyle(dpModal);
      if (dpComputed.display !== "none" && dpComputed.visibility !== "hidden") {
        var closeDP = document.getElementById("closeTrayScanModal_DayPlanning");
        if (closeDP) closeDP.click();
        return true;
      }
    }

    // 5. Accept popup
    var acceptPop = document.getElementById("newPopupModal");
    if (acceptPop && acceptPop.classList.contains("open")) {
      var closeAcc = document.getElementById("closeNewPopupModal");
      if (closeAcc) closeAcc.click();
      else acceptPop.classList.remove("open");
      return true;
    }

    // 6. Hold remark modal
    var holdModal = document.getElementById("holdRemarkModal");
    if (holdModal && holdModal.style.display === "flex") {
      holdModal.style.display = "none";
      return true;
    }

    // 7. Row highlight / selection
    if (_selectedRow) {
      _selectRow(null);
      if (typeof window.restoreRowPosition === "function") {
        window.restoreRowPosition();
      }
      return true;
    }

    return false;
  }

  // ─── Action helpers ────────────────────────────────────────────────────────

  /**
   * Trigger accept button directly - the picktable.js handler shows the confirmation.
   * No need for duplicate Swal here.
   */
  function _confirmAndAccept(acceptBtn) {
    acceptBtn.click();
  }

  /** Open the tray verification scan for the selected row (or first row). */
  function _openScanMode() {
    // If a row is selected, open TVM for that lot
    var row = _selectedRow || _getRows()[0];
    if (!row) {
      _toast("No lot available to scan.", "info");
      return;
    }
    var viewBtn = row.querySelector(".tray-scan-btn-DayPlanning-view");
    if (viewBtn) {
      viewBtn.click();
    } else {
      // Fallback: click the page-level scan button
      var pageBtn = document.getElementById("scanButton");
      if (pageBtn) pageBtn.click();
    }
  }

  /** Open the shared global scan popup without choosing any row. */
  function _openGlobalScanPopup() {
    _hidePleaseScan();
    if (window.GlobalPickTableScanner && typeof window.GlobalPickTableScanner.openScanModal === "function") {
      window.GlobalPickTableScanner.openScanModal();
      return true;
    }
    if (typeof window._gScanActivate === "function") {
      window._gScanActivate();
      return true;
    }
    var globalBtn = document.getElementById("globalScanBtn");
    if (globalBtn) {
      globalBtn.click();
      return true;
    }
    _showPleaseScan();
    return false;
  }

  /** Focus/select row for Accept action. Pressing Enter then opens confirmation. */
  function _focusAcceptRow() {
    var row = _selectedRow || _getRows()[0];
    if (!row) {
      _toast("No lot available.", "info");
      return;
    }
    _selectRow(row);  // Highlight the row
    var acceptBtn = row.querySelector(".btn-accept-is");
    if (!acceptBtn) {
      _toast("Accept button not found for this row.", "error");
      return;
    }
    if (acceptBtn.disabled) {
      _toast(
        "Accept is not available — all trays must be verified first.",
        "warning"
      );
      return;
    }
    // Mark row as accept-focused and immediately open confirmation
    row.classList.add("accept-focused");
    _openAcceptConfirm();
  }

  /** Trigger the Accept confirmation dialog. */
  function _openAcceptConfirm() {
    var row = _selectedRow;
    if (!row) {
      _toast("No row selected.", "info");
      return;
    }
    var acceptBtn = row.querySelector(".btn-accept-is");
    if (!acceptBtn) {
      _toast("Accept button not found for this row.", "error");
      return;
    }
    if (acceptBtn.disabled) {
      _toast(
        "Accept is not available — all trays must be verified first.",
        "warning"
      );
      return;
    }
    // Show confirm dialog with Cancel as default (safe) — arrow keys switch buttons
    _confirmAndAccept(acceptBtn);
  }

  /** Focus/select row for Reject action. Pressing Enter then opens reject modal. */
  function _focusRejectRow() {
    var row = _selectedRow || _getRows()[0];
    if (!row) {
      _toast("No lot available.", "info");
      return;
    }
    _selectRow(row);  // Highlight the row
    var rejectBtn = row.querySelector(".btn-reject-is");
    if (!rejectBtn) {
      _toast("Reject button not found for this row.", "error");
      return;
    }
    if (rejectBtn.disabled) {
      _toast(
        "Reject is not available — all trays must be verified first.",
        "warning"
      );
      return;
    }
    _toast("Press Enter to Reject this lot, or ↑↓ to select another row.", "info");
  }

  /** Trigger the Reject button on the selected / highlighted row. */
  function _openRejectWindow() {
    var row = _selectedRow;
    if (!row) {
      _toast("No row selected.", "info");
      return;
    }
    var rejectBtn = row.querySelector(".btn-reject-is");
    if (!rejectBtn) {
      _toast("Reject button not found for this row.", "error");
      return;
    }
    if (rejectBtn.disabled) {
      _toast(
        "Reject is not available — all trays must be verified first.",
        "warning"
      );
      return;
    }
    rejectBtn.click();
  }

  /** Open tray verification modal OR execute focused action (Enter key). */
  function _openSelectedRowDetail() {
    var row = _selectedRow;
    if (!row) return;
    
    // Check if Accept or Reject button was previously focused by A or R shortcut
    var acceptBtn = row.querySelector(".btn-accept-is");
    var rejectBtn = row.querySelector(".btn-reject-is");
    
    // If A was pressed and Accept is available, open confirmation
    if (acceptBtn && !acceptBtn.disabled && row.classList.contains("accept-focused")) {
      row.classList.remove("accept-focused");
      _openAcceptConfirm();
      return;
    }
    
    // If R was pressed and Reject is available, open reject modal
    if (rejectBtn && !rejectBtn.disabled && row.classList.contains("reject-focused")) {
      row.classList.remove("reject-focused");
      _openRejectWindow();
      return;
    }
    
    // Otherwise open tray verification modal (view icon)
    var viewBtn = row.querySelector(".tray-scan-btn-DayPlanning-view, .tray-scan-btn-BQ-view, .tray-scan-btn-Jig");
    if (viewBtn) viewBtn.click();
  }

  // ─── Row click → sync selection ───────────────────────────────────────────

  /** Keep _selectedRow in sync when user clicks a table row directly. */
  function _initRowClickSync() {
    document.addEventListener("click", function (e) {
      var tr = e.target.closest("#order-listing tbody tr[data-stock-lot-id], #order-listing tbody tr[data-lot-id]");
      if (tr) _selectRow(tr);
    });
  }

  // ─── Scan-status indicator helpers ────────────────────────────────────────

  var _pleaseScanTimer = null;

  /** Show the inline "PLEASE SCAN" hint next to the Scan button. */
  function _showPleaseScan() {
    var span = document.getElementById("scanStatusMessage");
    if (!span) return;
    span.style.display = "inline-block";
    if (_pleaseScanTimer) clearTimeout(_pleaseScanTimer);
    _pleaseScanTimer = setTimeout(function () {
      span.style.display = "none";
    }, 4000);
  }

  /** Hide the inline scan hint (called once a real scan modal opens). */
  function _hidePleaseScan() {
    var span = document.getElementById("scanStatusMessage");
    if (span) span.style.display = "none";
    if (_pleaseScanTimer) {
      clearTimeout(_pleaseScanTimer);
      _pleaseScanTimer = null;
    }
  }

  // ─── Global keydown handler ────────────────────────────────────────────────

  function _onKeydown(e) {
    // Editable controls own every keystroke. This guard is intentionally before
    // F2/Escape so no application shortcut fires while typing or scanning.
    if (_isTyping()) return;

    // F2 — intercept only when focus is not in an editable control
    if (e.key === "F2") {
      e.preventDefault();
      _openGlobalScanPopup();
      return;
    }

    // Escape — intercept only when focus is not in an editable control
    if (e.key === "Escape") {
      e.preventDefault();
      _closeTopPopup();
      return;
    }

    // ── ERR1 GUARD: when the IS Reject Modal is open, suppress every other
    // page-level shortcut (Enter, A, R, ArrowUp/Down).  During a tray scan
    // the input may briefly lose focus between auto-validate cycles or the
    // hardware scanner may emit a trailing Enter — letting those keys reach
    // the row handler would silently open the eye-icon view modal
    // (trayScanModal_DayPlanning) behind the reject modal.
    var _isrm = document.getElementById("isRejectModal");
    if (_isrm && _isrm.classList.contains("open")) return;

    // All other shortcuts: skip when any modal/popup is open. Editable focus
    // was already handled by the common guard at the top of this function.
    if (_isModalOpen()) return;

    switch (e.key) {
      case "a":
      case "A":
        e.preventDefault();
        _focusAcceptRow();  // Only focus the row, don't open confirmation yet
        if (_selectedRow) _selectedRow.classList.add("accept-focused");
        break;

      case "r":
      case "R":
        e.preventDefault();
        _focusRejectRow();  // Only focus the row, don't open modal yet
        if (_selectedRow) _selectedRow.classList.add("reject-focused");
        break;

      case "d":
      case "D":
        // Save as Draft shortcut (when tray verification modal is open)
        e.preventDefault();
        var tvmModal = document.getElementById("trayVerificationModal");
        if (tvmModal && tvmModal.style.display !== "none") {
          var draftBtn = document.getElementById("tvm-save-draft-btn");
          if (draftBtn && !draftBtn.disabled) draftBtn.click();
        }
        break;

      case "v":
      case "V":
        // View / Open tray verification modal for selected row
        e.preventDefault();
        _openScanMode();
        break;

      case "ArrowUp":
        e.preventDefault();
        _moveSelection(-1);
        // Clear action focus classes when moving up/down
        if (_selectedRow) {
          _selectedRow.classList.remove("accept-focused", "reject-focused");
        }
        break;

      case "ArrowDown":
        e.preventDefault();
        _moveSelection(1);
        // Clear action focus classes when moving up/down
        if (_selectedRow) {
          _selectedRow.classList.remove("accept-focused", "reject-focused");
        }
        break;

      case "ArrowLeft":
        // Horizontal scroll left in tables
        e.preventDefault();
        _scrollTableHorizontal(-100);
        break;

      case "ArrowRight":
        // Horizontal scroll right in tables
        e.preventDefault();
        _scrollTableHorizontal(100);
        break;

      case "Enter":
        e.preventDefault();
        _openSelectedRowDetail();  // Execute focused action or open view modal
        break;
    }
  }

  /** Scroll table horizontally (for Pick/Accept/Reject/Completed tables). */
  function _scrollTableHorizontal(delta) {
    // Target the actual scrollable container - .table-responsive div
    var container = document.querySelector(".table-responsive");
    if (!container) {
      // Fallback: try finding the card-body
      container = document.querySelector(".card-body");
    }
    if (container) {
      container.scrollLeft += delta;
    } else {
      console.warn("Horizontal scroll: scrollable container not found");
    }
  }

  // ─── Boot ──────────────────────────────────────────────────────────────────

  document.addEventListener("DOMContentLoaded", function () {
    _injectSelectionStyle();
    _initRowClickSync();
    if (!window.TTT_DB_SHORTCUTS_ENABLED) {
      document.addEventListener("keydown", _onKeydown);
    }

    // Page-level Scan button opens the shared global scan popup.
    var scanBtn = document.getElementById("scanButton");
    if (scanBtn) {
      scanBtn.addEventListener("click", function (event) {
        event.preventDefault();
        _openGlobalScanPopup();
      });
    }

    // Expose tvmClose globally (may be needed by Esc handler before tvmClose
    // is set by inputscreening_picktable.js).  The picktable script overwrites
    // this with the real implementation later; this is only a safe fallback.
    if (!window.tvmClose) {
      window.tvmClose = function () {
        var m = document.getElementById("trayVerificationModal");
        if (m) m.style.display = "none";
      };
    }
  });
})();

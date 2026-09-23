// ====== Original inline block #1 ======
document.addEventListener("DOMContentLoaded", function () {
  // ✅ HELPER FUNCTION: Get edited tray qty from modal
  function getEditedTrayQtyFromModal() {
    const modal = document.getElementById("trayScanModal_DayPlanning");
    if (!modal) {
      return null;
    }
    // Find the top tray input
    const editedTrayQtyInput = modal.querySelector(
      '.tray-qty-input[data-top-tray="1"]',
    );
    if (!editedTrayQtyInput) {
      return null;
    }
    const currentValue = editedTrayQtyInput.value.trim();
    const initialValue = editedTrayQtyInput.getAttribute("data-initial") || "";
    // Return current value regardless of whether it changed
    return currentValue || null;
  }
  // ✅ HELPER FUNCTION: Get CSRF token
  function getCookie(name) {
    let cookieValue = null;
    if (document.cookie && document.cookie !== "") {
      const cookies = document.cookie.split(";");
      for (let i = 0; i < cookies.length; i++) {
        const cookie = cookies[i].trim();
        if (cookie.substring(0, name.length + 1) === name + "=") {
          cookieValue = decodeURIComponent(cookie.substring(name.length + 1));
          break;
        }
      }
    }
    return cookieValue;
  }
  // ✅ SAVE IP CHECKBOX HANDLER
  document.addEventListener("click", function (e) {
    if (e.target.classList.contains("save-ip-checkbox")) {
      const element = e.target;
      const row = element.closest("tr");
      let lotId = element.getAttribute("data-lot-id");
      if (!lotId) lotId = row.getAttribute("data-stock-lot-id");
      if (!lotId) {
        Swal.fire("Error", "Lot ID not found", "error");
        return;
      }
      // Get the calculated missing qty from the input
      const missingQtyInput = row.querySelector(".missing-qty-input");
      const missingQty = missingQtyInput ? missingQtyInput.value.trim() : "0";
      // ✅ FIXED: Get edited tray qty using helper function
      const editedTrayQty = getEditedTrayQtyFromModal();
      // Change icon to indicate processing
      if (element.tagName === "I") {
        element.className = "fa fa-spinner fa-spin";
      }
      // Disable element during processing
      element.style.pointerEvents = "none";
      fetch("/inputscreening/save_ip_checkbox/", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-CSRFToken": getCookie("csrftoken"),
        },
        body: JSON.stringify({
          lot_id: lotId,
          missing_qty: missingQty || "0",
          edited_tray_qty: editedTrayQty,
        }),
      })
        .then((res) => res.json())
        .then((data) => {
          if (data.success) {
            // Change to checked state
            if (element.tagName === "I") {
              element.className = "fa fa-check-square";
              element.style.color = "#28a745";
            }
            element.title = "IP Checkbox saved";
            Swal.fire({
              icon: "success",
              title: "Saved successfully!",
              timer: 2000,
              showConfirmButton: false,
            });
            // Update row to show verification completed
            updateRowAfterIPSave(row, lotId, missingQty);
            // Reload page after a short delay
            setTimeout(() => {
              window.location.reload();
            }, 1500);
          } else {
            Swal.fire("Error", data.error || "Save failed", "error");
            // Reset icon and enable element
            if (element.tagName === "I") {
              element.className = "fa fa-square-o";
            }
            element.style.pointerEvents = "auto";
          }
        })
        .catch((error) => {
          Swal.fire("Error", "Network error", "error");
          // Reset icon and enable element
          if (element.tagName === "I") {
            element.className = "fa fa-square-o";
          }
          element.style.pointerEvents = "auto";
        });
    }
  });
  // ✅ HELPER FUNCTION: Update row after IP save
  function updateRowAfterIPSave(row, lotId, missingQty) {
    // Update main IP checkbox
    const mainIpCheckbox = row.querySelector(".ip-checkbox");
    if (mainIpCheckbox) {
      mainIpCheckbox.checked = true;
      mainIpCheckbox.disabled = true;
    }
    // Update missing qty input
    const missingQtyInput = row.querySelector(".missing-qty-input");
    if (missingQtyInput) {
      missingQtyInput.disabled = true;
    }
    // Update physical qty
    const physicalQtyInput = row.querySelector(".physical-qty-input");
    const lotQtySpan = row.querySelector(".lot-qty");
    if (physicalQtyInput && lotQtySpan && missingQty) {
      const totalQty = parseInt(lotQtySpan.textContent.trim(), 10);
      const newPhysicalQty = totalQty - parseInt(missingQty);
      physicalQtyInput.value = newPhysicalQty;
    }
    // Update process status Q icon
    const processIcons = row.querySelectorAll(".d-flex > div");
    if (processIcons.length > 0) {
      processIcons[0].style.backgroundColor = "#0c8249";
    }
  }
  // ✅ Make helper functions available globally
  window.getEditedTrayQtyFromModal = getEditedTrayQtyFromModal;
  window.getCookie = getCookie;
});
// ====== Original inline block #2 ======
document.addEventListener("DOMContentLoaded", function () {
  // Use event delegation to handle dynamically created buttons
  document.addEventListener("click", function (e) {
    // ✅ Check if the clicked element is a Delete button
    const deleteBtn =
      e.target.closest(".delete-batch-btn") ||
      (e.target.tagName === "IMG" &&
        e.target.alt === "Delete" &&
        e.target.closest('a[class*="delete-batch-btn"]'));
    if (deleteBtn) {
      e.preventDefault();
      const row = deleteBtn.closest("tr");
      if (!row) return;
      const batchId = deleteBtn.getAttribute("data-batch-id");
      const stockLotId = deleteBtn.getAttribute("data-stock-lot-id");
      if (!batchId || !stockLotId) {
        Swal.fire("Error", "Batch ID or Stock Lot ID not found!", "error");
        return;
      }
      Swal.fire({
        title: "Are you sure?",
        text: "Do you really want to delete this batch?",
        icon: "warning",
        showCancelButton: true,
        confirmButtonColor: "#d33",
        cancelButtonColor: "#3085d6",
        confirmButtonText: "Yes, delete it!",
        cancelButtonText: "Cancel",
      }).then((result) => {
        if (result.isConfirmed) {
          fetch("/inputscreening/ip_delete_batch/", {
            method: "POST",
            headers: {
              "Content-Type": "application/json",
              "X-CSRFToken": getCookie("csrftoken"),
            },
            body: JSON.stringify({
              batch_id: batchId,
              stock_lot_id: stockLotId,
            }),
          })
            .then((res) => res.json())
            .then((data) => {
              if (data.success) {
                row.remove();
                Swal.fire({
                  icon: "success",
                  title: "Deleted!",
                  text: "Batch has been deleted successfully.",
                  timer: 2000,
                  showConfirmButton: false,
                });
              } else {
                Swal.fire(
                  "Error",
                  data.message || "Failed to delete batch",
                  "error",
                );
              }
            })
            .catch((error) => {
              console.error("Delete error:", error);
              Swal.fire(
                "Error",
                "An error occurred while deleting the batch",
                "error",
              );
            });
        }
      });
      return;
    }
  });
  // Helper function for CSRF token (if not already defined)
  function getCookie(name) {
    let cookieValue = null;
    if (document.cookie && document.cookie !== "") {
      const cookies = document.cookie.split(";");
      for (let i = 0; i < cookies.length; i++) {
        const cookie = cookies[i].trim();
        if (cookie.substring(0, name.length + 1) === name + "=") {
          cookieValue = decodeURIComponent(cookie.substring(name.length + 1));
          break;
        }
      }
    }
    return cookieValue;
  }
});
// ====== Original inline block #3 ======
document.addEventListener("DOMContentLoaded", function () {
  const cancelBtn = document.getElementById("trayScanCancelBtn");
  if (cancelBtn) {
    cancelBtn.addEventListener("click", function () {
      const modal = document.getElementById("trayScanModal_BQ");
      if (modal) modal.classList.remove("open");
    });
  }
  // DELETE BUTTON HANDLER - REMOVED (Now handled via event delegation)
  // Helper function to show error notification
  function showErrorNotification(message) {
    // Create or get existing notification element
    let notification = document.getElementById("error-notification");
    if (!notification) {
      notification = document.createElement("div");
      notification.id = "error-notification";
      notification.style.cssText = `
         position: fixed;
         left: 50%;
         top: 50%;
         transform: translate(-50%, -50%);
         background: #dc3545;
         color: white;
         padding: 18px 32px;
         border-radius: 12px;
         box-shadow: 0 4px 24px rgba(220,53,69,0.18);
         z-index: 10001;
         font-weight: 600;
         font-size: 18px;
         text-align: center;
         transition: opacity 0.3s ease;
         opacity: 1;
       `;
      document.body.appendChild(notification);
    }
    notification.textContent = message;
    // Show notification
    notification.style.opacity = "1";
    // Hide notification after 4 seconds
    setTimeout(() => {
      notification.style.opacity = "0";
    }, 4000);
  }
  const table = document.getElementById("order-listing");
  if (!table) {
    return;
  }
  const headers = table.querySelectorAll("thead th");
  const tbody = table.querySelector("tbody");
  let sortDirection = {};
  headers.forEach((header, index) => {
    header.style.cursor = "pointer";
    header.addEventListener("click", function () {
      const rows = Array.from(tbody.querySelectorAll("tr"));
      const dir = sortDirection[index] === "asc" ? "desc" : "asc";
      sortDirection[index] = dir;
      rows.sort((a, b) => {
        const cellA = a.children[index].textContent.trim();
        const cellB = b.children[index].textContent.trim();
        const valA = isNaN(cellA) ? cellA : parseFloat(cellA);
        const valB = isNaN(cellB) ? cellB : parseFloat(cellB);
        if (valA < valB) return dir === "asc" ? -1 : 1;
        if (valA > valB) return dir === "asc" ? 1 : -1;
        return 0;
      });
      tbody.innerHTML = "";
      rows.forEach((row) => tbody.appendChild(row));
    });
  });
});
// ====== Original inline block #4 ======
// Row highlight & position swap for tray-scan-btn-Jig, tray-scan-btn (Set Top Tray/Reject), and btn-twitter (Accept)
document.addEventListener("DOMContentLoaded", function () {
  // Add highlight style if not present
  if (!document.getElementById("dp-row-action-highlight-style")) {
    var style = document.createElement("style");
    style.id = "dp-row-action-highlight-style";
    style.innerHTML = `
         .dp-row-action-highlight > td,
         tr.action-row-highlight > td {
           background-color: #fff5bd !important;
           transition: background-color 0.2s ease;
         }
       `;
    document.head.appendChild(style);
  }
  let activeRowState = null;
  // Function to handle row highlighting and movement
  function handleRowHighlight(event) {
    // Move the clicked row to the top and highlight
    var row = event.target.closest("tr");
    console.log("🐛 [DEBUG] handleRowHighlight fired. event.target:", event.target.tagName, event.target.className, "| row found:", !!row);
    if (row && row.parentNode) {
      restoreRowPosition();
      const parent = row.parentNode;
      const nextSibling = row.nextSibling;
      activeRowState = { row: row, parent: parent, nextSibling: nextSibling };
      row.classList.add("dp-row-action-highlight", "action-row-highlight");
      console.log("🐛 [DEBUG] before insertBefore. parent.tagName:", parent.tagName, "| row is currently child #", Array.from(parent.children).indexOf(row));
      parent.insertBefore(row, parent.firstElementChild);
      console.log("🐛 [DEBUG] after insertBefore. row is now child #", Array.from(parent.children).indexOf(row), "| row classList:", row.className);
      setTimeout(function () {
        console.log("🐛 [DEBUG] 500ms later, row is child #", Array.from(parent.children).indexOf(row), "| row classList:", row.className, "| still in DOM:", document.body.contains(row));
      }, 500);
    } else {
      console.log("🐛 [DEBUG] handleRowHighlight aborted — no row or no parentNode found");
    }
  }
  // Function to restore row position and remove highlight
  function restoreRowPosition() {
    if (activeRowState) {
      const row = activeRowState.row;
      const parent = activeRowState.parent;
      const nextSibling = activeRowState.nextSibling;
      row.classList.remove("dp-row-action-highlight", "action-row-highlight");
      if (nextSibling && nextSibling.parentNode === parent) {
        parent.insertBefore(row, nextSibling);
      } else if (parent) {
        parent.appendChild(row);
      }
      activeRowState = null;
    }
    // Clear ALL possible highlight classes from all rows
    document.querySelectorAll("tbody tr").forEach(function (row) {
      row.classList.remove(
        "dp-row-action-highlight",
        "action-row-highlight",
        "gkb-row-focus",
        "is-kbd-selected",
        "gs-active-scan",
        "gs-hi"
      );
    });
    // Clear session storage to prevent highlight persistence on page refresh
    if (window.GlobalShortcutManager && typeof window.GlobalShortcutManager.clear === "function") {
      window.GlobalShortcutManager.clear();
    } else if (typeof window._gkbClearPending === "function") {
      window._gkbClearPending();
    }
  }
  document.addEventListener("click", function (event) {
    var trigger = event.target.closest(
      ".tray-scan-btn-Jig, .tray-scan-btn-DayPlanning-view, .btn-accept-is, .btn-reject-is, .ip-remark-send-btn"
    );
    if (!trigger || !document.getElementById("order-listing") || !document.getElementById("order-listing").contains(trigger)) return;
    handleRowHighlight.call(trigger, event);
  });
  // Expose handleRowHighlight globally so other scripts (e.g. hold/unhold toggle) can call it
  window.handleRowHighlight = handleRowHighlight;
  document.addEventListener("globalScan:pageUpdated", restoreRowPosition);
  document.addEventListener("globalScan:closed", function (event) {
    if (event && event.detail && event.detail.reason === "success") return;
    restoreRowPosition();
  });
  // Expose restoreRowPosition globally so other scripts (e.g. tvmClose) can call it
  window.restoreRowPosition = restoreRowPosition;
  // On modal close events, restore row to original position and remove highlight
  // For View modal (trayScanModal_DayPlanning)
  var closeViewBtn = document.getElementById("closeTrayScanModal_DayPlanning");
  if (closeViewBtn) {
    closeViewBtn.addEventListener("click", restoreRowPosition);
  }
  // On Reject modal close (isRejectModal)
  var isRejectModal = document.getElementById("isRejectModal");
  if (isRejectModal) {
    var observer = new MutationObserver(function(mutations) {
      mutations.forEach(function(mutation) {
        if (mutation.attributeName === 'class') {
          if (!isRejectModal.classList.contains('open')) {
            restoreRowPosition();
          }
        }
      });
    });
    observer.observe(isRejectModal, { attributes: true });
  }
});
// ====== Original inline block #5 ======
// ========== HOLD/UNHOLD TOGGLE FUNCTIONALITY ==========
document.addEventListener("DOMContentLoaded", function () {
  console.log("🔄 Initializing Hold/Unhold toggle functionality");
  // Global state to track hold/unhold operations
  window.holdToggleState = {
    currentHoldCell: null,
    intendedState: null,
    currentBatchId: null,
    currentLotId: null,
    rowIdentifier: null,
  };
  // Attach batch_id to each row for easy access
  document.querySelectorAll("tbody tr").forEach(function (row) {
    const trayScanBtn = row.querySelector(".tray-scan-btn, .tray-scan-btn-Jig");
    if (trayScanBtn) {
      row.setAttribute(
        "data-batch-id",
        trayScanBtn.getAttribute("data-batch-id"),
      );
    }
  });
  // Function to handle hold toggle button clicks
  function attachHoldToggleListeners() {
    console.log("🔗 Attaching hold toggle listeners");
    document.querySelectorAll(".hold-toggle-btn").forEach(function (btn) {
      // Remove any existing event listeners by cloning the node
      const newBtn = btn.cloneNode(true);
      if (btn.parentNode) {
        btn.parentNode.replaceChild(newBtn, btn);
      }
      // Add new event listener
      newBtn.addEventListener("click", function (e) {
        e.preventDefault();
        console.log("🎯 Hold toggle clicked");
        const holdCell = newBtn.closest("td");
        const row = holdCell.closest("tr");
        if (typeof window.handleRowHighlight === "function") {
          window.handleRowHighlight(e);
        }
        // Store state globally with all needed data
        window.holdToggleState = {
          currentHoldCell: holdCell,
          intendedState: newBtn.checked,
          currentBatchId: row.getAttribute("data-batch-id"),
          currentLotId: row.getAttribute("data-stock-lot-id"),
          rowIdentifier:
            row.getAttribute("data-stock-lot-id") ||
            row.getAttribute("data-batch-id"),
        };
        console.log("�? Hold toggle state:", window.holdToggleState);
        // Update modal title based on intended action
        const modalTitle = window.holdToggleState.intendedState
          ? "Release Reason"
          : "Hold Reason";
        document
          .getElementById("holdRemarkModal")
          .querySelector("h5").textContent = modalTitle;
        // Clear and show modal
        document.getElementById("holdRemarkInput").value = "";
        document.getElementById("holdRemarkError").textContent = "";
        document.getElementById("holdRemarkModal").style.display = "flex";
        document.getElementById("holdRemarkInput").focus();
      });
    });
  }
  // Save hold/unhold reason
  function restoreHoldToggleState() {
    if (!window.holdToggleState || !window.holdToggleState.currentHoldCell) return;
    const toggle = window.holdToggleState.currentHoldCell.querySelector(".hold-toggle-btn");
    if (toggle) {
      toggle.checked = !window.holdToggleState.intendedState;
    }
  }

  function setupSaveButton() {
    const saveBtn = document.getElementById("saveHoldRemarkBtn");
    if (saveBtn && !window.holdSaveHandlerAttached) {
      window.holdSaveHandlerAttached = true;
      saveBtn.onclick = function () {
        console.log(
          "💾 Save button clicked, current state:",
          window.holdToggleState,
        );
        const remark = document.getElementById("holdRemarkInput").value.trim();
        const errorDiv = document.getElementById("holdRemarkError");
        // Validation
        if (!remark) {
          errorDiv.textContent = "Remark is required!";
          return;
        }
        if (remark.length > 50) {
          errorDiv.textContent = "Remark must be 50 characters or less!";
          return;
        }
        if (!window.holdToggleState.currentLotId) {
          errorDiv.textContent = "Lot ID not found!";
          return;
        }
        const action = window.holdToggleState.intendedState ? "unhold" : "hold";
        console.log("📡 Sending API request:", {
          lot_id: window.holdToggleState.currentLotId,
          remark: remark,
          action: action,
        });
        // Disable save button during request
        saveBtn.disabled = true;
        saveBtn.textContent = "Saving...";
        fetch("/inputscreening/ip_save_hold_unhold_reason/", {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "X-CSRFToken": getCookie("csrftoken"),
          },
          body: JSON.stringify({
            lot_id: window.holdToggleState.currentLotId,
            remark: remark,
            action: action,
          }),
        })
          .then((res) => res.json())
          .then((data) => {
            console.log("📥 Server response:", data);
            // Re-enable save button
            saveBtn.disabled = false;
            saveBtn.textContent = "Save";
            if (data.success) {
              // Close modal first
              document.getElementById("holdRemarkModal").style.display = "none";
              // Show success message
              if (typeof Swal !== "undefined") {
                Swal.fire({
                  icon: "success",
                  title:
                    action === "hold"
                      ? "Row hold successfully!"
                      : "Row released successfully!",
                  timer: 1500,
                  showConfirmButton: false,
                });
              }
              // Update UI immediately
              updateRowUI(window.holdToggleState.currentLotId, action, remark);
              if (typeof window.restoreRowPosition === "function") {
                window.restoreRowPosition();
              }
              // Refresh page after a short delay
              setTimeout(() => {
                location.reload();
              }, 1500);
            } else {
              errorDiv.textContent = data.error || "Failed to save reason!";
              restoreHoldToggleState();
              if (typeof window.restoreRowPosition === "function") {
                window.restoreRowPosition();
              }
            }
          })
          .catch((error) => {
            console.error("�?� Request failed:", error);
            saveBtn.disabled = false;
            saveBtn.textContent = "Save";
            errorDiv.textContent = "Network error occurred!";
            restoreHoldToggleState();
            if (typeof window.restoreRowPosition === "function") {
              window.restoreRowPosition();
            }
          });
      };
    }
  }
  // Close modal handler
  function setupCloseButton() {
    const closeBtn = document.getElementById("closeHoldRemarkModal");
    if (closeBtn && !window.holdCloseHandlerAttached) {
      window.holdCloseHandlerAttached = true;
      closeBtn.onclick = function () {
        console.log("�?� Close button clicked");
        document.getElementById("holdRemarkModal").style.display = "none";
        // Reset toggle to original state
        restoreHoldToggleState();
        // Clear state
        window.holdToggleState = {
          currentHoldCell: null,
          intendedState: null,
          currentBatchId: null,
          currentLotId: null,
          rowIdentifier: null,
        };
        if (typeof window.restoreRowPosition === "function") {
          window.restoreRowPosition();
        }
      };
    }
  }
  // Update row UI after successful hold/unhold operation
  function updateRowUI(lotId, action, remark) {
    console.log("🎨 Updating row UI for lot:", lotId, "action:", action);
    const currentRow = document.querySelector(
      `tr[data-stock-lot-id="${lotId}"]`,
    );
    if (!currentRow) {
      console.log("�?� Row not found for lot ID:", lotId);
      return;
    }
    const toggle = currentRow.querySelector(".hold-toggle-btn");
    const icon = currentRow.querySelector(".hold-remark-icon");
    if (action === "hold") {
      // Hold the row
      if (toggle) toggle.checked = false;
      currentRow.classList.add("row-inactive");
      // Blur all cells except the first one (S.No column)
      currentRow.querySelectorAll("td").forEach((td, idx) => {
        if (idx > 0) {
          td.classList.add("row-inactive-blur");
        } else {
          td.classList.remove("row-inactive-blur"); // Keep S.No column unblurred
        }
      });
      // Show remark icon
      if (icon) {
        icon.style.display = "inline-block";
        icon.innerHTML = `<img src="${(window.IS_STATIC && window.IS_STATIC.viewIcon) || ""}" alt="View Reason" style="width:18px; height:18px;" />`;
        icon.setAttribute("title", "Holding Reason: " + remark);
      }
      console.log("✅ Row hold successfully");
    } else {
      // Unhold/Release the row
      if (toggle) toggle.checked = true;
      currentRow.classList.remove("row-inactive");
      // Remove blur from all cells
      currentRow.querySelectorAll("td").forEach((td) => {
        td.classList.remove("row-inactive-blur");
      });
      // Update remark icon to show release reason or hide it
      if (icon) {
        icon.style.display = "inline-block";
        icon.innerHTML = `<img src="${(window.IS_STATIC && window.IS_STATIC.viewIcon) || ""}" alt="View Reason" style="width:18px; height:18px;" />`;
        icon.setAttribute("title", "Release Reason: " + remark);
      }
      console.log("✅ Row released successfully");
    }
  }
  // Helper function to get CSRF token
  function getCookie(name) {
    let cookieValue = null;
    if (document.cookie && document.cookie !== "") {
      const cookies = document.cookie.split(";");
      for (let i = 0; i < cookies.length; i++) {
        const cookie = cookies[i].trim();
        if (cookie.substring(0, name.length + 1) === name + "=") {
          cookieValue = decodeURIComponent(cookie.substring(name.length + 1));
          break;
        }
      }
    }
    return cookieValue;
  }
  // Initialize all functionality
  attachHoldToggleListeners();
  setupSaveButton();
  setupCloseButton();
  console.log("✅ Hold/Unhold functionality initialized successfully");
});
// ====== Original inline block #6 ======
document.addEventListener("DOMContentLoaded", function () {
  let openTooltip = null;
  let activeTrigger = null;
  let activeStockNo = "";
  const previewCache = new Map();

  function getStockNo(trigger) {
    return (trigger && trigger.dataset ? trigger.dataset.platingStkNo : "").trim();
  }

  function normalizeImageUrl(rawUrl) {
    const value = String(rawUrl || "").trim();
    if (!value) return "";
    try {
      const parsed = new URL(value, window.location.origin);
      if (parsed.origin === window.location.origin) {
        return parsed.pathname + parsed.search + parsed.hash;
      }
      return parsed.href;
    } catch (error) {
      return value;
    }
  }

  function setTooltipLoading(tooltip) {
    const gallery = tooltip ? tooltip.querySelector(".img-gallery") : null;
    if (!gallery) return;
    gallery.innerHTML = '<span style="font-size:12px;color:#777;text-align:center;">Loading...</span>';
  }

  function setTooltipMessage(tooltip, message) {
    const gallery = tooltip ? tooltip.querySelector(".img-gallery") : null;
    if (!gallery) return;
    gallery.textContent = "";
    const messageEl = document.createElement("span");
    messageEl.textContent = message;
    messageEl.style.cssText = "font-size:12px;color:#777;text-align:center;";
    gallery.appendChild(messageEl);
  }

  function renderPreviewImage(trigger, payload) {
    const tooltip = trigger ? trigger.querySelector(".model-image-tooltip") : null;
    const gallery = tooltip ? tooltip.querySelector(".img-gallery") : null;
    if (!gallery) return;

    const imageUrl = normalizeImageUrl(payload && payload.preview_image);
    if (!imageUrl) {
      setTooltipMessage(tooltip, "Coming Soon");
      return;
    }

    gallery.textContent = "";
    const image = document.createElement("img");
    image.src = imageUrl;
    image.alt = "Model image";
    image.decoding = "async";
    image.loading = "lazy";
    image.style.cssText =
      "width:100%;height:100%;object-fit:contain;object-position:center;" +
      "border-radius:6px;background:#fff;display:block;";

    gallery.appendChild(image);
  }

  function loadStockPreview(trigger) {
    const stockNo = getStockNo(trigger);
    const tooltip = trigger ? trigger.querySelector(".model-image-tooltip") : null;
    if (!stockNo || !tooltip) return;

    activeTrigger = trigger;
    activeStockNo = stockNo;

    if (previewCache.has(stockNo)) {
      renderPreviewImage(trigger, previewCache.get(stockNo));
      return;
    }

    setTooltipLoading(tooltip);
    fetch("/adminportal/api/model-hover-preview/?stock_no=" + encodeURIComponent(stockNo), {
      headers: {
        "Accept": "application/json",
        "X-Requested-With": "XMLHttpRequest",
      },
    })
      .then(function (response) {
        if (!response.ok) {
          throw new Error("HTTP " + response.status);
        }
        return response.json();
      })
      .then(function (payload) {
        previewCache.set(stockNo, payload);
        if (activeTrigger !== trigger || activeStockNo !== stockNo) {
          return;
        }
        renderPreviewImage(trigger, payload);
      })
      .catch(function () {
        if (activeTrigger !== trigger || activeStockNo !== stockNo) {
          return;
        }
        setTooltipMessage(tooltip, "Coming Soon");
      });
  }

  // Helper function to completely close a tooltip
  function closeTooltip(tooltip, trigger) {
    if (tooltip) {
      console.log("🔴 Closing tooltip completely");
      // Remove pinned class
      tooltip.classList.remove("pinned");
      // Hide the entire tooltip completely
      tooltip.style.opacity = "0";
      tooltip.style.pointerEvents = "none";
      tooltip.style.visibility = "hidden";
      tooltip.style.display = "none"; // ✅ ADDED: Force display none
      // Hide buttons
      const infoBtn = tooltip.querySelector(".info-btn");
      const closeBtn = tooltip.querySelector(".close-btn");
      if (infoBtn) infoBtn.style.display = "none";
      if (closeBtn) closeBtn.style.display = "none";
      // Remove visual indicator from trigger
      if (trigger) {
        trigger.style.backgroundColor = "";
        trigger.style.borderRadius = "";
      }
      // Clear global reference
      openTooltip = null;
      if (activeTrigger === trigger) {
        activeTrigger = null;
        activeStockNo = "";
      }
      console.log("✅ Tooltip completely closed");
    }
  }
  document
    .querySelectorAll(".model-image-tooltip .info-btn")
    .forEach(function (btn) {
      btn.addEventListener("click", function (e) {
        e.stopPropagation();
        const trigger = btn.closest(".model-hover-trigger");
        const stockNo = getStockNo(trigger);
        if (!stockNo) return;

        let url = "/adminportal/dp_visualaid/";
        url += "?plating_stk_no=" + encodeURIComponent(stockNo);
        window.location.href = url;
      });
    });
  // Tooltip show/hide logic
  document.querySelectorAll(".model-hover-trigger").forEach(function (trigger) {
    const tooltip = trigger.querySelector(".model-image-tooltip");
    trigger.addEventListener("mouseenter", function () {
      if (tooltip && !tooltip.classList.contains("pinned")) {
        loadStockPreview(trigger);
        tooltip.style.display = "flex"; // ✅ ADDED: Reset display
        tooltip.style.visibility = "visible"; // ✅ ADDED: Reset visibility
        tooltip.style.opacity = "1";
        tooltip.style.pointerEvents = "auto";
        // Show Info and Close buttons on hover
        const infoBtn = tooltip.querySelector(".info-btn");
        const closeBtn = tooltip.querySelector(".close-btn");
        if (infoBtn) infoBtn.style.display = "block";
        if (closeBtn) closeBtn.style.display = "block";
      }
    });
    trigger.addEventListener("mouseleave", function () {
      if (tooltip && !tooltip.classList.contains("pinned")) {
        tooltip.style.opacity = "0";
        tooltip.style.pointerEvents = "none";
        // Hide Info and Close buttons when not hovering and not pinned
        const infoBtn = tooltip.querySelector(".info-btn");
        const closeBtn = tooltip.querySelector(".close-btn");
        if (infoBtn) infoBtn.style.display = "none";
        if (closeBtn) closeBtn.style.display = "none";
      }
    });
    // Keep tooltip visible when hovering over it
    if (tooltip) {
      tooltip.addEventListener("mouseenter", function () {
        if (!tooltip.classList.contains("pinned")) {
          tooltip.style.display = "flex"; // ✅ ADDED: Reset display
          tooltip.style.visibility = "visible"; // ✅ ADDED: Reset visibility
        }
        tooltip.style.opacity = "1";
        tooltip.style.pointerEvents = "auto";
        // Keep buttons visible when hovering over tooltip
        const infoBtn = tooltip.querySelector(".info-btn");
        const closeBtn = tooltip.querySelector(".close-btn");
        if (infoBtn) infoBtn.style.display = "block";
        if (closeBtn) closeBtn.style.display = "block";
      });
      tooltip.addEventListener("mouseleave", function () {
        if (!tooltip.classList.contains("pinned")) {
          tooltip.style.opacity = "0";
          tooltip.style.pointerEvents = "none";
          // Hide buttons when leaving tooltip and not pinned
          const infoBtn = tooltip.querySelector(".info-btn");
          const closeBtn = tooltip.querySelector(".close-btn");
          if (infoBtn) infoBtn.style.display = "none";
          if (closeBtn) closeBtn.style.display = "none";
        }
      });
    }
    trigger.addEventListener("click", function (e) {
      e.stopPropagation();
      if (tooltip) {
        // Close any previously opened tooltip
        if (openTooltip && openTooltip !== tooltip) {
          const prevTrigger = openTooltip.closest(".model-hover-trigger");
          closeTooltip(openTooltip, prevTrigger);
        }
        loadStockPreview(trigger);
        tooltip.classList.add("pinned");
        tooltip.style.display = "flex"; // ✅ ADDED: Ensure display
        tooltip.style.visibility = "visible"; // ✅ ADDED: Ensure visibility
        tooltip.style.opacity = "1";
        tooltip.style.pointerEvents = "auto";
        openTooltip = tooltip;
        // Keep buttons visible when pinned
        const infoBtn = tooltip.querySelector(".info-btn");
        const closeBtn = tooltip.querySelector(".close-btn");
        if (infoBtn) infoBtn.style.display = "block";
        if (closeBtn) closeBtn.style.display = "block";
        // Add visual indicator that tooltip is pinned
        trigger.style.backgroundColor = "#e3f2fd";
        trigger.style.borderRadius = "4px";
      }
    });
    // ✅ FIXED: Handle Close button click - Complete tooltip closure
    const closeBtn = tooltip?.querySelector(".close-btn");
    if (closeBtn) {
      // Initially hide the button
      closeBtn.style.display = "none";
      closeBtn.addEventListener("click", function (e) {
        e.stopPropagation();
        console.log("🔴 Close button clicked");
        // Use the helper function to completely close tooltip
        closeTooltip(tooltip, trigger);
        // Feedback animation for close button
        closeBtn.style.transform = "scale(0.9)";
        setTimeout(() => {
          if (closeBtn.style) {
            closeBtn.style.transform = "scale(1)";
          }
        }, 150);
      });
      // ✅ Add hover effect for close button
      closeBtn.addEventListener("mouseenter", function () {
        closeBtn.style.backgroundColor = "#c82333";
        closeBtn.style.transform = "scale(1.05)";
      });
      closeBtn.addEventListener("mouseleave", function () {
        closeBtn.style.backgroundColor = "#dc3545";
        closeBtn.style.transform = "scale(1)";
      });
    }
  });
  // ✅ ENHANCED: Close tooltip when clicking outside
  document.addEventListener("click", function (e) {
    if (
      openTooltip &&
      !e.target.closest(".model-image-tooltip") &&
      !e.target.closest(".model-hover-trigger")
    ) {
      const openTrigger = openTooltip.closest(".model-hover-trigger");
      closeTooltip(openTooltip, openTrigger);
    }
  });
  // ✅ Close tooltip with ESC key
  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape" && openTooltip) {
      const openTrigger = openTooltip.closest(".model-hover-trigger");
      closeTooltip(openTooltip, openTrigger);
      console.log("✅ Tooltip closed with ESC key");
    }
  });
  // Prevent tooltip from closing when clicking inside it
  document.querySelectorAll(".model-image-tooltip").forEach(function (tooltip) {
    tooltip.addEventListener("click", function (e) {
      e.stopPropagation();
    });
  });
});
// ====== Original inline block #7 ======
document.addEventListener("DOMContentLoaded", function () {
  console.log(
    "🎬 IS Pick Table: Initializing tray verification modal + button state check",
  );
  // ══════════════════════════════════════════════════════════════════════════════
  // CHECK BUTTON STATE ON PAGE LOAD
  // If tray verification is already complete for a lot, enable Accept/Reject buttons
  // ══════════════════════════════════════════════════════════════════════════════
  function checkButtonStateOnLoad() {
    // Read ip_person_qty_verified directly from the server-rendered data attribute.
    // This avoids one API call per row on every page load and ensures the buttons
    // stay enabled after a browser refresh.
    const rows = document.querySelectorAll("tr[data-stock-lot-id]");
    rows.forEach(function (row) {
      const lotId = row.getAttribute("data-stock-lot-id");
      if (!lotId) return;
      // Check both the <tr> and the nested view <a> tag for the flag
      const rowFlag = row.getAttribute("data-all-trays-verified") || row.getAttribute("data-ip-person-qty-verified");
      const aTag = row.querySelector("a[data-ip-person-qty-verified]");
      const aFlag = aTag ? aTag.getAttribute("data-ip-person-qty-verified") : null;
      const verified = rowFlag === "true" || aFlag === "true";
      if (verified) {
        isEnableActionButtons(lotId, true);
      }
    });
  }
  // Run immediately – data-attribute is already present in rendered HTML
  checkButtonStateOnLoad();
  // ══════════════════════════════════════════════════════════════════════════════
  // TRAY VERIFICATION MODAL (EXISTING CODE BELOW)
  // ══════════════════════════════════════════════════════════════════════════════
  // ─── Helpers ───────────────────────────────────────────────────────────────
  function tvmGetCookie(name) {
    let val = null;
    if (document.cookie) {
      document.cookie.split(";").forEach(function (c) {
        const trimmed = c.trim();
        if (trimmed.startsWith(name + "=")) {
          val = decodeURIComponent(trimmed.slice(name.length + 1));
        }
      });
    }
    return val;
  }
  function tvmSafeId(trayId) {
    return trayId.replace(/[^a-zA-Z0-9]/g, "-");
  }
  // ─── Auto-format tray ID to standard format ─────────────────────────────────
  function tvmFormatTrayId(input) {
    // Accepted inputs: jb-a00001, JB-A00001, jbA00001, jba00001
    // Output: JB-A00001 (standard)
    if (!input) return "";
    input = input.trim().toUpperCase();
    // Remove spaces and convert to standard format JB-AXXXXX
    input = input.replace(/\s+/g, "");
    if (input.length < 9) return input;
    // If it's 9 chars and already in format JB-A00001, return as is
    if (input.match(/^[A-Z]{2}-[A-Z]\d{5}$/)) return input;
    // If it's 8 chars like JBA00001, insert hyphen: JB-A00001
    if (input.match(/^[A-Z]{2}[A-Z]\d{5}$/)) {
      return input.substring(0, 2) + "-" + input.substring(2);
    }
    return input.substring(0, 9);
  }
  // ─── HTML escaping (security: tray IDs are user-entered; never render raw) ──
  var tvmEscapeHtml = (typeof window !== "undefined" && window.escapeHtml) ||
    function (value) {
      return String(value === null || value === undefined ? "" : value)
        .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
    };
  // ─── Status helpers ────────────────────────────────────────────────────────
  const VERIFIED_BADGE_STYLE =
    "background:#f0fdf7;color:#1ba878;border:1px solid #c8f0e0;padding:6px 16px;border-radius:20px;font-size:12px;font-weight:700;white-space:nowrap;";
  const UNVERIFIED_BADGE_STYLE =
    "background:#fff5f0;color:#d67d3a;border:1px solid #fde8d0;padding:6px 16px;border-radius:20px;font-size:12px;font-weight:700;white-space:nowrap;";
  var tvmScanInFlight = false;
  var tvmPendingTrayId = "";

  function tvmFocusScanInput(selectText) {
    const input = document.getElementById("tvm-scan-input");
    if (!input) return;
    setTimeout(function () {
      input.focus();
      if (selectText) {
        try { input.select(); } catch (e) {}
        try { input.setSelectionRange(0, input.value.length); } catch (e2) {}
      } else {
        try { input.setSelectionRange(0, 0); } catch (e3) {}
      }
    }, 60);
  }

  // ─── Running info banner — updates the inner text span ────────────────────
  function tvmSetRunningInfo(text) {
    const textEl = document.getElementById("tvm-running-info-text");
    if (!textEl) return;
    const colonIdx = text.indexOf(":");
    if (colonIdx !== -1) {
      const label = text.substring(0, colonIdx);
      const value = text.substring(colonIdx + 1);
      textEl.innerHTML =
        '<span style="color:#028084;font-weight:700;">' + tvmEscapeHtml(label) + ":</span>" +
        '<span style="color:#555;">' + tvmEscapeHtml(value) + "</span>";
    } else {
      textEl.innerHTML = '<span style="color:#028084;">' + tvmEscapeHtml(text) + "</span>";
    }
  }
  // ─── Auto-scroll to tray row with smooth behavior and subtle highlight ───────────
  function tvmScrollToTray(trayId, shouldHighlight) {
    const sid = tvmSafeId(trayId);
    const row = document.getElementById("tvm-row-" + sid);
    if (!row) return;
    const container = document.getElementById("tvm-table-body-container");
    if (!container) return;
    // Smooth scroll to row (center it in viewport if possible)
    row.scrollIntoView({ behavior: "smooth", block: "nearest" });
    // Apply very subtle highlight for 1.5 seconds
    if (shouldHighlight) {
      row.style.boxShadow = "0 0 0 1px rgba(27,168,120,.2)";
      row.style.background = "rgba(240, 253, 247, 0.3)";
      setTimeout(function () {
        row.style.boxShadow = "";
        row.style.background = "";
      }, 1500);
    }
  }
  // ─── Enable Accept/Reject buttons in main table row from backend response ──
  function tvmEnableActions(lotId, enableActions) {
    if (enableActions.reject) {
      var rejectBtn = document.querySelector(
        '.tray-scan-btn[data-stock-lot-id="' + lotId + '"]',
      );
      if (rejectBtn) rejectBtn.removeAttribute("disabled");
    }
    // Update row's data-tray-verify attribute for correct state tracking
    var tableRow = document.querySelector(
      'tr[data-stock-lot-id="' + lotId + '"]',
    );
    if (tableRow) tableRow.setAttribute("data-tray-verify", "True");
  }
  function tvmSetActivity(type, msg) {
    const dot = document.getElementById("tvm-activity-dot");
    const el = document.getElementById("tvm-activity-msg");
    const bar = document.getElementById("tvm-activity-bar");
    if (!dot || !el) return;
    el.textContent = msg;
    const styles = {
      wait: {
        dot: "#f0ad4e",
        bar: "#fafbfc",
        text: "#444",
        boxShadow: "0 0 8px rgba(240,173,78,.4)",
      },
      success: {
        dot: "#1ba878",
        bar: "#f0fdf7",
        text: "#1ba878",
        boxShadow: "0 0 8px rgba(27,168,120,.3)",
      },
      error: {
        dot: "#d67d3a",
        bar: "#fff5f0",
        text: "#d67d3a",
        boxShadow: "0 0 8px rgba(214,125,58,.3)",
      },
      info: {
        dot: "#028084",
        bar: "#f0f9fa",
        text: "#028084",
        boxShadow: "0 0 8px rgba(2,128,132,.3)",
      },
    };
    const s = styles[type] || styles.wait;
    dot.style.background = s.dot;
    dot.style.boxShadow = s.boxShadow;
    bar.style.background = s.bar;
    el.style.color = s.text;
  }
  function tvmUpdateStats(verified, total, pending, verified_qty, total_qty) {
    document.getElementById("tvm-total-count").textContent = total;
    document.getElementById("tvm-verified-count").textContent = verified;
    document.getElementById("tvm-pending-count").textContent = pending;
    const display = document.getElementById("tvm-verified-qty-display");
    if (display && verified_qty !== undefined && total_qty !== undefined) {
      display.textContent = verified_qty + "/" + total_qty;
    }
    // Update progress bars
    const qtyProgress = document.getElementById("tvm-qty-progress");
    const pendingProgress = document.getElementById("tvm-pending-progress");
    // VERIFIED QTY progress bar - full green when quantity is verified
    if (qtyProgress && total_qty > 0) {
      const qtyPercent = (verified_qty / total_qty) * 100;
      qtyProgress.style.width = qtyPercent + "%";
      // Trigger sparkle animation when complete
      const sparkleEl = document.getElementById("tvm-qty-sparkle");
      if (sparkleEl && qtyPercent >= 100) {
        sparkleEl.style.animation = "none"; // Reset animation
        void sparkleEl.offsetWidth; // Trigger reflow
        sparkleEl.style.animation = "qtySparkle 1.2s ease-in-out";
      }
    }
    // PENDING progress bar - shows scanning progress (verified/total)
    if (pendingProgress && total > 0) {
      const scanPercent = ((total - pending) / total) * 100;
      pendingProgress.style.width = scanPercent + "%";
    }
  }
  // ─── Render table ──────────────────────────────────────────────────────────
  function tvmRenderTable(trays) {
    const tbody = document.getElementById("tvm-tray-tbody");
    if (!trays || trays.length === 0) {
      tbody.innerHTML =
        '<tr><td colspan="4" style="text-align:center;color:#bbb;padding:24px;font-size:13px;">No trays found for this lot</td></tr>';
      return;
    }
    tbody.innerHTML = trays
      .map(function (t) {
        const sid = tvmSafeId(t.tray_id);
        const badge = t.is_verified
          ? VERIFIED_BADGE_STYLE
          : UNVERIFIED_BADGE_STYLE;
        const label = t.is_verified ? "Verified ✅" : "Not Verified";
        const topTag = t.top_tray
          ? ' <sup style="color:#028084;font-size:10px;font-weight:700;">TOP</sup>'
          : "";
        // REMOVED: Individual undo buttons per tray - using common Undo All button instead
        return (
          '<tr id="tvm-row-' +
          sid +
          '" style="border-bottom:1px solid #e6f1f2;transition:background .4s; cursor:default;">' +
          '<td style="padding:12px 10px;color:#666;font-weight:600;font-size:13px;">' +
          t.sno +
          "</td>" +
          '<td style="padding:12px 10px;font-family:monospace;font-weight:600;color:#028084;font-size:13px;letter-spacing:.5px;cursor:pointer;user-select:text;transition:background .2s;" class="tvm-copy-tray-id" data-tray-id="' +
          tvmEscapeHtml(t.tray_id) +
          '" title="Click to copy">' +
          tvmEscapeHtml(t.tray_id) +
          topTag +
          "</td>" +
          '<td style="padding:12px 10px;text-align:center;color:#666;font-weight:600;">' +
          (t.qty !== null && t.qty !== undefined ? t.qty : "—") +
          "</td>" +
          '<td style="padding:12px 10px;text-align:center;">' +
          '<span id="tvm-badge-' +
          sid +
          '" style="' +
          badge +
          '">' +
          label +
          "</span>" +
          "</td>" +
          "</tr>"
        );
      })
      .join("");
    // FIX 3: Add copy-to-clipboard handlers for tray ID
    const trayIdCells = tbody.querySelectorAll(".tvm-copy-tray-id");
    trayIdCells.forEach(function (cell) {
      cell.addEventListener("click", function (e) {
        e.stopPropagation();
        const trayId = this.getAttribute("data-tray-id");
        navigator.clipboard
          .writeText(trayId)
          .then(function () {
            // FIX 1: Show compact feedback message
            tvmSetActivity("success", "Copied: " + trayId);
            // FIX 1: Auto-focus scan input for next scan
            const input = document.getElementById("tvm-scan-input");
            if (input) {
              setTimeout(function () {
                input.focus();
                input.setSelectionRange(0, 0);
              }, 100);
            }
            setTimeout(function () {
              tvmSetActivity("wait", "Waiting for tray scan…");
            }, 2000);
          })
          .catch(function () {
            tvmSetActivity("error", "Failed to copy \u274c");
          });
      });
    });
  }
  // Unverify a tray (redo option)
  function tvmUnverifyTray(trayId) {
    var lotId = window._tvmCurrentLotId;
    if (!lotId || !trayId) return;
    tvmSetActivity("info", "Reverting: " + trayId + "\u2026");
    fetch("/inputscreening/unverify_tray/", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-CSRFToken": tvmGetCookie("csrftoken") },
      body: JSON.stringify({ lot_id: lotId, tray_id: trayId }),
    })
      .then(function (r) { return r.json(); })
      .then(function (data) {
        if (data.success) {
          tvmUpdateStats(data.verified, data.total, data.pending, data.verified_qty, data.total_qty);
          var sid = tvmSafeId(trayId);
          var badge = document.getElementById("tvm-badge-" + sid);
          var row = document.getElementById("tvm-row-" + sid);
          if (badge) {
            badge.style.cssText = PENDING_BADGE_STYLE;
            badge.textContent = "Not Verified";
          }
          // Update local tray store
          if (window._tvmTrays) {
            var tr = window._tvmTrays.find(function (t) {
              return t.tray_id === trayId;
            });
            if (tr) tr.is_verified = false;
          }
          tvmApplyPickTableVerificationState(lotId, data);
          // Enable Undo button when trays become unverified (pending > 0)
          if (data.pending > 0) {
            var undoBtn = document.getElementById("tvm-undo-all-btn");
            if (undoBtn) {
              undoBtn.disabled = false;
              undoBtn.style.opacity = "1";
              undoBtn.style.cursor = "pointer";
              undoBtn.title = "Undo all verifications - mark all trays as Not Verified";
            }
          }
          tvmSetActivity("wait", "Tray unverified \u2013 " + data.pending + " pending. Scan next tray\u2026");
        } else {
          tvmSetActivity("error", data.error || "Failed to unverify tray");
        }
      })
      .catch(function () { tvmSetActivity("error", "Network error \u274c"); });
  }

  // ─── TVM Undo All ───────────────────────────────────────────────────────
  function tvmUndoAll() {
    var lotId = window._tvmCurrentLotId;
    if (!lotId) return;
    
    // Confirm before clearing all verifications
    if (window.Swal) {
      Swal.fire({
        title: 'Undo All Verifications?',
        text: 'This will mark all trays as Not Verified. Are you sure?',
        icon: 'warning',
        showCancelButton: true,
        confirmButtonText: 'Yes, Undo All',
        cancelButtonText: 'Cancel',
        confirmButtonColor: '#e65100',
        cancelButtonColor: '#6c757d',
        focusCancel: true,
      }).then(function(result) {
        if (result.isConfirmed) {
          _performUndoAll(lotId);
        }
      });
    } else {
      if (confirm('Undo all verifications? All trays will be marked as Not Verified.')) {
        _performUndoAll(lotId);
      }
    }
  }
  
  function _performUndoAll(lotId) {
    var btn = document.getElementById("tvm-undo-all-btn");
    if (btn) { btn.disabled = true; btn.textContent = "Undoing..."; }
    tvmSetActivity("info", "Clearing all verifications...");
    
    fetch("/inputscreening/clear_all_verifications/", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-CSRFToken": tvmGetCookie("csrftoken") },
      body: JSON.stringify({ lot_id: lotId }),
    })
      .then(function (r) { return r.json(); })
      .then(function (data) {
        if (data.success) {
          tvmSetActivity("success", "All verifications cleared ✓");
          tvmSetRunningInfo("Status: All verifications cleared");
          tvmApplyPickTableVerificationState(lotId, data);
          tvmLoadTrays(lotId);
          Swal.fire({
            icon: "success",
            title: "All Verifications Cleared",
            text: data.message || "All trays are now Not Verified.",
            timer: 1800,
            timerProgressBar: true,
            showConfirmButton: false,
          });
        } else {
          tvmSetActivity("error", data.error || "Failed to undo verifications.");
          Swal.fire("Error", data.error || "Failed to clear verifications.", "error");
        }
        if (btn) { btn.disabled = false; btn.innerHTML = "↶ Undo All"; }
      })
      .catch(function () {
        tvmSetActivity("error", "Network error ✗");
        Swal.fire("Error", "Network error while undoing verifications.", "error");
        if (btn) { btn.disabled = false; btn.innerHTML = "↶ Undo All"; }
      });
  }

  // ─── TVM Draft Save ─────────────────────────────────────────────────────
  function tvmSaveDraft() {
    var lotId = window._tvmCurrentLotId;
    if (!lotId) return;
    var btn = document.getElementById("tvm-save-draft-btn");
    if (btn) { btn.disabled = true; btn.textContent = "Saving…"; }
    tvmSetActivity("info", "Saving draft…");
    fetch("/inputscreening/save_tvm_draft/", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-CSRFToken": tvmGetCookie("csrftoken") },
      body: JSON.stringify({ lot_id: lotId }),
    })
      .then(function (r) { return r.json(); })
      .then(function (data) {
        if (data.success) {
          // Update the pick table row to show "Draft" badge immediately
          var table = document.getElementById("order-listing");
          if (table) {
            var rows = table.querySelectorAll("tbody tr");
            rows.forEach(function (row) {
              if (row.getAttribute("data-stock-lot-id") === lotId) {
                // Update Q circle to half-green (draft style)
                var lotStatusCell = row.querySelector("[data-lot-status-cell]") ||
                  (function () {
                    // Find the "Lot Status" td — it contains the Yet to Start / Draft pill
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
                // Update Q icon to half-green gradient
                var qIcon = row.querySelector(".process-status-group div:first-child");
                if (qIcon) {
                  qIcon.style.background = "linear-gradient(to right, #0c8249 50%, #bdbdbd 50%)";
                }
              }
            });
          }
          tvmSetActivity("success", "Draft saved \u2714 You can resume scanning anytime.");
          tvmSetRunningInfo("Status: Draft Saved \uD83D\uDCBE");
          Swal.fire({
            icon: "success",
            title: "Draft Saved!",
            text: "Verification progress saved. You can resume scanning anytime.",
            timer: 1800,
            timerProgressBar: true,
            showConfirmButton: false,
          }).then(function () { tvmClose(); });
        } else {
          tvmSetActivity("error", data.error || "Failed to save draft.");
          if (btn) { btn.disabled = false; btn.innerHTML = "\uD83D\uDCBE Save Draft"; }
        }
      })
      .catch(function () {
        tvmSetActivity("error", "Network error \u274c");
        if (btn) { btn.disabled = false; btn.innerHTML = "\uD83D\uDCBE Save Draft"; }
      });
  }
  function tvmCount(value) {
    var parsed = parseInt(value || 0, 10);
    return isNaN(parsed) ? 0 : parsed;
  }

  function tvmFindPickTableRows(lotId) {
    var table = document.getElementById("order-listing");
    if (!table || !lotId) return [];
    return Array.from(table.querySelectorAll("tbody tr")).filter(function (row) {
      return row.getAttribute("data-stock-lot-id") === lotId;
    });
  }

  function tvmSetActionButtons(row, enable) {
    [".btn-accept-is", ".btn-reject-is"].forEach(function (selector) {
      var button = row.querySelector(selector);
      if (!button) return;
      button.disabled = !enable;
      button.style.cursor = enable ? "pointer" : "not-allowed";
      button.style.opacity = enable ? "1" : "0.5";
    });
  }

  function tvmSetQStatus(row, state) {
    var qIcon = row.querySelector("[data-process-status-q]") ||
      row.querySelector(".process-status-group > div:first-child");
    if (!qIcon) return;
    if (state === "complete") {
      qIcon.style.backgroundColor = "#0c8249";
      qIcon.style.background = "#0c8249";
      qIcon.style.opacity = "1";
    } else if (state === "partial") {
      qIcon.style.backgroundColor = "";
      qIcon.style.background = "linear-gradient(to right, #0c8249 50%, #bdbdbd 50%)";
      qIcon.style.opacity = "1";
    } else {
      qIcon.style.backgroundColor = "#bdbdbd";
      qIcon.style.background = "#bdbdbd";
      qIcon.style.opacity = "1";
    }
  }

  function tvmPaintLotStatus(cell, label) {
    if (!cell) return;
    var pill = cell.querySelector("div");
    if (!pill) {
      pill = document.createElement("div");
      pill.className = "d-inline-block px-3 fw-semibold text-center rounded-pill";
      cell.innerHTML = "";
      cell.appendChild(pill);
    }
    pill.style.fontSize = label === "Yet to Start" ? "13px" : "12px";
    pill.style.whiteSpace = "nowrap";
    pill.style.padding = "5px";
    if (label === "In Progress") {
      pill.style.border = "1px solid #0c8249";
      pill.style.backgroundColor = "#d7f2e3";
      pill.style.color = "#0c5c33";
      pill.textContent = "In Progress";
      return;
    }
    if (label === "Draft") {
      pill.style.border = "1px solid #4997ac";
      pill.style.backgroundColor = "#d1f2f3";
      pill.style.color = "#03425d";
      pill.textContent = "Draft";
      return;
    }
    pill.style.border = "1px solid #f9a825";
    pill.style.backgroundColor = "#fff8e1";
    pill.style.color = "#b26a00";
    pill.textContent = "Yet to Start";
  }

  function tvmApplyPickTableVerificationState(lotId, data) {
    var rowUi = (data && data.row_ui) || {};
    var total = tvmCount(data && data.total);
    var verified = tvmCount(data && data.verified);
    var pending = data && data.pending !== undefined ? tvmCount(data.pending) : Math.max(total - verified, 0);
    var state = rowUi.verification_state || "not_started";
    if (!rowUi.verification_state) {
      if (data && data.all_verified) state = "all_verified";
      else if (total > 0 && verified > 0 && pending > 0) state = "partial_verified";
    }

    var complete = state === "all_verified";
    var partial = state === "partial_verified";
    var actionsEnabled = rowUi.actions_enabled !== undefined ? !!rowUi.actions_enabled : complete;
    var qState = rowUi.process_q_state || (complete ? "complete" : partial ? "partial" : "pending");
    var lotStatusLabel = rowUi.lot_status_label || (complete ? "In Progress" : partial ? "Draft" : "Yet to Start");
    var currentStageLabel = rowUi.current_stage_label || (complete ? "Input Screening" : "");

    tvmFindPickTableRows(lotId).forEach(function (row) {
      row.setAttribute("data-all-trays-verified", complete ? "true" : "false");
      row.setAttribute("data-partial-trays-verified", partial ? "true" : "false");
      row.setAttribute("data-ip-person-qty-verified", complete ? "true" : "false");
      row.setAttribute("data-draft-tray-verify", partial ? "True" : "False");
      tvmSetActionButtons(row, actionsEnabled);
      tvmSetQStatus(row, qState);
      tvmPaintLotStatus(row.querySelector("[data-lot-status-cell]") || row.querySelector("td:nth-child(10)"), lotStatusLabel);
      if (currentStageLabel) {
        var currentStageCell = row.querySelector("[data-current-stage-cell]") || row.querySelector("td:nth-child(11)");
        var currentStagePill = currentStageCell ? currentStageCell.querySelector("div") : null;
        if (currentStagePill) currentStagePill.textContent = currentStageLabel;
      }
    });
  }

  function isEnableActionButtons(lotId, enable) {
    tvmApplyPickTableVerificationState(lotId, {
      all_verified: !!enable,
      verified: enable ? 1 : 0,
      total: enable ? 1 : 0,
      pending: enable ? 0 : 1,
      row_ui: {
        verification_state: enable ? "all_verified" : "not_started",
        process_q_state: enable ? "complete" : "pending",
        lot_status_label: enable ? "In Progress" : "Yet to Start",
        current_stage_label: enable ? "Input Screening" : "",
        actions_enabled: !!enable,
      },
    });
  }
  // ─── Mark S circle as half-green (WIP) ──────────────────────────────
  // Exposed globally so inputscreening_reject_modal.js can call it too.
  window.isMarkSCircleWip = function (lotId) {
    const table = document.getElementById("order-listing");
    if (!table) return;
    table.querySelectorAll("tbody tr").forEach(function (row) {
      if (row.getAttribute("data-stock-lot-id") !== lotId) return;
      const processStatusCell = row.querySelector("td:nth-child(9)");
      if (!processStatusCell) return;
      const sIcon = processStatusCell.querySelector("div > div:nth-child(2)"); // S icon
      if (sIcon) {
        sIcon.style.background =
          "linear-gradient(to right, #0c8249 50%, #bdbdbd 50%)";
        sIcon.style.backgroundColor = ""; // clear solid colour so gradient shows
      }
    });
  };
  // ─── Verify All trays ──────────────────────────────────────────────────────
  function tvmVerifyAll() {
    var lotId = window._tvmCurrentLotId;
    if (!lotId) return;
    
    // Get all trays
    var trays = window._tvmTrays || [];
    var unverifiedTrays = trays.filter(function(t) { return !t.is_verified; });
    
    if (unverifiedTrays.length === 0) {
      Swal.fire({
        icon: "info",
        title: "All Verified",
        text: "All trays are already verified.",
        timer: 1500,
        showConfirmButton: false,
      });
      return;
    }
    
    // Confirm before verifying all
    if (window.Swal) {
      Swal.fire({
        title: 'Verify All Trays?',
        text: 'This will verify all ' + unverifiedTrays.length + ' unverified trays at once.',
        icon: 'info',
        showCancelButton: true,
        confirmButtonText: 'Yes, Verify All',
        cancelButtonText: 'Cancel',
        confirmButtonColor: '#1976d2',
        cancelButtonColor: '#6c757d',
        focusCancel: false,
      }).then(function(result) {
        if (result.isConfirmed) {
          _performVerifyAll(lotId, unverifiedTrays);
        }
      });
    } else {
      if (confirm('Verify all ' + unverifiedTrays.length + ' trays?')) {
        _performVerifyAll(lotId, unverifiedTrays);
      }
    }
  }
  
  function _performVerifyAll(lotId, unverifiedTrays) {
    var cb = document.getElementById("tvm-verify-all-cb");
    if (cb) cb.disabled = true;
    
    tvmSetActivity("info", "Verifying all trays... (0/" + unverifiedTrays.length + ")");
    tvmSetRunningInfo("Verifying: 0/" + unverifiedTrays.length);
    
    var verified = 0;
    var failed = [];
    
    // Verify trays sequentially
    function verifyNextTray(index) {
      if (index >= unverifiedTrays.length) {
        // All done
        if (failed.length > 0) {
          Swal.fire({
            icon: "warning",
            title: "Partial Success",
            text: "Verified " + verified + " tray(s), but " + failed.length + " failed:\n" + failed.join(", "),
            timer: 3000,
            timerProgressBar: true,
          });
        } else {
          Swal.fire({
            icon: "success",
            title: "All Verified!",
            text: "Refreshing the page before Accept / Reject.",
            timer: 2000,
            showConfirmButton: false,
          }).then(function () { window.location.reload(); });
        }
        tvmSetActivity("success", "Verify All completed ✓");
        tvmLoadTrays(lotId); // Reload to refresh status
        var cb = document.getElementById("tvm-verify-all-cb");
        if (cb) { cb.checked = false; cb.disabled = false; }
        return;
      }
      
      var tray = unverifiedTrays[index];
      
      fetch("/inputscreening/verify_tray/", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-CSRFToken": tvmGetCookie("csrftoken"),
        },
        body: JSON.stringify({ lot_id: lotId, tray_id: tray.tray_id }),
      })
        .then(function(r) { return r.json(); })
        .then(function(data) {
          if (data.success) {
            verified++;
            // Update badge and stats
            var sid = tvmSafeId(tray.tray_id);
            var badge = document.getElementById("tvm-badge-" + sid);
            if (badge) {
              badge.style.cssText = VERIFIED_BADGE_STYLE;
              badge.textContent = "Verified ✅";
            }
            tvmUpdateStats(
              data.verified,
              data.total,
              data.pending,
              data.verified_qty,
              data.total_qty,
            );
            tvmSetActivity("info", "Verifying... (" + verified + "/" + unverifiedTrays.length + ")");
            tvmSetRunningInfo("Verified: " + verified + "/" + unverifiedTrays.length);
            
            // Update local tray store
            if (window._tvmTrays) {
              var tr = window._tvmTrays.find(function(t) { return t.tray_id === tray.tray_id; });
              if (tr) tr.is_verified = true;
            }
          } else {
            failed.push(tray.tray_id);
          }
          
          // Continue with next tray
          setTimeout(function() { verifyNextTray(index + 1); }, 300);
        })
        .catch(function() {
          failed.push(tray.tray_id);
          setTimeout(function() { verifyNextTray(index + 1); }, 300);
        });
    }
    
    verifyNextTray(0);
  }
  function tvmFetchTrayData(lotId) {
    return fetch("/inputscreening/get_dp_trays/?lot_id=" + encodeURIComponent(lotId))
      .then(function (r) {
        return r.json();
      })
      .catch(function () {
        return { success: false, error: "Network error" };
      });
  }

  function tvmTrayPendingCount(data) {
    if (!data) return 0;
    if (data.pending !== undefined && data.pending !== null) {
      var pending = parseInt(data.pending || 0, 10);
      return isNaN(pending) ? 0 : pending;
    }
    return (data.trays || []).filter(function (tray) {
      return tray && !tray.is_verified;
    }).length;
  }

  function tvmTrayTotalCount(data) {
    if (!data) return 0;
    if (data.total !== undefined && data.total !== null) {
      var total = parseInt(data.total || 0, 10);
      return isNaN(total) ? 0 : total;
    }
    return (data.trays || []).length;
  }

  function tvmFindTrayInData(data, trayId) {
    var normalizedTrayId = tvmFormatTrayId(trayId || "");
    return (data && data.trays || []).find(function (tray) {
      return tray && tvmFormatTrayId(tray.tray_id || "") === normalizedTrayId;
    }) || null;
  }

  function tvmApplyTrayData(lotId, data) {
    const tbody = document.getElementById("tvm-tray-tbody");
    if (!tbody) return data;
    if (!data || !data.success) {
      tbody.innerHTML =
        '<tr><td colspan="4" style="text-align:center;color:#d67d3a;padding:28px;">Error: ' +
        tvmEscapeHtml((data && data.error) || "Could not load trays") +
        "</td></tr>";
      return data;
    }
    // Update plating stock number (ERR 3)
    const platEl = document.getElementById("tvm-plating-stk");
    if (platEl && data.plating_stk_no) {
      platEl.textContent = data.plating_stk_no;
    }
    // Store trays list and pending count for local lookup
    window._tvmPendingCount = data.pending;
    window._tvmTrays = data.trays || [];
    window._tvmCurrentLotId = lotId; // Store current lot for button handlers
    tvmUpdateStats(
      data.verified,
      data.total,
      data.pending,
      data.verified_qty,
      data.total_qty,
    );
    tvmRenderTable(data.trays);
    if (data.total === 0) {
      tvmSetActivity(
        "info",
        "No trays found for this lot in Day Planning.",
      );
    } else if (tvmTrayPendingCount(data) === 0) {
      tvmSetActivity(
        "success",
        "All trays already verified ✅  Ready for Input Screening",
      );
      // Enable action buttons when all verified
      isEnableActionButtons(lotId, true);
      // Disable Undo button when all trays verified
      var undoBtn = document.getElementById("tvm-undo-all-btn");
      if (undoBtn) {
        undoBtn.disabled = true;
        undoBtn.style.opacity = "0.5";
        undoBtn.style.cursor = "not-allowed";
        undoBtn.title = "All trays verified - cannot undo";
      }
    } else {
      tvmSetActivity(
        "wait",
        "Waiting for tray scan… (" + tvmTrayPendingCount(data) + " pending)",
      );
      // Enable Undo button when trays are pending
      var undoPendingBtn = document.getElementById("tvm-undo-all-btn");
      if (undoPendingBtn) {
        undoPendingBtn.disabled = false;
        undoPendingBtn.style.opacity = "1";
        undoPendingBtn.style.cursor = "pointer";
        undoPendingBtn.title = "Undo all verifications - mark all trays as Not Verified";
      }
    }
    return data;
  }

  // ─── Load trays from backend ───────────────────────────────────────────────
  function tvmLoadTrays(lotId) {
    const tbody = document.getElementById("tvm-tray-tbody");
    tbody.innerHTML =
      '<tr><td colspan="4" style="text-align:center;color:#bbb;padding:28px;font-size:13px;">Loading trays…</td></tr>';
    return tvmFetchTrayData(lotId).then(function (data) {
      return tvmApplyTrayData(lotId, data);
    });
  }
  // ─── Verify a tray scan ────────────────────────────────────────────────────
  function tvmVerifyScan(trayId) {
    const lotId = window._tvmCurrentLotId;
    if (!lotId || !trayId) return Promise.resolve(null);
    trayId = tvmFormatTrayId(trayId);
    if (tvmScanInFlight) return Promise.resolve(null);
    tvmScanInFlight = true;
    tvmPendingTrayId = trayId;
    document.dispatchEvent(new CustomEvent("inputScreening:globalScanVerification", {
      detail: { status: "verifying", tray_id: trayId, lot_id: lotId }
    }));
    tvmSetActivity("info", "Verifying: " + trayId + "…");
    return fetch("/inputscreening/verify_tray/", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-CSRFToken": tvmGetCookie("csrftoken"),
      },
      body: JSON.stringify({ lot_id: lotId, tray_id: trayId }),
    })
      .then(function (r) {
        return r.json();
      })
      .then(function (data) {
        const input = document.getElementById("tvm-scan-input");
        tvmScanInFlight = false;
        tvmPendingTrayId = "";
        if (data.success) {
          if (input) input.value = "";
          // ── success ──────────────────────────────────────────────────────
          tvmSetActivity(
            "success",
            data.message || "Tray Verified Successfully ✅",
          );
          tvmUpdateStats(
            data.verified,
            data.total,
            data.pending,
            data.verified_qty,
            data.total_qty,
          );
          const sid = tvmSafeId(trayId);
          const badge = document.getElementById("tvm-badge-" + sid);
          const row = document.getElementById("tvm-row-" + sid);
          if (badge) {
            badge.style.cssText = VERIFIED_BADGE_STYLE;
            badge.textContent = "Verified ✅";
          }
          if (row) {
            // FIX 5: Auto-scroll to row and apply highlight
            tvmScrollToTray(trayId, true);
            // REMOVED: Individual undo button injection - using common Undo All button instead
          }
          // Update local tray store so next scan of this tray auto-selects
          if (window._tvmTrays) {
            var tr = window._tvmTrays.find(function (t) {
              return t.tray_id === trayId;
            });
            if (tr) tr.is_verified = true;
          }
          // FIX 4: Update running info
          tvmSetRunningInfo("Scanned: " + trayId + " ✅");
          document.dispatchEvent(new CustomEvent("inputScreening:globalScanVerification", {
            detail: { status: "verified", tray_id: trayId, lot_id: lotId, all_verified: !!data.all_verified }
          }));
          if (data.all_verified) {
            // ── SSOT: enable action buttons from backend response ──────────
            isEnableActionButtons(window._tvmCurrentLotId, true);
            // ── Disable Undo button when all trays verified ────────────────
            var undoBtn = document.getElementById("tvm-undo-all-btn");
            if (undoBtn) {
              undoBtn.disabled = true;
              undoBtn.style.opacity = "0.5";
              undoBtn.style.cursor = "not-allowed";
              undoBtn.title = "All trays verified - cannot undo";
            }
            // ── Show auto-dismissing success alert ────────────────────────
            if (typeof Swal !== "undefined") {
              Swal.fire({
                icon: "success",
                title: "All trays are verified",
                text: "Refreshing the page before Accept / Reject.",
                timer: 2500,
                timerProgressBar: true,
                showConfirmButton: false,
                allowOutsideClick: false,
              }).then(function () { window.location.reload(); });
            } else {
              window.location.reload();
            }
            setTimeout(function () {
              tvmSetActivity(
                "success",
                "All trays verified ✅  Ready for Input Screening",
              );
              tvmSetRunningInfo("Status: All Complete ✅");
            }, 300);
          } else {
            setTimeout(function () {
              tvmSetActivity(
                "wait",
                "Waiting for next tray scan… (" + data.pending + " pending)",
              );
            }, 1600);
          }
        } else {
          // ── failure ───────────────────────────────────────────────────────
          const status = data.status || "error";
          if (status === "already_verified") {
            if (input) input.value = trayId;
            // Already verified in current lot - show warning, NOT invalid
            tvmSetActivity("info", data.message || "Already Verified ⚠�?");
            // Scroll to that tray and highlight it
            tvmScrollToTray(trayId, true);
            // Update running info
            tvmSetRunningInfo("Scanned: " + trayId + " (Already Verified)");
            document.dispatchEvent(new CustomEvent("inputScreening:globalScanVerification", {
              detail: { status: "already_verified", tray_id: trayId, lot_id: lotId }
            }));
            // Pick-table row may not have been repainted yet (e.g. modal
            // reopened without a full page reload) - sync it from backend now.
            if (data.all_verified) {
              isEnableActionButtons(window._tvmCurrentLotId, true);
            }
            // Auto-select input for already verified trays
            setTimeout(function () {
              tvmFocusScanInput(true);
            }, 500);
            setTimeout(function () {
              tvmSetActivity("wait", "Waiting for tray scan…");
            }, 2000);
          } else {
            if (input) input.value = trayId;
            // Tray belongs to another lot or not found - show as invalid
            tvmSetActivity("error", data.message || "Invalid Tray ID �?�");
            // Update running info with invalid scan
            tvmSetRunningInfo("Invalid: " + trayId + " �?�");
            document.dispatchEvent(new CustomEvent("inputScreening:globalScanVerification", {
              detail: { status: "not_in_lot", tray_id: trayId, lot_id: lotId }
            }));
            // Shake animation
            input.style.animation = "";
            void input.offsetWidth; // reflow to restart
            input.style.animation = "tvmShake 0.4s ease";
            setTimeout(function () {
              input.style.animation = "";
            }, 450);
            // Auto-select input on error so next scan replaces it
            setTimeout(function () {
              tvmFocusScanInput(true);
            }, 500);
            setTimeout(function () {
              tvmSetActivity("wait", "Waiting for tray scan…");
            }, 2800);
          }
        }
        tvmFocusScanInput(false);
      })
      .catch(function () {
        tvmScanInFlight = false;
        tvmPendingTrayId = "";
        tvmSetActivity("error", "Network error �?�");
        document.dispatchEvent(new CustomEvent("inputScreening:globalScanVerification", {
          detail: { status: "not_in_lot", tray_id: trayId, lot_id: lotId }
        }));
        const input = document.getElementById("tvm-scan-input");
        if (input) input.value = trayId;
        tvmFocusScanInput(true);
        setTimeout(function () {
          tvmSetActivity("wait", "Waiting for tray scan…");
        }, 2200);
      });
  }
  // ─── Open / close ──────────────────────────────────────────────────────────
  function tvmOpen(lotId, batchId, preloadedTrayData) {
    window._tvmCurrentLotId = lotId;
    window._tvmCurrentBatchId = batchId;
    tvmUpdateStats(0, 0, 0, 0, 0);
    tvmSetActivity("wait", "Waiting for tray scan…");
    tvmSetRunningInfo("Waiting for scan\u2026");
    // Reset Save Draft button state (may have been disabled by a previous draft save)
    var draftBtn = document.getElementById("tvm-save-draft-btn");
    if (draftBtn) { draftBtn.disabled = false; draftBtn.innerHTML = "\uD83D\uDCBE Save Draft"; }
    // Reset Undo button state (will be updated by tvmLoadTrays based on verification status)
    var undoBtn = document.getElementById("tvm-undo-all-btn");
    if (undoBtn) {
      undoBtn.disabled = false;
      undoBtn.style.opacity = "1";
      undoBtn.style.cursor = "pointer";
      undoBtn.title = "Undo all verifications - mark all trays as Not Verified";
    }
    // Reset success banner on modal open
    var banner = document.getElementById("tvm-success-banner");
    if (banner) banner.style.display = "none";
    const modal = document.getElementById("trayVerificationModal");
    modal.style.display = "flex";
    const loadPromise = preloadedTrayData
      ? Promise.resolve(tvmApplyTrayData(lotId, preloadedTrayData))
      : tvmLoadTrays(lotId);
    // ERR 2: Auto focus cursor on modal open
    setTimeout(function () {
      const input = document.getElementById("tvm-scan-input");
      if (input) {
        input.focus();
        input.value = "";
        input.setSelectionRange(0, 0);
      }
    }, 120);
    return loadPromise;
  }
  function tvmClose() {
    document.getElementById("trayVerificationModal").style.display = "none";
    window._tvmCurrentLotId = null;
    window._tvmCurrentBatchId = null;
    // Clear active row highlight and restore original row position
    if (typeof window.restoreRowPosition === "function") {
      window.restoreRowPosition();
    }
    // Clear session storage to prevent highlight on page refresh
    if (window.GlobalShortcutManager && typeof window.GlobalShortcutManager.clear === "function") {
      window.GlobalShortcutManager.clear();
    } else if (typeof window._gkbClearPending === "function") {
      window._gkbClearPending();
    }
  }
  // Expose globally so keyboard shortcut handler can close the modal via Esc
  window.tvmClose = tvmClose;
  window.openInputScreeningTrayVerificationFromGlobalScan = function (payload) {
    payload = payload || {};
    var row = payload.row || null;
    var trigger = payload.trigger || (row ? row.querySelector(".tray-scan-btn-DayPlanning-view") : null);
    var trayId = tvmFormatTrayId(payload.tray_id || "");
    var lotId = payload.lot_id || (trigger && trigger.getAttribute("data-stock-lot-id")) || (row && row.getAttribute("data-stock-lot-id"));
    var batchId = payload.batch_id || (trigger && trigger.getAttribute("data-batch-id")) || (row && row.getAttribute("data-batch-id"));

    if (!trayId || !lotId) return false;

    tvmFetchTrayData(lotId).then(function (trayData) {
      if (!trayData || !trayData.success) {
        document.dispatchEvent(new CustomEvent("inputScreening:globalScanVerification", {
          detail: { status: "not_in_lot", tray_id: trayId, lot_id: lotId }
        }));
        return;
      }

      var matchedTray = tvmFindTrayInData(trayData, trayId);
      var pendingCount = tvmTrayPendingCount(trayData);
      var totalCount = tvmTrayTotalCount(trayData);

      if (!matchedTray) {
        document.dispatchEvent(new CustomEvent("inputScreening:globalScanVerification", {
          detail: { status: "not_in_lot", tray_id: trayId, lot_id: lotId }
        }));
        return;
      }

      if (totalCount > 0 && pendingCount === 0) {
        isEnableActionButtons(lotId, true);
        if (row) row.scrollIntoView({ block: "center", behavior: "smooth" });
        document.dispatchEvent(new CustomEvent("inputScreening:globalScanVerification", {
          detail: { status: "already_verified", tray_id: trayId, lot_id: lotId, all_verified: true }
        }));
        return;
      }

      tvmSetActivity("info", "Opening scanned tray " + trayId + "...");
      tvmSetRunningInfo("Verification: " + trayId);

      Promise.resolve(tvmOpen(lotId, batchId || "", trayData))
        .then(function (loadedData) {
          var matchedTrayAfterOpen = tvmFindTrayInData(loadedData || trayData, trayId);

          if (!matchedTrayAfterOpen) {
            tvmSetActivity("error", "Scanned tray " + trayId + " is not in this lot.");
            tvmSetRunningInfo("Invalid: " + trayId);
            document.dispatchEvent(new CustomEvent("inputScreening:globalScanVerification", {
              detail: { status: "not_in_lot", tray_id: trayId, lot_id: lotId }
            }));
            return;
          }

          tvmScrollToTray(matchedTrayAfterOpen.tray_id, true);
          if (matchedTrayAfterOpen.is_verified) {
            tvmSetActivity("info", "Tray " + matchedTrayAfterOpen.tray_id + " is already verified. Continue pending trays.");
            tvmSetRunningInfo("Scanned: " + matchedTrayAfterOpen.tray_id + " (Already Verified)");
            document.dispatchEvent(new CustomEvent("inputScreening:globalScanVerification", {
              detail: { status: "already_verified", tray_id: matchedTrayAfterOpen.tray_id, lot_id: lotId, all_verified: false }
            }));
            return;
          }

          tvmVerifyScan(matchedTrayAfterOpen.tray_id);
        });
    });

    return true;
  };
  // ─── Event: Save Draft button ──────────────────────────────────────────────
  var tvmDraftBtn = document.getElementById("tvm-save-draft-btn");
  if (tvmDraftBtn) tvmDraftBtn.addEventListener("click", tvmSaveDraft);
  // ─── Event: view icon (delegated) ──────────────────────────────────────────
  document.addEventListener("click", function (e) {
    const viewBtn = e.target.closest(".tray-scan-btn-DayPlanning-view");
    if (viewBtn) {
      console.log("👁️ View icon clicked, opening tray verification modal");
      e.preventDefault();
      e.stopPropagation();
      const lotId = viewBtn.getAttribute("data-stock-lot-id");
      const batchId = viewBtn.getAttribute("data-batch-id");
      console.log("📦 Opening modal for Lot:", lotId, "Batch:", batchId);
      if (lotId && batchId) {
        tvmOpen(lotId, batchId);
      } else {
        console.error("❌ Missing lotId or batchId attributes on view button");
      }
    }
  });
  // ─── Event: close button ───────────────────────────────────────────────────
  const closeBtn = document.getElementById("closeTrayVerificationModal");
  if (closeBtn) closeBtn.addEventListener("click", tvmClose);
  // ─── Event: backdrop click ─────────────────────────────────────────────────
  document
    .getElementById("trayVerificationModal")
    .addEventListener("click", function (e) {
      if (e.target === this) tvmClose();
    });
  // ─── Event: ESC key ────────────────────────────────────────────────────────
  document.addEventListener("keydown", function (e) {
    const modal = document.getElementById("trayVerificationModal");
    if (!modal || modal.style.display === "none") return;
    if (document.querySelector(".swal2-container")) return;

    // All application shortcuts are disabled while the event originates from
    // an editable control. Scanner inputs keep their own Enter handler below.
    let shortcutTarget = e.target;
    if (shortcutTarget && shortcutTarget.nodeType === 3) shortcutTarget = shortcutTarget.parentElement;
    if (shortcutTarget && shortcutTarget.closest &&
        shortcutTarget.closest('input, textarea, select, [contenteditable]:not([contenteditable="false"])')) return;

    if (e.ctrlKey || e.metaKey || e.altKey) return;

    if (e.key === "Escape" || e.key === "c" || e.key === "C") {
      e.preventDefault();
      e.stopPropagation();
      if (typeof e.stopImmediatePropagation === "function") e.stopImmediatePropagation();
      tvmClose();
      return;
    }

    if (e.key === "d" || e.key === "D") {
      e.preventDefault();
      e.stopPropagation();
      if (typeof e.stopImmediatePropagation === "function") e.stopImmediatePropagation();
      tvmSaveDraft();
      return;
    }

    if (e.key === "o" || e.key === "O") {
      e.preventDefault();
      e.stopPropagation();
      if (typeof e.stopImmediatePropagation === "function") e.stopImmediatePropagation();
      tvmUndoAll();
    }
  }, true);
  // ─── Event: Verify All checkbox ────────────────────────────────────────────
  const tvmVerifyAllCb = document.getElementById("tvm-verify-all-cb");
  if (tvmVerifyAllCb) {
    tvmVerifyAllCb.addEventListener("change", function(e) {
      if (this.checked) {
        this.checked = false; // Uncheck immediately
        tvmVerifyAll(); // Show confirmation and proceed
      }
    });
  }
  // ─── Event: Undo All button ────────────────────────────────────────────────
  var tvmUndoAllBtn = document.getElementById("tvm-undo-all-btn");
  if (tvmUndoAllBtn) tvmUndoAllBtn.addEventListener("click", tvmUndoAll);
  // ─── Event: scan input — Enter & formatting ───────────────────────────────
  const scanInput = document.getElementById("tvm-scan-input");
  if (scanInput) {
    // ERR 6 + ERR 8: Focus handler - clear input and set cursor position
    scanInput.addEventListener("focus", function () {
      this.style.borderColor = "#028084";
      this.style.boxShadow = "0 0 0 4px rgba(2,128,132,.12)";
      if (this.value) {
        this.select();
      } else {
        this.setSelectionRange(0, 0);
      }
    });
    scanInput.addEventListener("blur", function () {
      this.style.borderColor = "#d0dfe1";
      this.style.boxShadow = "none";
    });
    // ERR 8: Auto-format + ERR 7: 9-char limit as user types
    scanInput.addEventListener("input", function () {
      let val = this.value.trim();
      if (val.length > 9) val = val.substring(0, 9);
      if (val.length > 0) {
        const formatted = tvmFormatTrayId(val);
        this.value = formatted;
        tvmSetActivity("info", "Typing: " + formatted);
        
        // BUG FIX 2: Auto-validate when 9 characters are entered
        if (formatted.length === 9) {
          tvmVerifyScan(formatted);
        }
      } else {
        tvmSetActivity("wait", "Waiting for tray scan…");
      }
    });
    // ERR 2: Enter key to verify
    scanInput.addEventListener("keydown", function (e) {
      if (e.key === "Enter") {
        e.preventDefault();
        e.stopPropagation();
        const val = this.value.trim();
        if (val) {
          const formatted = tvmFormatTrayId(val);
          tvmVerifyScan(formatted);
        } else {
          tvmFocusScanInput(false);
        }
      }
    });
  }
  // ─── Event: verify button click ───────────────────────────────────────────
  const verifyBtn = document.getElementById("tvm-scan-btn");
  if (verifyBtn) {
    verifyBtn.addEventListener("click", function () {
      const input = document.getElementById("tvm-scan-input");
      const val = input ? input.value.trim() : "";
      if (val) {
        const formatted = tvmFormatTrayId(val);
        tvmVerifyScan(formatted);
      }
      if (input)
        setTimeout(function () {
          input.focus();
        }, 60);
    });
  }
  // ─── Event: FIX 6 - Clear button click — revert ALL verifications ─────────
  const clearBtn = document.getElementById("tvm-clear-btn");
  if (clearBtn) {
    clearBtn.addEventListener("click", function () {
      const lotId = window._tvmCurrentLotId;
      if (!lotId) {
        // No lot loaded — just clear the scan input
        const input = document.getElementById("tvm-scan-input");
        if (input) { input.value = ""; input.focus(); input.setSelectionRange(0, 0); }
        return;
      }
      Swal.fire({
        title: "Clear All Verifications?",
        text: "This will reset all tray verifications for this lot. You will need to scan them again.",
        icon: "warning",
        showCancelButton: true,
        confirmButtonColor: "#d33",
        cancelButtonColor: "#3085d6",
        confirmButtonText: "Yes, Clear All!",
        cancelButtonText: "Cancel",
      }).then(function (result) {
        if (!result.isConfirmed) return;
        fetch("/inputscreening/clear_all_verifications/", {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "X-CSRFToken": tvmGetCookie("csrftoken"),
          },
          body: JSON.stringify({ lot_id: lotId }),
        })
          .then(function (r) { return r.json(); })
          .then(function (data) {
            if (data.success) {
              tvmSetActivity("wait", "All verifications cleared \u21ba Scan trays again\u2026");
              tvmSetRunningInfo("Status: Cleared \u21ba");
              isEnableActionButtons(lotId, false);
              tvmLoadTrays(lotId); // Reload the tray table to show all unverified
              const input = document.getElementById("tvm-scan-input");
              if (input) { input.value = ""; input.focus(); input.setSelectionRange(0, 0); }
            } else {
              tvmSetActivity("error", data.error || "Failed to clear verifications \u274c");
            }
          })
          .catch(function () { tvmSetActivity("error", "Network error \u274c"); });
      });
    });
  }
  // ─── Event: Accept button click ─────────────────────
  document.addEventListener("click", function (e) {
    if (!e.target.classList.contains("btn-accept-is")) return;
    e.preventDefault();
    const btn = e.target;
    const lotId = btn.getAttribute("data-stock-lot-id");
    const batchId = btn.getAttribute("data-batch-id");
    if (!lotId || !batchId) return;

    const restoreBtn = function () {
      btn.disabled = false;
      btn.style.opacity = "1";
    };

    const doSubmit = function () {
      btn.disabled = true;
      btn.style.opacity = "0.5";
      // ── mark S circle as scanning-WIP (half-green) ──────────────────
      if (typeof window.isMarkSCircleWip === "function") {
        window.isMarkSCircleWip(lotId);
      }
      fetch("/inputscreening/full_accept/", {
        method: "POST",
        credentials: "same-origin",
        headers: {
          "Content-Type": "application/json",
          "X-CSRFToken": tvmGetCookie("csrftoken"),
        },
        body: JSON.stringify({ lot_id: lotId, batch_id: batchId }),
      })
        .then(function (res) {
          return res.json().then(function (data) {
            return { ok: res.ok, data: data };
          });
        })
        .then(function (resp) {
          if (!resp.ok || !resp.data || !resp.data.success) {
            const err = (resp.data && resp.data.error) || "Failed to accept lot.";
            if (typeof Swal !== "undefined") {
              Swal.fire({ icon: "error", title: "Accept Failed", text: err });
            } else {
              window.alert(err);
            }
            restoreBtn();
            restoreRowPosition();
            return;
          }
          if (typeof Swal !== "undefined") {
            Swal.fire({
              icon: "success",
              title: "Lot Accepted",
              text: "The lot has moved to Brass QC.",
              timer: 1500,
              showConfirmButton: false,
            }).then(function () { window.location.reload(); });
          } else {
            window.location.reload();
          }
        })
        .catch(function () {
          if (typeof Swal !== "undefined") {
            Swal.fire({
              icon: "error",
              title: "Network Error",
              text: "Could not reach the server. Please retry.",
            });
          } else {
            window.alert("Network error.");
          }
          restoreBtn();
          restoreRowPosition();
        });
    };

    if (typeof Swal !== "undefined") {
      Swal.fire({
        icon: "warning",
        title: "Accept this lot?",
        text: "All trays have been verified. The lot will move to Brass QC.",
        showCancelButton: true,
        confirmButtonText: "Yes, Accept",
        cancelButtonText: "Cancel",
        focusCancel: true,
        reverseButtons: true,
        confirmButtonColor: "#1ba878",
        cancelButtonColor: "#888",
        didOpen: function(popup) {
          popup.addEventListener("keydown", function(ev) {
            if (ev.key !== "ArrowLeft" && ev.key !== "ArrowRight") return;
            ev.preventDefault();
            var focused = document.activeElement;
            var confirmBtn = popup.querySelector(".swal2-confirm");
            var cancelBtn = popup.querySelector(".swal2-cancel");
            if (focused === confirmBtn) cancelBtn.focus();
            else confirmBtn.focus();
          });
        },
      }).then(function (r) {
        if (r.isConfirmed) doSubmit();
        else restoreRowPosition();
      });
    } else if (window.confirm("Accept this lot? All trays have been verified.")) {
      doSubmit();
    } else {
      restoreRowPosition();
    }
  });
  // ─── Reject button click ── handled by inputscreening_reject_modal.js ──────

  // ─── Pick-table remark send button ──────────────────────────────────────
  document.addEventListener("click", function (e) {
    var btn = e.target.closest(".ip-remark-send-btn");
    if (!btn) return;
    e.preventDefault();
    var tooltip = btn.closest(".remark-tooltip");
    var textarea = tooltip ? tooltip.querySelector("textarea") : null;
    var remark = textarea ? textarea.value.trim() : "";
    var lotId = btn.getAttribute("data-lot-id");
    if (!remark) {
      if (typeof Swal !== "undefined") {
        Swal.fire({ icon: "warning", title: "Remark required", text: "Please type a remark before sending.", timer: 1500, showConfirmButton: false });
      }
      return;
    }
    btn.disabled = true;
    btn.innerHTML = '<i class="fa fa-spinner fa-spin"></i>';
    fetch("/inputscreening/save_ip_remark/", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-CSRFToken": getCookie("csrftoken") },
      body: JSON.stringify({ lot_id: lotId, remark: remark }),
    })
      .then(function (r) { return r.json(); })
      .then(function (data) {
        if (data.success) {
          if (typeof Swal !== "undefined") {
            Swal.fire({ icon: "success", title: "Remark Saved!", timer: 1200, showConfirmButton: false }).then(function () {
              location.reload();
            });
          } else {
            location.reload();
          }
        } else {
          btn.disabled = false;
          btn.innerHTML = '<i class="fa fa-send"></i>';
          if (typeof Swal !== "undefined") {
            Swal.fire({ icon: "error", title: "Error", text: data.error || "Failed to save remark.", timer: 2000, showConfirmButton: false });
          }
          restoreRowPosition();
        }
      })
      .catch(function () {
        btn.disabled = false;
        btn.innerHTML = '<i class="fa fa-send"></i>';
        if (typeof Swal !== "undefined") {
          Swal.fire({ icon: "error", title: "Network Error", text: "Could not save remark.", timer: 2000, showConfirmButton: false });
        }
        restoreRowPosition();
      });
  });
});

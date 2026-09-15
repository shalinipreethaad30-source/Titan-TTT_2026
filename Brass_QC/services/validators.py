"""
Brass QC Validators â€” input validation only.

All submission and tray scan validation lives here.
Returns (is_valid: bool, error_str: str | None).

Rule: No DB writes. No HTTP layer. Pure validation functions.
"""

import logging

from modelmasterapp.models import TrayId
from InputScreening.models import (
    IPTrayId,
    IP_Rejected_TrayScan,
    IS_AllocationTray,
    IS_PartialRejectLot,
)

logger = logging.getLogger(__name__)


def _norm_tray_id(tray_id):
    return (tray_id or "").strip().upper()


def _safe_int(value):
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def validate_accept_tray_current_lot(tray_id, active_trays):
    tid = _norm_tray_id(tray_id)
    active_ids = {
        _norm_tray_id(tray.get("tray_id"))
        for tray in active_trays
        if tray.get("tray_id")
    }
    if tid not in active_ids:
        return f"Accept tray '{tid}' must be one of this lot's current tray IDs"
    return None


def _iter_snapshot_reject_lots(tray_id):
    tid = _norm_tray_id(tray_id)
    base_qs = IS_PartialRejectLot.objects.exclude(trays_snapshot__isnull=True).only("trays_snapshot")
    try:
        return list(base_qs.filter(trays_snapshot__contains=[{"tray_id": tid}]))
    except Exception as exc:
        logger.debug(
            "[validators] JSON contains lookup unavailable for tray_id=%s: %s",
            tid,
            exc,
        )
        return list(base_qs)


def is_tray_released_for_reuse(tray_id):
    """Return True when master tray state says the tray is reusable."""
    tray = TrayId.objects.filter(tray_id=_norm_tray_id(tray_id)).first()
    if not tray:
        return False
    if tray.delink_tray and not tray.scanned:
        return True
    return bool(
        tray.new_tray
        and not tray.lot_id
        and not tray.batch_id_id
        and not tray.rejected_tray
        and not tray.scanned
    )


def _snapshot_has_actual_is_reject(tray_id):
    tid = _norm_tray_id(tray_id)
    for reject_lot in _iter_snapshot_reject_lots(tid):
        for tray in reject_lot.trays_snapshot or []:
            if _norm_tray_id(tray.get("tray_id")) != tid:
                continue
            qty = _safe_int(tray.get("qty"))
            has_reason = bool(tray.get("reason_id") or tray.get("reason_text"))
            if qty > 0 and has_reason and not bool(tray.get("is_delinked")):
                return True
    return False


def is_input_screening_delink_only_tray(tray_id):
    """True when IS history shows this tray only as a delink/release row."""
    tid = _norm_tray_id(tray_id)
    if IS_AllocationTray.objects.filter(
        tray_id=tid,
        reject_lot__isnull=False,
        is_delinked=True,
        qty__lte=0,
    ).exists():
        return True
    for reject_lot in _iter_snapshot_reject_lots(tid):
        for tray in reject_lot.trays_snapshot or []:
            if _norm_tray_id(tray.get("tray_id")) != tid:
                continue
            qty = _safe_int(tray.get("qty"))
            has_reason = bool(tray.get("reason_id") or tray.get("reason_text"))
            if qty <= 0 and not has_reason:
                return True
    return False


def is_tray_rejected_in_input_screening(tray_id):
    """Return True only for real IS rejects that have not been released."""
    tid = _norm_tray_id(tray_id)
    if is_tray_released_for_reuse(tid):
        return False

    has_actual_reject_allocation = IS_AllocationTray.objects.filter(
        tray_id=tid,
        reject_lot__isnull=False,
        qty__gt=0,
        is_delinked=False,
    ).exists()
    if has_actual_reject_allocation or IP_Rejected_TrayScan.objects.filter(rejected_tray_id=tid).exists():
        return True
    if _snapshot_has_actual_is_reject(tid):
        return True

    if IPTrayId.objects.filter(tray_id=tid, rejected_tray=True, delink_tray=False).exists():
        return not is_input_screening_delink_only_tray(tid)
    return False


def has_active_input_screening_reject_occupancy(tray_id, lot_id=None):
    """
    Return True while a physical tray still holds Input Screening rejected
    material. Historical rejects are allowed once master/release state marks the
    tray reusable.
    """
    tid = _norm_tray_id(tray_id)
    current_lot = str(lot_id or "").strip()
    if not tid or is_tray_released_for_reuse(tid):
        return False

    allocation_qs = IS_AllocationTray.objects.filter(
        tray_id=tid,
        reject_lot__isnull=False,
        qty__gt=0,
        is_delinked=False,
    )
    if current_lot:
        allocation_qs = allocation_qs.exclude(reject_lot__new_lot_id=current_lot)
    if allocation_qs.exists():
        return True

    rejected_scan_qs = IP_Rejected_TrayScan.objects.filter(rejected_tray_id=tid)
    if current_lot:
        rejected_scan_qs = rejected_scan_qs.exclude(lot_id=current_lot)
    if rejected_scan_qs.exists():
        return True

    ip_reject_qs = IPTrayId.objects.filter(
        tray_id=tid,
        rejected_tray=True,
        delink_tray=False,
    )
    if current_lot:
        ip_reject_qs = ip_reject_qs.exclude(lot_id=current_lot)
    if ip_reject_qs.exists() and not is_input_screening_delink_only_tray(tid):
        return True

    for reject_lot in _iter_snapshot_reject_lots(tid):
        if current_lot and reject_lot.new_lot_id == current_lot:
            continue
        for tray in reject_lot.trays_snapshot or []:
            if _norm_tray_id(tray.get("tray_id")) != tid:
                continue
            qty = _safe_int(tray.get("qty"))
            has_reason = bool(tray.get("reason_id") or tray.get("reason_text"))
            if qty > 0 and has_reason and not bool(tray.get("is_delinked")):
                return True

    return False


def _snapshot_has_tray(snapshot_data, tid):
    if not snapshot_data:
        return False
    for tray in snapshot_data.get("trays") or []:
        if _norm_tray_id(tray.get("tray_id")) == tid and _safe_int(tray.get("qty")) > 0:
            return True
    return False


def _is_brass_qc_reject_tray_explicitly_released(tray_id):
    """True only when a BQC reject-history tray was explicitly released."""
    tid = _norm_tray_id(tray_id)
    if not tid:
        return False

    tray = TrayId.objects.filter(tray_id__iexact=tid).first()
    return bool(tray and tray.delink_tray and not tray.scanned)


def _has_brass_qc_tray_level_rejection_evidence(tray_id):
    """Return True for tray-level BQC reject rows that still look active."""
    from ..models import Brass_QC_Rejected_TrayScan, BrassTrayId

    tid = _norm_tray_id(tray_id)
    if not tid:
        return False

    if Brass_QC_Rejected_TrayScan.objects.filter(
        rejected_tray_id__iexact=tid,
    ).exclude(
        rejected_tray_id__isnull=True,
    ).exclude(
        rejected_tray_id="",
    ).exists():
        return True

    return BrassTrayId.objects.filter(
        tray_id__iexact=tid,
        rejected_tray=True,
        delink_tray=False,
    ).exists()


def is_tray_rejected_in_brass_qc(tray_id):
    """
    Return True when this tray was rejected during Brass QC processing and has
    not since been released/delinked for reuse.

    Brass QC does not flag rejected trays on a live tray table (rejected trays
    are recorded only inside Brass_QC_Submission's reject snapshots and the
    BrassQC_PartialRejectLot snapshot), so those snapshots are the source of
    truth here â€” a downstream module like Brass Audit must check them before
    letting a scanned tray_id be accepted.
    """
    from ..models import Brass_QC_Submission, BrassQC_PartialRejectLot

    tid = _norm_tray_id(tray_id)
    if not tid:
        return False
    if _is_brass_qc_reject_tray_explicitly_released(tid):
        return False

    for submission in Brass_QC_Submission.objects.exclude(
        full_reject_data__isnull=True, partial_reject_data__isnull=True
    ).only("full_reject_data", "partial_reject_data"):
        if _snapshot_has_tray(submission.full_reject_data, tid):
            return True
        if _snapshot_has_tray(submission.partial_reject_data, tid):
            return True

    for reject_lot in BrassQC_PartialRejectLot.objects.exclude(
        trays_snapshot__isnull=True
    ).only("trays_snapshot"):
        for tray in reject_lot.trays_snapshot or []:
            if _norm_tray_id(tray.get("tray_id")) == tid and _safe_int(tray.get("qty")) > 0:
                return True

    if _has_brass_qc_tray_level_rejection_evidence(tid):
        return True

    return False


# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# Submission validators
# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

def validate_not_duplicate_submit(lot_id, is_iqf_reentry=False):
    """
    Returns (existing_submission | None, error_str | None).

    IQF reentry lots (send_brass_qc=True) are allowed to re-submit.
    For all other lots, duplicate submission is blocked.
    """
    from ..models import Brass_QC_Submission
    existing = Brass_QC_Submission.objects.filter(lot_id=lot_id, is_completed=True).first()
    if existing and not is_iqf_reentry:
        return existing, (
            f"This lot has already been submitted "
            f"(submission_id={existing.id}, type={existing.submission_type})"
        )
    return existing, None


def validate_full_reject_reasons(rejection_reasons, total_qty):
    """
    For FULL_REJECT: rejection reasons qty can be partial or zero.
    Backend auto-fills missing qty as "FULL LOT REJECTED".
    
    Returns error string or None.
    """
    if not rejection_reasons:
        return None  # âœ… Allow empty â€” backend will auto-fill
    
    total = sum(int(r.get("qty", 0)) for r in rejection_reasons)
    
    # âœ… FIX: Allow reasons to sum to <= total_qty (not must equal)
    # Missing qty will be auto-filled by backend
    if total > total_qty:
        return (
            f"Rejection reasons qty ({total}) exceeds total lot qty ({total_qty})"
        )
    
    return None


def validate_partial_reject_reasons(rejection_reasons, total_qty):
    """
    For PARTIAL: rejection reasons qty must be >0 and <total_qty.
    Returns (rejected_qty, error_str | None).
    """
    if not rejection_reasons:
        return 0, "Rejection reasons are required for partial reject"
    total = sum(int(r.get("qty", 0)) for r in rejection_reasons)
    if total <= 0:
        return 0, "Rejection qty must be greater than 0"
    if total >= total_qty:
        return 0, "Partial reject qty must be less than total lot qty"
    return total, None


def validate_process_tray_actions(tray_actions, active_trays, stock, lot_id):
    """
    For PROCESS action: validates tray_actions list.
    Returns (accepted_trays, rejected_trays, error_str | None).

    Also handles:
    - New reject trays not in this lot (validates against TrayId master)
    - IS-rejected tray blocking
    - Delink actions (writes BrassTrayId, TrayId delink flags â€” side effect only here)
    """
    from ..models import BrassTrayId
    if not tray_actions:
        return [], [], "tray_actions required for PROCESS action"

    active_tray_map = {
        _norm_tray_id(t.get("tray_id")): t
        for t in active_trays
        if t.get("tray_id")
    }
    accepted_trays = []
    rejected_trays = []

    for ta in tray_actions:
        tid = _norm_tray_id(ta.get("tray_id"))
        ta_action = ta.get("action")
        is_top = bool(ta.get("is_top", False))

        if not tid:
            return [], [], "Tray ID is required for every tray action"

        if ta_action not in ("ACCEPT", "REJECT", "DELINK"):
            return [], [], f"Invalid tray action '{ta_action}' for tray {tid}"

        tray_match = active_tray_map.get(tid)

        if not tray_match:
            if ta_action == "ACCEPT":
                return [], [], validate_accept_tray_current_lot(tid, active_trays)
            if ta_action == "REJECT":
                # New tray (not in this lot) scanned into a reject slot â€” validate master
                if not TrayId.objects.filter(tray_id=tid).exists():
                    return [], [], f"Reject tray '{tid}' not found in master tray list"

                # Brass QC-specific cross-module occupancy guard.  Keep the
                # existing shared validator unchanged because it is also consumed
                # by other modules; this wrapper adds Nickel Wiping/Audit coverage
                # only for Brass QC scan/submission paths.
                occupied_module, _occupied_error = validate_brass_qc_tray_occupancy(tid, lot_id)
                if occupied_module:
                    return [], [], "Tray is occupied."

                slot_qty = int(ta.get("qty") or 0)
                if slot_qty <= 0:
                    slot_qty = (stock.batch_id.tray_capacity if stock.batch_id else 0) or 0
                rejected_trays.append({"tray_id": tid, "qty": slot_qty, "is_top": False})
                logger.info(
                    f"[validators] New reject tray: lot_id={lot_id}, "
                    f"tray_id={tid}, qty={slot_qty}"
                )
                continue
            return [], [], f"Tray {tid} not found in lot"

        if ta_action == "ACCEPT":
            slot_qty = int(ta.get("qty") or tray_match["qty"])
            accepted_trays.append({"tray_id": tid, "qty": slot_qty, "is_top": is_top})

        elif ta_action == "REJECT":
            slot_qty = int(ta.get("qty") or tray_match["qty"])
            rejected_trays.append({"tray_id": tid, "qty": slot_qty, "is_top": is_top})

        elif ta_action == "DELINK":
            # Write delink flags â€” this is the only write in validators (necessary side effect)
            BrassTrayId.objects.filter(lot_id=lot_id, tray_id=tid).update(delink_tray=True)
            TrayId.objects.filter(lot_id=lot_id, tray_id=tid).update(delink_tray=True)

    # Validate exactly one top tray in accepted list
    if accepted_trays:
        top_count = sum(1 for t in accepted_trays if t["is_top"])
        if top_count != 1:
            return [], [], (
                f"Exactly one accepted tray must be marked as top (found {top_count})"
            )

    return accepted_trays, rejected_trays, None


# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# Tray scan validators
# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

def validate_tray_not_rejected_in_is(tray_id):
    """
    Returns error string if tray was rejected in Input Screening.
    Returns None if tray is eligible.

    Covers:
    - IPTrayId.rejected_tray flag (set by IS services)
    - IS_PartialRejectLot.trays_snapshot (historical rejections)
    """
    if is_tray_rejected_in_input_screening(tray_id):
        return (
            "Tray was rejected in Input Screening - permanently ineligible for reuse"
        )
    return None


def validate_tray_not_rejected_in_brass_qc(tray_id):
    """
    Returns error string if tray was rejected in Brass QC.
    Returns None if tray is eligible.

    Covers Brass_QC_Submission reject snapshots and BrassQC_PartialRejectLot
    snapshots â€” the only places Brass QC records a rejected tray_id.
    """
    if is_tray_rejected_in_brass_qc(tray_id):
        return (
            "Tray was rejected in Brass QC - permanently ineligible for reuse"
        )
    return None


def _submitted_jig_record_uses_lot(record, lot_id):
    for item in record.multi_model_allocation or []:
        if isinstance(item, dict) and str(item.get("lot_id") or "").strip() == lot_id:
            return True
    return False


def _is_jig_excess_lot_consumed(excess_lot_id):
    """Return True once the EX-* lot has been submitted again in Jig Loading."""
    from Jig_Loading.models import JigCompleted

    if not excess_lot_id:
        return False

    if JigCompleted.objects.filter(
        lot_id=excess_lot_id,
        draft_status="submitted",
    ).exists():
        return True

    submitted_multi_model_records = JigCompleted.objects.filter(
        draft_status="submitted",
        is_multi_model=True,
        multi_model_allocation__isnull=False,
    ).only("multi_model_allocation")
    for record in submitted_multi_model_records:
        if _submitted_jig_record_uses_lot(record, excess_lot_id):
            return True
    return False


def has_active_jig_loading_excess_occupancy(tray_id, lot_id=None):
    """
    Return True only while Jig Loading currently owns the physical tray as
    excess / half-filled stock. Historical ExcessLotTray rows do not block once
    the jig is released or the EX-* lot is consumed.
    """
    from django.db.models import Q
    from Jig_Loading.models import ExcessLotTray, Jig, JigCompleted

    tid = _norm_tray_id(tray_id)
    current_lot = str(lot_id or "").strip()
    if not tid:
        return False

    excess_trays = (
        ExcessLotTray.objects.filter(tray_id__iexact=tid)
        .select_related("excess_lot")
        .order_by("-created_at")
    )
    for excess_tray in excess_trays:
        excess_lot = excess_tray.excess_lot
        excess_lot_id = str(excess_lot.new_lot_id or "").strip()
        if current_lot and current_lot == excess_lot_id:
            continue

        active_jig = Jig.objects.filter(
            jig_qr_id=excess_lot.jig_id,
        ).filter(
            Q(occupied_flag=True) | Q(is_loaded=True)
        ).exists()
        if not active_jig:
            continue

        parent_still_has_excess = JigCompleted.objects.filter(
            lot_id=excess_lot.parent_lot_id,
            batch_id=excess_lot.parent_batch_id,
            jig_id=excess_lot.jig_id,
            draft_status="submitted",
            half_filled_tray_qty__gt=0,
        ).exists()
        if not parent_still_has_excess:
            continue

        if _is_jig_excess_lot_consumed(excess_lot_id):
            continue

        return True

    return False


def validate_tray_cross_module_occupancy(tray_id, lot_id):
    """
    Checks tray occupancy across IS, Brass QC, Brass Audit, and IQF modules.
    Returns (module_name, error_str) if occupied, or (None, None) if free.
    """
    from ..models import BrassTrayId
    from IQF.models import IQFTrayId, IQF_Accepted_TrayID_Store
    from BrassAudit.models import (
        BrassAuditTrayId,
        Brass_Audit_Draft_Store,
        Brass_Audit_Submission,
    )

    checks = [
        (
            IPTrayId.objects.filter(
                tray_id=tray_id, rejected_tray=False,
                delink_tray=False, lot_id__isnull=False,
            ).exclude(lot_id=lot_id),
            "Input Screening",
        ),
        (
            BrassTrayId.objects.filter(
                tray_id=tray_id,
                delink_tray=False, lot_id__isnull=False,
            ).exclude(lot_id=lot_id),
            "Brass QC",
        ),
        (
            BrassAuditTrayId.objects.filter(
                tray_id=tray_id,
                delink_tray=False, lot_id__isnull=False,
            ).exclude(lot_id=lot_id),
            "Brass Audit",
        ),
        (
            IQFTrayId.objects.filter(
                tray_id=tray_id,
                delink_tray=False, lot_id__isnull=False,
            ).exclude(lot_id=lot_id),
            "IQF",
        ),
    ]

    for qs, module_name in checks:
        if qs.exists():
            return module_name, f"Tray is currently occupied in {module_name}"

    # IQF PARTIAL accepted trays are moved to an accepted child lot and can be
    # removed from the live IQFTrayId occupancy path when the parent is consumed.
    # IQF_Accepted_TrayID_Store preserves that accepted physical-tray ownership.
    # Keep it blocking until the tray is explicitly delinked/released in TrayId.
    tid = _norm_tray_id(tray_id)
    if not is_tray_released_for_reuse(tid):
        iqf_accepted_qs = IQF_Accepted_TrayID_Store.objects.filter(
            tray_id__iexact=tid,
            is_save=True,
            is_draft=False,
        )
        if lot_id:
            iqf_accepted_qs = iqf_accepted_qs.exclude(lot_id=lot_id)
        if iqf_accepted_qs.exists():
            return "IQF", "Tray is currently occupied in IQF"

    if has_active_input_screening_reject_occupancy(tray_id, lot_id):
        return "Input Screening", "Tray is rejected in Input Screening"

    if has_active_jig_loading_excess_occupancy(tray_id, lot_id):
        return "Jig Loading", "Tray is currently occupied in Jig Loading"

    # Brass Audit drafts reserve tray IDs even before live BrassAuditTrayId rows
    # are created. Exclude the current lot so reopening/editing its own draft
    # does not block its already assigned trays.
    tid = _norm_tray_id(tray_id)
    draft_qs = Brass_Audit_Draft_Store.objects.filter(
        draft_type='rejection_draft'
    ).exclude(lot_id=lot_id).only('lot_id', 'draft_data')
    completed_draft_lot_ids = set(
        Brass_Audit_Submission.objects.filter(
            lot_id__in=draft_qs.values('lot_id'),
            is_completed=True,
        ).values_list('lot_id', flat=True)
    )

    for draft in draft_qs:
        if draft.lot_id in completed_draft_lot_ids:
            logger.info(
                "[BRA_AUDIT_DELINK] Ignoring completed Brass Audit draft "
                "reservation during occupancy check tray_id=%s draft_id=%s "
                "draft_lot_id=%s",
                tid,
                draft.id,
                draft.lot_id,
            )
            continue
        draft_data = draft.draft_data or {}
        if not isinstance(draft_data, dict):
            continue
        for slot_key in ('reject_slots', 'accept_slots', 'delink_slots'):
            for slot in draft_data.get(slot_key) or []:
                if not isinstance(slot, dict):
                    continue
                if _norm_tray_id(slot.get('tray_id')) == tid:
                    return (
                        "Brass Audit Draft",
                        "Tray is currently reserved in Brass Audit Draft",
                    )

    # Final ownership fallback: the master TrayId can still be assigned to a lot
    # even when that module's mirror row has already been cleaned up. Resolve the
    # owning lot's current stage so the operator sees WHERE the tray is occupied
    # instead of the generic "occupied in another lot" message.
    master_tray = TrayId.objects.filter(
        tray_id=tid, delink_tray=False, lot_id__isnull=False
    ).exclude(lot_id=lot_id).first()
    if master_tray:
        from modelmasterapp.models import TotalStockModel

        owner_stock = TotalStockModel.objects.filter(lot_id=master_tray.lot_id).first()
        if owner_stock:
            module_name = (
                getattr(owner_stock, 'current_stage', None)
                or getattr(owner_stock, 'next_process_module', None)
                or getattr(owner_stock, 'last_process_module', None)
            )
            if module_name:
                module_name = str(module_name).strip()
                return module_name, f"Tray is currently occupied in {module_name}"

        # If the owning lot no longer has a resolvable stage, keep a clear
        # occupancy message without exposing an unrelated lot ID.
        return "Assigned Lot", "Tray is currently occupied in an assigned lot"

    return None, None
def validate_brass_qc_tray_occupancy(tray_id, lot_id=None):
    """
    Brass QC-only occupancy wrapper used by tray scan and PROCESS submit.

    It deliberately leaves ``validate_tray_cross_module_occupancy`` unchanged
    because that shared helper is consumed by other modules.  This wrapper adds
    the missing Nickel Wiping / Nickel Audit checks while preserving each
    module's release-aware lifecycle and the current-lot exception.

    Returns (module_name, error_str) when blocked, otherwise (None, None).
    """
    tid = _norm_tray_id(tray_id)
    current_lot = str(lot_id or "").strip()
    if not tid:
        return None, None

    # Nickel Wiping Z1/Z2 share one backend lifecycle helper.  It only treats
    # historical reject snapshots as blocking while their owner lot is active,
    # so legitimately released/delinked trays remain reusable.
    try:
        from Nickel_Inspection.services import validate_nickel_wiping_rejection_tray_available

        nw_available, _nw_message = validate_nickel_wiping_rejection_tray_available(
            tid,
            current_lot_id=current_lot or None,
        )
        if not nw_available:
            return "Nickel Wiping", "Tray is occupied."
    except ImportError:
        logger.exception(
            "[Brass QC] Nickel Wiping tray validator import failed for tray_id=%s",
            tid,
        )

    # Nickel Audit Z1/Z2 share the same backend models.  Do NOT rely only on
    # Nickel_AuditTrayId here: after a partial rejection the live mirror/master
    # row can be cleared while the physical reject tray is still represented by
    # Nickel Audit reject scans/submission snapshots.  A reject snapshot remains
    # blocking until the tray is explicitly delinked/released in the master.
    try:
        from Nickel_Audit.models import (
            Nickel_AuditTrayId,
            Nickel_Audit_Rejected_TrayScan,
            NickelAudit_Submission,
            NickelAudit_PartialRejectLot,
        )

        master_tray = TrayId.objects.filter(tray_id__iexact=tid).first()
        explicitly_released = bool(
            master_tray
            and master_tray.delink_tray
            and not master_tray.scanned
        )

        if not explicitly_released:
            # Live Nickel Audit ownership (both zones use the same model).
            na_qs = Nickel_AuditTrayId.objects.filter(
                tray_id__iexact=tid,
                delink_tray=False,
                lot_id__isnull=False,
            )
            if na_qs.exists():
                return "Nickel Audit", "Tray is occupied."

            # Reject scan rows are the direct physical reject-tray record.
            if Nickel_Audit_Rejected_TrayScan.objects.filter(
                rejected_tray_id__iexact=tid,
            ).exists():
                return "Nickel Audit", "Tray is occupied."

            # Current submission snapshots are also checked because some Nickel
            # Audit paths persist reject tray identity there even when the mirror
            # row has already been cleaned up.
            for submission in NickelAudit_Submission.objects.filter(
                submission_type__in=["PARTIAL", "FULL_REJECT"],
            ).only("reject_trays_data"):
                for row in submission.reject_trays_data or []:
                    if not isinstance(row, dict):
                        continue
                    if (
                        _norm_tray_id(row.get("tray_id") or row.get("rejected_tray_id")) == tid
                        and _safe_int(row.get("qty") or row.get("tray_quantity")) > 0
                    ):
                        return "Nickel Audit", "Tray is occupied."

            # Partial-reject child snapshots provide the same protection for
            # split-lot records.
            for reject_lot in NickelAudit_PartialRejectLot.objects.exclude(
                trays_snapshot__isnull=True,
            ).only("trays_snapshot"):
                for row in reject_lot.trays_snapshot or []:
                    if not isinstance(row, dict):
                        continue
                    if (
                        _norm_tray_id(row.get("tray_id") or row.get("rejected_tray_id")) == tid
                        and _safe_int(row.get("qty") or row.get("tray_quantity")) > 0
                    ):
                        return "Nickel Audit", "Tray is occupied."
    except ImportError:
        logger.exception(
            "[Brass QC] Nickel Audit tray model import failed for tray_id=%s",
            tid,
        )

    # Existing shared validation remains the authority for Input Screening,
    # Brass QC, Brass Audit, IQF, Brass Audit drafts, Jig Loading excess and
    # master ownership.  We intentionally do not alter that helper's behavior.
    module_name, error = validate_tray_cross_module_occupancy(tid, current_lot)
    if module_name:
        return module_name, error or "Tray is occupied."

    return None, None

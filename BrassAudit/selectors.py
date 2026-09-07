"""
Brass Audit selectors.

Read-only queryset builders for the Brass Audit module.
"""

import logging

from django.db.models import Exists, F, OuterRef, Q

from modelmasterapp.models import TotalStockModel

from .models import (
    BrassAudit_PartialAcceptLot,
    BrassAudit_PartialRejectLot,
    Brass_Audit_Draft_Store,
    Brass_Audit_Rejection_ReasonStore,
    Brass_Audit_Rejection_Table,
    Brass_Audit_Submission,
    BrassAuditTrayId,
)
from Brass_QC.models import BrassTrayId


logger = logging.getLogger(__name__)


def get_picktable_base_queryset():
    """
    Return the same base queryset used by the Brass Audit pick table.

    Global scan must use this selector so tray scans resolve only to lots that
    are actually active and visible in Brass Audit.
    """
    has_draft_subquery = Exists(
        Brass_Audit_Draft_Store.objects.filter(lot_id=OuterRef('lot_id'))
    )
    draft_type_subquery = Brass_Audit_Draft_Store.objects.filter(
        lot_id=OuterRef('lot_id')
    ).values('draft_type')[:1]
    brass_rejection_qty_subquery = Brass_Audit_Rejection_ReasonStore.objects.filter(
        lot_id=OuterRef('lot_id')
    ).values('total_rejection_quantity')[:1]

    return TotalStockModel.objects.select_related(
        'batch_id',
        'batch_id__model_stock_no',
        'batch_id__version',
        'batch_id__location',
    ).filter(
        batch_id__total_batch_quantity__gt=0
    ).annotate(
        wiping_required=F('batch_id__model_stock_no__wiping_required'),
        has_draft=has_draft_subquery,
        draft_type=draft_type_subquery,
        brass_rejection_total_qty=brass_rejection_qty_subquery,
    ).filter(
        Q(brass_qc_accptance=True, brass_audit_accptance__isnull=True) |
        Q(brass_qc_accptance=True, brass_audit_accptance=False) |
        Q(brass_qc_few_cases_accptance=True, brass_onhold_picking=False) |
        Q(brass_audit_few_cases_accptance=True, brass_audit_onhold_picking=True)
    ).exclude(
        brass_audit_rejection=True
    ).exclude(
        Q(brass_audit_few_cases_accptance=True, brass_audit_onhold_picking=False)
    ).exclude(
        next_process_module='Split Completed'
    ).exclude(
        remove_lot=True
    )


def get_completed_submission(lot_id):
    """Return the latest completed Brass Audit submission for the parent lot."""
    return (
        Brass_Audit_Submission.objects
        .filter(lot_id=lot_id, is_completed=True)
        .order_by('-created_at')
        .first()
    )


def get_submission_by_child_lot(lot_id):
    """Return a completed Brass Audit submission where lot_id is a transition child."""
    return (
        Brass_Audit_Submission.objects
        .filter(
            Q(transition_accept_lot_id=lot_id) | Q(transition_reject_lot_id=lot_id),
            is_completed=True,
        )
        .order_by('-created_at')
        .first()
    )


def get_brass_audit_submitted_detail(lot_id):
    """
    Return read-only completed-history detail for the Brass Audit Completed eye modal.

    Uses persisted submission/partial-lot snapshots first. It avoids rebuilding
    accepted/rejected history from live tray occupancy because completed trays
    may be delinked, reused, or moved after Brass Audit completion.
    """
    from django.utils import timezone

    submission = get_completed_submission(lot_id)
    if not submission:
        submission = get_submission_by_child_lot(lot_id)

    if not submission:
        logger.warning("[BA][submitted_detail] No completed submission for lot=%s", lot_id)
        return {
            "success": False,
            "error": f"No completed Brass Audit record found for lot {lot_id}",
        }

    def _fmt_dt(dt):
        if not dt:
            return ""
        return timezone.localtime(dt).strftime("%B %d, %Y, %I:%M %p").lstrip("0")

    def _as_int(value, default=0):
        try:
            return int(value or default)
        except (TypeError, ValueError):
            return default

    def _snapshot_trays(snapshot):
        if not snapshot:
            return []
        if isinstance(snapshot, list):
            raw_trays = snapshot
        elif isinstance(snapshot, dict):
            raw_trays = snapshot.get("trays") or []
        else:
            raw_trays = []

        trays = []
        for tray in raw_trays:
            if not isinstance(tray, dict):
                continue
            item = {
                "tray_id": str(tray.get("tray_id") or "").strip().upper(),
                "qty": _as_int(tray.get("qty", tray.get("tray_quantity", 0))),
                "top_tray": bool(
                    tray.get("top_tray") or
                    tray.get("is_top") or
                    tray.get("is_top_tray")
                ),
            }
            if tray.get("source"):
                item["source"] = tray.get("source")
            if item["tray_id"]:
                trays.append(item)
        return trays

    def _normalize_reasons(reasons):
        if not reasons:
            return {}
        if isinstance(reasons, dict):
            return reasons
        if not isinstance(reasons, list):
            return {}

        reason_ids = [
            str(r.get("reason_id") or r.get("id") or "").strip()
            for r in reasons
            if isinstance(r, dict) and (r.get("reason_id") or r.get("id"))
        ]
        reason_lookup = {}
        if reason_ids:
            reason_lookup = {
                str(obj.id): obj
                for obj in Brass_Audit_Rejection_Table.objects.filter(id__in=reason_ids)
            }
            reason_lookup.update({
                str(obj.rejection_reason_id): obj
                for obj in Brass_Audit_Rejection_Table.objects.filter(
                    rejection_reason_id__in=reason_ids
                )
            })

        normalized = {}
        for index, reason in enumerate(reasons, start=1):
            if not isinstance(reason, dict):
                continue
            raw_id = str(reason.get("reason_id") or reason.get("id") or "").strip()
            reason_obj = reason_lookup.get(raw_id)
            key = (
                getattr(reason_obj, "rejection_reason_id", None) or
                raw_id or
                f"R{index:02d}"
            )
            label = (
                getattr(reason_obj, "rejection_reason", None) or
                reason.get("reason") or
                reason.get("reason_text") or
                key
            )
            normalized[key] = {
                "reason": label,
                "qty": _as_int(reason.get("qty", reason.get("quantity", 0))),
            }
        return normalized

    def _append_delink_rows(trays, delink_ids):
        existing = {t["tray_id"] for t in trays if t.get("tray_id")}
        for tray_id in delink_ids:
            tid = str(tray_id or "").strip().upper()
            if tid and tid not in existing:
                trays.append({
                    "tray_id": tid,
                    "qty": 0,
                    "top_tray": False,
                    "source": "delinked",
                })
                existing.add(tid)
        return trays

    stock = (
        TotalStockModel.objects
        .select_related("batch_id", "batch_id__model_stock_no", "model_stock_no")
        .filter(lot_id=submission.lot_id)
        .first()
    )
    batch = getattr(stock, "batch_id", None)
    plating_stock_no = ""
    if batch:
        plating_stock_no = getattr(batch, "plating_stk_no", "") or ""
    if not plating_stock_no and stock and getattr(stock, "model_stock_no", None):
        plating_stock_no = getattr(stock.model_stock_no, "plating_stk_no", "") or ""
    base_model_no = ""
    if stock and getattr(stock, "model_stock_no", None):
        base_model_no = getattr(stock.model_stock_no, "model_no", "") or ""
    if not base_model_no and batch and getattr(batch, "model_stock_no", None):
        base_model_no = getattr(batch.model_stock_no, "model_no", "") or ""
    display_model_no = plating_stock_no or base_model_no

    snapshot_data = submission.snapshot_data if isinstance(submission.snapshot_data, dict) else {}
    delinked_tray_ids = [
        str(tid or "").strip().upper()
        for tid in snapshot_data.get("delinked", [])
        if str(tid or "").strip()
    ]
    if not delinked_tray_ids:
        mirror_delinks = list(
            BrassAuditTrayId.objects
            .filter(lot_id=submission.lot_id, delink_tray=True)
            .values_list("tray_id", flat=True)
        )
        mirror_delinks.extend(
            BrassTrayId.objects
            .filter(lot_id=submission.lot_id, delink_tray=True)
            .values_list("tray_id", flat=True)
        )
        delinked_tray_ids = [
            str(tid or "").strip().upper()
            for tid in dict.fromkeys(mirror_delinks)
            if str(tid or "").strip()
        ]

    accept_lots = []
    accept_children = list(
        BrassAudit_PartialAcceptLot.objects
        .filter(parent_submission=submission)
        .order_by("created_at")
    )
    if accept_children:
        for child in accept_children:
            accept_lots.append({
                "new_lot_id": child.new_lot_id,
                "accepted_qty": child.accepted_qty,
                "accept_trays_count": child.accept_trays_count,
                "trays": _snapshot_trays(child.trays_snapshot),
                "created_at": _fmt_dt(child.created_at),
            })
    elif submission.accepted_qty > 0:
        accept_snapshot = submission.full_accept_data or submission.partial_accept_data or {}
        trays = _snapshot_trays(accept_snapshot or snapshot_data.get("accepted", []))
        accept_lots.append({
            "new_lot_id": submission.transition_accept_lot_id or submission.transition_lot_id or "",
            "accepted_qty": submission.accepted_qty,
            "accept_trays_count": len(trays),
            "trays": trays,
            "created_at": _fmt_dt(submission.created_at),
        })

    # Completed-history classification must remain transaction-specific.
    # Accepted trays can later be delinked/reused in live mirror tables, but that
    # current lifecycle state must not reclassify the historical BA acceptance.
    accepted_tray_ids = {
        str(tray.get("tray_id") or "").strip().upper()
        for lot in accept_lots
        for tray in (lot.get("trays") or [])
        if str(tray.get("tray_id") or "").strip()
    }
    if accepted_tray_ids and delinked_tray_ids:
        delinked_tray_ids = [
            tray_id
            for tray_id in delinked_tray_ids
            if tray_id not in accepted_tray_ids
        ]

    reject_lots = []
    reject_children = list(
        BrassAudit_PartialRejectLot.objects
        .filter(parent_submission=submission)
        .order_by("created_at")
    )
    if submission.submission_type == "PARTIAL" and reject_children:
        for child in reject_children:
            trays = _append_delink_rows(_snapshot_trays(child.trays_snapshot), delinked_tray_ids)
            reject_lots.append({
                "new_lot_id": child.new_lot_id,
                "rejected_qty": child.rejected_qty,
                "reject_trays_count": child.reject_trays_count,
                "rejection_reasons": _normalize_reasons(child.rejection_reasons),
                "trays": trays,
                "delinked_tray_ids": delinked_tray_ids,
                "delink_count": len(delinked_tray_ids),
                "remarks": child.remarks or "",
                "created_at": _fmt_dt(child.created_at),
            })
    elif (
        submission.submission_type in ("PARTIAL", "FULL_REJECT") and
        (submission.rejected_qty > 0 or delinked_tray_ids)
    ):
        reject_snapshot = submission.full_reject_data or submission.partial_reject_data or {}
        trays = _append_delink_rows(
            _snapshot_trays(reject_snapshot or snapshot_data.get("rejected", [])),
            delinked_tray_ids,
        )
        reject_lots.append({
            "new_lot_id": submission.transition_reject_lot_id or submission.transition_lot_id or "",
            "rejected_qty": submission.rejected_qty,
            "reject_trays_count": len([t for t in trays if _as_int(t.get("qty")) > 0]),
            "rejection_reasons": _normalize_reasons(snapshot_data.get("rejection_reasons")),
            "trays": trays,
            "delinked_tray_ids": delinked_tray_ids,
            "delink_count": len(delinked_tray_ids),
            "remarks": snapshot_data.get("remarks") or "",
            "created_at": _fmt_dt(submission.created_at),
        })

    return {
        "success": True,
        "lot_id": submission.lot_id,
        "requested_lot_id": lot_id,
        "model_no": display_model_no,
        "base_model_no": base_model_no,
        "plating_stock_no": plating_stock_no,
        "original_lot_qty": submission.total_lot_qty,
        "is_full_accept": submission.submission_type == "FULL_ACCEPT",
        "is_full_reject": submission.submission_type == "FULL_REJECT",
        "is_partial_accept": submission.submission_type == "PARTIAL" and bool(accept_lots),
        "is_partial_reject": submission.submission_type == "PARTIAL" and bool(reject_lots),
        "parent_created_at": _fmt_dt(submission.created_at),
        "accept_lots": accept_lots,
        "reject_lots": reject_lots,
    }
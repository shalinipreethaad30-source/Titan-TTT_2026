from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.renderers import TemplateHTMLRenderer
from django.shortcuts import render
from django.db import transaction
from django.db.models import OuterRef, Subquery, Exists, F
from django.core.paginator import Paginator
from django.templatetags.static import static
import math
from modelmasterapp.models import *
from DayPlanning.models import *
from InputScreening.models import *
from Brass_QC.models import *
from django.views.decorators.csrf import csrf_exempt
from django.utils.decorators import method_decorator
from django.contrib.auth.decorators import login_required
import traceback
from rest_framework import status
from django.http import JsonResponse
import json
from rest_framework.permissions import IsAuthenticated
from django.views.decorators.http import require_GET
from math import ceil
from django.db.models import Q
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from django.utils import timezone
from IQF.models import *
from BrassAudit.models import *
from Nickel_Audit.models import *
from Nickel_Inspection.models import *
from Jig_Unloading.models import *
from Jig_Unloading.tray_utils import get_upstream_tray_distribution, get_model_master_tray_info
from Nickel_Inspection.services import (
    build_nq_rejection_allocation,
    normalize_accept_trays,
    normalize_operator_delink_trays,
    normalize_reject_trays,
    release_tray_master_for_reuse,
    tray_qty_total,
    validate_original_tray_coverage,
    validate_nickel_wiping_rejection_tray_available,
)
import logging
logger = logging.getLogger(__name__)

def _sort_images_front_first_safe(images):
    """
    Sort model images with Front View first when the optional helper exists.
    Fall back to the original queryset/list order when it is not deployed.
    """
    try:
        from modelmasterapp.image_utils import sort_images_front_first
    except ImportError:
        logger.warning(
            "modelmasterapp.image_utils is unavailable; using default image order"
        )
        return images
    return sort_images_front_first(images)


from Inprocess_Inspection.models import InprocessInspectionTrayCapacity
from modelmasterapp.type_of_input import get_type_of_input_map


def _get_input_source(jig_unload_obj):
    """Return location names with fallback chain: M2M → TotalStockModel → TrayId → ModelMasterCreation."""
    names = [loc.location_name for loc in jig_unload_obj.location.all()]
    if not names:
        for raw_cid in (jig_unload_obj.combine_lot_ids or []):
            cid = raw_cid.rsplit('-', 1)[-1] if raw_cid and '-' in raw_cid else raw_cid
            if not cid:
                continue
            tsm = TotalStockModel.objects.filter(lot_id=cid).prefetch_related('location').select_related('batch_id__location').first()
            if tsm and tsm.location.exists():
                names = [loc.location_name for loc in tsm.location.all()]
                break
            if tsm and tsm.batch_id and tsm.batch_id.location:
                names = [tsm.batch_id.location.location_name]
                break
            tray = TrayId.objects.filter(lot_id=cid).select_related('batch_id__location').first()
            if tray and tray.batch_id and tray.batch_id.location:
                names = [tray.batch_id.location.location_name]
                break
    return ', '.join(names)


def _na_int(value):
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _na_tray_sort_key(tray_id):
    return str(tray_id or '').strip().upper()


def _na_tray_has_current_ownership(master_tray, current_lot_id=None):
    """
    True only when the authoritative master row still shows live ownership.
    Module tray rows are historical in completed/reworked flows and must not
    override a fully released master tray.
    """
    current_lot = str(current_lot_id or '').strip()
    owner_lot = str(getattr(master_tray, 'lot_id', '') or '').strip()

    if getattr(master_tray, 'rejected_tray', False) or getattr(master_tray, 'brass_rejected_tray', False):
        return True
    if owner_lot:
        return owner_lot != current_lot
    if getattr(master_tray, 'batch_id_id', None):
        return True
    if getattr(master_tray, 'scanned', False) and not getattr(master_tray, 'delink_tray', False):
        return True
    return False


def _na_validate_external_reject_tray_available(tray_id, current_lot_id=None):
    """
    Validate a Nickel Audit reject-tray scan against active ownership/rejection
    outside Nickel Audit.

    Zone 1 and Zone 2 both use ``na_action`` and the same models, so this one
    helper protects both zones while preserving current-lot and delink/reuse
    behavior.
    """
    tid = str(tray_id or '').strip().upper()
    current_lot = str(current_lot_id or '').strip()
    if not tid:
        return False, 'Tray ID required'

    from Brass_QC.services.validators import (
        validate_tray_cross_module_occupancy,
        validate_tray_not_rejected_in_brass_qc,
    )

    occupied_module, occupancy_error = validate_tray_cross_module_occupancy(
        tid,
        current_lot,
    )
    if occupancy_error:
        return False, 'Tray is occupied.'

    if validate_tray_not_rejected_in_brass_qc(tid):
        return False, 'Tray is occupied.'

    nw_available, _nw_message = validate_nickel_wiping_rejection_tray_available(
        tid,
        current_lot_id=None,
    )
    if not nw_available:
        return False, 'Tray is occupied.'

    return True, ''


def _na_normalize_active_trays(rows):
    clean_rows = []
    for row in rows or []:
        tray_id = row.get('tray_id') if isinstance(row, dict) else getattr(row, 'tray_id', '')
        qty = row.get('qty', row.get('tray_quantity', 0)) if isinstance(row, dict) else getattr(row, 'tray_quantity', 0)
        qty = _na_int(qty)
        if not tray_id or qty <= 0:
            continue
        clean_rows.append({'tray_id': tray_id, 'qty': qty, 'is_top': False})
    if not clean_rows:
        return []
    top_row = min(clean_rows, key=lambda item: (item['qty'], _na_tray_sort_key(item['tray_id'])))
    for item in clean_rows:
        item['is_top'] = item['tray_id'] == top_row['tray_id']
    return sorted(clean_rows, key=lambda item: (not item['is_top'], _na_tray_sort_key(item['tray_id'])))


def _na_normalize_tray_snapshot(rows, rejected=False):
    clean_rows = []
    for row in rows or []:
        tray_id = row.get('tray_id') if isinstance(row, dict) else getattr(row, 'tray_id', '')
        qty = row.get('qty', row.get('tray_quantity', 0)) if isinstance(row, dict) else getattr(row, 'tray_quantity', 0)
        qty = _na_int(qty)
        if not tray_id or qty <= 0:
            continue
        clean_rows.append({
            'tray_id': str(tray_id).strip().upper(),
            'tray_quantity': qty,
            'top_tray': bool(
                (row.get('is_top') or row.get('top_tray'))
                if isinstance(row, dict)
                else getattr(row, 'top_tray', False)
            ),
            'rejected_tray': bool(rejected),
            'delink_tray': False,
        })

    if not clean_rows:
        return []
    if rejected:
        if not any(item['top_tray'] for item in clean_rows):
            top_row = min(clean_rows, key=lambda item: (item['tray_quantity'], _na_tray_sort_key(item['tray_id'])))
            for item in clean_rows:
                item['top_tray'] = item['tray_id'] == top_row['tray_id']
        return sorted(clean_rows, key=lambda item: (not item['top_tray'], item['tray_quantity'], _na_tray_sort_key(item['tray_id'])))

    top_row = min(clean_rows, key=lambda item: (item['tray_quantity'], _na_tray_sort_key(item['tray_id'])))
    for item in clean_rows:
        item['top_tray'] = item['tray_id'] == top_row['tray_id']
    return sorted(clean_rows, key=lambda item: (not item['top_tray'], _na_tray_sort_key(item['tray_id'])))


def _na_delink_tray_snapshot(lot_id):
    rows = Nickel_AuditTrayId.objects.filter(
        lot_id=lot_id,
        delink_tray=True,
    ).order_by('tray_id').values('tray_id', 'tray_quantity', 'delink_tray_qty')
    trays = []
    for row in rows:
        tray_id = str(row.get('tray_id') or '').strip().upper()
        if not tray_id:
            continue
        trays.append({
            'tray_id': tray_id,
            'tray_quantity': _na_int(row.get('tray_quantity')),
            'delink_tray_qty': row.get('delink_tray_qty') or '',
            'top_tray': False,
            'rejected_tray': False,
            'delink_tray': True,
        })
    return trays


def _na_with_delink_tray_snapshot(lot_id, trays):
    combined = list(trays or [])
    existing_delink_ids = {
        str(row.get('tray_id') or '').strip().upper()
        for row in combined
        if row.get('delink_tray')
    }
    for row in _na_delink_tray_snapshot(lot_id):
        if row['tray_id'] not in existing_delink_ids:
            combined.append(row)
    return combined


def _na_normalize_delink_tray_snapshot(rows):
    clean_rows = []
    for row in rows or []:
        tray_id = row.get('tray_id') if isinstance(row, dict) else getattr(row, 'tray_id', '')
        qty = (
            row.get('qty', row.get('tray_quantity', row.get('delink_tray_qty', 0)))
            if isinstance(row, dict)
            else getattr(row, 'tray_quantity', 0)
        )
        qty = _na_int(qty)
        tray_id = str(tray_id or '').strip().upper()
        if not tray_id:
            continue
        clean_rows.append({
            'tray_id': tray_id,
            'tray_quantity': 0,
            'delink_tray_qty': qty if qty > 0 else '',
            'top_tray': False,
            'rejected_tray': False,
            'delink_tray': True,
        })
    return sorted(clean_rows, key=lambda item: _na_tray_sort_key(item['tray_id']))


def _na_completed_tray_snapshot(lot_id, submission_id=None):
    submission = None
    if str(submission_id or '').strip().isdigit():
        submission = NickelAudit_Submission.objects.filter(pk=submission_id).first()
    if submission is None:
        submission = NickelAudit_Submission.objects.filter(lot_id=lot_id).order_by('-created_at').first()
    if submission:
        trays = _na_normalize_tray_snapshot(submission.accept_trays_data or [])
        trays += _na_normalize_tray_snapshot(submission.reject_trays_data or [], rejected=True)
        delink_snapshot = getattr(submission, 'delink_trays_data', None)
        delink_trays = _na_normalize_delink_tray_snapshot(delink_snapshot or [])
        if delink_trays or str(submission_id or '').strip().isdigit():
            return trays + delink_trays
        return _na_with_delink_tray_snapshot(submission.lot_id or lot_id, trays)

    active_trays = Nickel_AuditTrayId.objects.filter(
        lot_id=lot_id,
        rejected_tray=False,
        delink_tray=False,
    ).values('tray_id', 'tray_quantity', 'top_tray')
    rejected_trays = Nickel_AuditTrayId.objects.filter(
        lot_id=lot_id,
        rejected_tray=True,
        delink_tray=False,
    ).values('tray_id', 'tray_quantity', 'top_tray')
    trays = _na_normalize_tray_snapshot(active_trays)
    trays += _na_normalize_tray_snapshot(rejected_trays, rejected=True)
    return _na_with_delink_tray_snapshot(lot_id, trays)


def _na_latest_submission_qtys(lot_id, accepted_fallback=0, rejected_fallback=0):
    submission = NickelAudit_Submission.objects.filter(lot_id=lot_id).order_by('-created_at').first()
    if submission:
        return submission.accepted_qty or 0, submission.rejected_qty or 0
    return _na_int(accepted_fallback), _na_int(rejected_fallback)


def _na_completed_lot_totals(lot_id, accepted_fallback=0, rejected_fallback=0):
    """Resolve accepted/rejected qty and tray count for a Completed-table row.

    Some accept paths (e.g. _na_do_submit_accept) never write a
    NickelAudit_Submission row, so `accepted_fallback`/`rejected_fallback`
    can be 0 even though real tray data exists. Falls back to the live
    Nickel_AuditTrayId snapshot (same source as the working
    pick_CompleteTable_tray_id_list API) so qty/tray count are always
    derived from actual completed-lot data instead of defaulting to 0.
    """
    accepted_qty, rejected_qty = _na_latest_submission_qtys(lot_id, accepted_fallback, rejected_fallback)
    trays = _na_completed_tray_snapshot(lot_id)
    no_of_trays = len(trays)
    if accepted_qty <= 0 and rejected_qty <= 0 and trays:
        accepted_qty = sum(_na_int(t.get('tray_quantity')) for t in trays if not t.get('rejected_tray'))
        rejected_qty = sum(_na_int(t.get('tray_quantity')) for t in trays if t.get('rejected_tray'))
    return accepted_qty, rejected_qty, no_of_trays


def _na_completed_row_priority(jig_unload_obj):
    if jig_unload_obj.na_qc_rejection:
        return 0
    if jig_unload_obj.na_qc_few_cases_accptance and not jig_unload_obj.na_onhold_picking:
        return 1
    return 2


def _na_unique_completed_rows(queryset, zone_label):
    selected_by_source = {}
    duplicate_count = 0
    for jig_unload_obj in queryset:
        source_key = tuple(_na_source_lot_ids(jig_unload_obj))
        current = selected_by_source.get(source_key)
        if current is None:
            selected_by_source[source_key] = jig_unload_obj
            continue
        duplicate_count += 1
        if _na_completed_row_priority(jig_unload_obj) < _na_completed_row_priority(current):
            selected_by_source[source_key] = jig_unload_obj

    rows = sorted(
        selected_by_source.values(),
        key=lambda row: (row.na_last_process_date_time or row.created_at, row.lot_id),
        reverse=True,
    )
    if duplicate_count:
        logger.info(
            "[AUDIT_COMPLETED_FILTER] zone=%s removed_duplicate_source_rows=%d output=%d",
            zone_label,
            duplicate_count,
            len(rows),
        )
    return rows


def _na_completed_event_rows(allowed_color_ids):
    """
    Build one row per Nickel Audit completion event (append-only
    NickelAudit_Submission history), for the Completed table listing.
    Serves Z1 (NACompletedView) and Z2 (NA_Zone_CompletedView).

    A lot's JigUnloadAfterTable row is reused across rework passes — a Full
    Reject routes the same row back to Nickel Wiping for rework (see
    _na_do_submit_full_reject) rather than creating a new row — so listing
    live JigUnloadAfterTable objects (one per lot) collapses multiple
    completed audit passes into a single row. NickelAudit_Submission is
    always inserted via .create() and never updated, so listing those
    instead preserves every completed pass as its own row.
    """
    import copy

    submissions = list(NickelAudit_Submission.objects.order_by('created_at'))
    if not submissions:
        return []

    child_lot_by_submission_id = {
        row['parent_submission_id']: row['new_lot_id']
        for row in NickelAudit_PartialAcceptLot.objects.values('parent_submission_id', 'new_lot_id')
        if row['parent_submission_id']
    }

    needed_lot_ids = set()
    for s in submissions:
        needed_lot_ids.add(s.lot_id)
        child_lot_id = child_lot_by_submission_id.get(s.id)
        if child_lot_id:
            needed_lot_ids.add(child_lot_id)

    juat_by_lot = {
        j.lot_id: j for j in JigUnloadAfterTable.objects
        .select_related('version', 'plating_color', 'polish_finish')
        .prefetch_related('location')
        .filter(lot_id__in=needed_lot_ids, plating_color_id__in=allowed_color_ids)
    }

    kind_map = {'FULL_ACCEPT': 'full_accept', 'FULL_REJECT': 'full_reject', 'PARTIAL': 'partial'}

    events = []
    for s in submissions:
        display_lot_id = child_lot_by_submission_id.get(s.id) or s.lot_id
        base = juat_by_lot.get(display_lot_id) or juat_by_lot.get(s.lot_id)
        if not base:
            continue
        kind = kind_map.get(s.submission_type, 'full_accept')
        row = copy.copy(base)
        row.lot_id = display_lot_id
        row.total_case_qty = s.total_lot_qty or 0
        row.na_qc_accepted_qty = s.accepted_qty or 0
        row.na_last_process_date_time = s.created_at
        row.na_qc_accptance = kind == 'full_accept'
        row.na_qc_rejection = kind == 'full_reject'
        row.na_qc_few_cases_accptance = kind == 'partial'
        row._na_rejected_qty = s.rejected_qty or 0
        row._na_no_of_trays = len(s.accept_trays_data or []) + len(s.reject_trays_data or [])
        row._na_submission_id = s.id
        events.append(row)

    events.sort(key=lambda r: r.na_last_process_date_time or timezone.now(), reverse=True)
    return events


def _na_upsert_accepted_tray_store(lot_id, tray_id, qty, user):
    tid = str(tray_id or '').strip()
    tray_qty = _na_int(qty)
    if not tid or tray_qty <= 0:
        return None
    return Nickel_Audit_Accepted_TrayID_Store.objects.update_or_create(
        tray_id=tid,
        defaults={
            'lot_id': lot_id,
            'tray_qty': tray_qty,
            'user': user,
            'is_save': True,
            'is_draft': False,
        },
    )


def _na_list(value):
    return value if isinstance(value, list) else []


def _na_bool(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {'1', 'true', 'yes', 'on'}
    return bool(value)


def _na_draft_zone_label(request):
    return 'Zone 2' if 'zone_two' in (request.path or '').lower() else 'Zone 1'


def _na_build_draft_snapshot(raw_draft_data, juat, request):
    draft_data = dict(raw_draft_data) if isinstance(raw_draft_data, dict) else {}
    total_qty = _na_int(draft_data.get('total_lot_qty', draft_data.get('total_qty', juat.nq_qc_accepted_qty or juat.total_case_qty or 0)))
    rejected_qty = _na_int(draft_data.get('rejected_qty', 0))
    accepted_qty = _na_int(draft_data.get('accepted_qty', max(total_qty - rejected_qty, 0)))
    reason_qtys = _na_list(draft_data.get('reason_qtys'))
    reject_trays = _na_list(draft_data.get('reject_trays'))
    accept_trays = _na_list(draft_data.get('accept_trays'))
    delink_trays = _na_list(draft_data.get('delink_trays'))
    original_trays = _na_list(draft_data.get('original_trays'))
    full_lot_rejection = _na_bool(draft_data.get('full_lot_rejection'))

    if full_lot_rejection:
        rejected_qty = total_qty
        accepted_qty = 0
        reject_trays = []
        accept_trays = []
        delink_trays = []

    draft_data.update({
        'isDraft': True,
        'is_draft': True,
        'status': 'Draft',
        'module': 'Nickel Audit',
        'zone': draft_data.get('zone') or _na_draft_zone_label(request),
        'lot_id': juat.lot_id,
        'batch_id': juat.unload_lot_id or juat.lot_id,
        'plating_stk_no': draft_data.get('plating_stk_no') or juat.plating_stk_no or '',
        'total_lot_qty': total_qty,
        'total_qty': total_qty,
        'rejected_qty': rejected_qty,
        'accepted_qty': accepted_qty,
        'full_lot_rejection': full_lot_rejection,
        'remaining_qty': max(total_qty - rejected_qty, 0),
        'reason_qtys': reason_qtys,
        'reject_trays': reject_trays,
        'accept_trays': accept_trays,
        'delink_trays': delink_trays,
        'original_trays': original_trays,
        'reject_slots': _na_list(draft_data.get('reject_slots')),
        'accept_slots': _na_list(draft_data.get('accept_slots')),
        'delink_slots': _na_list(draft_data.get('delink_slots')),
        'accept_auto_trays': _na_list(draft_data.get('accept_auto_trays')),
        'auto_delink_tray_ids': _na_list(draft_data.get('auto_delink_tray_ids')),
        'tray_counts': {
            'original': len(original_trays),
            'reject': len(reject_trays),
            'accept': len(accept_trays),
            'delink': len(delink_trays),
        },
    })
    draft_data.setdefault('rejection_reasons', reason_qtys)
    return draft_data


def _na_clear_draft_state(lot_id):
    Nickel_Audit_Draft_Store.objects.filter(lot_id=lot_id, draft_type='batch_rejection').delete()


def _na_normalize_source_lot_id(raw_lot_id):
    lot_id = str(raw_lot_id or '').strip()
    if '-' in lot_id:
        return lot_id.rsplit('-', 1)[-1]
    return lot_id


def _na_source_lot_ids(jig_unload_obj):
    source_lots = []
    for raw_lot_id in jig_unload_obj.combine_lot_ids or []:
        lot_id = _na_normalize_source_lot_id(raw_lot_id)
        if lot_id:
            source_lots.append(lot_id)
    return source_lots or [jig_unload_obj.lot_id]


def _na_fresh_nw_reacceptance_q():
    """
    Q condition: Nickel Wiping has genuinely processed this lot for a new
    cycle since Audit's last rejection — evidenced by nq_last_process_date_time
    (refreshed on every NW accept/reject submission) being newer than
    na_last_process_date_time (set when Audit rejected it), together with either
    a full NW acceptance or a partial NW acceptance/rejection split.

    A partial split keeps the parent row as the historical source and creates a
    new accepted child lot. Treating the newer partial state as a fresh NW cycle
    prevents the parent's stale na_qc_rejection=True from blacklisting that
    accepted child in _na_completed_source_lot_ids(); _na_partial_accept_child_maps()
    still closes the parent so only the accepted child can reach the Audit pick
    table.

    current_stage='Nickel Wiping' was previously used as this signal, but
    na_toggle_verified (the "Lot Qty verified" checkbox) advances
    current_stage to 'Nickel Audit' as soon as the operator ticks it — before
    they've actually submitted an Accept/Reject for this new cycle — which
    made a re-entered lot vanish from its own pick table the moment it was
    ticked. The timestamp comparison isn't touched by that action, so it
    stays valid for the lot's whole time in the pick table.
    """
    return (
        Q(nq_qc_accptance=True)
        | Q(nq_qc_few_cases_accptance=True, nq_onhold_picking=False)
    ) & Q(nq_last_process_date_time__gt=F('na_last_process_date_time'))

def _na_effective_qc_rejection(jig_unload_obj):
    """
    na_qc_rejection stays True permanently once a lot is rejected —
    _na_do_submit_full_reject routes the same row back to Nickel Wiping for
    rework without clearing it (it's NA's own flag, not NW's to reset). Once
    Nickel Wiping has re-accepted the lot for a fresh audit cycle, the stale
    flag no longer reflects this cycle's status. Mirrors the pick-table
    queryset carve-out in NA_PickTableView.get() so the Action column isn't
    disabled for a legitimately re-entered lot.
    """
    return bool(
        jig_unload_obj.na_qc_rejection
        and not (
            jig_unload_obj.nq_qc_accptance
            and jig_unload_obj.nq_last_process_date_time
            and jig_unload_obj.na_last_process_date_time
            and jig_unload_obj.nq_last_process_date_time > jig_unload_obj.na_last_process_date_time
        )
    )


def _na_completed_filter_q():
    return (
        Q(na_qc_accptance=True)
        | Q(na_qc_rejection=True)
        | Q(na_qc_few_cases_accptance=True, na_onhold_picking=False)
    )


def _na_completed_source_lot_ids(allowed_color_ids):
    completed_sources = {}
    completed_rows = (
        JigUnloadAfterTable.objects.filter(
            total_case_qty__gt=0,
            plating_color_id__in=allowed_color_ids,
        )
        .filter(_na_completed_filter_q())
        # A Full Reject from Audit routes the lot back to Nickel Wiping for
        # rework (see _na_do_submit_full_reject) while leaving na_qc_rejection
        # permanently True as a historical marker. Without this exclusion that
        # stale flag keeps the lot's original source id blacklisted here
        # forever, so any later Nickel Wiping resubmission for the same source
        # (fresh partial-accept child) is wrongly hidden from the Audit pick
        # table as a "duplicate" of an already-completed source.
        .exclude(_na_fresh_nw_reacceptance_q())
        .only('lot_id', 'combine_lot_ids', 'current_stage', 'na_last_process_date_time', 'created_at')
    )
    for completed_row in completed_rows:
        completed_at = completed_row.na_last_process_date_time or completed_row.created_at
        for source_lot_id in _na_source_lot_ids(completed_row):
            previous_completed_at = completed_sources.get(source_lot_id)
            if not previous_completed_at or (
                completed_at and completed_at > previous_completed_at
            ):
                completed_sources[source_lot_id] = completed_at
    return completed_sources


def _na_source_completed_for_current_cycle(source_lots, completed_source_lots, nq_last_process_date_time):
    for source_lot_id in source_lots:
        completed_at = completed_source_lots.get(source_lot_id)
        if not completed_at:
            continue
        if not nq_last_process_date_time or completed_at >= nq_last_process_date_time:
            return True
    return False


def _na_partial_accept_child_maps(rows):
    lot_ids = [str(row.lot_id or '').strip() for row in rows if getattr(row, 'lot_id', None)]
    visible_lot_ids = set(lot_ids)
    if not visible_lot_ids:
        return set(), {}

    partial_rows = NickelQC_PartialAcceptLot.objects.filter(
        Q(parent_lot_id__in=visible_lot_ids) | Q(new_lot_id__in=visible_lot_ids)
    ).values('parent_lot_id', 'new_lot_id', 'accepted_qty')

    closed_parent_lots = set()
    child_meta_by_lot = {}
    for partial_row in partial_rows:
        parent_lot_id = str(partial_row.get('parent_lot_id') or '').strip()
        child_lot_id = str(partial_row.get('new_lot_id') or '').strip()
        if parent_lot_id and parent_lot_id in visible_lot_ids:
            # This row already performed its partial split — the accepted
            # continuation lives in the child (new_lot_id), so the parent is
            # permanently closed. It must stay excluded even when the child
            # (or a further descendant, in a multi-generation re-entry chain
            # such as Wiping -> Audit full-reject -> back to Wiping -> Wiping
            # partial-accept again) is not itself visible in this queryset —
            # otherwise the closed parent wrongly reappears as a fresh
            # candidate and "reserves" the source lot ahead of the real child.
            closed_parent_lots.add(parent_lot_id)
        if child_lot_id and child_lot_id in visible_lot_ids:
            child_meta_by_lot[child_lot_id] = partial_row
    return closed_parent_lots, child_meta_by_lot


def _na_active_pick_rows(queryset, completed_source_lots, zone_label):
    rows = list(queryset)
    partial_parent_lots, partial_child_meta = _na_partial_accept_child_maps(rows)
    active_rows = []
    seen_source_lots = set()
    input_count = 0
    direct_submission_excluded = 0
    partial_parent_excluded = 0
    completed_source_excluded = 0
    duplicate_source_excluded = 0

    for jig_unload_obj in rows:
        input_count += 1
        lot_id = str(jig_unload_obj.lot_id or '').strip()
        if lot_id in partial_parent_lots and lot_id not in partial_child_meta:
            partial_parent_excluded += 1
            logger.info(
                "[AUDIT_PICKTABLE_FILTER] zone=%s exclude lot=%s reason=partial_parent_child_visible",
                zone_label,
                lot_id,
            )
            continue

        child_meta = partial_child_meta.get(lot_id)
        if child_meta:
            jig_unload_obj.nq_partial_parent_lot_id = child_meta.get('parent_lot_id') or ''
            jig_unload_obj.nq_partial_accept_qty = child_meta.get('accepted_qty') or jig_unload_obj.total_case_qty

        source_lots = _na_source_lot_ids(jig_unload_obj)
        if getattr(jig_unload_obj, 'has_submission', False):
            direct_submission_excluded += 1
            logger.info(
                "[AUDIT_PICKTABLE_FILTER] zone=%s exclude lot=%s sources=%s reason=direct_submission",
                zone_label,
                jig_unload_obj.lot_id,
                source_lots,
            )
            continue
        if _na_source_completed_for_current_cycle(
            source_lots,
            completed_source_lots,
            jig_unload_obj.nq_last_process_date_time,
        ):
            completed_source_excluded += 1
            logger.info(
                "[AUDIT_PICKTABLE_FILTER] zone=%s exclude lot=%s sources=%s reason=completed_source",
                zone_label,
                jig_unload_obj.lot_id,
                source_lots,
            )
            continue
        if any(lot_id in seen_source_lots for lot_id in source_lots):
            duplicate_source_excluded += 1
            logger.info(
                "[AUDIT_PICKTABLE_FILTER] zone=%s exclude lot=%s sources=%s reason=duplicate_source",
                zone_label,
                jig_unload_obj.lot_id,
                source_lots,
            )
            continue
        active_rows.append(jig_unload_obj)
        seen_source_lots.update(source_lots)

    logger.info(
        "[AUDIT_PICKTABLE_FILTER] zone=%s input=%d output=%d direct_submission_excluded=%d partial_parent_excluded=%d completed_source_excluded=%d duplicate_source_excluded=%d completed_sources=%d",
        zone_label,
        input_count,
        len(active_rows),
        direct_submission_excluded,
        partial_parent_excluded,
        completed_source_excluded,
        duplicate_source_excluded,
        len(completed_source_lots),
    )
    return active_rows


class NA_PickTableView(APIView):
    renderer_classes = [TemplateHTMLRenderer]
    template_name = 'Nickel_Audit/NickelAudit_PickTable.html'

    def get(self, request):
        user = request.user
        is_admin = user.groups.filter(name='Admin').exists() if user.is_authenticated else False

        nq_rejection_reasons = Nickel_Audit_Rejection_Table.objects.all()

        # Get all plating_color IDs where jig_unload_zone_1 is True
        allowed_color_ids = Plating_Color.objects.filter(
            jig_unload_zone_1=True
        ).values_list('id', flat=True)

        # ✅ CHANGED: Query JigUnloadAfterTable instead of TotalStockModel with zone filtering
        queryset = JigUnloadAfterTable.objects.select_related(
            'version',
            'plating_color',
            'polish_finish',
            'na_hold_by',
            'na_release_by'
        ).prefetch_related(
            'location'  # ManyToManyField requires prefetch_related
        ).filter(
            total_case_qty__gt=0,  # Only show records with quantity > 0
            plating_color_id__in=allowed_color_ids  # Only show records for zone 1
        )

        # ✅ Add draft status subqueries for Nickel QC
        has_draft_subquery = Exists(
            Nickel_Audit_Draft_Store.objects.filter(
                lot_id=OuterRef('lot_id')  # Using the auto-generated lot_id
            )
        )
        
        draft_type_subquery = Nickel_Audit_Draft_Store.objects.filter(
            lot_id=OuterRef('lot_id')
        ).values('draft_type')[:1]

        # Scoped to rejection-reason records created after this lot's most
        # recent Nickel Wiping (re-)acceptance — a lot rejected in a prior
        # audit cycle and routed back to Nickel Wiping under the same
        # lot_id must show Reject Qty = 0 on its new cycle, not the stale
        # rejection quantity from before it was re-accepted.
        brass_rejection_qty_subquery = Nickel_Audit_Rejection_ReasonStore.objects.filter(
            lot_id=OuterRef('lot_id'),
            created_at__gt=OuterRef('nq_last_process_date_time'),
        ).values('total_rejection_quantity')[:1]

        # Scoped to submissions created after this lot's most recent Nickel
        # Wiping (re-)acceptance — nq_last_process_date_time is refreshed on
        # every Nickel Wiping accept (see _nq_do_full_accept /
        # _nq_do_submit_accept). An unscoped "has ANY NickelAudit_Submission
        # ever" check would permanently block a lot from Nickel Audit's pick
        # table after its first audit pass, even after it cycles back
        # through Nickel Wiping and is re-accepted for a new audit cycle.
        has_submission_subquery = Exists(
            NickelAudit_Submission.objects.filter(
                lot_id=OuterRef('lot_id'),
                created_at__gt=OuterRef('nq_last_process_date_time'),
            )
        )

        is_nq_partial_accept_child_subquery = Exists(
            NickelQC_PartialAcceptLot.objects.filter(new_lot_id=OuterRef('lot_id'))
        )

        nq_partial_parent_lot_subquery = NickelQC_PartialAcceptLot.objects.filter(
            new_lot_id=OuterRef('lot_id')
        ).values('parent_lot_id')[:1]

        # ✅ Annotate with additional fields
        queryset = queryset.annotate(
            has_draft=has_draft_subquery,
            draft_type=draft_type_subquery,
            brass_rejection_total_qty=brass_rejection_qty_subquery,
            has_submission=has_submission_subquery,
            is_nq_partial_accept_child=is_nq_partial_accept_child_subquery,
            nq_partial_parent_lot_id=Subquery(nq_partial_parent_lot_subquery),
        )

        # ✅ UPDATED: Filter logic using JigUnloadAfterTable fields
        queryset = queryset.filter(
            (
                (
                    Q(na_qc_accptance__isnull=True) | Q(na_qc_accptance=False)
                ) &
                (
                    Q(na_qc_rejection__isnull=True) | Q(na_qc_rejection=False)
                    # A stale na_qc_rejection=True from a prior audit cycle must not
                    # block the lot forever — _na_do_submit_full_reject routes the
                    # lot back to Nickel Wiping under the same lot_id without
                    # clearing this flag. Once Nickel Wiping has genuinely
                    # re-accepted the lot for a new cycle, the stale flag no
                    # longer applies. See _na_fresh_nw_reacceptance_q().
                    | (Q(na_qc_rejection=True) & _na_fresh_nw_reacceptance_q())
                ) &
                ~Q(na_qc_few_cases_accptance=True, na_onhold_picking=False)
                &
                (
                    Q(nq_qc_accptance=True) |
                    Q(is_nq_partial_accept_child=True) |
                    Q(nq_qc_few_cases_accptance=True, nq_onhold_picking=False)
                )
            )
            |
            Q(na_qc_rejection=True, na_onhold_picking=True)
        ).distinct().order_by('-nq_last_process_date_time', '-lot_id')

        completed_source_lots = _na_completed_source_lot_ids(allowed_color_ids)
        pick_rows = _na_active_pick_rows(queryset, completed_source_lots, 'Z1')

        # Pagination
        page_number = request.GET.get('page', 1)
        paginator = Paginator(pick_rows, 10)
        page_obj = paginator.get_page(page_number)

        # ✅ UPDATED: Get values from JigUnloadAfterTable
        master_data = []
        jig_unload_by_lot = {}
        for jig_unload_obj in page_obj.object_list:
            jig_unload_by_lot[str(jig_unload_obj.lot_id or '').strip()] = jig_unload_obj

            data = {
                'batch_id': jig_unload_obj.unload_lot_id,  # Using unload_lot_id as batch identifier
                'lot_id': jig_unload_obj.lot_id,  # Auto-generated lot_id
                'date_time': jig_unload_obj.created_at,
                'model_stock_no__model_no': 'Combined Model',  # Since this combines multiple lots
                'plating_color': jig_unload_obj.plating_color.plating_color if jig_unload_obj.plating_color else '',
                'polish_finish': jig_unload_obj.polish_finish.polish_finish if jig_unload_obj.polish_finish else '',
                'version__version_name': jig_unload_obj.version.version_name if jig_unload_obj.version else '',
                'vendor_internal': '',  # Not available in JigUnloadAfterTable
                'location__location_name': _get_input_source(jig_unload_obj),
                'tray_type': get_model_master_tray_info(jig_unload_obj.plating_stk_no, jig_unload_obj.tray_type or '')[0],
                'tray_capacity': _na_tray_capacity(
                    get_model_master_tray_info(jig_unload_obj.plating_stk_no, jig_unload_obj.tray_type or '')[0]
                ) or _na_tray_capacity(jig_unload_obj.tray_type or '') or jig_unload_obj.tray_capacity or 0,
                'wiping_required': False,  # Default value, can be enhanced later
                'brass_audit_rejection': False,  # Not applicable for nickel IP
                
                # ✅ Stock-related fields from JigUnloadAfterTable
                'stock_lot_id': jig_unload_obj.lot_id,
                'total_IP_accpeted_quantity': jig_unload_obj.total_case_qty,
                'na_ac_accepted_qty_verified': False,  # Not applicable
                'nq_qc_accepted_qty': jig_unload_obj.nq_qc_accepted_qty,  # Not applicable
                'na_missing_qty': jig_unload_obj.na_missing_qty,  # Not applicable
                'na_physical_qty': jig_unload_obj.na_physical_qty,
                'na_physical_qty_edited': False,
                'rejected_audit_nickle_ip_stock': jig_unload_obj.unload_accepted,
                'rejected_ip_stock': jig_unload_obj.rejected_audit_nickle_ip_stock,
                'accepted_tray_scan_status': jig_unload_obj.na_accepted_tray_scan_status,
                'na_pick_remarks': jig_unload_obj.na_pick_remarks,  # Not applicable for nickel
                'nq_pick_remarks': jig_unload_obj.nq_pick_remarks,  # Nickel Inspection pick remark (previous stage)
                'nq_qc_accptance': False,  # Not applicable
                'na_accepted_tray_scan_status': False,  # Not applicable
                'na_qc_rejection': _na_effective_qc_rejection(jig_unload_obj),
                'na_qc_few_cases_accptance': jig_unload_obj.na_qc_few_cases_accptance,
                'na_onhold_picking': jig_unload_obj.na_onhold_picking,
                'na_draft': jig_unload_obj.na_draft,
                'nq_draft': False,  # Not applicable
                'send_to_nickel_brass': jig_unload_obj.send_to_nickel_brass,
                'nq_last_process_date_time': jig_unload_obj.nq_last_process_date_time,
                'iqf_last_process_date_time': None,
                'na_hold_lot': jig_unload_obj.na_hold_lot,
                'na_holding_reason': jig_unload_obj.na_holding_reason,  # Not applicable
                'na_release_lot': jig_unload_obj.na_release_lot,
                'na_release_reason': jig_unload_obj.na_release_reason,
                'na_hold_by': jig_unload_obj.na_hold_by.username if jig_unload_obj.na_hold_by else '',
                'na_hold_at': timezone.localtime(jig_unload_obj.na_hold_at).strftime("%d-%b-%Y %I:%M %p") if jig_unload_obj.na_hold_at else '',
                'na_release_by': jig_unload_obj.na_release_by.username if jig_unload_obj.na_release_by else '',
                'na_release_at': timezone.localtime(jig_unload_obj.na_release_at).strftime("%d-%b-%Y %I:%M %p") if jig_unload_obj.na_release_at else '',
                'has_draft': jig_unload_obj.has_draft,
                'draft_type': jig_unload_obj.draft_type,
                'brass_rejection_total_qty': jig_unload_obj.brass_rejection_total_qty,
                'nq_qc_accptance': jig_unload_obj.nq_qc_accptance,
                'is_nq_partial_accept_child': bool(getattr(jig_unload_obj, 'is_nq_partial_accept_child', False)),
                'nq_partial_parent_lot_id': getattr(jig_unload_obj, 'nq_partial_parent_lot_id', '') or '',
                'nq_partial_accept_qty': getattr(jig_unload_obj, 'nq_partial_accept_qty', None) or jig_unload_obj.total_case_qty,
                # Additional fields from JigUnloadAfterTable
                'plating_stk_no': jig_unload_obj.plating_stk_no or '',
                'polishing_stk_no': jig_unload_obj.polish_stk_no or '',
                'category': jig_unload_obj.category or '',
                # Prefer the live current_stage SSOT (modelmasterapp/stage_service.py) so
                # this stays in sync with downstream modules (e.g. Spider Spindle) that
                # only update current_stage and not last_process_module.
                'last_process_module': jig_unload_obj.current_stage or jig_unload_obj.last_process_module or 'Jig Unload',
                'combine_lot_ids': jig_unload_obj.combine_lot_ids,  # Show which lots were combined
                'unload_lot_id': jig_unload_obj.unload_lot_id,  # Additional identifier
                # Nickel-specific fields
                'na_ac_accepted_qty_verified': jig_unload_obj.na_ac_accepted_qty_verified,
                'na_qc_acceptance': jig_unload_obj.na_qc_accptance,  # template uses na_qc_acceptance key
                'audit_check': jig_unload_obj.audit_check,
            }

            # *** ENHANCED MODEL IMAGES LOGIC (Same as other views) ***
            images = []
            model_master = None
            model_no = None

            # Priority 1: Get images from ModelMaster based on plating_stk_no
            if jig_unload_obj.plating_stk_no:
                plating_stk_no = str(jig_unload_obj.plating_stk_no)
                if len(plating_stk_no) >= 4:
                    model_no_prefix = plating_stk_no[:4]

                    try:
                        # Find ModelMaster where model_no matches the prefix for images
                        model_master = ModelMaster.objects.filter(
                            model_no__startswith=model_no_prefix
                        ).prefetch_related('images').first()

                        if model_master:
                            # Get images from ModelMaster
                            for img in _sort_images_front_first_safe(model_master.images.all()):
                                if img.master_image:
                                    images.append(img.master_image.url)
                    except Exception as e:
                        logger.warning("NA Pick View - Error fetching ModelMaster for %s: %s", model_no_prefix, e)

            # Priority 2: Fallback to existing combine_lot_ids logic if no ModelMaster images
            if not images and data['combine_lot_ids']:
                first_lot_id = data['combine_lot_ids'][0] if data['combine_lot_ids'] else None
                if first_lot_id:
                    total_stock = TotalStockModel.objects.filter(lot_id=first_lot_id).first()
                    if total_stock and total_stock.batch_id:
                        batch_obj = total_stock.batch_id
                        if batch_obj.model_stock_no:
                            for img in _sort_images_front_first_safe(batch_obj.model_stock_no.images.all()):
                                if img.master_image:
                                    images.append(img.master_image.url)

            # Priority 3: Use placeholder if no images found
            if not images:
                images = [static('assets/images/imagePlaceholder.jpg')]

            data['model_images'] = images

            master_data.append(data)

        # ✅ Process the data (similar logic but adapted for JigUnloadAfterTable)
        type_of_input_map = get_type_of_input_map([data.get('stock_lot_id') for data in master_data])
        page_lot_ids = [data.get('stock_lot_id') for data in master_data]
        rejection_store_by_lot = {
            r.lot_id: r for r in Nickel_Audit_Rejection_ReasonStore.objects.filter(lot_id__in=page_lot_ids)
        }
        for data in master_data:
            total_IP_accpeted_quantity = data.get('total_IP_accpeted_quantity', 0)
            tray_capacity = data.get('tray_capacity', 0)
            data['vendor_location'] = f"{data.get('vendor_internal', '')}_{data.get('location__location_name', '')}"

            lot_id = data.get('stock_lot_id')
            data['type_of_input'] = type_of_input_map.get(lot_id, 'Fresh')

            # Use total_case_qty from JigUnloadAfterTable instead of TotalStockModel
            # (already fetched above while building master_data — avoid re-querying)
            jig_unload_obj = jig_unload_by_lot.get(lot_id)

            # Calculate display_accepted_qty
            total_rejection_qty = 0
            rejection_store = rejection_store_by_lot.get(lot_id)
            # A rejection store row from a prior audit cycle (lot rejected,
            # then routed back to Nickel Wiping and re-accepted under the
            # same lot_id) must not be subtracted from this new cycle's
            # accepted qty — only apply it if it postdates the lot's most
            # recent Nickel Wiping (re-)acceptance.
            if (
                rejection_store and rejection_store.total_rejection_quantity
                and jig_unload_obj and jig_unload_obj.nq_last_process_date_time
                and rejection_store.created_at > jig_unload_obj.nq_last_process_date_time
            ):
                total_rejection_qty = rejection_store.total_rejection_quantity

            if jig_unload_obj and total_rejection_qty > 0:
                data['display_accepted_qty'] = max(jig_unload_obj.nq_qc_accepted_qty - total_rejection_qty, 0)
            else:
                data['display_accepted_qty'] = jig_unload_obj.nq_qc_accepted_qty if jig_unload_obj else 0

            # Delink logic adapted for nickel IP
            na_physical_qty = data.get('na_physical_qty') or 0
            brass_rejection_total_qty = data.get('brass_rejection_total_qty') or 0
            is_delink_only = (na_physical_qty > 0 and 
                              brass_rejection_total_qty >= na_physical_qty and 
                              data.get('na_onhold_picking', False))
            data['is_delink_only'] = is_delink_only

            # Calculate number of trays
            display_qty = data.get('display_accepted_qty', 0)
            if tray_capacity > 0 and display_qty > 0:
                data['no_of_trays'] = math.ceil(display_qty / tray_capacity)
            else:
                data['no_of_trays'] = 0
        
            # Add available_qty
            if data.get('na_physical_qty') and data.get('na_physical_qty') > 0:
                data['available_qty'] = data.get('na_physical_qty')
            else:
                data['available_qty'] = data.get('total_IP_accpeted_quantity', 0)
                
            # --- AQL Sampling Plan Calculation ---
            display_accepted_qty = data.get('display_accepted_qty', 0)
            aql_plan = AQLSamplingPlan.objects.filter(
                lot_qty_from__lte=display_accepted_qty,
                lot_qty_to__gte=display_accepted_qty
            ).first()
            if aql_plan:
                data['aql_limit'] = float(aql_plan.aql_limit)
                data['sample_qty'] = aql_plan.sample_qty
            else:
                data['aql_limit'] = None
                data['sample_qty'] = None

        logger.info(
            "[AUDIT_PICKTABLE_FILTER] zone=Z1 page_rows=%d lot_ids=%s",
            len(master_data),
            [data['stock_lot_id'] for data in master_data],
        )
        
        context = {
            'master_data': master_data,
            'page_obj': page_obj,
            'paginator': paginator,
            'user': user,
            'is_admin': is_admin,
            'nq_rejection_reasons': nq_rejection_reasons,
            'pick_table_count': len(master_data),
        }
        return Response(context, template_name=self.template_name)


# ═══════════════════════════════════════════════════════════════
#  NA ACTION API  (mirrors nq_toggle_verified / nq_action / nq_delink_selected_trays)
# ═══════════════════════════════════════════════════════════════

def _na_tray_capacity(tray_type_name):
    """Return accept-tray capacity for a given tray_type string."""
    if not tray_type_name:
        return 0
    name = tray_type_name.strip().lower()
    if name.startswith('nr') or name.startswith('nb') or name.startswith('nd') or name in ['normal', 'normal tray']:
        return 20
    if name.startswith('jb') or 'jumbo' in name:
        return 12
    custom = InprocessInspectionTrayCapacity.objects.filter(
        tray_type__tray_type__iexact=tray_type_name, is_active=True
    ).first()
    if custom:
        return custom.custom_capacity
    tt = TrayType.objects.filter(tray_type__iexact=tray_type_name).first()
    return tt.tray_capacity if tt else 0


def _na_generate_lot_id():
    """Generate a unique LID-format lot ID for NA partial submission records."""
    from datetime import datetime
    import time
    for _ in range(10):
        now = datetime.now()
        lid = f"LID{now.strftime('%Y%m%d%H%M%S')}{str(now.microsecond).zfill(6)}"
        if not NickelAudit_PartialRejectLot.objects.filter(new_lot_id=lid).exists():
            return lid
        time.sleep(0.001)
    now = datetime.now()
    return f"LID{now.strftime('%Y%m%d%H%M%S')}{str(now.microsecond).zfill(6)}"


def _na_get_original_trays_for_allocation(lot_id, juat, create_missing=False):
    audit_trays_qs = Nickel_AuditTrayId.objects.filter(
        lot_id=lot_id,
        rejected_tray=False,
        delink_tray=False,
    ).order_by('tray_id')
    if audit_trays_qs.exists():
        return [
            {
                'tray_id': str(tray.tray_id or '').strip().upper(),
                'qty': tray.tray_quantity or 0,
                'is_top': index == 0,
            }
            for index, tray in enumerate(audit_trays_qs)
        ]

    nickel_qc_trays_qs = NickelQcTrayId.objects.filter(
        lot_id=lot_id,
        rejected_tray=False,
        delink_tray=False,
    ).order_by('tray_id')
    if nickel_qc_trays_qs.exists():
        source_rows = [
            {
                'tray_id': str(tray.tray_id or '').strip().upper(),
                'qty': tray.tray_quantity or 0,
                'is_top': index == 0,
            }
            for index, tray in enumerate(nickel_qc_trays_qs)
        ]
        if create_missing:
            for row in source_rows:
                Nickel_AuditTrayId.objects.get_or_create(
                    lot_id=lot_id,
                    tray_id=row['tray_id'],
                    defaults={
                        'tray_quantity': row['qty'],
                        'top_tray': row['is_top'],
                        'tray_type': juat.tray_type or '',
                        'tray_capacity': _na_tray_capacity(juat.tray_type or '') or juat.tray_capacity or 20,
                    },
                )
        return source_rows

    upstream, _ = get_upstream_tray_distribution(lot_id)
    raw_trays = sorted(
        [tray for tray in (upstream or []) if not tray.get('delink_tray') and not tray.get('rejected_tray')],
        key=lambda tray: str(tray.get('tray_id') or '').strip().upper(),
    )
    if create_missing:
        for index, tray in enumerate(raw_trays):
            Nickel_AuditTrayId.objects.get_or_create(
                lot_id=lot_id,
                tray_id=str(tray.get('tray_id') or '').strip().upper(),
                defaults={
                    'tray_quantity': tray.get('tray_quantity') or 0,
                    'top_tray': bool(tray.get('top_tray', False)) or index == 0,
                    'tray_type': juat.tray_type or '',
                    'tray_capacity': _na_tray_capacity(juat.tray_type or '') or juat.tray_capacity or 20,
                },
            )
    return [
        {
            'tray_id': str(tray.get('tray_id') or '').strip().upper(),
            'qty': tray.get('tray_quantity') or 0,
            'is_top': index == 0,
        }
        for index, tray in enumerate(raw_trays)
    ]


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def na_toggle_verified(request):
    """Toggle na_ac_accepted_qty_verified flag on JigUnloadAfterTable."""
    from django.db import transaction
    lot_id = request.data.get('lot_id', '').strip()
    if not lot_id:
        return Response({'success': False, 'error': 'lot_id required'}, status=400)
    try:
        with transaction.atomic():
            obj = JigUnloadAfterTable.objects.select_for_update().filter(lot_id=lot_id).first()
            if not obj:
                return Response({'success': False, 'error': 'Lot not found'}, status=404)
            obj.na_ac_accepted_qty_verified = True
            obj.save(update_fields=['na_ac_accepted_qty_verified'])
            # Qty verification is the real processing action for this stage —
            # update the shared current_stage SSOT so all completed tables
            # (Day Planning, Jig Loading, etc.) reflect "Nickel Audit".
            # obj.lot_id is this JigUnloadAfterTable row's own ID (UNLOT...),
            # which never exists in TotalStockModel — the original lot IDs
            # tracked by Day Planning/TotalStockModel are in combine_lot_ids
            # (a jig can combine multiple original lots).
            from modelmasterapp.stage_service import update_stock_stage, update_juat_stage
            for original_lot_id in (obj.combine_lot_ids or []):
                update_stock_stage(original_lot_id, 'Nickel Audit')
            update_juat_stage(lot_id, 'Nickel Audit')
        logger.info("[na_toggle_verified] lot=%s user=%s", lot_id, request.user)
        return Response({'success': True, 'last_process_module': obj.last_process_module or ''})
    except Exception as e:
        logger.exception("[na_toggle_verified] error lot=%s", lot_id)
        return Response({'success': False, 'error': 'Unable to process the request. Please verify the submitted data and try again.'}, status=500)


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def na_hold_unhold(request):
    """Hold/Release toggle for Nickel Audit pick-table lots (shared by Zone 1 and Zone 2)."""
    lot_id = (request.data.get('lot_id') or '').strip()
    action = request.data.get('action')
    remark = (request.data.get('remark') or '').strip()

    if not lot_id:
        return Response({"success": False, "error": "lot_id is required"}, status=400)
    if action not in ('hold', 'unhold'):
        return Response({"success": False, "error": "action must be 'hold' or 'unhold'"}, status=400)
    if not remark:
        return Response({"success": False, "error": "Remark is required"}, status=400)
    if len(remark) > 50:
        return Response({"success": False, "error": "Remark must be 50 characters or less"}, status=400)

    with transaction.atomic():
        juat = JigUnloadAfterTable.objects.select_for_update().filter(lot_id=lot_id).first()
        if not juat:
            return Response({"success": False, "error": "Lot not found"}, status=404)

        now = timezone.now()
        if action == 'hold':
            if juat.na_hold_lot:
                return Response({"success": False, "error": "Lot is already on hold"}, status=400)
            juat.na_hold_lot = True
            juat.na_holding_reason = remark
            juat.na_hold_by = request.user
            juat.na_hold_at = now
            juat.na_release_lot = False
            update_fields = [
                'na_hold_lot', 'na_holding_reason', 'na_hold_by', 'na_hold_at',
                'na_release_lot',
            ]
        else:
            if not juat.na_hold_lot:
                return Response({"success": False, "error": "Lot is not on hold"}, status=400)
            juat.na_hold_lot = False
            juat.na_release_reason = remark
            juat.na_release_by = request.user
            juat.na_release_at = now
            juat.na_release_lot = True
            update_fields = [
                'na_hold_lot',
                'na_release_lot', 'na_release_reason', 'na_release_by', 'na_release_at',
            ]

        juat.save(update_fields=update_fields)
    logger.info("[na_hold_unhold] lot=%s action=%s user=%s", lot_id, action, request.user)
    return Response({
        "success": True, "lot_id": lot_id, "action": action,
        "holding_reason": juat.na_holding_reason or '', "release_reason": juat.na_release_reason or '',
        "hold_lot": juat.na_hold_lot, "release_lot": juat.na_release_lot,
        "hold_by": juat.na_hold_by.username if juat.na_hold_by else '',
        "hold_at": timezone.localtime(juat.na_hold_at).strftime("%d-%b-%Y %I:%M %p") if juat.na_hold_at else '',
        "release_by": juat.na_release_by.username if juat.na_release_by else '',
        "release_at": timezone.localtime(juat.na_release_at).strftime("%d-%b-%Y %I:%M %p") if juat.na_release_at else '',
        "message": f"Lot {'held' if action == 'hold' else 'released'} successfully.",
    })


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def na_delete_lot(request):
    """Delete a Nickel Audit pick-table lot (shared by Zone 1 and Zone 2).

    Mirrors DayPlanning.DeleteBatchAPIView. Only permitted while the lot is
    still un-decided at this stage (no accept/reject/scan yet) — the same
    guard the pick-table template uses to show the bin icon — so a row that
    has already moved forward here can never be silently removed.
    """
    lot_id = (request.data.get('lot_id') or '').strip()
    if not lot_id:
        return Response({'success': False, 'error': 'lot_id is required'}, status=400)

    juat = JigUnloadAfterTable.objects.filter(lot_id=lot_id).first()
    if not juat:
        return Response({'success': False, 'error': 'Lot not found'}, status=404)

    if (
        juat.na_qc_accptance
        or juat.na_qc_rejection
        or juat.na_qc_few_cases_accptance
        or juat.na_accepted_tray_scan_status
    ):
        return Response(
            {'success': False, 'error': 'This lot has already been processed and cannot be deleted.'},
            status=400,
        )

    logger.info("[na_delete_lot] lot=%s user=%s", lot_id, request.user)
    juat.delete()
    return Response({'success': True, 'message': 'Lot deleted successfully.'})


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def na_action(request):
    """Unified NA action handler: GET_REASONS, GET_TRAYS, ALLOCATE, SUBMIT_REJECT, SUBMIT_ACCEPT, FULL_ACCEPT."""
    from django.db import transaction
    action = request.data.get('action', '')
    lot_id = request.data.get('lot_id', '').strip()
    if not action:
        return Response({'success': False, 'error': 'action required'}, status=400)
    if action == 'GET_REASONS':
        reasons = list(
            Nickel_Audit_Rejection_Table.objects.all().order_by('id').values('id', 'rejection_reason')
        )
        return Response({'success': True, 'reasons': reasons})
    if action == 'CHECK_TRAY':
        from modelmasterapp.models import TrayId as TrayMaster
        tray_id_val = request.data.get('tray_id', '').strip().upper()
        if not tray_id_val:
            return Response({'success': False, 'valid': False, 'message': 'Tray ID required'})
        master_tray = TrayMaster.objects.filter(tray_id__iexact=tray_id_val).first()
        if not master_tray:
            return Response({'success': True, 'valid': False, 'message': 'Tray not found in master'})
        if _na_tray_has_current_ownership(master_tray, current_lot_id=lot_id):
            return Response({'success': True, 'valid': False, 'message': 'Tray is occupied.'})
        external_available, _external_message = _na_validate_external_reject_tray_available(
            tray_id_val,
            current_lot_id=lot_id,
        )
        if not external_available:
            return Response({'success': True, 'valid': False, 'message': 'Tray is occupied.'})
        if len(tray_id_val) > 9:
            return Response({'success': True, 'valid': False, 'message': 'Tray ID cannot exceed 9 characters'})
        return Response({'success': True, 'valid': True, 'message': 'Valid tray'})
    if not lot_id:
        return Response({'success': False, 'error': 'lot_id required'}, status=400)
    juat = JigUnloadAfterTable.objects.filter(lot_id=lot_id).first()
    if not juat:
        return Response({'success': False, 'error': 'Lot not found'}, status=404)
    if juat.na_hold_lot:
        return Response({'success': False, 'error': 'This lot is on hold and cannot be processed until released.'}, status=400)
    if action == 'SAVE_REMARK':
        remark = (request.data.get('remark') or '').strip()
        if not remark:
            return Response({'success': False, 'error': 'remark required'}, status=400)
        if len(remark) > 100:
            return Response({'success': False, 'error': 'Remark must not exceed 100 characters.'}, status=400)
        try:
            juat.na_pick_remarks = remark
            juat.save(update_fields=['na_pick_remarks'])
            return Response({'success': True, 'message': 'Remark saved'})
        except Exception:
            logger.exception("[na_action SAVE_REMARK] lot=%s", lot_id)
            return Response({'success': False, 'error': 'Unable to process the request. Please verify the submitted data and try again.'}, status=500)
    if action == 'GET_TRAYS':
        trays_qs = Nickel_AuditTrayId.objects.filter(
            lot_id=lot_id, rejected_tray=False, delink_tray=False
        ).order_by('tray_id')
        if trays_qs.exists():
            trays = [
                {
                    'tray_id': item['tray_id'],
                    'qty': item['qty'],
                    'is_top': item['is_top'],
                    'is_delinked': False,
                }
                for item in _na_normalize_active_trays(trays_qs)
            ]
        else:
            # Fallback 1: NickelQcTrayId (lots that just passed Nickel Inspection)
            nq_trays_qs = NickelQcTrayId.objects.filter(
                lot_id=lot_id, rejected_tray=False, delink_tray=False
            ).order_by('tray_id')
            if nq_trays_qs.exists():
                trays = [
                    {
                        'tray_id': item['tray_id'],
                        'qty': item['qty'],
                        'is_top': item['is_top'],
                        'is_delinked': False,
                    }
                    for item in _na_normalize_active_trays(nq_trays_qs)
                ]
            else:
                # Fallback 2: Upstream jig unloading distribution
                upstream, _ = get_upstream_tray_distribution(lot_id)
                if upstream:
                    trays = [
                        {
                            'tray_id': item['tray_id'],
                            'qty': item['qty'],
                            'is_top': item['is_top'],
                            'is_delinked': False,
                        }
                        for item in _na_normalize_active_trays([
                            {'tray_id': t['tray_id'], 'qty': t['tray_quantity'] or 0}
                            for t in upstream
                            if not t.get('rejected_tray', False) and not t.get('delink_tray', False)
                        ])
                    ]
                else:
                    trays = []
        tray_type = (juat.tray_type or '').strip()
        tray_cap = _na_tray_capacity(tray_type) or juat.tray_capacity or 20
        logger.info(
            "[AUDIT_TRAY_DISTRIBUTION] action=GET_TRAYS lot=%s trays=%d total_qty=%s accept_cap=%s tray_type=%s",
            lot_id,
            len(trays),
            juat.nq_qc_accepted_qty or juat.total_case_qty or 0,
            tray_cap,
            tray_type,
        )
        return Response({
            'success': True,
            'trays': trays,
            'total_qty': juat.nq_qc_accepted_qty or juat.total_case_qty or 0,
            'tray_capacity': tray_cap,
            'tray_type': tray_type,
            'plating_stk_no': juat.plating_stk_no or '',
        })
    if action == 'ALLOCATE':
        try:
            rejected_qty = int(request.data.get('rejected_qty', 0))
        except (TypeError, ValueError):
            return Response({'success': False, 'error': 'Invalid rejected_qty'}, status=400)
        total_qty = juat.nq_qc_accepted_qty or juat.total_case_qty or 0
        if rejected_qty <= 0 or rejected_qty > total_qty:
            return Response({'success': False, 'error': 'rejected_qty out of range'}, status=400)
        accepted_qty = total_qty - rejected_qty
        tray_type = (juat.tray_type or '').strip().lower()
        orig_cap = _na_tray_capacity(juat.tray_type or '') or juat.tray_capacity or 20
        if tray_type.startswith('jb') or 'jumbo' in tray_type:
            rej_cap = 12
            rej_prefix = 'JB'
        else:
            rej_cap = 16
            rej_prefix = 'NB'
        orig_trays = _na_get_original_trays_for_allocation(lot_id, juat)
        allocation = build_nq_rejection_allocation(orig_trays, rejected_qty, rej_cap)
        logger.info(
            "[AUDIT_TRAY_DISTRIBUTION] action=ALLOCATE lot=%s total=%d rejected=%d accepted=%d accept_cap=%d reject_cap=%d accept_slots=%s reject_slots=%s auto_delink=%s",
            lot_id,
            total_qty,
            rejected_qty,
            accepted_qty,
            orig_cap,
            rej_cap,
            allocation['accept_slots'],
            allocation['reject_slots'],
            allocation['auto_delink_tray_ids'],
        )
        return Response({
            'success': True,
            'accepted_qty': accepted_qty,
            'rejected_qty': rejected_qty,
            'accept_slots': allocation['accept_slots'],
            'reject_slots': allocation['reject_slots'],
            'delink_slots': allocation['delink_slots'],
            'original_trays': orig_trays,
            'accept_auto_trays': allocation['accept_auto_trays'],
            'reuse_count': len(allocation['delink_slots']),
            'reuse_trays': allocation['delink_slots'],
            'auto_delink_tray_ids': allocation['auto_delink_tray_ids'],
            'rej_prefix': rej_prefix,
            'rej_cap': rej_cap,
        })
    if action == 'SUBMIT_REJECT':
        try:
            return _na_do_submit_reject(request, lot_id, juat)
        except Exception as e:
            logger.exception("[na_action SUBMIT_REJECT] lot=%s", lot_id)
            return Response({'success': False, 'error': 'Unable to process the request. Please verify the submitted data and try again.'}, status=500)
    if action == 'SUBMIT_ACCEPT':
        try:
            return _na_do_submit_accept(request, lot_id, juat)
        except Exception as e:
            logger.exception("[na_action SUBMIT_ACCEPT] lot=%s", lot_id)
            return Response({'success': False, 'error': 'Unable to process the request. Please verify the submitted data and try again.'}, status=500)
    if action == 'FULL_ACCEPT':
        try:
            return _na_do_full_accept(request, lot_id, juat)
        except Exception as e:
            logger.exception("[na_action FULL_ACCEPT] lot=%s", lot_id)
            return Response({'success': False, 'error': 'Unable to process the request. Please verify the submitted data and try again.'}, status=500)
    if action == 'SAVE_DRAFT':
        draft_data = _na_build_draft_snapshot(request.data.get('draft_data', {}), juat, request)
        with transaction.atomic():
            Nickel_Audit_Draft_Store.objects.update_or_create(
                lot_id=lot_id,
                draft_type='batch_rejection',
                defaults={
                    'batch_id': juat.unload_lot_id or lot_id,
                    'user': request.user,
                    'draft_data': draft_data,
                },
            )
            juat.na_draft = True
            update_fields = ['na_draft']
            if juat.na_onhold_picking and not juat.na_qc_rejection and not juat.na_qc_few_cases_accptance:
                juat.na_onhold_picking = False
                update_fields.append('na_onhold_picking')
            juat.save(update_fields=update_fields)
        logger.info("[AUDIT_SUBMISSION] action=SAVE_DRAFT lot=%s user=%s", lot_id, request.user)
        return Response({'success': True, 'isDraft': True, 'status': 'Draft', 'draft_data': draft_data})
    if action == 'GET_DRAFT':
        draft = Nickel_Audit_Draft_Store.objects.filter(lot_id=lot_id, draft_type='batch_rejection').first()
        if draft:
            return Response({'success': True, 'has_draft': True, 'isDraft': True, 'status': 'Draft', 'draft_data': _na_build_draft_snapshot(draft.draft_data, juat, request)})
        return Response({'success': True, 'has_draft': False, 'draft_data': {}})
    return Response({'success': False, 'error': f'Unknown action: {action}'}, status=400)


def _na_do_full_accept(request, lot_id, juat):
    """Persist FULL acceptance for a NA lot."""
    from django.db import transaction
    import django.utils.timezone as tz
    total_qty = juat.nq_qc_accepted_qty or juat.total_case_qty or 0
    accept_cap = _na_tray_capacity(juat.tray_type or '') or juat.tray_capacity or 20
    trays_qs = Nickel_AuditTrayId.objects.filter(
        lot_id=lot_id, rejected_tray=False, delink_tray=False
    ).order_by('tray_id')
    if trays_qs.exists():
        trays = _na_normalize_active_trays(trays_qs)
    else:
        # Fallback 1: NickelQcTrayId (lots that just passed Nickel Inspection)
        nq_trays_qs = NickelQcTrayId.objects.filter(
            lot_id=lot_id, rejected_tray=False, delink_tray=False
        ).order_by('tray_id')
        if nq_trays_qs.exists():
            trays = _na_normalize_active_trays(nq_trays_qs)
        else:
            # Fallback 2: Upstream jig unloading distribution
            upstream, _ = get_upstream_tray_distribution(lot_id)
            trays = _na_normalize_active_trays([
                {'tray_id': t['tray_id'], 'qty': t['tray_quantity'] or 0, 'is_top': bool(t.get('top_tray', False))}
                for t in (upstream or [])
                if not t.get('rejected_tray') and not t.get('delink_tray')
            ])
    with transaction.atomic():
        for at in trays:
            tid = at['tray_id']
            Nickel_AuditTrayId.objects.update_or_create(
                lot_id=lot_id,
                tray_id=tid,
                defaults={
                    'tray_quantity': at['qty'],
                    'top_tray': at['is_top'],
                    'tray_type': juat.tray_type or '',
                    'tray_capacity': accept_cap,
                },
            )
            _na_upsert_accepted_tray_store(lot_id, tid, at['qty'], request.user)
        NickelAudit_Submission.objects.create(
            lot_id=lot_id,
            submission_type='FULL_ACCEPT',
            total_lot_qty=total_qty,
            accepted_qty=total_qty,
            rejected_qty=0,
            accept_trays_data=trays,
            delink_trays_data=[],
            created_by=request.user,
        )
        juat.na_qc_accptance = True
        juat.na_qc_accepted_qty = total_qty
        juat.na_draft = False
        juat.na_onhold_picking = False
        juat.na_last_process_date_time = tz.now()
        juat.last_process_module = 'Nickel Audit'
        juat.current_stage = 'Nickel Audit'
        juat.save(update_fields=[
            'na_qc_accptance', 'na_qc_accepted_qty',
            'na_draft', 'na_onhold_picking',
            'na_last_process_date_time', 'last_process_module', 'current_stage',
        ])
        _na_clear_draft_state(lot_id)
    logger.info("[AUDIT_ACCEPT_FLOW] action=FULL_ACCEPT lot=%s user=%s qty=%d trays=%d", lot_id, request.user, total_qty, len(trays))
    logger.info("[AUDIT_SUBMISSION] lot=%s submission=FULL_ACCEPT accepted=%d rejected=0", lot_id, total_qty)
    return Response({'success': True})


def _na_do_submit_full_reject(request, lot_id, juat, reason_ids, rejected_qty, total_qty, remarks):
    """
    Persist a Full Lot Rejection (100% of the lot rejected) for a NA lot.

    Business rule: when the whole lot is rejected there is no tray split, so
    the operator is never asked to scan rejected trays and no new reject-tray
    records are generated. The lot's existing tray set moves back to Nickel
    Wiping (Zone 1: Nickel_Inspection app, Zone 2: nickel_inspection_zone_two
    app — both share this same JigUnloadAfterTable row and are routed purely
    by the lot's plating_color zone) by resetting the nq_qc_* flags that gate
    the Nickel Wiping pick-table queryset, mirroring how Nickel Wiping itself
    sets current_stage='Nickel Wiping' on its own accept/reject actions.
    """
    from django.db import transaction
    import django.utils.timezone as tz

    orig_trays = _na_get_original_trays_for_allocation(lot_id, juat, create_missing=True)

    with transaction.atomic():
        reasons_qs = Nickel_Audit_Rejection_Table.objects.filter(id__in=reason_ids)
        if not reasons_qs.exists():
            return Response({'success': False, 'error': 'Invalid rejection reason'}, status=400)
        reason_store, _ = Nickel_Audit_Rejection_ReasonStore.objects.update_or_create(
            lot_id=lot_id,
            defaults={
                'total_rejection_quantity': rejected_qty,
                'batch_rejection': True,
                'lot_rejected_comment': remarks,
                'user': request.user,
            },
        )
        reason_store.rejection_reason.set(reasons_qs)

        juat.na_qc_rejection = True
        juat.na_qc_few_cases_accptance = False
        juat.na_draft = False
        juat.na_onhold_picking = False
        juat.na_last_process_date_time = tz.now()
        juat.last_process_module = 'Nickel Audit'
        # Route the lot back to Nickel Wiping: clear the Nickel Wiping
        # accept/reject flags so it reappears in that module's pick table,
        # and flag it as an audit-rejection return for the popup/report trail.
        juat.nq_qc_accptance = False
        juat.nq_qc_rejection = False
        juat.rejected_nickle_ip_stock = True
        juat.current_stage = 'Nickel Wiping'
        # Lot Qty verification is a per-pass check — the lot is back in Nickel
        # Wiping for rework, so the operator must re-verify qty on this pass
        # rather than inheriting the verified checkmark from its first pass.
        juat.nq_qc_accepted_qty_verified = False
        # The lot's Nickel Inspection cycle data (accepted qty, missing/physical
        # qty, tray-scan status) belongs to the PRIOR Inspection pass and must
        # not leak into the fresh pick-table row for this new rework cycle —
        # without this reset, NQ_PickTableView/NQ_Zone_PickTableView display
        # the old accepted qty on a lot that hasn't been re-processed yet.
        juat.nq_qc_accepted_qty = 0
        juat.nq_missing_qty = 0
        juat.nq_physical_qty = 0
        juat.nq_accepted_tray_scan_status = False
        # This row's own "Lot Qty verified" checkbox (na_toggle_verified) is
        # also a per-pass check — if the operator ticked it on the pass that
        # got rejected, it must not come back pre-ticked when the lot
        # re-enters the Audit pick table for this new rework cycle; the
        # operator must verify it again.
        juat.na_ac_accepted_qty_verified = False
        juat.save(update_fields=[
            'na_qc_rejection', 'na_qc_few_cases_accptance',
            'na_draft', 'na_onhold_picking',
            'na_last_process_date_time', 'last_process_module',
            'nq_qc_accptance', 'nq_qc_rejection', 'rejected_nickle_ip_stock',
            'current_stage', 'nq_qc_accepted_qty_verified',
            'nq_qc_accepted_qty', 'nq_missing_qty', 'nq_physical_qty',
            'nq_accepted_tray_scan_status', 'na_ac_accepted_qty_verified',
        ])
        _na_clear_draft_state(lot_id)

        NickelAudit_Submission.objects.create(
            lot_id=lot_id,
            submission_type='FULL_REJECT',
            total_lot_qty=total_qty,
            accepted_qty=0,
            rejected_qty=rejected_qty,
            accept_trays_data=[],
            reject_trays_data=orig_trays,
            delink_trays_data=[],
            created_by=request.user,
        )
        logger.info(
            "[AUDIT_SUBMISSION] lot=%s submission=FULL_REJECT total=%d accepted=0 rejected=%d",
            lot_id, total_qty, rejected_qty,
        )
    logger.info(
        "[AUDIT_REJECT_FLOW] action=SUBMIT_FULL_REJECT lot=%s rej_qty=%d user=%s -> routed to Nickel Wiping (no tray scan)",
        lot_id, rejected_qty, request.user,
    )
    return Response({'success': True, 'is_partial': False})


def _na_do_submit_reject(request, lot_id, juat):
    """Persist rejection for a NA lot."""
    from django.db import transaction
    data = request.data
    reason_ids = data.get('reason_ids', [])
    full_lot_rejection = _na_bool(data.get('full_lot_rejection'))
    try:
        rejected_qty = int(data.get('rejected_qty', 0))
    except (TypeError, ValueError):
        return Response({'success': False, 'error': 'Invalid rejected_qty'}, status=400)
    reject_trays = data.get('reject_trays', [])
    accept_trays = data.get('accept_trays', [])
    submitted_delink_trays = data.get('delink_trays', [])
    remarks = (data.get('remarks', '') or '').strip()
    total_qty = juat.nq_qc_accepted_qty or juat.total_case_qty or 0
    if full_lot_rejection and not remarks:
        return Response(
            {'success': False, 'error': 'Remarks mandatory for full lot rejection.'},
            status=400,
        )
    if not reason_ids:
        return Response({'success': False, 'error': 'reason_ids and rejected_qty required'}, status=400)
    if full_lot_rejection:
        if total_qty <= 0:
            return Response({'success': False, 'error': 'reason_ids and rejected_qty required'}, status=400)
        rejected_qty = total_qty
        reject_trays = []
        accept_trays = []
        submitted_delink_trays = []
        return _na_do_submit_full_reject(request, lot_id, juat, reason_ids, rejected_qty, total_qty, remarks)
    if rejected_qty <= 0:
        return Response({'success': False, 'error': 'reason_ids and rejected_qty required'}, status=400)
    if rejected_qty > total_qty:
        return Response({'success': False, 'error': 'Rejected qty cannot exceed total qty'}, status=400)
    accepted_qty = total_qty - rejected_qty
    is_partial = accepted_qty > 0

    # Full Lot Rejection (100% of the lot): the entire lot moves back to
    # Nickel Wiping as-is — no tray split occurs, so no operator tray scan
    # is required. Handled by a dedicated path that bypasses the tray
    # allocation/scan validation below entirely. Partial rejection (accepted_qty > 0)
    # falls through unchanged to the existing tray-scan flow.
    if not is_partial:
        if not remarks:
            return Response(
                {'success': False, 'error': 'Remarks mandatory for full lot rejection.'},
                status=400,
            )
        return _na_do_submit_full_reject(request, lot_id, juat, reason_ids, rejected_qty, total_qty, remarks)

    # AQL enforcement: a partial reject whose rejected qty exceeds the AQL
    # limit for this lot-qty band must be submitted as a Full Reject instead.
    # Same AQL master data/rule as Brass Audit (shared AQLSamplingPlan table).
    if is_partial:
        _aql_plan = AQLSamplingPlan.objects.filter(
            lot_qty_from__lte=total_qty,
            lot_qty_to__gte=total_qty
        ).first()
        if _aql_plan and rejected_qty > _aql_plan.aql_limit:
            logger.warning(
                f"[AQL PARTIAL BLOCKED] lot_id={lot_id}, total_qty={total_qty}, "
                f"rejected_qty={rejected_qty}, aql_limit={_aql_plan.aql_limit} — must submit as FULL_REJECT"
            )
            return Response({
                'success': False,
                'error': (
                    f'Rejected qty ({rejected_qty}) exceeds the AQL limit '
                    f'({_aql_plan.aql_limit}) for lot qty {total_qty}. '
                    f'Submit this lot as a Full Reject instead.'
                ),
            }, status=400)

    tray_type = (juat.tray_type or '').strip().lower()
    accept_cap = _na_tray_capacity(juat.tray_type or '') or juat.tray_capacity or 20
    if tray_type.startswith('jb') or 'jumbo' in tray_type:
        allowed_prefix = 'JB'
        rej_cap = 12
    else:
        allowed_prefix = 'NB'
        rej_cap = 16
    orig_trays = _na_get_original_trays_for_allocation(lot_id, juat, create_missing=True)
    allocation = build_nq_rejection_allocation(orig_trays, rejected_qty, rej_cap)
    try:
        reject_trays = normalize_reject_trays(reject_trays, allocation['reject_slots'])
        delink_trays_snapshot = normalize_operator_delink_trays(
            submitted_delink_trays,
            allocation['delink_slots'],
            orig_trays,
        )
        accept_trays = normalize_accept_trays(
            accept_trays,
            allocation['accept_auto_trays'],
            original_trays=orig_trays,
            delink_trays=delink_trays_snapshot,
        )
        validate_original_tray_coverage(accept_trays, delink_trays_snapshot, orig_trays)
    except ValueError as exc:
        return Response({'success': False, 'error': str(exc)}, status=400)

    # A tray ID may be used in only one of reject / accept / delink for this
    # submission — catches a tray double-booked across sections that each
    # individual normalize_* call (which only checks duplicates within its own
    # list) would miss.
    reject_ids = {(rt.get('tray_id') or '').upper() for rt in reject_trays}
    delink_ids = {(dt.get('tray_id') or '').upper() for dt in delink_trays_snapshot}
    accept_ids = {(at.get('tray_id') or '').upper() for at in accept_trays}
    cross_dupes = (reject_ids & delink_ids) | (reject_ids & accept_ids) | (delink_ids & accept_ids)
    if cross_dupes:
        return Response(
            {'success': False, 'error': f"Tray(s) {', '.join(sorted(cross_dupes))} used in more than one section"},
            status=400,
        )

    if tray_qty_total(reject_trays) != rejected_qty:
        return Response({'success': False, 'error': 'Reject tray total does not match rejected qty'}, status=400)
    if tray_qty_total(accept_trays) != accepted_qty:
        return Response({'success': False, 'error': 'Accept tray total does not match accepted qty'}, status=400)
    for rt in reject_trays:
        tid = (rt.get('tray_id') or '').upper()
        if not tid.startswith(allowed_prefix):
            return Response(
                {'success': False, 'error': f'Reject tray {tid} must start with {allowed_prefix}'},
                status=400,
            )
        if int(rt.get('qty', 0)) > rej_cap:
            return Response(
                {'success': False, 'error': f'Reject tray {tid} qty exceeds max {rej_cap}'},
                status=400,
            )
        external_available, _external_message = _na_validate_external_reject_tray_available(
            tid,
            current_lot_id=lot_id,
        )
        if not external_available:
            return Response(
                {'success': False, 'error': 'Tray is occupied.'},
                status=400,
            )
    logger.info(
        "[AUDIT_REJECT_FLOW] lot=%s total=%d rejected=%d accepted=%d accept_cap=%d reject_cap=%d reject_trays=%s delink_trays=%s accept_trays=%s",
        lot_id,
        total_qty,
        rejected_qty,
        accepted_qty,
        accept_cap,
        rej_cap,
        reject_trays,
        delink_trays_snapshot,
        accept_trays,
    )
    with transaction.atomic():
        reasons_qs = Nickel_Audit_Rejection_Table.objects.filter(id__in=reason_ids)
        if not reasons_qs.exists():
            return Response({'success': False, 'error': 'Invalid rejection reason'}, status=400)
        reason_store, _ = Nickel_Audit_Rejection_ReasonStore.objects.update_or_create(
            lot_id=lot_id,
            defaults={
                'total_rejection_quantity': rejected_qty,
                'batch_rejection': not is_partial,
                'lot_rejected_comment': remarks,
                'user': request.user,
            },
        )
        reason_store.rejection_reason.set(reasons_qs)
        for rt in reject_trays:
            tid = rt.get('tray_id', '').strip()
            qty = int(rt.get('qty', 0))
            if not tid or qty <= 0:
                continue
            Nickel_Audit_Rejected_TrayScan.objects.update_or_create(
                lot_id=lot_id,
                rejected_tray_id=tid,
                defaults={
                    'rejected_tray_quantity': qty,
                    'rejection_reason': reasons_qs.first(),
                    'user': request.user,
                },
            )
        orig_trays_qs = Nickel_AuditTrayId.objects.filter(
            lot_id=lot_id,
            rejected_tray=False,
            delink_tray=False,
        )
        accept_tray_ids = {
            str(at['tray_id']).strip().upper(): at
            for at in accept_trays
            if at.get('tray_id')
        }
        delink_tray_ids = {tray['tray_id'] for tray in delink_trays_snapshot}
        for tray_obj in orig_trays_qs:
            tray_key = str(tray_obj.tray_id or '').strip().upper()
            if tray_key in accept_tray_ids:
                at = accept_tray_ids[tray_key]
                tray_obj.tray_quantity = int(at.get('qty', 0))
                tray_obj.top_tray = bool(at.get('is_top', False))
                tray_obj.save(update_fields=['tray_quantity', 'top_tray'])
            elif tray_key in delink_tray_ids:
                delink_qty = tray_obj.tray_quantity
                tray_obj.delink_tray = True
                tray_obj.delink_tray_qty = str(delink_qty)
                tray_obj.tray_quantity = 0
                tray_obj.top_tray = False
                tray_obj.save(update_fields=['delink_tray', 'delink_tray_qty', 'tray_quantity', 'top_tray'])
                release_tray_master_for_reuse(tray_obj.tray_id, delink_qty=delink_qty)
        existing_ids = set(
            Nickel_AuditTrayId.objects.filter(lot_id=lot_id).values_list('tray_id', flat=True)
        )
        for at in accept_trays:
            tid = (at.get('tray_id') or '').strip()
            qty = int(at.get('qty', 0))
            if not tid or qty <= 0 or tid in existing_ids:
                continue
            Nickel_AuditTrayId.objects.create(
                lot_id=lot_id,
                tray_id=tid,
                tray_quantity=qty,
                top_tray=bool(at.get('is_top', False)),
                tray_type=juat.tray_type or '',
                tray_capacity=accept_cap,
            )
        for at in accept_trays:
            tid = (at.get('tray_id') or '').strip()
            qty = int(at.get('qty', 0))
            if not tid or qty <= 0:
                continue
            _na_upsert_accepted_tray_store(lot_id, tid, qty, request.user)
        import django.utils.timezone as tz
        juat.na_qc_rejection = not is_partial
        juat.na_qc_few_cases_accptance = is_partial
        juat.na_draft = False
        juat.na_onhold_picking = False
        juat.na_last_process_date_time = tz.now()
        juat.last_process_module = 'Nickel Audit'
        juat.current_stage = 'Nickel Audit'
        if is_partial:
            juat.na_qc_accepted_qty = accepted_qty
        juat.save(update_fields=[
            'na_qc_rejection', 'na_qc_few_cases_accptance',
            'na_draft', 'na_onhold_picking',
            'na_last_process_date_time', 'last_process_module', 'na_qc_accepted_qty', 'current_stage',
        ])
        _na_clear_draft_state(lot_id)
        submission_type = 'PARTIAL' if is_partial else 'FULL_REJECT'
        reason_data = {str(r.id): {'reason': r.rejection_reason} for r in reasons_qs}
        submission = NickelAudit_Submission.objects.create(
            lot_id=lot_id,
            submission_type=submission_type,
            total_lot_qty=total_qty,
            accepted_qty=accepted_qty,
            rejected_qty=rejected_qty,
            accept_trays_data=accept_trays,
            reject_trays_data=reject_trays,
            delink_trays_data=delink_trays_snapshot,
            created_by=request.user,
        )
        logger.info(
            "[AUDIT_SUBMISSION] lot=%s submission=%s total=%d accepted=%d rejected=%d",
            lot_id,
            submission_type,
            total_qty,
            accepted_qty,
            rejected_qty,
        )
        if is_partial:
            child_juat = JigUnloadAfterTable(
                jig_qr_id=juat.jig_qr_id or '',
                combine_lot_ids=juat.combine_lot_ids or [],
                total_case_qty=accepted_qty,
                version=juat.version,
                plating_color=juat.plating_color,
                plating_stk_no=juat.plating_stk_no,
                polish_stk_no=juat.polish_stk_no,
                polish_finish=juat.polish_finish,
                plating_stk_no_list=juat.plating_stk_no_list or [],
                polish_stk_no_list=juat.polish_stk_no_list or [],
                version_list=juat.version_list or [],
                category=juat.category or '',
                tray_type=juat.tray_type or '',
                tray_capacity=accept_cap,
                nq_qc_accptance=True,
                nq_qc_accepted_qty=accepted_qty,
                na_qc_accptance=True,
                na_qc_accepted_qty=accepted_qty,
                na_last_process_date_time=tz.now(),
                last_process_module='Nickel Audit',
            )
            child_juat.save()
            for at in accept_trays:
                tid = (at.get('tray_id') or '').strip()
                qty = int(at.get('qty', 0))
                if tid and qty > 0:
                    Nickel_AuditTrayId.objects.update_or_create(
                        lot_id=child_juat.lot_id,
                        tray_id=tid,
                        defaults={
                            'tray_quantity': qty,
                            'top_tray': bool(at.get('is_top', False)),
                            'tray_type': juat.tray_type or '',
                            'tray_capacity': accept_cap,
                        },
                    )
            NickelAudit_PartialAcceptLot.objects.create(
                new_lot_id=child_juat.lot_id,
                parent_lot_id=lot_id,
                parent_submission=submission,
                accepted_qty=accepted_qty,
                trays_snapshot=accept_trays,
                created_by=request.user,
            )
            # Partial Reject is terminal in Nickel Audit. Preserve a dedicated
            # history identifier for NickelAudit_PartialRejectLot, but do NOT
            # create an actionable JigUnloadAfterTable / NickelQcTrayId
            # continuation in Nickel Wiping. Only Full Lot Reject is allowed
            # to route the lot back to Nickel Wiping.
            partial_reject_lot_id = _na_generate_lot_id()
            NickelAudit_PartialRejectLot.objects.create(
                new_lot_id=partial_reject_lot_id,
                parent_lot_id=lot_id,
                parent_submission=submission,
                rejected_qty=rejected_qty,
                rejection_reasons=reason_data,
                trays_snapshot=reject_trays,
                remarks=remarks,
                created_by=request.user,
            )
            logger.info(
                "[AUDIT_PARTIAL_REJECT_COMPLETE] parent=%s history_lot=%s qty=%d trays=%s -> Nickel Audit Completed",
                lot_id, partial_reject_lot_id, rejected_qty, reject_trays,
            )
    logger.info(
        "[AUDIT_REJECT_FLOW] action=SUBMIT_REJECT lot=%s rej_qty=%d partial=%s user=%s",
        lot_id, rejected_qty, is_partial, request.user,
    )
    return Response({'success': True, 'is_partial': is_partial})


def _na_do_submit_accept(request, lot_id, juat):
    """Persist full acceptance for a NA lot via tray scan."""
    from django.db import transaction
    import django.utils.timezone as tz
    accept_trays = request.data.get('accept_trays', [])
    if not accept_trays:
        return Response({'success': False, 'error': 'accept_trays required'}, status=400)
    accept_cap = _na_tray_capacity(juat.tray_type or '') or juat.tray_capacity or 20
    with transaction.atomic():
        for at in accept_trays:
            tid = (at.get('tray_id') or '').strip()
            qty = int(at.get('qty', 0))
            if not tid or qty <= 0:
                continue
            Nickel_AuditTrayId.objects.update_or_create(
                lot_id=lot_id,
                tray_id=tid,
                defaults={
                    'tray_quantity': qty,
                    'top_tray': bool(at.get('is_top', False)),
                    'tray_type': juat.tray_type or '',
                    'tray_capacity': accept_cap,
                },
            )
            _na_upsert_accepted_tray_store(lot_id, tid, qty, request.user)
        total_qty = juat.nq_qc_accepted_qty or juat.total_case_qty or 0
        juat.na_qc_accptance = True
        juat.na_qc_accepted_qty = total_qty
        juat.na_draft = False
        juat.na_onhold_picking = False
        juat.na_last_process_date_time = tz.now()
        juat.last_process_module = 'Nickel Audit'
        juat.current_stage = 'Nickel Audit'
        juat.save(update_fields=[
            'na_qc_accptance', 'na_qc_accepted_qty',
            'na_draft', 'na_onhold_picking',
            'na_last_process_date_time', 'last_process_module', 'current_stage',
        ])
        _na_clear_draft_state(lot_id)
    logger.info("[AUDIT_ACCEPT_FLOW] action=SUBMIT_ACCEPT lot=%s user=%s trays=%d", lot_id, request.user, len(accept_trays))
    logger.info("[AUDIT_SUBMISSION] lot=%s submission=SUBMIT_ACCEPT accepted=%d", lot_id, juat.total_case_qty or 0)
    return Response({'success': True})


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def na_delink_selected_trays(request):
    """Delink selected trays from NA lots."""
    from django.db import transaction
    stock_lot_ids = request.data.get('stock_lot_ids', [])
    if not stock_lot_ids:
        return Response({'success': False, 'error': 'stock_lot_ids required'}, status=400)
    updated = 0
    lots_processed = 0
    try:
        with transaction.atomic():
            for lot_id in stock_lot_ids:
                na_trays = Nickel_AuditTrayId.objects.select_for_update().filter(lot_id=lot_id, delink_tray=False)
                for tray_obj in na_trays:
                    delink_qty = tray_obj.tray_quantity
                    tray_obj.delink_tray = True
                    tray_obj.delink_tray_qty = str(delink_qty)
                    tray_obj.tray_quantity = 0
                    tray_obj.top_tray = False
                    tray_obj.save(update_fields=['delink_tray', 'delink_tray_qty', 'tray_quantity', 'top_tray'])
                    if release_tray_master_for_reuse(tray_obj.tray_id, delink_qty=delink_qty):
                        updated += 1
                lots_processed += 1
        logger.info("[AUDIT_DELINK_FLOW] user=%s lots=%s freed=%d processed=%d", request.user, stock_lot_ids, updated, lots_processed)
        return Response({'success': True, 'updated': updated, 'lots_processed': lots_processed})
    except Exception as e:
        logger.exception("[na_delink_selected_trays] error")
        return Response({'success': False, 'error': 'Unable to process the request. Please verify the submitted data and try again.'}, status=500)


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def na_completed_tray_list(request):
    lot_id = request.GET.get('lot_id', '').strip()
    submission_id = request.GET.get('submission_id', '').strip()
    if not lot_id:
        return JsonResponse({'success': False, 'error': 'lot_id required'}, status=400)
    return JsonResponse({'success': True, 'trays': _na_completed_tray_snapshot(lot_id, submission_id=submission_id)})


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def na_completed_tray_validate(request):
    lot_id = (request.data.get('lot_id') or '').strip()
    tray_id = (request.data.get('tray_id') or '').strip().upper()
    if not lot_id or not tray_id:
        return JsonResponse({'success': False, 'exists': False, 'error': 'lot_id and tray_id required'}, status=400)
    if len(tray_id) > 9:
        return JsonResponse({'success': True, 'exists': False, 'message': 'Tray ID cannot exceed 9 characters'})
    trays = _na_completed_tray_snapshot(lot_id)
    exists = any(str(tray.get('tray_id') or '').strip().upper() == tray_id for tray in trays)
    return JsonResponse({'success': True, 'exists': exists, 'data_source': 'Nickel_Audit_Submission'})


# ═══════════════════════════════════════════════════════════════
#  END NA ACTION API
# ═══════════════════════════════════════════════════════════════

@method_decorator(login_required, name='dispatch')
class NACompletedView(APIView):
    """Nickel Audit Zone 1 Completed Table."""
    renderer_classes = [TemplateHTMLRenderer]
    template_name = 'Nickel_Audit/NickelAudit_Completed.html'

    def get(self, request):
        from django.utils import timezone
        from datetime import datetime, timedelta
        import pytz

        user = request.user
        tz = pytz.timezone('Asia/Kolkata')
        now_local = timezone.now().astimezone(tz)
        today = now_local.date()
        yesterday = today - timedelta(days=1)

        from_date_str = request.GET.get('from_date')
        to_date_str = request.GET.get('to_date')
        if from_date_str and to_date_str:
            try:
                from_date = datetime.strptime(from_date_str, '%Y-%m-%d').date()
                to_date = datetime.strptime(to_date_str, '%Y-%m-%d').date()
            except ValueError:
                from_date = yesterday
                to_date = today
        else:
            from_date = yesterday
            to_date = today

        from_datetime = timezone.make_aware(datetime.combine(from_date, datetime.min.time()))
        to_datetime = timezone.make_aware(datetime.combine(to_date, datetime.max.time()))

        allowed_color_ids = Plating_Color.objects.filter(
            jig_unload_zone_1=True
        ).values_list('id', flat=True)

        # ERR4 Fix: list one row per completed submission event (append-only
        # NickelAudit_Submission history), not one row per live
        # JigUnloadAfterTable object — a Full Reject routes the same row
        # back to Nickel Wiping for rework instead of creating a new one, so
        # the latter silently collapsed multiple completed audit passes for
        # the same physical lot into a single row.
        completed_rows = _na_completed_event_rows(allowed_color_ids)
        completed_rows = [
            r for r in completed_rows
            if r.na_last_process_date_time
            and from_datetime <= r.na_last_process_date_time <= to_datetime
        ]

        page_number = request.GET.get('page', 1)
        paginator = Paginator(completed_rows, 10)
        page_obj = paginator.get_page(page_number)

        master_data = []
        for jig_unload_obj in page_obj.object_list:
            # accepted_qty/rejected_qty/no_of_trays come straight from this
            # row's own NickelAudit_Submission event (set by
            # _na_completed_event_rows) — no need to re-derive them via
            # _na_completed_lot_totals's "latest submission for this lot_id"
            # lookup, which would pick up a later, unrelated event.
            accepted_qty = jig_unload_obj.na_qc_accepted_qty or 0
            rejected_qty = getattr(jig_unload_obj, '_na_rejected_qty', 0)
            no_of_trays = getattr(jig_unload_obj, '_na_no_of_trays', 0)
            data = {
                'batch_id': jig_unload_obj.unload_lot_id,
                'lot_id': jig_unload_obj.lot_id,
                'date_time': jig_unload_obj.created_at,
                'model_stock_no__model_no': 'Combined Model',
                'plating_color': jig_unload_obj.plating_color.plating_color if jig_unload_obj.plating_color else '',
                'polish_finish': jig_unload_obj.polish_finish.polish_finish if jig_unload_obj.polish_finish else '',
                'version__version_name': jig_unload_obj.version.version_name if jig_unload_obj.version else '',
                'vendor_internal': '',
                'location__location_name': ', '.join([loc.location_name for loc in jig_unload_obj.location.all()]),
                'tray_type': jig_unload_obj.tray_type or '',
                'tray_capacity': jig_unload_obj.tray_capacity or 0,
                'stock_lot_id': jig_unload_obj.lot_id,
                # Prefer the live current_stage SSOT (modelmasterapp/stage_service.py) so
                # this stays in sync with downstream modules (e.g. Spider Spindle) that
                # only update current_stage and not last_process_module.
                'last_process_module': jig_unload_obj.current_stage or jig_unload_obj.last_process_module or 'Jig Unload',
                'total_IP_accpeted_quantity': jig_unload_obj.total_case_qty,
                'na_qc_accptance': jig_unload_obj.na_qc_accptance,
                'na_qc_rejection': jig_unload_obj.na_qc_rejection,
                'na_qc_few_cases_accptance': jig_unload_obj.na_qc_few_cases_accptance,
                'na_onhold_picking': jig_unload_obj.na_onhold_picking,
                'na_hold_lot': jig_unload_obj.na_hold_lot,
                'na_holding_reason': jig_unload_obj.na_holding_reason,
                'na_release_lot': jig_unload_obj.na_release_lot,
                'na_release_reason': jig_unload_obj.na_release_reason,
                'na_physical_qty': jig_unload_obj.na_physical_qty,
                'na_missing_qty': jig_unload_obj.na_missing_qty or 0,
                'na_pick_remarks': jig_unload_obj.na_pick_remarks,
                'nq_pick_remarks': jig_unload_obj.nq_pick_remarks,  # Nickel Inspection pick remark (previous stage)
                'na_accepted_tray_scan_status': jig_unload_obj.na_accepted_tray_scan_status,
                'na_ac_accepted_qty_verified': jig_unload_obj.na_ac_accepted_qty_verified,
                'na_qc_accepted_qty': accepted_qty,
                'na_rejection_qty': rejected_qty,
                'na_submission_id': getattr(jig_unload_obj, '_na_submission_id', ''),
                'na_last_process_date_time': jig_unload_obj.na_last_process_date_time,
                'plating_stk_no': jig_unload_obj.plating_stk_no or '',
                'polishing_stk_no': jig_unload_obj.polish_stk_no or '',
                'category': jig_unload_obj.category or '',
                'combine_lot_ids': jig_unload_obj.combine_lot_ids,
                'unload_lot_id': jig_unload_obj.unload_lot_id,
                'audit_check': jig_unload_obj.audit_check,
                # "Lot Qty" is the total quantity processed in this completion
                # event (accepted + rejected), not just the accepted portion —
                # for a Full Reject event accepted_qty is legitimately 0, so
                # falling back to accepted_qty alone showed "0" instead of the
                # lot's real total. total_case_qty (= this event's
                # NickelAudit_Submission.total_lot_qty) already carries that
                # correctly-scoped total.
                'display_accepted_qty': jig_unload_obj.total_case_qty or accepted_qty,
                'available_qty': accepted_qty or jig_unload_obj.na_physical_qty or jig_unload_obj.total_case_qty or 0,
                'no_of_trays': no_of_trays,
                # Lot is only "Released" once it has actually moved past this stage
                # (current_stage SSOT differs from 'Nickel Audit'). na_ac_accepted_qty_verified
                # only reflects the qty-check step done during picking, not a downstream move,
                # so it must not drive this pill (mirrors Inprocess_Inspection.lot_status).
                'lot_status': (
                    'Released'
                    if jig_unload_obj.current_stage and jig_unload_obj.current_stage != 'Nickel Audit'
                    else 'Yet to Release'
                ),
            }

            images = []
            if jig_unload_obj.plating_stk_no:
                plating_stk_no = str(jig_unload_obj.plating_stk_no)
                if len(plating_stk_no) >= 4:
                    model_no_prefix = plating_stk_no[:4]
                    model_master = (
                        ModelMaster.objects.filter(model_no__startswith=model_no_prefix)
                        .prefetch_related('images')
                        .first()
                    )
                    if model_master:
                        for img in _sort_images_front_first_safe(model_master.images.all()):
                            if img.master_image:
                                images.append(img.master_image.url)
            if not images and data['combine_lot_ids']:
                first_lot_id = data['combine_lot_ids'][0] if data['combine_lot_ids'] else None
                if first_lot_id:
                    total_stock = TotalStockModel.objects.filter(lot_id=first_lot_id).first()
                    if total_stock and total_stock.batch_id and total_stock.batch_id.model_stock_no:
                        for img in _sort_images_front_first_safe(total_stock.batch_id.model_stock_no.images.all()):
                            if img.master_image:
                                images.append(img.master_image.url)
            if not images:
                images = [static('assets/images/imagePlaceholder.jpg')]
            data['model_images'] = images
            master_data.append(data)

        type_of_input_map = get_type_of_input_map([data.get('stock_lot_id') for data in master_data])
        for data in master_data:
            data['type_of_input'] = type_of_input_map.get(data.get('stock_lot_id'), 'Fresh')

        context = {
            'master_data': master_data,
            'page_obj': page_obj,
            'paginator': paginator,
            'user': user,
            'from_date': from_date.strftime('%Y-%m-%d'),
            'to_date': to_date.strftime('%Y-%m-%d'),
            'date_filter_applied': bool(from_date_str and to_date_str),
        }
        return Response(context, template_name=self.template_name)

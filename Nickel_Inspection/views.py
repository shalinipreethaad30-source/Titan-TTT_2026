from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.renderers import TemplateHTMLRenderer
from django.shortcuts import render
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
from django.utils import timezone
from django.contrib.auth.decorators import login_required
import traceback
import uuid
import logging
from rest_framework import status
from django.http import JsonResponse
import json
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


from rest_framework.permissions import IsAuthenticated
from django.views.decorators.http import require_GET
from math import ceil
from django.db.models import Q
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from IQF.models import *
from BrassAudit.models import *
from Nickel_Inspection.models import *
from Nickel_Audit.models import NickelAudit_Submission
from Jig_Unloading.models import *
from Jig_Loading.models import JigCompleted
from Jig_Unloading.tray_utils import (
    get_upstream_tray_distribution,
    get_model_master_tray_info,
    normalize_combine_lot_id,
)
from Nickel_Inspection.services import (
    build_nq_rejection_allocation,
    get_current_nickel_wiping_reject_trays,
    get_nickel_wiping_rejection_tray_allocation,
    has_unreleased_nickel_wiping_reject_trays,
    is_nickel_wiping_tray_master_released,
    normalize_accept_trays,
    normalize_operator_delink_trays,
    normalize_reject_trays,
    release_tray_master_for_reuse,
    tray_qty_total,
    validate_original_tray_coverage,
    validate_nickel_wiping_rejection_tray_available,
    validate_nickel_wiping_rejection_tray_series,
)
from Inprocess_Inspection.models import InprocessInspectionTrayCapacity
from django.contrib.auth.decorators import login_required
from modelmasterapp.type_of_input import get_type_of_input_map

def _nq_tray_capacity(tray_type_name):
    """Return accept-tray capacity for a given tray_type string.
    Normal / NR / NR-16 variants → 20.  Jumbo / JB → 12.
    Falls back to InprocessInspectionTrayCapacity, then TrayType master.
    """
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


def _nq_int(value):
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _latest_na_full_reject_timestamps(lot_ids):
    cleaned_lot_ids = []
    seen = set()
    for lot_id in lot_ids:
        clean_lot_id = str(lot_id or "").strip()
        if clean_lot_id and clean_lot_id not in seen:
            cleaned_lot_ids.append(clean_lot_id)
            seen.add(clean_lot_id)

    if not cleaned_lot_ids:
        return {}

    latest_by_lot = {}
    submissions = (
        NickelAudit_Submission.objects.filter(
            lot_id__in=cleaned_lot_ids,
            submission_type="FULL_REJECT",
        )
        .order_by("lot_id", "-created_at", "-id")
        .values("lot_id", "created_at")
    )
    for submission in submissions:
        lot_id = submission["lot_id"]
        if lot_id not in latest_by_lot:
            latest_by_lot[lot_id] = submission["created_at"]
    return latest_by_lot


def _nq_pick_last_updated(jig_unload_obj, na_full_reject_timestamps):
    lot_id = str(getattr(jig_unload_obj, "lot_id", "") or "").strip()
    latest_full_reject_at = na_full_reject_timestamps.get(lot_id)
    is_returned_from_na_full_reject = (
        bool(latest_full_reject_at)
        and str(getattr(jig_unload_obj, "current_stage", "") or "").strip().lower() == "nickel wiping"
        and bool(getattr(jig_unload_obj, "rejected_nickle_ip_stock", False))
        and bool(getattr(jig_unload_obj, "na_qc_rejection", False))
        and not bool(getattr(jig_unload_obj, "nq_qc_accptance", False))
        and not bool(getattr(jig_unload_obj, "nq_qc_rejection", False))
    )
    if is_returned_from_na_full_reject:
        return latest_full_reject_at
    return jig_unload_obj.created_at


def _resolve_nq_previous_unloading_remark(jig_unload_obj):
    """Resolve the upstream Jig Unloading remark saved on JigCompleted."""
    candidates = []
    for raw_lot in (getattr(jig_unload_obj, "combine_lot_ids", None) or []):
        if raw_lot:
            candidates.append(str(raw_lot).rsplit("-", 1)[-1])
            candidates.append(str(raw_lot))
    for attr in ("lot_id", "unload_lot_id"):
        value = getattr(jig_unload_obj, attr, None)
        if value:
            candidates.append(str(value))

    seen = set()
    for lot_id in candidates:
        if not lot_id or lot_id in seen:
            continue
        seen.add(lot_id)
        jig_completed = (
            JigCompleted.objects.filter(lot_id=lot_id)
            .exclude(unloading_remarks__isnull=True)
            .exclude(unloading_remarks="")
            .order_by("-updated_at")
            .first()
        )
        if jig_completed and jig_completed.unloading_remarks:
            return jig_completed.unloading_remarks
    return ""


def _nq_tray_sort_key(tray_id):
    return str(tray_id or '').strip().upper()


def _nq_tray_has_current_ownership(master_tray, current_lot_id=None):
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


def _nq_normalize_tray_snapshot(rows, rejected=False):
    clean_rows = []
    for row in rows or []:
        tray_id = row.get('tray_id') if isinstance(row, dict) else getattr(row, 'tray_id', '')
        qty = row.get('qty', row.get('tray_quantity', 0)) if isinstance(row, dict) else getattr(row, 'tray_quantity', 0)
        qty = _nq_int(qty)
        if not tray_id or qty <= 0:
            continue
        clean_rows.append({
            'tray_id': tray_id,
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
            top_row = min(clean_rows, key=lambda item: (item['tray_quantity'], _nq_tray_sort_key(item['tray_id'])))
            for item in clean_rows:
                item['top_tray'] = item['tray_id'] == top_row['tray_id']
        return sorted(clean_rows, key=lambda item: (not item['top_tray'], item['tray_quantity'], _nq_tray_sort_key(item['tray_id'])))

    top_row = min(clean_rows, key=lambda item: (item['tray_quantity'], _nq_tray_sort_key(item['tray_id'])))
    for item in clean_rows:
        item['top_tray'] = item['tray_id'] == top_row['tray_id']
    return sorted(clean_rows, key=lambda item: (not item['top_tray'], _nq_tray_sort_key(item['tray_id'])))


def _nq_delink_tray_snapshot(lot_id):
    rows = NickelQcTrayId.objects.filter(
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
            'tray_quantity': 0,
            'delink_tray_qty': row.get('delink_tray_qty') or '',
            'top_tray': False,
            'rejected_tray': False,
            'delink_tray': True,
        })
    return trays


def _nq_normalize_delink_tray_snapshot(rows):
    clean_rows = []
    for row in rows or []:
        if isinstance(row, dict):
            tray_id = row.get('tray_id')
            qty = row.get('qty', row.get('tray_quantity', row.get('delink_tray_qty', 0)))
        else:
            tray_id = getattr(row, 'tray_id', '')
            qty = getattr(row, 'tray_quantity', 0)
        tray_id = str(tray_id or '').strip().upper()
        qty = _nq_int(qty)
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
    return sorted(clean_rows, key=lambda item: _nq_tray_sort_key(item['tray_id']))


def _nq_with_delink_tray_snapshot(lot_id, trays):
    combined = list(trays or [])
    existing_delink_ids = {
        str(row.get('tray_id') or '').strip().upper()
        for row in combined
        if row.get('delink_tray')
    }
    for row in _nq_delink_tray_snapshot(lot_id):
        if row['tray_id'] not in existing_delink_ids:
            combined.append(row)
    return combined

def _nq_upsert_accepted_tray_store(lot_id, tray_id, qty, user):
    tid = str(tray_id or '').strip()
    tray_qty = _nq_int(qty)
    if not tid or tray_qty <= 0:
        return None
    return Nickel_Qc_Accepted_TrayID_Store.objects.update_or_create(
        tray_id=tid,
        defaults={
            'lot_id': lot_id,
            'tray_qty': tray_qty,
            'user': user,
            'is_save': True,
            'is_draft': False,
        },
    )


def _nq_list(value):
    return value if isinstance(value, list) else []


def _nq_draft_zone_label(request):
    return 'Zone 2' if 'zone_two' in (request.path or '').lower() else 'Zone 1'


def _nq_build_draft_snapshot(raw_draft_data, juat, request):
    draft_data = dict(raw_draft_data) if isinstance(raw_draft_data, dict) else {}
    total_qty = _nq_int(draft_data.get('total_lot_qty', draft_data.get('total_qty', juat.total_case_qty or 0)))
    rejected_qty = _nq_int(draft_data.get('rejected_qty', 0))
    accepted_qty = _nq_int(draft_data.get('accepted_qty', max(total_qty - rejected_qty, 0)))
    reason_qtys = _nq_list(draft_data.get('reason_qtys'))
    reject_trays = _nq_list(draft_data.get('reject_trays'))
    accept_trays = _nq_list(draft_data.get('accept_trays'))
    delink_trays = _nq_list(draft_data.get('delink_trays'))
    original_trays = _nq_list(draft_data.get('original_trays'))

    draft_data.update({
        'isDraft': True,
        'is_draft': True,
        'status': 'Draft',
        'module': 'Nickel Inspection',
        'zone': draft_data.get('zone') or _nq_draft_zone_label(request),
        'lot_id': juat.lot_id,
        'batch_id': juat.unload_lot_id or juat.lot_id,
        'plating_stk_no': draft_data.get('plating_stk_no') or juat.plating_stk_no or '',
        'total_lot_qty': total_qty,
        'total_qty': total_qty,
        'rejected_qty': rejected_qty,
        'accepted_qty': accepted_qty,
        'remaining_qty': max(total_qty - rejected_qty, 0),
        'reason_qtys': reason_qtys,
        'reject_trays': reject_trays,
        'accept_trays': accept_trays,
        'delink_trays': delink_trays,
        'original_trays': original_trays,
        'reject_slots': _nq_list(draft_data.get('reject_slots')),
        'accept_slots': _nq_list(draft_data.get('accept_slots')),
        'delink_slots': _nq_list(draft_data.get('delink_slots')),
        'accept_auto_trays': _nq_list(draft_data.get('accept_auto_trays')),
        'auto_delink_tray_ids': _nq_list(draft_data.get('auto_delink_tray_ids')),
        'tray_counts': {
            'original': len(original_trays),
            'reject': len(reject_trays),
            'accept': len(accept_trays),
            'delink': len(delink_trays),
        },
    })
    draft_data.setdefault('rejection_reasons', reason_qtys)
    return draft_data


def _nq_clear_draft_state(lot_id):
    Nickel_QC_Draft_Store.objects.filter(lot_id=lot_id, draft_type='batch_rejection').delete()


def _nq_get_original_trays_for_allocation(lot_id, juat, create_missing=False):
    trays_qs = NickelQcTrayId.objects.filter(
        lot_id=lot_id, rejected_tray=False, delink_tray=False
    ).order_by('tray_id')
    if trays_qs.exists():
        return [
            {'tray_id': str(tray.tray_id or '').strip().upper(), 'qty': tray.tray_quantity or 0, 'is_top': index == 0}
            for index, tray in enumerate(trays_qs)
        ]

    upstream, _ = get_upstream_tray_distribution(lot_id)
    raw_trays = sorted(
        [tray for tray in (upstream or []) if not tray.get('delink_tray') and not tray.get('rejected_tray')],
        key=lambda tray: tray['tray_id'],
    )
    if create_missing:
        for tray in raw_trays:
            NickelQcTrayId.objects.get_or_create(
                lot_id=lot_id,
                tray_id=str(tray['tray_id'] or '').strip().upper(),
                defaults={
                    'tray_quantity': tray['tray_quantity'] or 0,
                    'top_tray': tray.get('top_tray', False),
                    'tray_type': juat.tray_type or '',
                    'tray_capacity': juat.tray_capacity or 20,
                },
            )
    return [
        {'tray_id': str(tray['tray_id'] or '').strip().upper(), 'qty': tray['tray_quantity'] or 0, 'is_top': index == 0}
        for index, tray in enumerate(raw_trays)
    ]

def _get_input_source(jig_unload_obj):
    """Return location names with fallback chain: M2M → TotalStockModel → TrayId → ModelMasterCreation."""
    names = [loc.location_name for loc in jig_unload_obj.location.all()]
    if not names:
        for raw_cid in jig_unload_obj.combine_lot_ids or []:
            # combine_lot_ids entries are formatted "-LIDxxx" or "JLOT-xxx-LIDxxx" — extract plain lot_id
            cid = raw_cid.rsplit("-", 1)[-1] if raw_cid and "-" in raw_cid else raw_cid
            if not cid:
                continue
            # Try TotalStockModel first
            tsm = (
                TotalStockModel.objects.filter(lot_id=cid)
                .prefetch_related("location")
                .select_related("batch_id__location")
                .first()
            )
            if tsm and tsm.location.exists():
                names = [loc.location_name for loc in tsm.location.all()]
                break
            if tsm and tsm.batch_id and tsm.batch_id.location:
                names = [tsm.batch_id.location.location_name]
                break
            # Fallback: LID... lot_ids belong to TrayId — trace TrayId.batch_id.location
            tray = TrayId.objects.filter(lot_id=cid).select_related("batch_id__location").first()
            if tray and tray.batch_id and tray.batch_id.location:
                names = [tray.batch_id.location.location_name]
                break
    return ", ".join(names)
@method_decorator(login_required, name="dispatch")

class NQ_PickTableView(APIView):
    renderer_classes = [TemplateHTMLRenderer]
    template_name = "Nickel_Inspection/Nickel_PickTable.html"
    def get_dynamic_tray_capacity(self, tray_type_name):
        return _nq_tray_capacity(tray_type_name)
    def get(self, request):
        user = request.user
        is_admin = user.groups.filter(name="Admin").exists() if user.is_authenticated else False
        nq_rejection_reasons = Nickel_QC_Rejection_Table.objects.all().order_by("id")
        # Get all plating_color IDs where jig_unload_zone_1 is True
        allowed_color_ids = Plating_Color.objects.filter(jig_unload_zone_1=True).values_list(
            "id", flat=True
        )
        # ✅ CHANGED: Query JigUnloadAfterTable instead of TotalStockModel with zone filtering
        queryset = (
            JigUnloadAfterTable.objects.select_related("version", "plating_color", "polish_finish", "nq_hold_by", "nq_release_by")
            .prefetch_related("location")  # ManyToManyField requires prefetch_related
            .filter(
                total_case_qty__gt=0,  # Only show records with quantity > 0
                plating_color_id__in=allowed_color_ids,  # Only show records for zone 1
            )
        )
            # ✅ Add draft status subqueries for Nickel QC
        has_draft_subquery = Exists(
            Nickel_QC_Draft_Store.objects.filter(
                lot_id=OuterRef("lot_id")  # Using the auto-generated lot_id
            )
        )
        draft_type_subquery = Nickel_QC_Draft_Store.objects.filter(
            lot_id=OuterRef("lot_id")
        ).values("draft_type")[:1]
        brass_rejection_qty_subquery = Nickel_QC_Rejection_ReasonStore.objects.filter(
            lot_id=OuterRef("lot_id")
        ).values("total_rejection_quantity")[:1]
        # ✅ Annotate with additional fields
        queryset = queryset.annotate(
            has_draft=has_draft_subquery,
            draft_type=draft_type_subquery,
            brass_rejection_total_qty=brass_rejection_qty_subquery,
        )
        # ✅ UPDATED: Filter logic using JigUnloadAfterTable fields
        queryset = queryset.filter(
            (
                # Not yet accepted or rejected in Nickel IP
                (Q(nq_qc_accptance__isnull=True) | Q(nq_qc_accptance=False))
                & (Q(nq_qc_rejection__isnull=True) | Q(nq_qc_rejection=False))
                &
                # Exclude few cases acceptance with no hold
                ~Q(nq_qc_few_cases_accptance=True, nq_onhold_picking=False)
            )
            &
            (
                # Must be coming from jig unload (basic requirement)
                Q(total_case_qty__gt=0)
                | Q(send_to_nickel_brass=True)  # Explicitly sent to nickel IP
                | Q(rejected_nickle_ip_stock=True, nq_onhold_picking=True)  # Rejected but on hold
            )
        ).order_by("-created_at", "-lot_id")

        # ✅ SECONDARY MULTI-MODEL LOT FILTER (same convention as Jig_Unloading/JigUnloading_Zone2):
        # When two lots are combined into a single jig (Add Model / multi-model Jig Loading),
        # Jig Unloading's "Submit All" creates one JigUnloadAfterTable source row per original
        # lot for traceability. The secondary lot's row carries no plating/tray reference of its
        # own — the combined data already lives on the primary row — so it must not surface here
        # as a separate, blank "no ref" entry.
        secondary_lot_ids = set()
        for _mm_rec in JigCompleted.objects.filter(
            is_multi_model=True,
            multi_model_allocation__isnull=False
        ).only('lot_id', 'multi_model_allocation'):
            if not _mm_rec.multi_model_allocation:
                continue
            _primary_model = ''
            for _alloc in _mm_rec.multi_model_allocation:
                if isinstance(_alloc, dict) and _alloc.get('lot_id') == _mm_rec.lot_id:
                    _primary_model = str(_alloc.get('model') or _alloc.get('model_name') or '').strip()
                    break
            for _alloc in _mm_rec.multi_model_allocation:
                if not isinstance(_alloc, dict):
                    continue
                _alloc_lot = _alloc.get('lot_id', '')
                if not _alloc_lot or _alloc_lot == _mm_rec.lot_id:
                    continue
                _alloc_model = str(_alloc.get('model') or _alloc.get('model_name') or '').strip()
                # Only fold a secondary lot into the primary row (hide it here) when its
                # plating stock number is identical ("ditto") to the primary's. If the
                # combined lots are DIFFERENT models, the secondary lot must keep showing
                # as its own separate row in Nickel Wiping, not be hidden/lost.
                if _primary_model and _alloc_model and _alloc_model == _primary_model:
                    secondary_lot_ids.add(_alloc_lot)
        if secondary_lot_ids:
            queryset = [
                row for row in queryset
                if not (
                    row.combine_lot_ids
                    and {normalize_combine_lot_id(cid) for cid in row.combine_lot_ids}.issubset(secondary_lot_ids)
                )
            ]

        # Pagination
        page_number = request.GET.get("page", 1)
        paginator = Paginator(queryset, 10)
        page_obj = paginator.get_page(page_number)
        na_full_reject_timestamps = _latest_na_full_reject_timestamps(
            [obj.lot_id for obj in page_obj.object_list]
        )
        # ✅ UPDATED: Get values from JigUnloadAfterTable
        master_data = []
        jig_unload_by_lot = {}
        for jig_unload_obj in page_obj.object_list:
            jig_unload_by_lot[jig_unload_obj.lot_id] = jig_unload_obj
            data = {
                "batch_id": jig_unload_obj.unload_lot_id,  # Using unload_lot_id as batch identifier
                "lot_id": jig_unload_obj.lot_id,  # Auto-generated lot_id
                "date_time": jig_unload_obj.created_at,
                "model_stock_no__model_no": "Combined Model",  # Since this combines multiple lots
                "plating_color": (
                    jig_unload_obj.plating_color.plating_color
                    if jig_unload_obj.plating_color
                    else ""
                ),
                "polish_finish": (
                    jig_unload_obj.polish_finish.polish_finish
                    if jig_unload_obj.polish_finish
                    else ""
                ),
                "version__version_name": (
                    jig_unload_obj.version.version_name if jig_unload_obj.version else ""
                ),
                "vendor_internal": "",  # Not available in JigUnloadAfterTable
                "location__location_name": _get_input_source(jig_unload_obj),
                "tray_type": get_model_master_tray_info(
                    jig_unload_obj.plating_stk_no, jig_unload_obj.tray_type or ""
                )[0],
                "tray_capacity": (
                    self.get_dynamic_tray_capacity(
                        get_model_master_tray_info(
                            jig_unload_obj.plating_stk_no,
                            jig_unload_obj.tray_type or "",
                        )[0]
                    )
                    if jig_unload_obj.plating_stk_no or jig_unload_obj.tray_type
                    else 0
                ),
                "wiping_required": False,  # Default value, can be enhanced later
                "brass_audit_rejection": False,  # Not applicable for nickel IP
                # ✅ Stock-related fields from JigUnloadAfterTable
                "stock_lot_id": jig_unload_obj.lot_id,
                "total_IP_accpeted_quantity": jig_unload_obj.total_case_qty,
                "nq_qc_accepted_qty_verified": False,  # Not applicable
                "nq_qc_accepted_qty": jig_unload_obj.nq_qc_accepted_qty,
                "nq_missing_qty": jig_unload_obj.nq_missing_qty,
                "nq_physical_qty": jig_unload_obj.nq_physical_qty,
                "nq_physical_qty_edited": False,
                "rejected_nickle_ip_stock": jig_unload_obj.unload_accepted,
                "rejected_ip_stock": jig_unload_obj.rejected_nickle_ip_stock,
                "accepted_tray_scan_status": jig_unload_obj.nq_accepted_tray_scan_status,
                "nq_pick_remarks": jig_unload_obj.nq_pick_remarks,  # Not applicable for nickel
                "previous_module": "Jig Unloading",
                "previous_module_remark": _resolve_nq_previous_unloading_remark(jig_unload_obj),
                "nq_qc_accptance": False,  # Not applicable
                "nq_accepted_tray_scan_status": False,  # Not applicable
                "nq_qc_rejection": False,  # Not applicable
                "nq_qc_few_cases_accptance": False,  # Not applicable
                "nq_onhold_picking": jig_unload_obj.nq_onhold_picking,
                "nq_draft": jig_unload_obj.nq_draft,
                "send_to_nickel_brass": jig_unload_obj.send_to_nickel_brass,
                "last_process_date_time": _nq_pick_last_updated(
                    jig_unload_obj, na_full_reject_timestamps
                ),
                "iqf_last_process_date_time": None,
                "nq_hold_lot": jig_unload_obj.nq_hold_lot,
                "nq_holding_reason": jig_unload_obj.nq_holding_reason,  # Not applicable
                "nq_release_lot": jig_unload_obj.nq_release_lot,
                "nq_release_reason": jig_unload_obj.nq_release_reason,
                "nq_hold_by": jig_unload_obj.nq_hold_by.username if jig_unload_obj.nq_hold_by else '',
                "nq_hold_at": timezone.localtime(jig_unload_obj.nq_hold_at).strftime("%d-%b-%Y %I:%M %p") if jig_unload_obj.nq_hold_at else '',
                "nq_release_by": jig_unload_obj.nq_release_by.username if jig_unload_obj.nq_release_by else '',
                "nq_release_at": timezone.localtime(jig_unload_obj.nq_release_at).strftime("%d-%b-%Y %I:%M %p") if jig_unload_obj.nq_release_at else '',
                "has_draft": jig_unload_obj.has_draft,
                "draft_type": jig_unload_obj.draft_type,
                "brass_rejection_total_qty": jig_unload_obj.brass_rejection_total_qty,
                "nq_qc_accptance": jig_unload_obj.nq_qc_accptance,
                # Additional fields from JigUnloadAfterTable
                "plating_stk_no": jig_unload_obj.plating_stk_no or "",
                "polishing_stk_no": jig_unload_obj.polish_stk_no or "",
                "category": jig_unload_obj.category or "",
                # Prefer the live current_stage SSOT (modelmasterapp/stage_service.py) so
                # this stays in sync with downstream modules (e.g. Spider Spindle) that
                # only update current_stage and not last_process_module.
                "last_process_module": jig_unload_obj.current_stage or jig_unload_obj.last_process_module or "Jig Unload",
                "combine_lot_ids": jig_unload_obj.combine_lot_ids,  # Show which lots were combined
                "unload_lot_id": jig_unload_obj.unload_lot_id,  # Additional identifier
                # Nickel-specific fields
                "nq_qc_accepted_qty_verified": jig_unload_obj.nq_qc_accepted_qty_verified,
                "audit_check": jig_unload_obj.audit_check,
                "na_last_process_date_time": jig_unload_obj.na_last_process_date_time,
            }
            # *** ENHANCED MODEL IMAGES LOGIC (Same as SpiderPickTableView) ***
            images = []
            model_master = None
            model_no = None
            # Priority 1: Get images from ModelMaster based on plating_stk_no (same as Spider view)
            if jig_unload_obj.plating_stk_no:
                plating_stk_no = str(jig_unload_obj.plating_stk_no)
                if len(plating_stk_no) >= 4:
                    model_no_prefix = plating_stk_no[:4]
                    try:
                        # Find ModelMaster where model_no matches the prefix for images
                        model_master = (
                            ModelMaster.objects.filter(model_no__startswith=model_no_prefix)
                            .prefetch_related("images")
                            .first()
                        )
                        if model_master:
                            # Get images from ModelMaster
                            for img in _sort_images_front_first_safe(model_master.images.all()):
                                if img.master_image:
                                    images.append(img.master_image.url)
                    except Exception as e:
                        logger.warning("NQ View - Error fetching ModelMaster for %s: %s", model_no_prefix, e)
            # Priority 2: Fallback to existing combine_lot_ids logic if no ModelMaster images
            if not images and data["combine_lot_ids"]:
                first_lot_id = data["combine_lot_ids"][0] if data["combine_lot_ids"] else None
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
                images = [static("assets/images/imagePlaceholder.jpg")]
            data["model_images"] = images
            # Normalize tray_type display label (NR -> Normal)
            if data.get("tray_type") and data["tray_type"].strip().lower() == "nr":
                data["tray_type"] = "Normal"
            master_data.append(data)
        # ✅ Process the data (similar logic but adapted for JigUnloadAfterTable)
        type_of_input_map = get_type_of_input_map([data.get("stock_lot_id") for data in master_data])
        page_lot_ids = [data.get("stock_lot_id") for data in master_data]
        rejection_store_by_lot = {
            r.lot_id: r for r in Nickel_QC_Rejection_ReasonStore.objects.filter(lot_id__in=page_lot_ids)
        }
        for data in master_data:
            total_IP_accpeted_quantity = data.get("total_IP_accpeted_quantity", 0)
            tray_capacity = data.get("tray_capacity", 0)
            data["vendor_location"] = (
                f"{data.get('vendor_internal', '')}_{data.get('location__location_name', '')}"
            )
            lot_id = data.get("stock_lot_id")
            data["type_of_input"] = type_of_input_map.get(lot_id, "Fresh")
            # Calculate total rejection quantity for this lot
            total_rejection_qty = 0
            rejection_store = rejection_store_by_lot.get(lot_id)
            if rejection_store and rejection_store.total_rejection_quantity:
                total_rejection_qty = rejection_store.total_rejection_quantity
            # Calculate display_accepted_qty
            if total_IP_accpeted_quantity and total_IP_accpeted_quantity > 0:
                data["display_accepted_qty"] = total_IP_accpeted_quantity
            else:
                # Already fetched above while building master_data — avoid re-querying
                jig_unload_obj = jig_unload_by_lot.get(lot_id)
                if jig_unload_obj and total_rejection_qty > 0:
                    data["display_accepted_qty"] = max(
                        jig_unload_obj.total_case_qty - total_rejection_qty, 0
                    )
                else:
                    data["display_accepted_qty"] = (
                        jig_unload_obj.total_case_qty if jig_unload_obj else 0
                    )
            # Delink logic adapted for nickel IP
            nq_physical_qty = data.get("nq_physical_qty") or 0
            is_delink_only = (
                nq_physical_qty > 0
                and total_rejection_qty >= nq_physical_qty
                and data.get("nq_onhold_picking", False)
            )
            data["is_delink_only"] = is_delink_only
            # Calculate number of trays
            display_qty = data.get("display_accepted_qty", 0)
            if tray_capacity > 0 and display_qty > 0:
                data["no_of_trays"] = math.ceil(display_qty / tray_capacity)
            else:
                data["no_of_trays"] = 0
            # Add available_qty
            if data.get("nq_physical_qty") and data.get("nq_physical_qty") > 0:
                data["available_qty"] = data.get("nq_physical_qty")
            else:
                data["available_qty"] = data.get("total_IP_accpeted_quantity", 0)
        context = {
            "master_data": master_data,
            "page_obj": page_obj,
            "paginator": paginator,
            "user": user,
            "is_admin": is_admin,
            "nq_rejection_reasons": nq_rejection_reasons,
            "pick_table_count": len(master_data),
        }
        return Response(context, template_name=self.template_name)

class NickelQcRejectTableView(APIView):
    renderer_classes = [TemplateHTMLRenderer]
    template_name = "Nickel_Inspection/NickelQc_RejectTable.html"
    def get(self, request):
        user = request.user
        # Subquery for total rejection quantity
        nickel_rejection_total_qty_subquery = Nickel_QC_Rejection_ReasonStore.objects.filter(
            lot_id=OuterRef("lot_id")
        ).values("total_rejection_quantity")[:1]
        # Zone 1 filter — only show lots belonging to Zone 1 plating colors
        allowed_color_ids = Plating_Color.objects.filter(jig_unload_zone_1=True).values_list(
            "id", flat=True
        )
        queryset = (
            JigUnloadAfterTable.objects.select_related("version", "plating_color", "polish_finish")
            .prefetch_related("location")
            .annotate(nickel_rejection_total_qty=nickel_rejection_total_qty_subquery)
            .filter(
                plating_color_id__in=allowed_color_ids,
            )
            .filter(Q(nq_qc_rejection=True) | Q(nq_qc_few_cases_accptance=True))
            .order_by("-nq_last_process_date_time", "-lot_id")
        )
        # Pagination
        page_number = request.GET.get("page", 1)
        paginator = Paginator(queryset, 10)
        page_obj = paginator.get_page(page_number)
        page_lot_ids = [obj.lot_id for obj in page_obj.object_list]
        rejection_records_by_lot = {
            r.lot_id: r
            for r in Nickel_QC_Rejection_ReasonStore.objects.filter(
                lot_id__in=page_lot_ids
            ).prefetch_related("rejection_reason")
        }
        master_data = []
        for obj in page_obj.object_list:
            data = {
                "batch_id": obj.unload_lot_id,
                "date_time": obj.created_at,
                "model_stock_no__model_no": "Combined Model",
                "plating_color": (obj.plating_color.plating_color if obj.plating_color else ""),
                "polish_finish": (obj.polish_finish.polish_finish if obj.polish_finish else ""),
                "version__version_name": (obj.version.version_name if obj.version else ""),
                "vendor_internal": "",  # Not available in JigUnloadAfterTable
                "location__location_name": _get_input_source(obj),
                "tray_type": obj.tray_type or "",
                "tray_capacity": _nq_tray_capacity(obj.tray_type) if obj.tray_type else 0,
                "plating_stk_no": obj.plating_stk_no,
                "polishing_stk_no": obj.polish_stk_no,
                "lot_id": obj.lot_id,
                "stock_lot_id": obj.lot_id,
                "last_process_module": obj.last_process_module,
                "next_process_module": obj.next_process_module,
                "nq_qc_accepted_qty_verified": obj.nq_qc_accepted_qty_verified,
                "nq_qc_rejection": obj.nq_qc_rejection,
                "nq_qc_few_cases_accptance": obj.nq_qc_few_cases_accptance,
                "nickel_rejection_total_qty": obj.nickel_rejection_total_qty,
                "nq_last_process_date_time": obj.nq_last_process_date_time,
                "nq_physical_qty": obj.nq_physical_qty,
                "nq_missing_qty": obj.nq_missing_qty,
                "nq_lot_qty": obj.nq_qc_accepted_qty or obj.total_case_qty or 0,
                "send_to_nickel_brass": obj.send_to_nickel_brass,
                "plating_stk_no_list": obj.plating_stk_no_list,
                "polish_stk_no_list": obj.polish_stk_no_list,
                "version_list": obj.version_list,
            }
            # *** ENHANCED MODEL IMAGES LOGIC (Same as other views) ***
            images = []
            model_master = None
            model_no = None
            # Priority 1: Get images from ModelMaster based on plating_stk_no
            if obj.plating_stk_no:
                plating_stk_no = str(obj.plating_stk_no)
                if len(plating_stk_no) >= 4:
                    model_no_prefix = plating_stk_no[:4]
                    try:
                        # Find ModelMaster where model_no matches the prefix for images
                        model_master = (
                            ModelMaster.objects.filter(model_no__startswith=model_no_prefix)
                            .prefetch_related("images")
                            .first()
                        )
                        if model_master:
                            # Get images from ModelMaster
                            for img in _sort_images_front_first_safe(model_master.images.all()):
                                if img.master_image:
                                    images.append(img.master_image.url)
                    except Exception as e:
                        logger.warning("Nickel Reject View - Error fetching ModelMaster for %s: %s", model_no_prefix, e)
            # Priority 2: Fallback to existing combine_lot_ids logic if no ModelMaster images
            if not images and obj.combine_lot_ids:
                first_lot_id = obj.combine_lot_ids[0] if obj.combine_lot_ids else None
                if first_lot_id:
                    total_stock_obj = TotalStockModel.objects.filter(lot_id=first_lot_id).first()
                    if total_stock_obj and total_stock_obj.batch_id:
                        batch_obj = total_stock_obj.batch_id
                        if batch_obj.model_stock_no:
                            for img in _sort_images_front_first_safe(batch_obj.model_stock_no.images.all()):
                                if img.master_image:
                                    images.append(img.master_image.url)
            # Priority 3: Use placeholder if no images found
            if not images:
                images = [static("assets/images/imagePlaceholder.jpg")]
            data["model_images"] = images
            # --- Add lot rejection remarks ---
            stock_lot_id = data.get("stock_lot_id")
            reason_store = rejection_records_by_lot.get(stock_lot_id) if stock_lot_id else None
            lot_rejected_comment = ""
            if reason_store:
                lot_rejected_comment = reason_store.lot_rejected_comment or ""
            data["lot_rejected_comment"] = lot_rejected_comment
            # --- End lot rejection remarks ---
            has_releasable_reject_trays = (
                has_unreleased_nickel_wiping_reject_trays(stock_lot_id)
            )
            data["has_releasable_reject_trays"] = has_releasable_reject_trays
            data["tray_id_in_trayid"] = has_releasable_reject_trays
            first_letters = []
            data["batch_rejection"] = False
            if stock_lot_id:
                try:
                    rejection_record = rejection_records_by_lot.get(stock_lot_id)
                    if rejection_record:
                        data["batch_rejection"] = rejection_record.batch_rejection
                        data["nickel_rejection_total_qty"] = (
                            rejection_record.total_rejection_quantity
                        )
                        reasons = rejection_record.rejection_reason.all()
                        first_letters = [
                            r.rejection_reason.strip()[0].upper()
                            for r in reasons
                            if r.rejection_reason
                        ]
                    else:
                        if (
                            "nickel_rejection_total_qty" not in data
                            or not data["nickel_rejection_total_qty"]
                        ):
                            data["nickel_rejection_total_qty"] = 0
                except Exception as e:
                    logger.error(f"❌ Error getting rejection for {stock_lot_id}: {str(e)}", exc_info=True)
                    data["nickel_rejection_total_qty"] = data.get("nickel_rejection_total_qty", 0)
            else:
                data["nickel_rejection_total_qty"] = 0
            data["rejection_reason_letters"] = first_letters
            # Calculate number of trays
            total_stock = data.get("nickel_rejection_total_qty", 0)
            tray_capacity = data.get("tray_capacity", 0)
            data["vendor_location"] = (
                f"{data.get('vendor_internal', '')}_{data.get('location__location_name', '')}"
            )
            if tray_capacity > 0 and total_stock > 0:
                data["no_of_trays"] = math.ceil(total_stock / tray_capacity)
            else:
                data["no_of_trays"] = 0
            master_data.append(data)
        type_of_input_map = get_type_of_input_map([data.get("stock_lot_id") for data in master_data])
        for data in master_data:
            data["type_of_input"] = type_of_input_map.get(data.get("stock_lot_id"), "Fresh")
        context = {
            "master_data": master_data,
            "page_obj": page_obj,
            "paginator": paginator,
            "user": user,
        }
        return Response(context, template_name=self.template_name)


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def nq_toggle_verified(request):
    """Toggle nq_qc_accepted_qty_verified flag on JigUnloadAfterTable."""
    from django.db import transaction
    lot_id = request.data.get('lot_id', '').strip()
    if not lot_id:
        return Response({'success': False, 'error': 'lot_id required'}, status=400)
    try:
        with transaction.atomic():
            obj = JigUnloadAfterTable.objects.select_for_update().filter(lot_id=lot_id).first()
            if not obj:
                return Response({'success': False, 'error': 'Lot not found'}, status=404)
            obj.nq_qc_accepted_qty_verified = True
            obj.save(update_fields=['nq_qc_accepted_qty_verified'])
            # Qty verification is the real processing action for this stage —
            # update the shared current_stage SSOT so all completed tables
            # (Day Planning, Jig Loading, etc.) reflect "Nickel Wiping".
            # obj.lot_id is this JigUnloadAfterTable row's own ID (UNLOT...),
            # which never exists in TotalStockModel — the original lot IDs
            # tracked by Day Planning/TotalStockModel are in combine_lot_ids
            # (a jig can combine multiple original lots).
            from modelmasterapp.stage_service import update_stock_stage, update_juat_stage
            for original_lot_id in (obj.combine_lot_ids or []):
                update_stock_stage(original_lot_id, 'Nickel Wiping')
            update_juat_stage(lot_id, 'Nickel Wiping')
        logger.info("[nq_toggle_verified] lot=%s user=%s", lot_id, request.user)
        return Response({'success': True, 'last_process_module': obj.last_process_module or ''})
    except Exception as e:
        logger.exception("[nq_toggle_verified] error lot=%s", lot_id)
        return Response({'success': False, 'error': 'Unable to process the request. Please verify the submitted data and try again.'}, status=500)


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def nq_hold_unhold(request):
    """Hold/Release toggle for Nickel Wiping pick table (Z1 + Z2 share this view)."""
    from django.db import transaction
    from django.utils import timezone
    lot_id = request.data.get('lot_id')
    action = request.data.get('action')  # 'hold' or 'unhold'
    remark = (request.data.get('remark', '') or '').strip()

    if not lot_id:
        return Response({'success': False, 'error': 'lot_id is required'}, status=400)
    if action not in ('hold', 'unhold'):
        return Response({'success': False, 'error': "action must be 'hold' or 'unhold'"}, status=400)
    if not remark:
        return Response({'success': False, 'error': 'Remark is required'}, status=400)
    if len(remark) > 50:
        return Response({'success': False, 'error': 'Remark must be 50 characters or less'}, status=400)

    try:
        with transaction.atomic():
            juat = JigUnloadAfterTable.objects.select_for_update().filter(lot_id=lot_id).first()
            if not juat:
                return Response({'success': False, 'error': 'Lot not found'}, status=404)

            now = timezone.now()
            if action == 'hold':
                juat.nq_hold_lot = True
                juat.nq_holding_reason = remark
                juat.nq_hold_by = request.user
                juat.nq_hold_at = now
                juat.nq_release_lot = False
                juat.nq_release_reason = ''
            else:
                juat.nq_hold_lot = False
                juat.nq_release_reason = remark
                juat.nq_release_by = request.user
                juat.nq_release_at = now
                juat.nq_release_lot = True

            juat.save(update_fields=[
                'nq_hold_lot', 'nq_holding_reason', 'nq_hold_by', 'nq_hold_at',
                'nq_release_lot', 'nq_release_reason', 'nq_release_by', 'nq_release_at',
            ])
        logger.info("[nq_hold_unhold] lot=%s action=%s user=%s", lot_id, action, request.user)
        return Response({
            'success': True, 'lot_id': lot_id, 'action': action,
            'holding_reason': juat.nq_holding_reason or '', 'release_reason': juat.nq_release_reason or '',
            'hold_lot': juat.nq_hold_lot, 'release_lot': juat.nq_release_lot,
            'hold_by': juat.nq_hold_by.username if juat.nq_hold_by else '',
            'hold_at': timezone.localtime(juat.nq_hold_at).strftime("%d-%b-%Y %I:%M %p") if juat.nq_hold_at else '',
            'release_by': juat.nq_release_by.username if juat.nq_release_by else '',
            'release_at': timezone.localtime(juat.nq_release_at).strftime("%d-%b-%Y %I:%M %p") if juat.nq_release_at else '',
            'message': f"Lot {'held' if action == 'hold' else 'released'} successfully.",
        })
    except Exception:
        logger.exception("[nq_hold_unhold] error lot=%s action=%s", lot_id, action)
        return Response({'success': False, 'error': 'Unable to process the request. Please verify the submitted data and try again.'}, status=500)


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def nq_action(request):
    """Unified NQ action handler: GET_REASONS, GET_TRAYS, ALLOCATE, SUBMIT_REJECT, SUBMIT_ACCEPT."""
    from django.db import transaction
    action = request.data.get('action', '')
    lot_id = request.data.get('lot_id', '').strip()
    if not action:
        return Response({'success': False, 'error': 'action required'}, status=400)
    if action == 'GET_REASONS':
        reasons = list(
            Nickel_QC_Rejection_Table.objects.all().order_by('id').values('id', 'rejection_reason')
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
        if lot_id:
            check_juat = JigUnloadAfterTable.objects.filter(lot_id=lot_id).first()
            if check_juat:
                series_valid, series_message, _ = validate_nickel_wiping_rejection_tray_series(
                    tray_id_val,
                    check_juat.tray_type,
                    check_juat.plating_stk_no,
                )
                if not series_valid:
                    return Response({'success': True, 'valid': False, 'message': series_message})
        # Cross-stage occupancy check — reject tray must be free across all modules
        if _nq_tray_has_current_ownership(master_tray, current_lot_id=lot_id):
            return Response({'success': True, 'valid': False, 'message': 'Tray id already occupied'})
        is_available, message = validate_nickel_wiping_rejection_tray_available(
            tray_id_val,
            current_lot_id=lot_id,
        )
        if not is_available:
            return Response({'success': True, 'valid': False, 'message': message})
        return Response({'success': True, 'valid': True, 'message': 'Valid tray'})
    if not lot_id:
        return Response({'success': False, 'error': 'lot_id required'}, status=400)
    juat = JigUnloadAfterTable.objects.filter(lot_id=lot_id).first()
    if not juat:
        return Response({'success': False, 'error': 'Lot not found'}, status=404)
    if juat.nq_hold_lot:
        return Response({'success': False, 'error': 'This lot is on hold and cannot be processed until released.'}, status=400)
    if action == 'GET_TRAYS':
        trays_qs = NickelQcTrayId.objects.filter(
            lot_id=lot_id, rejected_tray=False, delink_tray=False
        ).order_by('-top_tray', 'id')
        if trays_qs.exists():
            trays = [
                {
                    'tray_id': t.tray_id,
                    'qty': t.tray_quantity or 0,
                    'is_top': bool(t.top_tray),
                    'is_delinked': bool(t.delink_tray),
                }
                for t in trays_qs
            ]
        else:
            upstream, _ = get_upstream_tray_distribution(lot_id)
            if upstream:
                trays = [
                    {
                        'tray_id': t['tray_id'],
                        'qty': t['tray_quantity'] or 0,
                        'is_top': bool(t.get('top_tray', False)),
                        'is_delinked': bool(t.get('delink_tray', False)),
                    }
                    for t in upstream
                    if not t.get('rejected_tray', False)
                ]
            else:
                trays = []
        tray_type = (juat.tray_type or '').strip()
        tray_cap = _nq_tray_capacity(tray_type) or juat.tray_capacity or 20
        return Response({
            'success': True,
            'trays': trays,
            'total_qty': juat.total_case_qty or 0,
            'tray_capacity': tray_cap,
            'tray_type': tray_type,
            'plating_stk_no': juat.plating_stk_no or '',
        })
    if action == 'SAVE_REMARK':
        remark = (request.data.get('remark', '') or '').strip()
        if not remark:
            return Response({'success': False, 'error': 'Remark is required'}, status=400)
        if len(remark) > 100:
            return Response({'success': False, 'error': 'Remark must be 100 characters or less'}, status=400)
        juat.nq_pick_remarks = remark
        juat.save(update_fields=['nq_pick_remarks'])
        return Response({'success': True, 'message': 'Remark saved'})
    if action == 'ALLOCATE':
        try:
            rejected_qty = int(request.data.get('rejected_qty', 0))
        except (TypeError, ValueError):
            return Response({'success': False, 'error': 'Invalid rejected_qty'}, status=400)
        total_qty = juat.total_case_qty or 0
        if rejected_qty <= 0 or rejected_qty > total_qty:
            return Response({'success': False, 'error': 'rejected_qty out of range'}, status=400)
        accepted_qty = total_qty - rejected_qty
        orig_cap = _nq_tray_capacity(juat.tray_type or '') or juat.tray_capacity or 20
        rej_prefix, rej_cap = get_nickel_wiping_rejection_tray_allocation(
            juat.tray_type, juat.plating_stk_no
        )
        orig_trays = _nq_get_original_trays_for_allocation(lot_id, juat)
        allocation = build_nq_rejection_allocation(
            orig_trays,
            rejected_qty,
            rej_cap,
            accept_capacity=orig_cap,
            accepted_qty=accepted_qty,
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
            return _nq_do_submit_reject(request, lot_id, juat)
        except Exception as e:
            logger.exception("[nq_action SUBMIT_REJECT] lot=%s", lot_id)
            return Response({'success': False, 'error': 'Unable to process the request. Please verify the submitted data and try again.'}, status=500)
    if action == 'SUBMIT_ACCEPT':
        try:
            return _nq_do_submit_accept(request, lot_id, juat)
        except Exception as e:
            logger.exception("[nq_action SUBMIT_ACCEPT] lot=%s", lot_id)
            return Response({'success': False, 'error': 'Unable to process the request. Please verify the submitted data and try again.'}, status=500)
    if action == 'FULL_ACCEPT':
        try:
            return _nq_do_full_accept(request, lot_id, juat)
        except Exception as e:
            logger.exception("[nq_action FULL_ACCEPT] lot=%s", lot_id)
            return Response({'success': False, 'error': 'Unable to process the request. Please verify the submitted data and try again.'}, status=500)
    if action == 'SAVE_DRAFT':
        from django.db import transaction as _tx
        draft_data = _nq_build_draft_snapshot(request.data.get('draft_data', {}), juat, request)
        with _tx.atomic():
            Nickel_QC_Draft_Store.objects.update_or_create(
                lot_id=lot_id,
                draft_type='batch_rejection',
                defaults={
                    'batch_id': juat.unload_lot_id or lot_id,
                    'user': request.user,
                    'draft_data': draft_data,
                },
            )
            juat.nq_draft = True
            update_fields = ['nq_draft']
            if juat.nq_onhold_picking and not juat.nq_qc_rejection and not juat.nq_qc_few_cases_accptance:
                juat.nq_onhold_picking = False
                update_fields.append('nq_onhold_picking')
            juat.save(update_fields=update_fields)
        logger.info("[nq_action SAVE_DRAFT] lot=%s user=%s", lot_id, request.user)
        return Response({'success': True, 'isDraft': True, 'status': 'Draft', 'draft_data': draft_data})
    if action == 'GET_DRAFT':
        draft = Nickel_QC_Draft_Store.objects.filter(lot_id=lot_id, draft_type='batch_rejection').first()
        if draft:
            return Response({'success': True, 'has_draft': True, 'isDraft': True, 'status': 'Draft', 'draft_data': _nq_build_draft_snapshot(draft.draft_data, juat, request)})
        return Response({'success': True, 'has_draft': False, 'draft_data': {}})
    return Response({'success': False, 'error': f'Unknown action: {action}'}, status=400)


def _nq_generate_lot_id():
    """Generate a unique LID-format lot ID for NQ partial submission records."""
    from datetime import datetime
    import time
    for _ in range(10):
        now = datetime.now()
        lid = f"LID{now.strftime('%Y%m%d%H%M%S')}{str(now.microsecond).zfill(6)}"
        if not NickelQC_PartialRejectLot.objects.filter(new_lot_id=lid).exists():
            return lid
        time.sleep(0.001)
    now = datetime.now()
    return f"LID{now.strftime('%Y%m%d%H%M%S')}{str(now.microsecond).zfill(6)}"


def _nw_generate_record_id(prefix, model_class):
    """Generate a unique prefixed record ID for NickelWiping submission records."""
    from datetime import datetime
    import time
    for _ in range(10):
        now = datetime.now()
        rid = f"{prefix}{now.strftime('%Y%m%d%H%M%S')}{str(now.microsecond).zfill(6)}"
        if not model_class.objects.filter(record_lot_id=rid).exists():
            return rid
        time.sleep(0.001)
    now = datetime.now()
    return f"{prefix}{now.strftime('%Y%m%d%H%M%S')}{str(now.microsecond).zfill(6)}"


def _nq_completed_event_rows(allowed_color_ids):
    """
    Build one row per Nickel Wiping completion event (Full Accept / Full
    Reject / Partial Accept child lot), for the Completed table listing.
    Serves Z1 (NQCompletedView) and Z2 (NQ_Zone_CompletedView).

    A lot's JigUnloadAfterTable row is reused across rework passes — e.g. a
    lot returned to Nickel Wiping after a Nickel Audit rejection resubmits
    under the same lot_id/row — so listing live JigUnloadAfterTable objects
    (one per lot) collapses multiple completed passes into a single row.
    The NickelWiping_*Record tables are append-only (one row per submission,
    never updated — see _nq_do_full_accept / _nq_do_submit_accept /
    _nq_do_submit_reject), so listing those instead preserves every
    completed pass as its own row.
    """
    import copy

    fa_qs = list(NickelWiping_FullAcceptRecord.objects.all())
    fr_qs = list(NickelWiping_FullRejectRecord.objects.all())
    pa_qs = list(NickelWiping_PartialAcceptRecord.objects.order_by('created_at'))

    needed_lot_ids = set()
    for r in fa_qs:
        needed_lot_ids.add(r.source_lot_id)
    for r in fr_qs:
        needed_lot_ids.add(r.source_lot_id)
    for r in pa_qs:
        needed_lot_ids.add(r.source_lot_id)
        needed_lot_ids.add(r.child_lot_id or r.source_lot_id)

    juat_by_lot = {
        j.lot_id: j for j in JigUnloadAfterTable.objects
        .select_related('version', 'plating_color', 'polish_finish')
        .prefetch_related('location')
        .filter(lot_id__in=needed_lot_ids, plating_color_id__in=allowed_color_ids)
    }

    def make_row(
        base, *, lot_id, total_qty, accepted_qty, rejected_qty, event_time,
        no_of_trays, kind, record_lot_id
    ):
        row = copy.copy(base)
        row.lot_id = lot_id
        row.total_case_qty = total_qty
        row.nq_qc_accepted_qty = accepted_qty
        row.nq_last_process_date_time = event_time
        row.nq_qc_accptance = kind == 'full_accept'
        row.nq_qc_rejection = kind == 'full_reject'
        row.nq_qc_few_cases_accptance = kind == 'partial'
        row._nw_rejected_qty = rejected_qty
        row._nw_no_of_trays = no_of_trays
        row._nw_event_type = kind
        row._nw_record_lot_id = record_lot_id
        return row

    events = []
    for r in fa_qs:
        base = juat_by_lot.get(r.source_lot_id)
        if not base:
            continue
        events.append(make_row(
            base, lot_id=r.source_lot_id, total_qty=r.total_qty or 0,
            accepted_qty=r.total_qty or 0, rejected_qty=0,
            event_time=r.created_at, no_of_trays=len(r.accept_trays or []),
            kind='full_accept', record_lot_id=r.record_lot_id,
        ))
    for r in fr_qs:
        base = juat_by_lot.get(r.source_lot_id)
        if not base:
            continue
        events.append(make_row(
            base, lot_id=r.source_lot_id, total_qty=r.total_qty or 0,
            accepted_qty=0, rejected_qty=r.rejected_qty or 0,
            event_time=r.created_at, no_of_trays=len(r.reject_trays or []),
            kind='full_reject', record_lot_id=r.record_lot_id,
        ))

    # A partial submission is one completed Nickel Wiping processing event.
    # Keep the accepted child lot_id as the continuation identifier, but show
    # the quantities from the actual parent processing event in Completed:
    # original lot qty = accepted + rejected, Accept Qty = accepted,
    # Reject Qty = rejected. The child still carries only the accepted qty
    # downstream to Nickel Audit; this affects Completed-history display only.
    for pa in pa_qs:
        display_lot_id = pa.child_lot_id or pa.source_lot_id
        base = juat_by_lot.get(display_lot_id) or juat_by_lot.get(pa.source_lot_id)
        if not base:
            continue
        partial_total_qty = (pa.accepted_qty or 0) + (pa.rejected_qty or 0)
        events.append(make_row(
            base, lot_id=display_lot_id,
            total_qty=partial_total_qty,
            accepted_qty=pa.accepted_qty or 0,
            rejected_qty=pa.rejected_qty or 0,
            event_time=pa.created_at, no_of_trays=len(pa.accept_trays or []),
            kind='partial', record_lot_id=pa.record_lot_id,
        ))

    events.sort(key=lambda r: r.nq_last_process_date_time or timezone.now(), reverse=True)
    return events


def _nq_do_full_accept(request, lot_id, juat):
    """
    Persist FULL acceptance for a NQ lot.
    Auto-resolves trays from NickelQcTrayId or upstream.
    Creates NickelQC_Submission record and sets nq_qc_accptance=True.
    """
    from django.db import transaction
    import django.utils.timezone as tz
    total_qty = juat.total_case_qty or 0
    # Resolve trays — sorted by tray_id ascending (smallest = top tray)
    trays_qs = NickelQcTrayId.objects.filter(
        lot_id=lot_id, rejected_tray=False, delink_tray=False
    ).order_by('tray_id')
    if trays_qs.exists():
        trays = [
            {'tray_id': t.tray_id, 'qty': t.tray_quantity or 0, 'is_top': i == 0}
            for i, t in enumerate(trays_qs)
        ]
    else:
        upstream, _ = get_upstream_tray_distribution(lot_id)
        raw_trays = sorted(
            [t for t in (upstream or []) if not t.get('rejected_tray') and not t.get('delink_tray')],
            key=lambda t: t['tray_id']
        )
        trays = [
            {'tray_id': t['tray_id'], 'qty': t['tray_quantity'] or 0, 'is_top': i == 0}
            for i, t in enumerate(raw_trays)
        ]
    with transaction.atomic():
        for at in trays:
            tid = at['tray_id']
            NickelQcTrayId.objects.update_or_create(
                lot_id=lot_id,
                tray_id=tid,
                defaults={
                    'tray_quantity': at['qty'],
                    'top_tray': at['is_top'],
                    'tray_type': juat.tray_type or '',
                    'tray_capacity': juat.tray_capacity or 20,
                },
            )
            _nq_upsert_accepted_tray_store(lot_id, tid, at['qty'], request.user)
        NickelQC_Submission.objects.create(
            lot_id=lot_id,
            submission_type='FULL_ACCEPT',
            total_lot_qty=total_qty,
            accepted_qty=total_qty,
            rejected_qty=0,
            accept_trays_data=trays,
            created_by=request.user,
        )
        # ERR3: Save tray scan data to independent NickelWiping_FullAcceptRecord.
        # Always insert — source_lot_id can repeat across rework passes (e.g. a lot
        # returned from Nickel Audit reuses the same lot_id), so update_or_create()
        # here would silently overwrite the prior pass's history instead of
        # preserving it as its own completed record.
        NickelWiping_FullAcceptRecord.objects.create(
            source_lot_id=lot_id,
            record_lot_id=_nw_generate_record_id('NWFA', NickelWiping_FullAcceptRecord),
            total_qty=total_qty,
            accept_trays=trays,
            delink_trays=[],
            created_by=request.user,
        )
        juat.nq_qc_accptance = True
        juat.nq_qc_accepted_qty = total_qty
        juat.nq_draft = False
        juat.nq_onhold_picking = False
        juat.nq_last_process_date_time = tz.now()
        juat.last_process_module = 'Nickel QC'
        juat.current_stage = 'Nickel Wiping'
        juat.save(update_fields=[
            'nq_qc_accptance', 'nq_qc_accepted_qty',
            'nq_draft', 'nq_onhold_picking',
            'nq_last_process_date_time', 'last_process_module', 'current_stage',
        ])
        _nq_clear_draft_state(lot_id)
    logger.info("[nq_full_accept] lot=%s user=%s qty=%d", lot_id, request.user, total_qty)
    return Response({'success': True})


def _nq_do_submit_reject(request, lot_id, juat):
    """Persist rejection for a NQ lot. Called from nq_action."""
    from django.db import transaction
    data = request.data
    reason_ids = data.get('reason_ids', [])
    try:
        rejected_qty = int(data.get('rejected_qty', 0))
    except (TypeError, ValueError):
        return Response({'success': False, 'error': 'Invalid rejected_qty'}, status=400)
    reject_trays = data.get('reject_trays', [])   # [{tray_id, qty}]
    accept_trays = data.get('accept_trays', [])   # [{tray_id, qty, is_top}]
    submitted_delink_trays = data.get('delink_trays', [])
    remarks = (data.get('remarks', '') or '').strip()
    full_lot_rejection = str(data.get('full_lot_rejection', '')).strip().lower() in (
        '1', 'true', 'yes', 'on'
    )
    if full_lot_rejection and not remarks:
        return Response(
            {'success': False, 'error': 'Remarks mandatory for full lot rejection.'},
            status=400,
        )
    total_qty = juat.total_case_qty or 0
    if full_lot_rejection:
        rejected_qty = total_qty
    if rejected_qty <= 0:
        return Response({'success': False, 'error': 'rejected_qty required'}, status=400)
    if rejected_qty > total_qty:
        return Response({'success': False, 'error': 'rejected_qty exceeds lot qty'}, status=400)
    accepted_qty = total_qty - rejected_qty
    is_partial = accepted_qty > 0
    if not is_partial and not remarks:
        return Response(
            {'success': False, 'error': 'Remarks mandatory for full lot rejection.'},
            status=400,
        )
    # Full lot rejection (rejected_qty == total_qty) does not require picking a
    # rejection reason — only a partial rejection needs a reason to explain why
    # the non-rejected remainder is being split out.
    if is_partial and not reason_ids:
        return Response({'success': False, 'error': 'reason_ids required for partial rejection'}, status=400)
    _allowed_prefix, rej_cap = get_nickel_wiping_rejection_tray_allocation(
        juat.tray_type, juat.plating_stk_no
    )
    orig_cap = _nq_tray_capacity(juat.tray_type or '') or juat.tray_capacity or 20
    orig_trays = _nq_get_original_trays_for_allocation(lot_id, juat, create_missing=True)

    if is_partial:
        # Partial rejection still uses the existing tray allocation workflow.
        allocation = build_nq_rejection_allocation(
            orig_trays,
            rejected_qty,
            rej_cap,
            accept_capacity=orig_cap,
            accepted_qty=accepted_qty,
        )

        # A freed original tray (one the allocation would otherwise require to be
        # delinked) may instead be reused directly as its own reject container.
        delink_slot_ids = {slot['tray_id'] for slot in allocation['delink_slots']}
        reused_tray_ids = {
            (rt.get('tray_id') or '').strip().upper()
            for rt in reject_trays
            if (rt.get('tray_id') or '').strip().upper() in delink_slot_ids
        }
        delink_slots_required = [
            slot for slot in allocation['delink_slots']
            if slot['tray_id'] not in reused_tray_ids
        ]

        try:
            reject_trays = normalize_reject_trays(reject_trays, allocation['reject_slots'])
            delink_trays_snapshot = normalize_operator_delink_trays(
                submitted_delink_trays,
                delink_slots_required,
                orig_trays,
            )
            accept_trays = normalize_accept_trays(
                accept_trays,
                allocation['accept_auto_trays'],
                original_trays=orig_trays,
                delink_trays=delink_trays_snapshot,
                accepted_qty=accepted_qty,
                accept_capacity=orig_cap,
            )
            validate_original_tray_coverage(
                accept_trays, delink_trays_snapshot, orig_trays, reject_trays=reject_trays,
            )
        except ValueError as exc:
            return Response({'success': False, 'error': str(exc)}, status=400)

        # A tray ID may be used in only one section for a partial submission.
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
    else:
        # Full-lot rejection is a lot-level rejection. The operator must not be
        # asked to allocate/scan Reject, Delink, or Accept trays. Keep the
        # existing original trays attached to the lot and persist an empty tray
        # allocation snapshot for this rejection event.
        reject_trays = []
        accept_trays = []
        delink_trays_snapshot = []
        reused_tray_ids = set()
        reject_ids = set()
        delink_ids = set()
        accept_ids = set()

    for rt in reject_trays:
        tid = (rt.get('tray_id') or '').upper()
        series_valid, series_message, _ = validate_nickel_wiping_rejection_tray_series(
            tid,
            juat.tray_type,
            juat.plating_stk_no,
        )
        if not series_valid:
            return Response(
                {'success': False, 'error': series_message},
                status=400,
            )
        if int(rt.get('qty', 0)) > rej_cap:
            return Response(
                {'success': False, 'error': f'Reject tray {tid} qty exceeds max {rej_cap}'},
                status=400,
            )
    with transaction.atomic():
        for tid in sorted(reject_ids):
            is_available, message = validate_nickel_wiping_rejection_tray_available(
                tid,
                current_lot_id=lot_id,
                lock_master=True,
            )
            if not is_available:
                return Response({'success': False, 'error': message}, status=400)

        reasons_qs = Nickel_QC_Rejection_Table.objects.filter(id__in=reason_ids)
        if reason_ids and not reasons_qs.exists():
            return Response({'success': False, 'error': 'Invalid rejection reason'}, status=400)
        if not reasons_qs.exists():
            # Full lot rejection with no reason picked: Nickel_QC_Rejected_TrayScan.rejection_reason
            # is a mandatory FK, so fall back to a single system placeholder reason instead of
            # forcing the operator to choose one for a full-lot reject.
            full_reject_reason, _ = Nickel_QC_Rejection_Table.objects.get_or_create(
                rejection_reason='Full Lot Rejection',
            )
            reasons_qs = Nickel_QC_Rejection_Table.objects.filter(id=full_reject_reason.id)
        # Save or update rejection reason store
        reason_store, _ = Nickel_QC_Rejection_ReasonStore.objects.update_or_create(
            lot_id=lot_id,
            defaults={
                'total_rejection_quantity': rejected_qty,
                'batch_rejection': not is_partial,
                'lot_rejected_comment': remarks,
                'user': request.user,
            },
        )
        reason_store.rejection_reason.set(reasons_qs)
        # Save each reject tray scan
        for rt in reject_trays:
            tid = rt.get('tray_id', '').strip()
            qty = int(rt.get('qty', 0))
            if not tid or qty <= 0:
                continue
            Nickel_QC_Rejected_TrayScan.objects.update_or_create(
                lot_id=lot_id,
                rejected_tray_id=tid,
                defaults={
                    'rejected_tray_quantity': qty,
                    'rejection_reason': reasons_qs.first(),
                    'user': request.user,
                },
            )
        orig_trays_qs = NickelQcTrayId.objects.filter(
            lot_id=lot_id, rejected_tray=False, delink_tray=False
        )
        # Determine which accept tray IDs to assign
        accept_tray_ids = {at['tray_id']: at for at in accept_trays}
        delink_tray_ids = {tray['tray_id'] for tray in delink_trays_snapshot}
        reject_tray_qty_by_id = {rt['tray_id']: int(rt.get('qty', 0)) for rt in reject_trays}
        # Delink original trays that are no longer needed
        for tray_obj in orig_trays_qs:
            if tray_obj.tray_id in accept_tray_ids:
                at = accept_tray_ids[tray_obj.tray_id]
                tray_obj.tray_quantity = int(at.get('qty', 0))
                tray_obj.top_tray = bool(at.get('is_top', False))
                tray_obj.save(update_fields=['tray_quantity', 'top_tray'])
            elif tray_obj.tray_id in reused_tray_ids:
                # Freed original tray reused directly as its own reject container —
                # it holds the rejected pieces now, so it stays owned by this lot
                # as a rejected tray rather than being freed via delink.
                tray_obj.rejected_tray = True
                tray_obj.tray_quantity = reject_tray_qty_by_id.get(tray_obj.tray_id, tray_obj.tray_quantity)
                tray_obj.top_tray = False
                tray_obj.save(update_fields=['rejected_tray', 'tray_quantity', 'top_tray'])
            elif tray_obj.tray_id in delink_tray_ids:
                delink_qty = tray_obj.tray_quantity
                tray_obj.delink_tray = True
                tray_obj.delink_tray_qty = delink_qty
                tray_obj.tray_quantity = 0
                # A delinked tray must never keep the "top accept tray" flag —
                # otherwise it resurfaces (with stale is_top/is_delinked data)
                # the next time trays are fetched for this lot.
                tray_obj.top_tray = False
                tray_obj.save(update_fields=['delink_tray', 'delink_tray_qty', 'tray_quantity', 'top_tray'])
                release_tray_master_for_reuse(tray_obj.tray_id, delink_qty=delink_qty)
        # Save accepted trays that are new (not existing NickelQcTrayId)
        existing_ids = set(
            NickelQcTrayId.objects.filter(lot_id=lot_id).values_list('tray_id', flat=True)
        )
        for at in accept_trays:
            tid = (at.get('tray_id') or '').strip()
            qty = int(at.get('qty', 0))
            if not tid or qty <= 0 or tid in existing_ids:
                continue
            NickelQcTrayId.objects.create(
                lot_id=lot_id,
                tray_id=tid,
                tray_quantity=qty,
                top_tray=bool(at.get('is_top', False)),
                tray_type=juat.tray_type or '',
                tray_capacity=juat.tray_capacity or 20,
            )
        # Save accepted tray store
        for at in accept_trays:
            tid = (at.get('tray_id') or '').strip()
            qty = int(at.get('qty', 0))
            if not tid or qty <= 0:
                continue
            _nq_upsert_accepted_tray_store(lot_id, tid, qty, request.user)
        # Update JigUnloadAfterTable flags
        import django.utils.timezone as tz
        juat.nq_qc_rejection = not is_partial
        juat.nq_qc_few_cases_accptance = is_partial
        juat.nq_draft = False
        juat.nq_onhold_picking = False
        juat.nq_last_process_date_time = tz.now()
        juat.last_process_module = 'Nickel QC'
        juat.current_stage = 'Nickel Wiping'
        if is_partial:
            juat.nq_qc_accepted_qty = accepted_qty
        juat.save(update_fields=[
            'nq_qc_rejection', 'nq_qc_few_cases_accptance',
            'nq_draft', 'nq_onhold_picking',
            'nq_last_process_date_time', 'last_process_module', 'nq_qc_accepted_qty', 'current_stage',
        ])
        _nq_clear_draft_state(lot_id)
        # ── Create NickelQC_Submission record ──────────────────────────────────
        submission_type = 'PARTIAL' if is_partial else 'FULL_REJECT'
        reason_data = {
            str(r.id): {'reason': r.rejection_reason}
            for r in reasons_qs
        }
        submission = NickelQC_Submission.objects.create(
            lot_id=lot_id,
            submission_type=submission_type,
            total_lot_qty=total_qty,
            accepted_qty=accepted_qty,
            rejected_qty=rejected_qty,
            accept_trays_data=accept_trays,
            reject_trays_data=reject_trays,
            created_by=request.user,
        )
        # ── For partial: create child JigUnloadAfterTable row (accepted portion) ──
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
                tray_capacity=juat.tray_capacity,
                nq_qc_accptance=True,
                nq_qc_accepted_qty=accepted_qty,
                nq_last_process_date_time=tz.now(),
                last_process_module='Nickel QC',
            )
            child_juat.save()
            # Store accepted trays under the child lot
            for at in accept_trays:
                tid = (at.get('tray_id') or '').strip()
                qty = int(at.get('qty', 0))
                if tid and qty > 0:
                    NickelQcTrayId.objects.update_or_create(
                        lot_id=child_juat.lot_id,
                        tray_id=tid,
                        defaults={
                            'tray_quantity': qty,
                            'top_tray': bool(at.get('is_top', False)),
                            'tray_type': juat.tray_type or '',
                            'tray_capacity': juat.tray_capacity or 20,
                        },
                    )
            # Create NickelQC_PartialAcceptLot record
            NickelQC_PartialAcceptLot.objects.create(
                new_lot_id=child_juat.lot_id,
                parent_lot_id=lot_id,
                parent_submission=submission,
                accepted_qty=accepted_qty,
                trays_snapshot=accept_trays,
                created_by=request.user,
            )
            # Create NickelQC_PartialRejectLot record
            NickelQC_PartialRejectLot.objects.create(
                new_lot_id=_nq_generate_lot_id(),
                parent_lot_id=lot_id,
                parent_submission=submission,
                rejected_qty=rejected_qty,
                rejection_reasons=reason_data,
                trays_snapshot=reject_trays,
                remarks=remarks,
                created_by=request.user,
            )
            # ERR3: Save independent NickelWiping records for partial submission.
            # Always insert — see note on the Full Accept record above: source_lot_id
            # is not a unique key, so update_or_create() would overwrite an earlier
            # completed record for the same lot instead of preserving its history.
            NickelWiping_PartialAcceptRecord.objects.create(
                source_lot_id=lot_id,
                record_lot_id=_nw_generate_record_id('NWPA', NickelWiping_PartialAcceptRecord),
                child_lot_id=child_juat.lot_id,
                accepted_qty=accepted_qty,
                rejected_qty=rejected_qty,
                accept_trays=accept_trays,
                delink_trays=delink_trays_snapshot,
                created_by=request.user,
            )
            NickelWiping_PartialRejectRecord.objects.create(
                source_lot_id=lot_id,
                record_lot_id=_nw_generate_record_id('NWPR', NickelWiping_PartialRejectRecord),
                rejected_qty=rejected_qty,
                reject_trays=reject_trays,
                reject_reasons=reason_data,
                remarks=remarks,
                created_by=request.user,
            )
        else:
            # ERR3: Full Reject — save NickelWiping_FullRejectRecord (always insert; see above)
            NickelWiping_FullRejectRecord.objects.create(
                source_lot_id=lot_id,
                record_lot_id=_nw_generate_record_id('NWFR', NickelWiping_FullRejectRecord),
                total_qty=total_qty,
                rejected_qty=rejected_qty,
                reject_trays=reject_trays,
                delink_trays=delink_trays_snapshot,
                reject_reasons=reason_data,
                remarks=remarks,
                created_by=request.user,
            )
    logger.info(
        "[nq_submit_reject] lot=%s rej_qty=%d partial=%s user=%s",
        lot_id, rejected_qty, is_partial, request.user,
    )
    return Response({'success': True, 'is_partial': is_partial})


def _nq_do_submit_accept(request, lot_id, juat):
    """Persist full acceptance for a NQ lot. Called from nq_action."""
    from django.db import transaction
    import django.utils.timezone as tz
    accept_trays = request.data.get('accept_trays', [])
    if not accept_trays:
        return Response({'success': False, 'error': 'accept_trays required'}, status=400)
    with transaction.atomic():
        for at in accept_trays:
            tid = (at.get('tray_id') or '').strip()
            qty = int(at.get('qty', 0))
            if not tid or qty <= 0:
                continue
            NickelQcTrayId.objects.update_or_create(
                lot_id=lot_id,
                tray_id=tid,
                defaults={
                    'tray_quantity': qty,
                    'top_tray': bool(at.get('is_top', False)),
                    'tray_type': juat.tray_type or '',
                    'tray_capacity': juat.tray_capacity or 20,
                },
            )
            _nq_upsert_accepted_tray_store(lot_id, tid, qty, request.user)
        juat.nq_qc_accptance = True
        juat.nq_qc_accepted_qty = juat.total_case_qty
        juat.nq_draft = False
        juat.nq_onhold_picking = False
        juat.nq_last_process_date_time = tz.now()
        juat.last_process_module = 'Nickel QC'
        juat.current_stage = 'Nickel Wiping'
        juat.save(update_fields=[
            'nq_qc_accptance', 'nq_qc_accepted_qty',
            'nq_draft', 'nq_onhold_picking',
            'nq_last_process_date_time', 'last_process_module', 'current_stage',
        ])
        _nq_clear_draft_state(lot_id)
        # ERR3: Save independent NickelWiping_FullAcceptRecord for view icon (always insert; see note above)
        NickelWiping_FullAcceptRecord.objects.create(
            source_lot_id=lot_id,
            record_lot_id=_nw_generate_record_id('NWFA', NickelWiping_FullAcceptRecord),
            total_qty=juat.total_case_qty or 0,
            accept_trays=accept_trays,
            delink_trays=[],
            created_by=request.user,
        )
    logger.info("[nq_submit_accept] lot=%s user=%s", lot_id, request.user)
    return Response({'success': True})


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def nq_delink_selected_trays(request):
    """Release the rejected trays for the selected Nickel Wiping lot(s).

    The Reject Table selection is lot-level, but reject trays are persisted
    primarily in the submission/reject snapshots. They are not guaranteed to
    exist as ``NickelQcTrayId(rejected_tray=True)`` rows. Resolve the exact
    reject tray IDs from the latest rejection event, then release only those
    trays. Accepted trays belonging to the same lot are left untouched.
    """
    from django.db import transaction

    stock_lot_ids = request.data.get('stock_lot_ids', [])
    if not stock_lot_ids:
        return Response({'success': False, 'error': 'stock_lot_ids required'}, status=400)

    updated = 0
    lots_processed = 0

    try:
        with transaction.atomic():
            for lot_id in stock_lot_ids:
                reject_rows = get_current_nickel_wiping_reject_trays(lot_id)

                juat = (
                    JigUnloadAfterTable.objects
                    .filter(lot_id=lot_id)
                    .only('tray_type', 'tray_capacity')
                    .first()
                )

                for row in reject_rows:
                    tray_id = row.get('tray_id')
                    snapshot_qty = _nq_int(row.get('qty'))
                    if not tray_id or snapshot_qty <= 0:
                        continue

                    tray_obj = (
                        NickelQcTrayId.objects.select_for_update()
                        .filter(lot_id=lot_id, tray_id__iexact=tray_id)
                        .first()
                    )
                    if tray_obj is not None and tray_obj.delink_tray:
                        continue
                    master_already_released = is_nickel_wiping_tray_master_released(tray_id)
                    is_available, availability_message = (
                        validate_nickel_wiping_rejection_tray_available(
                            tray_id,
                            current_lot_id=lot_id,
                            lock_master=True,
                        )
                    )
                    if not is_available:
                        logger.warning(
                            "[nq_delink_selected_trays] blocked release lot=%s tray=%s reason=%s",
                            lot_id,
                            tray_id,
                            availability_message,
                        )
                        return Response(
                            {
                                'success': False,
                                'error': availability_message or 'Tray is occupied.',
                            },
                            status=400,
                        )

                    # A reject tray can be a reused original tray (and therefore
                    # already have a NickelQcTrayId row) or a newly scanned blue
                    # reject tray that exists only in the rejection snapshots.
                    # Handle both cases and persist a delink snapshot for the UI.
                    if tray_obj is not None:
                        delink_qty = _nq_int(tray_obj.tray_quantity) or snapshot_qty
                        tray_obj.rejected_tray = True
                        tray_obj.delink_tray = True
                        tray_obj.delink_tray_qty = delink_qty
                        tray_obj.tray_quantity = 0
                        tray_obj.top_tray = False
                        tray_obj.save(update_fields=[
                            'rejected_tray',
                            'delink_tray',
                            'delink_tray_qty',
                            'tray_quantity',
                            'top_tray',
                        ])
                    else:
                        delink_qty = snapshot_qty
                        NickelQcTrayId.objects.create(
                            lot_id=lot_id,
                            tray_id=tray_id,
                            tray_quantity=0,
                            top_tray=False,
                            tray_type=(juat.tray_type if juat else '') or '',
                            tray_capacity=(juat.tray_capacity if juat else 0) or 0,
                            user=request.user,
                            rejected_tray=True,
                            delink_tray=True,
                            delink_tray_qty=delink_qty,
                        )

                    if (
                        not master_already_released
                        and release_tray_master_for_reuse(tray_id, delink_qty=delink_qty)
                    ):
                        updated += 1

                lots_processed += 1

        logger.info(
            "[nq_delink_selected_trays] user=%s lots=%s freed_reject_trays=%d",
            request.user,
            stock_lot_ids,
            updated,
        )
        return Response({
            'success': True,
            'updated': updated,
            'lots_processed': lots_processed,
        })
    except Exception:
        logger.exception("[nq_delink_selected_trays] error")
        return Response(
            {
                'success': False,
                'error': 'Unable to process the request. Please verify the submitted data and try again.',
            },
            status=500,
        )


@method_decorator(login_required, name='dispatch')
class NQCompletedView(APIView):
    renderer_classes = [TemplateHTMLRenderer]
    template_name = 'Nickel_Inspection/NI_Completed.html'

    def get(self, request):
        from django.utils import timezone as tz
        user = request.user
        allowed_color_ids = Plating_Color.objects.filter(
            jig_unload_zone_1=True
        ).values_list('id', flat=True)

        # ERR4 Fix: list one row per completed submission event (append-only
        # NickelWiping_*Record history), not one row per live
        # JigUnloadAfterTable object — a lot's row is reused across rework
        # passes (e.g. returned from Nickel Audit), so the latter silently
        # collapsed multiple completed passes into a single row.
        rows = _nq_completed_event_rows(allowed_color_ids)

        from_date = request.GET.get('from_date', '')
        to_date = request.GET.get('to_date', '')
        if from_date and to_date:
            try:
                from datetime import datetime as _dt
                from_date_obj = _dt.strptime(from_date, '%Y-%m-%d').date()
                to_date_obj = _dt.strptime(to_date, '%Y-%m-%d').date()
                rows = [
                    r for r in rows
                    if r.nq_last_process_date_time
                    and from_date_obj <= timezone.localtime(r.nq_last_process_date_time).date() <= to_date_obj
                ]
            except ValueError:
                pass

        page_number = request.GET.get('page', 1)
        paginator = Paginator(rows, 10)
        page_obj = paginator.get_page(page_number)

        master_data = []
        for obj in page_obj.object_list:
            total_rejection_qty = getattr(obj, '_nw_rejected_qty', 0)

            data = {
                'batch_id': obj.unload_lot_id,
                'lot_id': obj.lot_id,
                'date_time': obj.created_at,
                'last_process_date_time': obj.nq_last_process_date_time,
                'na_last_process_date_time': obj.na_last_process_date_time,
                'plating_stk_no': obj.plating_stk_no or '',
                'polishing_stk_no': obj.polish_stk_no or '',
                'plating_color': obj.plating_color.plating_color if obj.plating_color else '',
                'polish_finish': obj.polish_finish.polish_finish if obj.polish_finish else '',
                'version__version_name': obj.version.version_name if obj.version else '',
                'location__location_name': _get_input_source(obj),
                'tray_type': obj.tray_type or '',
                'tray_capacity': obj.tray_capacity or 0,
                'category': obj.category or '',
                # Prefer the live current_stage SSOT (modelmasterapp/stage_service.py) so
                # this stays in sync with downstream modules (e.g. Spider Spindle) that
                # only update current_stage and not last_process_module.
                'last_process_module': obj.current_stage or obj.last_process_module or '',
                'combine_lot_ids': obj.combine_lot_ids,
                'unload_lot_id': obj.unload_lot_id,
                'stock_lot_id': obj.lot_id,
                'total_IP_accpeted_quantity': obj.total_case_qty,
                'nq_qc_accepted_qty': obj.nq_qc_accepted_qty,
                'nq_missing_qty': obj.nq_missing_qty,
                'nq_physical_qty': obj.nq_physical_qty,
                'nq_qc_accptance': obj.nq_qc_accptance,
                'nq_qc_rejection': obj.nq_qc_rejection,
                'nq_qc_few_cases_accptance': obj.nq_qc_few_cases_accptance,
                'nq_onhold_picking': obj.nq_onhold_picking,
                'nq_qc_accepted_qty_verified': obj.nq_qc_accepted_qty_verified,
                'nq_hold_lot': obj.nq_hold_lot,
                'nq_release_lot': obj.nq_release_lot,
                'nq_holding_reason': obj.nq_holding_reason,
                'nq_release_reason': obj.nq_release_reason,
                'nq_draft': obj.nq_draft,
                'nq_pick_remarks': obj.nq_pick_remarks,
                'audit_check': obj.audit_check,
                'accepted_tray_scan_status': obj.nq_accepted_tray_scan_status,
                'rejected_ip_stock': obj.rejected_nickle_ip_stock,
                'accepted_Ip_stock': obj.unload_accepted,
                'few_cases_accepted_ip_stock': obj.nq_qc_few_cases_accptance,
                'vendor_internal': '',
                'available_qty': obj.nq_physical_qty or obj.total_case_qty or 0,
                'nickel_rejection_total_qty': total_rejection_qty,
                'brass_rejection_total_qty': total_rejection_qty,
                'nw_event_type': getattr(obj, '_nw_event_type', ''),
                'nw_record_lot_id': getattr(obj, '_nw_record_lot_id', ''),
            }

            display_qty = obj.total_case_qty or 0
            tray_capacity = obj.tray_capacity or _nq_tray_capacity(obj.tray_type or '') or 0
            # Completed history must show the event's accepted quantity, not
            # the event's original lot quantity. For partial submissions these
            # differ (e.g. Lot Qty 64, Accept Qty 44, Reject Qty 20).
            data['display_accepted_qty'] = obj.nq_qc_accepted_qty or 0
            no_of_trays_override = getattr(obj, '_nw_no_of_trays', 0)
            data['no_of_trays'] = no_of_trays_override or (ceil(display_qty / tray_capacity) if display_qty > 0 and tray_capacity > 0 else 0)

            images = []
            if obj.plating_stk_no:
                prefix = str(obj.plating_stk_no)[:4]
                mm = ModelMaster.objects.filter(model_no__startswith=prefix).prefetch_related('images').first()
                if mm:
                    images = [img.master_image.url for img in _sort_images_front_first_safe(mm.images.all()) if img.master_image]
            if not images and obj.combine_lot_ids:
                first_lid = obj.combine_lot_ids[0] if obj.combine_lot_ids else None
                if first_lid:
                    ts = TotalStockModel.objects.filter(lot_id=first_lid).first()
                    if ts and ts.batch_id and ts.batch_id.model_stock_no:
                        images = [img.master_image.url for img in _sort_images_front_first_safe(ts.batch_id.model_stock_no.images.all()) if img.master_image]
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
            'from_date': from_date,
            'to_date': to_date,
        }
        return Response(context, template_name=self.template_name)


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def nq_completed_tray_list(request):
    """Fetch tray data for NI Completed table view icon. Serves Z1 and Z2."""
    lot_id = request.GET.get('lot_id', '').strip()
    record_lot_id = request.GET.get('record_lot_id', '').strip()
    event_type = request.GET.get('event_type', '').strip().lower()
    if not lot_id:
        return JsonResponse({'success': False, 'error': 'lot_id required'}, status=400)

    if record_lot_id:
        return _nq_completed_tray_list_for_event(lot_id, record_lot_id, event_type)

    # Legacy fallback for older completed rows/templates that do not carry an
    # immutable NickelWiping_*Record identity. New event-aware requests return
    # above and never fall through to "latest by lot" or live NickelQcTrayId.
    # NickelWiping_* records: source_lot_id can repeat across rework passes
    # (e.g. a lot returned from Nickel Audit resubmits under the same lot_id),
    # so multiple records of different types may exist for this lot_id. Pick
    # whichever record is actually the most recent submission event, not a
    # fixed type-priority — otherwise an older Full Accept record would keep
    # shadowing a newer Partial Accept/Reject event for the same lot forever.
    fa = NickelWiping_FullAcceptRecord.objects.filter(source_lot_id=lot_id).order_by('-created_at').first()
    fr = NickelWiping_FullRejectRecord.objects.filter(source_lot_id=lot_id).order_by('-created_at').first()
    # The Completed table lists a partial event under its child_lot_id (the
    # accepted continuation's own lot_id), not source_lot_id — so the view
    # icon must also resolve by child_lot_id, not just source_lot_id.
    pa = (
        NickelWiping_PartialAcceptRecord.objects.filter(source_lot_id=lot_id).order_by('-created_at').first()
        or NickelWiping_PartialAcceptRecord.objects.filter(child_lot_id=lot_id).order_by('-created_at').first()
    )
    pr_lookup_source = pa.source_lot_id if pa else lot_id
    pr = NickelWiping_PartialRejectRecord.objects.filter(source_lot_id=pr_lookup_source).order_by('-created_at').first()

    # partial accept/reject are written as a pair for one submission event —
    # date that event by whichever half is newest.
    partial_at = max((r.created_at for r in (pa, pr) if r is not None), default=None)
    events = [('full_accept', fa.created_at if fa else None), ('full_reject', fr.created_at if fr else None),
              ('partial', partial_at)]
    events = [e for e in events if e[1] is not None]
    latest_kind = max(events, key=lambda e: e[1])[0] if events else None

    if latest_kind == 'full_accept':
        trays = _nq_normalize_tray_snapshot(fa.accept_trays or [])
        return JsonResponse({'success': True, 'trays': _nq_with_delink_tray_snapshot(lot_id, trays)})
    if latest_kind == 'full_reject':
        trays = _nq_normalize_tray_snapshot(fr.reject_trays or [], rejected=True)
        return JsonResponse({'success': True, 'trays': _nq_with_delink_tray_snapshot(lot_id, trays)})
    if latest_kind == 'partial':
        trays = []
        if pa:
            trays += _nq_normalize_tray_snapshot(pa.accept_trays or [])
        if pr:
            trays += _nq_normalize_tray_snapshot(pr.reject_trays or [], rejected=True)
        return JsonResponse({'success': True, 'trays': _nq_with_delink_tray_snapshot(lot_id, trays)})

    # Priority 4: Submission snapshot from Nickel Inspection submit flow
    sub = NickelQC_Submission.objects.filter(lot_id=lot_id).order_by('-created_at').first()
    if sub:
        trays = _nq_normalize_tray_snapshot(sub.accept_trays_data or [])
        trays += _nq_normalize_tray_snapshot(sub.reject_trays_data or [], rejected=True)
        return JsonResponse({'success': True, 'trays': _nq_with_delink_tray_snapshot(lot_id, trays)})

    # Priority 5: Active NickelQcTrayId fallback, excluding delinked rows
    active_trays = NickelQcTrayId.objects.filter(
        lot_id=lot_id, rejected_tray=False, delink_tray=False
    ).values('tray_id', 'tray_quantity')
    rejected_trays = NickelQcTrayId.objects.filter(
        lot_id=lot_id, rejected_tray=True, delink_tray=False
    ).values('tray_id', 'tray_quantity')
    delink_trays = _nq_delink_tray_snapshot(lot_id)
    if active_trays.exists() or rejected_trays.exists() or delink_trays:
        trays = _nq_normalize_tray_snapshot(active_trays)
        trays += _nq_normalize_tray_snapshot(rejected_trays, rejected=True)
        trays += delink_trays
        return JsonResponse({'success': True, 'trays': trays})

    return JsonResponse({'success': True, 'trays': []})


def _nq_partial_event_suffix(record_lot_id):
    value = str(record_lot_id or '').strip().upper()
    for prefix in ('NWPA', 'NWPR'):
        if value.startswith(prefix):
            return value[len(prefix):]
    return ''


def _nq_completed_tray_list_for_event(lot_id, record_lot_id, event_type):
    normalized_type = str(event_type or '').strip().lower()
    normalized_record_id = str(record_lot_id or '').strip()

    if normalized_type == 'full_accept':
        event = NickelWiping_FullAcceptRecord.objects.filter(
            record_lot_id=normalized_record_id,
        ).first()
        if not event:
            return JsonResponse({'success': False, 'error': 'completed event not found'}, status=404)
        trays = _nq_normalize_tray_snapshot(event.accept_trays or [])
        return JsonResponse({'success': True, 'trays': trays, 'event_history': True})

    if normalized_type == 'full_reject':
        event = NickelWiping_FullRejectRecord.objects.filter(
            record_lot_id=normalized_record_id,
        ).first()
        if not event:
            return JsonResponse({'success': False, 'error': 'completed event not found'}, status=404)
        trays = _nq_normalize_tray_snapshot(event.reject_trays or [], rejected=True)
        return JsonResponse({'success': True, 'trays': trays, 'event_history': True})

    if normalized_type == 'partial':
        pa = NickelWiping_PartialAcceptRecord.objects.filter(
            record_lot_id=normalized_record_id,
        ).first()
        if not pa:
            return JsonResponse({'success': False, 'error': 'completed event not found'}, status=404)

        event_suffix = _nq_partial_event_suffix(pa.record_lot_id)
        pr = None
        if event_suffix:
            pr = NickelWiping_PartialRejectRecord.objects.filter(
                source_lot_id=pa.source_lot_id,
                record_lot_id=f'NWPR{event_suffix}',
            ).first()
        if not pr:
            from datetime import timedelta
            paired_rejects = list(NickelWiping_PartialRejectRecord.objects.filter(
                source_lot_id=pa.source_lot_id,
                rejected_qty=pa.rejected_qty,
                created_at__gte=pa.created_at - timedelta(seconds=5),
                created_at__lte=pa.created_at + timedelta(seconds=5),
            ).order_by('created_at')[:2])
            if len(paired_rejects) == 1:
                pr = paired_rejects[0]

        trays = _nq_normalize_tray_snapshot(pa.accept_trays or [])
        if pa.rejected_qty and not pr:
            return JsonResponse({'success': False, 'error': 'matching partial reject event not found'}, status=409)
        if pr:
            trays += _nq_normalize_tray_snapshot(pr.reject_trays or [], rejected=True)
        trays += _nq_normalize_delink_tray_snapshot(pa.delink_trays or [])
        return JsonResponse({'success': True, 'trays': trays, 'event_history': True})

    return JsonResponse({'success': False, 'error': 'invalid completed event type'}, status=400)

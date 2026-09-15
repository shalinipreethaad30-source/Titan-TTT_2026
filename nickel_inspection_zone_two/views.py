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
from Jig_Unloading.models import *
from Jig_Unloading.tray_utils import (
    get_upstream_tray_distribution,
    get_model_master_tray_info,
    normalize_combine_lot_id,
)
from Jig_Loading.models import JigCompleted
from Inprocess_Inspection.models import InprocessInspectionTrayCapacity
from django.contrib.auth.decorators import login_required
from Nickel_Inspection.views import (
    nq_toggle_verified, nq_action, _resolve_nq_previous_unloading_remark,
    _nq_completed_event_rows, _latest_na_full_reject_timestamps,
    _nq_pick_last_updated,
)
from Nickel_Inspection.services import has_unreleased_nickel_wiping_reject_trays
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

class NQ_Zone_PickTableView(APIView):
    renderer_classes = [TemplateHTMLRenderer]
    template_name = "Nickel_Inspection - Zone_two/Nickel_PickTable_zone_two.html"
    def get(self, request):
        user = request.user
        is_admin = user.groups.filter(name="Admin").exists() if user.is_authenticated else False
        nq_rejection_reasons = Nickel_QC_Rejection_Table.objects.all().order_by("id")
        # Get all plating_color IDs where jig_unload_zone_2 is True
        allowed_color_ids = Plating_Color.objects.filter(jig_unload_zone_2=True).values_list(
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
        print("All lot_ids in queryset:", list(queryset.values_list("lot_id", flat=True)))

        # ✅ SECONDARY MULTI-MODEL LOT FILTER (same convention as Jig_Unloading/JigUnloading_Zone2
        # and Nickel_Inspection Zone 1): when two lots are combined into a single jig (Add Model /
        # multi-model Jig Loading), Jig Unloading's "Submit All" creates one JigUnloadAfterTable
        # source row per original lot for traceability. The secondary lot's row carries no
        # plating/tray reference of its own — the combined data already lives on the primary row —
        # so it must not surface here as a separate, blank "no ref" entry.
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
        for jig_unload_obj in page_obj.object_list:
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
                    _nq_tray_capacity(
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
                    print(
                        f"🎯 NQ View - Extracted model_no: {model_no_prefix} from plating_stk_no: {plating_stk_no}"
                    )
                    try:
                        # Find ModelMaster where model_no matches the prefix for images
                        model_master = (
                            ModelMaster.objects.filter(model_no__startswith=model_no_prefix)
                            .prefetch_related("images")
                            .first()
                        )
                        if model_master:
                            print(
                                f"✅ NQ View - Found ModelMaster for images: {model_master.model_no}"
                            )
                            # Get images from ModelMaster
                            for img in _sort_images_front_first_safe(model_master.images.all()):
                                if img.master_image:
                                    images.append(img.master_image.url)
                                    print(
                                        f"📸 NQ View - Added image from ModelMaster: {img.master_image.url}"
                                    )
                        else:
                            print(
                                f"⚠️ NQ View - No ModelMaster found for model_no: {model_no_prefix}"
                            )
                    except Exception as e:
                        print(f"❌ NQ View - Error fetching ModelMaster: {e}")
            # Priority 2: Fallback to existing combine_lot_ids logic if no ModelMaster images
            if not images and data["combine_lot_ids"]:
                print("🔄 NQ View - No ModelMaster images, trying combine_lot_ids fallback")
                first_lot_id = data["combine_lot_ids"][0] if data["combine_lot_ids"] else None
                if first_lot_id:
                    total_stock = TotalStockModel.objects.filter(lot_id=first_lot_id).first()
                    if total_stock and total_stock.batch_id:
                        batch_obj = total_stock.batch_id
                        if batch_obj.model_stock_no:
                            for img in _sort_images_front_first_safe(batch_obj.model_stock_no.images.all()):
                                if img.master_image:
                                    images.append(img.master_image.url)
                                    print(
                                        f"📸 NQ View - Added image from TotalStockModel: {img.master_image.url}"
                                    )
            # Priority 3: Use placeholder if no images found
            if not images:
                print("📷 NQ View - No images found, using placeholder")
                images = [static("assets/images/imagePlaceholder.jpg")]
            data["model_images"] = images
            print(
                f"📸 NQ View - Final images for lot {jig_unload_obj.lot_id}: {len(images)} images"
            )
            # Normalize tray_type display label (NR -> Normal)
            if data.get("tray_type") and data["tray_type"].strip().lower() == "nr":
                data["tray_type"] = "Normal"
            master_data.append(data)
        # ✅ Process the data (similar logic but adapted for JigUnloadAfterTable)
        type_of_input_map = get_type_of_input_map([data.get("stock_lot_id") for data in master_data])
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
            rejection_store = Nickel_QC_Rejection_ReasonStore.objects.filter(lot_id=lot_id).first()
            if rejection_store and rejection_store.total_rejection_quantity:
                total_rejection_qty = rejection_store.total_rejection_quantity
            # Calculate display_accepted_qty
            if total_IP_accpeted_quantity and total_IP_accpeted_quantity > 0:
                data["display_accepted_qty"] = total_IP_accpeted_quantity
            else:
                # Use total_case_qty from JigUnloadAfterTable instead of TotalStockModel
                jig_unload_obj = JigUnloadAfterTable.objects.filter(lot_id=lot_id).first()
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
            # Get model images - since this is combined lots, we'll use a default approach
            images = []
            if data["combine_lot_ids"]:
                # Try to get images from the first original lot
                first_lot_id = data["combine_lot_ids"][0] if data["combine_lot_ids"] else None
                if first_lot_id:
                    total_stock = TotalStockModel.objects.filter(lot_id=first_lot_id).first()
                    if total_stock and total_stock.batch_id:
                        batch_obj = total_stock.batch_id
                        model_master = batch_obj.model_stock_no
                        for img in _sort_images_front_first_safe(model_master.images.all()):
                            if img.master_image:
                                images.append(img.master_image.url)
            if not images:
                images = [static("assets/images/imagePlaceholder.jpg")]
            data["model_images"] = images
            # Add available_qty
            if data.get("nq_physical_qty") and data.get("nq_physical_qty") > 0:
                data["available_qty"] = data.get("nq_physical_qty")
            else:
                data["available_qty"] = data.get("total_IP_accpeted_quantity", 0)
        print(
            f"[DEBUG] Master data loaded with {len(master_data)} entries from JigUnloadAfterTable."
        )
        print(
            "All lot_ids in processed data:",
            [data["stock_lot_id"] for data in master_data],
        )
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

class NQ_Zone_RejectTableView(APIView):
    renderer_classes = [TemplateHTMLRenderer]
    template_name = "Nickel_Inspection - Zone_two/NickelQc_RejectTable_zone_two.html"
    def get(self, request):
        user = request.user
        # Get all plating_color IDs where jig_unload_zone_2 is True for Zone 2 routing
        allowed_color_ids = Plating_Color.objects.filter(jig_unload_zone_2=True).values_list(
            "id", flat=True
        )
        # Subquery for total rejection quantity
        nickel_rejection_total_qty_subquery = Nickel_QC_Rejection_ReasonStore.objects.filter(
            lot_id=OuterRef("lot_id")
        ).values("total_rejection_quantity")[:1]
        queryset = (
            JigUnloadAfterTable.objects.select_related("version", "plating_color", "polish_finish")
            .prefetch_related("location")
            .annotate(nickel_rejection_total_qty=nickel_rejection_total_qty_subquery)
            .filter(Q(nq_qc_rejection=True) | Q(nq_qc_few_cases_accptance=True))
            .filter(
                plating_color_id__in=allowed_color_ids  # Only show records for Zone 2 plating colors
            )
            .order_by("-nq_last_process_date_time", "-lot_id")
        )
        print(f"📊 Found {queryset.count()} Nickel QC rejected records")
        print(
            "All lot_ids in Nickel QC reject queryset:",
            list(queryset.values_list("lot_id", flat=True)),
        )
        # Pagination
        page_number = request.GET.get("page", 1)
        paginator = Paginator(queryset, 10)
        page_obj = paginator.get_page(page_number)
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
                "total_IP_accpeted_quantity": obj.total_case_qty,
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
                    print(
                        f"🎯 Nickel Reject View - Extracted model_no: {model_no_prefix} from plating_stk_no: {plating_stk_no}"
                    )
                    try:
                        # Find ModelMaster where model_no matches the prefix for images
                        model_master = (
                            ModelMaster.objects.filter(model_no__startswith=model_no_prefix)
                            .prefetch_related("images")
                            .first()
                        )
                        if model_master:
                            print(
                                f"✅ Nickel Reject View - Found ModelMaster for images: {model_master.model_no}"
                            )
                            # Get images from ModelMaster
                            for img in _sort_images_front_first_safe(model_master.images.all()):
                                if img.master_image:
                                    images.append(img.master_image.url)
                                    print(
                                        f"📸 Nickel Reject View - Added image from ModelMaster: {img.master_image.url}"
                                    )
                        else:
                            print(
                                f"⚠️ Nickel Reject View - No ModelMaster found for model_no: {model_no_prefix}"
                            )
                    except Exception as e:
                        print(f"❌ Nickel Reject View - Error fetching ModelMaster: {e}")
            # Priority 2: Fallback to existing combine_lot_ids logic if no ModelMaster images
            if not images and obj.combine_lot_ids:
                print(
                    "🔄 Nickel Reject View - No ModelMaster images, trying combine_lot_ids fallback"
                )
                first_lot_id = obj.combine_lot_ids[0] if obj.combine_lot_ids else None
                if first_lot_id:
                    total_stock_obj = TotalStockModel.objects.filter(lot_id=first_lot_id).first()
                    if total_stock_obj and total_stock_obj.batch_id:
                        batch_obj = total_stock_obj.batch_id
                        if batch_obj.model_stock_no:
                            for img in _sort_images_front_first_safe(batch_obj.model_stock_no.images.all()):
                                if img.master_image:
                                    images.append(img.master_image.url)
                                    print(
                                        f"📸 Nickel Reject View - Added image from TotalStockModel: {img.master_image.url}"
                                    )
            # Priority 3: Use placeholder if no images found
            if not images:
                print("📷 Nickel Reject View - No images found, using placeholder")
                images = [static("assets/images/imagePlaceholder.jpg")]
            data["model_images"] = images
            print(
                f"📸 Nickel Reject View - Final images for lot {obj.lot_id}: {len(images)} images"
            )
            # --- Add lot rejection remarks ---
            stock_lot_id = data.get("stock_lot_id")
            lot_rejected_comment = ""
            if stock_lot_id:
                reason_store = Nickel_QC_Rejection_ReasonStore.objects.filter(
                    lot_id=stock_lot_id
                ).first()
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
                    rejection_record = Nickel_QC_Rejection_ReasonStore.objects.filter(
                        lot_id=stock_lot_id
                    ).first()
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
                        print(
                            f"✅ Found rejection for {stock_lot_id}: {rejection_record.total_rejection_quantity}"
                        )
                    else:
                        if (
                            "nickel_rejection_total_qty" not in data
                            or not data["nickel_rejection_total_qty"]
                        ):
                            data["nickel_rejection_total_qty"] = 0
                        print(f"⚠️ No rejection record found for {stock_lot_id}")
                except Exception as e:
                    logger.error(f"❌ Error getting rejection for {stock_lot_id}: {str(e)}", exc_info=True)
                    data["nickel_rejection_total_qty"] = data.get("nickel_rejection_total_qty", 0)
            else:
                data["nickel_rejection_total_qty"] = 0
                print(f"❌ No stock_lot_id for batch {data.get('batch_id')}")
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
        print("✅ Nickel QC Reject data processing completed")
        print("Processed lot_ids:", [data["stock_lot_id"] for data in master_data])
        context = {
            "master_data": master_data,
            "page_obj": page_obj,
            "paginator": paginator,
            "user": user,
        }
        return Response(context, template_name=self.template_name)

class NQ_Zone_CompletedView(APIView):
    renderer_classes = [TemplateHTMLRenderer]
    template_name = "Nickel_Inspection - Zone_two/NI_Completed_zone_two.html"
    def get(self, request):
        from django.utils import timezone
        from datetime import datetime, timedelta
        import pytz
        user = request.user
        tz = pytz.timezone("Asia/Kolkata")
        now_local = timezone.now().astimezone(tz)
        today = now_local.date()
        yesterday = today - timedelta(days=1)
        from_date_str = request.GET.get("from_date")
        to_date_str = request.GET.get("to_date")
        if from_date_str and to_date_str:
            try:
                from_date = datetime.strptime(from_date_str, "%Y-%m-%d").date()
                to_date = datetime.strptime(to_date_str, "%Y-%m-%d").date()
            except ValueError:
                from_date = yesterday
                to_date = today
        else:
            from_date = yesterday
            to_date = today
        from_datetime = timezone.make_aware(datetime.combine(from_date, datetime.min.time()))
        to_datetime = timezone.make_aware(datetime.combine(to_date, datetime.max.time()))
        allowed_color_ids = Plating_Color.objects.filter(jig_unload_zone_2=True).values_list(
            "id", flat=True
        )

        # ERR4 Fix: list one row per completed submission event (append-only
        # NickelWiping_*Record history), not one row per live
        # JigUnloadAfterTable object — a lot's row is reused across rework
        # passes (e.g. returned from Nickel Audit), so the latter silently
        # collapsed multiple completed passes into a single row.
        rows = _nq_completed_event_rows(allowed_color_ids)
        rows = [
            r for r in rows
            if r.nq_last_process_date_time
            and from_datetime <= r.nq_last_process_date_time <= to_datetime
        ]

        page_number = request.GET.get("page", 1)
        paginator = Paginator(rows, 10)
        page_obj = paginator.get_page(page_number)
        master_data = []
        for jig_unload_obj in page_obj.object_list:
            data = {
                "batch_id": jig_unload_obj.unload_lot_id,
                "lot_id": jig_unload_obj.lot_id,
                "date_time": jig_unload_obj.created_at,
                "model_stock_no__model_no": "Combined Model",
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
                "vendor_internal": "",
                "location__location_name": _get_input_source(jig_unload_obj),
                "tray_type": get_model_master_tray_info(
                    jig_unload_obj.plating_stk_no, jig_unload_obj.tray_type or ""
                )[0],
                "tray_capacity": jig_unload_obj.tray_capacity or 0,
                "Moved_to_D_Picker": False,
                "Draft_Saved": False,
                "nq_qc_rejection": jig_unload_obj.nq_qc_rejection,
                "nq_qc_accptance": jig_unload_obj.nq_qc_accptance,
                "stock_lot_id": jig_unload_obj.lot_id,
                # Prefer the live current_stage SSOT (modelmasterapp/stage_service.py) so
                # this stays in sync with downstream modules (e.g. Spider Spindle) that
                # only update current_stage and not last_process_module.
                "last_process_module": jig_unload_obj.current_stage or jig_unload_obj.last_process_module or "Jig Unload",
                "next_process_module": "Nickel QC",
                "nq_qc_accepted_qty_verified": jig_unload_obj.nq_qc_accepted_qty_verified,
                "nq_qc_accepted_qty": jig_unload_obj.nq_qc_accepted_qty or 0,
                "nq_rejection_qty": getattr(jig_unload_obj, '_nw_rejected_qty', 0),
                "brass_rejection_total_qty": getattr(jig_unload_obj, '_nw_rejected_qty', 0),
                "nq_missing_qty": jig_unload_obj.nq_missing_qty or 0,
                "nq_physical_qty": jig_unload_obj.nq_physical_qty,
                "nq_physical_qty_edited": False,
                "rejected_nickle_ip_stock": jig_unload_obj.rejected_nickle_ip_stock,
                "rejected_ip_stock": jig_unload_obj.rejected_nickle_ip_stock,
                "few_cases_accepted_Ip_stock": jig_unload_obj.nq_qc_few_cases_accptance,
                "accepted_tray_scan_status": jig_unload_obj.nq_accepted_tray_scan_status,
                "nq_pick_remarks": jig_unload_obj.nq_pick_remarks,
                "nq_accepted_tray_scan_status": jig_unload_obj.nq_accepted_tray_scan_status,
                "nq_qc_few_cases_accptance": jig_unload_obj.nq_qc_few_cases_accptance,
                "nq_onhold_picking": jig_unload_obj.nq_onhold_picking,
                "iqf_acceptance": False,
                "send_to_nickel_brass": jig_unload_obj.send_to_nickel_brass,
                "total_IP_accpeted_quantity": jig_unload_obj.total_case_qty,
                "bq_last_process_date_time": jig_unload_obj.created_at,
                "last_process_date_time": jig_unload_obj.nq_last_process_date_time,
                "nq_hold_lot": jig_unload_obj.nq_hold_lot,
                "brass_audit_accepted_qty_verified": False,
                "iqf_accepted_qty_verified": False,
                "plating_stk_no": jig_unload_obj.plating_stk_no or "",
                "polishing_stk_no": jig_unload_obj.polish_stk_no or "",
                "category": jig_unload_obj.category or "",
                "combine_lot_ids": jig_unload_obj.combine_lot_ids,
                "unload_lot_id": jig_unload_obj.unload_lot_id,
                "audit_check": jig_unload_obj.audit_check,
                "nw_event_type": getattr(jig_unload_obj, "_nw_event_type", ""),
                "nw_record_lot_id": getattr(jig_unload_obj, "_nw_record_lot_id", ""),
                "nw_no_of_trays": getattr(jig_unload_obj, "_nw_no_of_trays", None),
            }
            images = []
            if jig_unload_obj.plating_stk_no:
                plating_stk_no = str(jig_unload_obj.plating_stk_no)
                if len(plating_stk_no) >= 4:
                    model_no_prefix = plating_stk_no[:4]
                    model_master = (
                        ModelMaster.objects.filter(model_no__startswith=model_no_prefix)
                        .prefetch_related("images")
                        .first()
                    )
                    if model_master:
                        for img in _sort_images_front_first_safe(model_master.images.all()):
                            if img.master_image:
                                images.append(img.master_image.url)
            if not images and data["combine_lot_ids"]:
                first_lot_id = data["combine_lot_ids"][0] if data["combine_lot_ids"] else None
                if first_lot_id:
                    total_stock = TotalStockModel.objects.filter(lot_id=first_lot_id).first()
                    if total_stock and total_stock.batch_id and total_stock.batch_id.model_stock_no:
                        for img in _sort_images_front_first_safe(total_stock.batch_id.model_stock_no.images.all()):
                            if img.master_image:
                                images.append(img.master_image.url)
            if not images:
                images = [static("assets/images/imagePlaceholder.jpg")]
            data["model_images"] = images
            master_data.append(data)
        type_of_input_map = get_type_of_input_map([data.get("stock_lot_id") for data in master_data])
        for data in master_data:
            total_ip_accepted_quantity = data.get("total_IP_accpeted_quantity", 0)
            tray_capacity = data.get("tray_capacity", 0)
            data["vendor_location"] = (
                f"{data.get('vendor_internal', '')}_{data.get('location__location_name', '')}"
            )
            lot_id = data.get("stock_lot_id")
            data["type_of_input"] = type_of_input_map.get(lot_id, "Fresh")
            # nq_rejection_qty is already the exact per-event reject qty from
            # _nq_completed_event_rows — a genuine 0 (e.g. a Full Accept event)
            # must not be "corrected" by re-querying Nickel_QC_Rejection_ReasonStore
            # by lot_id, since that store reflects the lot's current/latest state
            # and could leak an unrelated later rejection onto an earlier event.
            rejection_qty = data.get("nq_rejection_qty") or 0
            data["brass_rejection_total_qty"] = rejection_qty
            if total_ip_accepted_quantity and total_ip_accepted_quantity > 0:
                data["display_accepted_qty"] = total_ip_accepted_quantity
            else:
                jig_unload_obj = JigUnloadAfterTable.objects.filter(lot_id=lot_id).first()
                if jig_unload_obj and rejection_qty > 0:
                    data["display_accepted_qty"] = max(
                        jig_unload_obj.total_case_qty - rejection_qty, 0
                    )
                else:
                    data["display_accepted_qty"] = (
                        jig_unload_obj.total_case_qty if jig_unload_obj else 0
                    )
            display_qty = data.get("display_accepted_qty", 0)
            event_no_of_trays = data.get("nw_no_of_trays")
            if data.get("nw_event_type") and event_no_of_trays is not None:
                data["no_of_trays"] = event_no_of_trays
            elif tray_capacity > 0 and display_qty > 0:
                data["no_of_trays"] = ceil(display_qty / tray_capacity)
            else:
                data["no_of_trays"] = 0
        context = {
            "master_data": master_data,
            "page_obj": page_obj,
            "paginator": paginator,
            "user": user,
            "from_date": from_date.strftime("%Y-%m-%d"),
            "to_date": to_date.strftime("%Y-%m-%d"),
            "date_filter_applied": bool(from_date_str and to_date_str),
        }
        return Response(context, template_name=self.template_name)

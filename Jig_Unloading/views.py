from django.shortcuts import render
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods
from modelmasterapp.models import *
from modelmasterapp.tray_code_mapping import get_tray_codes_for_plating_stock, validate_tray_code_for_stock
from modelmasterapp.color_service import get_model_colors_by_model_no
from django.db.models import OuterRef, Subquery, Exists, F, TextField, Q
from django.db import transaction
from django.db.models.functions import Cast
from django.db.models.fields.json import KeyTextTransform
from django.core.paginator import Paginator
import builtins
import math
import json
import re
import logging
import sys
from django.utils.decorators import method_decorator
from rest_framework.views import APIView
from django.views.generic import TemplateView
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework import status
from django.views.decorators.http import require_GET
from Jig_Loading.models import *
from Jig_Unloading.models import *
from Jig_Unloading.tray_utils import (
    find_jig_unload_tray_conflict,
    is_valid_jig_unload_tray_id_format,
    normalize_jig_unload_tray_id,
    normalize_combine_lot_id,
    validate_jig_unload_nickel_reject_tray,
)
from Recovery_DP.models import *
from Inprocess_Inspection.models import InprocessInspectionTrayCapacity
from django.contrib.auth.mixins import LoginRequiredMixin
from modelmasterapp.type_of_input import get_type_of_input_map, label_for_upload_type

logger = logging.getLogger(__name__)


def _zone1_safe_print(*args, **kwargs):
    """Prevent debug output from breaking requests on non-UTF-8 consoles."""
    try:
        builtins.print(*args, **kwargs)
    except UnicodeEncodeError:
        stream = kwargs.get('file') or sys.stdout
        encoding = getattr(stream, 'encoding', None) or 'utf-8'
        safe_args = [
            str(arg).encode(encoding, errors='replace').decode(encoding)
            for arg in args
        ]
        builtins.print(*safe_args, **kwargs)


print = _zone1_safe_print


def _jul_ordered_unique(values):
    seen = set()
    result = []
    for value in values or []:
        if value in (None, ''):
            continue
        text_value = str(value).strip()
        if not text_value or text_value in seen:
            continue
        seen.add(text_value)
        result.append(text_value)
    return result


def _jul_extract_source_lot_id(raw_id):
    if not raw_id:
        return ''
    value = str(raw_id).strip().lstrip('-')
    if ':' in value:
        possible_lot = value.rsplit(':', 1)[-1].strip()
        if possible_lot:
            return possible_lot
    if value.startswith('JLOT-') and '-' in value[5:]:
        return value.rsplit('-', 1)[-1]
    return value


def _jul_submission_tray_signature(tray_data):
    if not isinstance(tray_data, list):
        return tuple()
    signature = []
    for entry in tray_data:
        if not isinstance(entry, dict):
            continue
        tray_id = str(entry.get('tray_id') or '').strip().upper()
        if not tray_id:
            continue
        signature.append((
            int(entry.get('slot') or 0),
            tray_id,
            int(entry.get('qty') or entry.get('tray_qty') or entry.get('tray_quantity') or 0),
            bool(entry.get('is_top_tray') or entry.get('top_tray')),
        ))
    return tuple(sorted(signature))


def _jul_source_metadata_from_tray_data(tray_data):
    if not isinstance(tray_data, list):
        return {}
    for entry in tray_data:
        if not isinstance(entry, dict):
            continue
        metadata = entry.get('_source_metadata') or entry.get('source_metadata')
        if isinstance(metadata, dict):
            return metadata
    return {}


def _jul_enrich_tray_data_with_sources(tray_data, source_metadata):
    if not isinstance(tray_data, list) or not source_metadata:
        return tray_data
    enriched = []
    for entry in tray_data:
        if isinstance(entry, dict):
            copied = dict(entry)
            copied['_source_metadata'] = source_metadata
            enriched.append(copied)
        else:
            enriched.append(entry)
    return enriched


def _jul_rebuild_tray_slots_z1(total_qty, tray_capacity, old_slots=None):
    """Recompute a single fresh tray-slot list for total_qty at tray_capacity.

    Add/Remove Model must always redistribute the COMBINED quantity from
    scratch so there is never more than one partial (top) tray — appending a
    separate slot batch per merge (the previous approach) produced one
    partial tray per merge instead of one overall.

    Tray IDs already scanned onto the old slots are preserved at their exact
    original slot INDEX (not compacted to the front) — a tray physically
    scanned into slot 5 must stay displayed in slot 5, never jump to slot 2,
    so the user can still find it where they left it. Only genuinely new
    slots (beyond the old slot count) start blank for scanning.
    """
    cap = tray_capacity if tray_capacity and tray_capacity > 0 else 20
    num_trays = math.ceil(total_qty / cap) if total_qty > 0 else 1
    remainder = total_qty % cap if total_qty % cap != 0 else cap

    old_slots = old_slots or []

    slots = []
    for i in range(num_trays):
        is_top = (i == 0)
        is_pre_existing_slot = i < len(old_slots)
        tray_id = old_slots[i].get('tray_id', '') if is_pre_existing_slot else ''
        slots.append({
            'slot': i + 1,
            'tray_id': tray_id,
            'qty': remainder if is_top else cap,
            'is_top_tray': is_top,
            'editable_qty': is_top,
            'is_newly_added_slot': not is_pre_existing_slot,
        })
    return slots


def _jul_prepend_new_lot_tray_slots_z1(new_qty, combined_total_qty, tray_capacity, existing_slots):
    """Combine an Add-Model lot's quantity into an existing model's tray slots
    WITHOUT mingling tray IDs across lots.

    Placement: the newly added lot's trays are fresh/blank and placed FIRST
    (top of the combined list); the existing lot's trays — with their
    tray_id preserved — are pushed down after them, matching the frontend's
    stated contract (Jig_Unloading_Main.html openTrayScan): "new lot's trays
    first, old lot's trays pushed down".

    Quantity: only the overall TOP tray (slot 1) may hold a partial/leftover
    qty — every other slot always shows full capacity, even an existing slot
    that used to be its own lot's top (partial) tray before this merge. The
    partial amount it used to carry is absorbed into the single combined
    remainder instead, since two separate partial trays would violate the
    "one partial tray only" rule. Tray count is therefore recomputed from
    the combined total (not each lot's own independent split); if pooling
    the leftovers needs fewer trays than the sum of both lots' own splits,
    trailing NEVER-SCANNED (no tray_id) slots from the existing lot are
    trimmed to match. If already-scanned (real tray_id) slots still exceed
    what the combined total's tray math allows, the LOT simply no longer
    needs that many trays: the most-recently-scanned tail is kept and the
    earliest excess is dropped, so the final slot count always equals
    exactly ceil(combined_total_qty / capacity) — never more, never fewer —
    and every tray is either the single top remainder or a full-capacity
    tray. A tray is never left at qty=0.
    """
    cap = tray_capacity if tray_capacity and tray_capacity > 0 else 20
    existing_slots = existing_slots or []

    new_num_trays = math.ceil(new_qty / cap) if new_qty > 0 else 0
    num_trays_total = math.ceil(combined_total_qty / cap) if combined_total_qty > 0 else 1
    remainder = combined_total_qty % cap if combined_total_qty % cap != 0 else cap

    old_slots = list(existing_slots)
    max_old = max(num_trays_total - new_num_trays, 0)
    # Trim trailing NEVER-SCANNED (blank) slots first — free, no data loss.
    while len(old_slots) > max_old and old_slots and not old_slots[-1].get('tray_id'):
        old_slots.pop()
    # Still too many (all real, already-scanned)? Keep the tail (most
    # recently scanned), drop the earliest excess — never exceed max_old.
    if len(old_slots) > max_old:
        old_slots = old_slots[len(old_slots) - max_old:] if max_old > 0 else []

    combined = []
    for _ in range(new_num_trays):
        combined.append({'tray_id': '', 'is_newly_added_slot': True})
    for slot in old_slots:
        combined.append({
            'tray_id': slot.get('tray_id', ''),
            'is_newly_added_slot': slot.get('is_newly_added_slot', False),
        })
    # Defensive pad: guarantees slot count == num_trays_total even if the
    # surviving lot's own slot list was ever shorter than the capacity math
    # expects, so the invariant below always holds.
    while len(combined) < num_trays_total:
        combined.append({'tray_id': '', 'is_newly_added_slot': True})

    # Every tray is either the single top (remainder) tray or a full-capacity
    # tray, recomputed fresh from the combined total — mirrors
    # _jul_rebuild_tray_slots_z1. Sum always equals combined_total_qty
    # exactly; no tray is ever left at qty=0.
    for idx, slot in enumerate(combined):
        is_top = idx == 0
        slot['slot'] = idx + 1
        slot['qty'] = remainder if is_top else cap
        slot['is_top_tray'] = is_top
        slot['editable_qty'] = is_top

    return combined


def _jul_find_after_table_by_source_lot_id(lot_id):
    """Find a JigUnloadAfterTable row whose combine_lot_ids contains lot_id.

    Tries an exact-match containment filter first (fast path for Zone 1's own
    plain lot_id entries), then falls back to a normalised comparison because
    Zone 2 stores combine_lot_ids entries in a prefixed format (e.g.
    'JLOT-xxx-LIDyyy' or 'JLOT-xxx:LIDyyy'). Without the fallback, the same
    source lot already merged in via Zone 2 is invisible to Zone 1 lookups,
    letting Zone 1 create a duplicate standalone record for it.
    """
    existing = JigUnloadAfterTable.objects.filter(
        combine_lot_ids__contains=[lot_id]
    ).first()
    if existing:
        return existing
    for candidate in JigUnloadAfterTable.objects.filter(
        combine_lot_ids__isnull=False
    ).exclude(combine_lot_ids__exact=[]).order_by('-id'):
        if any(
            normalize_combine_lot_id(cid) == lot_id
            for cid in (candidate.combine_lot_ids or [])
        ):
            return candidate
    return None

class Jig_Unloading_MainTable(LoginRequiredMixin, TemplateView):
    template_name = "Jig_Unloading/Jig_Unloading_Main.html"
    login_url = 'login'

    def get_dynamic_tray_capacity(self, tray_type_name):
        """
        Get tray capacity based on tray type name.
        Rules (per workflow spec, sourced from TrayType master DB):
        - Normal (or NR/NB/ND/NL): 20
        - Jumbo  (or JR/JB/JD):    12 (matches TrayType master data)
        - Others: DB lookup fallback
        """
        try:
            # Workflow-spec capacity — covers both type names and tray code prefixes
            _tn = (tray_type_name or '').upper()
            if _tn in ('NORMAL', 'NR', 'NB', 'ND', 'NL', 'NW'):
                return 20
            elif _tn in ('JUMBO', 'JR', 'JB', 'JD'):
                return 12

            # Fallback: try custom capacity override table
            custom_capacity = InprocessInspectionTrayCapacity.objects.filter(
                tray_type__tray_type=tray_type_name,
                is_active=True
            ).first()

            if custom_capacity:
                return custom_capacity.custom_capacity

            # Fallback to TrayType table
            tray_type = TrayType.objects.filter(tray_type=tray_type_name).first()
            if tray_type:
                return tray_type.tray_capacity

            # Default fallback
            return 0

        except Exception as e:
            print(f"⚠️ Error getting dynamic tray capacity: {e}")
            return 0

    def get_stock_model_data(self, lot_id):
        """
        Helper function to get stock model data from either TotalStockModel or RecoveryStockModel
        Returns: (stock_model, is_recovery, batch_model_class)
        """
        # Try TotalStockModel first
        tsm = TotalStockModel.objects.filter(lot_id=lot_id).first()
        if tsm:
            return tsm, False, ModelMasterCreation
        
        # Try RecoveryStockModel if not found in TotalStockModel
        try:
            rsm = RecoveryStockModel.objects.filter(lot_id=lot_id).first()
            if rsm:
                # Try to import RecoveryMasterCreation safely
                try:
                    from Recovery_DP.models import RecoveryMasterCreation
                    return rsm, True, RecoveryMasterCreation
                except ImportError:
                    print("⚠️ RecoveryMasterCreation not found, using ModelMasterCreation as fallback")
                    return rsm, True, ModelMasterCreation
        except Exception as e:
            print(f"⚠️ Error accessing RecoveryStockModel: {e}")
        
        return None, False, None

    def get_lot_specific_data(self, lot_id, model_no):
        """Get lot-specific data from either TotalStockModel or RecoveryStockModel"""
        
        print(f"🔍 get_lot_specific_data called for lot_id: {lot_id}, model_no: {model_no}")
        
        # STEP 0: JigCompleted → batch_id → ModelMasterCreation (for Jig Loading lots not in TotalStockModel)
        _jc = JigCompleted.objects.filter(lot_id=lot_id).only('batch_id').first()
        if _jc and _jc.batch_id:
            _mmc = ModelMasterCreation.objects.select_related(
                'version', 'model_stock_no', 'model_stock_no__tray_type', 'location'
            ).filter(batch_id=_jc.batch_id).first()
            if _mmc:
                _version_name = "No Version"
                if hasattr(_mmc, 'version') and _mmc.version:
                    _version_name = getattr(_mmc.version, 'version_internal', None) or getattr(_mmc.version, 'version_name', 'No Version')
                _tray_type_str = _mmc.model_stock_no.tray_type.tray_type if _mmc.model_stock_no and _mmc.model_stock_no.tray_type else "No Tray Type"
                print(f"✅ get_lot_specific_data via JigCompleted.batch_id={_jc.batch_id}: tray={_tray_type_str}")
                return {
                    'version': _version_name,
                    'vendor': getattr(_mmc, 'vendor_internal', None) or "No Vendor",
                    'location': _mmc.location.location_name if hasattr(_mmc, 'location') and _mmc.location else "No Location",
                    'tray_type': _tray_type_str,
                    'tray_capacity': self.get_dynamic_tray_capacity(_tray_type_str) if _tray_type_str != "No Tray Type" else 0,
                    'plating_stk_no': getattr(_mmc, 'plating_stk_no', None) or "No Plating Stock No",
                    'polishing_stk_no': getattr(_mmc, 'polishing_stk_no', None) or "No Polishing Stock No",
                    'plating_color': getattr(_mmc, 'plating_color', None) or "N/A",
                    'source': 'JigCompleted→ModelMasterCreation'
                }

        # STEP 1: Get stock model data using the proven method
        stock_model, is_recovery, batch_model_class = self.get_stock_model_data(lot_id)
        
        if not stock_model or not stock_model.batch_id:
            print(f"❌ No stock model or batch_id found for lot_id: {lot_id}")
            return None
            
        batch_id = stock_model.batch_id.id
        print(f"✅ Found stock model: {'Recovery' if is_recovery else 'Total'}Stock -> batch_id: {batch_id}")
        
        # STEP 2: Get MasterCreation data using batch_id and correct model class
        try:
            print(f"🏭 Looking up {batch_model_class.__name__} for batch_id: {batch_id}")
            
            master_creation = batch_model_class.objects.select_related(
                'version', 'model_stock_no', 'model_stock_no__tray_type', 'location'
            ).filter(id=batch_id).first()
            
            if master_creation:
                print(f"🎯 Found {batch_model_class.__name__} for batch_id: {batch_id}")
                print(f"🏭 Details: plating_stk_no={getattr(master_creation, 'plating_stk_no', None)}, polishing_stk_no={getattr(master_creation, 'polishing_stk_no', None)}, version={getattr(master_creation, 'version', None)}")
                
                # Safe version access - prioritize version_internal
                version_name = "No Version"
                if hasattr(master_creation, 'version') and master_creation.version:
                    version_name = getattr(master_creation.version, 'version_internal', None) or getattr(master_creation.version, 'version_name', 'No Version')
                
                return {
                    'version': version_name,
                    'vendor': getattr(master_creation, 'vendor_internal', None) or "No Vendor",
                    'location': master_creation.location.location_name if hasattr(master_creation, 'location') and master_creation.location else "No Location",
                    'tray_type': master_creation.model_stock_no.tray_type.tray_type if master_creation.model_stock_no and master_creation.model_stock_no.tray_type else "No Tray Type",
                    'tray_capacity': self.get_dynamic_tray_capacity(master_creation.model_stock_no.tray_type.tray_type) if master_creation.model_stock_no and master_creation.model_stock_no.tray_type else 0,
                    'plating_stk_no': getattr(master_creation, 'plating_stk_no', None) or "No Plating Stock No",
                    'polishing_stk_no': getattr(master_creation, 'polishing_stk_no', None) or "No Polishing Stock No",
                    'plating_color': getattr(master_creation, 'plating_color', None) or "N/A",
                    'source': batch_model_class.__name__
                }
            else:
                print(f"❌ No {batch_model_class.__name__} found for batch_id: {batch_id}")
                
        except Exception as e:
            print(f"❌ Error getting {batch_model_class.__name__} data: {e}")
        
        print(f"❌ No lot-specific data found for lot_id: {lot_id}")
        return None

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)

        # Zone 1: colors flagged for Jig Unload Zone 1 in Model Master
        allowed_colors = Plating_Color.objects.filter(
            jig_unload_zone_1=True
        ).values_list('plating_color', flat=True)
        
        print(f"🔍 Zone 1 - Allowed colors: {list(allowed_colors)}")

        # Get all plating colors and strip "IP-" prefix from stored values for matching
        allowed_colors_list = list(allowed_colors)
        
        # Build list of patterns to match (handle both "IPS" and "IP-IPS" formats)
        plating_patterns = allowed_colors_list + [f"IP-{color}" for color in allowed_colors_list]
        
        # Create polish_finish subquery for annotation
        polish_finish_subquery = TotalStockModel.objects.filter(
            lot_id=OuterRef('lot_id')
        ).values('polish_finish__polish_finish')[:1]
        
        jig_unload = JigCompleted.objects.select_related('bath_numbers', 'unload_hold_by', 'unload_release_by').annotate(
            plating_color_cast=KeyTextTransform('plating_color', 'draft_data'),
            polish_finish_name=Subquery(polish_finish_subquery)
        ).filter(
            plating_color_cast__in=plating_patterns,
            last_process_module='Inprocess Inspection'
        ).exclude(
            last_process_module='Jig Unloading'
        ).order_by('-IP_loaded_date_time')

        # ENHANCED FILTER: Also get jigs where plating_color is not in draft_data
        # but can be determined from TotalStockModel or RecoveryStockModel
        jigs_without_plating_in_draft = JigCompleted.objects.select_related('bath_numbers', 'unload_hold_by', 'unload_release_by').annotate(
            plating_color_cast=KeyTextTransform('plating_color', 'draft_data'),
            polish_finish_name=Subquery(polish_finish_subquery)
        ).filter(
            plating_color_cast__isnull=True,  # draft_data has no plating_color
            last_process_module='Inprocess Inspection'
        ).order_by('-IP_loaded_date_time')
        
        # Get lot_ids from jigs without plating color in draft_data
        lot_ids_without_plating = list(jigs_without_plating_in_draft.values_list('lot_id', flat=True))
        
        if lot_ids_without_plating:
            # Get plating colors from TotalStockModel for these lot_ids
            total_stock_colors = TotalStockModel.objects.filter(
                lot_id__in=lot_ids_without_plating,
                plating_color__plating_color__in=allowed_colors_list
            ).values_list('lot_id', 'plating_color__plating_color')
            
            # Get plating colors from RecoveryStockModel for these lot_ids
            try:
                from Recovery_DP.models import RecoveryStockModel
                recovery_stock_colors = RecoveryStockModel.objects.filter(
                    lot_id__in=lot_ids_without_plating,
                    plating_color__plating_color__in=allowed_colors_list
                ).values_list('lot_id', 'plating_color__plating_color')
            except:
                recovery_stock_colors = []
            
            # Combine all valid lot_ids that have matching plating colors
            valid_lot_ids_for_zone1 = set()
            for lot_id, color in total_stock_colors:
                if color in allowed_colors_list:
                    valid_lot_ids_for_zone1.add(lot_id)
                    
            for lot_id, color in recovery_stock_colors:
                if color in allowed_colors_list:
                    valid_lot_ids_for_zone1.add(lot_id)

            # ✅ FIX: JigCompleted fallback — for Jig Loading lot IDs not in TotalStock/RecoveryStock.
            # JigCompleted.lot_id ≠ TotalStockModel.lot_id (Jig Loading creates its own lot IDs).
            # Use draft_data['batch_id'] → TotalStockModel.batch_id FK → plating_color check.
            batch_ids_to_check = []
            jig_lot_to_batch = {}
            for jig in jigs_without_plating_in_draft:
                if jig.lot_id not in valid_lot_ids_for_zone1:
                    batch_id_str = (jig.draft_data or {}).get('batch_id')
                    if batch_id_str:
                        batch_ids_to_check.append(batch_id_str)
                        jig_lot_to_batch[jig.lot_id] = batch_id_str

            if batch_ids_to_check:
                ips_batch_ids = set(
                    TotalStockModel.objects.filter(
                        batch_id__batch_id__in=batch_ids_to_check,
                        plating_color__plating_color__in=allowed_colors_list
                    ).values_list('batch_id__batch_id', flat=True)
                )
                ips_batch_ids.update(
                    ModelMasterCreation.objects.filter(
                        batch_id__in=batch_ids_to_check,
                        plating_color__in=allowed_colors_list
                    ).values_list('batch_id', flat=True)
                )
                for jig_lot_id, batch_id_str in jig_lot_to_batch.items():
                    if batch_id_str in ips_batch_ids:
                        valid_lot_ids_for_zone1.add(jig_lot_id)
                        print(f"✅ Zone1 JigCompleted fallback: {jig_lot_id} -> {batch_id_str} -> IPS")

            # Get additional jigs that match by lot_id even if draft_data lacks plating_color
            additional_jigs = jigs_without_plating_in_draft.filter(lot_id__in=valid_lot_ids_for_zone1)
            
            # Combine both sets: jigs with plating_color in draft_data + jigs with valid colors from TotalStock/Recovery
            jig_unload = jig_unload.union(additional_jigs, all=False)
        
        # 🧠 SMART FILTER: Remove jigs with ALL lot_ids unloaded
        jig_unload = self.filter_fully_unloaded_jigs(jig_unload)
        
        # Fetch all Bath Numbers for dropdown
        bath_numbers = BathNumbers.objects.all().order_by('bath_number')
        
        # Get all unique model numbers from all jig_unload for bulk processing
        all_model_numbers = set()
        all_lot_ids = set()
        all_batch_ids = set()  # For Jig Loading batch_id → images fallback
        for jig_detail in jig_unload:
            # Prefer draft_data['no_of_model_cases'] over the raw DB field, matching
            # the priority used later when building the actual rendered
            # jig_detail.no_of_model_cases (see `model_cases = draft_data.get(...)`
            # below). Without this, an Add Model merge that updates draft_data's
            # model list (but not the raw DB field yet) means the newly-added
            # model's key is never added to the global color palette here, so its
            # circle falls back to the default gray "no color assigned" style
            # instead of getting its own distinct color.
            _dd_early = getattr(jig_detail, 'draft_data', None) or {}
            if isinstance(_dd_early, str):
                try:
                    _dd_early = json.loads(_dd_early)
                except Exception:
                    _dd_early = {}
            _raw_mc = _dd_early.get('no_of_model_cases') if isinstance(_dd_early, dict) else None
            if not _raw_mc:
                _raw_mc = jig_detail.no_of_model_cases
            if _raw_mc:
                if isinstance(_raw_mc, str):
                    try:
                        import json
                        _parsed = json.loads(_raw_mc)
                        if isinstance(_parsed, list):
                            _raw_mc = _parsed
                    except Exception:
                        pass

                if isinstance(_raw_mc, list):
                    all_model_numbers.update([str(m) for m in _raw_mc])
                elif isinstance(_raw_mc, str):
                    # Handle both separator styles used across the codebase:
                    # 'MODEL1(LID...):QTY | MODEL2(LID...):QTY' (Jig_Loading.JigSaveAPI)
                    # and legacy 'MODEL1:QTY,MODEL2:QTY' / 'MODEL1 [LID...]:QTY'.
                    for _item in _raw_mc.replace('|', ',').split(','):
                        _mn = re.split(r'[\(\[]', _item, maxsplit=1)[0].split(':')[0].strip()
                        if _mn:
                            all_model_numbers.add(_mn)
            elif getattr(jig_detail, 'plating_stock_num', None):
                # Single-model jig: no_of_model_cases stored as '' but plating_stock_num has the model
                all_model_numbers.add(str(jig_detail.plating_stock_num).strip())
            # Collect lot_ids for dual-table lookup
            if hasattr(jig_detail, 'lot_id_quantities') and jig_detail.lot_id_quantities:
                all_lot_ids.update(jig_detail.lot_id_quantities.keys())
            # Collect batch_ids for Jig Loading image fallback path
            _jd_batch = getattr(jig_detail, 'batch_id', None)
            if _jd_batch:
                all_batch_ids.add(str(_jd_batch).strip())

        # ✅ Type of Input (Fresh/Recovery): bulk-resolve via TotalStockModel.lot_id → batch_id.upload_type
        # for every lot_id referenced, plus a batch_id-keyed fallback for Jig Loading-origin lots
        # (whose lot_id_quantities keys may not exist in TotalStockModel).
        type_of_input_map = get_type_of_input_map(list(all_lot_ids))
        batch_type_of_input_map = {}
        if all_batch_ids:
            for _row in ModelMasterCreation.objects.filter(
                batch_id__in=list(all_batch_ids)
            ).values('batch_id', 'upload_type'):
                batch_type_of_input_map[_row['batch_id']] = label_for_upload_type(_row['upload_type'])

        # DB-persisted, Plating Stk No-unique colors (see modelmasterapp.color_service)
        # so Model Presents circles match Inprocess Inspection / Jig Unloading Zone 2.
        sorted_model_numbers = sorted(str(m) for m in all_model_numbers)
        global_model_colors = get_model_colors_by_model_no(sorted_model_numbers)
        print(f"🎨 Resolved color mapping for models: {global_model_colors}")

        # 🚀 ENHANCED: Dual-table model data fetching (TotalStock + Recovery)
        model_data_map = {}
        model_images_map = {}
        
        if all_model_numbers:
            # Build a set of numeric model_nos to match ModelMasterCreation.model_stock_no__model_no
            # all_model_numbers may contain plating_stk_nos like '1805SAD02' — extract numeric prefix too
            numeric_model_nos = set()
            for _mn in all_model_numbers:
                _match = re.match(r'^(\d+)', str(_mn))
                numeric_model_nos.add(_match.group(1) if _match else str(_mn))

            # First, try ModelMasterCreation (linked to TotalStockModel)
            model_master_creations = ModelMasterCreation.objects.filter(
                model_stock_no__model_no__in=numeric_model_nos
            ).select_related(
                'version', 
                'model_stock_no', 
                'model_stock_no__tray_type', 
                'location'
            ).values(
                'model_stock_no__model_no', 
                'version__version_name',
                'vendor_internal',
                'location__location_name',
                'model_stock_no__tray_type__tray_type',
                'model_stock_no__tray_capacity',
                'plating_stk_no',
                'polishing_stk_no',
            ).distinct()
            
            # Store found model numbers to avoid duplicates
            found_model_numbers = set()
            
            for mmc in model_master_creations:
                model_no = mmc['model_stock_no__model_no']
                found_model_numbers.add(model_no)
                
                if model_no not in model_data_map:
                    tray_type_name = mmc['model_stock_no__tray_type__tray_type'] or "No Tray Type"
                    dynamic_capacity = self.get_dynamic_tray_capacity(tray_type_name) if tray_type_name != "No Tray Type" else 0
                    model_data_map[model_no] = {
                        'version': mmc['version__version_name'] or "No Version",
                        'vendor': mmc['vendor_internal'] or "No Vendor",
                        'location': mmc['location__location_name'] or "No Location",
                        'tray_type': tray_type_name,
                        'tray_capacity': dynamic_capacity,
                        'plating_stk_no': mmc['plating_stk_no'] or "No Plating Stock No",
                        'polishing_stk_no': mmc['polishing_stk_no'] or "No Polishing Stock No",
                        'plating_color': "N/A",
                        'source': 'ModelMasterCreation'
                    }

            # 🔥 NEW: For missing models, try RecoveryMasterCreation
            missing_model_numbers = numeric_model_nos - found_model_numbers
            
            if missing_model_numbers:
                print(f"🔍 Checking RecoveryMasterCreation for missing models: {missing_model_numbers}")
                
                recovery_master_creations = RecoveryMasterCreation.objects.filter(
                    model_stock_no__model_no__in=missing_model_numbers
                ).select_related(
                    'version', 
                    'model_stock_no', 
                    'model_stock_no__tray_type', 
                    'location'
                ).values(
                    'model_stock_no__model_no', 
                    'version__version_name',
                    'vendor_internal',
                    'location__location_name',
                    'model_stock_no__tray_type__tray_type',
                    'model_stock_no__tray_capacity',
                    'plating_stk_no',
                    'polishing_stk_no',
                ).distinct()
                
                for rmc in recovery_master_creations:
                    model_no = rmc['model_stock_no__model_no']
                    
                    if model_no not in model_data_map:
                        tray_type_name = rmc['model_stock_no__tray_type__tray_type'] or "No Tray Type"
                        dynamic_capacity = self.get_dynamic_tray_capacity(tray_type_name) if tray_type_name != "No Tray Type" else 0
                        model_data_map[model_no] = {
                            'version': rmc['version__version_name'] or "No Version",
                            'vendor': rmc['vendor_internal'] or "No Vendor", 
                            'location': rmc['location__location_name'] or "No Location",
                            'tray_type': tray_type_name,
                            'tray_capacity': dynamic_capacity,
                            'plating_stk_no': rmc['plating_stk_no'] or "No Plating Stock No",
                            'polishing_stk_no': rmc['polishing_stk_no'] or "No Polishing Stock No",
                            'plating_color': "N/A",
                            'source': 'RecoveryMasterCreation'
                        }
                        print(f"✅ Found {model_no} in RecoveryMasterCreation")

            # SEPARATE QUERY TO GET PLATING COLORS - ENHANCED FOR DUAL SOURCE
            print("=== DEBUGGING PLATING COLOR FIELD ===")
            try:
                # Try ModelMasterCreation first
                mmc_sample = ModelMasterCreation.objects.first()
                if mmc_sample and hasattr(mmc_sample, 'plating_color'):
                    if isinstance(mmc_sample.plating_color, str):
                        # Direct string field - try both tables
                        plating_colors = ModelMasterCreation.objects.filter(
                            model_stock_no__model_no__in=all_model_numbers
                        ).values('model_stock_no__model_no', 'plating_color').distinct()
                        
                        for pc in plating_colors:
                            model_no = pc['model_stock_no__model_no']
                            if model_no in model_data_map:
                                model_data_map[model_no]['plating_color'] = pc['plating_color'] or "N/A"
                        
                        # Also try RecoveryMasterCreation for plating colors
                        recovery_plating_colors = RecoveryMasterCreation.objects.filter(
                            model_stock_no__model_no__in=missing_model_numbers
                        ).values('model_stock_no__model_no', 'plating_color').distinct()
                        
                        for pc in recovery_plating_colors:
                            model_no = pc['model_stock_no__model_no']
                            if model_no in model_data_map:
                                model_data_map[model_no]['plating_color'] = pc['plating_color'] or "N/A"
                    
                    elif hasattr(mmc_sample.plating_color, '_meta'):
                        # Related object field
                        related_model = type(mmc_sample.plating_color)
                        related_fields = [f.name for f in related_model._meta.get_fields()]
                        
                        color_field = None
                        for field_name in ['name', 'color_name', 'color', 'title']:
                            if field_name in related_fields:
                                color_field = field_name
                                break
                        
                        if color_field:
                            # ModelMasterCreation plating colors
                            plating_colors = ModelMasterCreation.objects.filter(
                                model_stock_no__model_no__in=all_model_numbers
                            ).select_related('plating_color').values(
                                'model_stock_no__model_no', 
                                f'plating_color__{color_field}'
                            ).distinct()
                            
                            for pc in plating_colors:
                                model_no = pc['model_stock_no__model_no']
                                if model_no in model_data_map:
                                    model_data_map[model_no]['plating_color'] = pc[f'plating_color__{color_field}'] or "N/A"
                            
                            # RecoveryMasterCreation plating colors
                            recovery_plating_colors = RecoveryMasterCreation.objects.filter(
                                model_stock_no__model_no__in=missing_model_numbers
                            ).select_related('plating_color').values(
                                'model_stock_no__model_no', 
                                f'plating_color__{color_field}'
                            ).distinct()
                            
                            for pc in recovery_plating_colors:
                                model_no = pc['model_stock_no__model_no']
                                if model_no in model_data_map:
                                    model_data_map[model_no]['plating_color'] = pc[f'plating_color__{color_field}'] or "N/A"
                
            except Exception as e:
                print(f"Error debugging plating color: {e}")

            # Fetch images for each model
            print(f"🔍 DEBUG: Looking up images for models: {all_model_numbers}")
            
            # ✅ FIX: Create clean model mapping for image lookup (like Inprocess Inspection)
            clean_model_mapping = {}
            for model_no in all_model_numbers:
                clean_model_no = model_no
                # Extract just the numeric part (e.g., "1805SAD02" -> "1805")
                match = re.match(r'^(\d+)', str(model_no))
                if match:
                    clean_model_no = match.group(1)
                clean_model_mapping[model_no] = clean_model_no
                print(f"🔍 Model mapping: {model_no} -> {clean_model_no}")
            
            # Get unique clean model numbers for lookup
            clean_model_numbers = set(clean_model_mapping.values())
            
            model_masters = ModelMaster.objects.filter(
                model_no__in=clean_model_numbers
            ).prefetch_related('images').order_by('model_no', 'plating_stk_no')

            # Create lookup map: deduplicate by model_no, prefer the record that has images
            # Also build a direct plating_stk_no map for exact-match lookup
            clean_model_images = {}
            plating_stk_images = {}  # plating_stk_no -> image data
            seen_clean = set()
            from modelmasterapp.image_utils import sort_images_front_first
            for model_master in model_masters:
                images = sort_images_front_first(model_master.images.all())
                img_urls_mm = [img.master_image.url for img in images if img.master_image]

                # Always record by plating_stk_no (exact match for fallback)
                if model_master.plating_stk_no and img_urls_mm:
                    plating_stk_images[model_master.plating_stk_no] = {
                        'images': img_urls_mm, 'first_image': img_urls_mm[0]
                    }

                # For clean model_no key: only store if has images, or first occurrence
                mn = model_master.model_no
                if mn not in clean_model_images:
                    if img_urls_mm:
                        clean_model_images[mn] = {'images': img_urls_mm, 'first_image': img_urls_mm[0]}
                    seen_clean.add(mn)
                elif not clean_model_images[mn]['images'] and img_urls_mm:
                    # Upgrade to a record that actually has images
                    clean_model_images[mn] = {'images': img_urls_mm, 'first_image': img_urls_mm[0]}
            
            # Map back to original model numbers
            for original_model, clean_model in clean_model_mapping.items():
                # First: exact plating_stk_no match (most specific)
                if original_model in plating_stk_images:
                    model_images_map[original_model] = plating_stk_images[original_model]
                    print(f"📸 Mapped {original_model} via exact plating_stk_no -> {len(plating_stk_images[original_model]['images'])} images")
                elif clean_model in clean_model_images:
                    model_images_map[original_model] = clean_model_images[clean_model]
                    print(f"📸 Mapped {original_model} -> {clean_model} -> {len(clean_model_images[clean_model]['images'])} images")
                else:
                    model_images_map[original_model] = {'images': [], 'first_image': None}
                    print(f"❌ No images found for {original_model} (clean: {clean_model})")

            # Fallback: for models still missing images, scan ModelMaster by plating_stk_no
            missing_images = {m for m, v in model_images_map.items() if not v.get('images')}
            if missing_images:
                print(f"🔍 Searching plating_stk_no variants for models without images: {missing_images}")
                # Also try ModelMasterCreation.images directly (may differ from ModelMaster.images)
                for orig_no in list(missing_images):
                    _mmc_direct = ModelMasterCreation.objects.filter(
                        plating_stk_no=orig_no
                    ).prefetch_related('images').first()
                    if _mmc_direct and _mmc_direct.images.exists():
                        from modelmasterapp.image_utils import sort_images_front_first
                        _mmc_urls = [img.master_image.url for img in sort_images_front_first(_mmc_direct.images.all()) if img.master_image]
                        if _mmc_urls:
                            model_images_map[orig_no] = {'images': _mmc_urls, 'first_image': _mmc_urls[0]}
                            missing_images.discard(orig_no)
                            print(f"📸 MMC direct images: '{orig_no}' -> {len(_mmc_urls)} images")
                # Scan ModelMaster by plating_stk_no for any remaining
                if missing_images:
                    plating_candidates = ModelMaster.objects.filter(
                        plating_stk_no__isnull=False
                    ).exclude(plating_stk_no='').prefetch_related('images')
                    for mm in plating_candidates:
                        if not mm.plating_stk_no:
                            continue
                        for orig_no in list(missing_images):
                            _numeric = re.match(r'^(\d+)', str(orig_no))
                            _numeric_no = _numeric.group(1) if _numeric else None
                            # Match: exact plating_stk_no equality OR plating_stk_no starts with numeric prefix
                            if (mm.plating_stk_no == orig_no or
                                    (_numeric_no and str(mm.plating_stk_no).startswith(_numeric_no))) and mm.images.exists():
                                img_list = sort_images_front_first(mm.images.all())
                                img_urls_fb = [img.master_image.url for img in img_list if img.master_image]
                                if img_urls_fb:
                                    model_images_map[orig_no] = {'images': img_urls_fb, 'first_image': img_urls_fb[0]}
                                    missing_images.discard(orig_no)
                                    print(f"📸 Fallback: mapped '{orig_no}' via plating_stk_no '{mm.plating_stk_no}'")
                                    break

        # Build batch_id → ModelMasterCreation → images map
        # Checks ModelMasterCreation.images first (direct), then ModelMaster.images via FK
        # This is the reliable fallback for Jig Loading lots (which have their own lot_id scheme)
        batch_images_map = {}
        batch_tray_map = {}          # batch_id → (tray_type_str, tray_capacity_int)
        batch_polish_finish_map = {} # batch_id → polish_finish_str
        if all_batch_ids:
            for _bmmc in ModelMasterCreation.objects.filter(
                batch_id__in=all_batch_ids
            ).select_related('model_stock_no').prefetch_related('images', 'model_stock_no__images'):
                if not _bmmc.batch_id:
                    continue
                # Capture tray and polish_finish (plain CharField/IntegerField on MMC)
                if _bmmc.tray_type:
                    _tc = _bmmc.tray_capacity if _bmmc.tray_capacity else self.get_dynamic_tray_capacity(_bmmc.tray_type)
                    batch_tray_map[_bmmc.batch_id] = (_bmmc.tray_type, _tc)
                if _bmmc.polish_finish:
                    batch_polish_finish_map[_bmmc.batch_id] = _bmmc.polish_finish
                # Prefer ModelMasterCreation.images directly
                from modelmasterapp.image_utils import sort_images_front_first
                _bimgs = [img.master_image.url for img in sort_images_front_first(_bmmc.images.all()) if img.master_image]
                if not _bimgs and _bmmc.model_stock_no:
                    # Fall back to ModelMaster.images via FK
                    _bimgs = [img.master_image.url for img in sort_images_front_first(_bmmc.model_stock_no.images.all()) if img.master_image]
                if _bimgs:
                    batch_images_map[_bmmc.batch_id] = {'images': _bimgs, 'first_image': _bimgs[0]}
                    print(f"📦 batch_images: {_bmmc.batch_id} -> {len(_bimgs)} images")

        # Process each JigCompleted to attach all information
        for jig_detail in jig_unload:
            # Check if this lot_id already has unload data
            jig_detail.is_unloaded = JigUnload_TrayId.objects.filter(lot_id=jig_detail.lot_id).exists()

            # Type of Input (Fresh/Recovery): prefer lot_id_quantities keys resolved via TotalStockModel,
            # fall back to batch_id-keyed lookup for Jig Loading-origin lots.
            jig_detail.type_of_input = 'Fresh'
            _jd_lot_ids = list(jig_detail.lot_id_quantities.keys()) if getattr(jig_detail, 'lot_id_quantities', None) else []
            for _jd_lid in _jd_lot_ids:
                if _jd_lid in type_of_input_map:
                    jig_detail.type_of_input = type_of_input_map[_jd_lid]
                    break
            else:
                _jd_batch_id = getattr(jig_detail, 'batch_id', None)
                if _jd_batch_id and _jd_batch_id in batch_type_of_input_map:
                    jig_detail.type_of_input = batch_type_of_input_map[_jd_batch_id]

             # --- Tray Info Fallback Logic ---
            # Try to get tray_type and tray_capacity from lot-level, else fallback to model-level
            tray_type = None
            tray_capacity = None
            
            # 1. Try lot-level (from lot_id_quantities)
            if hasattr(jig_detail, 'lot_id_quantities') and jig_detail.lot_id_quantities:
                for lot_id in jig_detail.lot_id_quantities.keys():
                    lot_data = self.get_lot_specific_data(lot_id, None)
                    if lot_data and lot_data.get('tray_type') and lot_data.get('tray_type') != 'No Tray Type':
                        tray_type = lot_data['tray_type']
                    if lot_data and lot_data.get('tray_capacity') and lot_data.get('tray_capacity') > 0:
                        tray_capacity = lot_data['tray_capacity']
                    if tray_type and tray_capacity:
                        break
            
            # 2. Fallback to model-level (from first model in no_of_model_cases)
            if (not tray_type or not tray_capacity) and hasattr(jig_detail, 'no_of_model_cases') and jig_detail.no_of_model_cases:
                model_no = list(jig_detail.no_of_model_cases)[0]
                model_data = model_data_map.get(model_no)
                if model_data:
                    if not tray_type and model_data.get('tray_type') and model_data.get('tray_type') != 'No Tray Type':
                        tray_type = model_data['tray_type']
                    if not tray_capacity and model_data.get('tray_capacity') and model_data.get('tray_capacity') > 0:
                        tray_capacity = model_data['tray_capacity']
            
            # 3. Set on jig_detail for template
            jig_detail.tray_type = tray_type
            jig_detail.tray_capacity = tray_capacity
            # Fix tray_type from batch_tray_map if still missing
            if not jig_detail.tray_type or jig_detail.tray_type == 'No Tray Type':
                _jd_b1 = getattr(jig_detail, 'batch_id', None)
                if _jd_b1 and _jd_b1 in batch_tray_map:
                    _tt1, _tc1 = batch_tray_map[_jd_b1]
                    jig_detail.tray_type = _tt1
                    jig_detail.tray_capacity = self.get_dynamic_tray_capacity(_tt1)
            # Fix polish_finish_name for Jig Loading lots (not in TotalStockModel annotation)
            if not getattr(jig_detail, 'polish_finish_name', None):
                _jd_b1 = getattr(jig_detail, 'batch_id', None)
                if _jd_b1 and _jd_b1 in batch_polish_finish_map:
                    jig_detail.polish_finish_name = batch_polish_finish_map[_jd_b1]
            # Extract bath_number string from draft_data if bath_numbers FK is None
            # Explicitly resolve by FK ID so union() doesn't break lazy-load
            _bath_fk_id_z1 = getattr(jig_detail, 'bath_numbers_id', None)
            if _bath_fk_id_z1:
                _bn_row_z1 = BathNumbers.objects.filter(id=_bath_fk_id_z1).values('bath_number').first()
                jig_detail.bath_number = _bn_row_z1['bath_number'] if _bn_row_z1 else None
            else:
                _dd1 = getattr(jig_detail, 'draft_data', {}) or {}
                _bn_str1 = (_dd1.get('bath_number') or _dd1.get('bath_numbers') or _dd1.get('nickel_bath_number') or _dd1.get('bath_no') or _dd1.get('nickel_bath_type'))
                jig_detail.bath_number = str(_bn_str1) if _bn_str1 else None

            # Fallback: if bath_number still None, try to find from another JigCompleted with same jig_id
            if not jig_detail.bath_number:
                _jig_id_z1 = getattr(jig_detail, 'jig_id', None)
                if _jig_id_z1:
                    _sibling_jc = JigCompleted.objects.filter(
                        jig_id=_jig_id_z1, bath_numbers__isnull=False
                    ).values('bath_numbers__bath_number').first()
                    if _sibling_jc:
                        jig_detail.bath_number = _sibling_jc['bath_numbers__bath_number']
            
            # *** NEW: Add helper properties for lot_id_quantities values ***
            if hasattr(jig_detail, 'lot_id_quantities') and jig_detail.lot_id_quantities:
                jig_detail.quantity_values = list(jig_detail.lot_id_quantities.values())
                jig_detail.quantity_values_str = ", ".join(map(str, jig_detail.lot_id_quantities.values()))
                jig_detail.total_quantity = sum(jig_detail.lot_id_quantities.values())
            else:
                jig_detail.quantity_values = []
                jig_detail.quantity_values_str = "0"
                jig_detail.total_quantity = 0
            
            # 🚀 ENHANCED: Dual-table lot_id to model mapping (using full plating_stk_no)
            lot_id_model_map = {}
            if getattr(jig_detail, 'lot_id_quantities', None):
                for lot_id in jig_detail.lot_id_quantities.keys():
                    plating_stk_no = None
                    
                    # Try TotalStockModel → batch_id → ModelMasterCreation first
                    tsm = TotalStockModel.objects.filter(lot_id=lot_id).first()
                    if tsm and tsm.batch_id:
                        mmc = ModelMasterCreation.objects.filter(id=tsm.batch_id.id).first()
                        if mmc:
                            plating_stk_no = getattr(mmc, 'plating_stk_no', None) or (mmc.model_stock_no.model_no if mmc.model_stock_no else None)
                            print(f"🎯 Found {lot_id} in ModelMasterCreation -> {plating_stk_no}")
                    
                    if not plating_stk_no:
                        # 🔥 NEW: Try RecoveryStockModel → batch_id → RecoveryMasterCreation
                        rsm = RecoveryStockModel.objects.filter(lot_id=lot_id).first()
                        if rsm and rsm.batch_id:
                            rmc = RecoveryMasterCreation.objects.filter(id=rsm.batch_id.id).first()
                            if rmc:
                                plating_stk_no = getattr(rmc, 'plating_stk_no', None) or (rmc.model_stock_no.model_no if rmc.model_stock_no else None)
                                print(f"🔄 Found {lot_id} in RecoveryMasterCreation -> {plating_stk_no}")
                    
                    if not plating_stk_no:
                        # Existing fallback logic
                        tray = TrayId.objects.filter(lot_id=lot_id).select_related('batch_id__model_stock_no').first()
                        if tray and tray.batch_id:
                            plating_stk_no = getattr(tray.batch_id, 'plating_stk_no', None) or (tray.batch_id.model_stock_no.model_no if tray.batch_id.model_stock_no else None)
                            print(f"📦 Found {lot_id} in TrayId -> {plating_stk_no}")
                        else:
                            # Final fallback - use model_no from stock models
                            if tsm and hasattr(tsm, 'model_stock_no') and tsm.model_stock_no:
                                plating_stk_no = tsm.model_stock_no.model_no
                                print(f"📊 Found {lot_id} in TotalStockModel (fallback) -> {plating_stk_no}")
                            elif rsm and hasattr(rsm, 'model_stock_no') and rsm.model_stock_no:
                                plating_stk_no = rsm.model_stock_no.model_no
                                print(f"♻️ Found {lot_id} in RecoveryStockModel (fallback) -> {plating_stk_no}")
                    
                    lot_id_model_map[lot_id] = plating_stk_no
            
            print(f"DEBUG: {jig_detail.lot_id} lot_id_model_map = {lot_id_model_map}")
            jig_detail.lot_id_model_map = lot_id_model_map
            
            # Also check for individual model lot_ids if they exist
            if hasattr(jig_detail, 'lot_id_list') and jig_detail.lot_id_list:
                unloaded_model_lot_ids = set(
                    JigUnload_TrayId.objects.filter(
                        lot_id__in=jig_detail.lot_id_list
                    ).values_list('lot_id', flat=True)
                )
                jig_detail.unloaded_model_lot_ids = list(unloaded_model_lot_ids)
            else:
                jig_detail.unloaded_model_lot_ids = []

            # Normalize no_of_model_cases into a list BEFORE the color-assignment
            # loop below. At this point it is still the raw DB TextField string
            # ('MODEL(lot_id):qty | MODEL2(lot_id2):qty2' for multi-model, or a
            # bare 'MODEL' for single-model) - iterating a plain string below
            # walks it character-by-character, so every "model" is a single
            # letter that never matches global_model_colors and falls back to
            # gray. Zone 2 never hits this because it list-normalizes earlier
            # (create_enhanced_jig_detail); mirror that here with the same
            # parsing rules used later in this view (see ~L1573-1599).
            _raw_nomc = jig_detail.no_of_model_cases
            if isinstance(_raw_nomc, str) and _raw_nomc.strip():
                try:
                    _parsed_nomc = json.loads(_raw_nomc)
                    jig_detail.no_of_model_cases = _parsed_nomc if isinstance(_parsed_nomc, list) else [str(_parsed_nomc)]
                except Exception:
                    _nomc_parts = _raw_nomc.split(' | ') if ' | ' in _raw_nomc else _raw_nomc.split(',')
                    jig_detail.no_of_model_cases = [
                        _cleaned
                        for _cleaned in (
                            re.split(r'[\(\[]', _part, maxsplit=1)[0].split(':')[0].strip()
                            for _part in _nomc_parts
                        )
                        if _cleaned
                    ]

            if jig_detail.no_of_model_cases:
                # Initialize collections for this jig
                jig_versions = {}
                jig_vendors = {}
                jig_locations = {}
                jig_tray_types = {}
                jig_tray_capacities = {}
                jig_plating_stk_nos = {}
                jig_polishing_stk_nos = {}
                jig_plating_colors = {}
                jig_model_colors = {}
                jig_model_images = {}
                
                all_versions = []
                all_vendors = []
                all_locations = []
                all_tray_types = []
                all_tray_capacities = []
                all_plating_stk_nos = []
                all_polishing_stk_nos = []
                
                # 🚀 FIXED: Use no_of_model_cases as primary source for color assignment
                # This ensures we use full model numbers instead of truncated database values
                print(f"🎯 PRIMARY COLOR ASSIGNMENT: Using no_of_model_cases: {jig_detail.no_of_model_cases}")
                
                for model_no in jig_detail.no_of_model_cases:
                    print(f"🔍 PRIMARY: Processing model_no='{model_no}'")
                    
                    # Use global color mapping with full model number
                    model_key = str(model_no)  # Force string conversion
                    assigned_color = global_model_colors.get(model_key, "#cccccc")
                    jig_model_colors[model_key] = assigned_color
                    print(f"🎨 PRIMARY: Assigned color {assigned_color} to full model '{model_key}'")
                    
                    # Get images for this model
                    if model_key in model_images_map:
                        jig_model_images[model_key] = model_images_map[model_key]
                    else:
                        jig_model_images[model_key] = {'images': [], 'first_image': None}
                    
                    # Get additional data from model_data_map if available (with numeric prefix fallback)
                    model_data = model_data_map.get(model_key)
                    if not model_data:
                        _nm2 = re.match(r'^(\d+)', model_key)
                        if _nm2:
                            model_data = model_data_map.get(_nm2.group(1))
                    if model_data:
                        
                        # Collect from model_data_map
                        version = model_data['version']
                        if version and version != "No Version":
                            jig_versions[model_key] = version
                            all_versions.append(version)
                        
                        vendor = model_data['vendor']
                        if vendor and vendor != "No Vendor":
                            all_vendors.append(vendor)
                        
                        location = model_data['location']
                        if location and location != "No Location":
                            all_locations.append(location)
                        
                        tray_type = model_data['tray_type']
                        if tray_type and tray_type != "No Tray Type":
                            all_tray_types.append(tray_type)
                        
                        tray_capacity = model_data['tray_capacity']
                        if tray_capacity and tray_capacity > 0:
                            all_tray_capacities.append(tray_capacity)
                        
                        plating_stk_no = model_data['plating_stk_no']
                        if plating_stk_no and plating_stk_no != "No Plating Stock No":
                            all_plating_stk_nos.append(plating_stk_no)
                        
                        # Use plating_stk_no for display (polishing_stk_no has X mask)
                        _display_polishing = model_data['plating_stk_no']
                        if _display_polishing and _display_polishing != "No Plating Stock No":
                            all_polishing_stk_nos.append(_display_polishing)
                
                all_plating_colors = []
                
                # Replace the lot_id_quantities processing section with this version that keeps ALL values including duplicates:

                # 🚀 ENHANCED: Use lot_id_quantities for comprehensive lot-specific data collection (INCLUDING DUPLICATES)
                if hasattr(jig_detail, 'lot_id_quantities') and jig_detail.lot_id_quantities:
                    print(f"🔥 Processing lot_id_quantities for ALL individual values: {jig_detail.lot_id_quantities}")
                    
                    for lot_id, quantity in jig_detail.lot_id_quantities.items():
                        # Get model_no for this specific lot_id
                        model_no = jig_detail.lot_id_model_map.get(lot_id)
                        if not model_no:
                            print(f"❌ No model_no found for lot_id: {lot_id}")
                            continue
                            
                        print(f"🔄 Processing lot_id: {lot_id} -> model_no: {model_no}")
                        
                        # 🐛 DEBUG: Check for model number truncation
                        print(f"🔍 DEBUG: model_no type: {type(model_no)}, value: '{model_no}', length: {len(str(model_no))}")
                        
                        # Use global color mapping for model_no with explicit string conversion
                        model_key = str(model_no)  # Force string conversion
                        assigned_color = global_model_colors.get(model_key, "#cccccc")
                        # 🚨 SKIP: Don't assign color here - potentially truncated model_no
                        # jig_model_colors[model_key] = assigned_color  # COMMENTED OUT
                        print(f"🚨 SKIPPING color assignment for potentially truncated '{model_key}' -> {assigned_color}")
                        print(f"🔍 DEBUG: jig_model_colors kept unchanged: {jig_model_colors}")
                        
                        # Get images for this model
                        if model_no in model_images_map:
                            jig_model_images[model_no] = model_images_map[model_no]
                        else:
                            jig_model_images[model_no] = {
                                'images': [], 'first_image': None}
                        
                        # 🎯 PRIMARY: Get comprehensive lot-specific data
                        lot_specific_data = self.get_lot_specific_data(lot_id, model_no)
                        
                        if lot_specific_data:
                            # Store lot-specific data in dictionaries
                            # Complete field mapping per lot_id
                            jig_versions[lot_id] = lot_specific_data['version']
                            jig_vendors[lot_id] = lot_specific_data['vendor']
                            jig_locations[lot_id] = lot_specific_data['location']
                            jig_tray_types[lot_id] = lot_specific_data['tray_type']
                            jig_tray_capacities[lot_id] = lot_specific_data['tray_capacity']
                            jig_plating_stk_nos[lot_id] = lot_specific_data['plating_stk_no']
                            jig_polishing_stk_nos[lot_id] = lot_specific_data['polishing_stk_no']
                            jig_plating_colors[lot_id] = lot_specific_data['plating_color']
                            # ✅ COLLECT ALL VALUES (INCLUDING DUPLICATES)
                            version = lot_specific_data['version']
                            if version and version != "No Version":
                                all_versions.append(version)  # NO duplicate check - add every value
                                print(f"✅ Added version: '{version}' from lot_id: {lot_id}")
                            
                            vendor = lot_specific_data['vendor']
                            if vendor and vendor != "No Vendor":
                                all_vendors.append(vendor)  # NO duplicate check
                            
                            location = lot_specific_data['location']
                            if location and location != "No Location":
                                all_locations.append(location)  # NO duplicate check
                            
                            tray_type = lot_specific_data['tray_type']
                            if tray_type and tray_type != "No Tray Type":
                                all_tray_types.append(tray_type)  # NO duplicate check
                            
                            tray_capacity = lot_specific_data['tray_capacity']
                            if tray_capacity and tray_capacity > 0:
                                all_tray_capacities.append(tray_capacity)  # NO duplicate check
                            
                            # ✅ CRITICAL: Collect ALL plating_stk_no values (including duplicates)
                            plating_stk_no = lot_specific_data['plating_stk_no']
                            if plating_stk_no and plating_stk_no != "No Plating Stock No":
                                all_plating_stk_nos.append(plating_stk_no)  # NO duplicate check - add every value
                                print(f"✅ Added plating_stk_no: '{plating_stk_no}' from lot_id: {lot_id}")
                            
                            # ✅ CRITICAL: Use plating_stk_no for display (polishing_stk_no has X mask)
                            _disp_pol = lot_specific_data['plating_stk_no']
                            if _disp_pol and _disp_pol != "No Plating Stock No":
                                all_polishing_stk_nos.append(_disp_pol)  # NO duplicate check - add every value
                                print(f"✅ Added polishing_stk_no (from plating): '{_disp_pol}' from lot_id: {lot_id}")
                            
                            plating_color = lot_specific_data['plating_color']
                            if plating_color and plating_color != "N/A":
                                all_plating_colors.append(plating_color)  # NO duplicate check
                            
                            print(f"✅ Processed lot {lot_id}: plating={plating_stk_no}, version={version}")
                            
                        else:
                            print(f"⚠️ No lot-specific data from get_lot_specific_data for lot_id: {lot_id}, trying fallback...")
                            
                            # ✅ ENHANCED FALLBACK: Get data directly from stock models
                            stock_model, is_recovery, batch_model_class = self.get_stock_model_data(lot_id)
                            if stock_model:
                                print(f"📋 Using stock model fallback for lot_id: {lot_id}")
                                
                                # Extract plating_stk_no directly from stock model
                                fallback_plating = getattr(stock_model, 'plating_stk_no', None)
                                if fallback_plating:
                                    all_plating_stk_nos.append(fallback_plating)  # NO duplicate check
                                    jig_plating_stk_nos[lot_id] = fallback_plating
                                    print(f"📋 Added fallback plating_stk_no: '{fallback_plating}'")
                                
                                # Use plating_stk_no for display (polishing_stk_no has X mask)
                                if fallback_plating:
                                    all_polishing_stk_nos.append(fallback_plating)  # NO duplicate check
                                    jig_polishing_stk_nos[lot_id] = fallback_plating
                                    print(f"📋 Added fallback polishing_stk_no (from plating): '{fallback_plating}'")
                                
                                # Extract version from stock model
                                fallback_version = None
                                if hasattr(stock_model, 'version') and stock_model.version:
                                    if hasattr(stock_model.version, 'version_internal'):
                                        fallback_version = stock_model.version.version_internal
                                    elif hasattr(stock_model.version, 'version_name'):
                                        fallback_version = stock_model.version.version_name
                                    else:
                                        fallback_version = str(stock_model.version)
                                
                                if fallback_version:
                                    all_versions.append(fallback_version)  # NO duplicate check
                                    jig_versions[lot_id] = fallback_version
                                    print(f"📋 Added fallback version: '{fallback_version}'")
                            else:
                                print(f"❌ No fallback data available for lot_id: {lot_id}")
                
                else:
                    # Fallback to model-based logic if no lot_id_quantities
                    print(f"⚠️ No lot_id_quantities found, falling back to model-based logic for {jig_detail.lot_id}")
                    for model_no in jig_detail.no_of_model_cases:
                        # 🐛 DEBUG: Check for model number truncation in fallback
                        print(f"🔍 FALLBACK DEBUG: model_no type: {type(model_no)}, value: '{model_no}', length: {len(str(model_no))}")
                        
                        # Use global color mapping for model_no with explicit string conversion
                        model_key = str(model_no)  # Force string conversion
                        assigned_color = global_model_colors.get(model_key, "#cccccc")
                        # 🚨 SKIP: Color already assigned in primary section above
                        # jig_model_colors[model_key] = assigned_color  # COMMENTED OUT
                        print(f"🚨 FALLBACK SKIPPED: Color already assigned for '{model_key}' -> {assigned_color}")
                        print(f"🔍 FALLBACK DEBUG: jig_model_colors unchanged: {jig_model_colors}")
                        
                        # Get images for this model
                        if model_no in model_images_map:
                            jig_model_images[model_no] = model_images_map[model_no]
                            print(f"📸 Found images for model {model_no}: {len(model_images_map[model_no]['images'])} images")
                        else:
                            jig_model_images[model_no] = {'images': [], 'first_image': None}
                            print(f"❌ No images found for model {model_no}")
                            
                        # Special debug for 1805 models
                        if '1805' in str(model_no):
                            print(f"🎯 1805 MODEL DEBUG:")
                            print(f"   model_no: {model_no}")
                            print(f"   in model_images_map: {model_no in model_images_map}")
                            if model_no in model_images_map:
                                print(f"   images count: {len(model_images_map[model_no]['images'])}")
                                print(f"   first_image: {model_images_map[model_no]['first_image']}")
                            print(f"   assigned images: {jig_model_images.get(model_no, 'NOT FOUND')}")

                    # Attach all collected data to jig_detail object
                    jig_detail.model_colors = jig_model_colors
                    jig_detail.model_images = jig_model_images
                    
                    # Debug log for 1805 models
                    has_1805 = any('1805' in str(k) for k in jig_model_images.keys())
                    if has_1805:
                        print(f"🎯 JIG {jig_detail.lot_id} - Final model_images with 1805:")
                        for model_key, image_data in jig_model_images.items():
                            if '1805' in str(model_key):
                                print(f"   {model_key}: {len(image_data['images'])} images, first: {image_data['first_image']}")
                    
                    # Basic debug info
                    print(f"📊 JIG {jig_detail.jig_id} - Total models: {len(jig_model_images)}, Colors: {len(jig_model_colors)}")

                    # 🎨 ENHANCED DEBUG: Print color assignments for this jig
                    print(f"🎨 FINAL COLOR ASSIGNMENTS for {jig_detail.lot_id}:")
                    print(f"   🎯 model_colors dict: {jig_detail.model_colors}")
                    print(f"   📋 no_of_model_cases list: {jig_detail.no_of_model_cases}")
                    for model_no in (jig_detail.no_of_model_cases or []):
                        color = jig_detail.model_colors.get(model_no, 'NOT_FOUND')
                        print(f"   🔍 Model {model_no} -> Color {color}")
                        
                        # 🐛 EXTRA DEBUG: Check model number type and value
                        print(f"   🔍 EXTRA DEBUG: model_no type: {type(model_no)}, repr: {repr(model_no)}")
                        
                        if model_no in model_data_map:
                            model_data = model_data_map[model_no]
                            
                            # Collect from model_data_map (keep duplicates here too for consistency)
                            version = model_data['version']
                            if version and version != "No Version":
                                jig_versions[model_no] = version
                                all_versions.append(version)  # NO duplicate check
                            
                            vendor = model_data['vendor']
                            if vendor and vendor != "No Vendor":
                                jig_vendors[model_no] = vendor
                                all_vendors.append(vendor)  # NO duplicate check
                            
                            location = model_data['location']
                            if location and location != "No Location":
                                jig_locations[model_no] = location
                                all_locations.append(location)  # NO duplicate check
                            
                            tray_type = model_data['tray_type']
                            if tray_type and tray_type != "No Tray Type":
                                jig_tray_types[model_no] = tray_type
                                all_tray_types.append(tray_type)  # NO duplicate check
                            
                            tray_capacity = model_data['tray_capacity']
                            if tray_capacity and tray_capacity > 0:
                                jig_tray_capacities[model_no] = tray_capacity
                                all_tray_capacities.append(tray_capacity)  # NO duplicate check
                            
                            # ✅ CRITICAL: Collect ALL values including duplicates
                            plating_stk_no = model_data['plating_stk_no']
                            if plating_stk_no and plating_stk_no != "No Plating Stock No":
                                jig_plating_stk_nos[model_no] = plating_stk_no
                                all_plating_stk_nos.append(plating_stk_no)  # NO duplicate check
                            
                            # Use plating_stk_no for display (polishing_stk_no has X mask)
                            _disp_pol2 = model_data['plating_stk_no']
                            if _disp_pol2 and _disp_pol2 != "No Plating Stock No":
                                jig_polishing_stk_nos[model_no] = _disp_pol2
                                all_polishing_stk_nos.append(_disp_pol2)  # NO duplicate check
                              
                            plating_color = model_data['plating_color']
                            if plating_color and plating_color != "N/A":
                                jig_plating_colors[model_no] = plating_color
                                all_plating_colors.append(plating_color)  # NO duplicate check
                
                # Attach all collected data to jig_detail object
                jig_detail.model_versions = jig_versions
                jig_detail.model_vendors = jig_vendors
                jig_detail.model_locations = jig_locations
                jig_detail.model_tray_types = jig_tray_types
                jig_detail.model_tray_capacities = jig_tray_capacities
                jig_detail.model_plating_stk_nos = jig_plating_stk_nos
                jig_detail.model_polishing_stk_nos = jig_polishing_stk_nos
                jig_detail.model_plating_colors = jig_plating_colors
                
                jig_detail.model_colors = jig_model_colors
                jig_detail.model_images = jig_model_images
                
                # *** NEW: Prepare colored quantity data for multi-model display ***
                if hasattr(jig_detail, 'lot_id_quantities') and jig_detail.lot_id_quantities:
                    jig_detail.colored_quantities = []
                    if hasattr(jig_detail, 'lot_id_model_map') and jig_detail.lot_id_model_map:
                        for lot_id, quantity in jig_detail.lot_id_quantities.items():
                            model_no = jig_detail.lot_id_model_map.get(lot_id, 'Unknown')
                            color = jig_detail.model_colors.get(model_no, '#6c757d')
                            jig_detail.colored_quantities.append({
                                'quantity': quantity,
                                'model': model_no,
                                'color': color,
                                'tooltip': f'Model: {model_no}, Lot: {lot_id}, Qty: {quantity}'
                            })
                    else:
                        # Fallback: if no model mapping, use default color
                        for lot_id, quantity in jig_detail.lot_id_quantities.items():
                            jig_detail.colored_quantities.append({
                                'quantity': quantity,
                                'model': 'Unknown',
                                'color': '#6c757d',
                                'tooltip': f'Lot: {lot_id}, Qty: {quantity}'
                            })
                else:
                    jig_detail.colored_quantities = []
                
                # 🎨 DEBUG: Print color assignments for this jig
                print(f"🎨 FINAL COLOR ASSIGNMENTS for {jig_detail.lot_id}:")
                print(f"   🎯 model_colors: {jig_detail.model_colors}")
                print(f"   📋 no_of_model_cases: {jig_detail.no_of_model_cases}")
                for model_no in (jig_detail.no_of_model_cases or []):
                    color = jig_detail.model_colors.get(model_no, 'NOT_FOUND')
                    print(f"   🔍 Model {model_no} -> Color {color}")
                
                # ✅ KEEP ALL VALUES INCLUDING DUPLICATES (no set() function used)
                jig_detail.all_versions = [v for v in all_versions if v and v != "No Version"]
                jig_detail.all_vendors = [v for v in all_vendors if v and v != "No Vendor"]
                jig_detail.all_locations = [v for v in all_locations if v and v != "No Location"]
                jig_detail.all_plating_stk_nos = [v for v in all_plating_stk_nos if v and v != "No Plating Stock No"]
                jig_detail.all_polishing_stk_nos = [v for v in all_polishing_stk_nos if v and v != "No Polishing Stock No"]
                jig_detail.all_plating_colors = [v for v in all_plating_colors if v and v != "N/A"]
                
                # ✅ ALSO PROVIDE UNIQUE VERSIONS (for backward compatibility)
                jig_detail.unique_versions = sorted(list(set(jig_detail.all_versions)))
                jig_detail.unique_vendors = sorted(list(set(jig_detail.all_vendors)))
                jig_detail.unique_locations = sorted(list(set(jig_detail.all_locations)))
                jig_detail.unique_plating_stk_nos = sorted(list(set(jig_detail.all_plating_stk_nos)))
                jig_detail.unique_polishing_stk_nos = sorted(list(set(jig_detail.all_polishing_stk_nos)))
                jig_detail.unique_plating_colors = sorted(list(set(jig_detail.all_plating_colors)))
                
                # ✅ ENHANCED DEBUG: Print both all and unique values
                print(f"🎊 FINAL RESULTS for {jig_detail.lot_id}:")
                print(f"   📋 ALL plating_stk_nos ({len(jig_detail.all_plating_stk_nos)}): {jig_detail.all_plating_stk_nos}")
                print(f"   📋 ALL polishing_stk_nos ({len(jig_detail.all_polishing_stk_nos)}): {jig_detail.all_polishing_stk_nos}")
                print(f"   📋 ALL versions ({len(jig_detail.all_versions)}): {jig_detail.all_versions}")
                print(f"   🎯 UNIQUE plating_stk_nos ({len(jig_detail.unique_plating_stk_nos)}): {jig_detail.unique_plating_stk_nos}")
                print(f"   🎯 UNIQUE polishing_stk_nos ({len(jig_detail.unique_polishing_stk_nos)}): {jig_detail.unique_polishing_stk_nos}")
                print(f"   🎯 UNIQUE versions ({len(jig_detail.unique_versions)}): {jig_detail.unique_versions}")
                
                # Set default values for remaining fields
                jig_detail.jig_type = getattr(jig_detail, 'jig_type', "N/A")
                jig_detail.jig_capacity = getattr(jig_detail, 'jig_capacity', 0)
                # Safely resolve plating color — prefer explicit attribute, then annotated cast, else fallback
                plating_color_val = getattr(jig_detail, 'plating_color', None)
                if not plating_color_val:
                    plating_color_val = getattr(jig_detail, 'plating_color_cast', None)
                jig_detail.plating_color = plating_color_val or "N/A"
                
                # Calculate no_of_trays based on total_cases_loaded and tray_capacity
                valid_capacities = [cap for cap in all_tray_capacities if cap and cap > 0]
                if valid_capacities and hasattr(jig_detail, 'total_cases_loaded') and jig_detail.total_cases_loaded:
                    primary_tray_capacity = valid_capacities[0]
                    jig_detail.calculated_no_of_trays = math.ceil(jig_detail.total_cases_loaded / primary_tray_capacity)
                    jig_detail.primary_tray_capacity = primary_tray_capacity
                else:
                    jig_detail.calculated_no_of_trays = 0
                    jig_detail.primary_tray_capacity = 0
            else:
                jig_detail.model_versions = {}
                jig_detail.model_vendors = {}
                jig_detail.model_locations = {}
                jig_detail.model_tray_types = {}
                jig_detail.model_tray_capacities = {}
                jig_detail.model_plating_stk_nos = {}
                jig_detail.model_polishing_stk_nos = {}
                jig_detail.model_plating_colors = {}
                jig_detail.model_colors = {}
                jig_detail.model_images = {}
                jig_detail.unique_versions = []
                jig_detail.unique_vendors = []
                jig_detail.unique_locations = []
                jig_detail.unique_tray_types = []
                jig_detail.unique_tray_capacities = []
                jig_detail.unique_plating_stk_nos = []
                jig_detail.unique_polishing_stk_nos = []
                jig_detail.calculated_no_of_trays = 0
                jig_detail.primary_tray_capacity = 0
                jig_detail.plating_color = "N/A"
                
        # This converts the QuerySet to a list, so it MUST be last
        jig_unload = self.check_draft_status_for_jigs(jig_unload)
        
        # Ensure required fields are set for template compatibility
        for jig_detail in jig_unload:
            # Set jig_qr_id for template (use jig_id or fallback to lot_id)
            jig_detail.jig_qr_id = getattr(jig_detail, 'jig_id', None) or jig_detail.lot_id
            # Set jig_loaded_date_time for Last Updated column (fallback to updated_at)
            jig_detail.jig_loaded_date_time = getattr(jig_detail, 'IP_loaded_date_time', None) or getattr(jig_detail, 'updated_at', None)
            
            # Check if all models have been submitted for Z1 unloading
            _dd_z1 = getattr(jig_detail, 'draft_data', {}) or {}
            # Build all_lot_ids: prefer multi_model_allocation → lot_id_quantities → fallback
            _alloc_z1 = _dd_z1.get('multi_model_allocation', []) if isinstance(_dd_z1, dict) else []
            _liq_z1 = _dd_z1.get('lot_id_quantities', {}) if isinstance(_dd_z1, dict) else {}
            if _alloc_z1:
                _all_lids_z1 = set(a['lot_id'] for a in _alloc_z1 if a.get('lot_id'))
            elif _liq_z1:
                _all_lids_z1 = set(_liq_z1.keys())
            else:
                _all_lids_z1 = {jig_detail.lot_id}
            _submitted_z1 = set(
                JUSubmittedZ1.objects.filter(jig_completed_id=jig_detail.id, is_draft=False)
                .values_list('lot_id', flat=True)
            )
            jig_detail.all_models_submitted_z1 = _all_lids_z1.issubset(_submitted_z1) and len(_submitted_z1) > 0

            # Draft indicator: any JUSubmittedZ1 draft record for this jig,
            # OR any model partially submitted (final) while not all are done.
            _has_draft_z1 = JUSubmittedZ1.objects.filter(
                jig_completed_id=jig_detail.id, is_draft=True
            ).exists()
            _has_partial_submitted_z1 = (
                not jig_detail.all_models_submitted_z1
                and JUSubmittedZ1.objects.filter(
                    jig_completed_id=jig_detail.id, is_draft=False
                ).exists()
            )
            if (_has_draft_z1 or _has_partial_submitted_z1) and not jig_detail.all_models_submitted_z1:
                jig_detail.jig_unload_draft = True
                jig_detail.has_unload_draft = True

            # Already Loaded: ALL of this jig's own lots were consumed into
            # ANOTHER jig's unload via Add Model — nothing left here to unload,
            # so it must not be offered again as an Add Model candidate.
            # Must check ALL lots, not ANY: a multi-model jig where only one
            # of its lots was merged elsewhere still has its other, un-merged
            # lot pending — that leftover must keep appearing as a normal
            # candidate in other jigs' Add Model lists instead of being
            # hidden by a single merged lot dragging the whole row along.
            _merged_lot_ids_z1 = set(
                JUSubmittedZ1.objects.filter(
                    jig_completed_id=jig_detail.id, is_merged_additional=True
                ).values_list('lot_id', flat=True)
            )
            jig_detail.is_already_loaded_z1 = bool(_all_lids_z1) and _all_lids_z1.issubset(_merged_lot_ids_z1)

            # Parse draft_data if needed
            draft_data = {}
            if hasattr(jig_detail, 'draft_data') and jig_detail.draft_data:
                if isinstance(jig_detail.draft_data, str):
                    try:
                        import json
                        draft_data = json.loads(jig_detail.draft_data)
                    except:
                        draft_data = {}
                elif isinstance(jig_detail.draft_data, dict):
                    draft_data = jig_detail.draft_data
            
            # Set plating_color from draft_data or fetch from models
            plating_color = draft_data.get('plating_color')
            if not plating_color:
                # Try to get plating color from stock models
                stock_model, is_recovery, batch_model_class = self.get_stock_model_data(jig_detail.lot_id)
                if stock_model and hasattr(stock_model, 'plating_color') and stock_model.plating_color:
                    plating_color = stock_model.plating_color.plating_color
            # Fallback: JigCompleted.batch_id → ModelMasterCreation.plating_color (for Jig Loading lots)
            if not plating_color:
                _jd_b_pc = getattr(jig_detail, 'batch_id', None)
                if _jd_b_pc:
                    _mmc_pc = ModelMasterCreation.objects.filter(batch_id=_jd_b_pc).values('plating_color').first()
                    if _mmc_pc and _mmc_pc.get('plating_color'):
                        plating_color = _mmc_pc['plating_color']
            jig_detail.plating_color = plating_color or 'N/A'
            
            # Set tray info from draft_data or fetch from models
            tray_type = draft_data.get('tray_type')
            tray_capacity = draft_data.get('tray_capacity')
            if not tray_type or not tray_capacity:
                # Try to get tray info from model data
                lot_data = self.get_lot_specific_data(jig_detail.lot_id, None)
                if lot_data:
                    if not tray_type:
                        tray_type = lot_data.get('tray_type')
                    if not tray_capacity:
                        tray_capacity = lot_data.get('tray_capacity')
            jig_detail.tray_type = tray_type
            jig_detail.tray_capacity = tray_capacity
            
            # Set lot_id_quantities from draft_data or create from stock data
            lot_id_quantities = draft_data.get('lot_id_quantities', {})
            if not lot_id_quantities:
                # Use delink_tray_qty as primary source for processed quantity
                delink_qty = getattr(jig_detail, 'delink_tray_qty', 0)
                if delink_qty and delink_qty > 0:
                    lot_id_quantities = {jig_detail.lot_id: delink_qty}
                else:
                    # Try to get quantity from stock models as fallback
                    stock_model, is_recovery, batch_model_class = self.get_stock_model_data(jig_detail.lot_id)
                    if stock_model and hasattr(stock_model, 'total_stock'):
                        lot_id_quantities = {jig_detail.lot_id: stock_model.total_stock}
                    else:
                        lot_id_quantities = {jig_detail.lot_id: getattr(jig_detail, 'updated_lot_qty', 0)}
            jig_detail.lot_id_quantities = lot_id_quantities

            # Rebuild lot_id_model_map now that lot_id_quantities is properly set
            # Use jig_detail.lot_id directly as key (draft_data lot_id_quantities keys may have typos)
            if not getattr(jig_detail, 'lot_id_model_map', None) and lot_id_quantities:
                _psn = getattr(jig_detail, 'plating_stock_num', None) or draft_data.get('plating_stock_num')
                if _psn:
                    # Include jig_detail.lot_id (correct DB key) AND all lot_id_quantities keys
                    # (draft_data keys may differ due to typos; both must resolve to the same model)
                    _rebuilt_map = {jig_detail.lot_id: str(_psn).strip()}
                    for _k in lot_id_quantities.keys():
                        _rebuilt_map.setdefault(_k, str(_psn).strip())
                    jig_detail.lot_id_model_map = _rebuilt_map
                    print(f"🏷️ Rebuilt lot_id_model_map for {jig_detail.lot_id}: {jig_detail.lot_id_model_map}")

            # Ensure no_of_model_cases is a list
            model_cases = draft_data.get('no_of_model_cases', getattr(jig_detail, 'no_of_model_cases', None))
            if not model_cases:  # catches None AND empty string (single-model jigs store '' in no_of_model_cases)
                # plating_stock_num is the most reliable source for single-model jigs
                _plating_sn = getattr(jig_detail, 'plating_stock_num', None) or draft_data.get('plating_stock_num')
                if _plating_sn:
                    model_cases = [str(_plating_sn).strip()]
                else:
                    # Fallback to stock model lookup (may fail for JigCompleted lot_ids)
                    stock_model, is_recovery, batch_model_class = self.get_stock_model_data(jig_detail.lot_id)
                    if stock_model and hasattr(stock_model, 'model_stock_no') and stock_model.model_stock_no:
                        model_cases = [stock_model.model_stock_no.model_no]
                    else:
                        model_cases = []
            if isinstance(model_cases, str):
                try:
                    _parsed = json.loads(model_cases)
                    jig_detail.no_of_model_cases = _parsed if isinstance(_parsed, list) else ([str(_parsed)] if _parsed else [])
                except:
                    # Raw format written by Jig_Loading.JigSaveAPI for multi-model jigs:
                    # 'MODEL(lot_id):qty | MODEL2(lot_id):qty2'. Legacy single-model/no-lot
                    # format: '1805NAK02:14,1805NAR02:13'. Split on ' | ' when present so
                    # multi-model jigs aren't collapsed into one entry (no comma exists in
                    # the pipe format, so a plain comma-split would treat the whole string
                    # as a single garbled model). No dedup: two entries sharing the same
                    # Plating Stk No (ditto models) must stay as two separate entries so
                    # Model Presents renders one circle per entry.
                    _mc_parts = model_cases.split(' | ') if ' | ' in model_cases else model_cases.split(',')
                    _parsed_items = [
                        _cleaned
                        for _cleaned in (
                            re.split(r'[\(\[]', _part, maxsplit=1)[0].split(':')[0].strip()
                            for _part in _mc_parts
                        )
                        if _cleaned
                    ]
                    jig_detail.no_of_model_cases = _parsed_items if _parsed_items else []
            elif isinstance(model_cases, list):
                jig_detail.no_of_model_cases = model_cases
            else:
                jig_detail.no_of_model_cases = []

            # After parsing, if still empty (e.g. DB stored '[]'), fall back to plating_stock_num
            if not jig_detail.no_of_model_cases:
                _psn2 = getattr(jig_detail, 'plating_stock_num', None) or draft_data.get('plating_stock_num')
                if _psn2:
                    jig_detail.no_of_model_cases = [str(_psn2).strip()]

            # Set lot_plating_stk_nos for template (model widget + data-plating-stk-no attr)
            if not getattr(jig_detail, 'lot_plating_stk_nos', None):
                if jig_detail.no_of_model_cases:
                    jig_detail.lot_plating_stk_nos = ', '.join(str(x) for x in jig_detail.no_of_model_cases)
                else:
                    _fallback_psn = getattr(jig_detail, 'plating_stock_num', None) or draft_data.get('plating_stock_num', '')
                    jig_detail.lot_plating_stk_nos = str(_fallback_psn).strip() if _fallback_psn else ''

            # Re-populate model_images and model_colors if first loop skipped them
            # (first loop skips single-model jigs where no_of_model_cases DB value is '')
            if jig_detail.no_of_model_cases and not getattr(jig_detail, 'model_images', None):
                _model_imgs = {}
                _model_clrs = {}
                for _mn in jig_detail.no_of_model_cases:
                    _mk = str(_mn)
                    _model_clrs[_mk] = global_model_colors.get(_mk, '#cccccc')
                    # Image priority: exact plating_stk_no match → batch_id path
                    _img_data = model_images_map.get(_mk, {'images': [], 'first_image': None})
                    if not _img_data.get('images'):
                        _batch_img = batch_images_map.get(getattr(jig_detail, 'batch_id', None))
                        if _batch_img:
                            _img_data = _batch_img
                    _model_imgs[_mk] = _img_data
                jig_detail.model_images = _model_imgs
                jig_detail.model_colors = _model_clrs
                # Populate model_data (versions/tray info) with numeric prefix fallback
                _first_mk = str(jig_detail.no_of_model_cases[0])
                _md = model_data_map.get(_first_mk)
                if not _md:
                    _nmp = re.match(r'^(\d+)', _first_mk)
                    if _nmp:
                        _md = model_data_map.get(_nmp.group(1))
                if _md:
                    if not getattr(jig_detail, 'unique_versions', None):
                        jig_detail.unique_versions = [_md['version']]
                    if not getattr(jig_detail, 'unique_vendors', None):
                        jig_detail.unique_vendors = [_md['vendor']]
                    if not getattr(jig_detail, 'unique_locations', None):
                        jig_detail.unique_locations = [_md['location']]
                    if not getattr(jig_detail, 'unique_tray_types', None):
                        jig_detail.unique_tray_types = [_md['tray_type']]
                    if not getattr(jig_detail, 'unique_plating_stk_nos', None):
                        jig_detail.unique_plating_stk_nos = [_md['plating_stk_no']]
                    if not getattr(jig_detail, 'unique_polishing_stk_nos', None):
                        jig_detail.unique_polishing_stk_nos = [_md['polishing_stk_no']]
            
            # Ensure other list fields are lists
            for field in ['all_versions', 'all_vendors', 'all_locations', 'all_plating_stk_nos', 'all_polishing_stk_nos', 'all_plating_colors']:
                if not hasattr(jig_detail, field) or getattr(jig_detail, field) is None:
                    setattr(jig_detail, field, [])
            
            # If we don't have polishing stock numbers, try to fetch them
            if not jig_detail.all_polishing_stk_nos and jig_detail.no_of_model_cases:
                polishing_stk_nos = []
                plating_stk_nos = []
                for model_no in jig_detail.no_of_model_cases:
                    lot_data = self.get_lot_specific_data(jig_detail.lot_id, model_no)
                    if lot_data:
                        if lot_data.get('polishing_stk_no') and lot_data['polishing_stk_no'] != 'No Polishing Stock No':
                            polishing_stk_nos.append(lot_data['polishing_stk_no'])
                        if lot_data.get('plating_stk_no') and lot_data['plating_stk_no'] != 'No Plating Stock No':
                            plating_stk_nos.append(lot_data['plating_stk_no'])
                jig_detail.all_polishing_stk_nos = polishing_stk_nos
                if not jig_detail.all_plating_stk_nos:
                    jig_detail.all_plating_stk_nos = plating_stk_nos
        
        # Add pagination with consistent logic as Inprocess Inspection
        page_number = self.request.GET.get('page', 1)
        paginator = Paginator(jig_unload, 10)  # 10 items per page like Inprocess Inspection
        page_obj = paginator.get_page(page_number)
        
        print(f"\n📄 Jig Unloading Main - Pagination: Page {page_number}, Total items: {len(jig_unload)}")
        print(f"📄 Current page items: {len(page_obj.object_list)}")
        print(f"📄 Total pages: {paginator.num_pages}")
        
        context['jig_unload'] = page_obj  # For table data
        context['jig_details'] = page_obj  # For pagination controls (template uses this name)
        context['bath_numbers'] = bath_numbers
        
        return context
    
    def _get_z1_jig_lot_quantities(self, jig):
        """Return all model lot_ids represented by a Zone 1 jig row."""
        lot_quantities = {}

        def add_lot(lot_id, qty=1):
            lot_id = str(lot_id or '').strip()
            if not lot_id:
                return
            try:
                qty = int(qty or 1)
            except (TypeError, ValueError):
                qty = 1
            if lot_id not in lot_quantities:
                lot_quantities[lot_id] = qty

        draft_data = getattr(jig, 'draft_data', {}) or {}
        if isinstance(draft_data, dict):
            for lot_id, qty in (draft_data.get('lot_id_quantities', {}) or {}).items():
                add_lot(lot_id, qty)

            draft_allocation = draft_data.get('multi_model_allocation', []) or []
            if isinstance(draft_allocation, str):
                try:
                    draft_allocation = json.loads(draft_allocation)
                except Exception:
                    draft_allocation = []
            if isinstance(draft_allocation, list):
                for allocation in draft_allocation:
                    if isinstance(allocation, dict):
                        add_lot(
                            allocation.get('lot_id'),
                            allocation.get('allocated_qty') or allocation.get('qty') or allocation.get('quantity') or 1,
                        )

        model_allocation = getattr(jig, 'multi_model_allocation', None) or []
        if isinstance(model_allocation, str):
            try:
                model_allocation = json.loads(model_allocation)
            except Exception:
                model_allocation = []
        if isinstance(model_allocation, list):
            for allocation in model_allocation:
                if isinstance(allocation, dict):
                    add_lot(
                        allocation.get('lot_id'),
                        allocation.get('allocated_qty') or allocation.get('qty') or allocation.get('quantity') or 1,
                    )

        raw_model_cases = getattr(jig, 'no_of_model_cases', None)
        if raw_model_cases:
            if isinstance(raw_model_cases, str):
                try:
                    parsed_model_cases = json.loads(raw_model_cases)
                except Exception:
                    parsed_model_cases = raw_model_cases
            else:
                parsed_model_cases = raw_model_cases

            case_items = parsed_model_cases if isinstance(parsed_model_cases, (list, tuple)) else [parsed_model_cases]
            for case_item in case_items:
                for lot_id in re.findall(r'\b(?:[A-Z]*LID|UNLOT)[A-Za-z0-9]+\b', str(case_item)):
                    add_lot(lot_id)

        if not lot_quantities and getattr(jig, 'lot_id', None):
            add_lot(jig.lot_id, getattr(jig, 'updated_lot_qty', 1))

        return lot_quantities

    def filter_fully_unloaded_jigs(self, queryset):
        """Smart filter to remove jigs where ALL lot_ids are unloaded"""
        
        def parse_combined_lot_id(combined_id):
            try:
                return combined_id.rsplit('-', 1) if combined_id and '-' in combined_id else (None, None)
            except:
                return None, None

        # ✅ FAST PATH: lots already flagged as fully unloaded via last_process_module.
        # Do not key this by jig_id because Jig IDs are reused across cycles.
        completed_lot_ids = set(
            JigCompleted.objects.filter(last_process_module='Jig Unloading')
            .values_list('lot_id', flat=True)
        )
        print(f"[FILTER] Fast-path lot_ids with last_process_module='Jig Unloading': {len(completed_lot_ids)}")

        # Get all unload records once
        unload_records = JigUnloadAfterTable.objects.filter(
            combine_lot_ids__isnull=False
        ).exclude(combine_lot_ids__exact=[]).values('combine_lot_ids')

        
        # Build unload mapping: jig_lot_id -> set of unloaded lot_ids
        # Also collect bare lot_ids (stored when jig_lot_id was empty: "-LIDxxx" or plain "LIDxxx")
        unload_map = {}
        bare_unloaded_lot_ids = set()  # lot_ids with no valid jig_id prefix
        for record in unload_records:
            if record['combine_lot_ids']:
                for combined_id in record['combine_lot_ids']:
                    parts = parse_combined_lot_id(combined_id)
                    if len(parts) == 2 and parts[0] and parts[1]:
                        jig_id, lot_id = parts[0], parts[1]
                        unload_map.setdefault(jig_id, set()).add(lot_id)
                    elif len(parts) == 2 and parts[1]:
                        # empty prefix (e.g. "-LIDxxx") — store bare lot_id
                        bare_unloaded_lot_ids.add(parts[1])
                    elif combined_id and '-' not in combined_id:
                        # plain lot_id (no prefix at all)
                        bare_unloaded_lot_ids.add(combined_id)
        
        # Also check direct unloads
        direct_unloads = set(JigUnload_TrayId.objects.values_list('lot_id', flat=True))
        bare_unloaded_lot_ids |= direct_unloads

        # ✅ SECONDARY MULTI-MODEL LOT FILTER:
        # When Jig Loading creates a multi-model jig, each additional lot gets its own
        # JigCompleted record. These secondary records appear in the unloading pick table
        # as separate rows with N/A model (empty plating_color). They should be hidden
        # because they are already represented inside the primary multi-model row.
        # Logic: any lot_id that appears in another JigCompleted's multi_model_allocation
        # but is NOT that record's own primary lot_id is a secondary/absorbed lot.
        secondary_lot_ids = set()
        for _mm_rec in JigCompleted.objects.filter(
            is_multi_model=True,
            multi_model_allocation__isnull=False
        ).only('lot_id', 'multi_model_allocation'):
            if _mm_rec.multi_model_allocation:
                for _alloc in _mm_rec.multi_model_allocation:
                    if isinstance(_alloc, dict):
                        _alloc_lot = _alloc.get('lot_id', '')
                        if _alloc_lot and _alloc_lot != _mm_rec.lot_id:
                            secondary_lot_ids.add(_alloc_lot)
        print(f"[FILTER] Secondary multi-model lot_ids to hide: {len(secondary_lot_ids)}")

        # Filter jigs
        filtered_jigs = []
        for jig in queryset:
            # ✅ SECONDARY LOT CHECK: hide lots that are secondary models in a
            # multi-model Jig Loading submission (already shown inside primary row).
            if jig.lot_id in secondary_lot_ids:
                print(f"🚫 [SECONDARY LOT FILTER] Hiding secondary multi-model lot: {jig.lot_id}")
                continue

            _jfq = self._get_z1_jig_lot_quantities(jig)
            if not _jfq:
                # Fallback to base lot_id if lot_id_quantities isn't present in draft_data
                if getattr(jig, 'lot_id', None):
                    _jfq = {jig.lot_id: getattr(jig, 'updated_lot_qty', 1)}
                else:
                    # Truly no lot data — keep the jig visible
                    filtered_jigs.append(jig)
                    continue

            jig_lot_ids = set(_jfq.keys())
            _jig_id = getattr(jig, 'jig_id', None) or jig.lot_id

            # ✅ FAST PATH: hide only by completed lot state, not by jig_id alone.
            # Jig IDs are reusable across cycles, so an older completed cycle for the
            # same physical jig must not hide a new Inprocess Inspection row.
            if jig.lot_id in completed_lot_ids:
                print(f"🚫 [FAST PATH] Hiding completed lot: {jig.lot_id}")
                continue

            # Also hide if ALL lot_ids in lot_id_quantities are marked as unloaded
            if jig_lot_ids and jig_lot_ids.issubset(completed_lot_ids):
                print(f"🚫 [FAST PATH] Hiding jig - all lot_ids unloaded: {jig.lot_id}")
                continue

            # Fallback: scan combine_lot_ids unload map (handles older records without unload_over flag)
            unloaded_lot_ids = (
                unload_map.get(_jig_id, set())
                | (jig_lot_ids & bare_unloaded_lot_ids)
            )
            
            # Keep jig if ANY lot_id is NOT unloaded
            if not jig_lot_ids.issubset(unloaded_lot_ids):
                filtered_jigs.append(jig)
            else:
                print(f"🚫 [MAP PATH] Hiding fully unloaded jig: {jig.lot_id}")
        
        return filtered_jigs

    def check_draft_status_for_jigs(self, jig_queryset):
        """Check if any jig has draft records prioritizing draft_data['lot_id_quantities'].

        This function will inspect each jig's `draft_data` JSON field first and look
        for a `lot_id_quantities` dict. If present, keys of that dict are treated as
        draft lot ids. Falls back to JigUnloadDraft.main_lot_id values only when
        `lot_id_quantities` is not available.
        """

        # Convert QuerySet to list explicitly
        jig_list = list(jig_queryset)

        # Cache all saved draft main_lot_ids for fallback checks
        saved_draft_main_lot_ids = set(JigUnloadDraft.objects.values_list('main_lot_id', flat=True))
        print(f"🔍 Saved draft main_lot_ids (fallback): {saved_draft_main_lot_ids}")

        for jig_detail in jig_list:
            has_draft = False

            print(f"🔍 Zone 2 - Checking jig {getattr(jig_detail, 'jig_id', jig_detail.lot_id)}")

            # Primary: Inspect draft_data JSON on the jig itself (prefer this)
            draft_data = getattr(jig_detail, 'draft_data', None) or {}
            lot_quantities = {}
            if isinstance(draft_data, dict):
                lot_quantities = draft_data.get('lot_id_quantities', {}) or {}

            # Debug print keys when present
            if lot_quantities:
                print(f"lot_id_quantities keys: {list(lot_quantities.keys())}")
                # If the jig's main lot_id is present in lot_id_quantities, mark draft
                main_lot = getattr(jig_detail, 'lot_id', None)
                if main_lot and main_lot in lot_quantities:
                    has_draft = True
                    print(f"✅ Zone 2 - JIG {getattr(jig_detail, 'jig_id', jig_detail.lot_id)} DRAFT FOUND")

            # Secondary: check new_lot_ids array field if present and not already matched
            if not has_draft and hasattr(jig_detail, 'new_lot_ids') and jig_detail.new_lot_ids:
                print(f"   - new_lot_ids: {jig_detail.new_lot_ids}")
                for lid in jig_detail.new_lot_ids:
                    if lid in lot_quantities or lid in saved_draft_main_lot_ids:
                        has_draft = True
                        print(f"✅ DRAFT MATCH in new_lot_ids: {lid}")
                        break

            # Tertiary fallback: if lot_id_quantities absent, check saved drafts table
            if not has_draft and not lot_quantities:
                main_lot = getattr(jig_detail, 'lot_id', None)
                print(f"   - main lot_id fallback: {main_lot}")
                if main_lot and main_lot in saved_draft_main_lot_ids:
                    has_draft = True
                    print(f"✅ DRAFT MATCH in saved drafts for main lot_id: {main_lot}")

            # Set BOTH flags to ensure template compatibility
            jig_detail.has_unload_draft = has_draft
            jig_detail.jig_unload_draft = has_draft

            if not has_draft and not lot_quantities:
                print(f"❌ Zone 2 - JIG {getattr(jig_detail, 'jig_id', jig_detail.lot_id)} NO DRAFT")

        print(f"🔍 Draft check complete for {len(jig_list)} jigs")
        return jig_list  # Return the list, not queryset


class JigUnloading_Completedtable(LoginRequiredMixin, TemplateView):
    template_name = 'Jig_Unloading/JigUnloading_Completedtable.html'
    login_url = 'login'

    def get_dynamic_tray_capacity(self, tray_type_name):
        """
        Get tray capacity based on tray type name.
        Rules (per workflow spec, sourced from TrayType master DB):
        - Normal (or NR/NB/ND/NL): 20
        - Jumbo  (or JR/JB/JD):    12 (matches TrayType master data)
        - Others: DB lookup fallback
        """
        try:
            # Workflow-spec capacity — covers both type names and tray code prefixes
            _tn = (tray_type_name or '').upper()
            if _tn in ('NORMAL', 'NR', 'NB', 'ND', 'NL', 'NW'):
                return 20
            elif _tn in ('JUMBO', 'JR', 'JB', 'JD'):
                return 12

            # Fallback: try custom capacity override table
            custom_capacity = InprocessInspectionTrayCapacity.objects.filter(
                tray_type__tray_type=tray_type_name,
                is_active=True
            ).first()

            if custom_capacity:
                return custom_capacity.custom_capacity

            # Fallback to TrayType table
            tray_type = TrayType.objects.filter(tray_type=tray_type_name).first()
            if tray_type:
                return tray_type.tray_capacity

            # Default fallback
            return 0

        except Exception as e:
            print(f"⚠️ Error getting dynamic tray capacity: {e}")
            return 0

    def get_stock_model_data(self, lot_id):
        """
        Helper function to get stock model data from either TotalStockModel or RecoveryStockModel
        Returns: (stock_model, is_recovery, batch_model_class)
        """
        # Try TotalStockModel first
        tsm = TotalStockModel.objects.filter(lot_id=lot_id).first()
        if tsm:
            return tsm, False, ModelMasterCreation
        
        # Try RecoveryStockModel if not found in TotalStockModel
        try:
            rsm = RecoveryStockModel.objects.filter(lot_id=lot_id).first()
            if rsm:
                try:
                    from Recovery_DP.models import RecoveryMasterCreation
                    return rsm, True, RecoveryMasterCreation
                except ImportError:
                    print("⚠️ RecoveryMasterCreation not found, using ModelMasterCreation as fallback")
                    return rsm, True, ModelMasterCreation
        except Exception as e:
            print(f"⚠️ Error accessing RecoveryStockModel: {e}")
        
        return None, False, None

    def _resolve_bath_number_for_completed(self, unload, jig_qr_id):
        """Fetch bath number dynamically from JigCompleted for completed table entries."""
        try:
            # 1. Try via jig_qr_id (jig_id on JigCompleted)
            if jig_qr_id:
                jc = JigCompleted.objects.filter(
                    jig_id=jig_qr_id, bath_numbers__isnull=False
                ).values('bath_numbers__bath_number').first()
                if jc:
                    return jc['bath_numbers__bath_number']

            # 2. Try via combine_lot_ids
            if unload.combine_lot_ids:
                for cid in unload.combine_lot_ids:
                    # Extract plain lot_id
                    lid = cid.lstrip('-')
                    if lid.startswith('JLOT-') and '-' in lid[5:]:
                        lid = lid.rsplit('-', 1)[1]
                    jc = JigCompleted.objects.filter(
                        draft_data__lot_id_quantities__has_key=lid, bath_numbers__isnull=False
                    ).values('bath_numbers__bath_number').first()
                    if jc:
                        return jc['bath_numbers__bath_number']
                    # Also try direct lot_id match
                    jc = JigCompleted.objects.filter(
                        lot_id=lid, bath_numbers__isnull=False
                    ).values('bath_numbers__bath_number').first()
                    if jc:
                        return jc['bath_numbers__bath_number']

            # 3. Try via draft_data keys from JigCompleted
            if jig_qr_id:
                jc = JigCompleted.objects.filter(jig_id=jig_qr_id).values('draft_data').first()
                if jc and jc.get('draft_data'):
                    dd = jc['draft_data'] if isinstance(jc['draft_data'], dict) else {}
                    bn = dd.get('bath_number') or dd.get('nickel_bath_number') or dd.get('bath_no') or dd.get('nickel_bath_type')
                    if bn:
                        return str(bn)
        except Exception as e:
            print(f"[BATH FIX] Error resolving bath number: {e}")

        return 'N/A'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        
        # 🔥 NEW: Add date filtering logic (same as Inprocess Inspection)
        from django.utils import timezone
        import pytz
        from datetime import datetime, timedelta
        
        # Use IST timezone
        tz = pytz.timezone("Asia/Kolkata")
        now_local = timezone.now().astimezone(tz)
        today = now_local.date()
        yesterday = today - timedelta(days=1)

        # Get date filter parameters from request
        from_date_str = self.request.GET.get('from_date')
        to_date_str = self.request.GET.get('to_date')

        # Calculate date range
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

        print(f"[DEBUG] Zone 1 Completed - Date filter: {from_date} to {to_date}")

        # DEBUG: Check total records in JigUnloadAfterTable
        total_unload_records = JigUnloadAfterTable.objects.all().count()
        print(f"[DEBUG] Total JigUnloadAfterTable records: {total_unload_records}")

        # DEBUG: Check allowed color IDs for Zone 1 (Only IPS)
        allowed_color_ids = Plating_Color.objects.filter(
            plating_color='IPS'
        ).values_list('id', flat=True)
        print(f"[DEBUG] Zone 1 Completed - Allowed color IDs (IPS only): {list(allowed_color_ids)}")

        # Filter completed_unloads by Un_loaded_date_time with date filtering
        # ✅ FIX: Also include records where plating_color FK is NULL (happens when populate_jig_unload_fields
        # fails to resolve the FK) so we can post-filter them by resolving color from combine_lot_ids.
        from django.db.models import Q
        completed_unloads_qs = JigUnloadAfterTable.objects.filter(
            Q(plating_color_id__in=allowed_color_ids) | Q(plating_color__isnull=True),
            Un_loaded_date_time__date__gte=from_date,
            Un_loaded_date_time__date__lte=to_date
        ).select_related(
            'plating_color', 'polish_finish', 'version'
        ).prefetch_related('location').order_by('-Un_loaded_date_time')

        # Post-filter: for records with plating_color=None, verify they belong to IPS
        # by tracing combine_lot_ids → TotalStockModel → plating_color
        allowed_colors_set = set(Plating_Color.objects.filter(
            plating_color='IPS'
        ).values_list('plating_color', flat=True))  # {'IPS'}

        def _is_ips_record(rec):
            """Return True if the record's plating color is IPS (traced from combine_lot_ids if FK is null)."""
            if rec.plating_color and rec.plating_color.plating_color in allowed_colors_set:
                return True
            # FK is null — try to resolve from combine_lot_ids
            if rec.combine_lot_ids:
                for _cid in rec.combine_lot_ids:
                    _lid = _extract_lot_id_local(_cid)  # use local helper defined below
                    _tsm = TotalStockModel.objects.filter(lot_id=_lid).select_related('plating_color').first()
                    if _tsm and _tsm.plating_color and _tsm.plating_color.plating_color in allowed_colors_set:
                        # Backfill the FK so future reads are fast
                        try:
                            rec.plating_color = _tsm.plating_color
                            rec.save(update_fields=['plating_color'])
                            print(f"[COMPLETED TABLE FIX] ✅ Backfilled plating_color for record {rec.id} from lot {_lid}")
                        except Exception as _bfe:
                            print(f"[COMPLETED TABLE FIX] ⚠️ Could not backfill plating_color: {_bfe}")
                        return True
                    # Also try JigCompleted→batch_id→ModelMasterCreation path
                    _jc = JigCompleted.objects.filter(
                        draft_data__lot_id_quantities__has_key=_lid
                    ).first()
                    if _jc and _jc.batch_id:
                        _mmc = ModelMasterCreation.objects.filter(
                            batch_id=_jc.batch_id
                        ).values('plating_color').first()
                        if _mmc and _mmc.get('plating_color') in allowed_colors_set:
                            try:
                                _pc_obj = Plating_Color.objects.filter(
                                    plating_color=_mmc['plating_color']
                                ).first()
                                if _pc_obj:
                                    rec.plating_color = _pc_obj
                                    rec.save(update_fields=['plating_color'])
                            except Exception:
                                pass
                            return True
            return False

        # _extract_lot_id is defined later in this method — define a local copy here
        def _extract_lot_id_local(combined):
            if not combined:
                return combined
            s = combined.lstrip('-')
            if s.startswith('JLOT-') and '-' in s[5:]:
                return s.rsplit('-', 1)[1]
            return s

        completed_unloads = [rec for rec in completed_unloads_qs if _is_ips_record(rec)]
        
        print(f"[DEBUG] Filtered completed_unloads count: {len(completed_unloads)}")
        
        # Debug individual records
        for record in completed_unloads:
            print(f"[DEBUG] Record {record.id}: plating_color={record.plating_color}, plating_color_id={record.plating_color_id}")
        
        # Also check all records to see what plating colors they have
        all_records = JigUnloadAfterTable.objects.all()
        print(f"[DEBUG] All records plating colors:")
        for record in all_records:
            color_name = record.plating_color.plating_color if record.plating_color else "None"
            print(f"[DEBUG]   Record {record.id}: plating_color={color_name} (ID: {record.plating_color_id})")

        # ✅ ENHANCED: Get all model numbers from combine_lot_ids for bulk processing
        all_model_numbers = set()
        all_lot_ids = set()

        def _extract_lot_id(combined):
            """Normalise combine_lot_ids entry to a plain lot_id.
            Handles: 'JLOT-xxx-LIDyyy' → 'LIDyyy', '-LIDyyy' → 'LIDyyy', 'LIDyyy' → 'LIDyyy'."""
            if not combined:
                return combined
            s = str(combined).strip().lstrip('-')
            if ':' in s:
                possible_lot = s.rsplit(':', 1)[-1].strip()
                if possible_lot:
                    return possible_lot
            if s.startswith('JLOT-') and '-' in s[5:]:
                return s.rsplit('-', 1)[1]
            return s

        def _normalize_completed_model_tokens(raw_value):
            if raw_value in (None, ''):
                return []
            if isinstance(raw_value, dict):
                raw_value = (
                    raw_value.get('plating_stk_no')
                    or raw_value.get('model_name')
                    or raw_value.get('model')
                    or raw_value.get('model_no')
                )
            if isinstance(raw_value, (list, tuple, set)):
                normalized = []
                for item in raw_value:
                    normalized.extend(_normalize_completed_model_tokens(item))
                return normalized

            normalized = []
            for item in re.split(r'[,|]', str(raw_value)):
                model_no = re.sub(r'\[[^\]]*\]', '', item).strip()
                if ':' in model_no:
                    model_no = model_no.split(':', 1)[0].strip()
                model_no = model_no.strip(' -')
                if not model_no or model_no.upper() in {'N/A', 'NONE', 'NULL'}:
                    continue
                if re.fullmatch(r'(?:JLOT-)?(?:[A-Z]*LID|UNLOT)[A-Za-z0-9-]+', model_no):
                    continue
                normalized.append(model_no)
            return normalized

        def _get_full_plating_stock_for_lot(lot_id):
            actual_lot_id = _extract_lot_id(lot_id)
            if not actual_lot_id:
                return ''

            stock_model, is_recovery, batch_model_class = self.get_stock_model_data(actual_lot_id)
            if stock_model and getattr(stock_model, 'batch_id', None) and batch_model_class:
                master_creation = batch_model_class.objects.filter(
                    id=stock_model.batch_id.id
                ).select_related('model_stock_no').first()
                if master_creation:
                    plating_stk_no = getattr(master_creation, 'plating_stk_no', None)
                    if plating_stk_no:
                        return str(plating_stk_no).strip()
                    model_stock = getattr(master_creation, 'model_stock_no', None)
                    if model_stock:
                        return str(getattr(model_stock, 'plating_stk_no', None) or model_stock.model_no).strip()

            source_jig = JigCompleted.objects.filter(
                Q(lot_id=actual_lot_id) | Q(draft_data__lot_id_quantities__has_key=actual_lot_id)
            ).order_by('-id').first()
            if source_jig:
                allocation = getattr(source_jig, 'multi_model_allocation', None) or []
                if isinstance(allocation, str):
                    try:
                        allocation = json.loads(allocation)
                    except Exception:
                        allocation = []
                if isinstance(allocation, list):
                    for model_info in allocation:
                        if not isinstance(model_info, dict):
                            continue
                        model_lot_id = (
                            model_info.get('lot_id')
                            or model_info.get('source_lot_id')
                            or model_info.get('original_lot_id')
                        )
                        if model_lot_id and _extract_lot_id(model_lot_id) != actual_lot_id:
                            continue
                        for token in _normalize_completed_model_tokens(model_info):
                            return token

                if source_jig.batch_id:
                    master_creation = ModelMasterCreation.objects.filter(
                        batch_id=source_jig.batch_id
                    ).select_related('model_stock_no').first()
                    if master_creation:
                        plating_stk_no = getattr(master_creation, 'plating_stk_no', None)
                        if plating_stk_no:
                            return str(plating_stk_no).strip()
                        model_stock = getattr(master_creation, 'model_stock_no', None)
                        if model_stock:
                            return str(getattr(model_stock, 'plating_stk_no', None) or model_stock.model_no).strip()

                for token in _normalize_completed_model_tokens(getattr(source_jig, 'plating_stock_num', None)):
                    return token

            if stock_model and getattr(stock_model, 'model_stock_no', None):
                model_stock = stock_model.model_stock_no
                return str(getattr(model_stock, 'plating_stk_no', None) or model_stock.model_no).strip()
            return ''

        for unload in completed_unloads:
            saved_plating_numbers = _normalize_completed_model_tokens(getattr(unload, 'plating_stk_no_list', []) or [])
            if saved_plating_numbers:
                all_model_numbers.update(saved_plating_numbers)
            elif unload.plating_stk_no:
                all_model_numbers.update(_normalize_completed_model_tokens(unload.plating_stk_no))

            if unload.combine_lot_ids:
                all_lot_ids.update(_extract_lot_id(lot_id) for lot_id in unload.combine_lot_ids if lot_id)
                # Get model numbers from lot_ids
                for _raw_cid in unload.combine_lot_ids:
                    _actual_lid = _extract_lot_id(_raw_cid)
                    full_plating_stock_no = _get_full_plating_stock_for_lot(_actual_lid)
                    if full_plating_stock_no:
                        all_model_numbers.update(_normalize_completed_model_tokens(full_plating_stock_no))
                        continue
                    stock_model, is_recovery, batch_model_class = self.get_stock_model_data(_actual_lid)
                    if stock_model and hasattr(stock_model, 'model_stock_no') and stock_model.model_stock_no:
                        all_model_numbers.add(
                            getattr(stock_model.model_stock_no, 'plating_stk_no', None)
                            or stock_model.model_stock_no.model_no
                        )

        print(f"[DEBUG] Found {len(all_model_numbers)} unique model numbers: {all_model_numbers}")
        print(f"[DEBUG] Found {len(all_lot_ids)} unique lot IDs")

        # ✅ Type of Input (Fresh/Recovery): bulk-resolve via TotalStockModel.lot_id → batch_id.upload_type
        # for every lot_id referenced by combine_lot_ids across all completed unload records.
        type_of_input_map = get_type_of_input_map(list(all_lot_ids))

        # ✅ DB-persisted, Plating Stk No-unique colors (see modelmasterapp.color_service)
        # so Model Presents circles match Jig_Unloading_MainTable / Inprocess Inspection.
        sorted_model_numbers = sorted(str(m) for m in all_model_numbers)
        global_model_colors = get_model_colors_by_model_no(sorted_model_numbers)

        # ✅ ENHANCED: Comprehensive model images fetching (same as MainTable)
        model_images_map = {}
        if all_model_numbers:
            print(f"[DEBUG] Fetching images for {len(all_model_numbers)} models")
            clean_model_numbers = set()
            for model_no in all_model_numbers:
                match = re.match(r'^(\d+)', str(model_no))
                clean_model_numbers.add(match.group(1) if match else str(model_no))
            
            # Fetch images for each model using ModelMaster
            model_masters = ModelMaster.objects.filter(
                Q(model_no__in=clean_model_numbers) | Q(plating_stk_no__in=all_model_numbers)
            ).prefetch_related('images')
            
            from modelmasterapp.image_utils import sort_images_front_first
            for model_master in model_masters:
                images = sort_images_front_first(model_master.images.all())
                image_urls = []
                first_image = "/static/assets/images/imagePlaceholder.jpg"
                
                for img in images:
                    if img.master_image:
                        try:
                            img_url = img.master_image.url if hasattr(img.master_image, 'url') else str(img.master_image)
                            image_urls.append(img_url)
                            if first_image == "/static/assets/images/imagePlaceholder.jpg":
                                first_image = img_url
                        except Exception as img_err:
                            print(f"[DEBUG] Error processing image {img.id}: {img_err}")
                            continue
                
                image_payload = {
                    'images': image_urls,
                    'first_image': first_image
                }
                if model_master.model_no and model_master.model_no not in model_images_map:
                    model_images_map[model_master.model_no] = image_payload
                if model_master.plating_stk_no:
                    model_images_map[model_master.plating_stk_no] = image_payload
                print(f"[DEBUG] Model {model_master.model_no}: {len(image_urls)} images, first: {first_image}")

        # Nickel Wiping persists its first tray-scan activity in its own table,
        # while draft/hold/submit activity is stored on the unload row. Build
        # the lookup once so the completed table reflects entry into Nickel
        # Wiping without waiting for that module to complete.
        from Nickel_Inspection.models import (
            NickelQcTrayId,
            Nickel_QC_AutoSave,
            Nickel_QC_Draft_Store,
            Nickel_QC_TopTray_Draft_Store,
            NickelQC_Submission,
        )

        completed_unload_lot_ids = [
            unload.lot_id for unload in completed_unloads if unload.lot_id
        ]
        nickel_wiping_lot_ids = set(
            NickelQcTrayId.objects.filter(
                lot_id__in=completed_unload_lot_ids
            ).values_list('lot_id', flat=True)
        )
        for activity_model in (
            Nickel_QC_AutoSave,
            Nickel_QC_Draft_Store,
            Nickel_QC_TopTray_Draft_Store,
            NickelQC_Submission,
        ):
            nickel_wiping_lot_ids.update(
                activity_model.objects.filter(
                    lot_id__in=completed_unload_lot_ids
                ).values_list('lot_id', flat=True)
            )
        nickel_activity_fields = (
            'nq_draft',
            'nq_onhold_picking',
            'nq_hold_lot',
            'nq_release_lot',
            'nq_qc_accptance',
            'nq_qc_rejection',
            'nq_qc_few_cases_accptance',
            'nq_accepted_tray_scan_status',
            'nq_rejection_tray_scan_status',
        )
        nickel_wiping_lot_ids.update(
            unload.lot_id
            for unload in completed_unloads
            if any(getattr(unload, field, False) for field in nickel_activity_fields)
        )

        # ✅ ENHANCED: Process each unload record with comprehensive data
        table_data = []
        for idx, unload in enumerate(completed_unloads):
            print(f"\n[DEBUG] ===== PROCESSING RECORD {idx + 1} =====")
            print(f"[DEBUG] Unload lot_id: {unload.lot_id}")
            print(f"[DEBUG] combine_lot_ids: {unload.combine_lot_ids}")
            
            # Get jig_qr_id — use DB field, then parse from combine_lot_ids, then JigCompleted lookup
            jig_qr_id = unload.jig_qr_id or ''
            unloading_remarks = None
            if not jig_qr_id and unload.combine_lot_ids:
                for _cid_z1 in unload.combine_lot_ids:
                    # combine_lot_ids format: "JLOT-xxx-LIDyyy" → rsplit gives ("JLOT-xxx", "LIDyyy")
                    if _cid_z1 and '-' in _cid_z1:
                        _parts_z1 = _cid_z1.rsplit('-', 1)
                        _parsed_jig_id = _parts_z1[0] if len(_parts_z1) == 2 else None
                        _parsed_lot_id = _parts_z1[1] if len(_parts_z1) == 2 else _cid_z1
                        if _parsed_jig_id and _parsed_jig_id.startswith('JLOT-'):
                            jig_qr_id = _parsed_jig_id
                            # Also fetch remarks via parsed lot_id
                            _jc_z1 = JigCompleted.objects.filter(
                                draft_data__lot_id_quantities__has_key=_parsed_lot_id
                            ).first()
                            if _jc_z1:
                                unloading_remarks = getattr(_jc_z1, 'unloading_remarks', None)
                            break
                        elif _parsed_lot_id:
                            # Broken format '-LIDyyy': no JLOT prefix — look up JigCompleted via extracted lot_id
                            _actual_lot_z1 = _extract_lot_id(_cid_z1)
                            _jc_z1_fb = JigCompleted.objects.filter(
                                draft_data__lot_id_quantities__has_key=_actual_lot_z1
                            ).first()
                            if _jc_z1_fb:
                                jig_qr_id = getattr(_jc_z1_fb, 'jig_id', None) or ''
                                unloading_remarks = getattr(_jc_z1_fb, 'unloading_remarks', None)
                            break

            # ✅ FINAL FALLBACK: plain lot IDs (no dash) — query JigCompleted directly
            if not jig_qr_id and unload.combine_lot_ids:
                for _cid_plain in unload.combine_lot_ids:
                    _lid_plain = _extract_lot_id(_cid_plain) if _cid_plain else None
                    if not _lid_plain:
                        continue
                    # Try lot_id_quantities key match first
                    _jc_plain = JigCompleted.objects.filter(
                        draft_data__lot_id_quantities__has_key=_lid_plain
                    ).first()
                    if not _jc_plain:
                        # Try direct lot_id match on JigCompleted
                        _jc_plain = JigCompleted.objects.filter(lot_id=_lid_plain).first()
                    if _jc_plain:
                        _jid = getattr(_jc_plain, 'jig_id', None)
                        if _jid:
                            jig_qr_id = _jid
                            unloading_remarks = getattr(_jc_plain, 'unloading_remarks', None)
                            print(f"[ZONE1 FIX] ✅ jig_qr_id resolved from JigCompleted: {jig_qr_id}")
                            break
                        # Even if jig_id is empty, store remarks
                        if not unloading_remarks:
                            unloading_remarks = getattr(_jc_plain, 'unloading_remarks', None)

            # Also backfill jig_qr_id to DB so future renders skip the lookup
            if jig_qr_id and not unload.jig_qr_id:
                try:
                    unload.jig_qr_id = jig_qr_id
                    unload.save(update_fields=['jig_qr_id'])
                    print(f"[ZONE1 FIX] ✅ Backfilled jig_qr_id={jig_qr_id} for record {unload.id}")
                except Exception as _bjid:
                    print(f"[ZONE1 FIX] ⚠️ Backfill jig_qr_id failed: {_bjid}")

            if jig_qr_id and not unloading_remarks:
                _jc_z1_rem = JigCompleted.objects.filter(jig_id=jig_qr_id).first()
                if _jc_z1_rem:
                    unloading_remarks = getattr(_jc_z1_rem, 'unloading_remarks', None)

            # Handle location (many-to-many) with fallback from TotalStockModel
            try:
                locations = unload.location.all()
                location_names = [loc.location_name for loc in locations]
                location_display = ", ".join(location_names) if location_names else "N/A"
            except Exception as e:
                print(f"[DEBUG] Error processing location: {e}")
                location_display = "N/A"
                location_names = []
            # Fallback: look up location from TotalStockModel via combine_lot_ids
            if not location_names and unload.combine_lot_ids:
                for _cid_loc1 in unload.combine_lot_ids:
                    _actual_loc1 = _extract_lot_id(_cid_loc1)
                    _tsm_loc1 = TotalStockModel.objects.filter(lot_id=_actual_loc1).prefetch_related('location').first()
                    if _tsm_loc1 and _tsm_loc1.location.exists():
                        location_names = [loc.location_name for loc in _tsm_loc1.location.all()]
                        location_display = ", ".join(location_names)
                        break
                    elif _tsm_loc1 and _tsm_loc1.batch_id and getattr(_tsm_loc1.batch_id, 'location', None):
                        location_names = [_tsm_loc1.batch_id.location.location_name]
                        location_display = location_names[0]
                        break

            # ✅ NEW FALLBACK: JigCompleted → batch_id → ModelMasterCreation.location
            # This handles Jig Loading lots where TotalStockModel has no location set.
            if not location_names and unload.combine_lot_ids:
                for _cid_loc2 in unload.combine_lot_ids:
                    _actual_loc2 = _extract_lot_id(_cid_loc2)
                    # Try JigCompleted keyed by lot_id_quantities
                    _jc_loc = JigCompleted.objects.filter(
                        draft_data__lot_id_quantities__has_key=_actual_loc2
                    ).first()
                    if not _jc_loc:
                        # Try direct lot_id match on JigCompleted
                        _jc_loc = JigCompleted.objects.filter(lot_id=_actual_loc2).first()
                    if _jc_loc and _jc_loc.batch_id:
                        _mmc_loc = ModelMasterCreation.objects.filter(
                            batch_id=_jc_loc.batch_id
                        ).select_related('location').first()
                        if _mmc_loc and _mmc_loc.location:
                            location_names = [_mmc_loc.location.location_name]
                            location_display = location_names[0]
                            print(f"[COMPLETED TABLE] ✅ Location resolved via JigCompleted→MMC: {location_display}")
                            break
                    # Also try: jig_qr_id → JigCompleted → batch_id → MMC
                    if jig_qr_id:
                        _jc_jid = JigCompleted.objects.filter(jig_id=jig_qr_id).select_related().first()
                        if _jc_jid and _jc_jid.batch_id:
                            _mmc_jid = ModelMasterCreation.objects.filter(
                                batch_id=_jc_jid.batch_id
                            ).select_related('location').first()
                            if _mmc_jid and _mmc_jid.location:
                                location_names = [_mmc_jid.location.location_name]
                                location_display = location_names[0]
                                print(f"[COMPLETED TABLE] ✅ Location resolved via jig_id→JigCompleted→MMC: {location_display}")
                                break
                
            # 🔧 ENHANCED: Calculate tray info using dynamic capacity method
            tray_type_display = unload.tray_type or "N/A"
            
            # Use get_dynamic_tray_capacity method for proper Normal tray handling (16 -> 20)
            if tray_type_display != "N/A":
                dynamic_tray_capacity = self.get_dynamic_tray_capacity(tray_type_display)
                tray_capacity = dynamic_tray_capacity if dynamic_tray_capacity > 0 else (unload.tray_capacity if unload.tray_capacity else 1)
                print(f"[DEBUG] Zone 1 - Dynamic tray capacity for '{tray_type_display}': {dynamic_tray_capacity} (was: {unload.tray_capacity})")
            else:
                tray_capacity = unload.tray_capacity if unload.tray_capacity else 1
                print(f"[DEBUG] Zone 1 - Using raw tray capacity: {tray_capacity}")
            
            total_case_qty = unload.total_case_qty if unload.total_case_qty else 0
            no_of_trays = math.ceil(total_case_qty / tray_capacity) if tray_capacity > 0 else 0

            # ✅ SIMPLIFIED: Use saved list fields from database instead of processing individual lot_ids
            print(f"[DEBUG] ===== USING SAVED LIST FIELDS FROM DATABASE =====")
            
            # ✅ GET SAVED LISTS from database fields
            saved_plating_list = getattr(unload, 'plating_stk_no_list', []) or []
            saved_polish_list = getattr(unload, 'polish_stk_no_list', []) or []
            saved_version_list = getattr(unload, 'version_list', []) or []
            
            print(f"[DEBUG] saved_plating_stk_no_list: {saved_plating_list} (type: {type(saved_plating_list)})")
            print(f"[DEBUG] saved_polish_stk_no_list: {saved_polish_list} (type: {type(saved_polish_list)})")
            print(f"[DEBUG] saved_version_list: {saved_version_list} (type: {type(saved_version_list)})")
            
            # ✅ FIX ERR2/ERR3: Use plating_stk_no_list for display (NOT polish which has X-mask)
            # USE REAL PLATING VALUES FOR DISPLAY, not the X-masked polishing values
            all_plating_stk_nos = _jul_ordered_unique(_normalize_completed_model_tokens(saved_plating_list)) if saved_plating_list else []
            all_polish_stk_nos = list(all_plating_stk_nos) if all_plating_stk_nos else []  # ← CRITICAL FIX: Use plating, not polish
            all_versions = saved_version_list if saved_version_list else []
            
            # ✅ ALSO PROVIDE UNIQUE VERSIONS for backward compatibility
            unique_plating_stk_nos = list(set(all_plating_stk_nos)) if all_plating_stk_nos else []
            unique_polish_stk_nos = list(set(all_plating_stk_nos)) if all_plating_stk_nos else []  # ← CRITICAL FIX: Use plating for unique too
            unique_versions = list(set(all_versions)) if all_versions else []
            
            # ✅ FALLBACK: Use single field values if no saved lists (for backward compatibility)
            if not all_plating_stk_nos and unload.plating_stk_no:
                all_plating_stk_nos = _jul_ordered_unique(_normalize_completed_model_tokens(unload.plating_stk_no))
                unique_plating_stk_nos = list(all_plating_stk_nos)
                print(f"[DEBUG] Fallback: Using single plating_stk_no: {unload.plating_stk_no}")
            
            if not all_polish_stk_nos and unload.plating_stk_no:  # ← FIX: Use plating not polish in fallback too
                all_polish_stk_nos = _jul_ordered_unique(_normalize_completed_model_tokens(unload.plating_stk_no))
                unique_polish_stk_nos = list(all_polish_stk_nos)
                print(f"[DEBUG] Fallback: Using single plating_stk_no (not polishing): {unload.plating_stk_no}")
            
            if not all_versions and unload.version:
                version_display = getattr(unload.version, 'version_internal', str(unload.version)) if unload.version else "N/A"
                all_versions = [version_display]
                unique_versions = [version_display]
                print(f"[DEBUG] Fallback: Using single version: {version_display}")

            # ✅ BASIC MODEL DATA PROCESSING (simplified - no complex lot_id processing needed)
            model_images = {}
            model_colors = {}
            no_of_model_cases = []
            lot_id_quantities = {}
            
            if unload.combine_lot_ids:
                for lot_id in unload.combine_lot_ids:
                    lot_id = _extract_lot_id(lot_id)
                    try:
                        stock_model, is_recovery, batch_model_class = self.get_stock_model_data(lot_id)
                        full_plating_stock_no = _get_full_plating_stock_for_lot(lot_id)
                        if stock_model and hasattr(stock_model, 'model_stock_no') and stock_model.model_stock_no:
                            model_no = full_plating_stock_no or getattr(stock_model.model_stock_no, 'plating_stk_no', None) or stock_model.model_stock_no.model_no
                            model_no = _normalize_completed_model_tokens(model_no)[0] if _normalize_completed_model_tokens(model_no) else ''
                            if not model_no:
                                continue
                            no_of_model_cases.append(model_no)
                            
                            # Get lot quantity
                            if hasattr(stock_model, 'total_stock'):
                                lot_id_quantities[lot_id] = stock_model.total_stock
                            elif hasattr(stock_model, 'stock_qty'):
                                lot_id_quantities[lot_id] = stock_model.stock_qty
                            else:
                                lot_id_quantities[lot_id] = 0
                            
                            # Use global color mapping
                            model_colors[model_no] = global_model_colors.get(model_no, "#cccccc")
                            
                            # Get model images from the pre-built map
                            if model_no in model_images_map:
                                model_images[model_no] = model_images_map[model_no]
                            else:
                                model_images[model_no] = {
                                    'images': [],
                                    'first_image': "/static/assets/images/imagePlaceholder.jpg"
                                }
                    except Exception as e:
                        print(f"[DEBUG] Error processing lot_id {lot_id}: {e}")
                        continue

            source_lot_id_quantities = dict(lot_id_quantities)
            display_lot_id_quantities = {unload.lot_id: total_case_qty} if total_case_qty else source_lot_id_quantities

            # Remove duplicates while preserving order
            source_jigs_by_id = {}

            def _remember_source_jig(source_jig):
                if source_jig and getattr(source_jig, 'id', None) not in source_jigs_by_id:
                    source_jigs_by_id[source_jig.id] = source_jig

            def _ensure_completed_model(model_no):
                for normalized_model_no in _normalize_completed_model_tokens(model_no):
                    no_of_model_cases.append(normalized_model_no)
                    if normalized_model_no not in global_model_colors:
                        global_model_colors.update(get_model_colors_by_model_no([normalized_model_no]))
                    model_colors[normalized_model_no] = global_model_colors.get(normalized_model_no, '#cccccc')

                    if normalized_model_no not in model_images:
                        image_payload = model_images_map.get(normalized_model_no)
                        if not image_payload:
                            clean_model_no = normalized_model_no
                            match = re.match(r'^(\d+)', normalized_model_no)
                            if match:
                                clean_model_no = match.group(1)
                            image_urls = []
                            model_master = ModelMaster.objects.filter(
                                Q(model_no=clean_model_no) | Q(plating_stk_no=normalized_model_no)
                            ).prefetch_related('images').first()
                            if model_master:
                                from modelmasterapp.image_utils import sort_images_front_first
                                for image in sort_images_front_first(model_master.images.all()):
                                    if image.master_image:
                                        image_urls.append(image.master_image.url)
                            image_payload = {
                                'images': image_urls,
                                'first_image': image_urls[0] if image_urls else '/static/assets/images/imagePlaceholder.jpg'
                            }
                            model_images_map[normalized_model_no] = image_payload
                        model_images[normalized_model_no] = image_payload

            for saved_model_no in all_plating_stk_nos:
                _ensure_completed_model(saved_model_no)

            if not no_of_model_cases and unload.combine_lot_ids:
                for combined_lot in unload.combine_lot_ids:
                    actual_lot_id = _extract_lot_id(combined_lot)
                    _remember_source_jig(
                        JigCompleted.objects.filter(
                            draft_data__lot_id_quantities__has_key=actual_lot_id
                        ).first()
                    )
                    _remember_source_jig(JigCompleted.objects.filter(lot_id=actual_lot_id).first())

            if not no_of_model_cases and not source_jigs_by_id and jig_qr_id:
                _remember_source_jig(
                    JigCompleted.objects.filter(jig_id=jig_qr_id).order_by('-id').first()
                )

            if not no_of_model_cases:
                for source_jig in source_jigs_by_id.values():
                    allocation = getattr(source_jig, 'multi_model_allocation', None) or []
                    if isinstance(allocation, str):
                        try:
                            allocation = json.loads(allocation)
                        except Exception:
                            allocation = []
                    if isinstance(allocation, list):
                        for model_info in allocation:
                            if isinstance(model_info, dict):
                                _ensure_completed_model(model_info)

                    raw_model_cases = getattr(source_jig, 'no_of_model_cases', None)
                    if raw_model_cases:
                        _ensure_completed_model(raw_model_cases)

                    _ensure_completed_model(getattr(source_jig, 'plating_stock_num', None))

            no_of_model_cases = list(dict.fromkeys(no_of_model_cases))
            
            print(f"[DEBUG] ===== FINAL VALUES SUMMARY =====")
            print(f"[DEBUG] ALL plating_stk_nos ({len(all_plating_stk_nos)}): {all_plating_stk_nos}")
            print(f"[DEBUG] ALL polish_stk_nos ({len(all_polish_stk_nos)}): {all_polish_stk_nos}")
            print(f"[DEBUG] ALL versions ({len(all_versions)}): {all_versions}")
            print(f"[DEBUG] UNIQUE plating_stk_nos ({len(unique_plating_stk_nos)}): {unique_plating_stk_nos}")
            print(f"[DEBUG] UNIQUE polish_stk_nos ({len(unique_polish_stk_nos)}): {unique_polish_stk_nos}")
            print(f"[DEBUG] UNIQUE versions ({len(unique_versions)}): {unique_versions}")

            # Extract foreign key display values
            plating_color_display = "N/A"
            if unload.plating_color:
                plating_color_display = getattr(unload.plating_color, 'plating_color', str(unload.plating_color))
            
            polish_finish_display = "N/A"
            if unload.polish_finish:
                polish_finish_display = getattr(unload.polish_finish, 'polish_finish', str(unload.polish_finish))
            
            # Use the first version from all_versions or fallback
            version_display = all_versions[0] if all_versions else "N/A"

            # ✅ Type of Input (Fresh/Recovery): resolve from bulk map using combine_lot_ids,
            # falling back to the record's own lot_id.
            row_type_of_input = 'Fresh'
            if unload.combine_lot_ids:
                for _cid_toi in unload.combine_lot_ids:
                    _lid_toi = _extract_lot_id(_cid_toi)
                    if _lid_toi in type_of_input_map:
                        row_type_of_input = type_of_input_map[_lid_toi]
                        break
            if row_type_of_input == 'Fresh' and unload.lot_id in type_of_input_map:
                row_type_of_input = type_of_input_map[unload.lot_id]

            # ✅ Shared stock is the source of truth for completed-row status.
            # Build a wide candidate lot-id list (raw id, id without leading '-',
            # extracted id, id after ':', id after '-') so this table can find the
            # downstream stage even when Nickel Wiping/Nickel Inspection stores the
            # lot under a slightly different id format.
            _completed_lot_ids = set()

            def _remember_completed_lot_id(raw_lot_id):
                _raw_text = str(raw_lot_id or '').strip()
                if not _raw_text:
                    return
                _completed_lot_ids.add(_raw_text)
                _completed_lot_ids.add(_raw_text.lstrip('-'))
                _extracted = _extract_lot_id(_raw_text)
                if _extracted:
                    _completed_lot_ids.add(str(_extracted).strip())
                if ':' in _raw_text:
                    _completed_lot_ids.add(_raw_text.rsplit(':', 1)[-1].strip())
                if '-' in _raw_text:
                    _completed_lot_ids.add(_raw_text.rsplit('-', 1)[-1].strip())

            _remember_completed_lot_id(unload.lot_id)
            _remember_completed_lot_id(getattr(unload, 'unload_lot_id', None))
            for _raw_lot_id in (unload.combine_lot_ids or []):
                _remember_completed_lot_id(_raw_lot_id)
            _completed_lot_ids = {lot_id for lot_id in _completed_lot_ids if lot_id}

            _live_completed_stage = (
                TotalStockModel.objects.filter(lot_id__in=list(_completed_lot_ids))
                .exclude(current_stage__isnull=True)
                .exclude(current_stage__exact='')
                .values_list('current_stage', flat=True).first()
            )
            _object_stage = str(unload.current_stage or '').strip()
            _object_stage_key = _object_stage.lower().replace('_', ' ')
            _live_completed_stage_key = str(_live_completed_stage or '').strip().lower().replace('_', ' ')
            _completed_stage = (
                'Nickel Wiping'
                if unload.lot_id in nickel_wiping_lot_ids
                else (
                    _live_completed_stage if _live_completed_stage and not _live_completed_stage_key.startswith('jig unloading')
                    else (_object_stage if _object_stage and not _object_stage_key.startswith('jig unloading')
                          else (_live_completed_stage or _object_stage or unload.last_process_module or 'Jig Unloading'))
                )
            )
            _completed_stage_key = str(_completed_stage).strip().lower().replace('_', ' ')
            _completed_lot_status = (
                'Yet to Release' if _completed_stage_key.startswith('jig unloading')
                else 'Released'
            )

            # ✅ ENHANCED: Create comprehensive table data entry using SAVED LIST FIELDS
            table_entry = {
                # Basic fields
                'id': unload.id,
                'lot_id': unload.lot_id,
                'jig_qr_id': jig_qr_id,
                'combine_lot_ids': unload.combine_lot_ids,
                'total_case_qty': unload.total_case_qty,
                'missing_qty': unload.missing_qty,
                
                # ✅ PRIMARY: Stock numbers (using first value from saved lists)
                'plating_stk_no': all_plating_stk_nos[0] if all_plating_stk_nos else (unload.plating_stk_no or "N/A"),
                'polish_stk_no': all_polish_stk_nos[0] if all_polish_stk_nos else (unload.polish_stk_no or "N/A"),
                
                # ✅ ALL VALUES INCLUDING DUPLICATES (from saved database fields)
                'all_plating_stk_nos': all_plating_stk_nos,
                'all_polishing_stk_nos': all_polish_stk_nos,  # Using 'polishing' to match template expectations
                'all_versions': all_versions,
                
                # ✅ UNIQUE VALUES ONLY (for backward compatibility)
                'unique_plating_stk_nos': unique_plating_stk_nos,
                'unique_polishing_stk_nos': unique_polish_stk_nos,  # Using 'polishing' to match original name
                'unique_versions': unique_versions,
                
                # Foreign key displays
                'plating_color': plating_color_display,
                'polish_finish': polish_finish_display,
                'polish_finish_name': polish_finish_display,
                'version': version_display,  # First version from ALL list or fallback
                
                # Location
                'location': location_display,
                'unique_locations': location_names if location_names else ["N/A"],
                
                # Tray information (using dynamic capacity)
                'tray_type': tray_type_display,
                'tray_capacity': tray_capacity,  # Using calculated dynamic capacity
                'jig_type': tray_type_display,  # Alias for template
                'jig_capacity': tray_capacity,  # Using calculated dynamic capacity
                'no_of_trays': no_of_trays,
                'calculated_no_of_trays': no_of_trays,
                # Prefer the live current_stage SSOT (modelmasterapp/stage_service.py) so
                # this stays in sync with downstream modules (e.g. Nickel Wiping/Inspection,
                # Spider Spindle) that only update current_stage and not last_process_module.
                'last_process_module': _completed_stage,
                'current_stage_display': _completed_stage,
                'lot_status': _completed_lot_status,
                
                # Dates
                'created_at': unload.created_at,
                'un_loaded_date_time': unload.Un_loaded_date_time,
                'Un_loaded_date_time': unload.Un_loaded_date_time,
                
                # Status fields
                'jig_unload_draft': False,  # Default for completed
                'electroplating_only': False,  # Default
                
                # ✅ ENHANCED: Model data with comprehensive fetching
                'no_of_model_cases': no_of_model_cases,
                'model_images': model_images,
                'model_colors': model_colors,
                'lot_id_quantities': display_lot_id_quantities,
                'source_lot_id_quantities': source_lot_id_quantities,
                'lot_id_model_map': {},  # Can be populated if needed
                
                # Remarks
                'unloading_remarks': unloading_remarks,
                
                # Bath numbers - fetch dynamically from JigCompleted
                'bath_numbers': {'bath_number': self._resolve_bath_number_for_completed(unload, jig_qr_id)},

                # Type of Input (Fresh/Recovery)
                'type_of_input': row_type_of_input,
            }
            
            print(f"[DEBUG] ✅ Created table entry for {unload.lot_id}")
            table_data.append(table_entry)

        print(f"\n[DEBUG] ===== FINAL SUMMARY =====")
        print(f"[DEBUG] Total table_data entries: {len(table_data)}")
        
        # Add pagination with consistent logic as Inprocess Inspection
        page_number = self.request.GET.get('page', 1)
        paginator = Paginator(table_data, 10)  # 10 items per page like Inprocess Inspection
        page_obj = paginator.get_page(page_number)
        
        print(f"\n📄 Jig Unloading Completed - Pagination: Page {page_number}, Total items: {len(table_data)}")
        print(f"📄 Current page items: {len(page_obj.object_list)}")
        print(f"📄 Total pages: {paginator.num_pages}")
        
        # ✅ CONTEXT: Use consistent context variable name with pagination
        context['page_obj'] = page_obj  # For pagination controls
        context['completed_unloads'] = page_obj  # For table data
        context['debug'] = True  # Enable debug section in template (remove in production)
        
        # 🔥 NEW: Add date filter context variables
        context['from_date'] = from_date
        context['to_date'] = to_date
        
        return context


# =============================================================================
# ZONE 1 UNLOADING APIs — All names end with _z1
# Principle: "Frontend displays. Backend decides."
# =============================================================================

@method_decorator(csrf_exempt, name='dispatch')
class GetUnloadModelsZ1View(APIView):
    """
    GET /api/get_unload_models_z1/?jig_completed_id=<id>
    Returns computed model list with tray info for unload modal.
    """

    def _resolve_lot_id_to_model_z1(self, lot_id):
        """Resolve lot_id → model plating_stk_no via TotalStock/Recovery path"""
        tsm = TotalStockModel.objects.filter(lot_id=lot_id).select_related('batch_id__model_stock_no').first()
        if tsm and tsm.batch_id:
            mmc = ModelMasterCreation.objects.filter(id=tsm.batch_id.id).select_related('model_stock_no__tray_type').first()
            if mmc:
                return mmc
        # Recovery fallback
        rsm = RecoveryStockModel.objects.filter(lot_id=lot_id).select_related('batch_id').first()
        if rsm and rsm.batch_id:
            try:
                from Recovery_DP.models import RecoveryMasterCreation
                rmc = RecoveryMasterCreation.objects.filter(id=rsm.batch_id.id).select_related('model_stock_no__tray_type').first()
                if rmc:
                    return rmc
            except ImportError:
                pass
        return None

    def _get_tray_info_z1(self, model_master):
        """Get tray_type, tray_capacity, tray_code, tray_color from ModelMaster.
        Capacity is determined by Jig Unloading spec (Normal=20, Jumbo=12),
        matching the TrayType master DB values.
        ModelMaster.tray_code is the tray-code SSOT.
        TrayType remains the tray category (Normal/Jumbo).
        """
        if not model_master:
            return '', 20, '', ''
        tray_type_obj = model_master.tray_type
        tray_code = (getattr(model_master, 'tray_code', '') or '').strip().upper()
        if tray_type_obj:
            tray_type = tray_type_obj.tray_type or ''
            if not tray_code:
                tray_code = tray_type
            tray_color = tray_type_obj.tray_color or ''     # e.g. Red, Blue, D.Green, L.Green
            # Jig Unloading spec: Normal tray codes = 20, Jumbo = 12 (matches TrayType master DB)
            _tc = tray_code.upper()
            if _tc in ('NORMAL', 'NR', 'NB', 'ND', 'NL', 'NW'):
                tray_capacity = 20
            elif _tc in ('JUMBO', 'JR', 'JB', 'JD', 'JL'):
                tray_capacity = 12
            else:
                tray_capacity = tray_type_obj.tray_capacity or 20
            return tray_type, tray_capacity, tray_code, tray_color
        # Fallback: ModelMaster.tray_capacity (direct field)
        tray_capacity = model_master.tray_capacity or 20
        return '', tray_capacity, '', ''

    def get(self, request):
        jig_completed_id = request.GET.get('jig_completed_id')
        if not jig_completed_id:
            return Response({'error': 'jig_completed_id is required'}, status=400)

        try:
            jc = JigCompleted.objects.get(id=jig_completed_id)
        except JigCompleted.DoesNotExist:
            return Response({'error': 'JigCompleted not found'}, status=404)

        draft_data = jc.draft_data or {}
        jig_id = jc.jig_id or ''

        # ---------------------------------------------------------------
        # STEP 1: Build per-lot effective qty + tray list from draft_data.
        # Use tray_data[].delink_qty (effective qty after delinks) summed
        # per source_lot_id.  This is the ONLY authoritative source for
        # what is physically on the jig.
        # ---------------------------------------------------------------
        raw_tray_data = draft_data.get('tray_data', [])
        per_lot_trays = {}   # {lot_id: [{tray_id, qty, top_tray}]}
        per_lot_qty   = {}   # {lot_id: total_effective_qty}
        for entry in raw_tray_data:
            src = entry.get('source_lot_id', '')
            dq  = entry.get('delink_qty', 0)
            if not src or dq == 0:
                continue  # Fully-delinked tray — skip
            top = entry.get('top_tray', False)
            tid = entry.get('tray_id', '')
            per_lot_qty[src] = per_lot_qty.get(src, 0) + dq
            per_lot_trays.setdefault(src, []).append(
                {'tray_id': tid, 'qty': dq, 'top_tray': top}
            )

        # ---------------------------------------------------------------
        # STEP 2: Determine lot order and qty.
        # Priority: multi_model_allocation > per_lot_qty > legacy fallback
        # ---------------------------------------------------------------
        alloc = draft_data.get('multi_model_allocation', [])
        if alloc:
            lot_entries = []
            for a in alloc:
                lid = a['lot_id']
                # Prefer delink_qty sum; fall back to allocated_qty
                qty = per_lot_qty.get(lid, a.get('allocated_qty', 0))
                lot_entries.append((lid, a.get('model', ''), qty))
        elif per_lot_qty:
            lot_entries = [(lid, '', qty) for lid, qty in per_lot_qty.items()]
        else:
            # Legacy fallback — use loaded_cases_qty (never original_lot_qty)
            qty = jc.loaded_cases_qty or jc.updated_lot_qty or jc.original_lot_qty or 0
            lot_entries = [(jc.lot_id, '', qty)]

        # ---------------------------------------------------------------
        # STEP 3: Build model list
        # ---------------------------------------------------------------
        # Pre-resolve each lot's model_no so STEP 3b below knows which lots
        # will later be ditto-merged in STEP 3c (two+ lots sharing the same
        # plating_stk_no, physically present together on this one jig). This
        # must happen BEFORE the main loop, not per-iteration, since a lot
        # processed first doesn't yet know a later lot shares its model.
        _lot_model_no_map = {}
        for _lid, _hint, _q in lot_entries:
            if _q == 0:
                continue
            _mmc_peek = self._resolve_lot_id_to_model_z1(_lid)
            _lot_model_no_map[_lid] = (
                getattr(_mmc_peek, 'plating_stk_no', None) or ''
                if _mmc_peek else (_hint or draft_data.get('plating_stock_num', _lid))
            )
        _model_no_counts = {}
        for _mno in _lot_model_no_map.values():
            _model_no_counts[_mno] = _model_no_counts.get(_mno, 0) + 1
        # First lot_id seen for each model_no becomes that ditto-group's
        # "primary" — matching STEP 3c's own _by_model_no grouping below,
        # which likewise keeps the first-seen entry as the merge target.
        _primary_lot_for_model = {}
        for _lid, _hint, _q in lot_entries:
            if _q == 0:
                continue
            _mno = _lot_model_no_map.get(_lid)
            _primary_lot_for_model.setdefault(_mno, _lid)

        models_list = []
        for lot_id, model_hint, qty in lot_entries:
            if qty == 0:
                continue  # Nothing physically on jig for this lot

            mmc = self._resolve_lot_id_to_model_z1(lot_id)
            if mmc:
                plating_stk_no   = getattr(mmc, 'plating_stk_no', None) or ''
                polishing_stk_no = getattr(mmc, 'polishing_stk_no', None) or ''
                # Look up ModelMaster by plating_stk_no (not model_stock_no FK
                # which may point to the polishing model with wrong tray type).
                plating_model = ModelMaster.objects.filter(
                    plating_stk_no=plating_stk_no
                ).select_related('tray_type').first() if plating_stk_no else None
                model_stock = plating_model if plating_model else mmc.model_stock_no
                tray_type, tray_capacity, tray_code, tray_color = self._get_tray_info_z1(model_stock)
                images = []
                from modelmasterapp.image_utils import sort_images_front_first
                if model_stock:
                    images = [img.master_image.url for img in sort_images_front_first(model_stock.images.all()) if img.master_image]
                if not images:
                    images = [img.master_image.url for img in sort_images_front_first(mmc.images.all()) if img.master_image]
            else:
                plating_stk_no   = model_hint or draft_data.get('plating_stock_num', lot_id)
                polishing_stk_no = ''
                tray_type, tray_capacity, tray_code, tray_color = 'Normal', 20, 'N', ''
                images = []

            # ---------------------------------------------------------------
            # STEP 3b: If a submitted record exists (draft or final), prefer
            # its total_qty — this correctly reflects:
            #   - User-edited LOT Qty (if lot_qty_edited=True)
            #   - Add-Model merged qty
            # This ensures edited LOT Qty becomes the single source of truth.
            #
            # EXCEPTION: when this lot will be ditto-merged in STEP 3c below
            # (another lot on this same jig shares its model_no), only the
            # ditto-group's PRIMARY lot (first one seen) may take this
            # override — its total_qty is always saved as the group's full
            # combined total (native ditto amount, plus any Add-Model amount
            # merged in since). Applying the same override to the SECONDARY
            # lot(s) too double-counts them: STEP 3c would then add the
            # secondary's own native share on top of a primary total that
            # already includes it (144 -> 188 -> 232 -> ... on every reopen).
            # So secondary lots always fall back to their true per-lot
            # physical amount here, and STEP 3c (guarded by
            # `_qty_locked_by_override` below) skips re-adding it to a
            # primary whose total already accounts for it.
            # ---------------------------------------------------------------
            _model_no_for_lot = _lot_model_no_map.get(lot_id)
            _will_be_ditto_merged = _model_no_counts.get(_model_no_for_lot, 0) > 1
            _is_ditto_primary = _primary_lot_for_model.get(_model_no_for_lot) == lot_id
            _submitted_rec = JUSubmittedZ1.objects.filter(
                jig_completed_id=jig_completed_id, lot_id=lot_id
            ).order_by('-submitted_at').first()
            _qty_locked_by_override = False
            if _submitted_rec and _submitted_rec.total_qty is not None and (not _will_be_ditto_merged or _is_ditto_primary):
                # ✅ Use submitted LOT Qty - SSOT (whether draft or final)
                qty = _submitted_rec.total_qty
                _qty_locked_by_override = _will_be_ditto_merged

            # ---------------------------------------------------------------
            # STEP 4: Always recompute tray slots using DB capacity.
            #   - Top tray (partial / remainder qty) is ALWAYS slot 1.
            #   - Full trays follow after it.
            # ---------------------------------------------------------------
            cap = tray_capacity if tray_capacity > 0 else 20
            num_trays = math.ceil(qty / cap) if qty > 0 else 1
            remainder = qty % cap if qty % cap != 0 else cap  # top-tray qty
            tray_slots = []
            for i in range(num_trays):
                is_top = (i == 0)
                slot_qty = remainder if is_top else cap
                tray_slots.append({
                    'slot': i + 1,
                    'tray_id': '',
                    'qty': slot_qty,
                    'is_top_tray': is_top,
                    'editable_qty': is_top,
                    'is_newly_added_slot': False,
                })

            num_trays = len(tray_slots)

            # Check if already submitted
            # Check if already submitted (final, not draft)
            is_unloaded = JUSubmittedZ1.objects.filter(
                jig_completed_id=jig_completed_id, lot_id=lot_id, is_draft=False
            ).exists()

            # Load draft or final submitted data for pre-fill
            submitted_data = None
            sub = JUSubmittedZ1.objects.filter(
                jig_completed_id=jig_completed_id, lot_id=lot_id
            ).order_by('-submitted_at').first()
            # This lot's own record was created as a side-effect of an Add Model
            # merge into a DIFFERENT jig — it has no trays of its own to scan.
            is_already_loaded = bool(sub and sub.is_merged_additional)
            source_metadata = _jul_source_metadata_from_tray_data(sub.tray_data if sub else None)
            source_mappings = source_metadata.get('source_mappings', []) if isinstance(source_metadata, dict) else []
            restored_merged_lots = []
            display_jig_id = jig_id
            if source_mappings:
                source_jig_ids = []
                for source in source_mappings:
                    if not isinstance(source, dict):
                        continue
                    source_lot_id = str(source.get('lot_id') or '').strip()
                    source_jig_id = str(source.get('jig_id') or '').strip()
                    if source_jig_id:
                        source_jig_ids.append(source_jig_id)
                    if source_lot_id and source_lot_id != lot_id:
                        restored_merged_lots.append({
                            'jig_completed_id': source.get('jig_completed_id'),
                            'lot_id': source_lot_id,
                            'qty': int(source.get('qty') or 0),
                            'jig_id': source_jig_id,
                            # Reload from a draft loses the in-memory 'is_newly_added' flag
                            # STEP 5 sets; restore the value persisted at save time (see
                            # SaveModelUnloadZ1View source_mappings above) so the Remove
                            # control keeps showing only for lots actually merged in via the
                            # interactive Add Model action — NOT for a same-model ditto-merge
                            # that was already physically on the jig at load time. Hardcoding
                            # this True made every original/default sub-model show the Remove
                            # icon (and be removable, corrupting LOT Qty) after any draft save.
                            # Safe either way: RemoveMergedModelZ1View only ever deletes
                            # is_merged_additional=True rows, and the Remove control itself
                            # is hidden once the parent model is finally submitted (is_unloaded).
                            'is_newly_added': bool(source.get('is_newly_added', False)),
                        })
                if source_jig_ids:
                    display_jig_id = ', '.join(_jul_ordered_unique(source_jig_ids))
            if sub:
                submitted_data = {
                    'tray_data': sub.tray_data,
                    'missing_qty': sub.missing_qty,
                    'top_tray_remark': sub.top_tray_remark,
                    'is_draft': sub.is_draft,
                    'source_metadata': source_metadata,
                }

            # Determine draft status for this model
            has_draft = bool(submitted_data and submitted_data.get('is_draft'))

            # Carry previously-scanned tray IDs into the freshly-built tray_slots
            # (STEP 4 above always builds them blank) BEFORE any STEP 3c ditto-merge
            # rebuild runs. Without this, a model that gets automatically combined
            # with a same-model sibling lot on this jig (not via the interactive
            # Add Model flow) has its slot count grow, _jul_rebuild_tray_slots_z1
            # flags the extra slots as newly-added, and the frontend's reorder
            # guard then skips its usual submitted_data pre-fill entirely — so a
            # resumed draft would show every tray box empty despite already being
            # saved. Populating tray_id here means the data is already present on
            # the slot the moment it reaches the frontend, independent of that guard.
            if submitted_data and submitted_data.get('tray_data'):
                _saved_by_slot = {
                    t.get('slot'): t.get('tray_id')
                    for t in submitted_data['tray_data']
                    if isinstance(t, dict) and t.get('tray_id')
                }
                for _slot in tray_slots:
                    _saved_tid = _saved_by_slot.get(_slot['slot'])
                    if _saved_tid:
                        _slot['tray_id'] = _saved_tid

            # Plating color display name — try TotalStockModel → mmc → JigCompleted draft_data
            plating_color_name = ''
            _tsm = TotalStockModel.objects.filter(lot_id=lot_id).select_related('plating_color').first()
            if _tsm and _tsm.plating_color:
                plating_color_name = _tsm.plating_color.plating_color
            if not plating_color_name and mmc:
                plating_color_name = getattr(mmc, 'plating_color', '') or ''
            if not plating_color_name:
                _jc_pc = JigCompleted.objects.filter(
                    Q(lot_id=lot_id) | Q(draft_data__lot_id_quantities__has_key=lot_id)
                ).values('draft_data').first()
                if _jc_pc and isinstance(_jc_pc.get('draft_data'), dict):
                    plating_color_name = _jc_pc['draft_data'].get('plating_color', '') or ''

            # Check if already submitted as standalone lot (normalised: Zone 2
            # stores combine_lot_ids in a prefixed format, see
            # _jul_find_after_table_by_source_lot_id)
            is_submitted_lot = _jul_find_after_table_by_source_lot_id(lot_id) is not None

            models_list.append({
                'lot_id': lot_id,
                'model_no': plating_stk_no or lot_id,
                'polishing_stk_no': polishing_stk_no,
                'plating_color_name': plating_color_name,
                'qty': qty,
                'tray_type': tray_type,
                'tray_capacity': tray_capacity,
                'tray_code': tray_code,
                'tray_color': tray_color,
                'num_trays': num_trays,
                'tray_slots': tray_slots,
                'is_unloaded': is_unloaded,
                'is_draft': has_draft,
                'is_already_loaded': is_already_loaded,
                'is_submitted_lot': is_submitted_lot,
                'submitted_data': submitted_data,
                'images': images,
                'jig_id': display_jig_id,
                'merged_lots': restored_merged_lots,
                '_qty_locked_by_override': _qty_locked_by_override,
            })

        # ---------------------------------------------------------------
        # STEP 3c: Merge ditto entries within THIS jig — when the same
        # Plating Stk No shows up twice (e.g. two lots of "1805WBK02" loaded
        # together on one jig), the unload/tray-scan flow must treat them as
        # ONE model to scan with combined qty, not two separate rows.
        # (The pick table's Model Presents circles deliberately do NOT
        # dedupe ditto entries — this merge is scoped only to this scan-modal
        # model list, mirroring the existing STEP 5 "Add Model" merge below.)
        # ---------------------------------------------------------------
        if len(models_list) > 1:
            _merged_models_list = []
            _by_model_no = {}
            for m in models_list:
                _key = m.get('model_no') or None
                if _key and _key in _by_model_no:
                    primary = _by_model_no[_key]
                    if m['jig_id'] and m['jig_id'] not in primary['jig_id']:
                        primary['jig_id'] = primary['jig_id'] + ', ' + m['jig_id']
                    primary.setdefault('merged_lots', [])
                    primary['merged_lots'].append({
                        'jig_completed_id': int(jig_completed_id),
                        'lot_id': m['lot_id'],
                        'qty': m['qty'],
                        'jig_id': m['jig_id'],
                    })
                    primary['merged_lots'].extend(m.get('merged_lots') or [])
                    _info = f"+{m['qty']} from {m['lot_id']}"
                    primary['merged_info'] = (primary.get('merged_info', '') + '; ' + _info) if primary.get('merged_info') else _info
                    # If the primary's qty was restored from its own persisted
                    # total_qty (STEP 3b), that total already accounts for this
                    # sibling's native share (every save writes the full combined
                    # figure back). Adding m['qty'] again here would double-count
                    # it on every reopen. Only add it fresh when the primary's
                    # qty is still its raw, un-overridden per-lot amount.
                    if not primary.get('_qty_locked_by_override'):
                        primary['qty'] += m['qty']
                    # Recalculate the combined quantity from scratch so there is
                    # never more than one partial (top) tray. Tray IDs already
                    # scanned onto the existing slots are preserved in order.
                    _cap = primary['tray_capacity'] if primary['tray_capacity'] > 0 else 20
                    primary['tray_slots'] = _jul_rebuild_tray_slots_z1(
                        primary['qty'], _cap, primary['tray_slots']
                    )
                    primary['num_trays'] = len(primary['tray_slots'])
                    # Combined entry counts as unloaded only once every merged lot is unloaded.
                    primary['is_unloaded'] = primary['is_unloaded'] and m['is_unloaded']
                    primary['is_draft'] = primary['is_draft'] or m['is_draft']
                    # "Already Loaded" must hold for ALL merged lots, not ANY — mirroring
                    # the jig-level is_already_loaded_z1 rule. Using OR here let a single
                    # merged-in sibling lot (already consumed elsewhere) mask a primary lot
                    # that still has its own real draft/tray data, hiding the Resume action.
                    primary['is_already_loaded'] = primary.get('is_already_loaded', False) and m.get('is_already_loaded', False)
                else:
                    if _key:
                        _by_model_no[_key] = m
                    _merged_models_list.append(m)
            models_list = _merged_models_list

        is_multi_model = len(models_list) > 1

        # ---------------------------------------------------------------
        # STEP 5: Process additional jig IDs (Add Model merge).
        # If plating_stk_no matches an existing model → merge qty.
        # If different → add as separate row.
        # ---------------------------------------------------------------
        additional_ids = request.GET.getlist('additional_jig_ids')
        for add_id in additional_ids:
            try:
                add_jc = JigCompleted.objects.get(id=add_id)
            except JigCompleted.DoesNotExist:
                continue

            add_draft = add_jc.draft_data or {}
            add_jig_id = add_jc.jig_id or ''

            # Build lot entries for additional jig (same STEP 1+2 logic)
            add_raw_tray = add_draft.get('tray_data', [])
            add_plq = {}
            for e in add_raw_tray:
                src = e.get('source_lot_id', '')
                dq = e.get('delink_qty', 0)
                if src and dq > 0:
                    add_plq[src] = add_plq.get(src, 0) + dq

            add_alloc = add_draft.get('multi_model_allocation', [])
            if add_alloc:
                add_lots = [
                    (a['lot_id'], a.get('model', ''),
                     add_plq.get(a['lot_id'], a.get('allocated_qty', 0)))
                    for a in add_alloc
                ]
            elif add_plq:
                add_lots = [(lid, '', q) for lid, q in add_plq.items()]
            else:
                q = add_jc.loaded_cases_qty or add_jc.updated_lot_qty or add_jc.original_lot_qty or 0
                add_lots = [(add_jc.lot_id, '', q)]

            for a_lot_id, a_model_hint, a_qty in add_lots:
                if a_qty == 0:
                    continue

                a_mmc = self._resolve_lot_id_to_model_z1(a_lot_id)
                if a_mmc:
                    a_plating = getattr(a_mmc, 'plating_stk_no', None) or ''
                    a_polishing = getattr(a_mmc, 'polishing_stk_no', None) or ''
                else:
                    a_plating = a_model_hint or add_draft.get('plating_stock_num', a_lot_id)
                    a_polishing = ''

                # Try to merge with existing model by plating_stk_no
                merged = False
                for m in models_list:
                    if m['model_no'] == a_plating and a_plating:
                        # MERGE: same plating_stk_no — ADD qty and APPEND new tray slots.
                        # Existing slots (in particular the already-scanned top tray) keep
                        # their tray_id/qty exactly as-is — never rebuilt or rewritten,
                        # since a scanned tray's recorded qty reflects real physical
                        # contents, not a recomputed share of the new combined total.
                        # The newly-added qty gets its own fresh slots placed AHEAD of the
                        # existing ones (new lot on top, old lot pushed down); existing
                        # slots are renumbered and demoted from "top" accordingly.
                        # NOTE: m['jig_id'] itself is intentionally left untouched here —
                        # the newly-added jig is tracked only in merged_lots (below), so
                        # that if this merge is later undone via Remove, the base jig_id
                        # display reverts correctly without needing to be un-concatenated.
                        # The frontend renders the combined "JIG: X, Y" label by joining
                        # m.jig_id with merged_lots entries flagged is_newly_added.
                        info = f'+{a_qty} from {add_jig_id}'
                        m.setdefault('merged_info', '')
                        m['merged_info'] = (m['merged_info'] + '; ' + info) if m['merged_info'] else info
                        m.setdefault('merged_lots', [])
                        m['merged_lots'].append({
                            'jig_completed_id': int(add_id),
                            'lot_id': a_lot_id,
                            'qty': a_qty,
                            'jig_id': add_jig_id,
                            'is_newly_added': True,  # added in this Add Model action — safe to remove before save
                        })
                        m['qty'] += a_qty

                        # NOTE: tray_id for m['tray_slots'] is already carried forward from
                        # submitted_data earlier (see "Carry previously-scanned tray IDs into
                        # the freshly-built tray_slots" above, before STEP 3c). Re-applying
                        # submitted_data here by slot NUMBER is not just redundant — on the
                        # second (or later) additional jig merged in this same request, prior
                        # loop iterations have already renumbered m['tray_slots'] (new blank
                        # slots inserted ahead of the old ones), so matching stale
                        # submitted_data slot numbers here would stamp an already-scanned
                        # tray ID onto a genuinely new, blank slot from THIS iteration.

                        # Give the newly-added lot its own fresh, blank tray slots
                        # placed ahead of the existing ones (new lot on top, old lot
                        # pushed down). Tray IDs never mingle between the two lots —
                        # each lot's qty maps only to its own slots.
                        _cap = m['tray_capacity'] if m['tray_capacity'] > 0 else 20
                        m['tray_slots'] = _jul_prepend_new_lot_tray_slots_z1(a_qty, m['qty'], _cap, m['tray_slots'])
                        m['num_trays'] = len(m['tray_slots'])
                        # Keep is_draft and submitted_data — openTrayScan will pre-fill from them
                        merged = True
                        break

                if not merged:
                    # Different model — add as separate row
                    if a_mmc:
                        plating_model = ModelMaster.objects.filter(
                            plating_stk_no=a_plating
                        ).select_related('tray_type').first() if a_plating else None
                        a_model_stock = plating_model if plating_model else a_mmc.model_stock_no
                        a_tt, a_tc, a_tcode, a_tcolor = self._get_tray_info_z1(a_model_stock)
                        a_images = []
                        from modelmasterapp.image_utils import sort_images_front_first
                        if a_model_stock:
                            a_images = [img.master_image.url for img in sort_images_front_first(a_model_stock.images.all()) if img.master_image]
                        if not a_images:
                            a_images = [img.master_image.url for img in sort_images_front_first(a_mmc.images.all()) if img.master_image]
                    else:
                        a_tt, a_tc, a_tcode, a_tcolor = 'Normal', 20, 'N', ''
                        a_images = []

                    a_cap = a_tc if a_tc > 0 else 20
                    a_ntrays = math.ceil(a_qty / a_cap) if a_qty > 0 else 1
                    a_rem = a_qty % a_cap if a_qty % a_cap != 0 else a_cap
                    a_tslots = [
                        {'slot': i + 1, 'tray_id': '', 'qty': a_rem if i == 0 else a_cap,
                         'is_top_tray': i == 0, 'editable_qty': i == 0, 'is_newly_added_slot': True}
                        for i in range(a_ntrays)
                    ]

                    models_list.append({
                        'lot_id': a_lot_id,
                        'model_no': a_plating or a_lot_id,
                        'polishing_stk_no': a_polishing,
                        'qty': a_qty,
                        'tray_type': a_tt,
                        'tray_capacity': a_tc,
                        'tray_code': a_tcode,
                        'tray_color': a_tcolor,
                        'num_trays': len(a_tslots),
                        'tray_slots': a_tslots,
                        'is_unloaded': False,
                        'is_draft': False,
                        'is_already_loaded': False,
                        'submitted_data': None,
                        'images': a_images,
                        'jig_id': add_jig_id,
                        'added_jig_completed_id': int(add_id),
                    })

        # Sort: models newly appended via this Add Model request always float to the
        # top (regardless of status), so the user immediately sees what they just
        # added above the existing/already-scanned rows. Within each of those two
        # groups: Draft first (0), Pending (1), Done/Unloaded (2).
        def _z1_model_sort_key(m):
            is_newly_added = 0 if m.get('added_jig_completed_id') else 1
            status_rank = 2 if m['is_unloaded'] else (0 if m['is_draft'] else 1)
            return (is_newly_added, status_rank)
        models_list.sort(key=_z1_model_sort_key)

        is_multi_model = len(models_list) > 1
        all_unloaded = all(m['is_unloaded'] for m in models_list) if models_list else False

        return Response({
            'jig_completed_id': int(jig_completed_id),
            'jig_qr_id': jig_id,
            'models': models_list,
            'is_multi_model': is_multi_model,
            'all_unloaded': all_unloaded,
        })


@method_decorator(csrf_exempt, name='dispatch')
class RemoveMergedModelZ1View(APIView):
    """
    POST /api/remove_merged_model_z1/
    Undo an Add Model merge before it is finalised. Deletes the JUSubmittedZ1
    record created for the merged-in lot (jig_completed_id/lot_id) so that lot
    stops showing as "Already Loaded" and becomes available again as an Add
    Model candidate. Only ever deletes records explicitly flagged
    is_merged_additional=True, so this can never touch a lot's own primary
    unload record.
    """
    def post(self, request):
        removed_jig_completed_id = request.data.get('removed_jig_completed_id')
        removed_lot_id = request.data.get('removed_lot_id')
        if not removed_jig_completed_id or not removed_lot_id:
            return Response({'error': 'removed_jig_completed_id and removed_lot_id are required'}, status=400)

        deleted_count, _ = JUSubmittedZ1.objects.filter(
            jig_completed_id=removed_jig_completed_id,
            lot_id=removed_lot_id,
            is_merged_additional=True,
        ).delete()

        return Response({'success': True, 'removed': deleted_count})


@method_decorator(csrf_exempt, name='dispatch')
class SaveModelUnloadZ1View(APIView):
    """
    POST /api/save_model_unload_z1/
    Saves tray scan data for one model. Creates/updates JUSubmittedZ1 record.
    """
    def post(self, request):
        data = request.data
        jig_completed_id = data.get('jig_completed_id')
        lot_id = data.get('lot_id')
        model_no = data.get('model_no')
        tray_data = data.get('tray_data', [])
        total_qty = data.get('total_qty', 0)
        missing_qty = data.get('missing_qty', 0)
        top_tray_remark = data.get('top_tray_remark', '')
        tray_type = data.get('tray_type', '')
        tray_capacity = data.get('tray_capacity', 0)
        tray_code = data.get('tray_code', '')
        tray_color = data.get('tray_color', '')
        lot_qty_edited = data.get('lot_qty_edited', False)
        merged_lots = data.get('merged_lots', []) or []

        if not jig_completed_id or not lot_id or not model_no:
            return Response({'error': 'jig_completed_id, lot_id, and model_no are required'}, status=400)

        is_draft = data.get('is_draft', False)

        # ✅ CRITICAL: Validate LOT Qty matches tray distribution
        if not is_draft:
            # Validate total_qty is non-negative
            if total_qty < 0:
                return Response({'error': 'LOT Qty cannot be negative'}, status=400)
            
            # Validate sum of tray quantities equals total_qty
            tray_qty_sum = sum(t.get('qty', 0) for t in tray_data)
            if tray_qty_sum != total_qty:
                return Response({
                    'error': f'Total tray quantity ({tray_qty_sum}) does not match LOT Qty ({total_qty}). '
                            f'Please ensure tray distribution is correct.'
                }, status=400)

        allowed_lot_ids_for_trays = [lot_id] + [
            str(merged_lot.get('lot_id') or '').strip()
            for merged_lot in merged_lots
            if str(merged_lot.get('lot_id') or '').strip()
        ]
        seen_tray_ids = set()
        for tray in tray_data:
            tray_id = normalize_jig_unload_tray_id(tray.get('tray_id', ''))
            if tray_id:
                tray['tray_id'] = tray_id
            if not tray_id:
                if not is_draft:
                    return Response({'error': 'All tray slots must be scanned'}, status=400)
                continue
            if not is_valid_jig_unload_tray_id_format(tray_id):
                return Response({
                    'error': f'Tray ID "{tray_id}" has invalid format. Expected format: XX-A00001'
                }, status=400)
            if len(tray_id) > 9:
                return Response({'error': f'Tray ID "{tray_id}" exceeds 9 characters'}, status=400)
            if tray_id in seen_tray_ids:
                return Response({'error': f'Duplicate tray ID "{tray_id}" — each tray must be unique'}, status=400)
            seen_tray_ids.add(tray_id)
            # Tray code prefix validation: tray_id must start with the expected tray_code prefix
            if tray_code:
                expected_prefix = tray_code.upper() + '-'
                if not tray_id.upper().startswith(expected_prefix):
                    return Response({
                        'error': f'Tray ID "{tray_id}" does not match expected tray code "{tray_code}". '
                                 f'Expected prefix: {expected_prefix}'
                    }, status=400)

            tray_conflict = find_jig_unload_tray_conflict(
                tray_id,
                allowed_lot_ids=allowed_lot_ids_for_trays,
                include_tray_master=True,
            )
            if tray_conflict:
                return Response({
                    'error': tray_conflict['message'],
                    'validation_type': 'tray_occupied',
                    'linked_lot': tray_conflict.get('linked_lot', ''),
                    'source': tray_conflict.get('source', ''),
                }, status=400)

            nickel_conflict = validate_jig_unload_nickel_reject_tray(
                tray_id,
                current_lot_id=lot_id,
            )
            if nickel_conflict:
                return Response({
                    'error': nickel_conflict['message'],
                    'validation_type': 'tray_occupied',
                    'linked_lot': nickel_conflict.get('linked_lot', ''),
                    'source': nickel_conflict.get('source', ''),
                }, status=400)

            # Top tray ID is the primary identifier for a model's tray batch.
            # find_jig_unload_tray_conflict() above exempts records that belong to the
            # same lot family (allowed_lot_ids_for_trays), which is correct for cross-lot
            # occupancy but leaves a gap when an "Add Model" merge places several models
            # under the same jig_completed_id: a top tray already claimed by a sibling
            # model in that same lot family would silently pass. Enforce uniqueness of the
            # top tray explicitly across sibling models here.
            if tray.get('is_top_tray'):
                sibling_rows = JUSubmittedZ1.objects.filter(
                    jig_completed_id=jig_completed_id
                ).exclude(model_no=model_no).only('id', 'model_no', 'tray_data')
                for sibling in sibling_rows:
                    for s_tray in (sibling.tray_data or []):
                        if not s_tray.get('is_top_tray'):
                            continue
                        if normalize_jig_unload_tray_id(s_tray.get('tray_id', '')) == tray_id:
                            return Response({
                                'error': f'Top tray ID "{tray_id}" is already the primary/top tray for '
                                         f'model "{sibling.model_no}". Each model\'s top tray ID must be unique.'
                            }, status=400)

        # Validate completed tray IDs against tray master (only for final save)
        if not is_draft:
            for tray in tray_data:
                tray_id = tray.get('tray_id', '')

                # Tray occupancy validation: reject trays already assigned to another lot
                existing_tray = TrayId.objects.filter(tray_id=tray_id).first()
                if existing_tray:
                    # Already scanned and not delinked → occupied
                    if existing_tray.scanned and not existing_tray.delink_tray:
                        return Response({
                            'error': f'Tray "{tray_id}" is already scanned and occupied by lot {existing_tray.lot_id}. '
                                     f'Please use a free tray or delink this one first.'
                        }, status=400)
                    # Has a lot_id, not delinked, and belongs to a different lot → occupied
                    if existing_tray.lot_id and not existing_tray.delink_tray and existing_tray.lot_id != lot_id:
                        return Response({
                            'error': f'Tray "{tray_id}" is assigned to another lot ({existing_tray.lot_id}). '
                                     f'Please use a free tray or delink this one first.'
                        }, status=400)

        # Top tray remark validation (only if qty was edited, only on final save)
        if not is_draft:
            for tray in tray_data:
                if tray.get('is_top_tray') and tray.get('qty_edited') and not top_tray_remark.strip():
                    return Response({'error': 'Top tray remark is required when quantity is edited'}, status=400)

        try:
            jc = JigCompleted.objects.get(id=jig_completed_id)
        except JigCompleted.DoesNotExist:
            return Response({'error': 'JigCompleted not found'}, status=404)

        jig_qr_id = jc.jig_id or jc.lot_id

        merged_qty = 0
        for merged_lot in merged_lots:
            try:
                merged_qty += int(merged_lot.get('qty') or 0)
            except (TypeError, ValueError):
                continue
        try:
            primary_source_qty = max(int(total_qty or 0) - merged_qty, 0)
        except (TypeError, ValueError):
            primary_source_qty = 0

        source_mappings = [{
            'jig_completed_id': int(jig_completed_id),
            'lot_id': lot_id,
            'qty': primary_source_qty if merged_lots else int(total_qty or 0),
            'jig_id': jig_qr_id,
        }]
        for merged_lot in merged_lots:
            merged_lot_id = str(merged_lot.get('lot_id') or '').strip()
            if not merged_lot_id:
                continue
            source_mappings.append({
                'jig_completed_id': merged_lot.get('jig_completed_id'),
                'lot_id': merged_lot_id,
                'qty': int(merged_lot.get('qty') or 0),
                'jig_id': str(merged_lot.get('jig_id') or '').strip(),
                # Preserve whether this lot was merged in via the interactive Add
                # Model action (removable) vs. a same-model ditto-merge that was
                # already physically on the jig at load time (not removable) —
                # restoring this as an unconditional True on reload was making
                # every original/default sub-model show the Remove icon after
                # any draft save (see restore logic below).
                'is_newly_added': bool(merged_lot.get('is_newly_added', False)),
            })

        source_metadata = {
            'primary_lot_id': lot_id,
            'source_lot_ids': _jul_ordered_unique([source.get('lot_id') for source in source_mappings]),
            'source_jig_ids': _jul_ordered_unique([source.get('jig_id') for source in source_mappings]),
            'source_mappings': source_mappings,
        }
        tray_data_with_sources = _jul_enrich_tray_data_with_sources(tray_data, source_metadata)

        # ✅ CRITICAL: Use submitted total_qty as single source of truth
        # This is the edited LOT Qty from frontend - DO NOT recalculate from other sources
        # Create or update JUSubmittedZ1
        obj, created = JUSubmittedZ1.objects.update_or_create(
            jig_completed_id=jig_completed_id,
            lot_id=lot_id,
            defaults={
                'jig_qr_id': jig_qr_id,
                'model_no': model_no,
                'total_qty': total_qty,  # ✅ Use edited LOT Qty - SSOT
                'tray_type': tray_type,
                'tray_capacity': tray_capacity,
                'tray_code': tray_code,
                'tray_color': tray_color,
                'num_trays': len(tray_data),
                'tray_data': tray_data_with_sources,
                'missing_qty': missing_qty,
                'top_tray_remark': top_tray_remark,
                'is_draft': is_draft,
                'is_merged_additional': False,
                'submitted_by': request.user if request.user.is_authenticated else None,
            }
        )

        # Save records for merged lots (Add Model with same plating_stk_no).
        # These are NOT given the full combined tray_data — the physical trays
        # were scanned against the primary model above, and belong there. Saving
        # the same tray_data onto the merged lot's own record would leak those
        # tray IDs into that lot's own unload screen if it is ever opened
        # standalone (tray IDs would appear pre-filled for an unrelated lot).
        # `is_merged_additional=True` marks this lot as already consumed via
        # Add Model so it displays as "Already Loaded" and is hidden from future
        # Add Model candidate lists.
        for ml in merged_lots:
            ml_jc_id = ml.get('jig_completed_id')
            ml_lot_id = ml.get('lot_id')
            ml_jig_id = ml.get('jig_id', '')
            ml_qty = ml.get('qty', 0)
            if ml_jc_id and ml_lot_id:
                JUSubmittedZ1.objects.update_or_create(
                    jig_completed_id=ml_jc_id,
                    lot_id=ml_lot_id,
                    defaults={
                        'jig_qr_id': ml_jig_id,
                        'model_no': model_no,
                        'total_qty': ml_qty,
                        'tray_type': tray_type,
                        'tray_capacity': tray_capacity,
                        'tray_code': tray_code,
                        'tray_color': tray_color,
                        'num_trays': 0,
                        'tray_data': [],
                        'missing_qty': 0,
                        'top_tray_remark': '',
                        'is_draft': is_draft,
                        'is_merged_additional': True,
                        'submitted_by': request.user if request.user.is_authenticated else None,
                    }
                )

        # Check if all models for this jig are now unloaded
        draft_data = jc.draft_data or {}
        # Build all_lot_ids: prefer multi_model_allocation → lot_id_quantities → fallback
        _alloc = draft_data.get('multi_model_allocation', [])
        lot_id_quantities = draft_data.get('lot_id_quantities', {})
        if _alloc:
            all_lot_ids = set(a['lot_id'] for a in _alloc if a.get('lot_id'))
        elif lot_id_quantities:
            all_lot_ids = set(lot_id_quantities.keys())
        else:
            all_lot_ids = {jc.lot_id}
        submitted_lot_ids = set(
            JUSubmittedZ1.objects.filter(
                jig_completed_id=jig_completed_id, is_draft=False
            ).values_list('lot_id', flat=True)
        )
        all_unloaded = all_lot_ids.issubset(submitted_lot_ids)

        return Response({
            'success': True,
            'created': created,
            'all_unloaded': all_unloaded,
            'message': f'Model {model_no} tray data saved successfully',
        })


@method_decorator(csrf_exempt, name='dispatch')
class SubmitAllUnloadZ1View(APIView):
    """
    POST /api/submit_all_unload_z1/
    Final submission: unlocks jig, marks JigCompleted as fully unloaded.
    """

    def _build_lot_list_fields(self, lot_id, fallback_model_no=''):
        plating_stk_no_list = []
        polish_stk_no_list = []
        version_list = []

        try:
            tsm = TotalStockModel.objects.filter(lot_id=lot_id).select_related('batch_id').first()
            if tsm and tsm.batch_id:
                mmc = ModelMasterCreation.objects.select_related('version').filter(
                    id=tsm.batch_id.id
                ).first()
                if mmc:
                    if mmc.plating_stk_no:
                        plating_stk_no_list.append(mmc.plating_stk_no)
                    if mmc.polishing_stk_no:
                        polish_stk_no_list.append(mmc.polishing_stk_no)
                    if mmc.version and hasattr(mmc.version, 'version_internal'):
                        version_list.append(mmc.version.version_internal)

            if not plating_stk_no_list and fallback_model_no:
                plating_stk_no_list.append(str(fallback_model_no).strip())
        except Exception as e:
            logger.exception(
                "SubmitAllUnloadZ1: failed building model list fields for lot_id=%s: %s",
                lot_id,
                e,
            )

        return plating_stk_no_list, polish_stk_no_list, version_list

    def _build_combined_lot_list_fields(self, lot_ids, fallback_model_no=''):
        plating_stk_no_list = []
        polish_stk_no_list = []
        version_list = []

        for source_lot_id in _jul_ordered_unique(lot_ids):
            plating_values, polish_values, version_values = self._build_lot_list_fields(
                source_lot_id,
                fallback_model_no,
            )
            plating_stk_no_list.extend(plating_values)
            polish_stk_no_list.extend(polish_values)
            version_list.extend(version_values)

        return (
            _jul_ordered_unique(plating_stk_no_list),
            _jul_ordered_unique(polish_stk_no_list),
            _jul_ordered_unique(version_list),
        )

    def _discover_related_sources(self, submitted_record, excluded_lot_ids):
        if not submitted_record:
            return []

        excluded_lot_ids = set(excluded_lot_ids or [])
        sources_by_lot = {}

        def remember_source(lot_id, jig_completed_id=None, jig_id='', qty=0):
            lot_id = str(lot_id or '').strip()
            if not lot_id or lot_id in excluded_lot_ids or lot_id == submitted_record.lot_id:
                return
            sources_by_lot[lot_id] = {
                'lot_id': lot_id,
                'jig_completed_id': jig_completed_id,
                'jig_id': str(jig_id or '').strip(),
                'qty': int(qty or 0),
            }

        metadata = _jul_source_metadata_from_tray_data(submitted_record.tray_data)
        for source in metadata.get('source_mappings', []) if isinstance(metadata, dict) else []:
            if isinstance(source, dict):
                remember_source(
                    source.get('lot_id'),
                    source.get('jig_completed_id'),
                    source.get('jig_id'),
                    source.get('qty'),
                )

        if sources_by_lot:
            return list(sources_by_lot.values())

        signature = _jul_submission_tray_signature(submitted_record.tray_data)
        if not signature:
            return []

        candidates = JUSubmittedZ1.objects.filter(
            model_no=submitted_record.model_no,
            is_draft=False,
        ).exclude(id=submitted_record.id)
        for candidate in candidates:
            if candidate.lot_id in excluded_lot_ids:
                continue
            if _jul_submission_tray_signature(candidate.tray_data) != signature:
                continue
            remember_source(
                candidate.lot_id,
                candidate.jig_completed_id,
                candidate.jig_qr_id,
                candidate.total_qty,
            )

        return list(sources_by_lot.values())

    def _create_or_update_model_after_table(self, *, jc, jig_qr_id, source_lot_id, submitted_record,
                                            fallback_qty, request_user, now, extra_sources=None):
        model_no = submitted_record.model_no if submitted_record else ''
        qty = submitted_record.total_qty if submitted_record else fallback_qty
        qty = int(qty or 0)
        extra_sources = extra_sources or []
        source_lot_ids = _jul_ordered_unique(
            [source_lot_id] + [source.get('lot_id') for source in extra_sources if isinstance(source, dict)]
        )
        source_jig_ids = _jul_ordered_unique(
            [jig_qr_id or jc.lot_id] + [source.get('jig_id') for source in extra_sources if isinstance(source, dict)]
        )
        jig_qr_display = ', '.join(source_jig_ids) if source_jig_ids else (jig_qr_id or jc.lot_id)

        plating_stk_no_list, polish_stk_no_list, version_list = self._build_combined_lot_list_fields(
            source_lot_ids,
            model_no,
        )

        after_table = JigUnloadAfterTable.objects.filter(
            combine_lot_ids__contains=[source_lot_id]
        ).order_by('-id').first()
        if not after_table:
            # Zone 2 stores combine_lot_ids entries in a prefixed format
            # (e.g. 'JLOT-xxx-LIDyyy') rather than the plain lot_id Zone 1 uses above,
            # so the exact-match filter never finds a Zone-2-created row for the same
            # source lot. Fall back to a normalised comparison so the same lot
            # submitted from either zone merges into one combined record.
            for candidate in JigUnloadAfterTable.objects.filter(
                combine_lot_ids__isnull=False
            ).exclude(combine_lot_ids__exact=[]).order_by('-id'):
                if any(
                    normalize_combine_lot_id(cid) == source_lot_id
                    for cid in (candidate.combine_lot_ids or [])
                ):
                    after_table = candidate
                    break
        action = 'updated' if after_table else 'created'

        if after_table:
            changed_fields = []
            if after_table.jig_qr_id != jig_qr_display:
                after_table.jig_qr_id = jig_qr_display
                changed_fields.append('jig_qr_id')
            if after_table.combine_lot_ids != source_lot_ids:
                after_table.combine_lot_ids = source_lot_ids
                changed_fields.append('combine_lot_ids')
            if after_table.total_case_qty != qty:
                after_table.total_case_qty = qty
                changed_fields.append('total_case_qty')
            if not after_table.last_process_module:
                after_table.last_process_module = 'Jig Unloading'
                after_table.current_stage = 'Jig Unloading'
                changed_fields.append('last_process_module')
                changed_fields.append('current_stage')
            if not after_table.Un_loaded_date_time:
                after_table.Un_loaded_date_time = now
                changed_fields.append('Un_loaded_date_time')
            if plating_stk_no_list and after_table.plating_stk_no_list != plating_stk_no_list:
                after_table.plating_stk_no_list = plating_stk_no_list
                changed_fields.append('plating_stk_no_list')
            if polish_stk_no_list and after_table.polish_stk_no_list != polish_stk_no_list:
                after_table.polish_stk_no_list = polish_stk_no_list
                changed_fields.append('polish_stk_no_list')
            if version_list and after_table.version_list != version_list:
                after_table.version_list = version_list
                changed_fields.append('version_list')
            if changed_fields:
                after_table.save(update_fields=list(set(changed_fields)))
        else:
            after_table = JigUnloadAfterTable(
                jig_qr_id=jig_qr_display,
                combine_lot_ids=source_lot_ids,
                total_case_qty=qty,
                selected_user=request_user if request_user.is_authenticated else None,
                Un_loaded_date_time=now,
                last_process_module='Jig Unloading',
                current_stage='Jig Unloading',
                plating_stk_no_list=plating_stk_no_list,
                polish_stk_no_list=polish_stk_no_list,
                version_list=version_list,
            )
            after_table.save()

        logger.info(
            "Jig Unloading Submit All per-model Nickel Wiping source %s: "
            "jig_id=%s model_no=%s source_lot_ids=%s generated_lot_id=%s "
            "unload_lot_id=%s qty=%s after_table_id=%s",
            action,
            jig_qr_display,
            model_no,
            source_lot_ids,
            after_table.lot_id,
            after_table.unload_lot_id,
            qty,
            after_table.id,
        )

        return after_table, qty, model_no, action

    def post(self, request):
        jig_completed_id = request.data.get('jig_completed_id')
        if not jig_completed_id:
            return Response({'error': 'jig_completed_id is required'}, status=400)

        try:
            jc = JigCompleted.objects.get(id=jig_completed_id)
        except JigCompleted.DoesNotExist:
            return Response({'error': 'JigCompleted not found'}, status=404)

        if jc.unload_hold_lot:
            return Response({'success': False, 'error': 'This lot is on hold and cannot be processed until released.'}, status=400)

        # Verify all models are unloaded
        draft_data = jc.draft_data or {}
        # Build all_lot_ids: prefer multi_model_allocation → lot_id_quantities → fallback
        _alloc = draft_data.get('multi_model_allocation', [])
        lot_id_quantities = draft_data.get('lot_id_quantities', {})
        if _alloc:
            all_lot_ids_ordered = [a['lot_id'] for a in _alloc if a.get('lot_id')]
            # Build lot_id_quantities from allocation for qty lookup
            if not lot_id_quantities:
                lot_id_quantities = {a['lot_id']: a.get('allocated_qty', 0) for a in _alloc}
        elif lot_id_quantities:
            all_lot_ids_ordered = list(lot_id_quantities.keys())
        else:
            all_lot_ids_ordered = [jc.lot_id]
            lot_id_quantities = {jc.lot_id: jc.updated_lot_qty or 0}
        all_lot_ids_ordered = list(dict.fromkeys(all_lot_ids_ordered))
        all_lot_ids = set(all_lot_ids_ordered)
        submitted_lot_ids = set(
            JUSubmittedZ1.objects.filter(
                jig_completed_id=jig_completed_id, is_draft=False
            ).values_list('lot_id', flat=True)
        )

        if not all_lot_ids.issubset(submitted_lot_ids):
            missing = all_lot_ids - submitted_lot_ids
            return Response({
                'error': f'Not all models have been unloaded. Missing: {list(missing)}'
            }, status=400)

        from django.utils import timezone
        now = timezone.now()
        submitted_by_lot = {
            sub.lot_id: sub
            for sub in JUSubmittedZ1.objects.filter(
                jig_completed_id=jig_completed_id,
                lot_id__in=all_lot_ids_ordered,
                is_draft=False,
            )
        }
        for submitted_lot_id, submitted_record in submitted_by_lot.items():
            for tray in submitted_record.tray_data or []:
                if not isinstance(tray, dict):
                    continue
                tray_id = normalize_jig_unload_tray_id(tray.get('tray_id', ''))
                if not tray_id:
                    continue
                nickel_conflict = validate_jig_unload_nickel_reject_tray(
                    tray_id,
                    current_lot_id=submitted_lot_id,
                )
                if nickel_conflict:
                    return Response({
                        'error': nickel_conflict['message'],
                        'validation_type': 'tray_occupied',
                        'linked_lot': nickel_conflict.get('linked_lot', ''),
                        'source': nickel_conflict.get('source', ''),
                    }, status=400)

        related_sources_by_lot = {
            lot_id: self._discover_related_sources(submitted_record, all_lot_ids)
            for lot_id, submitted_record in submitted_by_lot.items()
        }
        moved_lot_ids = set(all_lot_ids)
        for related_sources in related_sources_by_lot.values():
            for related_source in related_sources:
                if related_source.get('lot_id'):
                    moved_lot_ids.add(related_source['lot_id'])

        logger.info(
            "Jig Unloading Submit All started: jig_completed_id=%s jig_id=%s lot_ids=%s",
            jig_completed_id,
            jc.jig_id or jc.lot_id,
            all_lot_ids_ordered,
        )

        with transaction.atomic():
            # Mark JigCompleted as fully unloaded
            jc.last_process_module = 'Jig Unloading'
            jc.save(update_fields=['last_process_module'])

            # Unlock the jig for reuse in Jig Loading and increment cycle count
            jig_qr_id = jc.jig_id
            if jig_qr_id:
                from django.db.models import F
                Jig.objects.filter(jig_qr_id=jig_qr_id).update(
                    is_loaded=False,
                    occupied_flag=False,
                    current_user=None,
                    locked_at=None,
                    drafted=False,
                    batch_id=None,
                    lot_id=None,
                    cycle_count=F('cycle_count') + 1,
                )
                logger.info(
                    "Zone 1 Jig Released: jig_qr_id=%s marked as free and cycle_count incremented",
                    jig_qr_id,
                )

            # Also check and free merged JigCompleted records whose lots were all submitted
            # via the merged_lots mechanism (e.g. Add Model across lots).
            merged_jig_ids = set(str(value) for value in request.data.get('merged_jig_completed_ids', []) if value)
            for related_sources in related_sources_by_lot.values():
                for related_source in related_sources:
                    related_jig_completed_id = related_source.get('jig_completed_id')
                    if related_jig_completed_id:
                        merged_jig_ids.add(str(related_jig_completed_id))
            for m_jc_id in merged_jig_ids:
                try:
                    m_jc = JigCompleted.objects.get(id=m_jc_id)
                    if m_jc.last_process_module == 'Jig Unloading':
                        continue  # Already freed
                    m_dd = m_jc.draft_data or {}
                    m_alloc = m_dd.get('multi_model_allocation', []) if isinstance(m_dd, dict) else []
                    m_liq = m_dd.get('lot_id_quantities', {}) if isinstance(m_dd, dict) else {}
                    if m_alloc:
                        m_all_lids = set(a['lot_id'] for a in m_alloc if a.get('lot_id'))
                    elif m_liq:
                        m_all_lids = set(m_liq.keys())
                    else:
                        m_all_lids = {m_jc.lot_id}
                    m_submitted = set(
                        JUSubmittedZ1.objects.filter(
                            jig_completed_id=m_jc_id, is_draft=False
                        ).values_list('lot_id', flat=True)
                    )
                    if m_all_lids and m_all_lids.issubset(m_submitted):
                        m_jc.last_process_module = 'Jig Unloading'
                        m_jc.save(update_fields=['last_process_module'])
                        m_jig_qr = m_jc.jig_id
                        if m_jig_qr:
                            from django.db.models import F
                            Jig.objects.filter(jig_qr_id=m_jig_qr).update(
                                is_loaded=False, occupied_flag=False, current_user=None, locked_at=None,
                                drafted=False, batch_id=None, lot_id=None, cycle_count=F('cycle_count') + 1,
                            )
                            logger.info(
                                "Zone 1 Merged Jig Released: jig_qr_id=%s marked as free and cycle_count incremented",
                                m_jig_qr,
                            )
                except JigCompleted.DoesNotExist:
                    pass

            # Submit All must create one downstream Nickel Wiping source row per model lot.
            created_records = []
            for lid in all_lot_ids_ordered:
                sub = submitted_by_lot.get(lid)
                related_sources = related_sources_by_lot.get(lid, [])
                after_table, qty, model_no, action = self._create_or_update_model_after_table(
                    jc=jc,
                    jig_qr_id=jig_qr_id,
                    source_lot_id=lid,
                    submitted_record=sub,
                    fallback_qty=lot_id_quantities.get(lid, 0),
                    request_user=request.user,
                    now=now,
                    extra_sources=related_sources,
                )
                created_records.append({
                    'source_lot_id': lid,
                    'source_lot_ids': _jul_ordered_unique([lid] + [source.get('lot_id') for source in related_sources]),
                    'lot_id': lid,
                    'generated_lot_id': after_table.lot_id,
                    'model_no': model_no,
                    'unload_lot_id': after_table.unload_lot_id,
                    'qty': qty,
                    'action': action,
                })

            # Update TotalStockModel records to reflect Jig Unloading completion
            updated_count = TotalStockModel.objects.filter(lot_id__in=moved_lot_ids).update(
                last_process_module='Jig Unloading',
                next_process_module='Nickel Inspection',
                current_stage='Jig Unloading',
                last_process_date_time=now
            )
            logger.info(
                "Jig Unloading Submit All stock update: jig_id=%s updated_total_stock_rows=%s lot_ids=%s",
                jig_qr_id or jc.lot_id,
                updated_count,
                sorted(moved_lot_ids),
            )

        first_record = created_records[0] if created_records else {}

        return Response({
            'success': True,
            'message': 'Jig unloading submitted successfully. Jig unlocked for reuse.',
            'records': created_records,
            'unload_lot_id': first_record.get('unload_lot_id') or '',
        })


@method_decorator(csrf_exempt, name='dispatch')
class SubmitSingleModelZ1View(APIView):
    """
    POST /api/submit_single_model_z1/
    Submits a single model as an independent lot (creates standalone JigUnloadAfterTable).
    Used by the per-model Submit button in the z1LayerModelList.
    """
    def post(self, request):
        from django.utils import timezone
        jig_completed_id = request.data.get('jig_completed_id')
        lot_id = request.data.get('lot_id')

        if not jig_completed_id or not lot_id:
            return Response({'error': 'jig_completed_id and lot_id are required'}, status=400)

        try:
            jc = JigCompleted.objects.get(id=jig_completed_id)
        except JigCompleted.DoesNotExist:
            return Response({'error': 'JigCompleted not found'}, status=404)

        if jc.unload_hold_lot:
            return Response({'success': False, 'error': 'This lot is on hold and cannot be processed until released.'}, status=400)

        # Verify this model has been unloaded (final, not draft)
        sub = JUSubmittedZ1.objects.filter(
            jig_completed_id=jig_completed_id, lot_id=lot_id, is_draft=False
        ).first()
        if not sub:
            return Response({'error': 'Model not yet unloaded. Complete tray scan first.'}, status=400)

        # Idempotency: return existing record if already submitted. Uses a
        # normalised fallback because Zone 2 stores combine_lot_ids in a
        # prefixed format (e.g. 'JLOT-xxx-LIDyyy'), so without it, a lot
        # already merged in via Zone 2 is invisible here and this endpoint
        # creates a duplicate standalone JigUnloadAfterTable row for it —
        # the duplicate then shows as a second lot in downstream Nickel
        # Inspection Zone 1/2 pick tables instead of one combined lot.
        existing = _jul_find_after_table_by_source_lot_id(lot_id)
        if existing:
            return Response({
                'success': True,
                'already_submitted': True,
                'lot_id': existing.lot_id,
                'unload_lot_id': existing.unload_lot_id or '',
                'message': f'Model already submitted as lot {existing.lot_id}',
            })

        for tray in sub.tray_data or []:
            if not isinstance(tray, dict):
                continue
            tray_id = normalize_jig_unload_tray_id(tray.get('tray_id', ''))
            if not tray_id:
                continue
            nickel_conflict = validate_jig_unload_nickel_reject_tray(
                tray_id,
                current_lot_id=lot_id,
            )
            if nickel_conflict:
                return Response({
                    'error': nickel_conflict['message'],
                    'validation_type': 'tray_occupied',
                    'linked_lot': nickel_conflict.get('linked_lot', ''),
                    'source': nickel_conflict.get('source', ''),
                }, status=400)

        # Build list fields for the single lot
        plating_stk_no_list = []
        polish_stk_no_list = []
        version_list = []
        try:
            tsm = TotalStockModel.objects.filter(lot_id=lot_id).select_related('batch_id').first()
            if tsm and tsm.batch_id:
                mmc = ModelMasterCreation.objects.select_related('version').filter(
                    id=tsm.batch_id.id
                ).first()
                if mmc:
                    if mmc.plating_stk_no:
                        plating_stk_no_list.append(mmc.plating_stk_no)
                    if mmc.polishing_stk_no:
                        polish_stk_no_list.append(mmc.polishing_stk_no)
                    if mmc.version and hasattr(mmc.version, 'version_internal'):
                        version_list.append(mmc.version.version_internal)
        except Exception as e:
            print(f"⚠️ SubmitSingleModelZ1: Error building list fields for {lot_id}: {e}")

        after_table = JigUnloadAfterTable(
            jig_qr_id=jc.jig_id or jc.lot_id,
            combine_lot_ids=[lot_id],
            total_case_qty=sub.total_qty,
            selected_user=request.user if request.user.is_authenticated else None,
            Un_loaded_date_time=timezone.now(),
            last_process_module='Jig Unloading',
            plating_stk_no_list=plating_stk_no_list,
            polish_stk_no_list=polish_stk_no_list,
            version_list=version_list,
        )
        after_table.save()

        # Update TotalStockModel to reflect Jig Unloading completion
        TotalStockModel.objects.filter(lot_id=lot_id).update(
            last_process_module='Jig Unloading',
            next_process_module='Nickel Inspection',
            last_process_date_time=timezone.now(),
        )

        return Response({
            'success': True,
            'lot_id': after_table.lot_id,
            'unload_lot_id': after_table.unload_lot_id or '',
            'message': f'Model submitted successfully as lot {after_table.lot_id}',
        })


@method_decorator(csrf_exempt, name='dispatch')
class GetUnloadViewZ1View(APIView):
    """
    GET /api/get_unload_view_z1/?jig_completed_id=<id>
    Returns submitted data for read-only view.
    """
    def get(self, request):
        jig_completed_id = request.GET.get('jig_completed_id')
        if not jig_completed_id:
            return Response({'error': 'jig_completed_id is required'}, status=400)

        records = JUSubmittedZ1.objects.filter(
            jig_completed_id=jig_completed_id
        ).order_by('model_no')

        if not records.exists():
            return Response({'error': 'No submitted records found'}, status=404)

        result = []
        for rec in records:
            result.append({
                'model_no': rec.model_no,
                'lot_id': rec.lot_id,
                'total_qty': rec.total_qty,
                'tray_type': rec.tray_type,
                'tray_capacity': rec.tray_capacity,
                'tray_code': rec.tray_code,
                'tray_color': rec.tray_color,
                'num_trays': rec.num_trays,
                'tray_data': rec.tray_data,
                'missing_qty': rec.missing_qty,
                'top_tray_remark': rec.top_tray_remark,
                'submitted_at': rec.submitted_at.strftime('%d-%b-%Y %I:%M %p') if rec.submitted_at else '',
                'submitted_by': rec.submitted_by.username if rec.submitted_by else 'System',
            })

        return Response({
            'jig_completed_id': int(jig_completed_id),
            'records': result,
        })


@method_decorator(csrf_exempt, name='dispatch')
class JigUnloadPickRemarkZ1View(APIView):
    """
    POST /api/save_jig_pick_remark_z1/
    Saves unloading_remarks on JigCompleted record.
    """
    def post(self, request):
        try:
            data = request.data
            jig_completed_id = data.get('jig_completed_id')
            remark = (data.get('unloading_remarks') or '').strip()
            if not jig_completed_id:
                return Response({'success': False, 'error': 'jig_completed_id is required'}, status=400)
            try:
                jc = JigCompleted.objects.get(id=jig_completed_id)
            except JigCompleted.DoesNotExist:
                return Response({'success': False, 'error': 'JigCompleted not found'}, status=404)
            jc.unloading_remarks = remark
            jc.save(update_fields=['unloading_remarks'])
            return Response({'success': True, 'message': 'Remark saved'})
        except Exception as e:
            return Response({'success': False, 'error': 'Unable to process the request. Please verify the submitted data and try again.'}, status=500)


@method_decorator(csrf_exempt, name='dispatch')
class JigUnloadHoldUnholdZ1View(APIView):
    """
    POST /jig_unloading/api/hold_unhold_z1/
    Holds or releases a JigCompleted lot with a mandatory remark.
    """
    permission_classes = [IsAuthenticated]

    def post(self, request):
        from django.utils import timezone

        lot_id = request.data.get('lot_id')
        action = request.data.get('action')
        remark = (request.data.get('remark') or '').strip()

        if not lot_id:
            return Response({'success': False, 'error': 'lot_id is required'}, status=400)
        if action not in ('hold', 'unhold'):
            return Response({'success': False, 'error': "action must be 'hold' or 'unhold'"}, status=400)
        if not remark:
            return Response({'success': False, 'error': 'Remark is required'}, status=400)
        if len(remark) > 50:
            return Response({'success': False, 'error': 'Remark must be 50 characters or less'}, status=400)

        jc = JigCompleted.objects.filter(lot_id=lot_id).first()
        if not jc:
            return Response({'success': False, 'error': 'Lot not found'}, status=404)

        now = timezone.now()
        if action == 'hold':
            jc.unload_hold_lot = True
            jc.unload_holding_reason = remark
            jc.unload_hold_by = request.user
            jc.unload_hold_at = now
            jc.unload_release_lot = False
            jc.unload_release_reason = ''
        else:
            jc.unload_hold_lot = False
            jc.unload_release_reason = remark
            jc.unload_release_by = request.user
            jc.unload_release_at = now
            jc.unload_release_lot = True

        jc.save(update_fields=[
            'unload_hold_lot', 'unload_holding_reason', 'unload_hold_by', 'unload_hold_at',
            'unload_release_lot', 'unload_release_reason', 'unload_release_by', 'unload_release_at',
        ])
        return Response({
            'success': True, 'lot_id': lot_id, 'action': action,
            'holding_reason': jc.unload_holding_reason or '', 'release_reason': jc.unload_release_reason or '',
            'hold_lot': jc.unload_hold_lot, 'release_lot': jc.unload_release_lot,
            'hold_by': jc.unload_hold_by.username if jc.unload_hold_by else '',
            'hold_at': timezone.localtime(jc.unload_hold_at).strftime("%d-%b-%Y %I:%M %p") if jc.unload_hold_at else '',
            'release_by': jc.unload_release_by.username if jc.unload_release_by else '',
            'release_at': timezone.localtime(jc.unload_release_at).strftime("%d-%b-%Y %I:%M %p") if jc.unload_release_at else '',
            'message': f"Lot {'held' if action == 'hold' else 'released'} successfully.",
        })


@method_decorator(csrf_exempt, name='dispatch')
class GetJigForTrayZ1View(APIView):
    """
    GET /api/get_jig_for_tray_z1/?tray_id=<id>
    Searches JigCompleted draft_data for a tray ID and returns matching jig info.
    Used by the scan button to locate jigs across pagination.
    """
    def get(self, request):
        tray_id = request.GET.get('tray_id', '').strip()
        if not tray_id:
            return Response({'success': False, 'error': 'Tray ID is required'})

        try:
            # Search in scanned_trays JSONField
            all_jigs = JigCompleted.objects.filter(
                draft_status='submitted'
            )
            for jc in all_jigs:
                scanned = jc.scanned_trays or []
                for tray_entry in scanned:
                    if isinstance(tray_entry, dict) and tray_entry.get('tray_id') == tray_id:
                        return Response({
                            'success': True,
                            'jig_completed_id': jc.id,
                            'lot_id': jc.lot_id,
                            'jig_id': jc.jig_id or jc.lot_id,
                        })

            # Fallback: search in draft_data tray slots
            for jc in all_jigs:
                dd = jc.draft_data or {}
                if isinstance(dd, dict):
                    alloc = dd.get('multi_model_allocation', [])
                    for model_alloc in alloc:
                        for tray_entry in (model_alloc.get('tray_data') or []):
                            if isinstance(tray_entry, dict) and tray_entry.get('tray_id') == tray_id:
                                return Response({
                                    'success': True,
                                    'jig_completed_id': jc.id,
                                    'lot_id': jc.lot_id,
                                    'jig_id': jc.jig_id or jc.lot_id,
                                })

            # Search in JUSubmittedZ1 tray_data
            submitted = JUSubmittedZ1.objects.all()
            for rec in submitted:
                for tray_entry in (rec.tray_data or []):
                    if isinstance(tray_entry, dict) and tray_entry.get('tray_id') == tray_id:
                        try:
                            jc = JigCompleted.objects.get(id=rec.jig_completed_id)
                            return Response({
                                'success': True,
                                'jig_completed_id': jc.id,
                                'lot_id': jc.lot_id,
                                'jig_id': jc.jig_id or jc.lot_id,
                            })
                        except JigCompleted.DoesNotExist:
                            pass

            return Response({'success': False, 'error': 'Tray ID not found in any jig'})

        except Exception as e:
            return Response({'success': False, 'error': 'Unable to process the request. Please verify the submitted data and try again.'}, status=500)


@require_GET
def validate_tray_occupancy_z1(request):
    """
    GET /api/validate_tray_occupancy_z1/?tray_id=XX&lot_id=YY
    Zone 1 tray validation — accepts any valid tray code prefix from master data
    (NR, ND, NB, NL, JR, JB, JD) and checks occupancy.
    """
    import re
    from modelmasterapp.tray_code_mapping import TRAY_CODE_MASTER_DATA
    
    tray_id = request.GET.get('tray_id', '').strip().upper()
    lot_id = request.GET.get('lot_id', '').strip()

    if not tray_id:
        return JsonResponse({'success': False, 'error': 'Tray ID is required'}, status=400)

    try:
        # STEP 1: Validate tray code format — must be <PREFIX>-A<5digits>
        # Allowed prefixes come from the master data: NR, ND, NB, NL, JR, JB, JD
        all_tray_codes = set()
        for info in TRAY_CODE_MASTER_DATA.values():
            for code in info.get('tray_codes', []):
                all_tray_codes.add(code)
        # Build regex: (NR|ND|NB|NL|JR|JB|JD)-A\d{5}
        prefix_pattern = '|'.join(sorted(all_tray_codes))
        pattern = rf'^({prefix_pattern})-A\d{{5}}$'
        match = re.match(pattern, tray_id)
        if not match:
            return JsonResponse({
                'success': False,
                'error': f'Invalid tray code format. Expected: <CODE>-A00001 where CODE is one of: {", ".join(sorted(all_tray_codes))}',
                'message': f'❌ Invalid tray code format: {tray_id}',
                'validation_type': 'invalid_format',
                'allowed_codes': sorted(list(all_tray_codes))
            }, status=400)

        scanned_prefix = match.group(1)

        # STEP 2: If lot_id provided, validate prefix against the lot's plating stock
        if lot_id:
            # Try to resolve plating_stk_no from TotalStockModel → ModelMasterCreation
            plating_stk = None
            tsm = TotalStockModel.objects.filter(lot_id=lot_id).select_related('batch_id').first()
            if tsm and tsm.batch_id:
                mmc = ModelMasterCreation.objects.filter(id=tsm.batch_id.id).first()
                if mmc:
                    plating_stk = getattr(mmc, 'plating_stk_no', None) or ''
            # Recovery fallback
            if not plating_stk:
                rsm = RecoveryStockModel.objects.filter(lot_id=lot_id).select_related('batch_id').first()
                if rsm and rsm.batch_id:
                    try:
                        from Recovery_DP.models import RecoveryMasterCreation
                        rmc = RecoveryMasterCreation.objects.filter(id=rsm.batch_id.id).first()
                        if rmc:
                            plating_stk = getattr(rmc, 'plating_stk_no', None) or ''
                    except ImportError:
                        pass
            # JigCompleted fallback
            if not plating_stk:
                jc = JigCompleted.objects.filter(lot_id=lot_id).first()
                if jc and jc.batch_id:
                    mmc = ModelMasterCreation.objects.filter(batch_id=jc.batch_id).first()
                    if mmc:
                        plating_stk = getattr(mmc, 'plating_stk_no', None) or ''

            if plating_stk and plating_stk in TRAY_CODE_MASTER_DATA:
                allowed = TRAY_CODE_MASTER_DATA[plating_stk]
                allowed_codes = allowed.get('tray_codes', [])
                if scanned_prefix not in allowed_codes:
                    return JsonResponse({
                        'success': False,
                        'error': f'Tray code "{scanned_prefix}" is not allowed for plating stock {plating_stk}. '
                                 f'Expected: {", ".join(allowed_codes)}',
                        'message': f'❌ Wrong tray code for this model. Expected: {", ".join(allowed_codes)}',
                        'validation_type': 'wrong_tray_code',
                        'expected_codes': allowed_codes,
                        'plating_stock': plating_stk
                    }, status=400)

        print(f"✅ [Z1] Tray code format valid: {tray_id} (prefix: {scanned_prefix})")

        tray_conflict = find_jig_unload_tray_conflict(
            tray_id,
            allowed_lot_ids=[lot_id] if lot_id else [],
            include_tray_master=True,
        )
        if tray_conflict:
            return JsonResponse({
                'success': False,
                'error': tray_conflict['message'],
                'message': f'❌ {tray_id} - Already reserved for another lot',
                'validation_type': 'tray_occupied',
                'linked_lot': tray_conflict.get('linked_lot', ''),
                'source': tray_conflict.get('source', '')
            }, status=400)

        nickel_conflict = validate_jig_unload_nickel_reject_tray(
            tray_id,
            current_lot_id=lot_id or None,
        )
        if nickel_conflict:
            return JsonResponse({
                'success': False,
                'error': nickel_conflict['message'],
                'message': f'âŒ {tray_id} - Already reserved for another lot',
                'validation_type': 'tray_occupied',
                'linked_lot': nickel_conflict.get('linked_lot', ''),
                'source': nickel_conflict.get('source', '')
            }, status=400)

        # STEP 3: Check occupancy in TrayId table
        existing_tray = TrayId.objects.filter(tray_id=tray_id).first()
        if not existing_tray:
            # Tray not in system — accept it (will be created on save)
            return JsonResponse({
                'success': True,
                'message': 'Tray ID not in system — new tray',
                'validation_type': 'new_tray'
            })

        # STEP 4: Check if already scanned and not delinked → occupied
        if existing_tray.scanned and not existing_tray.delink_tray:
            return JsonResponse({
                'success': False,
                'error': f'Tray "{tray_id}" is already scanned and occupied by lot {existing_tray.lot_id}. '
                         f'Please use a free tray or delink this one first.',
                'validation_type': 'already_scanned',
                'linked_lot': existing_tray.lot_id
            })

        # STEP 5: Check if assigned to different lot and not delinked → occupied
        if existing_tray.lot_id and not existing_tray.delink_tray:
            if lot_id and existing_tray.lot_id != lot_id:
                return JsonResponse({
                    'success': False,
                    'error': f'Tray "{tray_id}" is assigned to another lot ({existing_tray.lot_id}). '
                             f'Please use a free tray or delink this one first.',
                    'validation_type': 'already_assigned',
                    'linked_lot': existing_tray.lot_id
                })

        # STEP 6: Tray is valid - free, delinked, or belongs to current lot
        # Determine tray type from prefix
        is_jumbo = scanned_prefix.startswith('J')
        tray_type_str = 'Jumbo' if is_jumbo else 'Normal'
        tray_capacity = 12 if is_jumbo else 20

        print(f"✅ [Z1] Tray validation passed for {tray_id}")
        return JsonResponse({
            'success': True,
            'message': 'Tray is available',
            'validation_type': 'valid',
            'tray_details': {
                'tray_id': tray_id,
                'tray_type': tray_type_str,
                'tray_code': scanned_prefix,
                'capacity': existing_tray.tray_capacity if existing_tray else tray_capacity
            }
        })

    except Exception as e:
        logger.error(f"❌ [Z1] Validation error: {str(e)}", exc_info=True)
        return JsonResponse({'success': False, 'error': 'Unable to process the request. Please verify the submitted data and try again.'}, status=500)


@require_GET
def jig_unload_view_tray_list_z1(request):
    """
    GET /jig_unload_view_tray_list/?lot_id=<UNLOT_ID>
    Returns tray list for the Completed table View icon.
    Sources data from JUSubmittedZ1 (Z1 unload modal) with fallback to JigUnload_TrayId.
    """
    lot_id = request.GET.get('lot_id', '').strip()
    if not lot_id:
        return JsonResponse({'success': False, 'error': 'lot_id is required'}, status=400)

    try:
        record = JigUnloadAfterTable.objects.filter(lot_id=lot_id).first()
        if not record:
            return JsonResponse({'success': False, 'error': f'Record not found for lot_id: {lot_id}'}, status=404)

        combine_lot_ids = [_jul_extract_source_lot_id(lot_id) for lot_id in (record.combine_lot_ids or [])]
        combine_lot_ids = _jul_ordered_unique(combine_lot_ids)
        if not combine_lot_ids:
            return JsonResponse({'success': False, 'error': 'No combine_lot_ids found'}, status=404)

        # Primary source: JUSubmittedZ1 tray_data (Z1 unload modal saves here)
        tray_list = []
        subs = list(JUSubmittedZ1.objects.filter(lot_id__in=combine_lot_ids, is_draft=False).order_by('id'))
        source_summary_by_key = {}

        for sub in subs:
            metadata = _jul_source_metadata_from_tray_data(sub.tray_data)
            mappings = metadata.get('source_mappings', []) if isinstance(metadata, dict) else []
            if mappings:
                for mapping in mappings:
                    if not isinstance(mapping, dict):
                        continue
                    source_lot_id = str(mapping.get('lot_id') or '').strip()
                    source_jig_id = str(mapping.get('jig_id') or sub.jig_qr_id or '').strip()
                    if not source_lot_id and not source_jig_id:
                        continue
                    source_summary_by_key[(source_jig_id, source_lot_id)] = {
                        'jig_id': source_jig_id or 'N/A',
                        'lot_id': source_lot_id or sub.lot_id,
                        'qty': int(mapping.get('qty') or 0),
                    }
            else:
                source_summary_by_key[(sub.jig_qr_id or 'N/A', sub.lot_id)] = {
                    'jig_id': sub.jig_qr_id or 'N/A',
                    'lot_id': sub.lot_id,
                    'qty': int(sub.total_qty or 0),
                }

        canonical_subs = []
        exact_total_matches = [
            sub for sub in subs
            if int(sub.total_qty or 0) == int(record.total_case_qty or 0)
        ]
        if exact_total_matches:
            canonical_subs = [exact_total_matches[0]]
        else:
            seen_signatures = set()
            for sub in subs:
                signature = _jul_submission_tray_signature(sub.tray_data)
                if signature and signature in seen_signatures:
                    continue
                seen_signatures.add(signature)
                canonical_subs.append(sub)

        source_jig_display = ', '.join(_jul_ordered_unique(
            summary.get('jig_id') for summary in source_summary_by_key.values()
        ))

        for sub in canonical_subs:
            for entry in (sub.tray_data or []):
                tray_list.append({
                    'tray_id': entry.get('tray_id', ''),
                    'tray_quantity': entry.get('qty', 0),
                    'top_tray': entry.get('is_top_tray', False),
                    'source_jig': source_jig_display or sub.jig_qr_id or 'N/A',
                })

        # Fallback: JigUnload_TrayId (old Zone 2 flow)
        if not tray_list:
            trays = JigUnload_TrayId.objects.filter(lot_id__in=combine_lot_ids).order_by('id')
            for tray in trays:
                tray_list.append({
                    'tray_id': tray.tray_id,
                    'tray_quantity': tray.tray_qty,
                    'top_tray': tray.top_tray,
                    'source_jig': source_jig_display or 'N/A',
                })

        return JsonResponse({
            'success': True,
            'combine_lot_ids': combine_lot_ids,
            'jig_summary': list(source_summary_by_key.values()),
            'trays': tray_list,
        })

    except Exception as e:
        return JsonResponse({'success': False, 'error': 'Unable to process the request. Please verify the submitted data and try again.'}, status=500)


@require_GET
def jig_unload_get_model_images_z1(request):
    """
    GET /get_model_images/?lot_id=<UNLOT_ID>
    Returns model images for the Completed table View icon.
    """
    import re
    lot_id = request.GET.get('lot_id', '').strip()
    model_number = request.GET.get('model_number', '').strip()

    if not lot_id:
        return JsonResponse({'success': False, 'error': 'lot_id required'}, status=400)

    try:
        model = None
        model_no = None

        # Method 1: JigUnloadAfterTable → plating_stk_no → ModelMaster
        jig_unload = JigUnloadAfterTable.objects.filter(lot_id=lot_id).first()
        if jig_unload and jig_unload.plating_stk_no:
            candidate = ModelMaster.objects.prefetch_related('images').filter(
                plating_stk_no=jig_unload.plating_stk_no
            ).first()
            if candidate:
                model = candidate
                model_no = jig_unload.plating_stk_no

        # Method 2: JigUnloadAfterTable → polish_stk_no → ModelMaster by model_no
        if not model and jig_unload and jig_unload.polish_stk_no:
            raw_no = jig_unload.polish_stk_no.split('X')[0] if 'X' in jig_unload.polish_stk_no else jig_unload.polish_stk_no
            candidate = ModelMaster.objects.prefetch_related('images').filter(model_no=raw_no).first()
            if candidate:
                model = candidate
                model_no = raw_no

        # Method 3: combine_lot_ids → TotalStockModel → ModelMasterCreation → ModelMaster
        if not model and jig_unload:
            combine_lot_ids = jig_unload.combine_lot_ids or []
            for clid in combine_lot_ids:
                tsm = TotalStockModel.objects.filter(lot_id=clid).select_related('batch_id__model_stock_no').first()
                if tsm and tsm.batch_id and tsm.batch_id.model_stock_no:
                    candidate = ModelMaster.objects.prefetch_related('images').filter(pk=tsm.batch_id.model_stock_no.pk).first()
                    if candidate:
                        model = candidate
                        model_no = candidate.plating_stk_no or candidate.model_no
                        break

        # Method 4: model_number param
        if not model and model_number:
            candidate = ModelMaster.objects.prefetch_related('images').filter(plating_stk_no=model_number).first()
            if candidate:
                model = candidate
                model_no = model_number

        if not model:
            return JsonResponse({'success': False, 'image': None, 'error': f'No model found for lot_id: {lot_id}'})

        from modelmasterapp.image_utils import sort_images_front_first
        images = [img.master_image.url for img in sort_images_front_first(model.images.all()) if img.master_image]
        if images:
            return JsonResponse({
                'success': True, 'image': images[0],
                'total_images': len(images), 'all_images': images, 'model_no': model_no,
            })
        else:
            return JsonResponse({'success': False, 'image': None, 'error': f'Model {model_no} has no images'})

    except Exception as e:
        return JsonResponse({'success': False, 'error': 'Unable to process the request. Please verify the submitted data and try again.'}, status=500)

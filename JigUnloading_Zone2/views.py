from django.shortcuts import render
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods
from modelmasterapp.models import *
from modelmasterapp.tray_code_mapping import get_tray_codes_for_plating_stock, validate_tray_code_for_stock
from modelmasterapp.color_service import get_model_colors_by_model_no
from django.db.models import OuterRef, Subquery, Exists, F, TextField, Q
from django.db.models.functions import Cast
from django.db.models.fields.json import KeyTextTransform
from django.core.paginator import Paginator
import builtins
import math
import json
import logging
import re
import sys
from django.utils.decorators import method_decorator
from rest_framework.views import APIView
from django.views.generic import TemplateView
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST
from rest_framework.decorators import api_view, permission_classes
from rest_framework.exceptions import ParseError
from rest_framework.permissions import IsAuthenticated
from django.contrib.auth.decorators import login_required
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


def _zone2_safe_print(*args, **kwargs):
    """Prevent Zone 2 debug output from breaking requests on non-UTF-8 consoles."""
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


print = _zone2_safe_print


def _zone2_ordered_unique(values):
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


def _zone2_extract_lot_id(raw_id):
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


def _zone2_submission_tray_signature(tray_data):
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


def _zone2_source_metadata_from_tray_data(tray_data):
    if not isinstance(tray_data, list):
        return {}
    for entry in tray_data:
        if not isinstance(entry, dict):
            continue
        metadata = entry.get('_source_metadata') or entry.get('source_metadata')
        if isinstance(metadata, dict):
            return metadata
    return {}

class JU_Zone_MainTable(LoginRequiredMixin, TemplateView):
    template_name = "Jig_Unloading - Zone_two/Jig_Unloading_Main_zone_two.html"
    login_url = 'login'

    def get_dynamic_tray_capacity(self, tray_type_name):
        """
        Get dynamic tray capacity using InprocessInspectionTrayCapacity for overrides
        Rules (sourced from TrayType master DB):
        - Normal (or NR/NB/ND/NL): 20
        - Jumbo  (or JR/JB/JD):    12 (matches TrayType master data)
        - Others: Use InprocessInspectionTrayCapacity or ModelMaster capacity
        """
        try:
            # Workflow-spec capacity — covers both type names and tray code prefixes
            _tn = (tray_type_name or '').upper()
            if _tn in ('NORMAL', 'NR', 'NB', 'ND', 'NL', 'NW'):
                return 20
            elif _tn in ('JUMBO', 'JR', 'JB', 'JD'):
                return 12
            else:
                # First try to get custom capacity for this tray type
                custom_capacity = InprocessInspectionTrayCapacity.objects.filter(
                    tray_type__tray_type=tray_type_name,
                    is_active=True
                ).first()
                
                if custom_capacity:
                    return custom_capacity.custom_capacity
                
                # Fallback to ModelMaster tray capacity
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
                print(f"✅ Zone 2 get_lot_specific_data via JigCompleted.batch_id={_jc.batch_id}: tray={_tray_type_str}")
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

    def get_multiple_lot_ids(self, jig_detail):
        """
        Get multiple lot_ids exactly like InprocessInspectionCompleteView does
        This ensures we get the same comma-separated behavior
        """
        print(f"\n🔎 get_multiple_lot_ids for JigDetail ID: {jig_detail.id}")
        
        # First, check if new_lot_ids field exists and has data (like JigCompletedTable)
        new_lot_ids = getattr(jig_detail, 'new_lot_ids', None)
        print(f"   🔍 Checking new_lot_ids field: {new_lot_ids}")
        
        if new_lot_ids and len(new_lot_ids) > 0:
            print(f"   ✅ Found new_lot_ids with {len(new_lot_ids)} items: {new_lot_ids}")
            return new_lot_ids
        
        # Check if there's a lot_ids field (plural)
        lot_ids_field = getattr(jig_detail, 'lot_ids', None)
        print(f"   🔍 Checking lot_ids field: {lot_ids_field}")
        
        if lot_ids_field and len(lot_ids_field) > 0:
            print(f"   ✅ Found lot_ids with {len(lot_ids_field)} items: {lot_ids_field}")
            return lot_ids_field
        
        # As a fallback, use the single lot_id if it exists
        if jig_detail.lot_id:
            print(f"   📝 Using single lot_id as fallback: [{jig_detail.lot_id}]")
            return [jig_detail.lot_id]
        
        print(f"   ❌ No lot_ids found, returning empty list")
        return []

    def process_new_lot_ids(self, new_lot_ids):
        """
        Process new_lot_ids ArrayField to get plating_stk_no, polishing_stk_no, version_name
        Updated to search both TotalStockModel and RecoveryStockModel
        Returns comma-separated values for each field
        """
        print(f"\n🎯 process_new_lot_ids called with: {new_lot_ids}")
        
        if not new_lot_ids:
            print("   ❌ No new_lot_ids provided")
            return {
                'plating_stk_nos': [],
                'polishing_stk_nos': [],
                'version_names': '',
                'plating_stk_nos_list': [],
                'polishing_stk_nos_list': [],
                'version_names_list': []
            }
        
        # STEP 1: Get batch_ids from lot_ids using BOTH stock models
        print("   🔍 STEP 1: Getting batch_ids from lot_ids...")
        
        # *** UPDATED: Search both TotalStockModel and RecoveryStockModel ***
        total_stocks = TotalStockModel.objects.filter(
            lot_id__in=new_lot_ids
        ).select_related('batch_id')
        
        recovery_stocks = RecoveryStockModel.objects.filter(
            lot_id__in=new_lot_ids
        ).select_related('batch_id')
        
        print(f"   📦 Found {len(total_stocks)} TotalStockModel records")
        print(f"   📦 Found {len(recovery_stocks)} RecoveryStockModel records")
        
        # Create lot_id to batch_id mapping from both models
        lot_to_batch = {}
        batch_ids = []
        batch_to_model_type = {}  # Track which model type each batch_id comes from
        
        # Process TotalStock results first (priority)
        for stock in total_stocks:
            if stock.batch_id:
                lot_to_batch[stock.lot_id] = stock.batch_id.id
                if stock.batch_id.id not in batch_ids:
                    batch_ids.append(stock.batch_id.id)
                    batch_to_model_type[stock.batch_id.id] = 'TotalStockModel'
                print(f"   ✅ TotalStock: {stock.lot_id} -> batch_id: {stock.batch_id.id}")
        
        # Process RecoveryStock results (only if not already found in TotalStock)
        for stock in recovery_stocks:
            if stock.batch_id and stock.lot_id not in lot_to_batch:
                lot_to_batch[stock.lot_id] = stock.batch_id.id
                if stock.batch_id.id not in batch_ids:
                    batch_ids.append(stock.batch_id.id)
                    batch_to_model_type[stock.batch_id.id] = 'RecoveryStockModel'
                print(f"   ✅ RecoveryStock: {stock.lot_id} -> batch_id: {stock.batch_id.id}")
        
        # Fallback: JigCompleted → ModelMasterCreation (for Jig Loading lot IDs)
        # These lot IDs are not in TotalStockModel/RecoveryStockModel because
        # they are jig-level lot IDs generated during Jig Loading, not IS lot IDs.
        missing_lot_ids = [lid for lid in new_lot_ids if lid not in lot_to_batch]
        if missing_lot_ids:
            from Jig_Loading.models import JigCompleted
            jig_completed_recs = JigCompleted.objects.filter(lot_id__in=missing_lot_ids)
            for jc in jig_completed_recs:
                if jc.batch_id:
                    mmc = ModelMasterCreation.objects.filter(batch_id=jc.batch_id).first()
                    if mmc and jc.lot_id not in lot_to_batch:
                        lot_to_batch[jc.lot_id] = mmc.id
                        if mmc.id not in batch_ids:
                            batch_ids.append(mmc.id)
                            batch_to_model_type[mmc.id] = 'JigCompleted'
                        print(f"   ✅ JigCompleted: {jc.lot_id} -> MMC batch={jc.batch_id} -> id={mmc.id}")
        
        print(f"   📋 Total unique batch_ids found: {len(batch_ids)}")
        
        # STEP 2: Get ModelMasterCreation data for all batch_ids
        print("   🔍 STEP 2: Getting ModelMasterCreation data...")
        
        plating_stk_nos = []
        polishing_stk_nos = []
        version_names = []
        
        # Try ModelMasterCreation first
        mmc_results = ModelMasterCreation.objects.filter(
            id__in=batch_ids
        ).select_related('version').values(
            'id', 'plating_stk_no', 'polishing_stk_no', 'version__version_name'
        )
        
        mmc_dict = {mmc['id']: mmc for mmc in mmc_results}
        
        # Try RecoveryMasterCreation for missing batch_ids
        missing_batch_ids = [bid for bid in batch_ids if bid not in mmc_dict]
        rmc_dict = {}
        
        if missing_batch_ids:
            try:
                from Recovery_DP.models import RecoveryMasterCreation
                rmc_results = RecoveryMasterCreation.objects.filter(
                    id__in=missing_batch_ids
                ).select_related('version').values(
                    'id', 'plating_stk_no', 'polishing_stk_no', 'version__version_name'
                )
                rmc_dict = {rmc['id']: rmc for rmc in rmc_results}
                print(f"   ✅ Found {len(rmc_dict)} RecoveryMasterCreation records")
            except ImportError:
                print("   ⚠️ RecoveryMasterCreation not available")
        
        # Process all batch_ids in the order they appear in new_lot_ids
        for lot_id in new_lot_ids:
            batch_id = lot_to_batch.get(lot_id)
            if batch_id:
                # Try ModelMasterCreation first
                if batch_id in mmc_dict:
                    data = mmc_dict[batch_id]
                    plating_stk_nos.append(data.get('plating_stk_no') or '')
                    polishing_stk_nos.append(data.get('polishing_stk_no') or '')
                    version_names.append(data.get('version__version_name') or '')
                    print(f"   🎯 MMC: {lot_id} -> plating: {data.get('plating_stk_no')}, polishing: {data.get('polishing_stk_no')}, version: {data.get('version__version_name')}")
                
                # Try RecoveryMasterCreation as fallback
                elif batch_id in rmc_dict:
                    data = rmc_dict[batch_id]
                    plating_stk_nos.append(data.get('plating_stk_no') or '')
                    polishing_stk_nos.append(data.get('polishing_stk_no') or '')
                    version_names.append(data.get('version__version_name') or '')
                    print(f"   🎯 RMC: {lot_id} -> plating: {data.get('plating_stk_no')}, polishing: {data.get('polishing_stk_no')}, version: {data.get('version__version_name')}")
                
                else:
                    print(f"   ❌ No data found for batch_id: {batch_id}")
                    plating_stk_nos.append('')
                    polishing_stk_nos.append('')
                    version_names.append('')
            else:
                print(f"   ❌ No batch_id found for lot_id: {lot_id}")
                plating_stk_nos.append('')
                polishing_stk_nos.append('')
                version_names.append('')
        
        # Remove empty strings and join with commas
        plating_stk_nos = [p for p in plating_stk_nos if p]
        polishing_stk_nos = [p for p in polishing_stk_nos if p]
        version_names = [v for v in version_names if v]
        
        result = {
            'plating_stk_nos': plating_stk_nos,
            'polishing_stk_nos': polishing_stk_nos,
            'version_names': ', '.join(version_names),
            'plating_stk_nos_list': plating_stk_nos,
            'polishing_stk_nos_list': polishing_stk_nos,
            'version_names_list': version_names
        }
        
        print(f"   🎉 process_new_lot_ids FINAL RESULT:")
        print(f"      plating_stk_nos: {result['plating_stk_nos']}")
        print(f"      polishing_stk_nos: {result['polishing_stk_nos']}")
        print(f"      version_names: '{result['version_names']}'")
        
        return result

    def process_model_cases_corrected(self, no_of_model_cases, lot_ids):
        """
        Process model_cases using the SAME batch_ids from lot_ids
        Updated to search both TotalStockModel and RecoveryStockModel
        """
        print(f"\n🎲 process_model_cases_corrected called with:")
        print(f"   no_of_model_cases: {no_of_model_cases}")
        print(f"   lot_ids: {lot_ids}")
        
        if not lot_ids:
            print("   ❌ No lot_ids provided")
            return {
                'model_plating_stk_nos': '',
                'model_polishing_stk_nos': '',
                'model_version_names': '',
                'model_plating_stk_nos_list': [],
                'model_polishing_stk_nos_list': [],
                'model_version_names_list': [],
                'models_data': []
            }
        
        # STEP 1: Get batch_ids from lot_ids using BOTH stock models
        print("   🔍 STEP 1: Getting batch_ids from lot_ids...")
        
        # *** UPDATED: Search both TotalStockModel and RecoveryStockModel ***
        total_stocks = TotalStockModel.objects.filter(
            lot_id__in=lot_ids
        ).select_related('batch_id')
        
        recovery_stocks = RecoveryStockModel.objects.filter(
            lot_id__in=lot_ids
        ).select_related('batch_id')
        
        print(f"   📦 Found {len(total_stocks)} TotalStockModel records")
        print(f"   📦 Found {len(recovery_stocks)} RecoveryStockModel records")
        
        # Create lot_id to batch_id mapping from both models
        lot_to_batch = {}
        batch_ids = []
        batch_to_model_type = {}  # Track which model type each batch_id comes from
        
        # Process TotalStock results first (priority)
        for stock in total_stocks:
            if stock.batch_id:
                lot_to_batch[stock.lot_id] = stock.batch_id.id
                if stock.batch_id.id not in batch_ids:
                    batch_ids.append(stock.batch_id.id)
                    batch_to_model_type[stock.batch_id.id] = 'TotalStockModel'
                print(f"   ✅ TotalStock: {stock.lot_id} -> batch_id: {stock.batch_id.id}")
        
        # Process RecoveryStock results (only if not already found in TotalStock)
        for stock in recovery_stocks:
            if stock.batch_id and stock.lot_id not in lot_to_batch:
                lot_to_batch[stock.lot_id] = stock.batch_id.id
                if stock.batch_id.id not in batch_ids:
                    batch_ids.append(stock.batch_id.id)
                    batch_to_model_type[stock.batch_id.id] = 'RecoveryStockModel'
                print(f"   ✅ RecoveryStock: {stock.lot_id} -> batch_id: {stock.batch_id.id}")
        
        # Fallback: JigCompleted → ModelMasterCreation (for Jig Loading lot IDs)
        missing_lot_ids = [lid for lid in lot_ids if lid not in lot_to_batch]
        if missing_lot_ids:
            from Jig_Loading.models import JigCompleted
            jig_completed_recs = JigCompleted.objects.filter(lot_id__in=missing_lot_ids)
            for jc in jig_completed_recs:
                if jc.batch_id:
                    mmc = ModelMasterCreation.objects.filter(batch_id=jc.batch_id).first()
                    if mmc and jc.lot_id not in lot_to_batch:
                        lot_to_batch[jc.lot_id] = mmc.id
                        if mmc.id not in batch_ids:
                            batch_ids.append(mmc.id)
                            batch_to_model_type[mmc.id] = 'JigCompleted'
                        print(f"   ✅ JigCompleted: {jc.lot_id} -> MMC batch={jc.batch_id} -> id={mmc.id}")
        
        print(f"   📋 Total unique batch_ids found: {len(batch_ids)}")
        
        # STEP 2: Get ModelMasterCreation data for all batch_ids
        print("   🔍 STEP 2: Getting ModelMasterCreation data...")
        
        models_data = []
        
        # Try ModelMasterCreation first
        mmc_results = ModelMasterCreation.objects.filter(
            id__in=batch_ids
        ).select_related('version', 'model_stock_no').values(
            'id', 'plating_stk_no', 'polishing_stk_no', 'version__version_name',
            'model_stock_no__model_no', 'plating_color'
        )
        
        mmc_dict = {mmc['id']: mmc for mmc in mmc_results}
        
        # Try RecoveryMasterCreation for missing batch_ids
        missing_batch_ids = [bid for bid in batch_ids if bid not in mmc_dict]
        rmc_dict = {}
        
        if missing_batch_ids:
            try:
                from Recovery_DP.models import RecoveryMasterCreation
                rmc_results = RecoveryMasterCreation.objects.filter(
                    id__in=missing_batch_ids
                ).select_related('version', 'model_stock_no').values(
                    'id', 'plating_stk_no', 'polishing_stk_no', 'version__version_name',
                    'model_stock_no__model_no', 'plating_color'
                )
                rmc_dict = {rmc['id']: rmc for rmc in rmc_results}
                print(f"   ✅ Found {len(rmc_dict)} RecoveryMasterCreation records")
            except ImportError:
                print("   ⚠️ RecoveryMasterCreation not available")
        
        # Process all batch_ids and collect model data
        model_plating_stk_nos = []
        model_polishing_stk_nos = []
        model_version_names = []
        
        for batch_id in batch_ids:
            # Try ModelMasterCreation first
            if batch_id in mmc_dict:
                data = mmc_dict[batch_id]
                # Use plating_stk_no as model_name so no_of_model_cases matches the API
                model_name = data.get('plating_stk_no') or data.get('model_stock_no__model_no') or ''
                plating_stk_no = data.get('plating_stk_no') or ''
                polishing_stk_no = data.get('polishing_stk_no') or ''
                version_name = data.get('version__version_name') or ''
                plating_color = data.get('plating_color') or ''
                
                if model_name:
                    models_data.append({
                        'model_name': model_name,
                        'plating_stk_no': plating_stk_no,
                        'polishing_stk_no': polishing_stk_no,
                        'version_name': version_name,
                        'plating_color': plating_color
                    })
                    
                    model_plating_stk_nos.append(plating_stk_no)
                    model_polishing_stk_nos.append(polishing_stk_no)
                    model_version_names.append(version_name)
                    
                    print(f"   🎯 MMC Model: {model_name} -> plating: {plating_stk_no}, polishing: {polishing_stk_no}, version: {version_name}")
            
            # Try RecoveryMasterCreation as fallback
            elif batch_id in rmc_dict:
                data = rmc_dict[batch_id]
                # Use plating_stk_no as model_name so no_of_model_cases matches the API
                model_name = data.get('plating_stk_no') or data.get('model_stock_no__model_no') or ''
                plating_stk_no = data.get('plating_stk_no') or ''
                polishing_stk_no = data.get('polishing_stk_no') or ''
                version_name = data.get('version__version_name') or ''
                plating_color = data.get('plating_color') or ''
                
                if model_name:
                    models_data.append({
                        'model_name': model_name,
                        'plating_stk_no': plating_stk_no,
                        'polishing_stk_no': polishing_stk_no,
                        'version_name': version_name,
                        'plating_color': plating_color
                    })
                    
                    model_plating_stk_nos.append(plating_stk_no)
                    model_polishing_stk_nos.append(polishing_stk_no)
                    model_version_names.append(version_name)
                    
                    print(f"   🎯 RMC Model: {model_name} -> plating: {plating_stk_no}, polishing: {polishing_stk_no}, version: {version_name}")
        
        # Remove empty strings and join with commas
        model_plating_stk_nos = [p for p in model_plating_stk_nos if p]
        model_polishing_stk_nos = [p for p in model_polishing_stk_nos if p]
        model_version_names = [v for v in model_version_names if v]
        
        result = {
            'model_plating_stk_nos': ', '.join(model_plating_stk_nos),
            'model_polishing_stk_nos': ', '.join(model_polishing_stk_nos),
            'model_version_names': ', '.join(model_version_names),
            'model_plating_stk_nos_list': model_plating_stk_nos,
            'model_polishing_stk_nos_list': model_polishing_stk_nos,
            'model_version_names_list': model_version_names,
            'models_data': models_data
        }
        
        print(f"   🎉 process_model_cases_corrected FINAL RESULT:")
        print(f"      model_plating_stk_nos: '{result['model_plating_stk_nos']}'")
        print(f"      model_polishing_stk_nos: '{result['model_polishing_stk_nos']}'")
        print(f"      model_version_names: '{result['model_version_names']}'")
        print(f"      models_data count: {len(result['models_data'])}")
        
        return result

    def create_enhanced_jig_detail(self, original_jig_detail, lot_ids_data, model_cases_data):
        """
        Create enhanced jig_detail with multi-lot support and existing functionality
        EXACT SAME LOGIC AS InprocessInspectionCompleteView
        """
        print(f"\n🔄 create_enhanced_jig_detail:")
        print(f"   lot_ids_data: {lot_ids_data}")
        print(f"   model_cases_data: {model_cases_data}")
        
        # Keep the original object but add new attributes
        jig_detail = original_jig_detail
        
        # Add multi-lot data - EXACT SAME AS InprocessInspectionCompleteView format
        # lot_ids_data contains lists, so we need to join them for comma-separated display
        jig_detail.lot_plating_stk_nos = lot_ids_data['plating_stk_nos']  # This is a list
        jig_detail.lot_polishing_stk_nos = lot_ids_data['polishing_stk_nos']  # This is a list
        jig_detail.lot_version_names = lot_ids_data['version_names']  # This is already comma-separated
        jig_detail.lot_plating_stk_nos_list = lot_ids_data['plating_stk_nos_list']
        jig_detail.lot_polishing_stk_nos_list = lot_ids_data['polishing_stk_nos_list']
        jig_detail.lot_version_names_list = lot_ids_data['version_names_list']
        
        # Add model_cases data (comma-separated values from no_of_model_cases)
        jig_detail.model_plating_stk_nos = model_cases_data['model_plating_stk_nos']
        jig_detail.model_polishing_stk_nos = model_cases_data['model_polishing_stk_nos']
        jig_detail.model_version_names = model_cases_data['model_version_names']
        jig_detail.model_plating_stk_nos_list = model_cases_data['model_plating_stk_nos_list']
        jig_detail.model_polishing_stk_nos_list = model_cases_data['model_polishing_stk_nos_list']
        jig_detail.model_version_names_list = model_cases_data['model_version_names_list']
        
        # Set model_presents and plating_color from models_data
        models_data = model_cases_data.get('models_data', [])
        if models_data:
            jig_detail.model_presents = ", ".join([m.get('model_name', '') for m in models_data])
            jig_detail.plating_color = models_data[0].get('plating_color', 'No Plating Color') if models_data else 'No Plating Color'
            jig_detail.no_of_model_cases = [m.get('model_name', '') for m in models_data]  # For circles display
        else:
            # If no models_data, try to extract from original no_of_model_cases (from draft_data)
            jig_detail.model_presents = "No Model Info"
            jig_detail.plating_color = "No Plating Color"
            
            # CRITICAL FIX: Parse the original no_of_model_cases from draft_data if it exists
            # This preserves model data saved during jig loading
            original_no_of_model_cases = original_jig_detail.no_of_model_cases
            if original_no_of_model_cases:
                parsed_models = self.parse_model_cases(original_no_of_model_cases)
                jig_detail.no_of_model_cases = parsed_models
                print(f"   ✅ Parsed no_of_model_cases from draft_data: {parsed_models}")
            else:
                jig_detail.no_of_model_cases = []

        # After parsing, if still empty (e.g. DB stored '[]'), fall back to plating_stock_num
        if not jig_detail.no_of_model_cases:
            _ddata = getattr(jig_detail, 'draft_data', {}) or {}
            _psn2 = getattr(jig_detail, 'plating_stock_num', None) or (_ddata.get('plating_stock_num') if _ddata else None)
            if _psn2:
                jig_detail.no_of_model_cases = [str(_psn2).strip()]
                print(f"   🔄 no_of_model_cases fallback to plating_stock_num: {jig_detail.no_of_model_cases}")

        # For single model jigs, set no_of_model_cases if model_no is available
        if not jig_detail.no_of_model_cases and hasattr(jig_detail, 'model_no') and jig_detail.model_no:
            jig_detail.no_of_model_cases = [jig_detail.model_no]
        
        # Set template attributes for Jig Unloading table
        jig_detail.jig_qr_id = jig_detail.jig_id  # For JIG ID column
        jig_detail.jig_loaded_date_time = jig_detail.IP_loaded_date_time or jig_detail.updated_at  # For Date & Time column
        jig_detail.total_cases_loaded = jig_detail.updated_lot_qty  # For Lot Qty column
        
        # Parse draft_data for bath_type and tray info
        draft_data = jig_detail.draft_data or {}
        jig_detail.ep_bath_type = draft_data.get('nickel_bath_type', 'Bright')  # For Bath Type column
        
        # FIXED: Extract actual Bath No from ForeignKey since it's not always in draft_data
        jig_detail.bath_number = (
            getattr(jig_detail.bath_numbers, 'bath_number', None)
            or draft_data.get('bath_number')
            or draft_data.get('nickel_bath_type')
            or draft_data.get('nickel_bath_number')
            or draft_data.get('bath_no')
            or 'N/A'
        )
        
        # Get tray info from draft_data first, fallback to model_cases_data if not available
        jig_detail.tray_type = draft_data.get('tray_type', None)
        jig_detail.tray_capacity = draft_data.get('tray_capacity', None)
        
        # If tray info not in draft_data, try to get from model_cases_data (model data)
        if not jig_detail.tray_type or jig_detail.tray_type == 'No Tray Type':
            if models_data and len(models_data) > 0:
                jig_detail.tray_type = models_data[0].get('tray_type', 'No Tray Type')
            else:
                jig_detail.tray_type = 'No Tray Type'
        
        if not jig_detail.tray_capacity or jig_detail.tray_capacity == 0:
            if models_data and len(models_data) > 0:
                jig_detail.tray_capacity = models_data[0].get('tray_capacity', 0)
            else:
                jig_detail.tray_capacity = 0
        
        # Fallback: Fetch plating_color from TotalStockModel if not set and models_data is empty
        if not models_data and jig_detail.plating_color == 'No Plating Color':
            try:
                tsm = TotalStockModel.objects.filter(lot_id=jig_detail.lot_id).first()
                if tsm and tsm.plating_color:
                    jig_detail.plating_color = tsm.plating_color.plating_color
                    print(f"   🔄 Plating color fallback from TotalStockModel: {jig_detail.plating_color}")
            except Exception as e:
                print(f"   ⚠️ Error fetching plating_color from TotalStockModel: {e}")
        
        # Fallback: Fetch tray info from TotalStockModel if not set
        if not jig_detail.tray_type or jig_detail.tray_type == 'No Tray Type':
            try:
                tsm = TotalStockModel.objects.filter(lot_id=jig_detail.lot_id).first()
                if tsm and tsm.batch_id:
                    mmc = tsm.batch_id
                    if mmc.model_stock_no and mmc.model_stock_no.tray_type:
                        jig_detail.tray_type = mmc.model_stock_no.tray_type.tray_type
                        jig_detail.tray_capacity = self.get_dynamic_tray_capacity(mmc.model_stock_no.tray_type.tray_type if mmc.model_stock_no.tray_type else "No Tray Type")
                        print(f"   🔄 Tray info fallback from TotalStockModel: type={jig_detail.tray_type}, capacity={jig_detail.tray_capacity}")
            except Exception as e:
                print(f"   ⚠️ Error fetching tray info from TotalStockModel: {e}")
        
        # Ensure tray_capacity is always the dynamic capacity if tray_type is set
        if jig_detail.tray_type and jig_detail.tray_type != 'No Tray Type':
            jig_detail.tray_capacity = self.get_dynamic_tray_capacity(jig_detail.tray_type)
        
        print(f"   📝 Multi-lot data assigned (as lists):")
        print(f"      lot_plating_stk_nos: {jig_detail.lot_plating_stk_nos}")
        print(f"      lot_polishing_stk_nos: {jig_detail.lot_polishing_stk_nos}")
        print(f"      lot_version_names: '{jig_detail.lot_version_names}'")

        
        print(f"   📝 Multi-model data assigned:")
        print(f"      model_plating_stk_nos: '{jig_detail.model_plating_stk_nos}'")
        print(f"      model_polishing_stk_nos: '{jig_detail.model_polishing_stk_nos}'")
        print(f"      model_version_names: '{jig_detail.model_version_names}'")
        
        # Combine both sources for final display - EXACT SAME LOGIC AS InprocessInspectionCompleteView
        # Priority: model data if available, otherwise lot data
        if jig_detail.model_plating_stk_nos:
            jig_detail.final_plating_stk_nos = jig_detail.model_plating_stk_nos
        else:
            # Convert lot data list to comma-separated string like InprocessInspectionCompleteView
            jig_detail.final_plating_stk_nos = ', '.join(jig_detail.lot_plating_stk_nos) if jig_detail.lot_plating_stk_nos else ''
            
        if jig_detail.model_polishing_stk_nos:
            jig_detail.final_polishing_stk_nos = jig_detail.model_polishing_stk_nos
        else:
            # Convert lot data list to comma-separated string like InprocessInspectionCompleteView
            jig_detail.final_polishing_stk_nos = ', '.join(jig_detail.lot_polishing_stk_nos) if jig_detail.lot_polishing_stk_nos else ''
            
        if jig_detail.model_version_names:
            jig_detail.final_version_names = jig_detail.model_version_names
        else:
            # lot_version_names is already comma-separated from InprocessInspectionCompleteView logic
            jig_detail.final_version_names = jig_detail.lot_version_names if jig_detail.lot_version_names else ''
        
        print(f"   🎯 Final combined data (model takes priority):")
        print(f"      final_plating_stk_nos: '{jig_detail.final_plating_stk_nos}'")
        print(f"      final_polishing_stk_nos: '{jig_detail.final_polishing_stk_nos}'")
        print(f"      final_version_names: '{jig_detail.final_version_names}'")
        
        # Calculate total_quantity from lot_id_quantities - CRITICAL for Lot Qty display
        if hasattr(jig_detail, 'lot_id_quantities') and jig_detail.lot_id_quantities:
            jig_detail.total_quantity = sum(jig_detail.lot_id_quantities.values())
            print(f"      total_quantity: {jig_detail.total_quantity}")
        else:
            # For completed jigs, use updated_lot_qty or loaded_cases_qty as fallback
            jig_detail.total_quantity = getattr(jig_detail, 'updated_lot_qty', 0) or getattr(jig_detail, 'loaded_cases_qty', 0) or 0
            print(f"      total_quantity: {jig_detail.total_quantity} (from jig fields - no lot_id_quantities)")
        
        # Add indicators for template logic
        jig_detail.has_multiple_lots = bool(jig_detail.lot_plating_stk_nos)
        jig_detail.has_multiple_models = bool(model_cases_data['model_plating_stk_nos'])
        
        print(f"   🚩 Indicators:")
        print(f"      has_multiple_lots: {jig_detail.has_multiple_lots}")
        print(f"      has_multiple_models: {jig_detail.has_multiple_models}")
        
        # Apply existing Jig Unloading Zone 2 logic for single model data
        self.apply_existing_logic(jig_detail)
        
        return jig_detail

    def parse_model_cases(self, no_of_model_cases):
        """
        Parse no_of_model_cases field to extract model names
        """
        if not no_of_model_cases:
            return []
        
        # If it's already a list, return it
        if isinstance(no_of_model_cases, list):
            return no_of_model_cases
        
        # If it's a string, try to parse it
        if isinstance(no_of_model_cases, str):
            # Try to parse as JSON first
            try:
                import json
                parsed = json.loads(no_of_model_cases)
                if isinstance(parsed, list):
                    return parsed
            except:
                pass

            # Raw format written by Jig_Loading.JigSaveAPI for multi-model jigs:
            # 'MODEL(lot_id):qty | MODEL2(lot_id):qty2'. Legacy format: 'MODEL:qty,MODEL2:qty2'.
            # Split on ' | ' when present so multi-model jigs aren't collapsed into one
            # entry (no comma exists in the pipe format). No dedup: two entries sharing
            # the same Plating Stk No (ditto models) must stay separate so Model Presents
            # renders one circle per entry.
            _parts = no_of_model_cases.split(' | ') if ' | ' in no_of_model_cases else no_of_model_cases.split(',')
            return [
                cleaned
                for cleaned in (
                    re.split(r'[\(\[]', part, maxsplit=1)[0].split(':')[0].strip()
                    for part in _parts
                )
                if cleaned
            ]
        
        # If it's a single value, return as list
        return [str(no_of_model_cases)]

    def apply_existing_logic(self, jig_detail):
        """
        Apply existing Jig Unloading Zone 2 logic for single model data
        This preserves the original behavior while adding multi-lot support
        """
        # For single model jigs, ensure model_presents is set correctly
        if not hasattr(jig_detail, 'model_presents') or not jig_detail.model_presents or jig_detail.model_presents == "No Model Info":
            if hasattr(jig_detail, 'no_of_model_cases') and jig_detail.no_of_model_cases:
                if isinstance(jig_detail.no_of_model_cases, list):
                    jig_detail.model_presents = ", ".join(jig_detail.no_of_model_cases)
                else:
                    jig_detail.model_presents = str(jig_detail.no_of_model_cases)
            elif hasattr(jig_detail, 'model_no') and jig_detail.model_no:
                jig_detail.model_presents = jig_detail.model_no
        
        # Ensure plating_color is set from draft_data if available
        if not hasattr(jig_detail, 'plating_color') or not jig_detail.plating_color or jig_detail.plating_color == "No Plating Color":
            draft_data = getattr(jig_detail, 'draft_data', {}) or {}
            if draft_data.get('plating_color'):
                jig_detail.plating_color = draft_data.get('plating_color')
        
        # Set template attributes that the Zone 2 template expects
        jig_detail.jig_qr_id = getattr(jig_detail, 'jig_id', '')
        jig_detail.jig_loaded_date_time = getattr(jig_detail, 'IP_loaded_date_time', None) or getattr(jig_detail, 'updated_at', None)
        jig_detail.total_cases_loaded = getattr(jig_detail, 'updated_lot_qty', 0)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)

        # Zone 2: colors flagged for Jig Unload Zone 2 in Model Master
        allowed_colors = Plating_Color.objects.filter(
            jig_unload_zone_2=True
        ).values_list('plating_color', flat=True)
        
        print(f"🔍 Zone 2 - Allowed colors: {list(allowed_colors)}")

        # Get all plating colors and strip "IP-" prefix from stored values for matching
        allowed_colors_list = list(allowed_colors)
        
        # Build list of patterns to match (handle both "IP-GUN" and "GUN" formats)
        plating_patterns = allowed_colors_list + [f"IP-{color}" for color in allowed_colors_list]
        print(f"🔍 Zone 2 - Full plating patterns: {plating_patterns}")
        
        # Create polish_finish subquery for annotation
        polish_finish_subquery = TotalStockModel.objects.filter(
            lot_id=OuterRef('lot_id')
        ).values('polish_finish__polish_finish')[:1]
        
        jig_unload = JigCompleted.objects.select_related('bath_numbers', 'unload_hold_by', 'unload_release_by').annotate(
            plating_color_cast=KeyTextTransform('plating_color', 'draft_data'),
            polish_finish_name=Subquery(polish_finish_subquery)
        ).filter(
            plating_color_cast__in=plating_patterns
        ).filter(
            Q(last_process_module='Inprocess Inspection') |
            Q(last_process_module='Jig Unloading')
        ).order_by('-IP_loaded_date_time')

        # ENHANCED FILTER: Also get jigs where plating_color is not in draft_data
        # but can be determined from TotalStockModel or RecoveryStockModel
        jigs_without_plating_in_draft = JigCompleted.objects.select_related('bath_numbers', 'unload_hold_by', 'unload_release_by').annotate(
            plating_color_cast=KeyTextTransform('plating_color', 'draft_data'),
            polish_finish_name=Subquery(polish_finish_subquery)
        ).filter(
            plating_color_cast__isnull=True,  # draft_data has no plating_color
        ).filter(
            Q(last_process_module='Inprocess Inspection') |
            Q(last_process_module='Jig Unloading')
        ).order_by('-IP_loaded_date_time')
        
        # Get lot_ids from jigs without plating color in draft_data
        lot_ids_without_plating = list(jigs_without_plating_in_draft.values_list('lot_id', flat=True))
        
        if lot_ids_without_plating:
            print(f"🔍 Zone 2 - Found {len(lot_ids_without_plating)} jigs without plating_color in draft_data")
            
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
            valid_lot_ids_for_zone2 = set()
            for lot_id, color in total_stock_colors:
                if color in allowed_colors_list:
                    valid_lot_ids_for_zone2.add(lot_id)
                    print(f"✅ Zone 2 - Lot {lot_id} has valid color {color} from TotalStock")
                    
            for lot_id, color in recovery_stock_colors:
                if color in allowed_colors_list:
                    valid_lot_ids_for_zone2.add(lot_id)
                    print(f"✅ Zone 2 - Lot {lot_id} has valid color {color} from Recovery")

            batch_ids_to_check = []
            jig_lot_to_batch = {}
            for jig in jigs_without_plating_in_draft:
                if jig.lot_id not in valid_lot_ids_for_zone2:
                    batch_id_str = (jig.draft_data or {}).get('batch_id') or getattr(jig, 'batch_id', None)
                    if batch_id_str:
                        batch_ids_to_check.append(batch_id_str)
                        jig_lot_to_batch[jig.lot_id] = batch_id_str

            if batch_ids_to_check:
                zone2_batch_ids = set(
                    TotalStockModel.objects.filter(
                        batch_id__batch_id__in=batch_ids_to_check,
                        plating_color__plating_color__in=allowed_colors_list
                    ).values_list('batch_id__batch_id', flat=True)
                )
                zone2_batch_ids.update(
                    ModelMasterCreation.objects.filter(
                        batch_id__in=batch_ids_to_check,
                        plating_color__in=allowed_colors_list
                    ).values_list('batch_id', flat=True)
                )
                for jig_lot_id, batch_id_str in jig_lot_to_batch.items():
                    if batch_id_str in zone2_batch_ids:
                        valid_lot_ids_for_zone2.add(jig_lot_id)
                        print(f"✅ Zone 2 JigCompleted fallback: {jig_lot_id} -> {batch_id_str}")
            
            # Get additional jigs that match by lot_id even if draft_data lacks plating_color
            additional_jigs = jigs_without_plating_in_draft.filter(lot_id__in=valid_lot_ids_for_zone2)
            
            # Combine both sets: jigs with plating_color in draft_data + jigs with valid colors from TotalStock/Recovery
            jig_unload = jig_unload.union(additional_jigs, all=False)
            
            print(f"✅ Zone 2 - Added {additional_jigs.count()} jigs with plating color from TotalStock/Recovery")
        
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
                    # and legacy 'MODEL1 [LID...]:QTY, MODEL2 [LID...]:QTY'.
                    for _item in _raw_mc.replace('|', ',').split(','):
                        _mn = re.split(r'[\(\[]', _item, maxsplit=1)[0].split(':')[0].strip()
                        if _mn:
                            all_model_numbers.add(_mn)
            # Always collect plating_stock_num (handles comma-separated multi-model)
            if getattr(jig_detail, 'plating_stock_num', None):
                for _psn in str(jig_detail.plating_stock_num).split(','):
                    _psn = _psn.strip()
                    if _psn:
                        all_model_numbers.add(_psn)
            # 🔥 NEW: Collect all lot_ids for dual-table lookup
            if hasattr(jig_detail, 'lot_id_quantities') and jig_detail.lot_id_quantities:
                all_lot_ids.update(jig_detail.lot_id_quantities.keys())
            # Collect batch_ids for Jig Loading image fallback path
            _jd_batch = getattr(jig_detail, 'batch_id', None)
            if _jd_batch:
                all_batch_ids.add(str(_jd_batch).strip())

        # ✅ Type of Input (Fresh/Recovery): bulk-resolve via TotalStockModel.lot_id → batch_id.upload_type
        # for every lot_id referenced, plus a batch_id-keyed fallback for Jig Loading-origin lots.
        type_of_input_map = get_type_of_input_map(list(all_lot_ids))
        batch_type_of_input_map = {}
        if all_batch_ids:
            for _row in ModelMasterCreation.objects.filter(
                batch_id__in=list(all_batch_ids)
            ).values('batch_id', 'upload_type'):
                batch_type_of_input_map[_row['batch_id']] = label_for_upload_type(_row['upload_type'])

        # DB-persisted, Plating Stk No-unique colors (see modelmasterapp.color_service)
        # so Model Presents circles match Inprocess Inspection / Jig Unloading.
        sorted_model_numbers = sorted(str(m) for m in all_model_numbers)
        global_model_colors = get_model_colors_by_model_no(sorted_model_numbers)
        print(f"🎨 Resolved color mapping for models: {global_model_colors}")
        
        # 🚀 ENHANCED: Dual-table model data fetching (TotalStock + Recovery)
        model_data_map = {}
        model_images_map = {}
        
        if all_model_numbers:
            # First, try ModelMasterCreation (linked to TotalStockModel)
            model_master_creations = ModelMasterCreation.objects.filter(
                model_stock_no__model_no__in=all_model_numbers
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
            missing_model_numbers = all_model_numbers - found_model_numbers
            
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

            # ✅ FIX: Create clean model mapping for image lookup (like Zone 1 and Inprocess Inspection)
            print(f"🔍 DEBUG: Zone 2 Looking up images for models: {all_model_numbers}")
            
            clean_model_mapping = {}
            for model_no in all_model_numbers:
                clean_model_no = model_no
                # Extract just the numeric part (e.g., "1805NAD02" -> "1805")
                match = re.match(r'^(\d+)', str(model_no))
                if match:
                    clean_model_no = match.group(1)
                clean_model_mapping[model_no] = clean_model_no
                print(f"🔍 Zone 2 Model mapping: {model_no} -> {clean_model_no}")
            
            # Get unique clean model numbers for lookup
            clean_model_numbers = set(clean_model_mapping.values())
            
            model_masters = ModelMaster.objects.filter(
                model_no__in=clean_model_numbers
            ).prefetch_related('images').order_by('model_no', 'plating_stk_no')

            # Create lookup maps: clean_model_images by numeric key, plating_stk_images by exact match
            clean_model_images = {}
            plating_stk_images = {}
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
                    clean_model_images[mn] = {'images': img_urls_mm, 'first_image': img_urls_mm[0]}

            # Map back to original model numbers
            for original_model, clean_model in clean_model_mapping.items():
                # First: exact plating_stk_no match (most specific)
                if original_model in plating_stk_images:
                    model_images_map[original_model] = plating_stk_images[original_model]
                    print(f"📸 Zone 2 Mapped {original_model} via exact plating_stk_no -> {len(plating_stk_images[original_model]['images'])} images")
                elif clean_model in clean_model_images:
                    model_images_map[original_model] = clean_model_images[clean_model]
                    print(f"📸 Zone 2 Mapped {original_model} -> {clean_model} -> {len(clean_model_images[clean_model]['images'])} images")
                else:
                    model_images_map[original_model] = {'images': [], 'first_image': None}
                    print(f"❌ Zone 2 No images found for {original_model} (clean: {clean_model})")

            # Fallback: for models still missing images, try ModelMasterCreation.images + plating_stk_no scan
            missing_images = {m for m, v in model_images_map.items() if not v.get('images')}
            if missing_images:
                print(f"🔍 Zone 2: Searching plating_stk_no variants for models without images: {missing_images}")
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
                            print(f"📸 Zone 2 MMC direct images: '{orig_no}' -> {len(_mmc_urls)} images")
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
                            if (mm.plating_stk_no == orig_no or
                                    (_numeric_no and str(mm.plating_stk_no).startswith(_numeric_no))) and mm.images.exists():
                                img_list = sort_images_front_first(mm.images.all())
                                img_urls_fb = [img.master_image.url for img in img_list if img.master_image]
                                if img_urls_fb:
                                    model_images_map[orig_no] = {'images': img_urls_fb, 'first_image': img_urls_fb[0]}
                                    missing_images.discard(orig_no)
                                    print(f"📸 Zone 2 Fallback: mapped '{orig_no}' via plating_stk_no '{mm.plating_stk_no}'")
                                    break

        # Build batch_id → ModelMasterCreation → images/tray/polish map for Jig Loading lots
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
                from modelmasterapp.image_utils import sort_images_front_first
                _bimgs = [img.master_image.url for img in sort_images_front_first(_bmmc.images.all()) if img.master_image]
                if not _bimgs and _bmmc.model_stock_no:
                    _bimgs = [img.master_image.url for img in sort_images_front_first(_bmmc.model_stock_no.images.all()) if img.master_image]
                if _bimgs:
                    batch_images_map[_bmmc.batch_id] = {'images': _bimgs, 'first_image': _bimgs[0]}
                    print(f"📦 Zone 2 batch_images: {_bmmc.batch_id} -> {len(_bimgs)} images")

        # 🚀 ENHANCED: Dual-table lot_id to model mapping (using full plating_stk_no)
        def build_lot_id_model_map(jig_detail):
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
                    
                    if not plating_stk_no:
                        # JigCompleted fallback (for Jig Loading lot IDs not in TotalStockModel)
                        jc = JigCompleted.objects.filter(lot_id=lot_id).first()
                        if jc and jc.batch_id:
                            mmc = ModelMasterCreation.objects.filter(batch_id=jc.batch_id).select_related('model_stock_no').first()
                            if mmc:
                                plating_stk_no = getattr(mmc, 'plating_stk_no', None) or (mmc.model_stock_no.model_no if mmc.model_stock_no else None)
                                print(f"🎯 Found {lot_id} in JigCompleted -> MMC -> {plating_stk_no}")
                    
                    lot_id_model_map[lot_id] = plating_stk_no
            
            print(f"DEBUG: {jig_detail.lot_id} lot_id_model_map = {lot_id_model_map}")
            jig_detail.lot_id_model_map = lot_id_model_map
        
        # Process each JigCompleted to handle multiple models and lots - SAME AS InprocessInspectionCompleteView
        processed_jig_details = []
        
        for idx, jig_detail in enumerate(jig_unload):
            print(f"\n{'='*50}")
            print(f"🔧 Processing JigDetail #{idx+1} (ID: {jig_detail.id})")
            print(f"   lot_id: {jig_detail.lot_id}")
            
            # Get multiple lot_ids exactly like InprocessInspectionCompleteView
            multiple_lot_ids = self.get_multiple_lot_ids(jig_detail)
            print(f"   multiple_lot_ids found: {multiple_lot_ids}")
            print(f"   no_of_model_cases: {jig_detail.no_of_model_cases}")
            
            # Process multiple lot_ids to get comma-separated field values (SAME AS InprocessInspectionCompleteView)
            lot_ids_data = self.process_new_lot_ids(multiple_lot_ids)
            
            # Process model_cases using THE SAME batch_ids from lot_ids (CORRECTED LOGIC)
            model_cases_data = self.process_model_cases_corrected(jig_detail.no_of_model_cases, multiple_lot_ids)
            
            # Extract lot_id_quantities - CORRECTED PRIORITY ORDER
            draft_data = getattr(jig_detail, 'draft_data', {}) or {}
            
            # ✅ FIX err 4: Use JigCompleted fields (Jig Loading's updated qty) as primary source
            # Priority: JigCompleted.updated_lot_qty (set by Jig Loading) → loaded_cases_qty → draft_data
            if jig_detail.updated_lot_qty and jig_detail.updated_lot_qty > 0:
                # Use Jig Loading's updated qty (respects jig capacity constraints)
                jig_detail.lot_id_quantities = {jig_detail.lot_id: jig_detail.updated_lot_qty}
                print(f"   📦 Using JigCompleted.updated_lot_qty: {jig_detail.updated_lot_qty} (Jig Loading qty)")
            elif jig_detail.loaded_cases_qty and jig_detail.loaded_cases_qty > 0:
                # Fallback to loaded_cases_qty
                jig_detail.lot_id_quantities = {jig_detail.lot_id: jig_detail.loaded_cases_qty}
                print(f"   📦 Using JigCompleted.loaded_cases_qty: {jig_detail.loaded_cases_qty}")
            else:
                # Final fallback to draft_data or original_lot_qty
                jig_detail.lot_id_quantities = draft_data.get('lot_id_quantities', {jig_detail.lot_id: jig_detail.original_lot_qty or 0})
                print(f"   📦 Using draft_data lot_id_quantities: {jig_detail.lot_id_quantities}")
            
            jig_detail.lot_id_list = list(jig_detail.lot_id_quantities.keys())
            
            # Create enhanced jig_detail with multi-lot support
            enhanced_jig_detail = self.create_enhanced_jig_detail(jig_detail, lot_ids_data, model_cases_data)
            
            processed_jig_details.append(enhanced_jig_detail)
            
            print(f"✅ Final Results for JigDetail #{idx+1}:")
            print(f"   final_plating_stk_nos: {enhanced_jig_detail.final_plating_stk_nos}")
            print(f"   final_polishing_stk_nos: {enhanced_jig_detail.final_polishing_stk_nos}")
            print(f"   final_version_names: {enhanced_jig_detail.final_version_names}")
        
        # Replace the original jig_unload with processed_jig_details
        jig_unload = processed_jig_details

        # ── Second pass: fill gaps that create_enhanced_jig_detail may have left ──
        for jig_detail in jig_unload:
            draft_data = {}
            if hasattr(jig_detail, 'draft_data') and jig_detail.draft_data:
                if isinstance(jig_detail.draft_data, str):
                    try:
                        draft_data = json.loads(jig_detail.draft_data)
                    except Exception:
                        draft_data = {}
                elif isinstance(jig_detail.draft_data, dict):
                    draft_data = jig_detail.draft_data

            # Ensure lot_id_quantities is set
            lot_id_quantities = getattr(jig_detail, 'lot_id_quantities', None) or draft_data.get('lot_id_quantities', {})
            if not lot_id_quantities:
                delink_qty = getattr(jig_detail, 'delink_tray_qty', 0)
                if delink_qty and delink_qty > 0:
                    lot_id_quantities = {jig_detail.lot_id: delink_qty}
                else:
                    lot_id_quantities = {jig_detail.lot_id: getattr(jig_detail, 'updated_lot_qty', 0)}
            jig_detail.lot_id_quantities = lot_id_quantities

            # Type of Input (Fresh/Recovery): prefer lot_id_quantities keys resolved via TotalStockModel,
            # fall back to batch_id-keyed lookup for Jig Loading-origin lots.
            jig_detail.type_of_input = 'Fresh'
            for _jd_lid in lot_id_quantities.keys():
                if _jd_lid in type_of_input_map:
                    jig_detail.type_of_input = type_of_input_map[_jd_lid]
                    break
            else:
                _jd_batch_id = getattr(jig_detail, 'batch_id', None)
                if _jd_batch_id and _jd_batch_id in batch_type_of_input_map:
                    jig_detail.type_of_input = batch_type_of_input_map[_jd_batch_id]

            # Rebuild lot_id_model_map from plating_stock_num if not already set
            if not getattr(jig_detail, 'lot_id_model_map', None) and lot_id_quantities:
                _psn = getattr(jig_detail, 'plating_stock_num', None) or draft_data.get('plating_stock_num')
                if _psn:
                    _rebuilt_map = {jig_detail.lot_id: str(_psn).strip()}
                    for _k in lot_id_quantities.keys():
                        _rebuilt_map.setdefault(_k, str(_psn).strip())
                    jig_detail.lot_id_model_map = _rebuilt_map

            # Ensure no_of_model_cases is a proper list (handle '', '[]', 'stk:qty,...')
            model_cases = draft_data.get('no_of_model_cases', getattr(jig_detail, 'no_of_model_cases', None))
            if not model_cases:
                _plating_sn = getattr(jig_detail, 'plating_stock_num', None) or draft_data.get('plating_stock_num')
                if _plating_sn:
                    model_cases = [str(_plating_sn).strip()]
                else:
                    model_cases = []
            if isinstance(model_cases, str):
                try:
                    _parsed = json.loads(model_cases)
                    jig_detail.no_of_model_cases = _parsed if isinstance(_parsed, list) else ([str(_parsed)] if _parsed else [])
                except Exception:
                    # Raw format written by Jig_Loading.JigSaveAPI for multi-model jigs:
                    # 'MODEL(lot_id):qty | MODEL2(lot_id):qty2'. Split on ' | ' when present
                    # so multi-model jigs aren't collapsed into one entry; no dedup, so
                    # ditto models (same Plating Stk No twice) stay as two entries.
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
                    _fb_psn = getattr(jig_detail, 'plating_stock_num', None) or draft_data.get('plating_stock_num', '')
                    jig_detail.lot_plating_stk_nos = str(_fb_psn).strip() if _fb_psn else ''

            # Re-populate model_images / model_colors if first pass left them empty
            if jig_detail.no_of_model_cases and not getattr(jig_detail, 'model_images', None):
                _model_imgs = {}
                _model_clrs = {}
                # Collect plating_stk_nos from lot_id_model_map (e.g. '1805NAK02')
                _limp_sp = getattr(jig_detail, 'lot_id_model_map', {}) or {}
                _psn_set_sp = set(v for v in _limp_sp.values() if v)
                for _mn in jig_detail.no_of_model_cases:
                    _mk = str(_mn)
                    _model_clrs[_mk] = global_model_colors.get(_mk, '#cccccc')
                    _img_data = model_images_map.get(_mk, {'images': [], 'first_image': None})
                    if not _img_data.get('images'):
                        # Try plating_stk_no from lot_id_model_map first (e.g. '1805NAK02')
                        for _psn_sp in _psn_set_sp:
                            _psn_img = model_images_map.get(_psn_sp)
                            if _psn_img and _psn_img.get('images'):
                                _img_data = _psn_img
                                break
                    if not _img_data.get('images'):
                        # Numeric prefix match in model_images_map (e.g. '1805' matches '1805NAK02')
                        for _map_k, _map_v in model_images_map.items():
                            if str(_map_k).startswith(_mk) and _map_v.get('images'):
                                _img_data = _map_v
                                break
                    if not _img_data.get('images'):
                        _batch_img = batch_images_map.get(getattr(jig_detail, 'batch_id', None))
                        if _batch_img:
                            _img_data = _batch_img
                    _model_imgs[_mk] = _img_data
                # Also index by plating_stk_no so JS lookup via lot_id_model_map succeeds
                for _psn_sp in _psn_set_sp:
                    if _psn_sp not in _model_imgs:
                        _psn_img = model_images_map.get(_psn_sp, {'images': [], 'first_image': None})
                        if not _psn_img.get('images') and _model_imgs:
                            _psn_img = list(_model_imgs.values())[0]
                        _model_imgs[_psn_sp] = _psn_img
                jig_detail.model_images = _model_imgs
                jig_detail.model_colors = _model_clrs
                # Fill model_data (versions/tray info) with numeric prefix fallback
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

            # Fix tray_type / tray_capacity if still missing or 'No Tray Type'
            _jd_batch_sp = getattr(jig_detail, 'batch_id', None)
            if not getattr(jig_detail, 'tray_type', None) or jig_detail.tray_type in (None, 'No Tray Type'):
                if _jd_batch_sp and _jd_batch_sp in batch_tray_map:
                    _tt_sp, _tc_sp = batch_tray_map[_jd_batch_sp]
                    jig_detail.tray_type = _tt_sp
                    jig_detail.tray_capacity = self.get_dynamic_tray_capacity(_tt_sp)
            # Fix polish_finish_name if NULL (Jig Loading lots not in TotalStockModel)
            if not getattr(jig_detail, 'polish_finish_name', None):
                if _jd_batch_sp and _jd_batch_sp in batch_polish_finish_map:
                    jig_detail.polish_finish_name = batch_polish_finish_map[_jd_batch_sp]

            # Explicitly resolve bath_number string by FK ID (union() loses select_related)
            _bath_fk_id_sp = getattr(jig_detail, 'bath_numbers_id', None)
            if _bath_fk_id_sp:
                _bn_row_sp = BathNumbers.objects.filter(id=_bath_fk_id_sp).values('bath_number').first()
                jig_detail.bath_number = _bn_row_sp['bath_number'] if _bn_row_sp else None
            elif not getattr(jig_detail, 'bath_number', None) or jig_detail.bath_number == 'N/A':
                _dd_sp = getattr(jig_detail, 'draft_data', {}) or {}
                _bn_sp = _dd_sp.get('bath_number') or _dd_sp.get('nickel_bath_number') or _dd_sp.get('bath_no')
                jig_detail.bath_number = str(_bn_sp) if _bn_sp else None

            # Fallback: if bath_number still None, try to find from another JigCompleted with same jig_id
            if not getattr(jig_detail, 'bath_number', None) or jig_detail.bath_number == 'N/A':
                _jig_id_sp = getattr(jig_detail, 'jig_id', None)
                if _jig_id_sp:
                    _sibling_jc_sp = JigCompleted.objects.filter(
                        jig_id=_jig_id_sp, bath_numbers__isnull=False
                    ).values('bath_numbers__bath_number').first()
                    if _sibling_jc_sp:
                        jig_detail.bath_number = _sibling_jc_sp['bath_numbers__bath_number']

            # Ensure list fields exist
            for _fld in ['all_versions', 'all_vendors', 'all_locations', 'all_plating_stk_nos', 'all_polishing_stk_nos', 'all_plating_colors']:
                if not hasattr(jig_detail, _fld) or getattr(jig_detail, _fld) is None:
                    setattr(jig_detail, _fld, [])

            # Populate all_polishing_stk_nos from enhanced data if still empty
            if not jig_detail.all_polishing_stk_nos:
                if getattr(jig_detail, 'lot_polishing_stk_nos_list', None):
                    jig_detail.all_polishing_stk_nos = list(jig_detail.lot_polishing_stk_nos_list)
                elif getattr(jig_detail, 'lot_polishing_stk_nos', None):
                    jig_detail.all_polishing_stk_nos = list(jig_detail.lot_polishing_stk_nos)
                elif getattr(jig_detail, 'final_polishing_stk_nos', None):
                    jig_detail.all_polishing_stk_nos = [jig_detail.final_polishing_stk_nos]

            # Populate all_plating_stk_nos from enhanced data if still empty
            if not jig_detail.all_plating_stk_nos:
                if getattr(jig_detail, 'lot_plating_stk_nos_list', None):
                    jig_detail.all_plating_stk_nos = list(jig_detail.lot_plating_stk_nos_list)
                elif getattr(jig_detail, 'lot_plating_stk_nos', None):
                    jig_detail.all_plating_stk_nos = list(jig_detail.lot_plating_stk_nos)
                elif getattr(jig_detail, 'final_plating_stk_nos', None):
                    jig_detail.all_plating_stk_nos = [jig_detail.final_plating_stk_nos]

            # Guarantee unique_* fields are always initialized (template requires them)
            for _ufld in ['unique_versions', 'unique_vendors', 'unique_locations', 'unique_tray_types',
                          'unique_plating_stk_nos', 'unique_polishing_stk_nos']:
                if not getattr(jig_detail, _ufld, None):
                    _all = getattr(jig_detail, _ufld.replace('unique_', 'all_', 1), [])
                    setattr(jig_detail, _ufld, sorted(list(set(_all))) if _all else [])

        # This converts the QuerySet to a list, so it MUST be last
        jig_unload = self.check_draft_status_for_jigs(jig_unload)
        
        # Add pagination with consistent logic as Inprocess Inspection
        page_number = self.request
        paginator = Paginator(jig_unload, 10)  # 10 items per page like Inprocess Inspection
        page_obj = paginator.get_page(page_number)
        
        print(f"\n📄 Jig Unloading Zone 2 Main - Pagination: Page {page_number}, Total items: {len(jig_unload)}")
        print(f"📄 Current page items: {len(page_obj.object_list)}")
        print(f"📄 Total pages: {paginator.num_pages}")
        
        context['jig_unload'] = page_obj  # For table data
        context['page_obj'] = page_obj    # For pagination controls (template uses this name)
        context['bath_numbers'] = bath_numbers
        
        return context
    
    def _get_zone2_jig_lot_quantities(self, jig):
        """Return all model lot_ids represented by a Zone 2 jig row."""
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

        # ✅ FAST PATH: jigs flagged as fully unloaded — Zone 2's JigCompleted uses
        # last_process_module='Jig Unloading' (it does NOT have an unload_over field).
        completed_lot_ids = set(
            JigCompleted.objects.filter(last_process_module='Jig Unloading')
            .values_list('lot_id', flat=True)
        )
        print(f"[ZONE2 FILTER] Fast-path lot_ids with last_process_module='Jig Unloading': {len(completed_lot_ids)}")

        # Get all unload records once
        unload_records = JigUnloadAfterTable.objects.filter(
            combine_lot_ids__isnull=False
        ).exclude(combine_lot_ids__exact=[]).values('combine_lot_ids')
        
        # Build unload mapping: jig_lot_id -> set of unloaded lot_ids
        # Also collect bare lot_ids (stored when jig_lot_id was empty: "-LIDxxx" or plain "LIDxxx")
        unload_map = {}
        bare_unloaded_lot_ids = set()  # lot_ids with no valid jig_id prefix
        all_submitted_lot_ids = set()
        for record in unload_records:
            if record['combine_lot_ids']:
                for combined_id in record['combine_lot_ids']:
                    actual_lot_id = _zone2_extract_lot_id(combined_id)
                    if actual_lot_id:
                        all_submitted_lot_ids.add(actual_lot_id)
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

        final_submissions = list(
            JUSubmittedZ1.objects.filter(is_draft=False).only(
                'id', 'jig_completed_id', 'jig_qr_id', 'model_no', 'lot_id', 'total_qty', 'tray_data'
            )
        )
        submitted_signature_keys = set()
        for submission in final_submissions:
            metadata = _zone2_source_metadata_from_tray_data(submission.tray_data)
            if isinstance(metadata, dict):
                for source in metadata.get('source_mappings', []):
                    if isinstance(source, dict):
                        source_lot_id = str(source.get('lot_id') or '').strip()
                        if source_lot_id:
                            all_submitted_lot_ids.add(source_lot_id)

            if submission.lot_id in all_submitted_lot_ids:
                signature = _zone2_submission_tray_signature(submission.tray_data)
                if signature:
                    submitted_signature_keys.add((submission.model_no, signature))

        if submitted_signature_keys:
            for submission in final_submissions:
                signature = _zone2_submission_tray_signature(submission.tray_data)
                if signature and (submission.model_no, signature) in submitted_signature_keys:
                    all_submitted_lot_ids.add(submission.lot_id)

        print(f"[ZONE2 FILTER] Submitted source lot_ids to hide: {len(all_submitted_lot_ids)}")

        # ------------------------------------------------------------------
        # DRAFT VISIBILITY OVERRIDE (DISPLAY ONLY)
        # ------------------------------------------------------------------
        # Zone 2 filters this queryset BEFORE check_draft_status_for_jigs()
        # runs.  Therefore a jig that still has an active unload draft can be
        # removed by the normal "fully unloaded" checks before the UI gets
        # a chance to mark it as Draft.
        #
        # Keep the existing loaded/already-loaded/submission concepts intact.
        # This set is used ONLY to prevent an active draft row from disappearing
        # from the Zone 2 pick table.  Final-completed lots are still handled by
        # the existing completion checks below.
        draft_jig_completed_ids = set(
            JUSubmittedZ1.objects.filter(is_draft=True)
            .values_list('jig_completed_id', flat=True)
        )
        draft_lot_ids = set(
            JigUnloadDraft.objects.values_list('main_lot_id', flat=True)
        )
        print(
            f"[ZONE2 FILTER] Active draft JigCompleted IDs: {len(draft_jig_completed_ids)}; "
            f"legacy draft lot_ids: {len(draft_lot_ids)}"
        )
        
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
        print(f"[ZONE2 FILTER] Secondary multi-model lot_ids to hide: {len(secondary_lot_ids)}")

        # Filter jigs
        filtered_jigs = []
        for jig in queryset:
            # ✅ SECONDARY LOT CHECK: hide lots that are secondary models in a
            # multi-model Jig Loading submission (already shown inside primary row).
            if jig.lot_id in secondary_lot_ids:
                print(f"🚫 [ZONE2 SECONDARY LOT FILTER] Hiding secondary multi-model lot: {jig.lot_id}")
                continue

            _jfq = self._get_zone2_jig_lot_quantities(jig)
            if not _jfq:
                # Fallback to base lot_id if lot_id_quantities isn't present in draft_data
                if getattr(jig, 'lot_id', None):
                    _jfq = {jig.lot_id: getattr(jig, 'updated_lot_qty', 1)}
                else:
                    # Truly no lot data — keep the jig visible
                    filtered_jigs.append(jig)
                    continue

            jig_lot_ids = set(_jfq.keys())

            # ✅ FAST PATH: single-model jigs can be hidden by their completion flag.
            # Multi-model jigs must stay visible until every model lot_id is submitted.
            # Scoped to this record's own lot_id — jig hardware IDs (jig_id) are reused
            # across lots/cycles, so matching on jig_id alone would hide a brand-new
            # pending lot just because an unrelated earlier lot shared the same jig.
            if len(jig_lot_ids) <= 1 and jig.lot_id in completed_lot_ids:
                print(f"🚫 [ZONE2 FAST PATH] Hiding completed single-model jig: {jig.lot_id}")
                continue

            if jig_lot_ids and jig_lot_ids.issubset(completed_lot_ids):
                print(f"🚫 [ZONE2 FAST PATH] Hiding - all lot_ids unloaded: {jig.lot_id}")
                continue

            # IMPORTANT: the row is already past the final-completion checks.
            # If an unload draft belongs to this JigCompleted (or to one of the
            # lot_ids represented by this row), keep the row visible so the user
            # can resume it.  This does NOT change is_already_loaded_z1,
            # is_merged_additional, Add Model, or any final-submission behavior.
            has_active_draft = (
                getattr(jig, 'id', None) in draft_jig_completed_ids
                or bool(jig_lot_ids & draft_lot_ids)
            )
            if has_active_draft:
                print(
                    f"✅ [ZONE2 DRAFT VISIBILITY] Keeping jig visible for active draft: "
                    f"jig_id={getattr(jig, 'jig_id', '')}, lot_ids={sorted(jig_lot_ids)}"
                )
                filtered_jigs.append(jig)
                continue

            # Fallback: use only records that represent an actual/final unload.
            # A JUSubmittedZ1 record with is_draft=False is the model-level
            # "Save & Back" state used by the working Zone 1 flow.  It must NOT
            # by itself remove the JIG from the Zone 2 pick table.  The JIG is
            # hidden only when the final-completion state is reached
            # (last_process_module='Jig Unloading') or an actual unload record
            # exists in JigUnloadAfterTable/JigUnload_TrayId.
            #
            # This is intentionally NOT an Already Loaded change.
            # is_merged_additional / is_already_loaded_z1 remain untouched.
            unloaded_lot_ids = (
                unload_map.get(jig.jig_id, set())
                | (jig_lot_ids & bare_unloaded_lot_ids)
            )
            
            # Keep jig if ANY lot_id is NOT unloaded
            if not jig_lot_ids.issubset(unloaded_lot_ids):
                filtered_jigs.append(jig)
            else:
                print(f"🚫 [ZONE2 MAP PATH] Hiding fully unloaded jig: {jig.jig_id}")
        
        return filtered_jigs

    def check_draft_status_for_jigs(self, jig_queryset):
        """Check if any jig has draft records based on main_lot_id in new_lot_ids or lot_id_quantities"""
        
        # Get all main_lot_ids from saved drafts
        draft_lot_ids = set(JigUnloadDraft.objects.values_list('main_lot_id', flat=True))
        
        print(f"🔍 Zone 2 - Found {len(draft_lot_ids)} draft lot_ids: {draft_lot_ids}")
        
        # Convert QuerySet to list explicitly
        jig_list = list(jig_queryset)
        
        # Check each jig's lot_ids against draft lot_ids
        for jig_detail in jig_list:
            has_draft = False
            
            print(f"🔍 Zone 2 - Checking jig {jig_detail.jig_id} (lot: {jig_detail.lot_id}):")
            
            # Check new_lot_ids array field
            if hasattr(jig_detail, 'new_lot_ids') and jig_detail.new_lot_ids:
                print(f"   - new_lot_ids: {jig_detail.new_lot_ids}")
                for lot_id in jig_detail.new_lot_ids:
                    if lot_id in draft_lot_ids:
                        has_draft = True
                        print(f"✅ Zone 2 - DRAFT MATCH in new_lot_ids: {lot_id}")
                        break
            
            zone2_lot_quantities = self._get_zone2_jig_lot_quantities(jig_detail)

            # Check all lot_ids represented by this row, including multi-model lot_ids.
            if not has_draft and zone2_lot_quantities:
                print(f"   - zone2 lot_id keys: {list(zone2_lot_quantities.keys())}")
                for lot_id in zone2_lot_quantities.keys():
                    if lot_id in draft_lot_ids:
                        has_draft = True
                        print(f"✅ Zone 2 - DRAFT MATCH in lot_id_quantities: {lot_id}")
                        break
            
            # Check main lot_id as fallback
            if not has_draft and hasattr(jig_detail, 'lot_id') and jig_detail.lot_id:
                print(f"   - main lot_id: {jig_detail.lot_id}")
                if jig_detail.lot_id in draft_lot_ids:
                    has_draft = True
                    print(f"✅ Zone 2 - DRAFT MATCH in main lot_id: {jig_detail.lot_id}")
            
            # Compute all_models_submitted_z1 for View icon
            _all_lids_z1 = set(zone2_lot_quantities.keys()) or {jig_detail.lot_id}
            _submitted_z1 = set(
                JUSubmittedZ1.objects.filter(jig_completed_id=jig_detail.id, is_draft=False)
                .values_list('lot_id', flat=True)
            )
            jig_detail.all_models_submitted_z1 = _all_lids_z1.issubset(_submitted_z1) and len(_submitted_z1) > 0

            # Draft indicator: any JUSubmittedZ1 draft record for this jig (created
            # by the "Draft" button in the tray-scan modal) also counts as a draft,
            # not just the legacy JigUnloadDraft table checked above. Without this,
            # a model saved as Draft here never flips jig_unload_draft to True, so
            # the row falls through to "Pending"/"Already Loaded" with no way back
            # into the draft via the row-level Resume button.
            if not has_draft and not jig_detail.all_models_submitted_z1:
                _has_draft_z1_row = JUSubmittedZ1.objects.filter(
                    jig_completed_id=jig_detail.id, is_draft=True
                ).exists()
                if _has_draft_z1_row:
                    has_draft = True
                    print(f"✅ Zone 2 - DRAFT MATCH in JUSubmittedZ1.is_draft for jig {getattr(jig_detail, 'jig_id', jig_detail.lot_id)}")

            jig_detail.has_unload_draft = has_draft
            jig_detail.jig_unload_draft = has_draft

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

            # Also mark as draft when some (but not all) models have been finally submitted
            if not jig_detail.jig_unload_draft and not jig_detail.all_models_submitted_z1:
                _has_any_final = len(_submitted_z1) > 0
                if _has_any_final:
                    jig_detail.has_unload_draft = True
                    jig_detail.jig_unload_draft = True
                    has_draft = True
            
            if has_draft:
                print(f"🎯 Zone 2 - JIG {jig_detail.jig_id} MARKED AS DRAFT")
            else:
                print(f"❌ Zone 2 - JIG {jig_detail.jig_id} NO DRAFT")
        
        print(f"🔍 Zone 2 - Draft check complete for {len(jig_list)} jigs")
        return jig_list  # Return the list, not queryset


@login_required
@csrf_exempt
@require_http_methods(["POST"])
def JU_Zone_get_model_details(request):
    """
    Fetch model details including tray type and capacity for a given model number
    """
    try:
        data = json.loads(request.body)
        model_number = data.get('model_number')
        lot_id = data.get('lot_id')

        def build_model_detail_zone(mn, lot):
            import re as _re_local
            mmc = ModelMasterCreation.objects.filter(
                plating_stk_no=mn
            ).select_related('model_stock_no__tray_type', 'model_stock_no').first()

            if not mmc:
                _m = _re_local.match(r'^(\d+)', str(mn))
                if _m:
                    mmc = ModelMasterCreation.objects.filter(
                        model_stock_no__model_no=_m.group(1)
                    ).select_related('model_stock_no__tray_type', 'model_stock_no').first()

            if not mmc and lot:
                _tsm = TotalStockModel.objects.filter(lot_id=lot).select_related('batch_id').first()
                if _tsm and _tsm.batch_id:
                    mmc = ModelMasterCreation.objects.filter(
                        id=_tsm.batch_id.id
                    ).select_related('model_stock_no__tray_type', 'model_stock_no').first()

            if not mmc and lot:
                try:
                    _rsm = RecoveryStockModel.objects.filter(lot_id=lot).select_related('batch_id').first()
                    if _rsm and _rsm.batch_id:
                        from Recovery_DP.models import RecoveryMasterCreation
                        _rmc = RecoveryMasterCreation.objects.filter(
                            id=_rsm.batch_id.id
                        ).select_related('model_stock_no__tray_type', 'model_stock_no').first()
                        if _rmc:
                            mmc = _rmc
                except Exception:
                    pass

            tray_type = mmc.model_stock_no.tray_type.tray_type if mmc and mmc.model_stock_no.tray_type else 'Normal'
            view_instance = JU_Zone_MainTable()
            tray_capacity = view_instance.get_dynamic_tray_capacity(tray_type)

            plating_color_name = None
            if lot:
                total_stock_model = TotalStockModel.objects.filter(lot_id=lot).select_related('plating_color').first()
                if mmc and getattr(mmc, 'plating_color', None):
                    plating_color_name = mmc.plating_color
                elif total_stock_model and total_stock_model.plating_color:
                    plating_color_name = total_stock_model.plating_color.plating_color
                else:
                    # Try JigCompleted by direct lot_id OR by lot_id_quantities key (for JLOT sub-lots)
                    jig_detail = (
                        JigCompleted.objects.filter(lot_id=lot).first()
                        or JigCompleted.objects.filter(
                            draft_data__lot_id_quantities__has_key=lot
                        ).first()
                    )
                    if jig_detail and jig_detail.draft_data.get('plating_color'):
                        plating_color_name = jig_detail.draft_data.get('plating_color')
                    elif jig_detail and jig_detail.batch_id:
                        _mmc_jc = ModelMasterCreation.objects.filter(
                            batch_id=jig_detail.batch_id
                        ).values('plating_color').first()
                        if _mmc_jc and _mmc_jc.get('plating_color'):
                            plating_color_name = _mmc_jc['plating_color']

            tray_id_color = '#dc3545' if plating_color_name == 'IPS' else '#006400'
            tray_id_prefix = 'NR' if tray_type == 'Normal' else 'JD'

            return {
                'model_number': mn,
                'lot_id': lot,
                'tray_type': tray_type,
                'tray_capacity': tray_capacity,
                'plating_color': plating_color_name,
                'plating_stk_no': getattr(mmc, 'plating_stk_no', None) or mn,
                'tray_id_color': tray_id_color,
                'tray_id_prefix': tray_id_prefix,
                'model_name': mmc.model_stock_no.model_name if mmc and hasattr(mmc.model_stock_no, 'model_name') else mn
            }

        # If model_number not provided but lot_id is, try to find all models for that lot
        if not model_number and lot_id:
            candidate_model_numbers = []
            jig = JigCompleted.objects.filter(lot_id=lot_id).first()
            if jig and getattr(jig, 'no_of_model_cases', None):
                candidate_model_numbers = list(jig.no_of_model_cases)
            elif jig and getattr(jig, 'new_lot_ids', None):
                candidate_model_numbers = list(jig.new_lot_ids)

            if not candidate_model_numbers:
                tsm = TotalStockModel.objects.filter(lot_id=lot_id).select_related('batch_id').first()
                if tsm and tsm.batch_id and getattr(tsm.batch_id, 'plating_stk_no', None):
                    candidate_model_numbers = [tsm.batch_id.plating_stk_no]
                else:
                    try:
                        rsm = RecoveryStockModel.objects.filter(lot_id=lot_id).select_related('batch_id').first()
                        if rsm and rsm.batch_id and getattr(rsm.batch_id, 'plating_stk_no', None):
                            candidate_model_numbers = [rsm.batch_id.plating_stk_no]
                    except Exception:
                        candidate_model_numbers = []

            unique_models = []
            for m in candidate_model_numbers:
                if m and m not in unique_models:
                    unique_models.append(m)

            models = [build_model_detail_zone(mn, lot_id) for mn in unique_models]
            return JsonResponse({'success': True, 'models': models, 'multiple_models': len(models) > 1})
        
        # Fetch model details from ModelMaster and related tables
        # FIXED: Search by plating_stk_no since frontend sends full plating stock number
        import re as _re
        # Fetch model details — try multiple strategies to find ModelMasterCreation
        model_master_creation = ModelMasterCreation.objects.filter(
            plating_stk_no=model_number
        ).select_related(
            'model_stock_no__tray_type',
            'model_stock_no'
        ).first()

        # Fallback 1: search by numeric model_no (handles '1805' or '1805SAD02' → '1805')
        if not model_master_creation:
            _m = _re.match(r'^(\d+)', str(model_number))
            if _m:
                model_master_creation = ModelMasterCreation.objects.filter(
                    model_stock_no__model_no=_m.group(1)
                ).select_related('model_stock_no__tray_type', 'model_stock_no').first()
                if model_master_creation:
                    print(f"[DEBUG] JU_Zone_get_model_details fallback 1: found via model_no={_m.group(1)}")

        # Fallback 2: derive from lot_id via TotalStockModel → batch_id
        if not model_master_creation and lot_id:
            _tsm = TotalStockModel.objects.filter(lot_id=lot_id).select_related('batch_id').first()
            if _tsm and _tsm.batch_id:
                model_master_creation = ModelMasterCreation.objects.filter(
                    id=_tsm.batch_id.id
                ).select_related('model_stock_no__tray_type', 'model_stock_no').first()
                if model_master_creation:
                    print(f"[DEBUG] JU_Zone_get_model_details fallback 2: found via TotalStockModel lot_id={lot_id}")

        # Fallback 3: derive from lot_id via RecoveryStockModel → batch_id
        if not model_master_creation and lot_id:
            try:
                _rsm = RecoveryStockModel.objects.filter(lot_id=lot_id).select_related('batch_id').first()
                if _rsm and _rsm.batch_id:
                    from Recovery_DP.models import RecoveryMasterCreation
                    _rmc = RecoveryMasterCreation.objects.filter(
                        id=_rsm.batch_id.id
                    ).select_related('model_stock_no__tray_type', 'model_stock_no').first()
                    if _rmc:
                        model_master_creation = _rmc
                        print(f"[DEBUG] JU_Zone_get_model_details fallback 3: found via RecoveryStockModel lot_id={lot_id}")
            except Exception as _e3:
                print(f"[DEBUG] JU_Zone_get_model_details fallback 3 error: {_e3}")

        # Fallback 4: JigCompleted (Zone 2 Jig Loading lot IDs not in TotalStock/RecoveryStock)
        if not model_master_creation and lot_id:
            try:
                from Jig_Loading.models import JigCompleted as _JC4
                _jc4 = _JC4.objects.filter(lot_id=lot_id).first()
                if _jc4 and _jc4.batch_id:
                    _mmc4 = ModelMasterCreation.objects.filter(
                        batch_id=_jc4.batch_id
                    ).select_related('model_stock_no__tray_type', 'model_stock_no').first()
                    if _mmc4:
                        model_master_creation = _mmc4
                        print(f"[DEBUG] JU_Zone_get_model_details fallback 4: found via JigCompleted batch '{_jc4.batch_id}'")
            except Exception as _e4:
                print(f"[DEBUG] JU_Zone_get_model_details fallback 4 error: {_e4}")

        # Also try to get from TotalStockModel to get plating_color using lot_id
        total_stock_model = None
        if lot_id:
            total_stock_model = TotalStockModel.objects.filter(
                lot_id=lot_id
            ).select_related(
                'plating_color'
            ).first()

        # Fallback to plating_stk_no search if lot_id not provided or not found
        if not total_stock_model:
            if model_master_creation:
                total_stock_model = TotalStockModel.objects.filter(
                    batch_id=model_master_creation
                ).select_related(
                    'plating_color'
                ).first()

        if not model_master_creation:
            return JsonResponse({
                'success': False,
                'error': f'Model {model_number} not found'
            })
        
        # Get tray details with dynamic capacity override
        tray_type = model_master_creation.model_stock_no.tray_type.tray_type if model_master_creation.model_stock_no.tray_type else "Normal"
        
        # Use dynamic tray capacity (checks InprocessInspectionTrayCapacity for overrides)
        view_instance = JU_Zone_MainTable()
        tray_capacity = view_instance.get_dynamic_tray_capacity(tray_type)
        
        # Get plating color information
        plating_color = None
        plating_color_name = None
        tray_id_color = "#006400"  # Default dark green (Zone 2 handles non-IPS colors)
        tray_id_prefix = "ND" if tray_type == "Normal" else "JD"
        
        print(f"[DEBUG] Zone 2 JU_Zone_get_model_details - model_number: {model_number}, lot_id: {lot_id}")
        print(f"[DEBUG] Zone 2 - model_master_creation found: {model_master_creation is not None}")
        
        # ENHANCED: Get plating color from ModelMasterCreation first (most reliable)
        if model_master_creation and model_master_creation.plating_color:
            plating_color_name = model_master_creation.plating_color
            print(f"[DEBUG] Zone 2 - Found plating_color in ModelMasterCreation: {plating_color_name}")
        elif total_stock_model and total_stock_model.plating_color:
            plating_color = total_stock_model.plating_color
            plating_color_name = plating_color.plating_color
            print(f"[DEBUG] Zone 2 - Found plating_color in TotalStockModel: {plating_color_name}")
        else:
            # Try to get plating color from JigCompleted if not in other sources
            print(f"[DEBUG] Zone 2 - No plating_color in ModelMasterCreation/TotalStockModel, checking JigCompleted...")
            jig_detail = JigCompleted.objects.filter(lot_id=lot_id).first()
            if jig_detail and jig_detail.draft_data.get('plating_color'):
                plating_color_name = jig_detail.draft_data.get('plating_color')
                print(f"[DEBUG] Zone 2 - Found plating_color in JigCompleted: {plating_color_name}")
            else:
                print(f"[DEBUG] Zone 2 - No plating_color found in any source")
        
        # Determine tray ID color based on plating color
        if plating_color_name == "IPS":
            tray_id_color = "#dc3545"  # Red for IPS
            tray_id_prefix = "NR" if tray_type == "Normal" else "JR"
        elif plating_color_name and any(bi in plating_color_name.lower() for bi in ["bi color", "bicolor", "bi-color"]):
            tray_id_color = "#90EE90"  # Light green for Bi Color
            tray_id_prefix = "NL" if tray_type == "Normal" else "JL"
        else:
            tray_id_color = "#006400"  # Dark green for other colors
            tray_id_prefix = "ND" if tray_type == "Normal" else "JD"
        
        # Check for multiple models in the same lot/jig
        multiple_models = False
        if lot_id:
            # Check if there are multiple different models in the same JIG lot
            from Jig_Loading.models import JigCompleted
            jig_detail = JigCompleted.objects.filter(lot_id=lot_id).first()
            if jig_detail and jig_detail.no_of_model_cases:
                # Count unique models in the array
                multiple_models = len(set(jig_detail.no_of_model_cases)) > 1
            else:
                multiple_models = False
        
        # You can also fetch additional details if needed
        model_details = {
            'model_number': model_number,
            'lot_id': lot_id,
            'tray_type': tray_type,
            'tray_capacity': tray_capacity,
            'plating_color': plating_color_name,
            'plating_stk_no': getattr(model_master_creation, 'plating_stk_no', None) or model_number,
            'multiple_models': multiple_models,
            'tray_id_color': tray_id_color,
            'tray_id_prefix': tray_id_prefix,
            'model_name': model_master_creation.model_stock_no.model_name if hasattr(model_master_creation.model_stock_no, 'model_name') else model_number
        }
        
        print(f"[DEBUG] Zone 2 - Final model details: {model_details}")

        # Single-model response but include models array for backward compatibility
        return JsonResponse({
            'success': True,
            **model_details,
            'models': [model_details],
            'multiple_models': multiple_models
        })
        
    except json.JSONDecodeError:
        return JsonResponse({
            'success': False,
            'error': 'Invalid JSON data'
        })
    except Exception as e:
        return JsonResponse({
            'success': False,
            'error': 'Unable to process the request. Please verify the submitted data and try again.'
        })

@method_decorator(csrf_exempt, name='dispatch')
class JU_Zone_SaveHoldUnholdReasonAPIView(APIView):
    """
    POST with:
    {
        "remark": "Reason text",
        "action": "hold"  # or "unhold"
        "lot_id": "LOT123"
    }
    """
    permission_classes = [IsAuthenticated]

    def post(self, request):
        from django.utils import timezone
        try:
            data = request.data if hasattr(request, 'data') else json.loads(request.body.decode('utf-8'))
            lot_id = data.get('lot_id')
            remark = (data.get('remark') or '').strip()
            action = (data.get('action') or '').strip().lower()

            if not lot_id or not remark or action not in ['hold', 'unhold']:
                return JsonResponse({'success': False, 'error': 'Missing or invalid parameters.'}, status=400)

            # Only use JigCompleted table
            obj = JigCompleted.objects.filter(lot_id=lot_id).first()
            if not obj:
                return JsonResponse({'success': False, 'error': 'JigCompleted record not found.'}, status=404)

            now = timezone.now()
            if action == 'hold':
                obj.unload_holding_reason = remark
                obj.unload_hold_lot = True
                obj.unload_hold_by = request.user
                obj.unload_hold_at = now
                obj.unload_release_reason = ''
                obj.unload_release_lot = False
            elif action == 'unhold':
                obj.unload_release_reason = remark
                obj.unload_hold_lot = False
                obj.unload_release_lot = True
                obj.unload_release_by = request.user
                obj.unload_release_at = now

            obj.save(update_fields=[
                'unload_holding_reason', 'unload_release_reason', 'unload_hold_lot', 'unload_release_lot',
                'unload_hold_by', 'unload_hold_at', 'unload_release_by', 'unload_release_at',
            ])
            return JsonResponse({
                'success': True,
                'lot_id': lot_id,
                'action': action,
                'holding_reason': obj.unload_holding_reason or '',
                'release_reason': obj.unload_release_reason or '',
                'hold_lot': obj.unload_hold_lot,
                'release_lot': obj.unload_release_lot,
                'hold_by': obj.unload_hold_by.username if obj.unload_hold_by else '',
                'hold_at': timezone.localtime(obj.unload_hold_at).strftime("%d-%b-%Y %I:%M %p") if obj.unload_hold_at else '',
                'release_by': obj.unload_release_by.username if obj.unload_release_by else '',
                'release_at': timezone.localtime(obj.unload_release_at).strftime("%d-%b-%Y %I:%M %p") if obj.unload_release_at else '',
                'message': 'Reason saved.',
            })

        except Exception as e:
            return JsonResponse({'success': False, 'error': 'Unable to process the request. Please verify the submitted data and try again.'}, status=500)

@method_decorator(csrf_exempt, name='dispatch')
class JU_Zone_JigPickRemarkAPIView(APIView):
    def post(self, request):
        try:
            data = request.data if hasattr(request, 'data') else json.loads(request.body.decode('utf-8'))
            jig_completed_id = str(data.get('jig_completed_id') or '').strip()
            lot_id = str(data.get('lot_id') or data.get('jig_lot_id') or '').strip()
            jig_id = str(data.get('jig_id') or '').strip()
            remark = str(data.get('unloading_remarks') or '').strip()

            if not remark:
                return JsonResponse({'success': False, 'error': 'Remark is required.'}, status=400)

            jig_detail = None
            if jig_completed_id:
                if not jig_completed_id.isdigit():
                    return JsonResponse({'success': False, 'error': 'Invalid JigCompleted ID.'}, status=400)
                jig_detail = JigCompleted.objects.filter(id=jig_completed_id).first()
            if not jig_detail and lot_id:
                jig_detail = JigCompleted.objects.filter(lot_id=lot_id).first()
            if not jig_detail and jig_id:
                jig_detail = JigCompleted.objects.filter(jig_id=jig_id).first()
            if not jig_detail:
                return JsonResponse({'success': False, 'error': 'JigCompleted record not found.'}, status=404)

            jig_detail.unloading_remarks = remark
            jig_detail.save(update_fields=['unloading_remarks'])
            return JsonResponse({'success': True, 'message': 'Remark saved'})
        except (json.JSONDecodeError, ParseError, UnicodeDecodeError):
            return JsonResponse({'success': False, 'error': 'Invalid JSON payload.'}, status=400)
        except Exception:
            logger.exception('Unexpected error while saving Zone 2 jig unloading remark.')
            return JsonResponse({'success': False, 'error': 'Unable to process the request. Please verify the submitted data and try again.'}, status=500)

def populate_jig_unload_fields(jig_unload_instance, lot_ids, jig_lot_id=None):
    """
    Smart helper to populate JigUnloadAfterTable fields from TotalStockModel + JigCompleted
    """
    if not lot_ids:
        print("[SMART POPULATE Zone2] ⚠️ No lot_ids provided")
        return
    
    print(f"[SMART POPULATE Zone2] 🎯 Starting populate for lot_ids: {lot_ids}")
    
    # Get first available lot to extract data
    for raw_lot_id in lot_ids:
        try:
            # Clean lot_id - extract actual lot ID from JLOT format if needed
            lot_id = raw_lot_id
            if 'JLOT-' in raw_lot_id and '-' in raw_lot_id:
                parts = raw_lot_id.split('-')
                if len(parts) >= 3:  # JLOT-ABC123-LID456...
                    lot_id = '-'.join(parts[2:])  # Get LID... part
                elif len(parts) == 2:  # JLOT-ABC123
                    lot_id = raw_lot_id  # Keep original
                print(f"[SMART POPULATE Zone2] 🔄 Extracted lot_id: '{lot_id}' from '{raw_lot_id}'")
            
            print(f"[SMART POPULATE Zone2] 🔍 Processing lot_id: {lot_id}")
            
            # Get TotalStockModel data
            total_stock = TotalStockModel.objects.select_related(
                'batch_id', 'version', 'model_stock_no', 'plating_color',
                'model_stock_no__polish_finish', 'model_stock_no__tray_type'
            ).prefetch_related('location').filter(lot_id=lot_id).first()
            
            if not total_stock:
                print(f"[SMART POPULATE Zone2] ❌ No TotalStockModel found for lot_id: {lot_id}")
                # Set jig_qr_id from JigCompleted.jig_id even when TotalStockModel missing
                if not jig_unload_instance.jig_qr_id:
                    _jc_fb_z2 = JigCompleted.objects.filter(
                        draft_data__lot_id_quantities__has_key=lot_id
                    ).first()
                    if _jc_fb_z2:
                        _jid_fb_z2 = getattr(_jc_fb_z2, 'jig_id', None)
                        if _jid_fb_z2:
                            jig_unload_instance.jig_qr_id = _jid_fb_z2
                            jig_unload_instance.save(update_fields=['jig_qr_id'])
                            print(f"[SMART POPULATE Zone2] ✅ Fallback: saved jig_qr_id={_jid_fb_z2}")
                continue
                
            print(f"[SMART POPULATE Zone2] ✅ Found TotalStockModel for lot_id: {lot_id}")
            
            # Get JigCompleted data - CRITICAL for plating color
            jig_detail = None
            if jig_lot_id:
                # Try to find JigCompleted using lot_id field
                jig_detail = JigCompleted.objects.filter(lot_id=jig_lot_id).first()
                if not jig_detail:
                    # Fallback: try with lot_id_quantities (stored inside draft_data JSON)
                    jig_detail = JigCompleted.objects.filter(
                        draft_data__lot_id_quantities__has_key=lot_id
                    ).first()
            
            if not total_stock.batch_id:
                print(f"[SMART POPULATE Zone2] ❌ No batch_id in TotalStockModel for lot_id: {lot_id}")
                continue
                
            batch = total_stock.batch_id
            
            # Populate fields from TotalStockModel/ModelMasterCreation
            jig_unload_instance.version = total_stock.version
            
            # 🔧 ENHANCED PLATING COLOR LOGIC: Try TotalStockModel first, fallback to ModelMasterCreation
            plating_color_assigned = False
            if total_stock.plating_color:
                jig_unload_instance.plating_color = total_stock.plating_color
                plating_color_assigned = True
                print(f"[SMART POPULATE Zone2] ✅ Got plating_color from TotalStock: {total_stock.plating_color} (ID: {total_stock.plating_color.id})")
            else:
                # Fallback: Try to get from ModelMasterCreation.plating_color (string field)
                if batch.plating_color:
                    try:
                        from modelmasterapp.models import Plating_Color
                        plating_color_obj = Plating_Color.objects.filter(
                            plating_color=batch.plating_color
                        ).first()
                        if plating_color_obj:
                            jig_unload_instance.plating_color = plating_color_obj
                            plating_color_assigned = True
                            print(f"[SMART POPULATE Zone2] ✅ Fallback: Got plating_color from ModelMasterCreation: {plating_color_obj} (ID: {plating_color_obj.id})")
                        else:
                            print(f"[SMART POPULATE Zone2] ❌ No Plating_Color object found for string: '{batch.plating_color}'")
                    except Exception as e:
                        print(f"[SMART POPULATE Zone2] ❌ Error in plating_color fallback: {e}")
                        
            if not plating_color_assigned:
                print(f"[SMART POPULATE Zone2] ⚠️ No plating_color assigned from either source")
            
            jig_unload_instance.plating_stk_no = batch.plating_stk_no
            jig_unload_instance.polish_stk_no = batch.polishing_stk_no
            jig_unload_instance.category = batch.category
            
            # Debug logging for final plating color
            print(f"[SMART POPULATE Zone2] 🎨 Final plating_color: {jig_unload_instance.plating_color} (ID: {jig_unload_instance.plating_color.id if jig_unload_instance.plating_color else 'None'})")
            print(f"[SMART POPULATE Zone2] 🎯 Setting plating_stk_no: {batch.plating_stk_no}")
            print(f"[SMART POPULATE Zone2] ✨ Setting version: {total_stock.version}")
            
            # Polish finish from model master
            if total_stock.model_stock_no:
                jig_unload_instance.polish_finish = total_stock.model_stock_no.polish_finish
                if total_stock.model_stock_no.tray_type:
                    jig_unload_instance.tray_type = total_stock.model_stock_no.tray_type.tray_type
                    jig_unload_instance.tray_capacity = total_stock.model_stock_no.tray_capacity
            
            # 🧠 SMART: Get jig_qr_id from JigCompleted.jig_id
            if jig_detail:
                _jig_id_val = getattr(jig_detail, 'jig_id', None)
                if _jig_id_val:
                    jig_unload_instance.jig_qr_id = _jig_id_val
                    print(f"[SMART POPULATE Zone2] ✅ Got jig_qr_id from jig_id: {_jig_id_val}")
                else:
                    print(f"[SMART POPULATE Zone2] ⚠️ JigCompleted found but jig_id is empty")
            else:
                print(f"[SMART POPULATE Zone2] ⚠️ No JigCompleted found for jig_lot_id: {jig_lot_id}")
            
            # Save to persist field changes
            print(f"[SMART POPULATE Zone2] 💾 Saving instance before checking persistence...")
            jig_unload_instance.save()
            
            # Verify the save was successful
            jig_unload_instance.refresh_from_db()
            print(f"[SMART POPULATE Zone2] ✅ After save - plating_color: {jig_unload_instance.plating_color}")
            print(f"[SMART POPULATE Zone2] ✅ After save - plating_color ID: {jig_unload_instance.plating_color.id if jig_unload_instance.plating_color else 'None'}")
            print(f"[SMART POPULATE Zone2] ✅ After save - jig_qr_id: {jig_unload_instance.jig_qr_id}")
            print(f"[SMART POPULATE Zone2] ✅ After save - version: {jig_unload_instance.version}")
            print(f"[SMART POPULATE Zone2] ✅ After save - plating_stk_no: {jig_unload_instance.plating_stk_no}")
            print(f"[SMART POPULATE Zone2] ✅ After save - polish_stk_no: {jig_unload_instance.polish_stk_no}")
            print(f"[SMART POPULATE Zone2] ✅ After save - category: {jig_unload_instance.category}")
            print(f"[SMART POPULATE Zone2] ✅ After save - tray_type: {jig_unload_instance.tray_type}")
            print(f"[SMART POPULATE Zone2] ✅ After save - tray_capacity: {jig_unload_instance.tray_capacity}")
            print(f"[SMART POPULATE Zone2] ✅ After save - polish_finish: {jig_unload_instance.polish_finish}")
            
            print(f"[SMART POPULATE Zone2] ✅ Saved instance with plating_color: {jig_unload_instance.plating_color}")
            
            # 🧠 SMART LOCATION: Try multiple sources for location
            locations_to_set = []
            
            # First try: Get from TotalStockModel
            if total_stock.location.exists():
                locations_to_set = list(total_stock.location.all())
                print(f"[SMART POPULATE Zone2] ✅ Got locations from TotalStock: {[l.location_name for l in locations_to_set]}")
            
            # Second try: Get from ModelMasterCreation batch
            elif batch.location:
                locations_to_set = [batch.location]
                print(f"[SMART POPULATE Zone2] ✅ Got location from Batch: {batch.location.location_name}")
            
            # Third try: Get from JigCompleted if it has location field
            elif jig_detail and hasattr(jig_detail, 'location') and jig_detail.location:
                if hasattr(jig_detail.location, 'all'):  # ManyToMany
                    locations_to_set = list(jig_detail.location.all())
                else:  # ForeignKey
                    locations_to_set = [jig_detail.location]
                print(f"[SMART POPULATE Zone2] ✅ Got locations from JigCompleted")
            
            # Set locations if found
            if locations_to_set:
                jig_unload_instance.location.set(locations_to_set)
                print(f"[SMART POPULATE Zone2] ✅ Set {len(locations_to_set)} locations")
                
            print(f"[SMART POPULATE Zone2] ✅ Populated all fields from lot: {lot_id}")
            break  # Use first valid lot found
            
        except Exception as e:
            print(f"[SMART POPULATE Zone2] ⚠️ Error with lot {lot_id}: {e}")
            import traceback
            traceback.print_exc()
            continue

@csrf_exempt
@require_POST
def JU_Zone_save_jig_unload_tray_ids(request):
    import json
    import logging
    
    logger = logging.getLogger(__name__)
    
    try:
        data = json.loads(request.body)
        trays = data.get('trays', [])
        combined_lot_ids = data.get('combined_lot_ids', [])
        main_lot_id = data.get('main_lot_id', '')
        jig_lot_id = data.get('jig_lot_id', '')
        
        # 🔧 NEW: Get jig-aware sources from frontend
        jig_aware_sources = data.get('jig_aware_sources', [])

        print(f"[SMART SAVE] jig_lot_id: '{jig_lot_id}'")
        print(f"[SMART SAVE] combined_lot_ids: {combined_lot_ids}")
        print(f"[SMART SAVE] main_lot_id: '{main_lot_id}'")
        print(f"[SMART SAVE] jig_aware_sources: {jig_aware_sources}")
        print(f"[SMART SAVE] trays count: {len(trays)}")

        _hold_check_lot_id = jig_lot_id or main_lot_id
        if _hold_check_lot_id:
            _hold_check_jc = JigCompleted.objects.filter(lot_id=_hold_check_lot_id).only('unload_hold_lot').first()
            if _hold_check_jc and _hold_check_jc.unload_hold_lot:
                return JsonResponse({'success': False, 'error': 'This lot is on hold and cannot be processed until released.'}, status=400)

        if not trays:
            return JsonResponse({'success': False, 'error': 'Trays data is missing.'})

        allowed_lot_ids_for_trays = [main_lot_id, jig_lot_id] + list(combined_lot_ids or [])
        allowed_lot_ids_for_trays.extend(
            tray.get('lot_id') for tray in trays if tray.get('lot_id')
        )
        seen_tray_ids = set()
        for i, tray in enumerate(trays):
            tray_id = normalize_jig_unload_tray_id(tray.get('tray_id', ''))
            if not tray_id:
                return JsonResponse({'success': False, 'error': f'Missing tray ID for tray {i}'}, status=400)
            tray['tray_id'] = tray_id
            if not is_valid_jig_unload_tray_id_format(tray_id):
                return JsonResponse({
                    'success': False,
                    'error': f'Tray ID "{tray_id}" has invalid format. Expected format: XX-A00001'
                }, status=400)
            if tray_id in seen_tray_ids:
                return JsonResponse({
                    'success': False,
                    'error': f'Duplicate tray ID "{tray_id}". Each tray must be unique.'
                }, status=400)
            seen_tray_ids.add(tray_id)
            tray_conflict = find_jig_unload_tray_conflict(
                tray_id,
                allowed_lot_ids=allowed_lot_ids_for_trays,
                include_tray_master=True,
            )
            if tray_conflict:
                return JsonResponse({
                    'success': False,
                    'error': tray_conflict['message'],
                    'validation_type': 'tray_occupied',
                    'linked_lot': tray_conflict.get('linked_lot', ''),
                    'source': tray_conflict.get('source', ''),
                    'tray_index': i,
                }, status=400)
            nickel_conflict = validate_jig_unload_nickel_reject_tray(
                tray_id,
                current_lot_id=tray.get('lot_id') or main_lot_id or None,
            )
            if nickel_conflict:
                return JsonResponse({
                    'success': False,
                    'error': nickel_conflict['message'],
                    'validation_type': 'tray_occupied',
                    'linked_lot': nickel_conflict.get('linked_lot', ''),
                    'source': nickel_conflict.get('source', ''),
                    'tray_index': i,
                }, status=400)

        # Collect tray data
        all_lot_ids_from_trays = set()
        total_case_qty = 0
        
        for i, tray in enumerate(trays):
            tray_id = tray.get('tray_id')
            tray_qty = tray.get('tray_qty')
            tray_lot_id = tray.get('lot_id')

            if not all([tray_id, tray_qty, tray_lot_id]):
                return JsonResponse({'success': False, 'error': f'Missing data for tray {i}'})

            all_lot_ids_from_trays.add(tray_lot_id)
            total_case_qty += int(tray_qty)

        # 🔧 FIXED: Smart combined lot_ids formatting with PRESERVED jig_lot_id mapping
        final_formatted_combined_lot_ids = []
        
        if jig_aware_sources:
            print(f"[SMART SAVE] 🎯 Using jig-aware sources for accurate formatting")
            
            # 🔧 FIXED: Parse jig-aware sources and preserve original jig_lot_id
            for jig_aware_source in jig_aware_sources:
                if ':' in jig_aware_source:
                    # Format: "JLOT-8CEDE491A4A3:LID210820252112300004"
                    original_jig_lot_id, lot_id = jig_aware_source.split(':', 1)
                    formatted_id = f"{original_jig_lot_id}-{lot_id}"
                    final_formatted_combined_lot_ids.append(formatted_id)
                    print(f"[SMART SAVE] ✅ Preserved mapping: {jig_aware_source} -> {formatted_id}")
                else:
                    print(f"[SMART SAVE] ⚠️ Invalid jig-aware source format: {jig_aware_source}")
            
        elif combined_lot_ids and jig_lot_id:
            print(f"[SMART SAVE] 📋 Fallback: Using current jig_lot_id for all lot_ids")
            # Fallback: use current jig_lot_id for all (original behavior)
            for lot_id in combined_lot_ids:
                formatted_id = f"{jig_lot_id}-{lot_id}"
                final_formatted_combined_lot_ids.append(formatted_id)
                print(f"[SMART SAVE] 🔄 Formatted (fallback): {lot_id} -> {formatted_id}")
        else:
            # Final fallback: use lot_ids from trays
            final_formatted_combined_lot_ids = list(all_lot_ids_from_trays)
            print(f"[SMART SAVE] 🆘 Final fallback to tray lot_ids: {final_formatted_combined_lot_ids}")
        
        # Clean and validate lot_ids
        cleaned_combined_lot_ids = combined_lot_ids if combined_lot_ids else list(all_lot_ids_from_trays)
        cleaned_combined_lot_ids = list(set([lot_id.strip() for lot_id in cleaned_combined_lot_ids if lot_id and lot_id.strip()]))
        
        print(f"[SMART SAVE] 📋 cleaned_combined_lot_ids: {cleaned_combined_lot_ids}")
        print(f"[SMART SAVE] 🎯 FINAL formatted_combined_lot_ids for DB: {final_formatted_combined_lot_ids}")

        # 🧠 SMART DEDUPLICATION: Check existing records
        existing_formatted_ids = set()
        if final_formatted_combined_lot_ids:
            # Find existing records that contain any of these formatted IDs
            existing_records = JigUnloadAfterTable.objects.filter(
                combine_lot_ids__isnull=False
            ).exclude(combine_lot_ids__exact=[])
            
            for record in existing_records:
                if record.combine_lot_ids:
                    for existing_combined_id in record.combine_lot_ids:
                        for new_formatted_id in final_formatted_combined_lot_ids:
                            if existing_combined_id == new_formatted_id:
                                existing_formatted_ids.add(existing_combined_id)
            
            print(f"[SMART SAVE] existing formatted IDs in DB: {existing_formatted_ids}")

        # 🧠 SMART MERGE: Remove duplicates and merge with existing
        unique_formatted_ids = []
        for formatted_id in final_formatted_combined_lot_ids:
            if formatted_id not in existing_formatted_ids:
                unique_formatted_ids.append(formatted_id)
                print(f"[SMART SAVE] ✅ Adding new: {formatted_id}")
            else:
                print(f"[SMART SAVE] ⚠️ Skipping duplicate: {formatted_id}")
        
        # Final formatted list for database
        final_db_combined_lot_ids = unique_formatted_ids
        print(f"[SMART SAVE] 📋 FINAL DB combined_lot_ids: {final_db_combined_lot_ids}")

        # 🔧 FIXED: Enhanced get_stock_model_data_for_save function
        def get_stock_model_data_for_save(lot_id):
            """Get stock model data for individual lot_id during save operation"""
            print(f"🔍 [SAVE] get_stock_model_data_for_save called for lot_id: '{lot_id}'")
            
            # Handle JLOT format - extract actual lot_id
            search_lot_ids = [lot_id]
            
            # 🔧 FIXED: Better parsing for various lot_id formats
            if 'JLOT-' in lot_id and (':' in lot_id or '-' in lot_id):
                # Handle both "JLOT-XXX:LID123" and "JLOT-XXX-LID123" formats
                if ':' in lot_id:
                    actual_lot_id = lot_id.split(':')[-1]  # Get part after ':'
                elif '-' in lot_id:
                    parts = lot_id.split('-')
                    if len(parts) > 1:
                        actual_lot_id = parts[-1]  # Get last part after final '-'
                    else:
                        actual_lot_id = lot_id
                else:
                    actual_lot_id = lot_id
                    
                search_lot_ids.append(actual_lot_id)
                print(f"📋 [SAVE] Extracted actual lot_id: '{actual_lot_id}' from '{lot_id}'")
            
            # Try each possible lot_id format
            for search_id in search_lot_ids:
                # Try TotalStockModel first
                tsm = TotalStockModel.objects.select_related('batch_id').filter(lot_id=search_id).first()
                if tsm:
                    print(f"✅ [SAVE] Found in TotalStockModel with lot_id: '{search_id}'")
                    return tsm, False, ModelMasterCreation
                
                # Try RecoveryStockModel
                try:
                    rsm = RecoveryStockModel.objects.select_related('batch_id').filter(lot_id=search_id).first()
                    if rsm:
                        print(f"✅ [SAVE] Found in RecoveryStockModel with lot_id: '{search_id}'")
                        from Recovery_DP.models import RecoveryMasterCreation
                        return rsm, True, RecoveryMasterCreation
                except:
                    pass
            
            print(f"❌ [SAVE] No stock model found for lot_id: '{lot_id}'")
            return None, False, None

        # Calculate missing_qty (existing logic)
        missing_qty = 0
        expected_qty = 0
        jig_detail = None
        if cleaned_combined_lot_ids:
            for lot_id in cleaned_combined_lot_ids:
                jig_detail = JigCompleted.objects.filter(draft_data__lot_id_quantities__has_key=lot_id).first()
                if jig_detail:
                    break
            lot_id_quantities = (jig_detail.draft_data or {}).get('lot_id_quantities', {}) if jig_detail else {}
            if jig_detail and lot_id_quantities:
                for lot_id in cleaned_combined_lot_ids:
                    expected_qty += int(lot_id_quantities.get(lot_id, 0))
            missing_qty = max(expected_qty - total_case_qty, 0)

        # ✅ COLLECT ALL VALUES FROM INDIVIDUAL LOT_IDS (corrected lot_id extraction)
        all_plating_stk_nos = []
        all_polish_stk_nos = []
        all_versions = []

        print(f"🔍 [SAVE] ===== COLLECTING VALUES FROM LOT_IDS WITH FIXED EXTRACTION =====")

        for lot_id in cleaned_combined_lot_ids:
            print(f"🔄 [SAVE] Processing lot_id: '{lot_id}'")
            
            try:
                stock_model, is_recovery, batch_model_class = get_stock_model_data_for_save(lot_id)
                
                if stock_model:
                    print(f"✅ [SAVE] Found stock model for {lot_id}: {type(stock_model).__name__}")
                    
                    # 🔧 FIXED: Get data from batch_id (ModelMasterCreation) instead of stock model directly
                    batch_model = stock_model.batch_id if hasattr(stock_model, 'batch_id') and stock_model.batch_id else None
                    
                    if batch_model:
                        print(f"✅ [SAVE] Found batch model for {lot_id}: {type(batch_model).__name__}")
                        
                        # Check for plating field in batch model
                        plating_fields_to_check = [
                            'plating_stk_no', 'plating_stock_no', 'plating_stk_number',
                            'plating_number', 'plat_stk_no', 'plat_stock_no'
                        ]
                        
                        found_plating = False
                        for field_name in plating_fields_to_check:
                            if hasattr(batch_model, field_name):
                                field_value = getattr(batch_model, field_name)
                                if field_value:
                                    all_plating_stk_nos.append(str(field_value))
                                    found_plating = True
                                    print(f"✅ [SAVE] Added plating_stk_no: '{field_value}' from batch field '{field_name}' in {lot_id}")
                                    break
                        
                        if not found_plating:
                            print(f"⚠️ [SAVE] No plating_stk_no found in batch model for {lot_id}")
                        
                        # Check for polish field in batch model
                        polish_fields_to_check = [
                            'polish_stk_no', 'polishing_stk_no', 'polish_stock_no', 
                            'polishing_stock_no', 'polish_number', 'pol_stk_no'
                        ]
                        
                        found_polish = False
                        for field_name in polish_fields_to_check:
                            if hasattr(batch_model, field_name):
                                field_value = getattr(batch_model, field_name)
                                if field_value:
                                    all_polish_stk_nos.append(str(field_value))
                                    found_polish = True
                                    print(f"✅ [SAVE] Added polish_stk_no: '{field_value}' from batch field '{field_name}' in {lot_id}")
                                    break
                        
                        if not found_polish:
                            print(f"⚠️ [SAVE] No polish_stk_no found in batch model for {lot_id}")
                    else:
                        print(f"⚠️ [SAVE] No batch_id found in stock model for {lot_id}")
                        
                        # 🔧 FALLBACK: Try to get plating/polish directly from stock model
                        plating_fields_to_check = [
                            'plating_stk_no', 'plating_stock_no', 'plating_stk_number',
                            'plating_number', 'plat_stk_no', 'plat_stock_no'
                        ]
                        
                        for field_name in plating_fields_to_check:
                            if hasattr(stock_model, field_name):
                                field_value = getattr(stock_model, field_name)
                                if field_value:
                                    all_plating_stk_nos.append(str(field_value))
                                    print(f"✅ [SAVE] Added plating_stk_no: '{field_value}' from stock field '{field_name}' in {lot_id}")
                                    break
                        
                        polish_fields_to_check = [
                            'polish_stk_no', 'polishing_stk_no', 'polish_stock_no', 
                            'polishing_stock_no', 'polish_number', 'pol_stk_no'
                        ]
                        
                        for field_name in polish_fields_to_check:
                            if hasattr(stock_model, field_name):
                                field_value = getattr(stock_model, field_name)
                                if field_value:
                                    all_polish_stk_nos.append(str(field_value))
                                    print(f"✅ [SAVE] Added polish_stk_no: '{field_value}' from stock field '{field_name}' in {lot_id}")
                                    break
                    
                    # Collect version (can be from stock model or batch model)
                    version_display = None
                    
                    # Try stock model first for version
                    if hasattr(stock_model, 'version') and stock_model.version:
                        if hasattr(stock_model.version, 'version_internal'):
                            version_display = stock_model.version.version_internal
                        elif hasattr(stock_model.version, 'version_name'):
                            version_display = stock_model.version.version_name
                        elif hasattr(stock_model.version, 'version'):
                            version_display = stock_model.version.version
                        else:
                            version_display = str(stock_model.version)
                    # Fallback to batch model for version
                    elif batch_model and hasattr(batch_model, 'version') and batch_model.version:
                        if hasattr(batch_model.version, 'version_internal'):
                            version_display = batch_model.version.version_internal
                        elif hasattr(batch_model.version, 'version_name'):
                            version_display = batch_model.version.version_name
                        elif hasattr(batch_model.version, 'version'):
                            version_display = batch_model.version.version
                        else:
                            version_display = str(batch_model.version)
                    
                    if version_display:
                        all_versions.append(str(version_display))
                        print(f"✅ [SAVE] Added version: '{version_display}' from {lot_id}")
                    else:
                        print(f"⚠️ [SAVE] No version found for {lot_id}")
                
                else:
                    print(f"⚠️ [SAVE] No stock model found for lot_id: '{lot_id}'")
                    
            except Exception as e:
                print(f"❌ [SAVE] Error processing lot_id '{lot_id}': {e}")
                import traceback
                traceback.print_exc()
                continue

        print(f"🔍 [SAVE] ===== FINAL COLLECTION RESULTS =====")
        print(f"✅ [SAVE] Collected plating_stk_nos: {all_plating_stk_nos}")
        print(f"✅ [SAVE] Collected polish_stk_nos: {all_polish_stk_nos}")
        print(f"✅ [SAVE] Collected versions: {all_versions}")

        # 🧠 SMART CREATE/UPDATE: Check if we should update existing record or create new
        jig_unload_fter_instance = None
        unload_lot_id = None
        
        if final_db_combined_lot_ids and total_case_qty > 0:
            # Check if there's an existing record we should update
            existing_record = None
            
            # Find record that contains any of these specific formatted lot_ids.
            # Zone 1 stores plain lot_ids in combine_lot_ids while this zone stores
            # prefixed ids (e.g. 'JLOT-xxx-LIDyyy'), so an exact-string containment
            # check never matches a Zone-1-created row for the same source lot_id,
            # causing a duplicate JigUnloadAfterTable row instead of a merge. Compare
            # normalised (plain) lot_ids instead so either zone can find the other's row.
            potential_records = JigUnloadAfterTable.objects.filter(
                combine_lot_ids__isnull=False
            ).exclude(combine_lot_ids__exact=[])

            new_plain_ids = {normalize_combine_lot_id(fid) for fid in final_db_combined_lot_ids}

            for record in potential_records:
                if record.combine_lot_ids:
                    record_plain_ids = {
                        normalize_combine_lot_id(cid) for cid in record.combine_lot_ids
                    }
                    if record_plain_ids & new_plain_ids:
                        existing_record = record
                        break

            if existing_record:
                # 🔄 UPDATE existing record
                # Merge with existing combine_lot_ids (avoid duplicates)
                updated_combine_lot_ids = list(set(existing_record.combine_lot_ids + final_db_combined_lot_ids))
                
                existing_record.combine_lot_ids = updated_combine_lot_ids
                existing_record.total_case_qty = total_case_qty
                existing_record.missing_qty = max(existing_record.missing_qty, missing_qty)
                existing_record.Un_loaded_date_time = timezone.now()
                existing_record.last_process_module = "Jig Unloading"
                existing_record.current_stage = "Jig Unloading"

                # ✅ UPDATE: MERGE COLLECTED VALUES AS LISTS
                if all_plating_stk_nos:
                    existing_plating = existing_record.plating_stk_no_list or []
                    merged_plating = list(set(existing_plating + all_plating_stk_nos))
                    existing_record.plating_stk_no_list = merged_plating
                    
                if all_polish_stk_nos:
                    existing_polish = existing_record.polish_stk_no_list or []
                    merged_polish = list(set(existing_polish + all_polish_stk_nos))
                    existing_record.polish_stk_no_list = merged_polish
                    
                if all_versions:
                    existing_versions = existing_record.version_list or []
                    merged_versions = list(set(existing_versions + all_versions))
                    existing_record.version_list = merged_versions
                
                existing_record.save()
                
                # 🧠 SMART POPULATE: Auto-fill other fields from TotalStockModel (for existing records too)
                populate_jig_unload_fields(existing_record, cleaned_combined_lot_ids, jig_lot_id)
                
                # Save again after population to ensure plating_color and other fields are persisted
                existing_record.save()
                
                jig_unload_fter_instance = existing_record
                unload_lot_id = str(existing_record.lot_id)
                print(f"[SMART SAVE] 🔄 Updated existing record {existing_record.id}")
                print(f"[SMART SAVE] 📋 Final combine_lot_ids: {updated_combine_lot_ids}")
                print(f"[SMART SAVE] ✅ Refreshed plating color and other fields from TotalStockModel")
                print(f"[SMART SAVE] 🎨 Final plating_color: {existing_record.plating_color}")
                
                # 🔧 CRITICAL: Also check if we need to populate plating_color for existing record
                if not existing_record.plating_color:
                    print(f"[SMART SAVE] ⚠️ Existing record {existing_record.id} has no plating_color, attempting to populate...")
                    # Try to get plating_color from JigCompleted associated with any of the lot_ids
                    for lot_id in cleaned_combined_lot_ids:
                        try:
                            jig_detail = JigCompleted.objects.filter(
                                draft_data__lot_id_quantities__has_key=lot_id
                            ).first()
                            if jig_detail and jig_detail.draft_data.get('plating_color'):
                                # Convert string to Plating_Color object
                                plating_color_obj = Plating_Color.objects.get(plating_color=jig_detail.draft_data.get('plating_color'))
                                existing_record.plating_color = plating_color_obj
                                existing_record.save()
                                print(f"[SMART SAVE] ✅ Fixed plating_color for existing record: {plating_color_obj.plating_color}")
                                break
                        except (Plating_Color.DoesNotExist, Exception) as e:
                            print(f"[SMART SAVE] ⚠️ Error fixing plating_color for lot {lot_id}: {e}")
                            continue
                
            else:
                # 🆕 CREATE new record
                try:
                    # ✅ CREATE WITH PRESERVED JIG-LOT MAPPING
                    create_data = {
                        'combine_lot_ids': final_db_combined_lot_ids,  # 🔧 FIXED: Use preserved mapping
                        'total_case_qty': total_case_qty,
                        'missing_qty': missing_qty,
                        'Un_loaded_date_time': timezone.now(),
                        'last_process_module': "Jig Unloading",
                        'current_stage': "Jig Unloading"
                    }
                    
                    # ✅ ADD COLLECTED VALUES AS LISTS
                    if all_plating_stk_nos:
                        create_data['plating_stk_no_list'] = all_plating_stk_nos
                    if all_polish_stk_nos:
                        create_data['polish_stk_no_list'] = all_polish_stk_nos
                    if all_versions:
                        create_data['version_list'] = all_versions
                    
                    jig_unload_fter_instance = JigUnloadAfterTable.objects.create(**create_data)
                    
                    # 🧠 SMART POPULATE: Auto-fill other fields from TotalStockModel
                    populate_jig_unload_fields(jig_unload_fter_instance, cleaned_combined_lot_ids, jig_lot_id)
                    
                    # � DEBUG: Check fields immediately after population
                    print(f"[DEBUG ZONE2] 🔍 IMMEDIATELY AFTER POPULATE:")
                    print(f"[DEBUG ZONE2] 🎯 jig_qr_id: '{jig_unload_fter_instance.jig_qr_id}'")
                    print(f"[DEBUG ZONE2] 📝 version: '{jig_unload_fter_instance.version}'")
                    print(f"[DEBUG ZONE2] 🏷️ plating_stk_no: '{jig_unload_fter_instance.plating_stk_no}'")
                    print(f"[DEBUG ZONE2] ✨ polish_stk_no: '{jig_unload_fter_instance.polish_stk_no}'")
                    print(f"[DEBUG ZONE2] 💎 polish_finish: '{jig_unload_fter_instance.polish_finish}'")
                    print(f"[DEBUG ZONE2] 📂 category: '{jig_unload_fter_instance.category}'")
                    print(f"[DEBUG ZONE2] 📦 tray_type: '{jig_unload_fter_instance.tray_type}'")
                    print(f"[DEBUG ZONE2] 🔢 tray_capacity: '{jig_unload_fter_instance.tray_capacity}'")
                    print(f"[DEBUG ZONE2] 🎨 plating_color: '{jig_unload_fter_instance.plating_color}'")
                    
                    # �🔧 FIX: Save again after population to ensure all fields are persisted
                    jig_unload_fter_instance.save()
                    
                    # 🔍 CRITICAL DEBUG: Check what was actually saved to database
                    jig_unload_fter_instance.refresh_from_db()
                    print(f"[CRITICAL DEBUG Zone2] 🔍 AFTER FINAL SAVE - Database verification:")
                    print(f"[CRITICAL DEBUG Zone2] 🎯 jig_qr_id: '{jig_unload_fter_instance.jig_qr_id}'")
                    print(f"[CRITICAL DEBUG Zone2] 📝 version: '{jig_unload_fter_instance.version}'")
                    print(f"[CRITICAL DEBUG Zone2] 🏷️ plating_stk_no: '{jig_unload_fter_instance.plating_stk_no}'")
                    print(f"[CRITICAL DEBUG Zone2] ✨ polish_stk_no: '{jig_unload_fter_instance.polish_stk_no}'")
                    print(f"[CRITICAL DEBUG Zone2] 💎 polish_finish: '{jig_unload_fter_instance.polish_finish}'")
                    print(f"[CRITICAL DEBUG Zone2] 📂 category: '{jig_unload_fter_instance.category}'")
                    print(f"[CRITICAL DEBUG Zone2] 📦 tray_type: '{jig_unload_fter_instance.tray_type}'")
                    print(f"[CRITICAL DEBUG Zone2] 🔢 tray_capacity: '{jig_unload_fter_instance.tray_capacity}'")
                    print(f"[CRITICAL DEBUG Zone2] 🎨 plating_color: '{jig_unload_fter_instance.plating_color}'")
                    
                    unload_lot_id = str(jig_unload_fter_instance.lot_id)
                    print(f"[SMART SAVE] 🆕 Created new record {jig_unload_fter_instance.id}")
                    print(f"[SMART SAVE] 📋 Saved combine_lot_ids: {final_db_combined_lot_ids}")
                    print(f"[SMART SAVE] ✅ Saved plating_stk_no_list: {all_plating_stk_nos}")
                    print(f"[SMART SAVE] ✅ Saved polish_stk_no_list: {all_polish_stk_nos}")
                    print(f"[SMART SAVE] ✅ Saved version_list: {all_versions}")
                    print(f"[SMART SAVE] 🎨 Final plating_color: {jig_unload_fter_instance.plating_color}")
                    
                    # 🔧 CRITICAL: Also check if we need to populate plating_color for new record
                    if not jig_unload_fter_instance.plating_color:
                        print(f"[PLATING COLOR FALLBACK] ⚠️ New record {jig_unload_fter_instance.id} has no plating_color, attempting to populate...")
                        # Try to get plating_color from JigCompleted associated with any of the lot_ids
                        for lot_id in cleaned_combined_lot_ids:
                            try:
                                jig_detail = JigCompleted.objects.filter(
                                    draft_data__lot_id_quantities__has_key=lot_id
                                ).first()
                                if jig_detail and jig_detail.draft_data.get('plating_color'):
                                    # Convert string to Plating_Color object
                                    plating_color_obj = Plating_Color.objects.get(plating_color=jig_detail.draft_data.get('plating_color'))
                                    jig_unload_fter_instance.plating_color = plating_color_obj
                                    
                                    # 🔍 DEBUG: Check other fields before fallback save
                                    print(f"[PLATING COLOR FALLBACK] 🔍 BEFORE fallback save:")
                                    print(f"[PLATING COLOR FALLBACK] 🎯 jig_qr_id: '{jig_unload_fter_instance.jig_qr_id}'")
                                    print(f"[PLATING COLOR FALLBACK] 📝 version: '{jig_unload_fter_instance.version}'")
                                    print(f"[PLATING COLOR FALLBACK] 🏷️ plating_stk_no: '{jig_unload_fter_instance.plating_stk_no}'")
                                    
                                    jig_unload_fter_instance.save()
                                    
                                    # 🔍 DEBUG: Check other fields after fallback save
                                    jig_unload_fter_instance.refresh_from_db()
                                    print(f"[PLATING COLOR FALLBACK] 🔍 AFTER fallback save:")
                                    print(f"[PLATING COLOR FALLBACK] 🎯 jig_qr_id: '{jig_unload_fter_instance.jig_qr_id}'")
                                    print(f"[PLATING COLOR FALLBACK] 📝 version: '{jig_unload_fter_instance.version}'")
                                    print(f"[PLATING COLOR FALLBACK] 🏷️ plating_stk_no: '{jig_unload_fter_instance.plating_stk_no}'")
                                    
                                    print(f"[PLATING COLOR FALLBACK] ✅ Fixed plating_color for new record: {plating_color_obj.plating_color}")
                                    break
                            except (Plating_Color.DoesNotExist, Exception) as e:
                                print(f"[PLATING COLOR FALLBACK] ⚠️ Error fixing plating_color for lot {lot_id}: {e}")
                                continue
                    else:
                        print(f"[PLATING COLOR FALLBACK] ✅ Plating color already populated: {jig_unload_fter_instance.plating_color}")
                    
                except Exception as e:
                    logger.error(f"Error creating JigUnloadAfterTable: {str(e)}")
                    return JsonResponse({'success': False, 'error': 'Unable to process the request. Please verify the submitted data and try again.'})

        # Fallback: if all IDs were duplicates (already in DB), find the existing record.
        # Uses the shared normalize_combine_lot_id() (handles both the ':'-separated
        # and 'JLOT-...-LIDyyy' formats) rather than a plain rsplit('-', 1), which
        # mis-splits the ':'-separated variant (e.g. 'JLOT-xxx:LIDyyy') on the hyphen
        # inside 'JLOT-xxx' and never reaches the plain lot_id after the colon.
        if not unload_lot_id:
            _search_lot_ids = [
                normalize_combine_lot_id(fid)
                for fid in (final_formatted_combined_lot_ids or list(all_lot_ids_from_trays))
            ]
            for _record in JigUnloadAfterTable.objects.filter(
                combine_lot_ids__isnull=False
            ).exclude(combine_lot_ids__exact=[]).order_by('-id'):
                if _record.combine_lot_ids:
                    _stored_lids = [
                        normalize_combine_lot_id(cid)
                        for cid in _record.combine_lot_ids
                    ]
                    if any(sl in _stored_lids for sl in _search_lot_ids):
                        jig_unload_fter_instance = _record
                        unload_lot_id = str(_record.lot_id)
                        print(f"[SMART SAVE] ♻️ Re-using existing record id={_record.id} (all IDs already stored)")
                        break

        if not unload_lot_id:
            return JsonResponse({'success': False, 'error': 'Failed to generate unload_lot_id'})

        # Update trays (existing logic)
        unload_lot_id = str(jig_unload_fter_instance.lot_id)
        saved_trays = []
        for i, tray in enumerate(trays):
            tray_id = tray.get('tray_id')
            tray_qty = tray.get('tray_qty')
            original_lot_id = tray.get('lot_id')
            is_top_tray = tray.get('is_top_tray', False)  # Get top_tray flag from frontend

            # Update TrayId
            try:
                tray_obj = TrayId.objects.get(tray_id=tray_id)
                tray_obj.lot_id = unload_lot_id
                tray_obj.tray_quantity = tray_qty
                tray_obj.save(update_fields=['lot_id', 'tray_quantity'])
            except TrayId.DoesNotExist:
                logger.warning(f"TrayId '{tray_id}' does not exist")

            # Update JigUnload_TrayId
            jig_unload_tray, created = JigUnload_TrayId.objects.update_or_create(
                tray_id=tray_id,
                lot_id=unload_lot_id,
                defaults={
                    'tray_qty': tray_qty,
                    'top_tray': is_top_tray
                }
            )

            saved_trays.append({
                'tray_id': tray_id,
                'original_lot_id': original_lot_id,
                'unload_lot_id': unload_lot_id,
                'tray_qty': tray_qty,
                'top_tray': is_top_tray,  # Include in response
                'created': created
            })

        # Update JigCompleted (existing logic)
        if cleaned_combined_lot_ids:
            try:
                jig_detail = None
                for lot_id in cleaned_combined_lot_ids:
                    potential_jig_details = JigCompleted.objects.filter(draft_data__lot_id_quantities__has_key=lot_id)
                    if potential_jig_details.exists():
                        jig_detail = potential_jig_details.first()
                        break
                
                if jig_detail:
                    jig_detail.combined_lot_ids = cleaned_combined_lot_ids
                    jig_detail.last_process_module = "Jig Unloading"
                    jig_detail.save()
                    print(f"[SMART SAVE] ✅ Updated JigCompleted {jig_detail.id}")
            except Exception as e:
                logger.error(f"Error updating JigCompleted: {str(e)}")
        
        # Update Inprocess Inspection completed table records
        if cleaned_combined_lot_ids:
            try:
                # Zone 2 work is the first real downstream action after
                # Inprocess Inspection. Keep the shared stage SSOT in sync.
                from modelmasterapp.stage_service import update_stock_stage
                for _lot_id in cleaned_combined_lot_ids:
                    try:
                        update_stock_stage(_lot_id, 'Jig Unloading')
                    except Exception:
                        logger.exception(
                            'Zone 2 current_stage update failed for lot_id=%s',
                            _lot_id,
                        )

                from django.db.models import Q
                # Find all JigCompleted records that contain any of the unloaded lot_ids
                affected_jig_details = JigCompleted.objects.filter(
                    Q(draft_data__lot_id_quantities__has_any_keys=cleaned_combined_lot_ids) |
                    Q(lot_id__in=cleaned_combined_lot_ids)
                )
                
                # Update last_process_module to "Jig Unloading" for all affected records
                updated_count = affected_jig_details.update(last_process_module="Jig Unloading")
                
                # 🆕 UPDATE: Update Models Present field (no_of_model_cases) based on remaining lot_ids
                for jig_detail in affected_jig_details:
                    try:
                        # Get all remaining lot_ids in this jig after unloading
                        remaining_lot_ids = list((jig_detail.draft_data or {}).get('lot_id_quantities', {}).keys())
                        
                        # Remove the unloaded lot_ids from remaining_lot_ids
                        remaining_lot_ids = [lot_id for lot_id in remaining_lot_ids if lot_id not in cleaned_combined_lot_ids]
                        
                        print(f"[MODELS UPDATE] Jig {jig_detail.id} - Remaining lot_ids after unload: {remaining_lot_ids}")
                        
                        # Map remaining lot_ids to model numbers
                        remaining_model_numbers = set()
                        
                        for lot_id in remaining_lot_ids:
                            try:
                                # Get stock model data for this lot_id
                                stock_model, is_recovery, batch_model_class = get_stock_model_data_for_save(lot_id)
                                
                                if stock_model and stock_model.batch_id:
                                    # Get the model number from the batch
                                    batch_model = batch_model_class.objects.filter(id=stock_model.batch_id.id).first()
                                    if batch_model and hasattr(batch_model, 'model_stock_no') and batch_model.model_stock_no:
                                        model_no = batch_model.model_stock_no.model_no
                                        if model_no:
                                            remaining_model_numbers.add(model_no)
                                            print(f"[MODELS UPDATE] Found model '{model_no}' for lot_id '{lot_id}'")
                            
                            except Exception as e:
                                print(f"[MODELS UPDATE] Error getting model for lot_id '{lot_id}': {e}")
                                continue
                        
                        # Update no_of_model_cases with remaining models
                        remaining_model_list = list(remaining_model_numbers)
                        jig_detail.no_of_model_cases = remaining_model_list
                        jig_detail.save(update_fields=['no_of_model_cases'])
                        
                        print(f"[MODELS UPDATE] ✅ Updated no_of_model_cases for jig {jig_detail.id}: {remaining_model_list}")
                        
                    except Exception as e:
                        print(f"[MODELS UPDATE] Error updating no_of_model_cases for jig {jig_detail.id}: {e}")
                
                print(f"[SMART SAVE] ✅ Updated {updated_count} Inprocess Inspection completed table records")
                
            except Exception as e:
                logger.error(f"Error updating Inprocess Inspection completed table: {str(e)}")
        
        # ✅ ENHANCED JIG RELEASE LOGIC - Release jig QR ID after successful unloading with data consistency fix
        # This implements the user's requirement: "unload func is to release the jig qr id and making it free to use for next cycle so once unloaded - is_loaded - will be unchecked"
        if jig_lot_id and jig_unload_fter_instance:
            try:
                from django.db.models import Q
                # Mark the corresponding JigCompleted record as unloaded
                jig_details_to_release = JigCompleted.objects.filter(
                    Q(draft_data__lot_id_quantities__has_any_keys=cleaned_combined_lot_ids) |
                    Q(lot_id__in=cleaned_combined_lot_ids)
                ).exclude(last_process_module='Jig Unloading')
                
                released_jig_details_count = 0
                jig_qr_ids_to_release = set()  # Collect all jig QR IDs that need to be released
                
                for jig_detail in jig_details_to_release:
                    jig_detail.last_process_module = 'Jig Unloading'
                    jig_detail.save(update_fields=['last_process_module'])
                    released_jig_details_count += 1
                    
                    # Collect jig QR ID for release
                    if jig_detail.jig_qr_id:
                        jig_qr_ids_to_release.add(jig_detail.jig_qr_id)
                    
                    print(f"[JIG RELEASE Zone2] ✅ Set unload_over=True for JigCompleted {jig_detail.id}")
                
                # Extract JIG QR ID from jig_lot_id (remove JLOT- prefix) as primary method
                primary_jig_qr_id = jig_lot_id
                if primary_jig_qr_id.startswith('JLOT-'):
                    primary_jig_qr_id = primary_jig_qr_id[5:]  # Remove 'JLOT-' prefix
                
                # Add primary jig QR ID to the release set
                jig_qr_ids_to_release.add(primary_jig_qr_id)
                
                # Release all collected Jig QR IDs
                total_released_jigs_count = 0
                for jig_qr_id in jig_qr_ids_to_release:
                    if not jig_qr_id:
                        continue
                        
                    print(f"[JIG RELEASE Zone2] 🔍 Processing Jig QR ID: '{jig_qr_id}'")
                    
                    # 🔧 ENHANCED: Data consistency check and fix
                    # Check if the jig exists but is incorrectly marked as not loaded
                    jig_obj = Jig.objects.filter(jig_qr_id=jig_qr_id).first()
                    if jig_obj and not jig_obj.is_loaded:
                        # Check if there are corresponding active JigCompleted
                        active_jig_details = JigCompleted.objects.filter(
                            jig_qr_id=jig_qr_id, 
                            unload_over=False
                        ).exists()
                        
                        if active_jig_details:
                            print(f"[JIG RELEASE Zone2] 🔧 Data inconsistency detected: JigCompleted '{jig_qr_id}' is active but Jig is not loaded")
                            print(f"[JIG RELEASE Zone2] 🔧 Auto-fixing: Setting Jig '{jig_qr_id}' as loaded before release")
                            jig_obj.is_loaded = True
                            jig_obj.save(update_fields=['is_loaded'])
                    
                    # Now proceed with normal release logic
                    # Filter by jig_qr_id only (not is_loaded=True) so jigs left
                    # stuck with drafted/occupied_flag still True (e.g. from a
                    # prior partial release) are also normalized on this pass.
                    released_jigs = Jig.objects.filter(jig_qr_id=jig_qr_id)
                    released_jigs_count = released_jigs.count()
                    
                    if released_jigs_count > 0:
                        # Update all matching jigs - mark as free, increment cycle count
                        from django.db.models import F
                        released_jigs.update(
                            is_loaded=False,
                            occupied_flag=False,
                            drafted=False,
                            current_user=None,
                            locked_at=None,
                            batch_id=None,
                            lot_id=None,
                            cycle_count=F('cycle_count') + 1
                        )
                        total_released_jigs_count += released_jigs_count
                        print(f"[JIG RELEASE Zone2] ✅ Released {released_jigs_count} Jig QR ID(s) '{jig_qr_id}' (set is_loaded=False, occupied_flag=False, drafted=False, cycle_count+1)")
                        
                        # Log each released jig for tracking with cycle count
                        for jig in released_jigs:
                            jig.refresh_from_db()  # Get updated cycle_count
                            print(f"[JIG RELEASE Zone2] ✅ Jig ID {jig.id} - QR: '{jig.jig_qr_id}' is now available for reuse (Cycle: {jig.cycle_count})")
                    else:
                        print(f"[JIG RELEASE Zone2] ⚠️ No loaded Jig found with QR ID '{jig_qr_id}' to release")
                
                print(f"[JIG RELEASE Zone2] ✅ Jig release completed - {total_released_jigs_count} total jigs released, {released_jig_details_count} JigCompleted marked as unloaded")
                
                # Store release info for response
                released_jigs_count = total_released_jigs_count
                
            except Exception as e:
                logger.error(f"Error releasing jig QR ID: {str(e)}")
                print(f"[JIG RELEASE Zone2] ❌ Failed to release jig QR ID '{jig_lot_id}': {str(e)}")
                import traceback
                traceback.print_exc()
                # Don't fail the entire operation if jig release fails
        
        print(f"[SMART SAVE] ✅ Successfully saved {len(saved_trays)} trays with PRESERVED jig mappings")
        
        return JsonResponse({
            'success': True,
            'message': f'Smart saved {len(saved_trays)} tray records with preserved jig mappings - {released_jigs_count if "released_jigs_count" in locals() else 0} Jig QR ID(s) Released',
            'saved_trays': saved_trays,
            'unload_lot_id': unload_lot_id,
            'jig_release_info': {
                'jig_qr_ids_released': list(jig_qr_ids_to_release) if 'jig_qr_ids_to_release' in locals() else [primary_jig_qr_id] if 'primary_jig_qr_id' in locals() else [],
                'jigs_released_count': released_jigs_count if 'released_jigs_count' in locals() else 0,
                'jig_details_marked_unloaded': released_jig_details_count if 'released_jig_details_count' in locals() else 0
            },
            'preserved_mappings': {
                'jig_aware_sources': jig_aware_sources,
                'final_formatted_ids': final_db_combined_lot_ids
            },
            'collected_values': {
                'plating_stk_nos': all_plating_stk_nos,
                'polish_stk_nos': all_polish_stk_nos,
                'versions': all_versions
            },
            'jig_unload_fter_table': {
                'created': bool(jig_unload_fter_instance),
                'id': jig_unload_fter_instance.id if jig_unload_fter_instance else None,
                'combine_lot_ids': jig_unload_fter_instance.combine_lot_ids if jig_unload_fter_instance else [],
                'total_case_qty': jig_unload_fter_instance.total_case_qty if jig_unload_fter_instance else 0
            }
        })

    except Exception as e:
        logger.error(f'Unexpected error: {str(e)}', exc_info=True)
        return JsonResponse({'success': False, 'error': 'Unable to process the request. Please verify the submitted data and try again.'})


@login_required
@csrf_exempt
@require_POST
def JU_Zone_save_jig_unload_draft(request):
    """
    Save draft data for Jig Unload - stores all data as JSON
    """
    import json
    import logging
    
    logger = logging.getLogger(__name__)
    
    try:
        data = json.loads(request.body)
        
        # Extract main data
        
        main_lot_id = data.get('main_lot_id', '')
        model_number = data.get('model_number', '')
        total_quantity = data.get('total_quantity', 0)
        trays = data.get('trays', [])
        combined_lot_ids = data.get('combined_lot_ids', [])
        
        logger.info(f"Saving draft for model: {model_number}, lot_id: {main_lot_id}")
        
        if not main_lot_id or not model_number or not trays:
            return JsonResponse({
                'success': False, 
                'error': 'Missing required data: main_lot_id, model_number, or trays'
            })

        allowed_lot_ids_for_trays = [main_lot_id] + list(combined_lot_ids or [])
        allowed_lot_ids_for_trays.extend(
            tray.get('lot_id') for tray in trays if tray.get('lot_id')
        )
        seen_tray_ids = set()
        for i, tray in enumerate(trays):
            tray_id = normalize_jig_unload_tray_id(tray.get('tray_id', ''))
            if not tray_id:
                continue
            tray['tray_id'] = tray_id
            if not is_valid_jig_unload_tray_id_format(tray_id):
                return JsonResponse({
                    'success': False,
                    'error': f'Tray ID "{tray_id}" has invalid format. Expected format: XX-A00001'
                }, status=400)
            if tray_id in seen_tray_ids:
                return JsonResponse({
                    'success': False,
                    'error': f'Duplicate tray ID "{tray_id}" in draft. Each tray must be unique.'
                }, status=400)
            seen_tray_ids.add(tray_id)
            tray_conflict = find_jig_unload_tray_conflict(
                tray_id,
                allowed_lot_ids=allowed_lot_ids_for_trays,
                include_tray_master=True,
            )
            if tray_conflict:
                return JsonResponse({
                    'success': False,
                    'error': tray_conflict['message'],
                    'validation_type': 'tray_occupied',
                    'linked_lot': tray_conflict.get('linked_lot', ''),
                    'source': tray_conflict.get('source', ''),
                    'tray_index': i,
                }, status=400)
            nickel_conflict = validate_jig_unload_nickel_reject_tray(
                tray_id,
                current_lot_id=tray.get('lot_id') or main_lot_id or None,
            )
            if nickel_conflict:
                return JsonResponse({
                    'success': False,
                    'error': nickel_conflict['message'],
                    'validation_type': 'tray_occupied',
                    'linked_lot': nickel_conflict.get('linked_lot', ''),
                    'source': nickel_conflict.get('source', ''),
                    'tray_index': i,
                }, status=400)
        
        # Prepare draft data JSON
        draft_data = {
            'model_number': model_number,
            'main_lot_id': main_lot_id,
            'total_quantity': total_quantity,
            'tray_data': [],
            'tray_type_capacity': data.get('tray_type_capacity', 'Normal - 20'),
            'created_timestamp': timezone.now().isoformat(),
            'combined_lot_ids': combined_lot_ids
        }
        
        # Process tray data
        for i, tray in enumerate(trays):
            tray_entry = {
                'sno': i + 1,
                'tray_id': tray.get('tray_id', ''),
                'tray_qty': tray.get('tray_qty', 0),
                'lot_id': tray.get('lot_id', main_lot_id),
                'is_top_tray': i == 0
            }
            draft_data['tray_data'].append(tray_entry)
        
        # Save or update draft
        draft, created = JigUnloadDraft.objects.update_or_create(
            main_lot_id=main_lot_id,
            model_number=model_number,
            defaults={
                'total_quantity': total_quantity,
                'draft_data': draft_data,
                'combined_lot_ids': combined_lot_ids,
                'created_by': 'System'  # You can get from request.user if available
            }
        )
        
        logger.info(f"{'Created' if created else 'Updated'} draft with ID: {draft.draft_id}")
        
        return JsonResponse({
            'success': True,
            'message': f'Draft {"saved" if created else "updated"} successfully',
            'draft_id': draft.draft_id,
            'model_number': model_number,
            'total_quantity': total_quantity,
            'tray_count': len(trays)
        })
        
    except json.JSONDecodeError as e:
        logger.error(f'JSON decode error: {e}')
        return JsonResponse({'success': False, 'error': 'Invalid JSON data'})
    except Exception as e:
        logger.error(f'Unexpected error: {e}', exc_info=True)
        return JsonResponse({'success': False, 'error': 'Unable to process the request. Please verify the submitted data and try again.'})

@login_required
@csrf_exempt
def JU_Zone_load_jig_unload_draft(request):
    """Load draft data for Jig Unload based on main_lot_id"""
    try:
        main_lot_id = request.GET.get('main_lot_id')
        if not main_lot_id:
            return JsonResponse({'success': False, 'error': 'main_lot_id required'})
        
        draft = JigUnloadDraft.objects.filter(main_lot_id=main_lot_id).first()
        
        if draft:
            return JsonResponse({
                'success': True,
                'has_draft': True,
                'draft_data': draft.draft_data,
                'draft_id': draft.draft_id
            })
        else:
            return JsonResponse({
                'success': True,
                'has_draft': False
            })
            
    except Exception as e:
        return JsonResponse({'success': False, 'error': 'Unable to process the request. Please verify the submitted data and try again.'})


def get_plating_stock_for_lot(lot_id):
    """
    Retrieve plating stock number for a given lot_id
    Searches in: TotalStockModel, RecoveryStockModel, JigCompleted
    """
    try:
        # Try TotalStockModel first
        tsm = TotalStockModel.objects.select_related('model_stock_no').filter(lot_id=lot_id).first()
        if tsm and getattr(tsm.model_stock_no, 'plating_stk_no', None):
            return tsm.model_stock_no.plating_stk_no
        
        # Try RecoveryStockModel
        rsm = RecoveryStockModel.objects.select_related('model_stock_no').filter(lot_id=lot_id).first()
        if rsm and getattr(rsm.model_stock_no, 'plating_stk_no', None):
            return rsm.model_stock_no.plating_stk_no
        
        # Try JigCompleted (for Jig Loading lot IDs with draft_data)
        jc = JigCompleted.objects.filter(lot_id=lot_id).first()
        if jc and hasattr(jc, 'draft_data') and jc.draft_data:
            draft_data = jc.draft_data or {}
            plating_stock = draft_data.get('plating_stock_no') or draft_data.get('plating_stock_num')
            if plating_stock:
                return plating_stock
            batch_id = draft_data.get('batch_id') or getattr(jc, 'batch_id', None)
            if batch_id:
                mmc = ModelMasterCreation.objects.filter(batch_id=batch_id).first()
                if mmc and mmc.plating_stk_no:
                    return mmc.plating_stk_no
        
        return None
    except Exception as e:
        print(f"[DEBUG] Error getting plating stock for lot {lot_id}: {e}")
        return None


def validate_tray_code_by_master_data(tray_id, lot_id):
    """
    Validate tray code against master data mapping
    Returns: (is_valid: bool, message: str, allowed_codes: list, zone: str)
    """
    import re
    
    # Extract tray code prefix from tray_id (e.g., "NR" from "NR-A00001")
    match = re.match(r'^([A-Z]+)-A\d{5}$', tray_id)
    if not match:
        return False, f"Invalid tray format. Expected format: XX-A00001", [], None
    
    tray_code_prefix = match.group(1)
    
    # Get plating stock number for this lot
    plating_stock = get_plating_stock_for_lot(lot_id)
    if not plating_stock:
        print(f"[DEBUG] Zone 2 - No plating stock found for lot {lot_id}, allowing all non-IPS prefixes")
        # Fallback: allow all non-IPS prefixes (ND-, JD-, NL-, JL-, NB-, JB-)
        return True, f"Tray validation skipped (plating stock unknown), allowing {tray_code_prefix}", ['ND', 'JD', 'NL', 'JL', 'NB', 'JB'], 'Zone2'
    
    # Validate against master data
    is_valid, message, tray_info = validate_tray_code_for_stock(tray_code_prefix, plating_stock)
    
    if not is_valid:
        allowed_codes = tray_info.get('tray_codes', []) if tray_info else []
        zone = tray_info.get('zone', 'Zone2') if tray_info else 'Zone2'
        return False, message, allowed_codes, zone
    
    # Valid - return the allowed codes from master data
    allowed_codes = tray_info.get('tray_codes', [tray_code_prefix]) if tray_info else [tray_code_prefix]
    zone = tray_info.get('zone', 'Zone2') if tray_info else 'Zone2'
    
    return True, f"Tray code {tray_code_prefix} is valid for plating stock {plating_stock}", allowed_codes, zone

    
@login_required
@csrf_exempt
@require_POST
def JU_Zone_validate_tray_id(request):
    import json
    data = json.loads(request.body)
    tray_id = normalize_jig_unload_tray_id(data.get('tray_id', ''))
    lot_id = data.get('lot_id', '').strip()

    print(f"[DEBUG] JU_Zone_validate_tray_id called with tray_id: '{tray_id}', lot_id: '{lot_id}'")

    if not tray_id:
        return JsonResponse({'success': False, 'error': 'Tray ID required.'})

    if not is_valid_jig_unload_tray_id_format(tray_id):
        return JsonResponse({
            'success': False,
            'error': f'Tray ID "{tray_id}" has invalid format. Expected format: XX-A00001'
        })

    allowed_lot_ids_for_trays = [lid.strip() for lid in lot_id.split(',') if lid.strip()] if lot_id else []
    tray_conflict = find_jig_unload_tray_conflict(
        tray_id,
        allowed_lot_ids=allowed_lot_ids_for_trays,
        include_tray_master=True,
    )
    if tray_conflict:
        return JsonResponse({
            'success': False,
            'error': tray_conflict['message'],
            'validation_type': 'tray_occupied',
            'linked_lot': tray_conflict.get('linked_lot', ''),
            'source': tray_conflict.get('source', ''),
        })
    nickel_conflict = validate_jig_unload_nickel_reject_tray(
        tray_id,
        current_lot_id=lot_id or None,
    )
    if nickel_conflict:
        return JsonResponse({
            'success': False,
            'error': nickel_conflict['message'],
            'validation_type': 'tray_occupied',
            'linked_lot': nickel_conflict.get('linked_lot', ''),
            'source': nickel_conflict.get('source', ''),
        })

    try:
        tray = TrayId.objects.get(tray_id=tray_id)
        tray_tray_type = tray.tray_type if tray.tray_type else "Unknown"
        print(f"[DEBUG] Tray '{tray_id}' tray_type: {tray_tray_type}")

        # 🔧 FIXED: Handle combined lot_ids (comma-separated)
        lot_tray_type = None
        
        # Check if lot_id is combined (contains comma)
        if ',' in lot_id:
            # Split combined lot_ids and try each one
            individual_lot_ids = [lid.strip() for lid in lot_id.split(',')]
            print(f"[DEBUG] Combined lot_id detected. Individual lot_ids: {individual_lot_ids}")
            
            for individual_lot_id in individual_lot_ids:
                jig_load_tray = JigLoadTrayId.objects.filter(lot_id=individual_lot_id).first()
                if jig_load_tray and hasattr(jig_load_tray, 'tray_type') and jig_load_tray.tray_type:
                    lot_tray_type = jig_load_tray.tray_type
                    print(f"[DEBUG] Found tray_type '{lot_tray_type}' from individual lot_id: '{individual_lot_id}'")
                    break
            
            if not lot_tray_type:
                print(f"[DEBUG] No tray_type found in JigLoadTrayId for any individual lot_id")
        else:
            # Single lot_id - original logic
            jig_load_tray = JigLoadTrayId.objects.filter(lot_id=lot_id).first()
            if jig_load_tray and hasattr(jig_load_tray, 'tray_type') and jig_load_tray.tray_type:
                lot_tray_type = jig_load_tray.tray_type
            print(f"[DEBUG] Single lot_id '{lot_id}' tray_type from JigLoadTrayId: {lot_tray_type}")

        # 🔧 ENHANCED: More flexible tray type comparison
        if lot_tray_type:
            # Normalize tray types for comparison (case-insensitive, strip spaces)
            tray_type_normalized = str(tray_tray_type).strip().lower()
            lot_type_normalized = str(lot_tray_type).strip().lower()
            
            print(f"[DEBUG] Comparing tray types: '{tray_type_normalized}' vs '{lot_type_normalized}'")
            
            if tray_type_normalized != lot_type_normalized:
                return JsonResponse({
                    'success': False, 
                    'error': f'Tray type mismatch: Tray is {tray_tray_type}, but lot requires {lot_tray_type}.'
                })
            else:
                print(f"[DEBUG] ✅ Tray types match: {tray_tray_type}")
        else:
            print(f"[DEBUG] ⚠️ No lot tray_type found to compare with")

        # 🔧 ENHANCED: Check if tray is available
        print(f"[DEBUG] Tray status check:")
        print(f"   tray.lot_id: {tray.lot_id}")
        print(f"   tray.delink_tray: {tray.delink_tray}")
        print(f"   tray.scanned: {getattr(tray, 'scanned', 'N/A')}")

        # Tray availability logic
        if tray.lot_id is None:
            print(f"[DEBUG] ✅ Tray available: lot_id is None")
            return JsonResponse({'success': True, 'message': 'Tray available - not assigned'})
        elif tray.delink_tray:
            print(f"[DEBUG] ✅ Tray available: delinked and reusable")
            return JsonResponse({'success': True, 'message': 'Tray available - delinked'})
        elif lot_id:
            # For Jig Unloading: tray assigned to the current lot being unloaded is VALID
            lot_ids_to_check = [lid.strip() for lid in lot_id.split(',')] if ',' in lot_id else [lot_id.strip()]
            tray_lot_str = str(tray.lot_id).strip()
            if tray_lot_str in lot_ids_to_check:
                print(f"[DEBUG] ✅ Tray valid for unloading: assigned to current lot {tray.lot_id}")
                return JsonResponse({'success': True, 'message': f'Tray valid - assigned to current lot {tray.lot_id}'})
            else:
                print(f"[DEBUG] ❌ Tray assigned to a different lot: {tray.lot_id}")
                return JsonResponse({
                    'success': False,
                    'error': f'Tray is already assigned to a different lot {tray.lot_id}.'
                })
        else:
            print(f"[DEBUG] ❌ Tray not available: already assigned to lot_id {tray.lot_id}")
            return JsonResponse({
                'success': False, 
                'error': f'Tray is already assigned to lot {tray.lot_id}. Please use a different tray or delink this one first.'
            })
            
    except TrayId.DoesNotExist:
        print(f"[DEBUG] ❌ Tray '{tray_id}' not found in TrayId table")
        return JsonResponse({'success': False, 'error': 'Tray ID not found in system.'})
    except Exception as e:
        logger.error(f"[DEBUG] ❌ Unexpected error: {str(e)}", exc_info=True)
        import traceback
        traceback.print_exc()
        return JsonResponse({'success': False, 'error': 'Unable to process the request. Please verify the submitted data and try again.'})


@login_required
@csrf_exempt
@require_POST
def JU_Zone_validate_tray_id_dynamic(request):
    """
    Enhanced Dynamic Tray Validation API for Jig Unloading Zone 2 (Non-IPS colors)
    Validates tray ID format and availability like Day Planning
    """
    try:
        data = json.loads(request.body)
        tray_id = normalize_jig_unload_tray_id(data.get('tray_id', ''))
        lot_id = data.get('lot_id', '').strip()
        plating_color = data.get('plating_color', '').strip()

        print(f"[DEBUG] JU_Zone_validate_tray_id_dynamic Zone 2 called with tray_id: '{tray_id}', lot_id: '{lot_id}', plating_color: '{plating_color}'")

        if not tray_id:
            return JsonResponse({
                'success': False,
                'error': 'Please enter a tray ID',
                'message': '⚠️ Tray ID is required',
                'validation_type': 'empty_input'
            }, status=400)

        # ✅ ENHANCED: Validate tray ID format based on color and optionally capacity (capacity-aware for safety)
        def validate_tray_format(tray_id, expected_color, lot_id_for_capacity=None, derived_tray_type_override=None):
            """Validate tray ID format based on plating color (Zone 2 - non-IPS) and capacity when available."""
            print(f"[DEBUG] JU_Zone validate_tray_format called with tray_id: '{tray_id}', expected_color: '{expected_color}', lot_id_for_capacity: {lot_id_for_capacity}")

            # Prefer tray_type (Jumbo/Normal) derived from lot/tray metadata; fall back to capacity
            determined_capacity = None
            determined_tray_type = None
            try:
                # If caller provided a derived tray_type, use it immediately
                if derived_tray_type_override:
                    determined_tray_type = derived_tray_type_override
                    print(f"[DEBUG] JU_Zone validate_tray_format using derived_tray_type_override: {determined_tray_type}")
                if lot_id_for_capacity:
                    # 1) Try TrayId record for lot
                    t = TrayId.objects.filter(lot_id__icontains=lot_id_for_capacity).first()
                    if t:
                        if getattr(t, 'tray_type', None):
                            determined_tray_type = t.tray_type
                            print(f"[DEBUG] JU_Zone found tray_type from TrayId: {determined_tray_type}")
                        if getattr(t, 'tray_capacity', None):
                            determined_capacity = int(t.tray_capacity)
                            print(f"[DEBUG] JU_Zone found tray_capacity from TrayId: {determined_capacity}")

                    # 2) Try JigCompleted for tray_type or capacity
                    if not determined_tray_type:
                        jd = JigCompleted.objects.filter(draft_data__lot_id_quantities__has_key=lot_id_for_capacity).first()
                        if jd:
                            if getattr(jd, 'tray_type', None):
                                determined_tray_type = jd.tray_type
                                print(f"[DEBUG] JU_Zone found tray_type from JigCompleted: {determined_tray_type}")
                            if getattr(jd, 'tray_capacity', None) and not determined_capacity:
                                determined_capacity = int(jd.tray_capacity)
                                print(f"[DEBUG] JU_Zone found tray_capacity from JigCompleted: {determined_capacity}")

                    # 3) Try TotalStockModel -> model_stock_no.tray_type/tray_capacity
                    try:
                        tsm = TotalStockModel.objects.select_related('model_stock_no__tray_type').filter(lot_id=lot_id_for_capacity).first()
                        if tsm and getattr(tsm, 'model_stock_no', None):
                            ms = tsm.model_stock_no
                            if not determined_tray_type and getattr(ms, 'tray_type', None):
                                determined_tray_type = ms.tray_type.tray_type if getattr(ms.tray_type, 'tray_type', None) else ms.tray_type
                                print(f"[DEBUG] JU_Zone found tray_type from TotalStockModel.model_stock_no: {determined_tray_type}")
                            if getattr(ms, 'tray_capacity', None) and not determined_capacity:
                                determined_capacity = int(ms.tray_capacity)
                                print(f"[DEBUG] JU_Zone found tray_capacity from TotalStockModel.model_stock_no: {determined_capacity}")
                        else:
                            rsm = RecoveryStockModel.objects.select_related('model_stock_no__tray_type').filter(lot_id=lot_id_for_capacity).first()
                            if rsm and getattr(rsm, 'model_stock_no', None):
                                rms = rsm.model_stock_no
                                if not determined_tray_type and getattr(rms, 'tray_type', None):
                                    determined_tray_type = rms.tray_type.tray_type if getattr(rms.tray_type, 'tray_type', None) else rms.tray_type
                                    print(f"[DEBUG] JU_Zone found tray_type from RecoveryStockModel.model_stock_no: {determined_tray_type}")
                                if getattr(rms, 'tray_capacity', None) and not determined_capacity:
                                    determined_capacity = int(rms.tray_capacity)
                                    print(f"[DEBUG] JU_Zone found tray_capacity from RecoveryStockModel.model_stock_no: {determined_capacity}")
                    except Exception as inner_e:
                        print(f"[DEBUG] JU_Zone error checking Total/RecoveryStockModel for lot '{lot_id_for_capacity}': {inner_e}")
            except Exception as e:
                print(f"[DEBUG] JU_Zone error attempting to determine tray_type/capacity for lot '{lot_id_for_capacity}': {e}")

            # Build allowed prefixes depending on color and derived tray_type (preferred) or capacity
            valid_prefixes = []
            if expected_color == 'IPS':
                # Prefer tray_type mapping
                if determined_tray_type:
                    dt = str(determined_tray_type).strip().lower()
                    if 'jumbo' in dt:
                        valid_prefixes = ['JR-']
                        print(f"[DEBUG] JU_Zone IPS derived tray_type Jumbo - only 'JR-' allowed")
                    elif 'normal' in dt:
                        valid_prefixes = ['NR-']
                        print(f"[DEBUG] JU_Zone IPS derived tray_type Normal - only 'NR-' allowed")
                    else:
                        print(f"[DEBUG] JU_Zone IPS derived tray_type '{determined_tray_type}' not recognized, falling back to capacity")
                if not valid_prefixes:
                    if determined_capacity == 20:
                        valid_prefixes = ['NR-']
                        print(f"[DEBUG] JU_Zone IPS capacity Normal (20) detected - only 'NR-' allowed")
                    elif determined_capacity == 12:
                        valid_prefixes = ['JR-']
                        print(f"[DEBUG] JU_Zone IPS capacity Jumbo (12) detected - only 'JR-' allowed")
                    else:
                        valid_prefixes = ['NR-', 'JR-']
                        print(f"[DEBUG] JU_Zone IPS detected - capacity unknown and tray_type unavailable, allowing both 'NR-' and 'JR-'")
                zone_msg = "IPS colors should use Zone 1 (Red trays)"
            else:
                # Non-IPS: enforce prefixes depending on tray_type when known
                if determined_tray_type:
                    dt = str(determined_tray_type).strip().lower()
                    if 'jumbo' in dt:
                        # Jumbo non-IPS should start with JD- or JL-
                        valid_prefixes = ['JD-', 'JL-']
                        zone_msg = "Non-IPS Jumbo trays should use JD- or JL-"
                        print(f"[DEBUG] JU_Zone Non-IPS derived tray_type Jumbo - only 'JD-'/'JL-' allowed")
                    elif 'normal' in dt:
                        # Normal non-IPS should start with ND- or NL-
                        valid_prefixes = ['ND-', 'NL-']
                        zone_msg = "Non-IPS Normal trays should use ND- or NL-"
                        print(f"[DEBUG] JU_Zone Non-IPS derived tray_type Normal - only 'ND-'/'NL-' allowed")
                    else:
                        valid_prefixes = ['ND-', 'JD-', 'NL-', 'JL-']
                        zone_msg = "Non-IPS colors use Zone 2 (Dark/Light Green trays)"
                        print(f"[DEBUG] JU_Zone Non-IPS derived tray_type '{determined_tray_type}' not recognized - allowing all non-IPS prefixes")
                else:
                    # If tray_type unknown, remain permissive (allow all non-IPS prefixes)
                    valid_prefixes = ['ND-', 'JD-', 'NL-', 'JL-']
                    zone_msg = "Non-IPS colors use Zone 2 (Dark/Light Green trays)"

            has_valid_prefix = any(tray_id.startswith(prefix) for prefix in valid_prefixes)
            if not has_valid_prefix:
                expected_formats = ' or '.join(valid_prefixes)
                return False, f"Expected format: {expected_formats}A00001. {zone_msg}"

            import re
            pattern = r'^(NR-|JR-|ND-|JD-|NL-|JL-)A\d{5}$'
            if not re.match(pattern, tray_id):
                return False, "Format should be: PREFIX-A00001 (e.g., ND-A00001)"

            return True, "Valid format"

        # Try to derive tray_type from model/lot data first, then fallback to existing TrayId
        derived_override = None
        try:
            if lot_id:
                try:
                    candidate = lot_id
                    if ':' in candidate:
                        candidate = candidate.split(':')[-1]
                    if '-' in candidate:
                        parts = candidate.split('-')
                        if len(parts) > 1:
                            candidate = parts[-1]

                    # Prefer TotalStockModel/ModelMaster -> tray_type
                    tsm = TotalStockModel.objects.select_related('model_stock_no__tray_type').filter(lot_id__icontains=candidate).first()
                    if tsm and getattr(tsm, 'model_stock_no', None) and getattr(tsm.model_stock_no, 'tray_type', None):
                        derived_override = tsm.model_stock_no.tray_type.tray_type if getattr(tsm.model_stock_no.tray_type, 'tray_type', None) else tsm.model_stock_no.tray_type
                        print(f"[DEBUG] JU_Zone - derived_override tray_type from TotalStockModel: {derived_override}")
                    else:
                        rsm = RecoveryStockModel.objects.select_related('model_stock_no__tray_type').filter(lot_id__icontains=candidate).first()
                        if rsm and getattr(rsm, 'model_stock_no', None) and getattr(rsm.model_stock_no, 'tray_type', None):
                            derived_override = rsm.model_stock_no.tray_type.tray_type if getattr(rsm.model_stock_no.tray_type, 'tray_type', None) else rsm.model_stock_no.tray_type
                            print(f"[DEBUG] JU_Zone - derived_override tray_type from RecoveryStockModel: {derived_override}")
                except Exception as e:
                    print(f"[DEBUG] JU_Zone - error deriving tray_type from lot_id: {e}")

            # If still not found, fallback to TrayId record
            if not derived_override:
                try:
                    preview_tray = TrayId.objects.filter(tray_id=tray_id).first()
                    if preview_tray and getattr(preview_tray, 'tray_type', None):
                        derived_override = preview_tray.tray_type
                        print(f"[DEBUG] JU_Zone - derived_override tray_type from TrayId: {derived_override}")
                except Exception as e:
                    print(f"[DEBUG] JU_Zone - error previewing TrayId for derive: {e}")
        except Exception as e:
            print(f"[DEBUG] JU_Zone - unexpected error deriving tray_type override: {e}")

        # Validate format first (prefer derived tray_type when available)
        format_valid, format_message = validate_tray_format(tray_id, plating_color or 'OTHER', lot_id_for_capacity=lot_id or None, derived_tray_type_override=derived_override)
        if not format_valid:
            return JsonResponse({
                'success': False,
                'error': f'Invalid tray ID format',
                'message': f'❌ {tray_id} - {format_message}',
                'validation_type': 'invalid_format'
            }, status=400)

        # STEP 1.5: ✅ NEW - Validate tray code against master data (Zone 2 specific)
        try:
            master_valid, master_message, allowed_codes, zone = validate_tray_code_by_master_data(tray_id, lot_id)
            
            if not master_valid:
                print(f"[DEBUG] Zone 2 master data validation failed: {master_message}")
                return JsonResponse({
                    'success': False,
                    'error': f'Tray code validation failed',
                    'message': f'❌ {tray_id} - {master_message}',
                    'validation_type': 'invalid_tray_code_for_lot',
                    'details': {
                        'required_codes': allowed_codes,
                        'zone': zone
                    }
                }, status=400)
            
            print(f"[DEBUG] Zone 2 master data validation passed: {master_message}")
        except Exception as e:
            print(f"[DEBUG] Zone 2 master data validation skipped due to error: {e}")
            # Non-fatal error - continue with other validations

        allowed_lot_ids_for_trays = [lid.strip() for lid in lot_id.split(',') if lid.strip()] if lot_id else []
        tray_conflict = find_jig_unload_tray_conflict(
            tray_id,
            allowed_lot_ids=allowed_lot_ids_for_trays,
            include_tray_master=True,
        )
        if tray_conflict:
            return JsonResponse({
                'success': False,
                'error': tray_conflict['message'],
                'message': f'❌ {tray_id} - Already reserved for another lot',
                'validation_type': 'tray_occupied',
                'details': {
                    'linked_lot': tray_conflict.get('linked_lot', ''),
                    'source': tray_conflict.get('source', '')
                }
            }, status=400)
        nickel_conflict = validate_jig_unload_nickel_reject_tray(
            tray_id,
            current_lot_id=lot_id or None,
        )
        if nickel_conflict:
            return JsonResponse({
                'success': False,
                'error': nickel_conflict['message'],
                'message': f'❌ {tray_id} - Already reserved for another lot',
                'validation_type': 'tray_occupied',
                'details': {
                    'linked_lot': nickel_conflict.get('linked_lot', ''),
                    'source': nickel_conflict.get('source', '')
                }
            }, status=400)

        try:
            # Step 1: Check if tray exists in system
            existing_tray = TrayId.objects.filter(tray_id=tray_id).first()
            
            if not existing_tray:
                return JsonResponse({
                    'success': False,
                    'error': f'Tray ID "{tray_id}" not found in system',
                    'message': f'❌ {tray_id} - Not found in system. Contact admin to add tray.',
                    'validation_type': 'tray_not_found'
                }, status=404)
            
            # Step 2: ✅ ENHANCED - Check if tray is already scanned (like Day Planning)
            if existing_tray.scanned and not existing_tray.delink_tray:
                return JsonResponse({
                    'success': False,
                    'error': f'Tray {tray_id} is already scanned',
                    'message': f'❌ {tray_id} - Already scanned ({existing_tray.date.strftime("%d-%m-%Y %H:%M") if existing_tray.date else "Unknown date"})',
                    'validation_type': 'already_scanned',
                    'details': {
                        'linked_lot': existing_tray.lot_id,
                        'batch_id': existing_tray.batch_id.batch_id if existing_tray.batch_id else None,
                        'scan_date': existing_tray.date.strftime('%d-%m-%Y %H:%M') if existing_tray.date else None
                    }
                }, status=400)
            
            # NEW: Derive tray_type (prefer over capacity) and enforce prefix if known
            derived_tray_type = None
            try:
                if getattr(existing_tray, 'tray_type', None):
                    derived_tray_type = existing_tray.tray_type
                    print(f"[DEBUG] JU_Zone derived tray_type from TrayId: {derived_tray_type}")
                else:
                    if existing_tray.lot_id:
                        candidate = existing_tray.lot_id
                        if ':' in candidate:
                            candidate = candidate.split(':')[-1]
                        if '-' in candidate:
                            parts = candidate.split('-')
                            if len(parts) > 1:
                                candidate = parts[-1]
                        tsm = TotalStockModel.objects.select_related('model_stock_no').filter(lot_id__icontains=candidate).first()
                        if tsm and getattr(tsm.model_stock_no, 'tray_type', None):
                            derived_tray_type = tsm.model_stock_no.tray_type.tray_type if getattr(tsm.model_stock_no, 'tray_type', None) and getattr(tsm.model_stock_no.tray_type, 'tray_type', None) else None
                            if derived_tray_type:
                                print(f"[DEBUG] JU_Zone derived tray_type from TotalStockModel.model_stock_no: {derived_tray_type}")
            except Exception as e:
                print(f"[DEBUG] JU_Zone error deriving tray_type: {e}")

            if derived_tray_type:
                dt_norm = str(derived_tray_type).strip().lower()
                if dt_norm == 'jumbo' and not tray_id.startswith('JD-'):
                    return JsonResponse({
                        'success': False,
                        'error': f'Tray {tray_id} prefix mismatch for tray type Jumbo',
                        'message': f'❌ {tray_id} - Tray Type is Jumbo. Expected prefix: JR- (e.g. JR-A00001)',
                        'validation_type': 'invalid_prefix_for_tray_type'
                    }, status=400)
                if dt_norm == 'normal' and not tray_id.startswith('ND-'):
                    return JsonResponse({
                        'success': False,
                        'error': f'Tray {tray_id} prefix mismatch for tray type Normal',
                        'message': f'❌ {tray_id} - Tray Type is Normal. Expected prefix: NR- (e.g. NR-A00001)',
                        'validation_type': 'invalid_prefix_for_tray_type'
                    }, status=400)

            # Step 3: Validate tray is ready for Jig Unloading process
            # Check if tray has been through required processes (should be loaded in jig)
            # Zone 2 handles all non-IPS colors
            if existing_tray.lot_id:
                non_ips_colors = Plating_Color.objects.exclude(plating_color='IPS').values_list('plating_color', flat=True)
                
                jig_details = JigCompleted.objects.filter(
                    lot_id__contains=existing_tray.lot_id,
                    plating_color__in=non_ips_colors  # Zone 2 handles non-IPS colors
                ).first()
                
                if not jig_details:
                    return JsonResponse({
                        'success': False,
                        'error': f'Tray {tray_id} not found in jig loading records for non-IPS processing',
                        'message': f'⚠️ {tray_id} - Not ready for unloading (not in jig)',
                        'validation_type': 'not_in_jig'
                    }, status=400)
            
            # Step 4: Check if tray has already been unloaded
            already_unloaded = JigUnload_TrayId.objects.filter(tray_id=tray_id).exists()
            if already_unloaded:
                return JsonResponse({
                    'success': False,
                    'error': f'Tray {tray_id} has already been unloaded',
                    'message': f'❌ {tray_id} - Already unloaded',
                    'validation_type': 'already_unloaded'
                }, status=400)
            
            # Step 5: ✅ SUCCESS - Tray is valid for unloading
            return JsonResponse({
                'success': True,
                'message': f'✅ {tray_id} - Valid for unloading',
                'validation_type': 'valid',
                'tray_details': {
                    'tray_id': tray_id,
                    'tray_type': existing_tray.tray_type or 'Unknown',
                    'capacity': existing_tray.tray_capacity or 0,
                    'current_quantity': existing_tray.tray_quantity or 0,
                    'lot_id': existing_tray.lot_id,
                    'batch_id': existing_tray.batch_id.batch_id if existing_tray.batch_id else None,
                    'is_available': existing_tray.is_available_for_scanning
                }
            }, status=200)
            
        except Exception as inner_e:
            print(f"[ERROR] JU_Zone_validate_tray_id_dynamic inner exception: {str(inner_e)}")
            return JsonResponse({
                'success': False,
                'error': f'Validation failed for tray {tray_id}',
                'message': f'❌ {tray_id} - Validation error',
                'validation_type': 'validation_error'
            }, status=500)
            
    except json.JSONDecodeError:
        return JsonResponse({
            'success': False,
            'error': 'Invalid JSON data',
            'message': '❌ Invalid request format',
            'validation_type': 'invalid_json'
        }, status=400)
    except Exception as e:
        logger.error(f"[ERROR] JU_Zone_validate_tray_id_dynamic outer exception: {str(e)}", exc_info=True)
        return JsonResponse({
            'success': False,
            'error': 'Server error during validation',
            'message': '❌ Server error - Please try again',
            'validation_type': 'server_error'
        }, status=500)


@login_required
@csrf_exempt
@require_POST
def JU_Zone_check_unload_status(request):
    """Smart cross-jig multi-lot checker"""
    
    def parse_combined_lot_id(combined_id):
        try:
            if not combined_id or '-' not in combined_id:
                return None, None
            parts = combined_id.rsplit('-', 1)
            return (parts[0], parts[1]) if len(parts) == 2 else (None, None)
        except:
            return None, None

    try:
        data = json.loads(request.body)
        lot_ids = data.get('lot_ids', [])
        jig_lot_id = data.get('jig_lot_id')

        print(f"🎯 [CROSS-JIG] Input - lot_ids: {lot_ids}, jig: {jig_lot_id}")

        if not lot_ids:
            return JsonResponse({'success': True, 'unloaded_lot_ids': []})

        # Get all unload records
        unload_records = JigUnloadAfterTable.objects.filter(
            combine_lot_ids__isnull=False
        ).exclude(combine_lot_ids__exact=[]).values('id', 'combine_lot_ids')

        # Build master unload mapping: lot_id -> jig_lot_id
        lot_to_jig_map = {}
        unloaded_lot_ids = set()
        
        for record in unload_records:
            if not record['combine_lot_ids']:
                continue
                
            for combined_id in record['combine_lot_ids']:
                parsed_jig_id, parsed_lot_id = parse_combined_lot_id(combined_id)
                
                if parsed_jig_id and parsed_lot_id:
                    lot_to_jig_map[parsed_lot_id] = parsed_jig_id
                    print(f"🗺️ [MAP] {parsed_lot_id} belongs to jig {parsed_jig_id}")

        # Smart matching: check if each lot_id is unloaded (regardless of which jig it came from originally)
        for lot_id in lot_ids:
            if lot_id in lot_to_jig_map:
                original_jig = lot_to_jig_map[lot_id]
                if original_jig == jig_lot_id:
                    unloaded_lot_ids.add(lot_id)
                    print(f"✅ [FOUND] {lot_id} is unloaded for jig {original_jig}")
                else:
                    print(f"❌ [SKIP] {lot_id} is unloaded for jig {original_jig}, not {jig_lot_id}")
            else:
                print(f"❌ [NOT_FOUND] {lot_id} not in unload records")
        # Also check JigUnload_TrayId for direct unloads
        direct_unloaded = set()
        for tray in JigUnload_TrayId.objects.filter(lot_id__in=lot_ids):
            if lot_to_jig_map.get(tray.lot_id) == jig_lot_id:
                direct_unloaded.add(tray.lot_id)
        if direct_unloaded:
            unloaded_lot_ids.update(direct_unloaded)
            print(f"✅ [DIRECT] Additional unloaded: {list(direct_unloaded)}")
        
        if direct_unloaded:
            unloaded_lot_ids.update(direct_unloaded)
            print(f"✅ [DIRECT] Additional unloaded: {list(direct_unloaded)}")

        result = list(unloaded_lot_ids)
        print(f"🎯 [FINAL] Unloaded lot_ids: {result}")

        return JsonResponse({
            'success': True,
            'unloaded_lot_ids': result
        })

    except Exception as e:
        print(f"❌ [ERROR] {e}")
        import traceback
        traceback.print_exc()
        return JsonResponse({'success': False, 'error': 'Unable to process the request. Please verify the submitted data and try again.'})        

@method_decorator(csrf_exempt, name='dispatch')
class JU_Zone_ListAPIView(APIView):
    def get(self, request):
        lot_id = request.GET.get('lot_id', '').strip()
        if not lot_id:
            return JsonResponse({'success': False, 'error': 'Missing lot_id'}, status=400)

        try:
            record = JigUnloadAfterTable.objects.filter(lot_id=lot_id).first()
            if not record:
                return JsonResponse({
                    'success': False,
                    'error': f'Record not found for lot_id: {lot_id}'
                }, status=404)

            combine_lot_ids = [
                _zone2_extract_lot_id(src_lot)
                for src_lot in (record.combine_lot_ids or [])
            ]
            combine_lot_ids = _zone2_ordered_unique(combine_lot_ids)

            if not combine_lot_ids:
                return JsonResponse({
                    'success': False,
                    'error': 'No combine_lot_ids found'
                }, status=404)

            tray_list = []

            # Primary source: saved unload tray snapshot.
            # Zone 2 completed rows pass the generated UNLOT id, while tray
            # snapshots are saved against the original/source lot ids.
            subs = list(
                JUSubmittedZ1.objects
                .filter(lot_id__in=combine_lot_ids, is_draft=False)
                .order_by('id')
            )

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
                    signature = _zone2_submission_tray_signature(sub.tray_data)
                    if signature and signature in seen_signatures:
                        continue
                    seen_signatures.add(signature)
                    canonical_subs.append(sub)

            for sub in canonical_subs:
                for entry in (sub.tray_data or []):
                    if not isinstance(entry, dict):
                        continue
                    tray_list.append({
                        'tray_id': entry.get('tray_id', ''),
                        'tray_quantity': (
                            entry.get('qty')
                            or entry.get('tray_qty')
                            or entry.get('tray_quantity')
                            or entry.get('original_qty')
                            or 0
                        ),
                        'top_tray': (
                            entry.get('is_top_tray')
                            if entry.get('is_top_tray') is not None
                            else entry.get('top_tray', False)
                        ),
                        'source_jig': sub.jig_qr_id or 'N/A',
                    })

            # Fallback: old JigUnload_TrayId storage.
            if not tray_list:
                trays = JigUnload_TrayId.objects.filter(lot_id__in=combine_lot_ids).order_by('id')
                for tray in trays:
                    tray_list.append({
                        'tray_id': tray.tray_id,
                        'tray_quantity': tray.tray_qty,
                        'top_tray': tray.top_tray,
                        'source_jig': 'N/A',
                    })

            return JsonResponse({
                'success': True,
                'combine_lot_ids': combine_lot_ids,
                'trays': tray_list
            })

        except Exception as e:
            logger.error(f"Error in JU_Zone_ListAPIView for lot_id={lot_id}: {str(e)}", exc_info=True)
            return JsonResponse({
                'success': False,
                'error': 'Unable to process the request. Please verify the submitted data and try again.'
            }, status=500)


class JU_Zone_Completedtable(LoginRequiredMixin, TemplateView):
    template_name = 'Jig_Unloading - Zone_two/JigUnloading_Completedtable_zone_two.html'
    login_url = 'login'

    def get_dynamic_tray_capacity(self, tray_type_name):
        """
        Get dynamic tray capacity using InprocessInspectionTrayCapacity for overrides
        Rules (sourced from TrayType master DB):
        - Normal (or NR/NB/ND/NL): 20
        - Jumbo  (or JR/JB/JD):    12 (matches TrayType master data)
        - Others: Use InprocessInspectionTrayCapacity or ModelMaster capacity
        """
        try:
            # Workflow-spec capacity — covers both type names and tray code prefixes
            _tn = (tray_type_name or '').upper()
            if _tn in ('NORMAL', 'NR', 'NB', 'ND', 'NL', 'NW'):
                return 20
            elif _tn in ('JUMBO', 'JR', 'JB', 'JD'):
                return 12

            # First try to get custom capacity for this tray type
            custom_capacity = InprocessInspectionTrayCapacity.objects.filter(
                tray_type__tray_type=tray_type_name,
                is_active=True
            ).first()
            
            if custom_capacity:
                return custom_capacity.custom_capacity
            
            # Fallback to ModelMaster tray capacity
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
                    lid = cid.lstrip('-')
                    if lid.startswith('JLOT-') and '-' in lid[5:]:
                        lid = lid.rsplit('-', 1)[1]
                    jc = JigCompleted.objects.filter(
                        draft_data__lot_id_quantities__has_key=lid, bath_numbers__isnull=False
                    ).values('bath_numbers__bath_number').first()
                    if jc:
                        return jc['bath_numbers__bath_number']
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

        print(f"[DEBUG] Zone 2 Completed - Date filter: {from_date} to {to_date}")

        # DEBUG: Check total records in JigUnloadAfterTable
        total_unload_records = JigUnloadAfterTable.objects.all().count()
        print(f"[DEBUG] Total JigUnloadAfterTable records: {total_unload_records}")

        # Zone 2 = all non-IPS plating colors; also include null plating_color for records
        # that failed FK population during save_jig_unload_tray_ids.
        from django.db.models import Q
        zone2_color_ids = list(Plating_Color.objects.exclude(plating_color='IPS').values_list('id', flat=True))
        print(f"[DEBUG] Zone 2 Completed - Allowed color IDs (non-IPS): {zone2_color_ids}")

        completed_unloads_qs = JigUnloadAfterTable.objects.filter(
            Q(plating_color_id__in=zone2_color_ids) | Q(plating_color__isnull=True),
            Un_loaded_date_time__date__gte=from_date,
            Un_loaded_date_time__date__lte=to_date
        ).select_related(
            'plating_color', 'polish_finish', 'version'
        ).prefetch_related('location').order_by('-Un_loaded_date_time')

        # ✅ Post-filter: for null plating_color records, verify they are non-IPS
        # by tracing combine_lot_ids → TotalStockModel → plating_color.
        ips_color_ids = set(Plating_Color.objects.filter(plating_color='IPS').values_list('id', flat=True))

        def _extract_lot_id_z2(combined):
            if not combined:
                return combined
            s = combined.lstrip('-')
            if s.startswith('JLOT-') and '-' in s[5:]:
                return s.rsplit('-', 1)[1]
            return s

        def _is_zone2_record(rec):
            """Return True if the record belongs to Zone 2 (non-IPS)."""
            if rec.plating_color:
                if rec.plating_color_id not in ips_color_ids:
                    return True
                # FK is IPS — but check if ANY combine_lot_id has a non-IPS color
                # (happens for multi-model jigs with mixed plating colors)
                if rec.combine_lot_ids:
                    for _cid in rec.combine_lot_ids:
                        _lid = _extract_lot_id_z2(_cid)
                        _tsm = TotalStockModel.objects.filter(lot_id=_lid).select_related('plating_color').first()
                        if _tsm and _tsm.plating_color and _tsm.plating_color.id not in ips_color_ids:
                            return True
                return False
            # plating_color is null — resolve from combine_lot_ids
            if rec.combine_lot_ids:
                for _cid in rec.combine_lot_ids:
                    _lid = _extract_lot_id_z2(_cid)
                    _tsm = TotalStockModel.objects.filter(lot_id=_lid).select_related('plating_color').first()
                    if _tsm and _tsm.plating_color:
                        if _tsm.plating_color.id not in ips_color_ids:
                            # Backfill FK for faster future reads
                            try:
                                rec.plating_color = _tsm.plating_color
                                rec.save(update_fields=['plating_color'])
                                print(f"[ZONE2 FIX] ✅ Backfilled plating_color for record {rec.id} from lot {_lid}")
                            except Exception as _bfe:
                                print(f"[ZONE2 FIX] ⚠️ Backfill failed: {_bfe}")
                            return True
                        else:
                            return False  # It's IPS — belongs to Zone 1
                    # Try JigCompleted → batch_id → ModelMasterCreation.plating_color
                    _jc = JigCompleted.objects.filter(
                        draft_data__lot_id_quantities__has_key=_lid
                    ).first()
                    if _jc and _jc.batch_id:
                        _mmc = ModelMasterCreation.objects.filter(
                            batch_id=_jc.batch_id
                        ).values('plating_color').first()
                        if _mmc:
                            _pc_name = _mmc.get('plating_color', '')
                            if _pc_name and _pc_name != 'IPS':
                                try:
                                    _pc_obj = Plating_Color.objects.filter(
                                        plating_color=_pc_name
                                    ).first()
                                    if _pc_obj:
                                        rec.plating_color = _pc_obj
                                        rec.save(update_fields=['plating_color'])
                                except Exception:
                                    pass
                            return _pc_name != 'IPS'
            # No color info — include by default (better to show than hide)
            return True

        completed_unloads = [rec for rec in completed_unloads_qs if _is_zone2_record(rec)]

        print(f"[DEBUG] Filtered completed_unloads count: {len(completed_unloads)}")
        for record in completed_unloads:
            print(f"[DEBUG] Record {record.id}: plating_color={record.plating_color}, plating_color_id={record.plating_color_id}")

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
        # so Model Presents circles match JU_Zone_MainTable / Inprocess Inspection.
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
                
                # Only store if we have images (preserve empty until fallback below)
                if image_urls:
                    image_payload = {
                        'images': image_urls,
                        'first_image': first_image
                    }
                    if model_master.model_no and model_master.model_no not in model_images_map:
                        model_images_map[model_master.model_no] = image_payload
                    if model_master.plating_stk_no:
                        model_images_map[model_master.plating_stk_no] = image_payload
                    print(f"[DEBUG] Model {model_master.model_no}: {len(image_urls)} images, first: {first_image}")

            # Fallback: for numeric model_nos still missing images, scan ModelMaster variants by plating_stk_no
            missing_images = all_model_numbers - set(model_images_map.keys())
            if missing_images:
                print(f"[DEBUG] Searching plating_stk_no variants for models without images: {missing_images}")
                # Collect plating_stk_nos that share the numeric prefix
                plating_candidates = ModelMaster.objects.filter(
                    plating_stk_no__isnull=False
                ).exclude(plating_stk_no='').prefetch_related('images')
                for mm in plating_candidates:
                    if not mm.plating_stk_no:
                        continue
                    # Check if this plating_stk_no starts with any missing numeric model_no
                    for numeric_no in list(missing_images):
                        if str(mm.plating_stk_no).startswith(numeric_no) and mm.images.exists():
                            img_list = sort_images_front_first(mm.images.all())
                            image_urls = []
                            first_image = "/static/assets/images/imagePlaceholder.jpg"
                            for img in img_list:
                                if img.master_image:
                                    try:
                                        img_url = img.master_image.url if hasattr(img.master_image, 'url') else str(img.master_image)
                                        image_urls.append(img_url)
                                        if first_image == "/static/assets/images/imagePlaceholder.jpg":
                                            first_image = img_url
                                    except Exception:
                                        continue
                            if image_urls:
                                model_images_map[numeric_no] = {'images': image_urls, 'first_image': first_image}
                                missing_images.discard(numeric_no)
                                print(f"[DEBUG] Fallback: mapped numeric '{numeric_no}' via plating_stk_no '{mm.plating_stk_no}'")
                                break  # Found images for this numeric_no

            # Ensure every model_no has an entry (even if empty)
            for model_no in all_model_numbers:
                if model_no not in model_images_map:
                    model_images_map[model_no] = {
                        'images': [],
                        'first_image': "/static/assets/images/imagePlaceholder.jpg"
                    }

        # Detect Nickel Inspection activity in bulk. Zone 2 shares the Nickel
        # workflow tables and unload-row activity flags with Zone 1, so these
        # records are the earliest reliable signal that the lot has left Jig
        # Unloading.
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

        # ✅ ENHANCED: Process each unload record using saved list fields (mirroring Zone 1)
        table_data = []
        for idx, unload in enumerate(completed_unloads):
            print(f"\n[DEBUG] ===== PROCESSING RECORD {idx + 1} =====")
            print(f"[DEBUG] Unload lot_id: {unload.lot_id}")
            print(f"[DEBUG] combine_lot_ids: {unload.combine_lot_ids}")

            # Get jig_qr_id — use DB field, then parse from combine_lot_ids, then JigCompleted lookup
            jig_qr_id = unload.jig_qr_id or ''
            unloading_remarks = None
            if not jig_qr_id and unload.combine_lot_ids:
                for _cid_z2 in unload.combine_lot_ids:
                    # combine_lot_ids format: "JLOT-xxx-LIDyyy" → rsplit gives ("JLOT-xxx", "LIDyyy")
                    if _cid_z2 and '-' in _cid_z2:
                        _parts_z2 = _cid_z2.rsplit('-', 1)
                        _parsed_jig_id_z2 = _parts_z2[0] if len(_parts_z2) == 2 else None
                        _parsed_lot_id_z2 = _parts_z2[1] if len(_parts_z2) == 2 else _cid_z2
                        if _parsed_jig_id_z2 and _parsed_jig_id_z2.startswith('JLOT-'):
                            jig_qr_id = _parsed_jig_id_z2
                            # Also fetch remarks via parsed lot_id
                            _jc_z2 = JigCompleted.objects.filter(
                                draft_data__lot_id_quantities__has_key=_parsed_lot_id_z2
                            ).first()
                            if _jc_z2:
                                unloading_remarks = getattr(_jc_z2, 'remarks', None) or getattr(_jc_z2, 'unloading_remarks', None)
                            break
                        elif _parsed_lot_id_z2:
                            # Broken format '-LIDyyy': no JLOT prefix — look up JigCompleted via extracted lot_id
                            _actual_lot_z2 = _extract_lot_id(_cid_z2)
                            _jc_z2_fb = JigCompleted.objects.filter(
                                draft_data__lot_id_quantities__has_key=_actual_lot_z2
                            ).first()
                            if _jc_z2_fb:
                                jig_qr_id = getattr(_jc_z2_fb, 'jig_id', None) or ''
                                unloading_remarks = getattr(_jc_z2_fb, 'remarks', None) or getattr(_jc_z2_fb, 'unloading_remarks', None)
                            break

            # ✅ FINAL FALLBACK: plain lot IDs (no dash) — query JigCompleted directly
            if not jig_qr_id and unload.combine_lot_ids:
                for _cid_plain_z2 in unload.combine_lot_ids:
                    _lid_plain_z2 = _extract_lot_id(_cid_plain_z2) if _cid_plain_z2 else None
                    if not _lid_plain_z2:
                        continue
                    _jc_plain_z2 = JigCompleted.objects.filter(
                        draft_data__lot_id_quantities__has_key=_lid_plain_z2
                    ).first()
                    if not _jc_plain_z2:
                        _jc_plain_z2 = JigCompleted.objects.filter(lot_id=_lid_plain_z2).first()
                    if _jc_plain_z2:
                        _jid_z2 = getattr(_jc_plain_z2, 'jig_id', None)
                        if _jid_z2:
                            jig_qr_id = _jid_z2
                            unloading_remarks = (getattr(_jc_plain_z2, 'remarks', None)
                                                 or getattr(_jc_plain_z2, 'unloading_remarks', None))
                            print(f"[ZONE2 FIX] ✅ jig_qr_id resolved from JigCompleted: {jig_qr_id}")
                            break
                        if not unloading_remarks:
                            unloading_remarks = (getattr(_jc_plain_z2, 'remarks', None)
                                                 or getattr(_jc_plain_z2, 'unloading_remarks', None))

            # Backfill jig_qr_id to DB so future renders skip the lookup
            if jig_qr_id and not unload.jig_qr_id:
                try:
                    unload.jig_qr_id = jig_qr_id
                    unload.save(update_fields=['jig_qr_id'])
                    print(f"[ZONE2 FIX] ✅ Backfilled jig_qr_id={jig_qr_id} for record {unload.id}")
                except Exception as _bjid_z2:
                    print(f"[ZONE2 FIX] ⚠️ Backfill jig_qr_id failed: {_bjid_z2}")

            if jig_qr_id and not unloading_remarks:
                _jc_rem = JigCompleted.objects.filter(jig_id=jig_qr_id).first()
                if _jc_rem:
                    unloading_remarks = getattr(_jc_rem, 'remarks', None) or getattr(_jc_rem, 'unloading_remarks', None)

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
                for _cid_loc in unload.combine_lot_ids:
                    _actual_loc = _extract_lot_id(_cid_loc)
                    _tsm_loc = TotalStockModel.objects.filter(lot_id=_actual_loc).prefetch_related('location').first()
                    if _tsm_loc and _tsm_loc.location.exists():
                        location_names = [loc.location_name for loc in _tsm_loc.location.all()]
                        location_display = ", ".join(location_names)
                        break
                    elif _tsm_loc and _tsm_loc.batch_id and getattr(_tsm_loc.batch_id, 'location', None):
                        location_names = [_tsm_loc.batch_id.location.location_name]
                        location_display = location_names[0]
                        break

            # ✅ NEW FALLBACK: JigCompleted → batch_id → ModelMasterCreation.location
            if not location_names and unload.combine_lot_ids:
                for _cid_loc2 in unload.combine_lot_ids:
                    _actual_loc2 = _extract_lot_id(_cid_loc2)
                    _jc_loc = JigCompleted.objects.filter(
                        draft_data__lot_id_quantities__has_key=_actual_loc2
                    ).first()
                    if not _jc_loc:
                        _jc_loc = JigCompleted.objects.filter(lot_id=_actual_loc2).first()
                    if _jc_loc and _jc_loc.batch_id:
                        _mmc_loc = ModelMasterCreation.objects.filter(
                            batch_id=_jc_loc.batch_id
                        ).select_related('location').first()
                        if _mmc_loc and _mmc_loc.location:
                            location_names = [_mmc_loc.location.location_name]
                            location_display = location_names[0]
                            print(f"[ZONE2 COMPLETED] ✅ Location via JigCompleted→MMC: {location_display}")
                            break
                    if jig_qr_id:
                        _jc_jid = JigCompleted.objects.filter(jig_id=jig_qr_id).first()
                        if _jc_jid and _jc_jid.batch_id:
                            _mmc_jid = ModelMasterCreation.objects.filter(
                                batch_id=_jc_jid.batch_id
                            ).select_related('location').first()
                            if _mmc_jid and _mmc_jid.location:
                                location_names = [_mmc_jid.location.location_name]
                                location_display = location_names[0]
                                print(f"[ZONE2 COMPLETED] ✅ Location via jig_id→JigCompleted→MMC: {location_display}")
                                break

            # Tray info using dynamic capacity
            tray_type_display = unload.tray_type or "N/A"
            if tray_type_display != "N/A":
                dynamic_tray_capacity = self.get_dynamic_tray_capacity(tray_type_display)
                tray_capacity = dynamic_tray_capacity if dynamic_tray_capacity > 0 else (unload.tray_capacity if unload.tray_capacity else 1)
            else:
                tray_capacity = unload.tray_capacity if unload.tray_capacity else 1

            total_case_qty = unload.total_case_qty if unload.total_case_qty else 0
            no_of_trays = math.ceil(total_case_qty / tray_capacity) if tray_capacity > 0 else 0

            # ✅ Use saved list fields directly from JigUnloadAfterTable
            saved_plating_list = getattr(unload, 'plating_stk_no_list', []) or []
            saved_polish_list = getattr(unload, 'polish_stk_no_list', []) or []
            saved_version_list = getattr(unload, 'version_list', []) or []

            all_plating_stk_nos = _zone2_ordered_unique(_normalize_completed_model_tokens(saved_plating_list)) if saved_plating_list else []
            all_polish_stk_nos = list(saved_polish_list) if saved_polish_list else []
            all_versions = list(saved_version_list) if saved_version_list else []

            # Fallback: use single field values if no saved lists
            if not all_plating_stk_nos and unload.plating_stk_no:
                all_plating_stk_nos = _zone2_ordered_unique(_normalize_completed_model_tokens(unload.plating_stk_no))
            if not all_polish_stk_nos and unload.polish_stk_no:
                all_polish_stk_nos = [unload.polish_stk_no]
            if not all_versions and unload.version:
                version_val = getattr(unload.version, 'version_internal', str(unload.version))
                all_versions = [version_val]

            unique_plating_stk_nos = list(set(all_plating_stk_nos)) if all_plating_stk_nos else []
            unique_polish_stk_nos = list(set(all_polish_stk_nos)) if all_polish_stk_nos else []
            unique_versions = list(set(all_versions)) if all_versions else []

            # ✅ Model data from combine_lot_ids (same as Zone 1)
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
                            if hasattr(stock_model, 'total_stock'):
                                lot_id_quantities[lot_id] = stock_model.total_stock
                            elif hasattr(stock_model, 'stock_qty'):
                                lot_id_quantities[lot_id] = stock_model.stock_qty
                            else:
                                lot_id_quantities[lot_id] = 0
                            model_colors[model_no] = global_model_colors.get(model_no, '#cccccc')
                            if model_no in model_images_map:
                                model_images[model_no] = model_images_map[model_no]
                            else:
                                model_images[model_no] = {'images': [], 'first_image': "/static/assets/images/imagePlaceholder.jpg"}
                    except Exception as e:
                        print(f"[DEBUG] Error processing lot_id {lot_id}: {e}")
                        continue

            # If no model data from combine_lot_ids, fall back using plating_stk_no
            if not no_of_model_cases and all_plating_stk_nos:
                _mk_fb = all_plating_stk_nos[0]
                no_of_model_cases = [_mk_fb]
                lot_id_quantities = {unload.lot_id: total_case_qty}
                model_colors[_mk_fb] = global_model_colors.get(_mk_fb, '#cccccc')
                model_images[_mk_fb] = model_images_map.get(_mk_fb, {'images': [], 'first_image': "/static/assets/images/imagePlaceholder.jpg"})

            source_lot_id_quantities = dict(lot_id_quantities)
            display_lot_id_quantities = {unload.lot_id: total_case_qty} if total_case_qty else source_lot_id_quantities

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

            # FK display values
            plating_color_display = "N/A"
            if unload.plating_color:
                plating_color_display = getattr(unload.plating_color, 'plating_color', str(unload.plating_color))

            polish_finish_display = "N/A"
            if unload.polish_finish:
                polish_finish_display = getattr(unload.polish_finish, 'polish_finish', str(unload.polish_finish))

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

            table_entry = {
                'id': unload.id,
                'lot_id': unload.lot_id,
                'jig_qr_id': jig_qr_id,
                'combine_lot_ids': unload.combine_lot_ids,
                'total_case_qty': unload.total_case_qty,
                'missing_qty': unload.missing_qty,
                'plating_stk_no': all_plating_stk_nos[0] if all_plating_stk_nos else (unload.plating_stk_no or "N/A"),
                'polish_stk_no': all_polish_stk_nos[0] if all_polish_stk_nos else (unload.polish_stk_no or "N/A"),
                'all_plating_stk_nos': all_plating_stk_nos,
                'all_polishing_stk_nos': all_polish_stk_nos,
                'all_versions': all_versions,
                'unique_plating_stk_nos': unique_plating_stk_nos,
                'unique_polishing_stk_nos': unique_polish_stk_nos,
                'unique_versions': unique_versions,
                'plating_color': plating_color_display,
                'polish_finish': polish_finish_display,
                'polish_finish_name': polish_finish_display,
                'version': version_display,
                'location': location_display,
                'unique_locations': location_names if location_names else ["N/A"],
                'tray_type': tray_type_display,
                'tray_capacity': tray_capacity,
                'jig_type': tray_type_display,
                'jig_capacity': tray_capacity,
                'no_of_trays': no_of_trays,
                'calculated_no_of_trays': no_of_trays,
                # Prefer the live current_stage SSOT (modelmasterapp/stage_service.py) so
                # this stays in sync with downstream modules (e.g. Spider Spindle) that
                # only update current_stage and not last_process_module.
                'last_process_module': (
                    'Nickel Wiping'
                    if unload.lot_id in nickel_wiping_lot_ids
                    else unload.current_stage or unload.last_process_module
                ),
                'created_at': unload.created_at,
                'un_loaded_date_time': unload.Un_loaded_date_time,
                'Un_loaded_date_time': unload.Un_loaded_date_time,
                'jig_unload_draft': False,
                'electroplating_only': False,
                'no_of_model_cases': no_of_model_cases,
                'model_images': model_images,
                'model_colors': model_colors,
                'lot_id_quantities': display_lot_id_quantities,
                'source_lot_id_quantities': source_lot_id_quantities,
                'lot_id_model_map': {},
                'unloading_remarks': unloading_remarks,
                'bath_numbers': {'bath_number': self._resolve_bath_number_for_completed(unload, jig_qr_id)},
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
        
        print(f"\n📄 Jig Unloading Zone 2 Completed - Pagination: Page {page_number}, Total items: {len(table_data)}")
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

@require_GET
def JU_Zone_get_model_images(request):
    lot_id = request.GET.get('lot_id')
    model_number = request.GET.get('model_number')

    if not lot_id:
        return JsonResponse({'success': False, 'error': 'lot_id required'}, status=400)

    try:
        import re
        print(f"[DEBUG] JU_Zone_get_model_images called with lot_id: {lot_id}, model_number: {model_number}")

        model = None
        model_no = None

        # Method 1: JigUnloadAfterTable (available post-submission)
        jig_unload = JigUnloadAfterTable.objects.filter(lot_id=lot_id).first()
        if jig_unload and jig_unload.polish_stk_no:
            raw_no = jig_unload.polish_stk_no.split('X')[0] if 'X' in jig_unload.polish_stk_no else jig_unload.polish_stk_no
            candidate = ModelMaster.objects.prefetch_related('images').filter(model_no=raw_no).first()
            if candidate:
                model = candidate
                model_no = raw_no
                print(f"[DEBUG] Method 1 success: model_no={model_no}")

        # Method 2: model_number param — try plating_stk_no first, then numeric prefix fallback
        if not model and model_number:
            # Method 2a: exact plating_stk_no match (model_number may be a full plating stk no like '1805NAK02')
            candidate = ModelMaster.objects.prefetch_related('images').filter(plating_stk_no=str(model_number)).first()
            if candidate:
                model = candidate
                model_no = candidate.plating_stk_no or candidate.model_no
                print(f"[DEBUG] Method 2a success via plating_stk_no='{model_number}'")
            else:
                # Method 2b: extract numeric prefix and look up ModelMaster
                match = re.match(r'^(\d+)', str(model_number))
                search_no = match.group(1) if match else str(model_number)
                candidate = ModelMaster.objects.prefetch_related('images').filter(model_no=search_no).first()
                if candidate:
                    model = candidate
                    model_no = search_no
                    print(f"[DEBUG] Method 2b success: numeric model_no='{model_no}' from '{model_number}'")

        # Method 3: TotalStockModel → model_stock_no FK → ModelMaster
        if not model:
            total_stock = TotalStockModel.objects.filter(lot_id=lot_id).select_related('model_stock_no').first()
            if total_stock and total_stock.model_stock_no:
                candidate = ModelMaster.objects.prefetch_related('images').filter(pk=total_stock.model_stock_no.pk).first()
                if candidate:
                    model = candidate
                    model_no = candidate.model_no
                    print(f"[DEBUG] Method 3 success via TotalStockModel: model_no={model_no}")

        # Method 4: RecoveryStockModel → model_stock_no FK → ModelMaster
        if not model:
            try:
                rsm = RecoveryStockModel.objects.filter(lot_id=lot_id).select_related('model_stock_no').first()
                if rsm and rsm.model_stock_no:
                    candidate = ModelMaster.objects.prefetch_related('images').filter(pk=rsm.model_stock_no.pk).first()
                    if candidate:
                        model = candidate
                        model_no = candidate.model_no
                        print(f"[DEBUG] Method 4 success via RecoveryStockModel: model_no={model_no}")
            except Exception as e4:
                print(f"[DEBUG] Method 4 error: {e4}")

        # Method 5: JigCompleted (Zone 2 Jig Loading lot IDs not in TotalStock/RecoveryStock)
        # JigCompleted.batch_id is a string → ModelMasterCreation.batch_id → model_stock_no (ModelMaster FK)
        if not model:
            try:
                from Jig_Loading.models import JigCompleted as _JC5
                jc5 = _JC5.objects.filter(lot_id=lot_id).first()
                if jc5 and jc5.batch_id:
                    mmc5 = ModelMasterCreation.objects.filter(
                        batch_id=jc5.batch_id
                    ).select_related('model_stock_no').prefetch_related('model_stock_no__images').first()
                    if mmc5 and mmc5.model_stock_no:
                        # Use model_stock_no directly (it's the ModelMaster FK — preserves plating_stk_no)
                        candidate = mmc5.model_stock_no
                        model = candidate
                        model_no = candidate.plating_stk_no or candidate.model_no
                        print(f"[DEBUG] Method 5 success via JigCompleted batch '{jc5.batch_id}': model_no={model_no}")
                        # If model_stock_no has no images, try lookup by ModelMasterCreation.plating_stk_no
                        if not candidate.images.exists() and mmc5.plating_stk_no:
                            ps5 = ModelMaster.objects.prefetch_related('images').filter(
                                plating_stk_no=mmc5.plating_stk_no
                            ).first()
                            if ps5 and ps5.images.exists():
                                model = ps5
                                model_no = ps5.plating_stk_no or ps5.model_no
                                print(f"[DEBUG] Method 5b: via MMC.plating_stk_no='{mmc5.plating_stk_no}': model_no={model_no}")
            except Exception as e5:
                print(f"[DEBUG] Method 5 error: {e5}")

        if not model:
            print(f"[DEBUG] All methods failed for lot_id={lot_id}, model_number={model_number}")
            return JsonResponse({
                'success': False,
                'image': None,
                'error': f'No model data found for lot_id: {lot_id}',
                'debug_info': {
                    'lot_id': lot_id,
                    'model_number_provided': model_number,
                }
            })

        # Image fallback: if found model has no images, search for a ModelMaster variant that does
        if not model.images.exists():
            print(f"[DEBUG] Model {model_no} (pk={model.pk}) has no images — searching for variant with images")
            try:
                # FIRST: JigCompleted → batch_id → ModelMasterCreation.images (direct, most reliable for Jig Loading lots)
                if lot_id:
                    _jc_fb2 = JigCompleted.objects.filter(lot_id=lot_id).only('batch_id').first()
                    if _jc_fb2 and _jc_fb2.batch_id:
                        _mmc_fb2 = ModelMasterCreation.objects.filter(
                            batch_id=_jc_fb2.batch_id
                        ).select_related('model_stock_no').prefetch_related(
                            'images', 'model_stock_no__images'
                        ).first()
                        if _mmc_fb2:
                            if _mmc_fb2.images.exists():
                                from modelmasterapp.image_utils import sort_images_front_first
                                _mmc_imgs2 = [img.master_image.url for img in sort_images_front_first(_mmc_fb2.images.all()) if img.master_image]
                                if _mmc_imgs2:
                                    print(f"[DEBUG] Zone 2 Image fallback (MMC direct): batch={_jc_fb2.batch_id} -> {len(_mmc_imgs2)} images")
                                    return JsonResponse({
                                        'success': True,
                                        'image': _mmc_imgs2[0],
                                        'total_images': len(_mmc_imgs2),
                                        'all_images': _mmc_imgs2,
                                        'model_no': model_no
                                    })
                            if _mmc_fb2.model_stock_no and _mmc_fb2.model_stock_no.images.exists():
                                model = _mmc_fb2.model_stock_no
                                model_no = model.plating_stk_no or model.model_no
                                print(f"[DEBUG] Zone 2 Image fallback (MMC→ModelMaster): batch={_jc_fb2.batch_id}, model_no={model_no}")

                # Try by plating_stk_no from TotalStockModel.batch_id (most accurate)
                if lot_id:
                    _tsm_fb = TotalStockModel.objects.filter(lot_id=lot_id).select_related('batch_id').first()
                    if _tsm_fb and _tsm_fb.batch_id and _tsm_fb.batch_id.plating_stk_no:
                        _ps_fb = ModelMaster.objects.prefetch_related('images').filter(
                            plating_stk_no=_tsm_fb.batch_id.plating_stk_no
                        ).first()
                        if _ps_fb and _ps_fb.images.exists():
                            model = _ps_fb
                            model_no = _ps_fb.plating_stk_no or _ps_fb.model_no
                            print(f"[DEBUG] Image fallback (TotalStock): plating_stk_no='{model_no}'")

                # If still no images, try RecoveryStockModel
                if not model.images.exists() and lot_id:
                    try:
                        _rsm_fb = RecoveryStockModel.objects.filter(lot_id=lot_id).select_related('batch_id').first()
                        if _rsm_fb and _rsm_fb.batch_id and _rsm_fb.batch_id.plating_stk_no:
                            _ps_fb2 = ModelMaster.objects.prefetch_related('images').filter(
                                plating_stk_no=_rsm_fb.batch_id.plating_stk_no
                            ).first()
                            if _ps_fb2 and _ps_fb2.images.exists():
                                model = _ps_fb2
                                model_no = _ps_fb2.plating_stk_no or _ps_fb2.model_no
                                print(f"[DEBUG] Image fallback (Recovery): plating_stk_no='{model_no}'")
                    except Exception:
                        pass

                # Last resort: find any ModelMaster with same numeric model_no that has images
                if not model.images.exists():
                    _m_prefix = re.match(r'^(\d+)', model.model_no or '')
                    _numeric = _m_prefix.group(1) if _m_prefix else None
                    if _numeric:
                        for _variant in ModelMaster.objects.filter(model_no=_numeric).prefetch_related('images'):
                            if _variant.images.exists():
                                model = _variant
                                model_no = _variant.plating_stk_no or _variant.model_no
                                print(f"[DEBUG] Image fallback (numeric scan): model_no={_variant.model_no}, plating_stk_no={_variant.plating_stk_no}")
                                break
            except Exception as _efb:
                print(f"[DEBUG] Image fallback error: {_efb}")

        # Get all images for this model
        from modelmasterapp.image_utils import sort_images_front_first
        images = [img.master_image.url for img in sort_images_front_first(model.images.all())]
        print(f"[DEBUG] Found {len(images)} images for model_no {model_no}")

        if images:
            return JsonResponse({
                'success': True,
                'image': images[0],
                'total_images': len(images),
                'all_images': images,
                'model_no': model_no
            })
        else:
            return JsonResponse({
                'success': False,
                'image': None,
                'error': f'Model {model_no} found but has no images',
                'debug_info': {
                    'model_found': True,
                    'model_no': model_no,
                    'images_count': 0
                }
            })

    except Exception as e:
        logger.error(f"[DEBUG] Error in JU_Zone_get_model_images: {str(e)}", exc_info=True)
        return JsonResponse({
            'success': False,
            'error': 'Unable to process the request. Please verify the submitted data and try again.',
            'image': None,
            'debug_info': {
                'exception_type': type(e).__name__,
                'exception_message': 'Unable to process the request. Please verify the submitted data and try again.'
            }
        })

@api_view(['GET'])
@permission_classes([IsAuthenticated])
def JU_Zone_after_view_tray_list(request):
    """
    Returns tray list for all lot_ids in combine_lot_ids for the given stock_lot_id (UNLOT ID) from JigUnloadAfterTable.
    """
    stock_lot_id = request.GET.get('stock_lot_id')

    if not stock_lot_id or stock_lot_id in ['None', 'null', '']:
        return Response({
            'success': False, 
            'error': 'Valid stock_lot_id parameter is required'
        }, status=400)

    try:
        print(f"[DEBUG] Received stock_lot_id: {stock_lot_id}")
        
        # Find the JigUnloadAfterTable record by the auto-generated lot_id (UNLOT ID)
        jig_unload_record = JigUnloadAfterTable.objects.filter(lot_id=stock_lot_id).first()
        
        if not jig_unload_record:
            print(f"[DEBUG] JigUnloadAfterTable record not found for lot_id: {stock_lot_id}")
            return Response({
                'success': False, 
                'error': f'JigUnloadAfterTable record not found for lot_id: {stock_lot_id}'
            }, status=404)

        # Get all lot_ids from combine_lot_ids
        combine_lot_ids = jig_unload_record.combine_lot_ids or []
        print(f"[DEBUG] Found combine_lot_ids: {combine_lot_ids}")
        
        if not combine_lot_ids:
            return Response({
                'success': False, 
                'error': 'No combine_lot_ids found in the record'
            }, status=404)

        # Query all trays for these combine_lot_ids from JigUnload_TrayId
        trays = JigUnload_TrayId.objects.filter(lot_id__in=combine_lot_ids).order_by('id')
        print(f"[DEBUG] Found {trays.count()} trays for combine_lot_ids")
        
        tray_list = []
        for idx, tray in enumerate(trays):
            tray_list.append({
                'sno': idx + 1,
                'tray_id': tray.tray_id,
                'tray_qty': tray.tray_qty,
                'lot_id': tray.lot_id,  # This will be one of the original lot_ids
                'is_top_tray': idx == 0  # Mark first tray as top tray
            })

        return Response({
            'success': True,
            'unlot_id': stock_lot_id,  # The UNLOT ID that was searched
            'combine_lot_ids': combine_lot_ids,  # All the original lot IDs
            'total_trays': len(tray_list),
            'trays': tray_list,
        })
        
    except Exception as e:
        logger.error(f"[DEBUG] ERROR in JU_Zone_after_view_tray_list: {str(e)}", exc_info=True)
        import traceback
        traceback.print_exc()
        return Response({
            'success': False, 
            'error': 'Unable to process the request. Please verify the submitted data and try again.'
        }, status=500)



@method_decorator(csrf_exempt, name='dispatch')
class JU_Zone_AfterTrayValidateAPIView(APIView):
    def post(self, request):
        try:
            data = json.loads(request.body)
            tray_id = data.get('tray_id')

            # Validate tray_id first
            if not tray_id:
                return Response({'valid': False, 'message': 'Tray ID is required.'}, status=400)

            try:
                # Check if tray exists and get its details
                tray = TrayId.objects.get(tray_id=tray_id)
                
                return Response({
                    'valid': True, 
                    'tray': {
                        'tray_id': tray.tray_id,
                        'lot_id': tray.lot_id,
                        'tray_quantity': tray.tray_quantity,
                    },
                    'message': f'Tray {tray_id} is valid'
                })
                
            except TrayId.DoesNotExist:
                return Response({'valid': False, 'message': f'Tray {tray_id} not found.'}, status=404)
                
        except json.JSONDecodeError:
            return Response({'valid': False, 'message': 'Invalid JSON data.'}, status=400)
        except Exception as e:
            return Response({'valid': False, 'message': 'Unable to process the request. Please verify the submitted data and try again.'}, status=500)


@login_required
@csrf_exempt
@require_POST
def JU_Zone_fix_missing_plating_colors(request):
    """
    Utility function to fix existing JigUnloadAfterTable records that have NULL plating_color
    """
    try:
        print("[FIX PLATING] 🔧 Starting to fix missing plating colors...")
        
        # Find all records with missing plating_color
        records_with_missing_color = JigUnloadAfterTable.objects.filter(plating_color__isnull=True)
        total_records = records_with_missing_color.count()
        
        print(f"[FIX PLATING] Found {total_records} records with missing plating_color")
        
        fixed_count = 0
        for record in records_with_missing_color:
            print(f"[FIX PLATING] Processing record {record.id} (lot_id: {record.lot_id})")
            
            # Try to fix using combine_lot_ids
            if record.combine_lot_ids:
                for combined_lot_id in record.combine_lot_ids:
                    print(f"[FIX PLATING] Checking combined lot_id: {combined_lot_id}")
                    
                    # Extract actual lot_id from combined format
                    actual_lot_id = combined_lot_id
                    if 'JLOT-' in combined_lot_id and '-' in combined_lot_id:
                        parts = combined_lot_id.split('-')
                        if len(parts) >= 3:
                            actual_lot_id = '-'.join(parts[2:])
                        print(f"[FIX PLATING] Extracted lot_id: {actual_lot_id}")
                    
                    # Find JigCompleted with this lot_id
                    jig_detail = JigCompleted.objects.filter(
                        draft_data__lot_id_quantities__has_key=actual_lot_id
                    ).first()
                    
                    if jig_detail and jig_detail.draft_data.get('plating_color'):
                        print(f"[FIX PLATING] Found plating_color in JigCompleted: {jig_detail.draft_data.get('plating_color')}")
                        record.plating_color = jig_detail.draft_data.get('plating_color')
                        record.save()
                        fixed_count += 1
                        print(f"[FIX PLATING] ✅ Fixed record {record.id} with plating_color: {jig_detail.draft_data.get('plating_color')}")
                        break
                    else:
                        print(f"[FIX PLATING] No JigCompleted or plating_color found for lot_id: {actual_lot_id}")
            else:
                print(f"[FIX PLATING] Record {record.id} has no combine_lot_ids")
        
        print(f"[FIX PLATING] ✅ Fixed {fixed_count} out of {total_records} records")
        
        return JsonResponse({
            'success': True,
            'message': f'Fixed {fixed_count} out of {total_records} records with missing plating_color'
        })
        
    except Exception as e:
        print(f"[FIX PLATING] ❌ Error: {e}")
        import traceback
        traceback.print_exc()
        return JsonResponse({'success': False, 'error': 'Unable to process the request. Please verify the submitted data and try again.'}, status=500)
    def post(self, request):
        try:
            data = request.data if hasattr(request, 'data') else json.loads(request.body.decode('utf-8'))
            lot_id = data.get('lot_id', '').strip()  # This is the UNLOT ID
            tray_id = str(data.get('tray_id', '')).strip()

            print(f"[DEBUG] Raw request data: {data}")
            print(f"[DEBUG] Extracted lot_id: '{lot_id}'")
            print(f"[DEBUG] Extracted tray_id: '{tray_id}'")

            if not lot_id or not tray_id:
                return JsonResponse({
                    'success': False, 
                    'error': 'Both lot_id and tray_id are required'
                }, status=400)

            # Get combine_lot_ids for this UNLOT ID
            jig_unload_record = JigUnloadAfterTable.objects.filter(lot_id=lot_id).first()
            if not jig_unload_record or not jig_unload_record.combine_lot_ids:
                print(f"[DEBUG] No JigUnloadAfterTable or combine_lot_ids found for lot_id: {lot_id}")
                return JsonResponse({
                    'success': False, 
                    'error': 'No combine_lot_ids found for this UNLOT ID'
                }, status=404)


            # Check for tray in any of the original lot_ids
            tray_record = JigUnload_TrayId.objects.filter(
                tray_id=tray_id,
            ).first()

            tray_exists = tray_record is not None
            print(f"[DEBUG] Tray exists in JigUnload_TrayId: {tray_exists}")

            if tray_record:
                print(f"[DEBUG] Tray '{tray_id}' found for lot_id: {tray_record.lot_id} with qty: {tray_record.tray_qty}")

            print(f"[DEBUG] Final result - exists: {tray_exists}")
            print("="*50)

            return JsonResponse({
                'success': True, 
                'exists': tray_exists,
                'tray_info': {
                    'tray_id': tray_record.tray_id,
                    'tray_qty': tray_record.tray_qty,
                    'lot_id': tray_record.lot_id
                } if tray_record else None
            })

        except Exception as e:
            logger.error(f"[DEBUG] ERROR in JigAfterTrayValidateAPIView: {str(e)}", exc_info=True)
            import traceback
            traceback.print_exc()
            return JsonResponse({
                'success': False, 
                'error': 'Unable to process the request. Please verify the submitted data and try again.'
            }, status=500)


@require_GET
def debug_model_availability_zone2(request):
    """
    Debug endpoint to check model availability for Zone 2 and suggest working models
    Call this at: /jig_unloading_zone2/debug_models/
    """
    try:
        print("[DEBUG] Starting debug_model_availability_zone2 analysis...")
        
        # Find models with images
        models_with_images = ModelMaster.objects.prefetch_related('images').filter(images__isnull=False).distinct()[:20]
        
        working_models = []
        for model in models_with_images:
            try:
                from modelmasterapp.image_utils import sort_images_front_first
                images = [img.master_image.url for img in sort_images_front_first(model.images.all())]
                working_models.append({
                    'model_no': model.model_no,
                    'model_name': getattr(model, 'model_name', 'N/A'),
                    'image_count': len(images),
                    'first_image': images[0] if images else None
                })
                print(f"[DEBUG] Zone 2 Found model {model.model_no} with {len(images)} images")
            except Exception as e:
                print(f"[DEBUG] Zone 2 Error processing model {model.model_no}: {e}")
        
        # Check specific model 1805SSA02
        specific_model = ModelMaster.objects.filter(model_no='1805SSA02').first()
        specific_model_info = None
        if specific_model:
            try:
                from modelmasterapp.image_utils import sort_images_front_first
                images = [img.master_image.url for img in sort_images_front_first(specific_model.images.all())]
                specific_model_info = {
                    'exists': True,
                    'model_no': specific_model.model_no,
                    'image_count': len(images),
                    'images': images
                }
                print(f"[DEBUG] Zone 2 Model 1805SSA02 exists with {len(images)} images")
            except Exception as e:
                print(f"[DEBUG] Zone 2 Error checking model 1805SSA02: {e}")
                specific_model_info = {'exists': True, 'error': 'Unable to process the request. Please verify the submitted data and try again.'}
        else:
            specific_model_info = {'exists': False}
            print("[DEBUG] Zone 2 Model 1805SSA02 does not exist")
        
        # Also check for partial match (like just "1805")
        partial_model = ModelMaster.objects.filter(model_no='1805').first()
        partial_model_info = None
        if partial_model:
            try:
                from modelmasterapp.image_utils import sort_images_front_first
                images = [img.master_image.url for img in sort_images_front_first(partial_model.images.all())]
                partial_model_info = {
                    'exists': True,
                    'model_no': partial_model.model_no,
                    'image_count': len(images),
                    'images': images
                }
                print(f"[DEBUG] Zone 2 Model 1805 (partial) exists with {len(images)} images")
            except Exception as e:
                print(f"[DEBUG] Zone 2 Error checking model 1805: {e}")
                partial_model_info = {'exists': True, 'error': 'Unable to process the request. Please verify the submitted data and try again.'}
        else:
            partial_model_info = {'exists': False}
            print("[DEBUG] Zone 2 Model 1805 (partial) does not exist")
        
        # Check lot_id data
        lot_id = 'LID021020251920110018'
        lot_data = {'lot_id': lot_id}
        
        try:
            # Check TotalStockModel
            total_stock = TotalStockModel.objects.select_related('model_stock_no').filter(lot_id=lot_id).first()
            if total_stock and total_stock.model_stock_no:
                lot_data['total_stock_model'] = total_stock.model_stock_no.model_no
                lot_data['total_stock_images'] = total_stock.model_stock_no.images.count()
                print(f"[DEBUG] Zone 2 TotalStockModel found: {total_stock.model_stock_no.model_no}")
            else:
                lot_data['total_stock_model'] = None
                print(f"[DEBUG] Zone 2 No TotalStockModel found for {lot_id}")
                
            # Also try RecoveryStockModel
            try:
                from Recovery_DP.models import RecoveryStockModel
                recovery_stock = RecoveryStockModel.objects.select_related('model_stock_no').filter(lot_id=lot_id).first()
                if recovery_stock and recovery_stock.model_stock_no:
                    lot_data['recovery_stock_model'] = recovery_stock.model_stock_no.model_no
                    lot_data['recovery_stock_images'] = recovery_stock.model_stock_no.images.count()
                    print(f"[DEBUG] Zone 2 RecoveryStockModel found: {recovery_stock.model_stock_no.model_no}")
                else:
                    lot_data['recovery_stock_model'] = None
                    print(f"[DEBUG] Zone 2 No RecoveryStockModel found for {lot_id}")
            except ImportError:
                lot_data['recovery_stock_model'] = 'Import error'
                print("[DEBUG] Zone 2 Could not import RecoveryStockModel")
        except Exception as e:
            print(f"[DEBUG] Zone 2 Error checking stock models: {e}")
            lot_data['stock_model_error'] = str(e)
        
        try:
            # Check JigUnloadAfterTable
            jig_unload = JigUnloadAfterTable.objects.filter(lot_id=lot_id).first()
            if jig_unload:
                lot_data['jig_unload_plating_stk_no'] = jig_unload.plating_stk_no
                print(f"[DEBUG] Zone 2 JigUnloadAfterTable found: plating_stk_no = {jig_unload.plating_stk_no}")
                if jig_unload.plating_stk_no:
                    # For Zone 2, extract just the numeric part (e.g., "1805SSA02" -> "1805")
                    match = re.match(r'^(\d+)', str(jig_unload.plating_stk_no))
                    if match:
                        extracted_model = match.group(1)
                        lot_data['extracted_model'] = extracted_model
                        # Check if extracted model exists
                        extracted_model_obj = ModelMaster.objects.filter(model_no=extracted_model).first()
                        if extracted_model_obj:
                            lot_data['extracted_model_exists'] = True
                            lot_data['extracted_model_images'] = extracted_model_obj.images.count()
                            print(f"[DEBUG] Zone 2 Extracted model {extracted_model} exists with {extracted_model_obj.images.count()} images")
                        else:
                            lot_data['extracted_model_exists'] = False
                            print(f"[DEBUG] Zone 2 Extracted model {extracted_model} does not exist")
                    else:
                        lot_data['extracted_model'] = None
                        print(f"[DEBUG] Zone 2 Could not extract numeric model from {jig_unload.plating_stk_no}")
            else:
                lot_data['jig_unload_plating_stk_no'] = None
                print(f"[DEBUG] Zone 2 No JigUnloadAfterTable found for {lot_id}")
        except Exception as e:
            print(f"[DEBUG] Zone 2 Error checking JigUnloadAfterTable: {e}")
            lot_data['jig_unload_error'] = str(e)
        
        # Get some sample lot_ids that might have working models for Zone 2
        sample_working_lot_ids = []
        try:
            # Find lot_ids from TotalStockModel that have models with images
            for model in working_models[:5]:
                sample_lots = TotalStockModel.objects.filter(
                    model_stock_no__model_no=model['model_no']
                ).values_list('lot_id', flat=True)[:2]
                sample_working_lot_ids.extend(list(sample_lots))
            print(f"[DEBUG] Zone 2 Found sample working lot_ids: {sample_working_lot_ids}")
        except Exception as e:
            print(f"[DEBUG] Zone 2 Error finding sample lot_ids: {e}")
        
        return JsonResponse({
            'success': True,
            'zone': 'Zone 2 (Non-IPS)',
            'total_models_with_images': models_with_images.count(),
            'working_models': working_models,
            'specific_model_1805SSA02': specific_model_info,
            'partial_model_1805': partial_model_info,
            'lot_id_analysis': lot_data,
            'sample_working_lot_ids': sample_working_lot_ids,
            'zone2_specifics': {
                'extraction_method': 'Extract numeric part using regex (e.g., "1805SSA02" -> "1805")',
                'model_mapping': 'Full model number -> Numeric part for image lookup'
            },
            'recommendations': {
                'solution_1': 'Add images to model 1805 (numeric part) in Django admin',
                'solution_2': f'Test with working models: {[m["model_no"] for m in working_models[:3]]}' if working_models else 'No models with images found',
                'solution_3': f'Test with these lot_ids that have images: {sample_working_lot_ids[:3]}' if sample_working_lot_ids else 'No working lot_ids found',
                'solution_4': 'Create model 1805 in ModelMaster and add images (for Zone 2 numeric extraction)'
            },
            'next_steps': [
                f'Go to http://localhost:8000/admin/',
                'Navigate to: modelmasterapp → Model masters',
                'Search for "1805" or create it if not found',
                'Add images to the model',
                'Test the Zone 2 image display functionality'
            ]
        })
        
    except Exception as e:
        logger.error(f"[DEBUG] Zone 2 Error in debug_model_availability: {str(e)}", exc_info=True)
        import traceback
        print(f"[DEBUG] Zone 2 Traceback: {traceback.format_exc()}")
        return JsonResponse({
            'success': False,
            'error': 'Unable to process the request. Please verify the submitted data and try again.',
            'message': 'Error analyzing Zone 2 model availability',
            'traceback': traceback.format_exc()
        })


# ==================== AUTO-SAVE FUNCTIONALITY (Zone 2) ====================

@login_required
@csrf_exempt
def JU_Zone_autosave_jig_unload(request):
    """Auto-save jig unload modal data on typing/changes for Zone 2"""
    import json
    import logging
    from Jig_Unloading.models import JigUnloadAutoSave
    
    logger = logging.getLogger(__name__)
    
    if request.method == 'POST':
        try:
            data = json.loads(request.body)
            
            main_lot_id = data.get('main_lot_id', '')
            if not main_lot_id:
                return JsonResponse({'success': False, 'error': 'main_lot_id required'})

            tray_data = data.get('tray_data', [])
            allowed_lot_ids_for_trays = [main_lot_id] + list(data.get('combined_lot_ids', []) or [])
            seen_tray_ids = set()
            for i, tray in enumerate(tray_data):
                if not isinstance(tray, dict):
                    continue
                tray_id = normalize_jig_unload_tray_id(tray.get('tray_id', ''))
                if not tray_id:
                    continue
                tray['tray_id'] = tray_id
                if not is_valid_jig_unload_tray_id_format(tray_id):
                    continue
                if tray_id in seen_tray_ids:
                    return JsonResponse({
                        'success': False,
                        'error': f'Duplicate tray ID "{tray_id}" in autosave data.'
                    }, status=400)
                seen_tray_ids.add(tray_id)
                tray_conflict = find_jig_unload_tray_conflict(
                    tray_id,
                    allowed_lot_ids=allowed_lot_ids_for_trays,
                    include_tray_master=True,
                )
                if tray_conflict:
                    return JsonResponse({
                        'success': False,
                        'error': tray_conflict['message'],
                        'validation_type': 'tray_occupied',
                        'linked_lot': tray_conflict.get('linked_lot', ''),
                        'source': tray_conflict.get('source', ''),
                        'tray_index': i,
                    }, status=400)
                nickel_conflict = validate_jig_unload_nickel_reject_tray(
                    tray_id,
                    current_lot_id=main_lot_id or None,
                )
                if nickel_conflict:
                    return JsonResponse({
                        'success': False,
                        'error': nickel_conflict['message'],
                        'validation_type': 'tray_occupied',
                        'linked_lot': nickel_conflict.get('linked_lot', ''),
                        'source': nickel_conflict.get('source', ''),
                        'tray_index': i,
                    }, status=400)
            
            # Get user or session key
            user = request.user if request.user.is_authenticated else None
            session_key = request.session.session_key if not user else None
            
            if not user and not session_key:
                # Create session if it doesn't exist
                request.session.create()
                session_key = request.session.session_key
            
            # Update or create auto-save record
            filter_kwargs = {'main_lot_id': main_lot_id}
            if user:
                filter_kwargs['user'] = user
                defaults = {'session_key': None}
            else:
                filter_kwargs['session_key'] = session_key
                defaults = {'user': None}
            
            # Add all the data fields to defaults
            defaults.update({
                'model_number': data.get('model_number', ''),
                'total_quantity': data.get('total_quantity', 0),
                'tray_data': tray_data,
                'combined_lot_ids': data.get('combined_lot_ids', []),
                'tray_type_capacity': data.get('tray_type_capacity', 'Normal - 20'),
                'missing_qty': data.get('missing_qty', 0),
                'jig_id': data.get('jig_id', ''),
            })
            
            autosave, created = JigUnloadAutoSave.objects.update_or_create(
                **filter_kwargs,
                defaults=defaults
            )
            
            logger.info(f"Zone 2 Auto-save {'created' if created else 'updated'} for lot_id: {main_lot_id}")
            
            return JsonResponse({
                'success': True,
                'action': 'created' if created else 'updated',
                'timestamp': autosave.updated_at.isoformat()
            })
            
        except json.JSONDecodeError as e:
            logger.error(f'Zone 2 Auto-save JSON decode error: {e}')
            return JsonResponse({'success': False, 'error': 'Invalid JSON data'})
        except Exception as e:
            logger.error(f'Zone 2 Auto-save error: {e}', exc_info=True)
            return JsonResponse({'success': False, 'error': 'Unable to process the request. Please verify the submitted data and try again.'})
    
    return JsonResponse({'success': False, 'error': 'POST method required'})


@login_required
@csrf_exempt
def JU_Zone_load_autosave_jig_unload(request, main_lot_id):
    """Load auto-saved data for a specific lot_id for Zone 2"""
    import logging
    from Jig_Unloading.models import JigUnloadAutoSave
    
    logger = logging.getLogger(__name__)
    
    try:
        # Get user or session key
        user = request.user if request.user.is_authenticated else None
        session_key = request.session.session_key if not user else None
        
        # Build filter for auto-save lookup
        filter_kwargs = {'main_lot_id': main_lot_id}
        if user:
            filter_kwargs['user'] = user
        elif session_key:
            filter_kwargs['session_key'] = session_key
        else:
            return JsonResponse({
                'success': True,
                'has_autosave': False,
                'message': 'No user session found'
            })
        
        autosave = JigUnloadAutoSave.objects.filter(**filter_kwargs).first()
        
        if autosave and autosave.has_meaningful_data():
            return JsonResponse({
                'success': True,
                'has_autosave': True,
                'autosave_data': autosave.to_dict(),
                'last_updated': autosave.updated_at.isoformat()
            })
        else:
            return JsonResponse({
                'success': True,
                'has_autosave': False
            })
            
    except Exception as e:
        logger.error(f'Zone 2 Load auto-save error: {e}', exc_info=True)
        return JsonResponse({'success': False, 'error': 'Unable to process the request. Please verify the submitted data and try again.'})


@login_required
@csrf_exempt
def JU_Zone_clear_autosave_jig_unload(request, main_lot_id):
    """Clear auto-save data after successful submit/draft for Zone 2"""
    import logging
    from Jig_Unloading.models import JigUnloadAutoSave
    
    logger = logging.getLogger(__name__)
    
    if request.method == 'DELETE':
        try:
            # Get user or session key
            user = request.user if request.user.is_authenticated else None
            session_key = request.session.session_key if not user else None
            
            # Build filter for auto-save deletion
            filter_kwargs = {'main_lot_id': main_lot_id}
            if user:
                filter_kwargs['user'] = user
            elif session_key:
                filter_kwargs['session_key'] = session_key
            else:
                return JsonResponse({'success': True, 'message': 'No session found'})
            
            deleted_count, _ = JigUnloadAutoSave.objects.filter(**filter_kwargs).delete()
            
            logger.info(f"Zone 2 Cleared {deleted_count} auto-save record(s) for lot_id: {main_lot_id}")
            
            return JsonResponse({
                'success': True,
                'deleted_count': deleted_count
            })
            
        except Exception as e:
            logger.error(f'Zone 2 Clear auto-save error: {e}', exc_info=True)
            return JsonResponse({'success': False, 'error': 'Unable to process the request. Please verify the submitted data and try again.'})
    
    return JsonResponse({'success': False, 'error': 'DELETE method required'})


@login_required
@csrf_exempt
@require_POST
def JU_Zone_delete_jig_details(request):
    """
    Delete a jig record from JigCompleted table (Zone 2)
    """
    try:
        data = json.loads(request.body)
        lot_id = data.get('lot_id')
        
        if not lot_id:
            return JsonResponse({
                'success': False, 
                'message': 'Lot ID is required'
            })
        
        # Find the jig detail record
        jig_detail = JigCompleted.objects.filter(lot_id=lot_id).first()
        
        if not jig_detail:
            return JsonResponse({
                'success': False,
                'message': f'No record found with Lot ID: {lot_id}'
            })
        
        # Check if the jig has any dependent records that should be preserved
        # You might want to add validation here based on business rules
        
        # Delete the record
        jig_detail.delete()
        
        return JsonResponse({
            'success': True,
            'message': f'Zone 2: Record with Lot ID {lot_id} deleted successfully'
        })
        
    except json.JSONDecodeError:
        return JsonResponse({
            'success': False,
            'message': 'Invalid JSON data'
        })
    except Exception as e:
        return JsonResponse({
            'success': False,
            'message': 'Unable to process the request. Please verify the submitted data and try again.'
        })

@require_GET
def JU_Zone_get_jig_for_tray(request):
    """Get jig information for a scanned tray ID (Zone 2)"""
    tray_id = request.GET.get('tray_id', '').strip()

    if not tray_id:
        return JsonResponse({
            'success': False,
            'error': 'Tray ID is required'
        })

    try:
        # First, try to find JigCompleted that contain this tray_id in their lot_id_quantities
        jig_detail = JigCompleted.objects.filter(
            draft_data__lot_id_quantities__has_key=tray_id
        ).first()

        if jig_detail:
            # Check if this jig is in draft mode
            is_draft = bool(jig_detail.jig_unload_draft)
            return JsonResponse({
                'success': True,
                'jig_id': jig_detail.id,
                'lot_id': jig_detail.lot_id,
                'is_draft': is_draft
            })

        # If not found in lot_id_quantities, check draft data
        # Look for jigs with draft data that contain this tray_id
        draft_jigs = JigCompleted.objects.exclude(jig_unload_draft__isnull=True).exclude(jig_unload_draft='')

        for jig in draft_jigs:
            try:
                draft_data = json.loads(jig.jig_unload_draft) if isinstance(jig.jig_unload_draft, str) else jig.jig_unload_draft
                if draft_data and 'tray_data' in draft_data:
                    for tray_item in draft_data['tray_data']:
                        if tray_item.get('tray_id') == tray_id:
                            return JsonResponse({
                                'success': True,
                                'jig_id': jig.id,
                                'lot_id': jig.lot_id,
                                'is_draft': True  # Draft jigs are always draft
                            })
            except (json.JSONDecodeError, TypeError):
                continue

        return JsonResponse({
            'success': False,
            'error': 'Tray ID not found or not in draft jig'
        })

    except Exception as e:
        return JsonResponse({
            'success': False,
            'error': 'Unable to process the request. Please verify the submitted data and try again.'
        })

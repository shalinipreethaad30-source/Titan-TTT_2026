from django.views.generic import *
from modelmasterapp.models import *
from .models import Jig, JigLoadingMaster, JigLoadTrayId, JigLoadingManualDraft, JigCompleted, JigLoadingRecord, JigDelinkRecord, ExcessLotRecord, ExcessLotTray
from rest_framework.decorators import *
from django.http import JsonResponse
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from django.shortcuts import get_object_or_404
from django.utils import timezone
from math import ceil
from rest_framework.permissions import IsAuthenticated
from django.views.decorators.csrf import csrf_exempt
from django.utils.decorators import method_decorator
from django.contrib.auth.decorators import login_required
import logging
logger = logging.getLogger(__name__)
import re
import json
from django.db import transaction
from django.core.paginator import Paginator
from django.db.models import Count
from datetime import datetime, timezone as dt_timezone
from django.views.generic import TemplateView
from rest_framework.permissions import IsAuthenticated
from django.utils.decorators import method_decorator
from django.contrib.auth.decorators import login_required
from rest_framework import exceptions
from BrassAudit.models import Brass_Audit_Accepted_TrayID_Store
# from BrassAudit.views import brass_audit_get_accepted_tray_scan_data
from modelmasterapp.models import TotalStockModel
from modelmasterapp.models import ModelMasterCreation
from modelmasterapp.type_of_input import get_type_of_input_for_batch


# ===== MULTI-MODEL HELPER FUNCTIONS =====
def allocate_trays_for_model(lot_id, model_lot_qty, effective_capacity_remaining, used_tray_ids):
	"""
	Fetch and allocate trays for a specific model.
	
	Args:
		lot_id: Model's lot ID
		model_lot_qty: Target quantity for this model
		effective_capacity_remaining: Remaining capacity in jig
		used_tray_ids: Set of tray IDs already allocated (for deduplication)
	
	Returns:
		{
			'allocated_qty': total allocated,
			'tray_info': [{'tray_id', 'qty'}, ...],
			'allocated_tray_ids': set of allocated tray IDs
		}
	"""
	try:
		allocated_tray_ids = set()
		tray_info = []
		total_allocated = 0
		
		# Fetch trays for this specific lot_id — unified resolver (JigLoadTrayId → BrassAuditTrayId → BrassTrayId)
		tray_list = fetch_trays_for_lot(lot_id)
		
		for tray_item in tray_list:
			tray_id = tray_item.get('tray_id', '')
			
			# Skip if already used by another model
			if tray_id in used_tray_ids:
				logging.warning(f"[MULTI_MODEL] Tray {tray_id} skipped (already allocated)")
				continue
			
			tray_qty = int(tray_item.get('qty', 0) or 0)
			
			# Stop if we've met this model's quota
			if total_allocated >= model_lot_qty:
				break
			
			# Check if full tray fits within model's remaining allocation
			if total_allocated + tray_qty <= model_lot_qty:
				# Full tray fits
				tray_info.append({
					'tray_id': tray_id,
					'qty': tray_qty
				})
				allocated_tray_ids.add(tray_id)
				total_allocated += tray_qty
			else:
				# Partial tray: take only what's needed for this model
				remaining_for_model = model_lot_qty - total_allocated
				tray_info.append({
					'tray_id': tray_id,
					'qty': remaining_for_model
				})
				allocated_tray_ids.add(tray_id)
				total_allocated += remaining_for_model
				break
		
		logging.info(f"[MULTI_MODEL] Model {lot_id}: allocated {total_allocated} qty in {len(tray_info)} trays")
		
		return {
			'allocated_qty': total_allocated,
			'tray_info': tray_info,
			'allocated_tray_ids': allocated_tray_ids
		}
	except Exception as e:
		logging.exception(f"[MULTI_MODEL] Error allocating trays for {lot_id}: {e}")
		return {
			'allocated_qty': 0,
			'tray_info': [],
			'allocated_tray_ids': set()
		}


def fetch_model_metadata(lot_id, batch_id):
	"""Fetch model metadata for display (plating_stk_no, etc.)"""
	try:
		logging.info(f'[MULTI_MODEL] fetch_model_metadata called: lot_id={lot_id}, batch_id={batch_id}')
		# Try batch first
		batch_obj = ModelMasterCreation.objects.filter(batch_id=batch_id).first() if batch_id else None
		if batch_obj:
			plating_stk = getattr(batch_obj, 'plating_stk_no', '') or ''
			if not plating_stk:
				# Fall back to model_stock_no FK's plating_stk_no
				model_master = getattr(batch_obj, 'model_stock_no', None)
				if model_master:
					plating_stk = getattr(model_master, 'plating_stk_no', '') or ''
			logging.info(f'[MULTI_MODEL] fetch_model_metadata resolved via batch: {plating_stk}')
			return str(plating_stk) if plating_stk else f"Model-{lot_id}"
		
		# Fallback to lot-based lookup
		stock = TotalStockModel.objects.filter(lot_id=lot_id).first()
		if stock and hasattr(stock, 'batch_id'):
			batch = getattr(stock, 'batch_id', None)
			if batch:
				plating_stk = getattr(batch, 'plating_stk_no', '') or ''
				if not plating_stk:
					model_master = getattr(batch, 'model_stock_no', None)
					if model_master:
						plating_stk = getattr(model_master, 'plating_stk_no', '') or ''
				logging.info(f'[MULTI_MODEL] fetch_model_metadata resolved via lot fallback: {plating_stk}')
				return str(plating_stk) if plating_stk else f"Model-{lot_id}"
		
		return f"Model-{lot_id}"
	except Exception as e:
		logging.exception(f"[MULTI_MODEL] Error fetching metadata for {lot_id}: {e}")
		return f"Model-{lot_id}"


def fetch_model_image_metadata(lot_id, batch_id):
	"""Fetch model image URL and label for a given lot/batch (multi-model UI).
	Returns dict with model_image_url and model_image_label."""
	result = {
		'model_image_url': '/static/assets/images/imagePlaceholder.jpg',
		'model_image_label': ''
	}
	try:
		logging.info(f'[MULTI_MODEL] fetch_model_image_metadata called: lot_id={lot_id}, batch_id={batch_id}')
		mm = None
		batch_obj = ModelMasterCreation.objects.filter(batch_id=batch_id).first() if batch_id else None
		if batch_obj:
			mm = getattr(batch_obj, 'model_stock_no', None)
			# Use batch-level plating_stk_no first (more specific than model master)
			batch_plating = getattr(batch_obj, 'plating_stk_no', '') or ''
			if batch_plating:
				result['model_image_label'] = batch_plating
		if not mm:
			stock = TotalStockModel.objects.filter(lot_id=lot_id).first()
			if stock and hasattr(stock, 'batch_id'):
				b = getattr(stock, 'batch_id', None)
				if b:
					mm = getattr(b, 'model_stock_no', None)
					# Use batch-level plating_stk_no from lot fallback
					batch_plating = getattr(b, 'plating_stk_no', '') or ''
					if batch_plating and not result['model_image_label']:
						result['model_image_label'] = batch_plating
		if mm:
			try:
				if hasattr(mm, 'images'):
					from modelmasterapp.image_utils import sort_images_front_first
					imgs = mm.images.all()
					if imgs and imgs.exists():
						first_img = sort_images_front_first(imgs)[0]
						if getattr(first_img, 'master_image', None):
							result['model_image_url'] = first_img.master_image.url
			except Exception:
				pass
			# Only set label from model master if batch didn't provide one
			if not result['model_image_label']:
				result['model_image_label'] = getattr(mm, 'plating_stk_no', '') or getattr(mm, 'model_no', '') or ''
		# Append lot_id suffix for multi-model disambiguation
		if result['model_image_label'] and lot_id:
			result['model_image_label'] = f"{result['model_image_label']} [{lot_id}]"
		logging.info(f'[MULTI_MODEL] fetch_model_image_metadata result: label={result["model_image_label"]}')
	except Exception as e:
		logging.exception(f"[MULTI_MODEL] Error fetching image metadata for {lot_id}: {e}")
	return result


def exclude_draft_rows_from_add_model_filter(rows, is_add_model_popup):
	"""Hide active draft allocations only from the Add Model filter screen."""
	if not is_add_model_popup:
		return rows
	return [
		row for row in rows
		if row.get('lot_status') not in {'Draft', 'Partial Draft'}
	]

@method_decorator(login_required, name='dispatch')
class JigView(TemplateView):
	"""Minimal Jig view to render the pick table template."""
	template_name = "JigLoading/Jig_Picktable.html"

	def get(self, request, *args, **kwargs):
		import time as _time
		_full_start = _time.time()
		response = super().get(request, *args, **kwargs)
		# Force template rendering so we can measure it
		if hasattr(response, 'render') and callable(response.render):
			response.render()
		print(f"[JIG PERF] FULL REQUEST (context + template render): {_time.time() - _full_start:.3f}s")
		return response

	def get_context_data(self, **kwargs):
		import time as _time
		_t0 = _time.time()
		context = super().get_context_data(**kwargs)
		# Populate master_data with Brass Audit accepted lots so Jig Pick shows them
		try:
			from modelmasterapp.models import TotalStockModel
			master_data = []
			# Build base queryset without slicing so we can safely apply exclusions
			from django.db.models import Q as _Q
			base_qs = TotalStockModel.objects.filter(
				_Q(brass_audit_accptance=True) |
				_Q(brass_audit_few_cases_accptance=True, brass_audit_onhold_picking=False)
			).select_related('batch_id', 'batch_id__model_stock_no', 'jig_hold_by', 'jig_release_by')
			# Optional exclusion: when JigView is opened to "Add Model", exclude already-selected lots
			# Frontend sends comma-separated lot IDs: exclude_lot_id=LID1,LID2,LID3
			exclude_lot_raw = self.request.GET.get('exclude_lot_id', '')
			primary_lot = self.request.GET.get('primary_lot_id') or self.request.GET.get('primary_lot')
			exclude_list = [x.strip() for x in exclude_lot_raw.split(',') if x.strip()]
			if exclude_list:
				base_qs = base_qs.exclude(lot_id__in=exclude_list)
			# PSN-based exclusion: already-selected models (primary + any added) by plating_stk_no.
			# selected_models param = comma-separated PSNs already on the jig.
			selected_models_raw = self.request.GET.get('selected_models', '')
			selected_model_psns = [x.strip() for x in selected_models_raw.split(',') if x.strip()]
			_t1 = _time.time()
			print(f"[JIG PERF] base_qs built: {_t1 - _t0:.3f}s")
			# Exclude lots already SUBMITTED in JigCompleted (they move to Completed table).
			# In Add Model flow, exclude other drafts but never use the current draft's
			# stale multi_model_allocation to hide a model the user has just removed.
			try:
				is_merge_return = bool(self.request.GET.get('merge_model'))
				is_add_model_popup = bool(exclude_lot_raw) and not is_merge_return
				# Held lots must never be selectable in the Add Jig -> Model Filter screen.
				# They remain visible (blurred) on the main pick table, so this exclusion
				# is scoped strictly to the Add Model popup context.
				if is_add_model_popup:
					base_qs = base_qs.exclude(jig_hold_lot=True)
				# Exclude exact selected lots by lot_id only. Same/ditto PSN lots are valid
				# Add Model candidates when another accepted lot exists for the same model.
				if is_add_model_popup and selected_model_psns:
					print(f'[JIG PICK] Same/ditto PSNs allowed in Add Model flow: {selected_model_psns}')
				# Submitted lots are permanently consumed.
				exclude_lot_ids = set(
					JigCompleted.objects.filter(
						draft_status='submitted'
					).values_list('lot_id', flat=True)
				)
				# For submitted multi-model records, also exclude secondary lot IDs.
				for rec in JigCompleted.objects.filter(
					draft_status='submitted',
					is_multi_model=True,
					multi_model_allocation__isnull=False
				).only('multi_model_allocation')[:100]:
					if rec.multi_model_allocation:
						for m in rec.multi_model_allocation:
							mlot = m.get('lot_id', '') if isinstance(m, dict) else ''
							if mlot:
								exclude_lot_ids.add(mlot)
				if is_add_model_popup:
					other_drafts = JigCompleted.objects.filter(draft_status__in=['draft', 'active'])
					if primary_lot:
						other_drafts = other_drafts.exclude(lot_id=primary_lot)
					exclude_lot_ids.update(other_drafts.values_list('lot_id', flat=True))
					for rec in other_drafts.filter(
						is_multi_model=True,
						multi_model_allocation__isnull=False
					).only('multi_model_allocation')[:100]:
						if rec.multi_model_allocation:
							for m in rec.multi_model_allocation:
								mlot = m.get('lot_id', '') if isinstance(m, dict) else ''
								if mlot:
									exclude_lot_ids.add(mlot)
				if exclude_lot_ids:
					base_qs = base_qs.exclude(lot_id__in=list(exclude_lot_ids))
			except Exception:
				logging.exception("[JIG PICK] Failed to exclude submitted/draft lots")
			_t2 = _time.time()
			print(f"[JIG PERF] exclude lots: {_t2 - _t1:.3f}s")
			# ===== ADD MODEL ELIGIBILITY FILTER (DB-driven, Add Model flow only) =====
			# When primary_lot_id + exclude_lot_id present, restrict pick table to
			# backend-approved PSNs: same micro group plus same/ditto model rows.
			# eligible_psns_for_filter is set here and reused by the excess lot loop below.
			eligible_psns_for_filter = None  # None = no filter active; [] = filter active but empty
			if primary_lot and is_add_model_popup:
				try:
					from Jig_Loading.model_combination_validator import resolve_add_model_eligible_psns

					# Resolve primary PSN from request first, then backend sources.
					primary_psn = (self.request.GET.get('primary_psn', '') or '').strip()
					if not primary_psn:
						row = TotalStockModel.objects.filter(lot_id=primary_lot).values('batch_id__plating_stk_no').first()
						primary_psn = ((row or {}).get('batch_id__plating_stk_no') or '').strip()
					if not primary_psn:
						row = ModelMasterCreation.objects.filter(lot_id=primary_lot).values('plating_stk_no').first()
						primary_psn = ((row or {}).get('plating_stk_no') or '').strip()
					if not primary_psn:
						row = JigCompleted.objects.filter(lot_id=primary_lot).values('plating_stock_num').first()
						primary_psn = ((row or {}).get('plating_stock_num') or '').strip()

					print(f'[JIG PICK] Add-model filter: primary_psn={primary_psn!r}')

					if not primary_psn:
						print(f'[JIG PICK] Add-model filter: primary_psn not resolved for lot_id={primary_lot!r} — showing no rows')
						base_qs = base_qs.none()
						eligible_psns_for_filter = []
					else:
						selected_psns_for_validation = [
							psn for psn in selected_model_psns
							if psn and psn != primary_psn
						]
						eligible_psns, invalid_selected = resolve_add_model_eligible_psns(primary_psn, selected_psns_for_validation)
						print(f'[JIG PICK] Add-model filter: eligible_psns={eligible_psns}, invalid_selected={invalid_selected}')

						if invalid_selected or not eligible_psns:
							base_qs = base_qs.none()
							eligible_psns_for_filter = []
						else:
							base_qs = base_qs.filter(batch_id__plating_stk_no__in=eligible_psns)
							eligible_psns_for_filter = eligible_psns
							print(f'[JIG PICK] Add-model filter applied: base_qs count={base_qs.count()}')
				except Exception:
					logging.exception("[JIG PICK] Failed to apply Add Model eligibility filter")
			# Apply ordering — no slice limit in Add Model mode (show all eligible)
			_slice = None if (primary_lot and is_add_model_popup) else 10
			qs = base_qs.only(
				'lot_id', 'batch_id', 'brass_audit_accepted_qty',
				'brass_audit_physical_qty', 'total_stock',
				'brass_audit_last_process_date_time',
				'BA_pick_remarks',
				'jig_hold_lot', 'jig_holding_reason', 'jig_release_lot', 'jig_release_reason',
				'jig_hold_by', 'jig_hold_at', 'jig_release_by', 'jig_release_at', 'plating_color',
				'batch_id__batch_id', 'batch_id__plating_stk_no',
				'batch_id__polishing_stk_no', 'batch_id__plating_color',
				'batch_id__polish_finish', 'batch_id__model_stock_no',
				'batch_id__model_stock_no__id',
			).order_by('-brass_audit_last_process_date_time') if _slice is None else base_qs.only(
				'lot_id', 'batch_id', 'brass_audit_accepted_qty',
				'brass_audit_physical_qty', 'total_stock',
				'brass_audit_last_process_date_time',
				'BA_pick_remarks',
				'jig_hold_lot', 'jig_holding_reason', 'jig_release_lot', 'jig_release_reason',
				'jig_hold_by', 'jig_hold_at', 'jig_release_by', 'jig_release_at', 'plating_color',
				'batch_id__batch_id', 'batch_id__plating_stk_no',
				'batch_id__polishing_stk_no', 'batch_id__plating_color',
				'batch_id__polish_finish', 'batch_id__model_stock_no',
				'batch_id__model_stock_no__id',
			).order_by('-brass_audit_last_process_date_time')[:10]

			# ===== BULK PRE-FETCH: eliminate N+1 queries in the row loop =====
			# FIX 1: Extract lot_ids from already-fetched qs first
			_t_extract_start = _time.time()
			qs_list = list(qs)
			qs_lot_ids = [obj.lot_id for obj in qs_list]
			_t_extract = _time.time()
			print(f"[JIG PERF] extracted {len(qs_lot_ids)} lot_ids: {_t_extract - _t_extract_start:.3f}s")
			
			try:
				# FIX 2: DYNAMICALLY fetch tray counts using unified resolver (not hardcoded table)
				if qs_lot_ids:
					tray_counts = {}
					for lot_id in qs_lot_ids:
						tray_counts[lot_id] = count_trays_for_lot(lot_id)
				else:
					tray_counts = {}
			except Exception as e:
				logging.exception(f'[JIG PICK] Failed to count trays dynamically: {e}')
				tray_counts = {}
			try:
				master_capacity_map = {
					m.model_stock_no_id: int(m.jig_capacity)
					for m in JigLoadingMaster.objects.filter(jig_capacity__isnull=False)
				}
			except Exception:
				master_capacity_map = {}
			_t3 = _time.time()
			print(f"[JIG PERF] bulk prefetch: {_t3 - _t_extract:.3f}s")
			_t_stock_loop = _time.time()

			for stock in qs_list:
				batch = getattr(stock, 'batch_id', None)
				# Use dynamically fetched tray counts (unified resolver: JigLoadTrayId → BrassAuditTrayId → BrassTrayId)
				no_of_trays = tray_counts.get(stock.lot_id, 0)
				data = {
					'batch_id': getattr(batch, 'batch_id', '') if batch else '',
					'stock_lot_id': getattr(stock, 'lot_id', ''),
					'plating_stk_no': getattr(batch, 'plating_stk_no', '') if batch else '',
					'polishing_stk_no': getattr(batch, 'polishing_stk_no', '') if batch else '',
					'plating_color': getattr(batch, 'plating_color', ''),
					'polish_finish': getattr(batch, 'polish_finish', ''),
					'no_of_trays': no_of_trays,
					'display_qty': getattr(stock, 'brass_audit_accepted_qty', None) or getattr(stock, 'brass_audit_physical_qty', None) or getattr(stock, 'total_stock', 0),
					# Prefer jig capacity from JigLoadingMaster (per-model) else fall back to batch.tray_capacity
					'jig_capacity': None,
					'brass_audit_last_process_date_time': getattr(stock, 'brass_audit_last_process_date_time', None),
					'model_stock_no': getattr(batch, 'model_stock_no', None) if batch else None,
					# model images: prefer batch images, else model master images
					'model_images': [],  # images resolved lazily in template via data-attribute only
					'jig_hold_lot': getattr(stock, 'jig_hold_lot', False),
					'jig_holding_reason': getattr(stock, 'jig_holding_reason', ''),
					'jig_release_lot': getattr(stock, 'jig_release_lot', False),
					'jig_release_reason': getattr(stock, 'jig_release_reason', ''),
					'jig_hold_by': stock.jig_hold_by.username if getattr(stock, 'jig_hold_by', None) else '',
					'jig_hold_at': timezone.localtime(stock.jig_hold_at).strftime("%d-%b-%Y %I:%M %p") if getattr(stock, 'jig_hold_at', None) else '',
					'jig_release_by': stock.jig_release_by.username if getattr(stock, 'jig_release_by', None) else '',
					'jig_release_at': timezone.localtime(stock.jig_release_at).strftime("%d-%b-%Y %I:%M %p") if getattr(stock, 'jig_release_at', None) else '',
					'previous_module': 'Brass Audit',
					'previous_module_remark': getattr(stock, 'BA_pick_remarks', '') or '',
				}

				# Resolve by exact plating stock first; fall back to the pre-fetched FK map.
				# This avoids stale legacy model_stock_no links overriding the exact model.
				model_obj = getattr(batch, 'model_stock_no', None) if batch else None
				if batch:
					resolved_cap = resolve_jig_capacity_for_batch(batch)
					if resolved_cap:
						data['jig_capacity'] = resolved_cap
				if not data['jig_capacity'] and model_obj:
					cap = master_capacity_map.get(getattr(model_obj, 'id', None))
					if cap:
						data['jig_capacity'] = cap
				master_data.append(data)

			_t4 = _time.time()
			print(f"[JIG PERF] stock loop ({len(master_data)} rows): {_t4 - _t_stock_loop:.3f}s")

			# ===== ADD HALF-FILLED / EXCESS LOT RECORDS BACK TO PICK TABLE =====
			# When a jig is submitted with excess qty, those trays (stored in half_filled_tray_info)
			# need to appear in the pick table as available for the next cycle.
			try:
				submitted_with_excess = list(JigCompleted.objects.filter(
					draft_status='submitted',
					half_filled_tray_qty__gt=0
				).only(
					'lot_id', 'batch_id', 'plating_stock_num', 'half_filled_tray_qty',
					'half_filled_tray_info', 'delink_tray_info', 'draft_data', 'tray_type', 'tray_capacity',
					'is_multi_model', 'multi_model_allocation', 'jig_id',
					'nickel_bath_type', 'excess_qty', 'updated_at'
				)[:50])

				# Bulk prefetch TotalStockModel for all excess source lots (eliminates N+1)
				excess_lot_ids = set()
				for jc in submitted_with_excess:
					excess_lot_ids.add(jc.lot_id)
				excess_stock_map = {}
				if excess_lot_ids:
					for s in TotalStockModel.objects.filter(
						lot_id__in=list(excess_lot_ids)
					).select_related('batch_id', 'batch_id__model_stock_no'):
						excess_stock_map[s.lot_id] = s

				# Bulk prefetch ExcessLotRecord to get real excess lot IDs (EX-*)
				excess_lot_record_map = {}  # key: (parent_lot_id, batch_id, jig_id) → ExcessLotRecord
				if excess_lot_ids:
					for elr in ExcessLotRecord.objects.filter(parent_lot_id__in=list(excess_lot_ids)).order_by('-created_at'):
						key = (elr.parent_lot_id, elr.parent_batch_id, elr.jig_id)
						if key not in excess_lot_record_map:
							excess_lot_record_map[key] = elr

				# Bulk prefetch submitted EX-* lot_ids to skip excess lots already submitted
				submitted_excess_lot_ids = set()
				all_excess_new_lot_ids = set(elr.new_lot_id for elr in excess_lot_record_map.values() if elr.new_lot_id)
				if all_excess_new_lot_ids:
					# Check both direct submissions and secondary lot references in multi-model submissions
					submitted_excess_lot_ids = set(
						JigCompleted.objects.filter(
							lot_id__in=list(all_excess_new_lot_ids),
							draft_status='submitted'
						).values_list('lot_id', flat=True)
					)
					# Also check if any EX-* lot appears as a secondary in a submitted multi-model
					for rec in JigCompleted.objects.filter(
						draft_status='submitted',
						is_multi_model=True,
						multi_model_allocation__isnull=False
					).only('multi_model_allocation')[:100]:
						if rec.multi_model_allocation:
							for m in rec.multi_model_allocation:
								mlot = m.get('lot_id', '') if isinstance(m, dict) else ''
								if mlot in all_excess_new_lot_ids:
									submitted_excess_lot_ids.add(mlot)

				# Bulk prefetch TotalStockModel rows for real EX-* lot ids (no N+1).
				# Hold/unhold state (jig_hold_lot, reasons, audit fields) is SSOT on
				# these rows — the excess loop below must render it, not hardcode False,
				# otherwise a held excess lot loses its row blur after page refresh.
				if all_excess_new_lot_ids:
					for s in TotalStockModel.objects.filter(
						lot_id__in=list(all_excess_new_lot_ids)
					).select_related('batch_id', 'batch_id__model_stock_no', 'jig_hold_by', 'jig_release_by'):
						excess_stock_map.setdefault(s.lot_id, s)

				for jc in submitted_with_excess:
					# For multi-model, find which source lot the excess trays belong to
					# by cross-referencing half_filled_tray_info tray_ids with all tray data
					excess_source_lot = jc.lot_id  # default to primary
					excess_model_name = jc.plating_stock_num or ''
					if jc.is_multi_model and jc.half_filled_tray_info:
						hf_tray_ids = {t.get('tray_id') for t in jc.half_filled_tray_info if isinstance(t, dict)}
						# Build lookup from draft_data.tray_data (has ALL trays including pure excess)
						# Fallback to delink_tray_info (only has trays with delink_qty > 0)
						all_tray_map = {}
						if jc.draft_data and isinstance(jc.draft_data, dict):
							for t in jc.draft_data.get('tray_data', []):
								if isinstance(t, dict) and t.get('tray_id'):
									all_tray_map[t['tray_id']] = t
						if not all_tray_map and jc.delink_tray_info:
							for t in jc.delink_tray_info:
								if isinstance(t, dict) and t.get('tray_id'):
									all_tray_map[t['tray_id']] = t
						for hf_tid in hf_tray_ids:
							if hf_tid in all_tray_map:
								src = all_tray_map[hf_tid].get('source_lot_id', '')
								if src:
									excess_source_lot = src
									mc = all_tray_map[hf_tid].get('model_code', '')
									if mc:
										excess_model_name = mc.split(' [')[0]  # strip lot ref
									break
					
					# Look up stock model from bulk-prefetched map (no per-row DB query)
					stock = excess_stock_map.get(excess_source_lot) or excess_stock_map.get(jc.lot_id)
					batch = getattr(stock, 'batch_id', None) if stock else None

					# Resolve real excess lot ID (EX-*) from ExcessLotRecord
					elr_key = (jc.lot_id, jc.batch_id, jc.jig_id)
					excess_lot_record = excess_lot_record_map.get(elr_key)
					real_excess_lot_id = excess_lot_record.new_lot_id if excess_lot_record else excess_source_lot

					# Skip excess lots that have already been submitted
					if real_excess_lot_id in submitted_excess_lot_ids:
						logging.info(f'[JIG PICK] Skipping excess lot {real_excess_lot_id} — already submitted')
						continue

					# Skip excess lots explicitly excluded by lot ID (e.g. primary lot in Add Model flow)
					if exclude_list and real_excess_lot_id in set(exclude_list):
						continue

					# In Add Model mode, only show excess lots from backend-approved PSNs.
					excess_psn_val = (excess_model_name or jc.plating_stock_num or '').strip().split(' [')[0]
					if eligible_psns_for_filter is not None:
						if excess_psn_val not in eligible_psns_for_filter:
							continue

					# Exact selected lots are excluded by lot_id above. Same/ditto PSNs remain eligible
					# so Add Model can show available excess partial-draft lots for the same model.

					# Hold/unhold state comes from the EX-* lot's TotalStockModel row (SSOT).
					# Falls back to unheld when no stock row exists (legacy excess lots).
					hold_src = excess_stock_map.get(real_excess_lot_id)

					excess_data = {
						'batch_id': jc.batch_id,
						'stock_lot_id': real_excess_lot_id,
						'plating_stk_no': excess_model_name if excess_model_name else (jc.plating_stock_num or ''),
						'polishing_stk_no': getattr(batch, 'polishing_stk_no', '') if batch else '',
						'plating_color': getattr(batch, 'plating_color', '') if batch else '',
						'polish_finish': getattr(batch, 'polish_finish', '') if batch else '',
						'no_of_trays': len(jc.half_filled_tray_info) if jc.half_filled_tray_info else 0,
						'display_qty': jc.half_filled_tray_qty or jc.excess_qty or 0,
						'jig_capacity': None,
						'brass_audit_last_process_date_time': jc.updated_at,
						'model_stock_no': getattr(batch, 'model_stock_no', None) if batch else None,
						'model_images': [],
						'jig_hold_lot': getattr(hold_src, 'jig_hold_lot', False) if hold_src else False,
						'jig_holding_reason': (getattr(hold_src, 'jig_holding_reason', '') or '') if hold_src else '',
						'jig_release_lot': getattr(hold_src, 'jig_release_lot', False) if hold_src else False,
						'jig_release_reason': (getattr(hold_src, 'jig_release_reason', '') or '') if hold_src else '',
						'jig_hold_by': hold_src.jig_hold_by.username if hold_src and getattr(hold_src, 'jig_hold_by', None) else '',
						'jig_hold_at': timezone.localtime(hold_src.jig_hold_at).strftime("%d-%b-%Y %I:%M %p") if hold_src and getattr(hold_src, 'jig_hold_at', None) else '',
						'jig_release_by': hold_src.jig_release_by.username if hold_src and getattr(hold_src, 'jig_release_by', None) else '',
						'jig_release_at': timezone.localtime(hold_src.jig_release_at).strftime("%d-%b-%Y %I:%M %p") if hold_src and getattr(hold_src, 'jig_release_at', None) else '',
						'previous_module': 'Brass Audit',
						'previous_module_remark': getattr(stock, 'BA_pick_remarks', '') if stock else '',
						'is_excess_lot': True,
						'source_jig_id': jc.jig_id,
						'half_filled_tray_info_json': json.dumps(jc.half_filled_tray_info or []),
						'type_of_input': get_type_of_input_for_batch(batch),
					}
					# Resolve by exact plating stock first; fall back to the pre-fetched FK map.
					# This avoids stale legacy model_stock_no links overriding the exact model.
					model_obj = getattr(batch, 'model_stock_no', None) if batch else None
					if batch:
						resolved_cap = resolve_jig_capacity_for_batch(batch)
						if resolved_cap:
							excess_data['jig_capacity'] = resolved_cap
					if not excess_data['jig_capacity'] and model_obj:
						cap = master_capacity_map.get(getattr(model_obj, 'id', None))
						if cap:
							excess_data['jig_capacity'] = cap
					master_data.append(excess_data)

			except Exception:
				logging.exception("[JIG PICK] Failed to add excess lot records to pick table")

			_t5 = _time.time()
			print(f"[JIG PERF] excess lot loop: {_t5 - _t4:.3f}s")

			# ===== DEDUPLICATE: excess lots appear in both TotalStockModel and excess loop =====
			# When JigSaveAPI submits a lot with excess, it creates a TotalStockModel entry for
			# the EX-* lot (brass_audit_accptance=True). This causes the EX-* lot to appear in
			# base_qs (as "Yet to Start") AND in the excess loop (as "Released").
			# Rule: keep only the excess-loop entry (is_excess_lot=True) when duplicate exists.
			try:
				excess_lot_ids_in_table = {d['stock_lot_id'] for d in master_data if d.get('is_excess_lot')}
				if excess_lot_ids_in_table:
					before_dedup = len(master_data)
					master_data = [
						d for d in master_data
						if d.get('is_excess_lot') or d.get('stock_lot_id') not in excess_lot_ids_in_table
					]
					removed = before_dedup - len(master_data)
					if removed:
						logging.info(f'[JIG PICK] Removed {removed} duplicate TotalStockModel row(s) for excess lots: {excess_lot_ids_in_table}')
			except Exception:
				logging.exception('[JIG PICK] Failed to deduplicate excess lots')

			# Exclude lots with 0 display qty — prevents split parent lots or empty records appearing
			before_zero_filter = len(master_data)
			master_data = [d for d in master_data if (d.get('display_qty') or 0) > 0]
			zero_removed = before_zero_filter - len(master_data)
			if zero_removed:
				logging.info(f'[JIG PICK] Removed {zero_removed} lot(s) with display_qty=0')

			# ===== MARK LOTS WITH ACTIVE DRAFT STATUS =====
			# Supports per-model status for multi-model drafts:
			#   Primary model → "Draft", Secondary model(s) → "Partial Draft"
			# Excess lots always → "Yet to Start"
			try:
				all_lot_ids = [d.get('stock_lot_id', '') for d in master_data if d.get('stock_lot_id')]
				if all_lot_ids:
					# Non-multi-model drafts: get lot_ids directly via DB (no Python loop)
					draft_lot_ids = set(
						JigCompleted.objects.filter(
							draft_status__in=['draft', 'active'], is_multi_model=False
						).values_list('lot_id', flat=True)
					)
					partial_draft_lot_ids = set()
					for rec in JigCompleted.objects.filter(
						draft_status__in=['draft', 'active'], is_multi_model=True,
						multi_model_allocation__isnull=False
					).only('multi_model_allocation')[:100]:
						if rec.multi_model_allocation:
							for m in rec.multi_model_allocation:
								mlot = m.get('lot_id', '') if isinstance(m, dict) else ''
								mstatus = m.get('status', '') if isinstance(m, dict) else ''
								if mlot:
									if mstatus == 'partial_draft':
										partial_draft_lot_ids.add(mlot)
									else:
										draft_lot_ids.add(mlot)

					for d in master_data:
						# Excess lots: check if they have an active draft
						if d.get('is_excess_lot'):
							excess_lot_id_val = d.get('stock_lot_id', '')
							if excess_lot_id_val in draft_lot_ids:
								d['lot_status'] = 'Draft'
								d['lot_status_class'] = 'lot-status-draft'
							elif excess_lot_id_val in partial_draft_lot_ids:
								d['lot_status'] = 'Partial Draft'
								d['lot_status_class'] = 'lot-status-partial-draft'
							else:
								# FIXED: Excess lots should be "Yet to Start" when no draft exists
								d['lot_status'] = 'Yet to Start'
								d['lot_status_class'] = 'lot-status-yet'
						elif d.get('stock_lot_id') in draft_lot_ids:
							d['lot_status'] = 'Draft'
							d['lot_status_class'] = 'lot-status-draft'
						elif d.get('stock_lot_id') in partial_draft_lot_ids:
							d['lot_status'] = 'Partial Draft'
							d['lot_status_class'] = 'lot-status-partial-draft'
						else:
							d.setdefault('lot_status', 'Yet to Start')
							d.setdefault('lot_status_class', 'lot-status-yet')
			except Exception:
				logging.exception('[JIG PICK] Failed to compute lot draft statuses')

			# Add Model is a selection screen, so active draft allocations must not
			# appear there regardless of whether they came from TotalStockModel or
			# the appended excess/half-filled flow. The main pick table still keeps
			# draft rows visible so operators can resume them.
			master_data = exclude_draft_rows_from_add_model_filter(master_data, is_add_model_popup)

			_t6 = _time.time()
			print(f"[JIG PERF] draft status: {_t6 - _t5:.3f}s")

			# ===== PAGINATE — MUST be LAST, after excess lots + status marking =====
			from django.core.paginator import Paginator
			page_number = self.request.GET.get('page', 1)
			paginator = Paginator(master_data, 10)  # 10 records per page
			page_obj = paginator.get_page(page_number)

			# ===== IP INFO REMARK: shared per plating_stk_no (PSN), not per lot =====
			# Resolved only for the current page (not all of master_data) to avoid a
			# large IN() query when Add Model mode returns an unsliced result set.
			try:
				from .selectors import get_ip_info_remarks_by_psn
				page_psns = {d.get('plating_stk_no', '') for d in page_obj.object_list if d.get('plating_stk_no')}
				ip_info_remark_map = get_ip_info_remarks_by_psn(page_psns)
				for d in page_obj.object_list:
					d['inprocess_remarks'] = ip_info_remark_map.get(d.get('plating_stk_no', ''), '')
			except Exception:
				logging.exception('[JIG PICK] Failed to resolve IP Info remarks by PSN')

			context['master_data'] = page_obj
			context['page_obj'] = page_obj
			# Pass Add Model filter context to template (for empty-state warning)
			context['is_add_model_popup'] = is_add_model_popup
			context['add_model_primary_lot'] = primary_lot or ''
			context['add_model_primary_batch'] = self.request.GET.get('primary_batch_id', '')
			context['add_model_no_results'] = is_add_model_popup and len(master_data) == 0

			print(f"[JIG PERF] TOTAL JigView: {_time.time() - _t0:.3f}s ({len(master_data)} records)")

		except Exception:
			logging.exception('Failed to populate master_data for Jig pick')
		return context


class TrayInfoView(APIView):
	"""Return tray records for a given lot (used by InitJigLoad).
	Simple, read-only view that returns a JSON structure with `trays`.
	"""
	permission_classes = [IsAuthenticated]

	def get(self, request, *args, **kwargs):
		lot_id = request.GET.get('lot_id')
		if not lot_id:
			return Response({'trays': []})

		try:
			trays = fetch_trays_for_lot(lot_id)
			return Response({'trays': trays})
		except Exception:
			logging.exception('TrayInfoView failed')
			return Response({'trays': []}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class InitJigLoad(APIView):
	"""Initialize or return an active draft from JigCompleted for the user and lot.

	Returns lot qty, jig_capacity, current draft state and tray list (from TrayInfoView).
	"""
	permission_classes = [IsAuthenticated]

	def get(self, request, *args, **kwargs):
		lot_id = request.GET.get('lot_id')
		batch_id = request.GET.get('batch_id')
		jig_capacity = request.GET.get('jig_capacity')

		if not lot_id or not batch_id:
			raise exceptions.ParseError(detail='lot_id and batch_id are required')

		# determine lot qty similar to JigView
		lot_qty = 0
		try:
			stock = TotalStockModel.objects.filter(lot_id=lot_id).first()
			if stock:
				lot_qty = getattr(stock, 'brass_audit_accepted_qty', None) or getattr(stock, 'brass_audit_physical_qty', None) or getattr(stock, 'total_stock', 0)
		except Exception:
			logging.exception('Failed to fetch lot qty for InitJigLoad')

		try:
			if jig_capacity:
				jig_capacity = int(jig_capacity)
			else:
				# Resolve jig capacity through the shared exact-plating-first resolver.
				try:
					batch = ModelMasterCreation.objects.filter(batch_id=batch_id).first()
					resolved_cap = resolve_jig_capacity_for_batch(batch)
					jig_capacity = int(resolved_cap) if resolved_cap else int(lot_qty or 0)
				except Exception:
					jig_capacity = int(lot_qty or 0)
		except Exception:
			jig_capacity = int(lot_qty or 0)

		# NOTE: Do NOT create or modify a persistent draft here. Per UI flow,
		# the draft must only be saved when the user clicks the Draft button.
		# Try to fetch an existing draft if present, but do not create one.
		draft = JigCompleted.objects.filter(batch_id=batch_id, lot_id=lot_id, user=request.user, draft_status__in=['draft', 'active']).first()

		# Fetch trays directly from DB — unified resolver (JigLoadTrayId → BrassAuditTrayId → BrassTrayId)
		try:
			trays = fetch_trays_for_lot(lot_id)
		except Exception:
			trays = []

		# Detect PERFECT_FIT scenario: lot == jig_capacity and no broken hooks
		is_perfect_fit = False
		try:
			is_perfect_fit = (int(lot_qty or 0) == int(jig_capacity or 0)) and (int(getattr(draft, 'broken_hooks', 0) or 0) == 0)
		except Exception:
			is_perfect_fit = False

		# ===== Stable delink calculation (do not depend on tray records for initial screen) =====
		try:
			# determine tray capacity from batch/model if available, else default to 12
			tray_capacity = None
			try:
				batch_obj = ModelMasterCreation.objects.filter(batch_id=batch_id).first()
				if batch_obj:
					tray_capacity = getattr(batch_obj, 'tray_capacity', None)
					model_obj = getattr(batch_obj, 'model_stock_no', None)
					if not tray_capacity and model_obj:
						tray_capacity = getattr(model_obj, 'tray_capacity', None)
			except Exception:
				tray_capacity = None

			# fallback to any ad-hoc data from brass audit (if available in this scope)
			adata = None
			try:
				if 'adata' in locals() and adata:
					tray_capacity = tray_capacity or int(adata.get('tray_capacity', 0) or 0)
			except Exception:
				pass

			tray_capacity = int(tray_capacity or 12)

			# STRICT DELINK: delink is only the jig fill (cases that will go onto the jig)
			lot_qty_int = int(lot_qty or 0)
			jig_capacity_int = int(jig_capacity or 0)

			# ==========================================================
			# BROKEN HOOKS 
			# ==========================================================

			# Prefer an explicit broken hooks value passed from the frontend (query param)
			# so users can live-preview splits without saving a draft.
			try:
				bh_param = request.GET.get('broken_hooks') or request.GET.get('broken_buildup_hooks')
				if bh_param is not None:
					broken_hooks = int(bh_param or 0)
				else:
					broken_hooks = int(getattr(draft, 'broken_hooks', 0) or 0)
			except Exception:
				broken_hooks = int(getattr(draft, 'broken_hooks', 0) or 0)

			effective_jig_capacity = max(0, jig_capacity_int - broken_hooks)

			delink_qty = min(lot_qty_int, effective_jig_capacity)
			excess_qty = max(0, lot_qty_int - delink_qty)

			logging.info(f"[BH] jig={jig_capacity_int}, broken={broken_hooks}, effective={effective_jig_capacity}")
			logging.info(f"[BH_SPLIT] delink={delink_qty}, excess={excess_qty}")

			# ==========================================================
			# 🔥 PARTIAL TRAY SPLIT (BROKEN HOOKS SAFE LOGIC)
			# ==========================================================


			# Allocate delink trays using LAST-TRAY deduction logic:
			# Only reduce the current overflowing tray instead of recomputing
			# cumulative remaining. This ensures e.g. 12 -> 11 when capacity
			# is exceeded by 1.
			delink_tray_info = []
			excess_tray_info = []
			total = 0
			last_delink_index = -1

			for idx, tray in enumerate(trays):
				tray_id = tray.get('tray_id')
				tray_qty = int(tray.get('qty', 0) or 0)

				# Full tray fits within effective capacity
				if total + tray_qty <= effective_jig_capacity:
					delink_tray_info.append({
						"tray_id": tray_id,
						"qty": tray_qty,
						"top_tray": False,
						"is_partial": False
					})
					total += tray_qty
					last_delink_index = idx
					continue

				# Overflow: only reduce the current tray by the excess amount
				excess = (total + tray_qty) - effective_jig_capacity
				adjusted_qty = tray_qty - excess

				if adjusted_qty > 0:
					delink_tray_info.append({
						"tray_id": tray_id,
						"qty": adjusted_qty,
						"top_tray": True,
						"is_partial": True
					})
					last_delink_index = idx

				# Allocation complete — remaining trays (if any) are excess
				break
			# Log final distribution info for debugging
			try:
				logging.info(f"[DELINK_SPLIT] total allocated: {total}, delink trays: {len(delink_tray_info)}, excess trays: {len(excess_tray_info)}")
			except Exception:
				pass

			# ==========================================================
			# 🔥 EXCESS RENDER - Half filled tray scan
			# ==========================================================
			try:
				# Use tray_capacity determined above (fallbacks already applied)
				_excess_trays = []
				if 'excess_qty' in locals() and excess_qty > 0 and tray_capacity > 0:
					full_trays = excess_qty // tray_capacity
					remainder = excess_qty % tray_capacity

					logging.info(f"[EXCESS_CALC] full={full_trays}, remainder={remainder}")

					tray_counter = 1
					for _ in range(full_trays):
						_excess_trays.append({
							"tray_id": f"JB-A{str(tray_counter).zfill(5)}",
							"qty": tray_capacity,
							"top_tray": False
						})
						tray_counter += 1

					if remainder > 0:
						# top tray displayed first in UI
						_excess_trays.insert(0, {
							"tray_id": f"JB-A{str(tray_counter).zfill(5)}",
							"qty": remainder,
							"top_tray": True
						})

				# attach to local response structure
				response_excess = {
					"excess_qty": int(excess_qty or 0),
					"excess_tray_count": len(_excess_trays),
					"excess_trays": _excess_trays
				}
			except Exception:
				logging.exception('[ERROR] Excess calculation failed')

		except Exception:
			logging.exception('Stable delink calculation failed')

		# Populate model metadata for frontend (best-effort)
		model_image_url = '/static/assets/images/imagePlaceholder.jpg'
		model_image_label = ''
		nickel_bath_type = ''
		tray_type_name = ''
		try:
			mm = None
			batch_obj = ModelMasterCreation.objects.filter(batch_id=batch_id).first()
			if batch_obj:
				mm = getattr(batch_obj, 'model_stock_no', None) or batch_obj
			# fallback to stock relation
			if not mm and 'stock' in locals() and stock:
				mm = getattr(stock, 'model_master', None) or getattr(stock, 'model', None)
			if mm:
				try:
					if hasattr(mm, 'images'):
						from modelmasterapp.image_utils import sort_images_front_first
						imgs = mm.images.all()
						if imgs and imgs.exists():
							first_img = sort_images_front_first(imgs)[0]
							if getattr(first_img, 'master_image', None):
								model_image_url = first_img.master_image.url
				except Exception:
					pass
				model_image_label = getattr(mm, 'plating_stk_no', '') or getattr(mm, 'model_no', '') or ''
				nickel_bath_type = getattr(mm, 'ep_bath_type', '') or getattr(mm, 'nickle_bath_type', '') or ''
				try:
					tt = getattr(mm, 'tray_type', None)
					if tt:
						tray_type_name = getattr(tt, 'tray_type', '') if not isinstance(tt, str) else tt
				except Exception:
					pass
				# Resolve abbreviation to full parent name (e.g. JB→Jumbo, ND→Normal)
				if tray_type_name:
					try:
						from modelmasterapp.models import TrayType as _TrayType
						_tt_obj = _TrayType.objects.filter(tray_type=tray_type_name).first()
						if _tt_obj and _tt_obj.tray_color:
							_parent = _TrayType.objects.filter(
								tray_capacity=_tt_obj.tray_capacity, tray_color__isnull=True
							).first()
							if _parent:
								tray_type_name = _parent.tray_type
					except Exception:
						pass
		except Exception:
			pass

		# ===== MULTI MODEL SUPPORT (NEW - NON BREAKING) =====
		multi_model_flag = request.GET.get('multi_model')
		secondary_lots_raw = request.GET.get('secondary_lots')
		secondary_lots = []
		multi_model_allocation = []

		# Step 1: Parse secondary_lots only when both params are present
		if multi_model_flag and secondary_lots_raw:
			try:
				secondary_lots = json.loads(secondary_lots_raw)
			except Exception:
				logging.warning("[MULTI_MODEL] Invalid secondary_lots JSON — skipping multi-model flow")
				secondary_lots = []

		logging.info(f"[MULTI_MODEL] Flag={multi_model_flag}, Secondary lots count={len(secondary_lots)}")

		# Step 2: Run allocation only when flag is set AND secondary_lots parsed correctly
		if multi_model_flag and secondary_lots:
			# Safe fallbacks: these variables come from the delink try-block above;
			# guard against NameError if that block threw before defining them.
			_mm_eff_cap = locals()['effective_jig_capacity'] if 'effective_jig_capacity' in locals() else max(0, int(jig_capacity or 0))
			_mm_lot_qty = locals()['lot_qty_int'] if 'lot_qty_int' in locals() else int(lot_qty or 0)

			used_tray_ids = set()

			# STEP 1: PRIMARY MODEL ALLOCATION
			try:
				primary_result = allocate_trays_for_model(
					lot_id=lot_id,
					model_lot_qty=_mm_lot_qty,
					effective_capacity_remaining=_mm_eff_cap,
					used_tray_ids=used_tray_ids
				)
				used_tray_ids.update(primary_result['allocated_tray_ids'])
				primary_img = fetch_model_image_metadata(lot_id, batch_id)
				multi_model_allocation.append({
					'model': fetch_model_metadata(lot_id, batch_id),
					'model_name': fetch_model_metadata(lot_id, batch_id),
					'model_role': 'primary',
					'lot_id': lot_id,
					'batch_id': batch_id,
					'sequence': 0,
					'model_index': 1,
					'color_class': 'model-bg-1',
					'allocated_qty': primary_result['allocated_qty'],
					'tray_info': primary_result['tray_info'],
					'model_image_url': primary_img['model_image_url'],
					'model_image_label': primary_img['model_image_label'],
				})
				logging.info(f"[MULTI_MODEL] Primary {lot_id}: {primary_result['allocated_qty']} qty")
			except Exception as e:
				logging.exception(f"[MULTI_MODEL] Primary allocation failed: {e}")

			# STEP 2: SECONDARY MODEL ALLOCATIONS (with capacity enforcement + excess → half-filled)
			mm_half_filled_tray_info = []
			mm_half_filled_tray_qty = 0

			for seq, sec in enumerate(secondary_lots, start=1):
				try:
					sec_lot_id = sec.get('lot_id')
					sec_batch_id = sec.get('batch_id')
					sec_lot_qty = int(sec.get('qty', 0) or 0)

					if not sec_lot_id:
						continue

					# Remaining capacity = effective - already allocated
					capacity_used = sum(m['allocated_qty'] for m in multi_model_allocation)
					capacity_remaining = max(0, _mm_eff_cap - capacity_used)

					# CAPACITY CONTROL: cap allocation to remaining jig capacity
					allowed_qty = min(sec_lot_qty, capacity_remaining)
					excess_for_model = max(0, sec_lot_qty - allowed_qty)
					logging.info(f"[MULTI_MODEL] Secondary {sec_lot_id}: remaining_capacity={capacity_remaining}, sec_lot_qty={sec_lot_qty}, allowed_qty={allowed_qty}, excess_qty={excess_for_model}")

					secondary_result = allocate_trays_for_model(
						lot_id=sec_lot_id,
						model_lot_qty=allowed_qty,
						effective_capacity_remaining=capacity_remaining,
						used_tray_ids=used_tray_ids
					)
					used_tray_ids.update(secondary_result['allocated_tray_ids'])
					sec_img = fetch_model_image_metadata(sec_lot_id, sec_batch_id)
					sec_model_name = fetch_model_metadata(sec_lot_id, sec_batch_id)

					# Check for partial tray: last allocated tray may be partially used
					partial_remainder = 0
					partial_tray_id = None
					if secondary_result['tray_info']:
						last_alloc = secondary_result['tray_info'][-1]
						# Find original tray qty to detect partial usage — unified resolver
						try:
							_all_sec_trays = fetch_trays_for_lot(sec_lot_id)
							_orig = next((t for t in _all_sec_trays if t.get('tray_id') == last_alloc['tray_id']), None)
							if _orig:
								orig_qty = int(_orig.get('qty', 0) or 0)
								if last_alloc['qty'] < orig_qty:
									partial_remainder = orig_qty - last_alloc['qty']
									partial_tray_id = last_alloc['tray_id']
									logging.info(f"[MULTI_MODEL] Partial tray detected: {partial_tray_id} used={last_alloc['qty']}, remainder={partial_remainder}")
						except Exception:
							pass

					sec_model_idx = seq + 1
					sec_color_idx = ((sec_model_idx - 1) % 5) + 1
					multi_model_allocation.append({
						'model': sec_model_name,
						'model_name': sec_model_name,
						'model_role': 'secondary',
						'lot_id': sec_lot_id,
						'batch_id': sec_batch_id,
						'sequence': seq,
						'model_index': sec_model_idx,
						'color_class': f'model-bg-{sec_color_idx}',
						'allocated_qty': secondary_result['allocated_qty'],
						'tray_info': secondary_result['tray_info'],
						'model_image_url': sec_img['model_image_url'],
						'model_image_label': sec_img['model_image_label'],
					})
					logging.info(f"[MULTI_MODEL] Secondary {sec_lot_id}: allocated {secondary_result['allocated_qty']} qty")

					# EXCESS HANDLING: collect excess trays into half_filled_tray_info
					if excess_for_model > 0:
						excess_remaining = excess_for_model

						# 1) If last allocated tray was partial, its remainder goes to half-filled
						if partial_remainder > 0 and partial_tray_id:
							hf_qty = min(partial_remainder, excess_remaining)
							mm_half_filled_tray_info.append({
								'tray_id': partial_tray_id,
								'qty': hf_qty,
								'model': sec_model_name,
							})
							excess_remaining -= hf_qty
							mm_half_filled_tray_qty += hf_qty
							logging.info(f"[MULTI_MODEL] Half-filled partial tray: {partial_tray_id} qty={hf_qty}")

						# 2) Continue with unallocated trays from same lot for remaining excess
						if excess_remaining > 0:
							try:
								excess_tray_list = fetch_trays_for_lot(sec_lot_id)
								for tray_item in excess_tray_list:
									if excess_remaining <= 0:
										break
									tid = tray_item.get('tray_id', '')
									# Skip already allocated trays
									if tid in used_tray_ids:
										continue
									tq = int(tray_item.get('qty', 0) or 0)
									hf_qty = min(tq, excess_remaining)
									mm_half_filled_tray_info.append({
										'tray_id': tid,
										'qty': hf_qty,
										'model': sec_model_name,
									})
									excess_remaining -= hf_qty
									mm_half_filled_tray_qty += hf_qty
									used_tray_ids.add(tid)
									logging.info(f"[MULTI_MODEL] Half-filled excess tray: {tid} qty={hf_qty}")
							except Exception as ex:
								logging.exception(f"[MULTI_MODEL] Excess tray fetch failed for {sec_lot_id}: {ex}")

						logging.info(f"[MULTI_MODEL] Excess for {sec_lot_id}: total half_filled_qty={mm_half_filled_tray_qty}, trays={len(mm_half_filled_tray_info)}")

				except Exception as e:
					logging.exception(f"[MULTI_MODEL] Secondary allocation failed for {sec.get('lot_id')}: {e}")
					continue

			# Validation: no duplicate tray IDs across models
			all_tray_ids = [t['tray_id'] for m in multi_model_allocation for t in m['tray_info']]
			if len(all_tray_ids) != len(set(all_tray_ids)):
				logging.error("[MULTI_MODEL] VALIDATION FAILED: Duplicate tray IDs detected!")
			logging.info(f"[MULTI_MODEL] Final: {len(multi_model_allocation)} models, {len(all_tray_ids)} total trays, half_filled_qty={mm_half_filled_tray_qty}")

		# Build ui_delink_tray_info: flattened tray list from multi_model_allocation for FE binding
		ui_delink_tray_info = []
		if multi_model_flag and multi_model_allocation:
			for m_alloc in multi_model_allocation:
				for t in m_alloc.get('tray_info', []):
					ui_delink_tray_info.append({
						'tray_id': t.get('tray_id', ''),
						'qty': t.get('qty', 0),
						'top_tray': False,
						'is_partial': False,
						'model': m_alloc.get('model', ''),
						'model_role': m_alloc.get('model_role', ''),
						'lot_id': m_alloc.get('lot_id', ''),
						'batch_id': m_alloc.get('batch_id', ''),
					})
			logging.info(f"[MULTI_MODEL] ui_delink_tray_info: {len(ui_delink_tray_info)} trays flattened")

		# ===== UNIFIED HALF-FILLED FIX (SINGLE + MULTI + BH SAFE) =====
		try:
			# Ensure vars are always defined before any check
			if 'mm_half_filled_tray_info' not in locals():
				mm_half_filled_tray_info = []
			if 'mm_half_filled_tray_qty' not in locals():
				mm_half_filled_tray_qty = 0

			# Step 1: TOTAL REQUESTED quantity — single source of truth
			# Use REQUESTED qty (not allocated) so BH-reduced capacity triggers overflow correctly
			if multi_model_flag and secondary_lots:
				_hf_total_qty = int(lot_qty or 0) + sum(int(s.get('qty', 0) or 0) for s in secondary_lots)
				logging.info(f"[HALF FIX] MULTI total_qty (requested): {_hf_total_qty}")
			else:
				_hf_total_qty = int(lot_qty or 0)
				logging.info(f"[HALF FIX] SINGLE total_qty: {_hf_total_qty}")

			# Step 2: Effective capacity — BH-aware (single source of truth)
			_hf_cap = effective_jig_capacity if 'effective_jig_capacity' in locals() else int(jig_capacity or 0)

			# Step 3: Only initialise if overflow AND secondary loop did not already populate
			if _hf_total_qty > _hf_cap and not mm_half_filled_tray_info:
				_hf_overflow = _hf_total_qty - _hf_cap
				logging.info(f"[HALF FIX] Overflow={_hf_overflow}, creating half-filled trays")
				_hf_tc = int(tray_capacity if tray_capacity else 12)
				mm_half_filled_tray_info = []
				_hf_rem = _hf_overflow
				while _hf_rem > 0:
					_hf_fill = min(_hf_tc, _hf_rem)
					mm_half_filled_tray_info.append({"tray_id": None, "qty": _hf_fill})
					_hf_rem -= _hf_fill
				mm_half_filled_tray_qty = sum(t['qty'] for t in mm_half_filled_tray_info)
				logging.info(f"[HALF FIX] CREATED: {mm_half_filled_tray_info}")
			else:
				logging.info(f"[HALF FIX] No overflow or already populated — skipping (total={_hf_total_qty}, cap={_hf_cap}, existing={len(mm_half_filled_tray_info)})")

		except Exception as _hf_err:
			logging.exception(f"[HALF FIX ERROR]: {_hf_err}")
			if 'mm_half_filled_tray_info' not in locals():
				mm_half_filled_tray_info = []
			if 'mm_half_filled_tray_qty' not in locals():
				mm_half_filled_tray_qty = 0

		# ===== CALCULATE SERVER-AUTHORITATIVE LOADED_CASES_QTY AND EMPTY_HOOKS ==
		loaded_cases_qty = 0
		# 🔥 FIX: Use the broken_hooks value calculated earlier (from GET param OR draft)
		broken_hooks_int = int(broken_hooks or 0)  # This already includes GET param logic
		jig_capacity_int = int(jig_capacity or 0)
		lot_qty_int = int(lot_qty or 0)

		# 🔥 MULTI-MODEL CUMULATIVE: aggregate total qty from all models
		if multi_model_flag and multi_model_allocation:
			total_multi_model_qty = sum(m.get('allocated_qty', 0) for m in multi_model_allocation)
			logging.info(f"[MULTI_MODEL] Incoming Models: {len(multi_model_allocation)}")
			logging.info(f"[MULTI_MODEL] Computed Total: {total_multi_model_qty}")
		else:
			total_multi_model_qty = lot_qty_int

		# 🔥 FIX: NO auto-loading on initial load. Only use persisted draft value if exists.
		# All initial states (including perfect fit 144/144) start with loaded_cases_qty = 0
		if draft and getattr(draft, 'loaded_cases_qty', None):
			# Use persisted draft value (user already scanned)
			loaded_cases_qty = int(draft.loaded_cases_qty)
		else:
			# Initial state: no auto-loading (user hasn't scanned yet)
			loaded_cases_qty = 0

		# empty_hooks calculation
		effective_capacity = max(0, jig_capacity_int - broken_hooks_int)

		if loaded_cases_qty > 0:
			# AFTER SCAN: use scan-based calculation
			empty_hooks = max(0, effective_capacity - loaded_cases_qty)
		else:
			# BEFORE SCAN: use cumulative lot-based calculation (multi-model aware)
			if total_multi_model_qty < effective_capacity:
				empty_hooks = effective_capacity - total_multi_model_qty
			else:
				empty_hooks = 0

		logging.info(f"[BACKEND_STATE] lot={lot_qty_int}, cap={jig_capacity_int}, broken={broken_hooks_int}, loaded={loaded_cases_qty}, empty={empty_hooks}, total_multi_model_qty={total_multi_model_qty}")
		if multi_model_flag and multi_model_allocation:
			logging.info(f"[MULTI_MODEL] Empty Hooks: {empty_hooks}")

		# Detect BH preview mode: when broken_hooks is explicitly passed via query param,
		# ALWAYS use freshly computed delink_tray_info (not stale draft data)
		bh_preview = request.GET.get('broken_hooks') is not None or request.GET.get('broken_buildup_hooks') is not None

		# Build a non-persistent draft dict to return to the frontend
		resp_draft = {
			'batch_id': batch_id,
			'lot_id': lot_id,
			'original_lot_qty': int(lot_qty or 0),
			'jig_capacity': jig_capacity,
			'effective_capacity': int(max(0, jig_capacity_int - broken_hooks_int) or 0),
			'loaded_cases_qty': int(draft.loaded_cases_qty) if draft else 0,
			'delink_tray_info': delink_tray_info if bh_preview else (draft.delink_tray_info if draft and draft.delink_tray_info else delink_tray_info),
			'delink_tray_qty': int(delink_qty or 0) if bh_preview else (int(draft.delink_tray_qty) if draft else int(delink_qty or 0)),
			'excess_qty': int(excess_qty or 0) if 'excess_qty' in locals() else 0,
			# model metadata
			'model_image_url': model_image_url,
			'model_image_label': model_image_label,
			'plating_stock_num': model_image_label,
			'nickel_bath_type': nickel_bath_type,
			'tray_type': tray_type_name,
			'is_multi_model': True if multi_model_flag else False,
			'total_multi_model_qty': int(total_multi_model_qty or 0),
			'draft_data': {
				'primary_lot': lot_id,
				'secondary_lots': secondary_lots
			},
			'secondary_lots': secondary_lots,
		}

		return Response({
			'draft': resp_draft,
			'trays': trays,
			'lot_qty': int(lot_qty or 0),
			'original_capacity': int(jig_capacity_int or 0),
			'effective_capacity': int(max(0, jig_capacity_int - broken_hooks_int) or 0),
			'loaded_cases_qty': int(loaded_cases_qty or 0),
			'broken_hooks': int(broken_hooks_int or 0),
			'empty_hooks': int(empty_hooks or 0),
			'excess_qty': int(excess_qty or 0) if 'excess_qty' in locals() else 0,
			'excess_info': response_excess if 'response_excess' in locals() else {"excess_qty": 0, "excess_tray_count": 0, "excess_trays": []},
			# Top-level delink_tray_info for refreshTrayCalculation (frontend)
			'delink_tray_info': delink_tray_info if bh_preview else (draft.delink_tray_info if draft and draft.delink_tray_info else delink_tray_info),
			# duplicate top-level metadata for compatibility
			'model_image_url': model_image_url,
			'model_image_label': model_image_label,
			'nickel_bath_type': nickel_bath_type,
			'tray_type': tray_type_name,
			'secondary_lots': secondary_lots,
			'scenario': 'PERFECT_FIT' if is_perfect_fit else '',
			'is_multi_model': True if multi_model_flag else False,
			'total_multi_model_qty': int(total_multi_model_qty or 0),
			# ===== NEW: MULTI-MODEL ALLOCATION (when multi_model flag is set) =====
			'multi_model_allocation': multi_model_allocation if multi_model_flag else [],
			# Flattened tray list from all models for FE delink binding in multi-model mode
			'ui_delink_tray_info': ui_delink_tray_info if ui_delink_tray_info else [],
			'half_filled_tray_info': mm_half_filled_tray_info if 'mm_half_filled_tray_info' in locals() else [],
			'half_filled_tray_qty': mm_half_filled_tray_qty if 'mm_half_filled_tray_qty' in locals() else 0,
		})


class ScanTray(APIView):
	"""Handle scanning (delinking) a tray for Jig Loading.

	Expected POST JSON: { lot_id, batch_id, tray_id }
	"""
	permission_classes = [IsAuthenticated]

	def post(self, request, *args, **kwargs):
		payload = request.data
		lot_id = payload.get('lot_id')
		batch_id = payload.get('batch_id')
		tray_id = payload.get('tray_id')

		if not lot_id or not batch_id or not tray_id:
			raise exceptions.ParseError(detail='lot_id, batch_id and tray_id are required')

		# Validation-only scan: do not create or modify drafts here. Return tray qty.
		try:
			tray = JigLoadTrayId.objects.filter(tray_id=tray_id, lot_id=lot_id).first()
			if not tray:
				return Response({'status': 'error', 'message': 'Invalid tray or wrong lot'}, status=status.HTTP_400_BAD_REQUEST)
		except Exception:
			logging.exception('Error fetching tray')
			return Response({'status': 'error', 'message': 'Tray fetch error'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

		tray_qty = int(tray.tray_quantity or 0)

		print(f"Tray validated: {tray_id} (lot: {lot_id}) qty:{tray_qty}")

		return Response({'status': 'success', 'tray_id': tray_id, 'tray_qty': tray_qty})


# =============================================================================
# CORE COMPUTATION ENGINE (SINGLE SOURCE OF TRUTH)
# =============================================================================
def compute_jig_loading(trays, jig_capacity, broken_hooks, tray_capacity=12):
	"""
	Core computation engine for Jig Loading. Single source of truth.
	Called by: JigLoadInitAPI, JigLoadUpdateAPI, JigSaveAPI.

	BH Logic: Apply broken hooks from LAST tray → FIRST.
	- If tray qty becomes 0 → REMOVE tray from output
	- If partial → reduce qty
	- Mandatory validation: total_before - total_after == broken_hooks

	Args:
		trays: list of dicts [{'tray_id': str, 'qty': int}, ...]
		jig_capacity: total jig capacity (int)
		broken_hooks: number of broken/buildup hooks (int)
		tray_capacity: default tray capacity for excess allocation (int)

	Returns:
		dict with effective_capacity, loaded_cases_qty, empty_hooks,
		delink_tray_info, excess_info, validation, etc.
	"""
	jig_capacity = int(jig_capacity or 0)
	broken_hooks = max(0, int(broken_hooks or 0))
	tray_capacity = int(tray_capacity or 12)
	effective_capacity = max(0, jig_capacity - broken_hooks)

	total_lot_qty = sum(int(t.get('qty', 0) or 0) for t in trays)

	validation_errors = []
	if broken_hooks < 0:
		validation_errors.append('Broken hooks cannot be negative')
	if broken_hooks > jig_capacity:
		validation_errors.append('Broken hooks exceeds jig capacity')

	# Step 1: Build working list of trays (allocate up to jig_capacity, first→last)
	# Use jig_capacity (NOT effective_capacity) because BH is applied in Step 2
	working_trays = []
	total_allocated = 0
	for tray in trays:
		tray_id = tray.get('tray_id', '')
		tray_qty = int(tray.get('qty', 0) or 0)
		if tray_qty <= 0:
			continue  # Skip zero-qty trays from source
		if total_allocated >= jig_capacity:
			break
		if total_allocated + tray_qty <= jig_capacity:
			working_trays.append({
				'tray_id': tray_id,
				'qty': tray_qty,
				'original_qty': tray_qty,
				'allocated_qty_before_bh': tray_qty,
				'capacity_split_excess_qty': 0,
				'is_capacity_split': False,
				'top_tray': bool(tray.get('top_tray', False)),
			})
			total_allocated += tray_qty
		else:
			remaining = jig_capacity - total_allocated
			working_trays.append({
				'tray_id': tray_id,
				'qty': remaining,
				'original_qty': tray_qty,
				'allocated_qty_before_bh': remaining,
				'capacity_split_excess_qty': max(0, tray_qty - remaining),
				'is_capacity_split': remaining < tray_qty,
				'top_tray': bool(tray.get('top_tray', False)),
			})
			total_allocated += remaining

	# Step 2: Apply BH deduction from LAST tray → FIRST (strict tray removal)
	total_before_bh = sum(t['qty'] for t in working_trays)
	bh_remaining = broken_hooks
	if bh_remaining > 0 and working_trays:
		# Walk backwards through trays
		i = len(working_trays) - 1
		while i >= 0 and bh_remaining > 0:
			tray = working_trays[i]
			if tray['qty'] <= bh_remaining:
				# Full tray removed
				bh_remaining -= tray['qty']
				tray['qty'] = 0
			else:
				# Partial reduction
				tray['qty'] -= bh_remaining
				bh_remaining = 0
			i -= 1

	# Step 3: Remove zero-qty trays (CRITICAL — no empty trays in output)
	delink_tray_info = []
	for t in working_trays:
		if t['qty'] > 0:
			is_partial = t['qty'] < t['original_qty']
			is_bh_partial = t['qty'] < t.get('allocated_qty_before_bh', t['qty'])
			delink_tray_info.append({
				'tray_id': t['tray_id'],
				'original_qty': t['original_qty'],
				'excluded_qty': t['original_qty'] - t['qty'],
				'effective_qty': t['qty'],
				'qty': t['qty'],
				'status': 'partial' if is_partial else 'loaded',
				'top_tray': bool(t.get('top_tray', False)),  # Preserve DB flag (actual top tray)
				'is_partial': is_partial,  # Qty differs from original (due to capacity/BH)
				'is_capacity_split': bool(t.get('is_capacity_split', False)),
				'is_broken_hooks_partial': is_bh_partial,
			})

	# Step 4: Integrity validation
	total_after_bh = sum(t['qty'] for t in delink_tray_info)
	loaded_cases_qty = total_after_bh
	if broken_hooks > 0:
		expected_diff = min(broken_hooks, total_before_bh)
		actual_diff = total_before_bh - total_after_bh
		if actual_diff != expected_diff:
			validation_errors.append(
				f'BH integrity check failed: expected removal={expected_diff}, actual={actual_diff}'
			)
			logging.error(f'[BH_INTEGRITY] MISMATCH: before={total_before_bh}, after={total_after_bh}, '
						  f'bh={broken_hooks}, expected_diff={expected_diff}, actual_diff={actual_diff}')

	empty_hooks = max(0, effective_capacity - loaded_cases_qty)
	excess_qty = max(0, total_lot_qty - effective_capacity)

	# ===== TRACK REAL EXCESS TRAYS (trays not allocated to delink) =====
	# These are the actual trays from the lot that overflow the jig capacity.
	# We track them with their real tray IDs for the half-filled section.
	allocated_tray_ids = set(t['tray_id'] for t in working_trays)
	real_excess_trays = []
	if excess_qty > 0:
		excess_remaining = excess_qty
		for tray in trays:
			if excess_remaining <= 0:
				break
			tray_id = tray.get('tray_id', '')
			tray_qty = int(tray.get('qty', 0) or 0)
			if tray_qty <= 0:
				continue
			if tray_id in allocated_tray_ids:
				# Check if this tray was partially allocated (split tray)
				allocated = next((t for t in working_trays if t['tray_id'] == tray_id), None)
				if allocated and allocated.get('capacity_split_excess_qty', 0) > 0:
					# Only the capacity-overflow remainder belongs in half-filled.
					# Broken-hooks reductions must not be treated as excess trays.
					split_excess = int(allocated.get('capacity_split_excess_qty', 0) or 0)
					fill = min(split_excess, excess_remaining)
					if fill > 0:
						real_excess_trays.append({'tray_id': tray_id, 'qty': fill, 'top_tray': bool(tray.get('top_tray', False)), 'source_lot_id': tray.get('source_lot_id', '')})
						excess_remaining -= fill
				continue
			# Tray not allocated at all → fully excess
			fill = min(tray_qty, excess_remaining)
			real_excess_trays.append({'tray_id': tray_id, 'qty': fill, 'top_tray': bool(tray.get('top_tray', False)), 'source_lot_id': tray.get('source_lot_id', '')})
			excess_remaining -= fill

	# Build excess tray info (uses real_excess_trays)
	excess_trays = list(real_excess_trays) if real_excess_trays else []

	# ===== ALL TRAYS: every input tray with delink/excess qty split =====
	# Algorithm: walk through ALL input trays, distribute up to effective_capacity as delink,
	# the rest as excess. sum(delink_qty) = effective_capacity (when lot > capacity).
	# sum(excess_qty) = total_lot_qty - effective_capacity. No tray is hidden.
	all_trays = []
	cap_remaining = effective_capacity
	for tray in trays:
		t_id = tray.get('tray_id', '')
		t_qty = int(tray.get('qty', 0) or 0)
		if t_qty <= 0:
			continue
		if cap_remaining <= 0:
			d_qty = 0
			e_qty = t_qty
		elif t_qty <= cap_remaining:
			d_qty = t_qty
			e_qty = 0
			cap_remaining -= t_qty
		else:
			d_qty = cap_remaining
			e_qty = t_qty - cap_remaining
			cap_remaining = 0
		all_trays.append({
			'tray_id': t_id,
			'original_qty': t_qty,
			'delink_qty': d_qty,
			'excess_qty': e_qty,
			'top_tray': bool(tray.get('top_tray', False)),
			'source_lot_id': tray.get('source_lot_id', ''),
		})

	# ===== HALF-FILLED: STRUCTURE ONLY — no tray IDs during scanning =====
	# STRICT RULE: tray_ids are NULL until delink scan is complete.
	# Only slots (qty, type, editable) are returned here.
	# Tray ID assignment happens in the API layer after delink completion.
	half_filled_tray_info = {
		'exists': False,
		'total_qty': 0,
		'tray_count': 0,
		'slots': [],
		'tray_ids': None,  # 🚫 MUST BE NULL until delink scan complete
	}
	half_filled_tray_qty = 0
	if excess_qty > 0 and delink_tray_info:
		# Find partial delink tray: capacity-split OR last tray with qty < tray_capacity
		partial_delink = None
		for dt in delink_tray_info:
			if dt.get('is_capacity_split', False):
				partial_delink = dt
				# Take the LAST partial one (closest to the capacity boundary)

		# Also check if last delink tray is naturally partial (qty < tray_capacity)
		if not partial_delink and delink_tray_info:
			last_delink = delink_tray_info[-1]
			if last_delink['qty'] < tray_capacity:
				partial_delink = last_delink

		slots = []
		slot_index = 1

		if partial_delink:
			# Top half-filled slot: partial (will be auto-linked AFTER delink scan complete)
			top_hf_qty = min(tray_capacity - partial_delink['qty'], excess_qty)
			if top_hf_qty > 0:
				slots.append({
					'index': slot_index,
					'qty': top_hf_qty,
					'type': 'partial',
					'editable': True,
				})
				slot_index += 1
				half_filled_tray_qty += top_hf_qty
				excess_qty_remaining = excess_qty - top_hf_qty
			else:
				excess_qty_remaining = excess_qty
		else:
			excess_qty_remaining = excess_qty

		# Remaining half-filled slots: distribute into tray_capacity-sized chunks
		if excess_qty_remaining > 0 and tray_capacity > 0:
			remaining = excess_qty_remaining
			while remaining > 0:
				fill = min(tray_capacity, remaining)
				slots.append({
					'index': slot_index,
					'qty': fill,
					'type': 'auto',
					'editable': False,
				})
				slot_index += 1
				remaining -= fill
				half_filled_tray_qty += fill

		half_filled_tray_info = {
			'exists': len(slots) > 0,
			'total_qty': half_filled_tray_qty,
			'tray_count': len(slots),
			'slots': slots,
			'tray_ids': None,  # Assigned by API when delink_completed
		}

	result = {
		'effective_capacity': effective_capacity,
		'loaded_cases_qty': loaded_cases_qty,
		'empty_hooks': empty_hooks,
		'excess_qty': max(0, total_lot_qty - effective_capacity),
		'total_qty': total_lot_qty,
		'tray_count': len(delink_tray_info),
		'delink_tray_info': delink_tray_info,
		'delink_tray_qty': loaded_cases_qty,
		'all_trays': all_trays,
		'excess_info': {'excess_qty': max(0, total_lot_qty - effective_capacity), 'excess_tray_count': len(excess_trays), 'excess_trays': excess_trays},
		'half_filled_tray_info': half_filled_tray_info,
		'half_filled_tray_qty': half_filled_tray_qty,
		'validation': {'is_overloaded': total_lot_qty > effective_capacity, 'errors': validation_errors}
	}

	return result


def assign_half_filled_tray_ids(half_filled, delink_tray_info, excess_trays, tray_capacity=12):
	"""Assign real tray IDs to half-filled slots. Called ONLY when delink scan is complete.

	Args:
		half_filled: dict with 'exists', 'slots', 'tray_ids' from compute_jig_loading
		delink_tray_info: list from compute_jig_loading
		excess_trays: list from compute_jig_loading excess_info.excess_trays
		tray_capacity: default tray capacity

	Returns:
		updated half_filled dict with tray_ids populated
	"""
	if not half_filled or not half_filled.get('exists'):
		return half_filled

	# Find partial delink tray: capacity-split OR last tray with qty < tray_capacity
	partial_delink = None
	for dt in delink_tray_info:
		if dt.get('is_capacity_split', False):
			partial_delink = dt

	# Also check if last delink tray is naturally partial (qty < tray_capacity)
	if not partial_delink and delink_tray_info:
		last_delink = delink_tray_info[-1]
		if last_delink['qty'] < tray_capacity:
			partial_delink = last_delink

	tray_ids = []
	linked_tray_id = partial_delink['tray_id'] if partial_delink else None
	excess_idx = 0

	for slot in half_filled.get('slots', []):
		if slot.get('type') == 'partial' and partial_delink:
			# Auto-link: use the partial delink tray's ID
			tray_ids.append({
				'tray_id': partial_delink['tray_id'],
				'qty': slot['qty'],
				'auto_linked': True,
				'linked_to': partial_delink['tray_id'],
				'is_top_half_filled': True,
				'editable': True,
			})
		else:
			# Skip excess tray that matches the auto-linked partial tray (avoid double use)
			while excess_idx < len(excess_trays) and excess_trays[excess_idx].get('tray_id') == linked_tray_id:
				linked_tray_id = None  # only skip once
				excess_idx += 1

			if excess_idx < len(excess_trays):
				et = excess_trays[excess_idx]
				tray_ids.append({
					'tray_id': et['tray_id'],
					'qty': slot['qty'],
					'auto_linked': False,
					'linked_to': None,
					'is_top_half_filled': False,
					'editable': False,
				})
				excess_idx += 1
			else:
				tray_ids.append({
					'tray_id': None,
					'qty': slot['qty'],
					'auto_linked': False,
					'linked_to': None,
					'is_top_half_filled': False,
					'editable': False,
				})

	half_filled['tray_ids'] = tray_ids
	return half_filled


def _half_filled_list_to_dict(hf_list, total_qty=0):
	"""Convert old half_filled list format (from multi-model) to new dict format.
	Preserves tray_ids from source entries when available (for BH recalc)."""
	if not hf_list:
		return {'exists': False, 'total_qty': 0, 'tray_count': 0, 'slots': [], 'tray_ids': None}
	slots = []
	tray_ids_list = []
	has_any_tray_id = False
	for i, hf in enumerate(hf_list):
		qty_val = int(hf.get('qty', 0) or 0)
		slots.append({
			'index': i + 1,
			'qty': qty_val,
			'type': 'partial' if i == 0 else 'auto',
			'editable': i == 0,
		})
		tid = hf.get('tray_id')
		if tid:
			has_any_tray_id = True
			tray_ids_list.append({
				'tray_id': tid, 'qty': qty_val,
				'auto_linked': i == 0, 'linked_to': tid if i == 0 else None,
				'is_top_half_filled': i == 0, 'editable': i == 0,
				'model': hf.get('model', ''),
			})
		else:
			tray_ids_list.append({
				'tray_id': None, 'qty': qty_val,
				'auto_linked': False, 'linked_to': None,
				'is_top_half_filled': i == 0, 'editable': i == 0,
				'model': hf.get('model', ''),
			})
	total = total_qty or sum(s['qty'] for s in slots)
	has_tray_ids = has_any_tray_id and len(tray_ids_list) == len(slots)
	return {
		'exists': True, 'total_qty': total, 'tray_count': len(slots), 'slots': slots,
		'tray_ids': tray_ids_list if has_tray_ids else None,
	}


def build_unified_tray_table(computed, lot_qty, jig_capacity, model_code='', tray_capacity=12):
	"""Build a unified tray table combining delink + excess trays into one flat list.

	Each row represents a tray or a split portion of a tray. The frontend renders
	this as a single <table> — no separate delink / excess sections needed.

	Args:
		computed: dict returned by compute_jig_loading()
		lot_qty: total lot quantity
		jig_capacity: original jig capacity (before BH)
		model_code: display label for the model (e.g. '1805NAD02')
		tray_capacity: default tray capacity

	Returns:
		list of row dicts, each with:
			sno, model_code, tray_id, original_qty, scan_tray_id, scan_label,
			delink_qty, status, row_type, is_scannable, is_checkbox_enabled
	"""
	delink_tray_info = computed.get('delink_tray_info', [])
	excess_info = computed.get('excess_info', {})
	excess_trays = excess_info.get('excess_trays', [])
	excess_qty = int(excess_info.get('excess_qty', 0) or 0)
	effective_capacity = int(computed.get('effective_capacity', 0) or 0)

	rows = []
	sno = 1

	# --- DELINK ROWS (trays allocated to the jig) ---
	for dt in delink_tray_info:
		tray_id = dt.get('tray_id', '')
		qty = int(dt.get('qty', 0) or 0)
		original_qty = int(dt.get('original_qty', qty) or qty)
		is_capacity_split = bool(dt.get('is_capacity_split', False))

		if is_capacity_split:
			# This tray is split: delink portion + excess portion
			split_excess = int(dt.get('capacity_split_excess_qty', 0) or (original_qty - qty))
			# Row 1: delink portion
			rows.append({
				'sno': sno,
				'model_code': model_code,
				'tray_id': tray_id,
				'original_qty': original_qty,
				'scan_tray_id': tray_id,
				'delink_qty': qty,
				'status': 'Partially Qty - Delink',
				'row_type': 'delink_partial',
				'is_scannable': True,
				'is_checkbox_enabled': True,
			})
			sno += 1
			# Row 2: excess portion of the SAME tray (mandate scan)
			# Calculate total excess for the entire lot
			total_excess_qty = excess_qty
			rows.append({
				'sno': '',  # sub-row, no separate serial number
				'model_code': '',
				'tray_id': '',
				'original_qty': '',
				'scan_tray_id': tray_id,
				'scan_label': '(Mandate Scan)',
				'delink_qty': split_excess,
				'status': f'Partial Tray - Excess Lot Tray Scan = {total_excess_qty} (new lot in jig pick table)',
				'row_type': 'partial_excess',
				'is_scannable': True,
				'is_checkbox_enabled': False,
			})
		else:
			# Fully delinked tray
			rows.append({
				'sno': sno,
				'model_code': model_code,
				'tray_id': tray_id,
				'original_qty': original_qty,
				'scan_tray_id': tray_id,
				'delink_qty': qty,
				'status': 'Fully Delinked',
				'row_type': 'delink_full',
				'is_scannable': True,
				'is_checkbox_enabled': True,
			})
			sno += 1

	# --- EXCESS ROWS (trays beyond jig capacity) ---
	for et in excess_trays:
		et_tray_id = et.get('tray_id', '')
		et_qty = int(et.get('qty', 0) or 0)

		# Skip excess entries that are capacity-split (already handled above)
		already_in_delink = any(
			r.get('tray_id') == et_tray_id and r.get('row_type') in ('delink_partial', 'partial_excess')
			for r in rows
		)
		if already_in_delink:
			continue

		rows.append({
			'sno': sno,
			'model_code': model_code,
			'tray_id': et_tray_id,
			'original_qty': et_qty,
			'scan_tray_id': et_tray_id,
			'scan_label': '',
			'delink_qty': et_qty,
			'status': 'Excess Lot Tray Scan',
			'row_type': 'excess',
			'is_scannable': True,
			'is_checkbox_enabled': True,
		})
		sno += 1

	logging.info(json.dumps({
		'event': 'UNIFIED_TRAY_TABLE_BUILT',
		'total_rows': len(rows),
		'delink_rows': sum(1 for r in rows if r['row_type'].startswith('delink')),
		'excess_rows': sum(1 for r in rows if r['row_type'] in ('excess', 'partial_excess')),
		'lot_qty': lot_qty,
		'jig_capacity': jig_capacity,
		'effective_capacity': effective_capacity,
	}))

	return rows


def build_split_panel_data(computed, lot_qty, jig_capacity, model_code='', tray_capacity=12, scanned_tray_ids=None):
	"""Build split panel data for the 2-column delink + excess UI.

	Uses computed['all_trays'] so ALL trays are shown in the delink panel
	with original_qty, delink_qty, excess_qty.  No tray is hidden.

	LEFT column = delink_panel (ALL trays with qty split)
	RIGHT column = excess_panel (trays with excess_qty > 0)

	scanned_tray_ids: set of UPPERCASED tray_ids the frontend currently holds as
	validated/scanned (across any panel), used ONLY to auto-verify the single
	"partial delink" row (the tray physically split between delink + excess) once
	its paired Excess Top Tray has been scanned. This is a narrow, explicit
	exception — no other delink row is ever auto-completed this way.
	"""
	all_trays = computed.get('all_trays', [])
	excess_info = computed.get('excess_info', {})
	excess_qty = int(excess_info.get('excess_qty', 0) or 0)
	effective_capacity = int(computed.get('effective_capacity', 0) or 0)
	scanned_tray_ids = scanned_tray_ids or set()

	# ===== LEFT: DELINK PANEL (ALL TRAYS) =====
	delink_rows = []
	sno = 1
	split_tray = None  # tray with both delink_qty > 0 and excess_qty > 0

	for at in all_trays:
		tray_id = at.get('tray_id', '')
		original_qty = int(at.get('original_qty', 0) or 0)
		delink_qty = int(at.get('delink_qty', 0) or 0)
		excess_qty_row = int(at.get('excess_qty', 0) or 0)
		is_top = bool(at.get('top_tray', False))

		is_partial = delink_qty > 0 and excess_qty_row > 0

		row = {
			'sno': sno,
			'model_code': model_code,
			'tray_id': tray_id,
			'original_qty': original_qty,
			'delink_qty': delink_qty,
			'excess_qty': excess_qty_row,
			'scan_tray_id': tray_id,
			'is_scannable': True,
			'is_checkbox_enabled': True,
			'is_top_tray': is_top,
			'is_partial': is_partial,
			'status': '',  # Empty until user action
			'state': 'default',
		}

		if is_partial:
			split_tray = {
				'tray_id': tray_id,
				'delink_qty': delink_qty,
				'excess_qty': excess_qty_row,
				'original_qty': original_qty,
			}
			# The partial delink row is the SAME physical tray as the Excess Top Tray
			# row — never a separate scan target. It is auto-verified the instant that
			# top tray is scanned/verified, and stays read-only otherwise.
			verified = str(tray_id or '').strip().upper() in scanned_tray_ids
			row['is_scannable'] = False
			row['is_checkbox_enabled'] = False
			row['auto_verify'] = True
			row['verified'] = verified
			row['state'] = 'scanned' if verified else 'default'
			row['status'] = 'Verified' if verified else 'Awaiting Excess Top Tray Scan'
			row['verify_message'] = (
				f'Delink ({delink_qty}) + Excess Top ({excess_qty_row}) = Verified'
				if verified else
				f'Awaiting Top Tray (Qty {excess_qty_row})'
			)

		delink_rows.append(row)
		sno += 1

	total_delink = sum(r['delink_qty'] for r in delink_rows)
	total_excess = sum(r['excess_qty'] for r in delink_rows)

	delink_panel = {
		'mode': 'inactive',
		'trays': delink_rows,
		'total_delink_qty': total_delink,
		'total_excess_qty': total_excess,
		'tray_count': len(delink_rows),
		'selection_limit': 0,
		'selected_count': 0,
	}

	# ===== RIGHT: EXCESS PANEL (trays with excess_qty > 0) =====
	excess_exists = excess_qty > 0
	excess_panel_trays = []
	top_tray_info = None
	ex_sno = 1

	if excess_exists:
		# 1. PARTIAL TRAY (split tray → top tray)
		if split_tray:
			top_tray_info = {
				'tray_id': split_tray['tray_id'],
				'qty': split_tray['excess_qty'],
				'is_mandate_scan': True,
				'is_top_tray': True,
				'is_editable': True,
				'scan_tray_id': split_tray['tray_id'],
				'model_code': model_code,
				'original_tray_id': split_tray['tray_id'],
				'verified': str(split_tray['tray_id'] or '').strip().upper() in scanned_tray_ids,
			}

		# 2. FULL EXCESS TRAYS (trays with delink_qty == 0)
		for at in all_trays:
			at_delink = int(at.get('delink_qty', 0) or 0)
			at_excess = int(at.get('excess_qty', 0) or 0)
			if at_excess <= 0 or at_delink > 0:
				continue  # Skip delink-only trays and the split tray
			excess_panel_trays.append({
				'sno': ex_sno,
				'tray_id': at.get('tray_id', ''),
				'original_tray_id': at.get('tray_id', ''),
				'model_code': model_code,
				'qty': at_excess,
				'scan_tray_id': at.get('tray_id', ''),
				'row_type': 'excess',
				'is_top_tray': False,
				'is_mandate_scan': False,
				'is_editable': False,
				'is_auto': True,
				'is_checkbox_enabled': True,
				'state': 'default',
			})
			ex_sno += 1

	excess_panel = {
		'exists': excess_exists,
		'total_excess_qty': excess_qty,
		'excess_tray_count': len(excess_panel_trays),
		'top_tray': top_tray_info,
		'trays': excess_panel_trays,
		'partial_tray': split_tray,
	}

	logging.info(json.dumps({
		'event': 'SPLIT_PANEL_BUILT',
		'all_trays': len(all_trays),
		'delink_trays': len(delink_rows),
		'excess_trays': len(excess_panel_trays),
		'excess_exists': excess_exists,
		'excess_qty': excess_qty,
		'total_delink': total_delink,
		'total_excess': total_excess,
		'has_split_tray': split_tray is not None,
		'lot_qty': lot_qty,
		'jig_capacity': jig_capacity,
	}))

	return {
		'delink_panel': delink_panel,
		'excess_panel': excess_panel,
		'meta': {
			'model_name': model_code,
			'tray_placeholder': f' {model_code}' if model_code else 'Scan Tray ID',
			'excess_placeholder': 'Scan excess tray',
		},
	}


def build_split_panel_data_multi_model(multi_model_allocation, computed, lot_qty, jig_capacity, tray_capacity=12, scanned_tray_ids=None):
	"""Build split panel data for multi-model jig loading.
	Uses computed['all_trays'] so ALL trays (from all models) are shown.
	Each tray has original_qty, delink_qty, excess_qty."""

	effective_capacity = int(computed.get('effective_capacity', 0) or 0)
	excess_qty = int(computed.get('excess_qty', 0) or 0)
	all_trays = computed.get('all_trays', [])
	scanned_tray_ids = scanned_tray_ids or set()

	# Build tray→model maps for resolving model_code
	tray_model_map = {}
	lot_model_map = {}
	tray_meta_map = {}  # tray_id → {lot_id, batch_id, model_index, color_class}
	for m_alloc in multi_model_allocation:
		m_code = (m_alloc.get('model_image_label') or m_alloc.get('model_name') or m_alloc.get('model') or '').strip()
		m_lot_id = m_alloc.get('lot_id', '')
		m_batch_id = m_alloc.get('batch_id', '')
		m_index = m_alloc.get('model_index', 0)
		m_color = m_alloc.get('color_class', '')
		if m_lot_id:
			lot_model_map[m_lot_id] = m_code
		for tray in m_alloc.get('tray_info', []):
			tid = tray.get('tray_id', '')
			tray_model_map[tid] = m_code
			tray_meta_map[tid] = {'lot_id': m_lot_id, 'batch_id': m_batch_id, 'model_index': m_index, 'color_class': m_color}

	delink_rows = []
	sno = 1
	split_tray = None

	for at in all_trays:
		tray_id = at.get('tray_id', '')
		original_qty = int(at.get('original_qty', 0) or 0)
		delink_qty = int(at.get('delink_qty', 0) or 0)
		excess_qty_row = int(at.get('excess_qty', 0) or 0)
		source_lot = at.get('source_lot_id', '')

		# Resolve model from tray map → lot map fallback
		m_code = tray_model_map.get(tray_id, '')
		if not m_code and source_lot:
			m_code = lot_model_map.get(source_lot, '')
		meta = tray_meta_map.get(tray_id, {})

		is_partial = delink_qty > 0 and excess_qty_row > 0

		delink_row = {
			'sno': sno,
			'model_code': m_code,
			'tray_id': tray_id,
			'original_qty': original_qty,
			'delink_qty': delink_qty,
			'excess_qty': excess_qty_row,
			'scan_tray_id': tray_id,
			'is_scannable': True,
			'is_checkbox_enabled': True,
			'is_top_tray': False,
			'is_partial': is_partial,
			'status': '',
			'state': 'default',
			'lot_id': meta.get('lot_id', source_lot),
			'batch_id': meta.get('batch_id', ''),
			'model_index': meta.get('model_index', 0),
			'color_class': meta.get('color_class', ''),
		}

		if is_partial:
			split_tray = {
				'tray_id': tray_id,
				'delink_qty': delink_qty,
				'excess_qty': excess_qty_row,
				'original_qty': original_qty,
				'source_lot_id': meta.get('lot_id', source_lot),
				'batch_id': meta.get('batch_id', ''),
				'model_code': m_code,
			}
			# Same physical tray as the Excess Top Tray row — auto-verify once that
			# top tray is scanned, never a separate scan target of its own.
			verified = str(tray_id or '').strip().upper() in scanned_tray_ids
			delink_row['is_scannable'] = False
			delink_row['is_checkbox_enabled'] = False
			delink_row['auto_verify'] = True
			delink_row['verified'] = verified
			delink_row['state'] = 'scanned' if verified else 'default'
			delink_row['status'] = 'Verified' if verified else 'Awaiting Excess Top Tray Scan'
			delink_row['verify_message'] = (
				f'Delink ({delink_qty}) + Excess Top ({excess_qty_row}) = Verified'
				if verified else
				f'Awaiting Top Tray (Qty {excess_qty_row})'
			)

		delink_rows.append(delink_row)
		sno += 1

	total_delink = sum(r['delink_qty'] for r in delink_rows)
	total_excess = sum(r['excess_qty'] for r in delink_rows)

	delink_panel = {
		'mode': 'inactive',
		'trays': delink_rows,
		'total_delink_qty': total_delink,
		'total_excess_qty': total_excess,
		'tray_count': len(delink_rows),
		'selection_limit': 0,
		'selected_count': 0,
	}

	# Excess panel: trays with delink_qty == 0 and excess_qty > 0
	excess_exists = excess_qty > 0
	excess_panel_trays = []
	top_tray_info = None
	ex_sno = 1

	if excess_exists:
		# Partial (split) tray as top tray
		if split_tray:
			st_model = split_tray.get('model_code') or tray_model_map.get(split_tray['tray_id'], '')
			st_lot_id = split_tray.get('source_lot_id', '')
			st_batch_id = split_tray.get('batch_id', '')
			if not st_model:
				for at in all_trays:
					if at.get('tray_id') == split_tray['tray_id'] and at.get('source_lot_id'):
						st_model = lot_model_map.get(at['source_lot_id'], '')
						st_lot_id = at.get('source_lot_id', '')
						break
			if not st_lot_id:
				st_meta = tray_meta_map.get(split_tray['tray_id'], {})
				st_lot_id = st_meta.get('lot_id', '')
				st_batch_id = st_batch_id or st_meta.get('batch_id', '')
				if not st_lot_id:
					for at in all_trays:
						if at.get('tray_id') == split_tray['tray_id']:
							st_lot_id = at.get('source_lot_id', '')
							break
			top_tray_info = {
				'tray_id': split_tray['tray_id'],
				'qty': split_tray['excess_qty'],
				'is_mandate_scan': True,
				'is_top_tray': True,
				'is_editable': True,
				'scan_tray_id': split_tray['tray_id'],
				'model_code': st_model,
				'original_tray_id': split_tray['tray_id'],
				'lot_id': st_lot_id,
				'batch_id': st_batch_id,
				'verified': str(split_tray['tray_id'] or '').strip().upper() in scanned_tray_ids,
			}

		# Full excess trays (delink_qty == 0)
		for at in all_trays:
			at_delink = int(at.get('delink_qty', 0) or 0)
			at_excess = int(at.get('excess_qty', 0) or 0)
			if at_excess <= 0 or at_delink > 0:
				continue
			at_tid = at.get('tray_id', '')
			et_meta = tray_meta_map.get(at_tid, {})
			et_model = tray_model_map.get(at_tid, '')
			et_lot_id = at.get('source_lot_id', '')
			if not et_model and et_lot_id:
				et_model = lot_model_map.get(et_lot_id, '')
			if not et_lot_id:
				et_lot_id = et_meta.get('lot_id', '')
			excess_panel_trays.append({
				'sno': ex_sno,
				'tray_id': at_tid,
				'original_tray_id': at_tid,
				'model_code': et_model,
				'qty': at_excess,
				'scan_tray_id': at_tid,
				'row_type': 'excess',
				'is_top_tray': False,
				'is_mandate_scan': False,
				'is_editable': False,
				'is_auto': True,
				'is_checkbox_enabled': True,
				'state': 'default',
				'lot_id': et_lot_id,
				'batch_id': et_meta.get('batch_id', ''),
			})
			ex_sno += 1

	excess_panel = {
		'exists': excess_exists,
		'total_excess_qty': excess_qty,
		'excess_tray_count': len(excess_panel_trays),
		'top_tray': top_tray_info,
		'trays': excess_panel_trays,
		'partial_tray': split_tray,
	}

	# Build model names from allocations for meta
	model_names = []
	for ma in multi_model_allocation:
		name = (ma.get('model_image_label') or ma.get('model_name') or ma.get('model') or '').strip()
		if name:
			model_names.append(name)

	logging.info(json.dumps({
		'event': 'SPLIT_PANEL_MULTI_MODEL_BUILT',
		'all_trays': len(all_trays),
		'delink_trays': len(delink_rows),
		'excess_trays': len(excess_panel_trays),
		'model_count': len(multi_model_allocation),
	}))

	return {
		'delink_panel': delink_panel,
		'excess_panel': excess_panel,
		'meta': {
			'model_name': ', '.join(model_names) if model_names else '',
			'tray_placeholder': 'Scan Tray ID',
			'excess_placeholder': 'Scan excess tray',
		},
	}


def build_unified_tray_table_multi_model(multi_model_allocation, computed, lot_qty, jig_capacity, tray_capacity=12):
	"""Build unified tray table for multi-model jig loading.

	Merges all model allocations + excess into a single flat table.
	"""
	rows = []
	sno = 1
	effective_capacity = int(computed.get('effective_capacity', 0) or 0)
	excess_qty = int(computed.get('excess_qty', 0) or 0)

	for m_alloc in multi_model_allocation:
		model_code = (m_alloc.get('model_image_label') or m_alloc.get('model_name') or m_alloc.get('model') or '').strip()
		model_lot_id = m_alloc.get('lot_id', '')
		model_batch_id = m_alloc.get('batch_id', '')
		model_index = m_alloc.get('model_index', 0)
		color_class = m_alloc.get('color_class', '')

		for tray in m_alloc.get('tray_info', []):
			tray_id = tray.get('tray_id', '')
			qty = int(tray.get('qty', 0) or 0)
			original_qty = int(tray.get('original_qty', qty) or qty)
			is_partial = bool(tray.get('is_partial', False))

			rows.append({
				'sno': sno,
				'model_code': model_code,
				'tray_id': tray_id,
				'original_qty': original_qty,
				'scan_tray_id': tray_id,
				'delink_qty': qty,
				'status': 'Partially Qty - Delink' if is_partial else 'Fully Delinked',
				'row_type': 'delink_partial' if is_partial else 'delink_full',
				'is_scannable': True,
				'is_checkbox_enabled': True,
				'lot_id': model_lot_id,
				'batch_id': model_batch_id,
				'model_index': model_index,
				'color_class': color_class,
			})
			sno += 1

	# Add excess trays from computed excess_info
	excess_info = computed.get('excess_info', {})
	for et in excess_info.get('excess_trays', []):
		et_tray_id = et.get('tray_id', '')
		et_qty = int(et.get('qty', 0) or 0)
		already_in = any(r.get('tray_id') == et_tray_id for r in rows)
		if already_in:
			continue
		rows.append({
			'sno': sno,
			'model_code': '',
			'tray_id': et_tray_id,
			'original_qty': et_qty,
			'scan_tray_id': et_tray_id,
			'scan_label': '',
			'delink_qty': et_qty,
			'status': 'Excess Lot Tray Scan',
			'row_type': 'excess',
			'is_scannable': True,
			'is_checkbox_enabled': True,
			'lot_id': '',
			'batch_id': '',
			'model_index': 0,
			'color_class': '',
		})
		sno += 1

	logging.info(json.dumps({
		'event': 'UNIFIED_TRAY_TABLE_MULTI_MODEL_BUILT',
		'total_rows': len(rows),
		'model_count': len(multi_model_allocation),
	}))

	return rows


def get_next_jig_cycle(jig_id, lot_id):
	"""Calculate the next jig cycle count based on completed operations.

	CRITICAL: Lot-aware cycle tracking + draft detection across all lots.
	Checks BOTH Jig.drafted flag AND draft records in JigCompleted.

	Logic:
	- Check if jig has ANY draft record (across all lots) OR jig.drafted=True
	- If drafted by different lot → return 'drafted_by_other_lot': True
	- Count submitted JigCompleted records for THIS (jig_id, lot_id) pair
	- Cycle starts at 1 for first use, increments after unload/reload

	Args:
		jig_id: The jig identifier
		lot_id: The lot ID attempting to use this jig

	Returns:
		{
			'cycle_count': cycle number per (jig_id, lot_id) pair,
			'is_loaded': boolean,
			'can_reuse': boolean,
			'is_drafted': True if jig has draft in ANY lot,
			'drafted_by_other_lot': True if drafted by different lot
		}
	"""
	try:
		# Check if jig exists in master table
		jig_obj = Jig.objects.filter(jig_qr_id=jig_id).first()
		if not jig_obj:
			return {
				'cycle_count': 1,
				'is_loaded': False,
				'can_reuse': True,
				'is_drafted': False,
				'drafted_by_other_lot': False
			}

		# CRITICAL: Check if jig has drafted flag set in Jig model
		# This is the primary indicator that draft is active
		has_draft = jig_obj.drafted
		drafted_by_other_lot = False

		if has_draft:
			# If drafted flag is set, check which lot has the draft
			# by looking at jig_obj.lot_id
			if jig_obj.lot_id and jig_obj.lot_id != lot_id:
				drafted_by_other_lot = True
		else:
			# Fallback: Check draft records in JigCompleted (for consistency)
			draft_records = JigCompleted.objects.filter(
				jig_id=jig_id,
				draft_status__in=['draft', 'active']
			)
			has_draft = draft_records.exists()
			if has_draft:
				current_lot_draft = draft_records.filter(lot_id=lot_id).exists()
				drafted_by_other_lot = not current_lot_draft

		# Current load status (independent of lot)
		is_loaded = jig_obj.is_loaded

		# ✅ Read cycle_count directly from Jig model (SSOT)
		# cycle_count is incremented automatically during unloading
		# Display cycle_count + 1 to show "next cycle" for new loading
		# (cycle_count=0 means never unloaded, display as Cycle 1)
		# (cycle_count=1 means unloaded once, display as Cycle 2 for next load)
		current_cycle_count = jig_obj.cycle_count if jig_obj.cycle_count is not None else 0
		next_cycle = current_cycle_count + 1

		# Can reuse only if:
		# 1. NOT currently loaded (is_loaded = False)
		# 2. NO draft records OR the draft belongs to the SAME lot (same-lot re-entry is allowed)
		can_reuse = (not is_loaded) and (not has_draft or not drafted_by_other_lot)

		return {
			'cycle_count': next_cycle,
			'is_loaded': is_loaded,
			'can_reuse': can_reuse,
			'is_drafted': has_draft,
			'drafted_by_other_lot': drafted_by_other_lot
		}

	except Exception as e:
		logging.exception(f"Error calculating jig cycle for {jig_id}, {lot_id}: {e}")
		# Fallback
		return {
			'cycle_count': 1,
			'is_loaded': False,
			'can_reuse': True,
			'is_drafted': False,
			'drafted_by_other_lot': False
		}


def resolve_jig_capacity_for_batch(batch_obj):
	"""
	Resolve jig capacity for a batch using JigLoadingMaster.

	Fallback chain (in order):
	  1. Exact batch plating_stk_no -> JigLoadingMaster.
	  2. Direct model_stock_no ForeignKey match.
	  3. Batch model_no prefix -> JigLoadingMaster.

	Exact plating stock is authoritative when present so a stale legacy
	model_stock_no link cannot override the correct per-model jig master.
	"""
	if not batch_obj:
		return None

	model_obj = getattr(batch_obj, 'model_stock_no', None)

	# 1. Exact lookup via batch plating_stk_no
	plating_stk_no = getattr(batch_obj, 'plating_stk_no', '') or ''
	if plating_stk_no:
		master = JigLoadingMaster.objects.filter(
			model_stock_no__plating_stk_no__iexact=plating_stk_no
		).first()
		if master and getattr(master, 'jig_capacity', None):
			return int(master.jig_capacity)

	# 2. Direct model_stock_no match (legacy/fallback path)
	if model_obj:
		master = JigLoadingMaster.objects.filter(model_stock_no=model_obj).first()
		if master and getattr(master, 'jig_capacity', None):
			return int(master.jig_capacity)

	# 3. Lookup via model_no prefix
	if model_obj:
		model_no = getattr(model_obj, 'model_no', '') or ''
		if model_no:
			master = JigLoadingMaster.objects.filter(
				model_stock_no__model_no=model_no
			).first()
			if master and getattr(master, 'jig_capacity', None):
				return int(master.jig_capacity)

	return None


def fetch_lot_data(lot_id, batch_id, jig_capacity_override=None):
	"""Fetch lot qty, jig capacity, tray capacity, and model metadata from DB."""
	lot_qty = 0
	jig_capacity = 0
	tray_capacity = 12
	model_image_url = '/static/assets/images/imagePlaceholder.jpg'
	model_image_label = ''
	nickel_bath_type = ''
	tray_type_name = ''

	try:
		stock = TotalStockModel.objects.filter(lot_id=lot_id).first()
		if stock:
			lot_qty = int(
				getattr(stock, 'brass_audit_accepted_qty', None)
				or getattr(stock, 'brass_audit_physical_qty', None)
				or getattr(stock, 'total_stock', 0) or 0
			)
		else:
			# Fallback: check if this is an excess lot (EX-* prefix)
			excess_rec = ExcessLotRecord.objects.filter(new_lot_id=lot_id).first()
			if excess_rec:
				lot_qty = int(excess_rec.lot_qty or 0)
				logging.info(f'[FETCH_LOT_DATA] Excess lot {lot_id} resolved via ExcessLotRecord — qty={lot_qty}')
	except Exception:
		logging.exception('fetch_lot_data: lot qty fetch failed')

	try:
		if jig_capacity_override:
			jig_capacity = int(jig_capacity_override)
		else:
			batch_obj = ModelMasterCreation.objects.filter(batch_id=batch_id).first()
			resolved = resolve_jig_capacity_for_batch(batch_obj)
			if resolved:
				jig_capacity = resolved
			else:
				jig_capacity = lot_qty
	except Exception:
		jig_capacity = lot_qty or 0

	try:
		batch_obj = ModelMasterCreation.objects.filter(batch_id=batch_id).first()
		if batch_obj:
			tc = getattr(batch_obj, 'tray_capacity', None)
			if not tc:
				model_obj = getattr(batch_obj, 'model_stock_no', None)
				if model_obj:
					tc = getattr(model_obj, 'tray_capacity', None)
			if tc:
				tray_capacity = int(tc)
	except Exception:
		pass

	try:
		mm = None
		batch_obj = ModelMasterCreation.objects.filter(batch_id=batch_id).first()
		if batch_obj:
			mm = getattr(batch_obj, 'model_stock_no', None) or batch_obj
		if mm:
			try:
				if hasattr(mm, 'images'):
					from modelmasterapp.image_utils import sort_images_front_first
					imgs = mm.images.all()
					if imgs.exists():
						first_img = sort_images_front_first(imgs)[0]
						if getattr(first_img, 'master_image', None):
							model_image_url = first_img.master_image.url
			except Exception:
				pass
			# Prefer batch-level plating_stk_no (ModelMasterCreation) — consistent with pick table and fetch_model_metadata
			batch_plating = (getattr(batch_obj, 'plating_stk_no', '') or '') if batch_obj else ''
			model_image_label = batch_plating or getattr(mm, 'plating_stk_no', '') or getattr(mm, 'model_no', '') or ''
			nickel_bath_type = getattr(mm, 'ep_bath_type', '') or ''
			try:
				tt = getattr(mm, 'tray_type', None)
				if tt:
					tray_type_name = getattr(tt, 'tray_type', '') if not isinstance(tt, str) else tt
			except Exception:
				pass
			# Resolve abbreviation to full parent name (e.g. JB→Jumbo, ND→Normal)
			if tray_type_name:
				try:
					from modelmasterapp.models import TrayType as _TrayType
					_tt_obj = _TrayType.objects.filter(tray_type=tray_type_name).first()
					if _tt_obj and _tt_obj.tray_color:
						_parent = _TrayType.objects.filter(
							tray_capacity=_tt_obj.tray_capacity, tray_color__isnull=True
						).first()
						if _parent:
							tray_type_name = _parent.tray_type
				except Exception:
					pass
	except Exception:
		pass

	return {
		'lot_qty': lot_qty,
		'jig_capacity': jig_capacity,
		'tray_capacity': tray_capacity,
		'model_image_url': model_image_url,
		'model_image_label': model_image_label,
		'nickel_bath_type': nickel_bath_type,
		'tray_type': tray_type_name,
	}


def fetch_trays_for_lot(lot_id):
	"""Unified tray resolver — single source of truth for Jig Loading.

	Priority chain:
	  0. ExcessLotTray  — excess lot's own trays (highest priority for EX-* lots)
	  1. JigLoadTrayId  — Jig's own table (most authoritative)
	  2. BrassAuditTrayId — trays synced after Brass Audit acceptance
	  3. BrassTrayId (Brass QC) — upstream fallback

	Logs the source so tray origin is always traceable.
	"""
	trays = []
	source = 'none'

	# Priority 0: ExcessLotTray — excess lot trays (for EX-* lots)
	try:
		excess_trays_qs = ExcessLotTray.objects.filter(lot_id=lot_id).order_by('id')
		if excess_trays_qs.exists():
			source = 'ExcessLotTray'
			for t in excess_trays_qs:
				trays.append({
					'tray_id': t.tray_id or '',
					'qty': int(t.qty or 0),
					'top_tray': False,
					'rejected': False,
					'delinked': False,
				})
			logging.info(f'[TRAY SOURCE] lot_id={lot_id}, source={source}, count={len(trays)}')
			return trays
	except Exception:
		logging.exception('fetch_trays_for_lot ExcessLotTray query failed')

	try:
		qs = JigLoadTrayId.objects.filter(lot_id=lot_id).order_by('id')
		if qs.exists():
			source = 'JigLoadTrayId'
			for t in qs:
				trays.append({
					'tray_id': getattr(t, 'tray_id', ''),
					'qty': int(getattr(t, 'tray_quantity', 0) or 0),
					'top_tray': bool(getattr(t, 'top_tray', False) or False),
					'rejected': bool(getattr(t, 'rejected_tray', False) or False),
					'delinked': bool(getattr(t, 'delink_tray', False) or False),
				})
	except Exception:
		logging.exception('fetch_trays_for_lot JigLoadTrayId query failed')

	if not trays:
		try:
			from BrassAudit.models import BrassAuditTrayId as _BATrayId
			ba_qs = _BATrayId.objects.filter(
				lot_id=lot_id, delink_tray=False, rejected_tray=False
			).order_by('id')
			if ba_qs.exists():
				source = 'BrassAuditTrayId'
				for t in ba_qs:
					trays.append({
						'tray_id': getattr(t, 'tray_id', ''),
						'qty': int(getattr(t, 'tray_quantity', 0) or 0),
						'top_tray': bool(getattr(t, 'top_tray', False) or False),
						'rejected': False,
						'delinked': False,
					})
		except Exception:
			logging.exception('fetch_trays_for_lot BrassAuditTrayId query failed')

	if not trays:
		try:
			from Brass_QC.models import BrassTrayId as _BQTrayId
			bq_qs = _BQTrayId.objects.filter(
				lot_id=lot_id, delink_tray=False
			).order_by('id')
			if bq_qs.exists():
				source = 'BrassTrayId'
				for t in bq_qs:
					trays.append({
						'tray_id': getattr(t, 'tray_id', ''),
						'qty': int(getattr(t, 'tray_quantity', 0) or 0),
						'top_tray': bool(getattr(t, 'top_tray', False) or False),
						'rejected': False,
						'delinked': False,
					})
		except Exception:
			logging.exception('fetch_trays_for_lot BrassTrayId query failed')

	logging.info(f'[TRAY SOURCE] lot_id={lot_id}, source={source}, count={len(trays)}')
	return trays


def count_trays_for_lot(lot_id):
	"""Count trays for a lot using the unified resolver. Returns count only (no details)."""
	return len(fetch_trays_for_lot(lot_id))


def cap_trays_to_lot_qty(trays, lot_qty, lot_id):
	"""Return ordered tray portions capped to the backend-authoritative lot qty."""
	remaining_qty = max(0, int(lot_qty or 0))
	capped_trays = []
	for tray in trays:
		if remaining_qty <= 0:
			break
		tray_qty = max(0, int(tray.get('qty', 0) or 0))
		if tray_qty <= 0:
			continue
		capped_tray = dict(tray)
		capped_tray['qty'] = min(tray_qty, remaining_qty)
		capped_tray['source_lot_id'] = lot_id
		capped_trays.append(capped_tray)
		remaining_qty -= capped_tray['qty']
	return capped_trays


def resolve_secondary_lots(secondary_lots):
	"""Resolve secondary quantities from the database; never trust browser-stored qty."""
	resolved_lots = []
	seen_lot_ids = set()
	for secondary in secondary_lots or []:
		if not isinstance(secondary, dict):
			continue
		lot_id = secondary.get('lot_id')
		if not lot_id or lot_id in seen_lot_ids:
			continue
		batch_id = secondary.get('batch_id') or ''
		lot_data = fetch_lot_data(lot_id, batch_id)
		resolved_lots.append({
			'lot_id': lot_id,
			'batch_id': batch_id,
			'qty': int(lot_data.get('lot_qty', 0) or 0),
		})
		seen_lot_ids.add(lot_id)
	return resolved_lots


def aggregate_multi_model_trays(primary_lot_id, secondary_lots, primary_lot_qty=None):
	"""Aggregate trays from all model lots (primary + secondary) into one combined list.
	Used by JigLoadUpdateAPI and JigSaveAPI for multi-model recomputation.
	Each lot is capped to its backend-authoritative quantity before computation."""
	all_trays = []
	seen_lot_ids = set()

	# Primary model trays first
	if primary_lot_id:
		primary_trays = fetch_trays_for_lot(primary_lot_id)
		if primary_lot_qty is None:
			primary_lot_qty = fetch_lot_data(primary_lot_id, '').get('lot_qty', 0)
		all_trays.extend(cap_trays_to_lot_qty(primary_trays, primary_lot_qty, primary_lot_id))
		seen_lot_ids.add(primary_lot_id)

	# Secondary model trays
	for sec in (secondary_lots or []):
		sec_lot_id = sec.get('lot_id')
		if not sec_lot_id or sec_lot_id in seen_lot_ids:
			continue
		sec_trays = fetch_trays_for_lot(sec_lot_id)
		all_trays.extend(cap_trays_to_lot_qty(sec_trays, sec.get('qty', 0), sec_lot_id))
		seen_lot_ids.add(sec_lot_id)

	logging.info(f"[AGGREGATE] Combined {len(all_trays)} trays from {len(seen_lot_ids)} lots: {list(seen_lot_ids)}")
	return all_trays


def validate_tray_for_scan(tray_id, lot_id, already_scanned_ids=None, allow_reuse_delink=False, allow_new_half_filled=False, strict_excess_lot=False):
	"""Validate a tray ID for scanning.
	Returns: (is_valid, tray_qty, validation_status, message)
	Falls back to BrassAuditTrayId and BrassTrayId if not found in JigLoadTrayId.
	"""
	if not tray_id or not lot_id:
		return False, 0, 'error', 'tray_id and lot_id are required'
	if already_scanned_ids and tray_id in already_scanned_ids and not allow_reuse_delink:
		return False, 0, 'duplicate', 'Tray already scanned'
	try:
		# Priority 0: ExcessLotTray (for EX-* excess lots)
		is_registered_excess_lot = False
		if lot_id:
			try:
				is_registered_excess_lot = lot_id.startswith('EX-') or ExcessLotRecord.objects.filter(new_lot_id=lot_id).exists()
			except Exception:
				is_registered_excess_lot = lot_id.startswith('EX-')
		if is_registered_excess_lot:
			try:
				excess_tray = ExcessLotTray.objects.filter(tray_id=tray_id, lot_id=lot_id).first()
				if excess_tray:
					tray_qty = int(excess_tray.qty or 0)
					logging.info(f'[VALIDATE TRAY] tray_id={tray_id} resolved via ExcessLotTray for excess lot {lot_id}')
					return True, tray_qty, 'success', 'Tray validated'
			except Exception:
				pass
			if strict_excess_lot:
				return False, 0, 'invalid_tray', 'Tray does not belong to this excess lot/model'

		# Priority 1: JigLoadTrayId
		tray = JigLoadTrayId.objects.filter(tray_id=tray_id, lot_id=lot_id).first()
		if tray:
			tray_qty = int(tray.tray_quantity or 0)
			return True, tray_qty, 'success', 'Tray validated'

		# Priority 2: BrassAuditTrayId
		try:
			from BrassAudit.models import BrassAuditTrayId as _BATrayId
			ba_tray = _BATrayId.objects.filter(tray_id=tray_id, lot_id=lot_id, delink_tray=False, rejected_tray=False).first()
			if ba_tray:
				tray_qty = int(ba_tray.tray_quantity or 0)
				logging.info(f'[VALIDATE TRAY] tray_id={tray_id} resolved via BrassAuditTrayId')
				return True, tray_qty, 'success', 'Tray validated'
		except Exception:
			pass

		# Priority 3: BrassTrayId
		try:
			from Brass_QC.models import BrassTrayId as _BQTrayId
			bq_tray = _BQTrayId.objects.filter(tray_id=tray_id, lot_id=lot_id, delink_tray=False).first()
			if bq_tray:
				tray_qty = int(bq_tray.tray_quantity or 0)
				logging.info(f'[VALIDATE TRAY] tray_id={tray_id} resolved via BrassTrayId')
				return True, tray_qty, 'success', 'Tray validated'
		except Exception:
			pass

		if allow_new_half_filled:
			return True, 0, 'success', 'New tray accepted'
		return False, 0, 'invalid_tray', 'Invalid tray or wrong lot'
	except Exception as e:
		logging.exception(f'validate_tray_for_scan error: {e}')
		return False, 0, 'error', 'Server error during tray validation'


# =============================================================================
# NEW CONSOLIDATED APIs — ONE API PER ACTION
# =============================================================================
class JigLoadInitAPI(APIView):
	"""POST /api/jig/load/init/ — Unified initialization for Jig Loading.
	Merges init-jig-load + tray-info. Single source of truth."""
	permission_classes = [IsAuthenticated]

	def post(self, request):
		payload = request.data
		lot_id = payload.get('lot_id')
		batch_id = payload.get('batch_id')
		jig_capacity_override = payload.get('jig_capacity')
		multi_model_flag = payload.get('multi_model', False)
		secondary_lots = payload.get('secondary_lots', [])

		if not lot_id or not batch_id:
			return Response({'error': 'lot_id and batch_id are required'}, status=status.HTTP_400_BAD_REQUEST)
		if multi_model_flag:
			secondary_lots = resolve_secondary_lots(secondary_lots)

		# 0. Check for existing draft up front (single source of truth) — needed below to
		# resolve the default broken_hooks so a re-opened draft renders the SAME split-panel
		# layout (delink/excess slot counts) it had when saved. Without this, effective_capacity
		# is recomputed with broken_hooks=0 on every open, shifting the excess-panel slot count
		# and breaking positional restore of previously-scanned excess trays (see JigSaveAPI GET).
		draft = JigCompleted.objects.filter(
			batch_id=batch_id, lot_id=lot_id, user=request.user, draft_status__in=['draft', 'active']
		).first()

		# Prefer an explicit broken_hooks from the frontend (live BH preview / recalculation).
		# Otherwise fall back to the saved draft's broken_hooks — but ONLY for a plain reopen,
		# never for a fresh multi-model merge (secondary_lots present), so stale BH from a prior
		# single-model draft never bleeds into a brand-new "Add Model" combination.
		if 'broken_hooks' in payload:
			broken_hooks = int(payload.get('broken_hooks', 0) or 0)
		elif draft and not secondary_lots:
			broken_hooks = int(getattr(draft, 'broken_hooks', 0) or 0)
		else:
			broken_hooks = 0

		logging.info(json.dumps({
			'event': 'JIG_LOAD_INIT',
			'lot_id': lot_id, 'batch_id': batch_id,
			'multi_model': bool(multi_model_flag)
		}))

		# 1. Fetch all data in one place
		lot_data = fetch_lot_data(lot_id, batch_id, jig_capacity_override)
		lot_qty = lot_data['lot_qty']
		jig_capacity = lot_data['jig_capacity']
		tray_capacity = lot_data['tray_capacity']

		# ===== EXCESS LOT DETECTION (early — applies to BOTH single and multi-model) =====
		# Two paths: (A) EX-* lot_id → fetch_trays_for_lot resolves from ExcessLotTray automatically
		#             (B) is_excess_lot flag with parent lot_id → resolve from JigCompleted.half_filled_tray_info
		is_excess_lot = payload.get('is_excess_lot', False)
		excess_primary_trays = None

		# Path A: lot_id is already an EX-* excess lot → fetch_trays_for_lot handles it
		is_excess_by_lot_id = ExcessLotRecord.objects.filter(new_lot_id=lot_id).exists()

		# Path B: Frontend signals is_excess_lot but lot_id is parent → resolve from JigCompleted
		if is_excess_lot and not is_excess_by_lot_id:
			try:
				submitted_jig = JigCompleted.objects.filter(
					batch_id=batch_id,
					draft_status='submitted',
					half_filled_tray_qty__gt=0
				).first()
				if submitted_jig:
					hf_info = submitted_jig.half_filled_tray_info or []
					if hf_info:
						excess_primary_trays = [
							{
								'tray_id': t['tray_id'],
								'qty': int(t.get('qty') or 0),
								'top_tray': bool(t.get('is_top_half_filled', False)),
							}
							for t in hf_info if isinstance(t, dict) and t.get('tray_id')
						]
						lot_qty = submitted_jig.half_filled_tray_qty or submitted_jig.excess_qty or 0
						logging.info(f'[INIT_EXCESS_LOT] Batch {batch_id} — excess primary trays: {len(excess_primary_trays)}, lot_qty={lot_qty}')
			except Exception:
				logging.exception('[INIT_EXCESS_LOT] Failed to resolve excess trays — falling back to normal flow')

		# For multi-model, aggregate ALL model trays so excess_qty is computed globally
		if multi_model_flag and secondary_lots:
			if excess_primary_trays:
				# Excess lot + multi-model: use excess trays for primary, fetch normally for secondary
				trays = list(excess_primary_trays)
				seen_lot_ids = {lot_id}
				for t in trays:
					t['source_lot_id'] = lot_id
				for sec in secondary_lots:
					sec_lot_id = sec.get('lot_id')
					if sec_lot_id and sec_lot_id not in seen_lot_ids:
						sec_trays = fetch_trays_for_lot(sec_lot_id)
						for t in sec_trays:
							t['source_lot_id'] = sec_lot_id
						trays.extend(sec_trays)
						seen_lot_ids.add(sec_lot_id)
				logging.info(f'[AGGREGATE_EXCESS] Excess primary + {len(seen_lot_ids)-1} secondary lot(s), total {len(trays)} trays, lot_qty={lot_qty}')
			else:
				trays = aggregate_multi_model_trays(lot_id, secondary_lots, lot_qty)
		else:
			if excess_primary_trays:
				trays = excess_primary_trays
				logging.info(f'[INIT_EXCESS_LOT] Single-model excess — using {len(trays)} half_filled tray(s), qty={lot_qty}')
			else:
				trays = fetch_trays_for_lot(lot_id)

		# ===== EXCESS LOT DETECTION FALLBACK (auto-detect for single-model without frontend flag) =====
		# Skip if lot_id is already an EX-* excess lot (trays already resolved from ExcessLotTray)
		if not (multi_model_flag and secondary_lots) and not excess_primary_trays and not is_excess_by_lot_id:
			try:
				submitted_jig = JigCompleted.objects.filter(
					batch_id=batch_id,
					draft_status='submitted',
					half_filled_tray_qty__gt=0
				).first()
				if submitted_jig:
					hf_info = submitted_jig.half_filled_tray_info or []
					if hf_info:
						trays = [
							{
								'tray_id': t['tray_id'],
								'qty': int(t.get('qty') or 0),
								'top_tray': bool(t.get('is_top_half_filled', False)),
							}
							for t in hf_info if isinstance(t, dict) and t.get('tray_id')
						]
						lot_qty = submitted_jig.half_filled_tray_qty or submitted_jig.excess_qty or 0
						logging.info(f'[INIT_EXCESS_LOT] Batch {batch_id} auto-detected — using half_filled tray info: {len(trays)} tray(s), qty={lot_qty}')
			except Exception:
				logging.exception('[INIT_EXCESS_LOT] Failed to check submitted JigCompleted — continuing with normal flow')

		# 3. Core computation — single source of truth
		computed = compute_jig_loading(trays, jig_capacity, broken_hooks, tray_capacity)

		# 4. Separate PLANNED allocation from ACTUAL loaded (scanned) qty.
		# planned_loaded = what the plan says (for Add Model enable/disable)
		# loaded_cases_qty = what user has actually scanned (0 on fresh init)
		planned_loaded_cases_qty = computed['delink_tray_qty']
		planned_empty_hooks = max(0, computed['effective_capacity'] - planned_loaded_cases_qty)
		# Fresh init: nothing scanned yet → loaded=0, empty=full capacity
		loaded_cases_qty = 0
		empty_hooks = computed['effective_capacity']
		# Adjust empty_hooks based on total lot qty (remaining capacity after planned allocation)
		total_lot_qty_for_empty = lot_qty
		empty_hooks = max(0, empty_hooks - total_lot_qty_for_empty)

		# 5. Multi-model allocation
		multi_model_allocation = []
		half_filled_tray_info = computed.get('half_filled_tray_info', {})
		half_filled_tray_qty = computed.get('half_filled_tray_qty', 0)
		ui_delink_tray_info = []
		tray_distribution = []
		models_list = []
		total_multi_model_qty = lot_qty

		if multi_model_flag and secondary_lots:
			mm_result = self._handle_multi_model(
				lot_id, batch_id, lot_qty, secondary_lots,
				computed['effective_capacity'], tray_capacity
			)
			multi_model_allocation = mm_result['allocation']
			# Convert old list format to new dict format
			half_filled_tray_info = _half_filled_list_to_dict(mm_result['half_filled'], mm_result['half_filled_qty'])
			half_filled_tray_qty = mm_result['half_filled_qty']
			total_multi_model_qty = mm_result['total_qty']
			ui_delink_tray_info = mm_result['ui_delink']
			tray_distribution = mm_result.get('tray_distribution', [])
			models_list = mm_result.get('models', [])
			# planned allocation from total multi-model allocation (for Add Model enable/disable)
			planned_loaded_cases_qty = min(total_multi_model_qty, computed['effective_capacity'])
			planned_empty_hooks = max(0, computed['effective_capacity'] - planned_loaded_cases_qty)
			# Fresh init: nothing scanned yet → loaded=0, empty=full capacity
			loaded_cases_qty = 0
			empty_hooks = computed['effective_capacity']
			# Adjust empty_hooks based on total lot qty (remaining capacity after planned allocation)
			total_lot_qty_for_empty = total_multi_model_qty
			empty_hooks = max(0, empty_hooks - total_lot_qty_for_empty)

		# 6. Detect PERFECT_FIT — for multi-model, compare total allocation vs capacity
		if multi_model_flag and secondary_lots:
			hf_exists = half_filled_tray_info.get('exists', False) if isinstance(half_filled_tray_info, dict) else bool(half_filled_tray_info)
			is_perfect_fit = (total_multi_model_qty == computed['effective_capacity']) and (broken_hooks == 0) and (not hf_exists)
		else:
			is_perfect_fit = (lot_qty == jig_capacity) and (broken_hooks == 0)

		# 6b. Multi-model excess: total requested across ALL models minus effective capacity
		if multi_model_flag and secondary_lots:
			total_all_models_requested = lot_qty + sum(int(s.get('qty', 0) or 0) for s in secondary_lots)
			multi_model_excess = max(0, total_all_models_requested - computed['effective_capacity'])
		else:
			multi_model_excess = None  # use single-model excess

		# ✅ BUG FIX #4/#5: If scanned_trays provided (BH recalc), compute loaded/delink_completed
		scanned_trays = payload.get('scanned_trays', [])
		delink_completed_flag = False
		if scanned_trays:
			scanned_ids = set(s.get('tray_id', '') for s in scanned_trays if s.get('tray_id'))
			if multi_model_flag and multi_model_allocation:
				delink_plan = {}
				for m_alloc in multi_model_allocation:
					for t in m_alloc.get('tray_info', []):
						delink_plan[t.get('tray_id', '')] = t.get('qty', 0)
			else:
				delink_plan = {dt['tray_id']: dt['qty'] for dt in computed['delink_tray_info']}
			loaded_cases_qty = sum(delink_plan.get(sid, 0) for sid in scanned_ids)
			empty_hooks = max(0, computed['effective_capacity'] - loaded_cases_qty)
			# planned_empty_hooks stays based on planned allocation (already computed above)
			delink_count = len(delink_plan)
			delink_completed_flag = len(scanned_ids) >= delink_count and delink_count > 0
			if delink_completed_flag and isinstance(half_filled_tray_info, dict) and half_filled_tray_info.get('exists') and not half_filled_tray_info.get('tray_ids'):
				excess_trays_for_hf = computed.get('excess_info', {}).get('excess_trays', [])
				half_filled_tray_info = assign_half_filled_tray_ids(
					half_filled_tray_info, computed['delink_tray_info'],
					excess_trays_for_hf, tray_capacity
				)
			logging.info(f'[INIT_BH_RECALC] scanned={len(scanned_ids)}, loaded={loaded_cases_qty}, delink_completed={delink_completed_flag}')

		# 7a. Build unified tray table (single source of truth for delink + excess UI)
		# scanned_tray_ids: everything the frontend currently holds as scanned, used only
		# to auto-verify the single partial-delink row once its paired Excess Top Tray
		# (same physical tray_id) has been scanned — see build_split_panel_data.
		_scanned_tray_ids_upper = set(
			str(s.get('tray_id', '')).strip().upper() for s in scanned_trays if s.get('tray_id')
		)
		if multi_model_flag and multi_model_allocation:
			unified_tray_table = build_unified_tray_table_multi_model(
				multi_model_allocation, computed, lot_qty, jig_capacity, tray_capacity
			)
			split_panel = build_split_panel_data_multi_model(
				multi_model_allocation, computed, lot_qty, jig_capacity, tray_capacity,
				scanned_tray_ids=_scanned_tray_ids_upper
			)
		else:
			model_label = lot_data.get('model_image_label', '') if isinstance(lot_data, dict) else ''
			unified_tray_table = build_unified_tray_table(
				computed, lot_qty, jig_capacity, model_label, tray_capacity
			)
			split_panel = build_split_panel_data(
				computed, lot_qty, jig_capacity, model_label, tray_capacity,
				scanned_tray_ids=_scanned_tray_ids_upper
			)

		# 7. Build unified response
		response = {
			'lot_id': lot_id,
			'batch_id': batch_id,
			'lot_qty': lot_qty,
			'total_qty': computed['total_qty'],
			'tray_count': computed['tray_count'],
			'jig_capacity': jig_capacity,
			'original_capacity': jig_capacity,
			'broken_hooks': broken_hooks,
			'placeholders': {
				'jig_id': f'J{jig_capacity:03d}-0000',
				'tray_scan': 'Scan or enter tray ID',
			},
			'effective_capacity': computed['effective_capacity'],
			'loaded_cases_qty': loaded_cases_qty,
			'empty_hooks': empty_hooks,
			'excess_qty': multi_model_excess if multi_model_excess is not None else computed['excess_qty'],
			'trays': trays,
			'delink_tray_info': computed['delink_tray_info'],
			'delink_trays': computed['delink_tray_info'],
			'delink_tray_qty': computed['delink_tray_qty'],
			'excess_info': computed['excess_info'],
			'scenario': 'PERFECT_FIT' if is_perfect_fit else '',
			'model_image_url': lot_data['model_image_url'],
			'model_image_label': lot_data['model_image_label'],
			'plating_stock_num': lot_data['model_image_label'],
			'nickel_bath_type': lot_data['nickel_bath_type'],
			'tray_type': lot_data['tray_type'],
			'tray_capacity': tray_capacity,
			'is_multi_model': bool(multi_model_flag),
			'total_multi_model_qty': total_multi_model_qty,
			'multi_model_allocation': multi_model_allocation,
			'secondary_lots': secondary_lots,
			'ui_delink_tray_info': ui_delink_tray_info,
			'tray_distribution': tray_distribution,
			'models': models_list,
			'half_filled_tray_info': half_filled_tray_info,
			'half_filled': half_filled_tray_info,
			'half_filled_tray_qty': half_filled_tray_qty,
			'delink_completed': delink_completed_flag,
			'validation': computed['validation'],
			'planned_empty_hooks': planned_empty_hooks,
			'bh_editable': True,
			'can_submit': loaded_cases_qty > 0 and loaded_cases_qty >= computed['effective_capacity'],
			'unified_tray_table': unified_tray_table,
			'split_panel': split_panel,
			# Legacy 'draft' key for frontend backward compatibility
			'draft': {
				'batch_id': batch_id,
				'lot_id': lot_id,
				'original_lot_qty': lot_qty,
				'jig_capacity': jig_capacity,
				'effective_capacity': computed['effective_capacity'],
				'loaded_cases_qty': loaded_cases_qty,
				'delink_tray_info': computed['delink_tray_info'],
				'delink_tray_qty': computed['delink_tray_qty'],
				'excess_qty': multi_model_excess if multi_model_excess is not None else computed['excess_qty'],
				'broken_hooks': broken_hooks,
				'model_image_url': lot_data['model_image_url'],
				'model_image_label': lot_data['model_image_label'],
				'plating_stock_num': lot_data['model_image_label'],
				'nickel_bath_type': lot_data['nickel_bath_type'],
				'tray_type': lot_data['tray_type'],
				'tray_capacity': tray_capacity,
				'is_multi_model': bool(multi_model_flag),
				'total_multi_model_qty': total_multi_model_qty,
				'draft_data': {'primary_lot': lot_id, 'secondary_lots': secondary_lots},
				'secondary_lots': secondary_lots,
			},
		}
		return Response(response)

	def _handle_multi_model(self, primary_lot_id, primary_batch_id, primary_lot_qty,
							secondary_lots, effective_capacity, tray_capacity):
		"""Handle multi-model tray allocation across primary + secondary models."""
		used_tray_ids = set()
		allocation = []
		half_filled_tray_info = []
		half_filled_tray_qty = 0

		# Primary model
		try:
			primary_result = allocate_trays_for_model(primary_lot_id, primary_lot_qty, effective_capacity, used_tray_ids)
			used_tray_ids.update(primary_result['allocated_tray_ids'])
			primary_img = fetch_model_image_metadata(primary_lot_id, primary_batch_id)
			primary_model_name = fetch_model_metadata(primary_lot_id, primary_batch_id)
			# Ensure model_image_label is NEVER empty — fall back to model_name
			primary_image_label = primary_img['model_image_label'] or primary_model_name or f'Model-{primary_lot_id}'
			allocation.append({
				'model': primary_model_name,
				'model_name': primary_model_name,
				'model_role': 'primary', 'lot_id': primary_lot_id, 'batch_id': primary_batch_id,
				'sequence': 0, 'requested_qty': primary_lot_qty,
				'allocated_qty': primary_result['allocated_qty'],
				'tray_info': primary_result['tray_info'],
				'model_image_url': primary_img['model_image_url'],
				'model_image_label': primary_image_label,
				# Backend-controlled rendering metadata
				'model_index': 1,
				'color_class': 'model-bg-1',
				'display_name': 'Model 1',
			})
		except Exception as e:
			logging.exception(f'Multi-model primary allocation failed: {e}')

		# Secondary models
		for seq, sec in enumerate(secondary_lots, start=1):
			try:
				sec_lot_id = sec.get('lot_id')
				sec_batch_id = sec.get('batch_id')
				sec_lot_qty = int(sec.get('qty', 0) or 0)
				if not sec_lot_id:
					continue
				capacity_used = sum(m['allocated_qty'] for m in allocation)
				capacity_remaining = max(0, effective_capacity - capacity_used)
				allowed_qty = min(sec_lot_qty, capacity_remaining)
				excess_for_model = max(0, sec_lot_qty - allowed_qty)

				secondary_result = allocate_trays_for_model(sec_lot_id, allowed_qty, capacity_remaining, used_tray_ids)
				used_tray_ids.update(secondary_result['allocated_tray_ids'])
				sec_img = fetch_model_image_metadata(sec_lot_id, sec_batch_id)
				sec_model_name = fetch_model_metadata(sec_lot_id, sec_batch_id)
				# Ensure model_image_label is NEVER empty — fall back to model_name
				sec_image_label = sec_img['model_image_label'] or sec_model_name or f'Model-{sec_lot_id}'
				model_idx = len(allocation) + 1  # 1-based index (primary=1, first secondary=2, etc.)
				# ✅ Modulo cycling so Model 6+ wraps back to model-bg-1 (only 5 CSS classes exist)
				normalized_color_idx = ((model_idx - 1) % 5) + 1
				allocation.append({
					'model': sec_model_name, 'model_name': sec_model_name,
					'model_role': 'secondary', 'lot_id': sec_lot_id, 'batch_id': sec_batch_id,
					'sequence': seq, 'requested_qty': sec_lot_qty,
					'allocated_qty': secondary_result['allocated_qty'],
					'tray_info': secondary_result['tray_info'],
					'model_image_url': sec_img['model_image_url'],
					'model_image_label': sec_image_label,
					# Backend-controlled rendering metadata — cycled so no out-of-range classes
					'model_index': model_idx,
					'color_class': f'model-bg-{normalized_color_idx}',
					'display_name': f'Model {model_idx}',
				})

				# Excess handling → half-filled trays
				if excess_for_model > 0:
					excess_remaining = excess_for_model
					if secondary_result['tray_info']:
						last_alloc = secondary_result['tray_info'][-1]
						try:
							_all_sec_trays2 = fetch_trays_for_lot(sec_lot_id)
							orig_tray = next((t for t in _all_sec_trays2 if t.get('tray_id') == last_alloc['tray_id']), None)
							if orig_tray:
								orig_qty = int(orig_tray.get('qty', 0) or 0)
								if last_alloc['qty'] < orig_qty:
									partial_rem = orig_qty - last_alloc['qty']
									hf_qty = min(partial_rem, excess_remaining)
									half_filled_tray_info.append({'tray_id': last_alloc['tray_id'], 'qty': hf_qty, 'model': sec_model_name})
									excess_remaining -= hf_qty
									half_filled_tray_qty += hf_qty
						except Exception:
							pass
					if excess_remaining > 0:
						try:
							for tray_item in fetch_trays_for_lot(sec_lot_id):
								if excess_remaining <= 0:
									break
								tid = tray_item.get('tray_id', '')
								if tid in used_tray_ids:
									continue
								tq = int(tray_item.get('qty', 0) or 0)
								hf_qty = min(tq, excess_remaining)
								half_filled_tray_info.append({'tray_id': tid, 'qty': hf_qty, 'model': sec_model_name})
								excess_remaining -= hf_qty
								half_filled_tray_qty += hf_qty
								used_tray_ids.add(tid)
						except Exception:
							pass
			except Exception as e:
				logging.exception(f'Multi-model secondary allocation failed: {e}')
				continue

		# Build flattened UI delink tray info (with backend-controlled color/display)
		ui_delink = []
		for m_alloc in allocation:
			m_color = m_alloc.get('color_class', '')
			m_display = m_alloc.get('display_name', '')
			m_index = m_alloc.get('model_index', 0)
			for t in m_alloc.get('tray_info', []):
				ui_delink.append({
					'tray_id': t.get('tray_id', ''), 'qty': t.get('qty', 0),
					'top_tray': False, 'is_partial': False,
					'model': m_alloc.get('model', ''), 'model_role': m_alloc.get('model_role', ''),
					'lot_id': m_alloc.get('lot_id', ''), 'batch_id': m_alloc.get('batch_id', ''),
					'color_class': m_color, 'display_name': m_display, 'model_index': m_index,
				})

		# Unified half-filled fix
		total_requested = primary_lot_qty + sum(int(s.get('qty', 0) or 0) for s in secondary_lots)
		if total_requested > effective_capacity and not half_filled_tray_info:
			overflow = total_requested - effective_capacity
			tc = tray_capacity or 12
			while overflow > 0:
				fill = min(tc, overflow)
				half_filled_tray_info.append({'tray_id': None, 'qty': fill, 'model': 'Overflow'})
				overflow -= fill
			half_filled_tray_qty = sum(t['qty'] for t in half_filled_tray_info)

		# Build unified tray_distribution: delink trays + half-filled trays merged
		tray_distribution = list(ui_delink)
		for hf in half_filled_tray_info:
			tray_distribution.append({
				'tray_id': hf.get('tray_id'), 'qty': hf.get('qty', 0),
				'top_tray': False, 'is_partial': True,
				'model': hf.get('model', ''), 'model_role': 'half_filled',
				'lot_id': '', 'batch_id': '',
				'color_class': 'half-filled', 'display_name': 'Half Filled',
				'model_index': 0,
			})

		# Build models summary list for frontend (backend-controlled model identity)
		models_summary = []
		for m_alloc in allocation:
			models_summary.append({
				'model_index': m_alloc.get('model_index', 0),
				'display_name': m_alloc.get('display_name', ''),
				'color_class': m_alloc.get('color_class', ''),
				'model_no': m_alloc.get('model_image_label', ''),
				'model_image_url': m_alloc.get('model_image_url', ''),
				'model_role': m_alloc.get('model_role', ''),
				'lot_id': m_alloc.get('lot_id', ''),
				'batch_id': m_alloc.get('batch_id', ''),
				'qty': m_alloc.get('requested_qty', 0),
				'allocated_qty': m_alloc.get('allocated_qty', 0),
			})

		return {
			'allocation': allocation, 'half_filled': half_filled_tray_info,
			'half_filled_qty': half_filled_tray_qty,
			'total_qty': sum(int(m.get('requested_qty', 0) or 0) for m in allocation),
			'ui_delink': ui_delink,
			'tray_distribution': tray_distribution,
			'models': models_summary,
		}

class JigLoadUpdateAPI(APIView):
	"""POST /api/jig/load/update/ — Unified update API.
	Handles: scan_tray, unscan_tray, update_broken_hooks, save_draft.
	Always returns full recalculated state from compute_jig_loading."""
	permission_classes = [IsAuthenticated]

	def post(self, request):
		payload = request.data
		lot_id = payload.get('lot_id')
		batch_id = payload.get('batch_id')
		action = payload.get('action', 'scan_tray')
		tray_id = payload.get('tray_id')
		broken_hooks = int(payload.get('broken_hooks', 0) or 0)
		jig_capacity_override = payload.get('jig_capacity')
		scanned_trays = payload.get('scanned_trays', [])
		multi_model_flag = payload.get('multi_model', False)
		secondary_lots = payload.get('secondary_lots', [])
		primary_lot_id = payload.get('primary_lot_id', lot_id)
		primary_batch_id = payload.get('primary_batch_id', batch_id)
		scan_panel = str(payload.get('scan_panel', 'delink') or 'delink').strip().lower()

		if isinstance(multi_model_flag, str):
			multi_model_flag = multi_model_flag.lower() in ('1', 'true', 'yes', 'on')
		if isinstance(secondary_lots, str):
			try:
				secondary_lots = json.loads(secondary_lots)
			except json.JSONDecodeError:
				secondary_lots = []
		if not isinstance(secondary_lots, list):
			secondary_lots = []
		if multi_model_flag:
			secondary_lots = resolve_secondary_lots(secondary_lots)

		state_lot_id = primary_lot_id if multi_model_flag and primary_lot_id else lot_id
		state_batch_id = primary_batch_id if multi_model_flag and primary_batch_id else batch_id

		if not lot_id or not batch_id:
			return Response({'error': 'lot_id and batch_id are required'}, status=status.HTTP_400_BAD_REQUEST)

		logging.info(json.dumps({'event': 'JIG_LOAD_UPDATE', 'lot_id': lot_id, 'action': action, 'tray_id': tray_id, 'multi_model': bool(multi_model_flag)}))

		# Validate tray scan if requested
		scan_result = None
		if action == 'scan_tray' and tray_id:
			allow_reuse_delink = bool(payload.get('allow_reuse_delink', False))
			allow_new_half_filled = bool(payload.get('allow_new_half_filled', False))
			already_scanned = set(s.get('tray_id', '') for s in scanned_trays if s.get('tray_id'))

			# ===== EXCESS-SECTION DUPLICATE GUARD =====
			# allow_reuse_delink intentionally lets a tray already scanned into the
			# DELINK panel be reused for its EXCESS portion (the same physical tray
			# split across delink + excess for the partial/split-tray case). It must
			# NOT also let the SAME tray be scanned into two different EXCESS rows —
			# each excess row is a physically distinct tray, so within the Excess
			# section duplicates are always rejected. Excludes delink-scanned ids from
			# the check (crossing FROM delink INTO excess remains the allowed case).
			if scan_panel in ('excess', 'excess_top'):
				_delink_scanned_payload = payload.get('delink_scanned_trays', None)
				if _delink_scanned_payload is not None:
					_delink_ids = set(s.get('tray_id', '') for s in _delink_scanned_payload if s.get('tray_id'))
					duplicate_check_ids = already_scanned - _delink_ids
				else:
					duplicate_check_ids = already_scanned
				effective_allow_reuse = False
			else:
				duplicate_check_ids = already_scanned
				effective_allow_reuse = allow_reuse_delink

			candidate_lot_ids = []
			if scan_panel in ('excess', 'excess_top'):
				# Excess/top panel rows carry the exact source lot. Do not search other
				# models, or a tray from another model can be accepted accidentally.
				if lot_id:
					candidate_lot_ids.append(lot_id)
			else:
				for candidate in [lot_id, primary_lot_id] + [sec.get('lot_id') for sec in secondary_lots if isinstance(sec, dict)]:
					if candidate and candidate not in candidate_lot_ids:
						candidate_lot_ids.append(candidate)

			is_valid = False
			tray_qty = 0
			validation_status = 'invalid_tray'
			message = 'Invalid tray or wrong lot'
			validated_lot_id = lot_id
			for candidate_lot_id in candidate_lot_ids:
				is_valid, tray_qty, validation_status, message = validate_tray_for_scan(
					tray_id,
					candidate_lot_id,
					duplicate_check_ids,
					allow_reuse_delink=effective_allow_reuse,
					allow_new_half_filled=allow_new_half_filled,
					strict_excess_lot=scan_panel in ('excess', 'excess_top'),
				)
				if is_valid:
					validated_lot_id = candidate_lot_id
					break
				if validation_status != 'invalid_tray':
					break
			if not is_valid and scan_panel in ('excess', 'excess_top') and validation_status == 'invalid_tray':
				message = 'Tray does not belong to this excess lot/model'
			if is_valid and validated_lot_id != lot_id:
				logging.info(f'[UPDATE_MM_SCAN_CONTEXT] tray={tray_id} posted_lot={lot_id} validated_lot={validated_lot_id}')
			scan_result = {
				'validation_status': validation_status,
				'message': message,
				'tray_id': tray_id,
				'tray_qty': tray_qty,
				'validated_lot_id': validated_lot_id,
			}
			if not is_valid:
				return Response(scan_result, status=status.HTTP_400_BAD_REQUEST if validation_status != 'error' else status.HTTP_500_INTERNAL_SERVER_ERROR)

		# Handle unscan: just acknowledge and recalculate with updated scanned list
		if action == 'unscan_tray' and tray_id:
			# Client sends updated scanned_trays list AFTER removing the tray
			scan_result = {
				'validation_status': 'unscan_success',
				'message': f'Tray {tray_id} removed',
				'tray_id': tray_id,
				'tray_qty': 0,
			}

		# Handle clear: FULL RESET — delete WORKING-STATE drafts, zero all state
		# Only delete 'active' (auto-created during scanning), NOT explicit 'draft' (user clicked Draft)
		if action == 'clear':
			broken_hooks = 0
			scanned_trays = []
			try:
				JigCompleted.objects.filter(
					batch_id=state_batch_id, lot_id=state_lot_id, user=request.user, draft_status='active'
				).delete()
				logging.info(f'[CLEAR] Working-state draft deleted for lot={state_lot_id}, batch={state_batch_id}')
			except Exception:
				logging.exception('JigLoadUpdateAPI: clear draft delete failed')

		# Fetch data and recompute full state (SINGLE SOURCE OF TRUTH)
		# Multi-model: use PRIMARY lot for capacity/metadata, aggregate trays from ALL lots
		if multi_model_flag and secondary_lots:
			lot_data = fetch_lot_data(primary_lot_id, primary_batch_id, jig_capacity_override)
			trays = aggregate_multi_model_trays(primary_lot_id, secondary_lots, lot_data['lot_qty'])
			logging.info(f"[UPDATE_MM] Aggregated {len(trays)} trays from primary={primary_lot_id} + {len(secondary_lots)} secondary lots")
		else:
			lot_data = fetch_lot_data(lot_id, batch_id, jig_capacity_override)
			trays = fetch_trays_for_lot(lot_id)
		computed = compute_jig_loading(trays, lot_data['jig_capacity'], broken_hooks, lot_data['tray_capacity'])

		# ===== LOADED QTY: derive from DELINK PLAN, not frontend-sent qty =====
		# Build set of ALL scanned tray IDs (used for delink_completed check)
		scanned_ids = set(s.get('tray_id', '') for s in scanned_trays if s.get('tray_id'))
		if action == 'scan_tray' and scan_result and scan_result['validation_status'] == 'success':
			scanned_ids.add(tray_id)
		# Sum PLANNED qty from delink_tray_info for each scanned tray (not raw DB qty)
		delink_plan = {dt['tray_id']: dt['qty'] for dt in computed['delink_tray_info']}
		# STRICT RULE: loaded_cases_qty = DELINK-panel scans ONLY (not excess/top-tray scans).
		# Frontend sends delink_scanned_trays (delink panel only) and scan_panel to distinguish.
		delink_scanned_trays_payload = payload.get('delink_scanned_trays', None)
		if delink_scanned_trays_payload is not None:
			# Use explicit delink-only scan list
			delink_only_ids = set(s.get('tray_id', '') for s in delink_scanned_trays_payload if s.get('tray_id'))
			if action == 'scan_tray' and scan_result and scan_result['validation_status'] == 'success' and scan_panel == 'delink':
				delink_only_ids.add(tray_id)
			loaded_cases_qty = sum(delink_plan.get(sid, 0) for sid in delink_only_ids)
		else:
			# Legacy: filter all scanned against delink_plan
			loaded_cases_qty = sum(delink_plan.get(sid, 0) for sid in scanned_ids)

		# BUG FIX: Include partial/split tray delink_qty even when scanned from excess/top panel.
		# A partial tray has both delink_qty>0 and excess_qty>0 in all_trays.
		# Its delink_qty is ALWAYS part of the jig load, regardless of which panel it was scanned from.
		all_trays_data = computed.get('all_trays', [])
		all_trays_map = {at['tray_id']: at for at in all_trays_data}
		counted_ids = delink_only_ids if delink_scanned_trays_payload is not None else scanned_ids
		for sid in scanned_ids:
			if sid in counted_ids:
				continue  # Already counted from delink panel
			at = all_trays_map.get(sid)
			# Scoped to the true partial/split tray only (delink_qty>0 AND
			# excess_qty>0), matching the comment above — a plain full delink
			# tray (delink_qty>0, excess_qty==0) must never get credited just
			# because its id happens to appear in an excess-panel scan.
			if at and int(at.get('delink_qty', 0) or 0) > 0 and int(at.get('excess_qty', 0) or 0) > 0:
				loaded_cases_qty += int(at['delink_qty'])

		empty_hooks = max(0, computed['effective_capacity'] - loaded_cases_qty)

		# ===== DELINK COMPLETION: all delink trays scanned? =====
		delink_count = len(computed['delink_tray_info'])
		# Use delink_only_ids if available (more accurate), else fall back to all scanned_ids
		delink_check_ids = set(delink_only_ids if delink_scanned_trays_payload is not None else scanned_ids)
		# The partial-delink tray is never scanned into the Delink panel on its own —
		# it is auto-verified once its paired Excess Top Tray (same tray_id) is
		# scanned. Credit it toward delink completion the same way its qty is already
		# credited toward loaded_cases_qty above (see BUG FIX block). Identified from
		# computed['all_trays'] — the SAME source build_split_panel_data uses for its
		# 'is_partial' row — not computed['delink_tray_info']['is_capacity_split'],
		# which is derived pre-broken-hooks and can name a different tray once BH > 0.
		_split_tray_id = next(
			(at['tray_id'] for at in all_trays_data
			 if int(at.get('delink_qty', 0) or 0) > 0 and int(at.get('excess_qty', 0) or 0) > 0),
			None
		)
		if _split_tray_id and _split_tray_id in scanned_ids:
			delink_check_ids.add(_split_tray_id)
		delink_completed = len(delink_check_ids) >= delink_count and delink_count > 0

		# ===== HALF-FILLED TRAY IDs: only assigned when delink is COMPLETE =====
		half_filled = computed.get('half_filled_tray_info', {})
		if delink_completed and isinstance(half_filled, dict) and half_filled.get('exists'):
			excess_trays = computed.get('excess_info', {}).get('excess_trays', [])
			half_filled = assign_half_filled_tray_ids(
				half_filled, computed['delink_tray_info'],
				excess_trays, lot_data['tray_capacity']
			)

		# NOTE: JigLoadUpdateAPI is READ/COMPUTE ONLY — it must never write to JigCompleted.
		# A record is persisted ONLY when the user explicitly clicks Draft or Submit
		# (see JigSaveAPI below). Scanning trays, updating broken hooks, or opening the
		# Add Model flow must never silently create/update a JigCompleted row.

		# Compute total_multi_model_qty for multi-model
		if multi_model_flag and secondary_lots:
			# Total = primary lot_qty + sum of all secondary qtys
			_primary_qty = lot_data['lot_qty']
			_secondary_total = sum(int(s.get('qty', 0) or 0) for s in secondary_lots)
			total_multi_model_qty = _primary_qty + _secondary_total
			# Multi-model excess: total requested across ALL models minus effective capacity
			mm_excess_qty = max(0, total_multi_model_qty - computed['effective_capacity'])
			logging.info(f"[UPDATE_MM] total_multi_model_qty={total_multi_model_qty}, mm_excess={mm_excess_qty}")
		else:
			total_multi_model_qty = lot_data['lot_qty']
			mm_excess_qty = computed['excess_qty']

		# planned_empty_hooks: based on planned allocation (for Add Model enable/disable)
		if multi_model_flag and secondary_lots:
			planned_loaded = min(total_multi_model_qty, computed['effective_capacity'])
		else:
			planned_loaded = computed['delink_tray_qty']
		planned_empty_hooks = max(0, computed['effective_capacity'] - planned_loaded)

		# Build multi-model allocation + unified tray table for backend-driven FE rendering
		multi_model_allocation = []
		if multi_model_flag and secondary_lots:
			try:
				mm_result = JigLoadInitAPI()._handle_multi_model(
					primary_lot_id,
					primary_batch_id,
					lot_data['lot_qty'],
					secondary_lots,
					computed['effective_capacity'],
					lot_data['tray_capacity']
				)
				multi_model_allocation = mm_result.get('allocation', [])
			except Exception:
				logging.exception('JigLoadUpdateAPI: multi-model allocation build failed')
				multi_model_allocation = []

		# scanned_tray_ids: every tray_id confirmed scanned in THIS request (across any
		# panel) — used only to auto-verify the partial-delink row once its paired
		# Excess Top Tray (same physical tray_id) is scanned. See build_split_panel_data.
		_scanned_tray_ids_upper = set(str(sid or '').strip().upper() for sid in scanned_ids)
		if multi_model_flag and multi_model_allocation:
			unified_tray_table = build_unified_tray_table_multi_model(
				multi_model_allocation,
				computed,
				lot_data['lot_qty'],
				lot_data['jig_capacity'],
				lot_data['tray_capacity']
			)
			split_panel = build_split_panel_data_multi_model(
				multi_model_allocation,
				computed,
				lot_data['lot_qty'],
				lot_data['jig_capacity'],
				lot_data['tray_capacity'],
				scanned_tray_ids=_scanned_tray_ids_upper
			)
		else:
			unified_tray_table = build_unified_tray_table(
				computed,
				lot_data['lot_qty'],
				lot_data['jig_capacity'],
				lot_data.get('model_image_label', ''),
				lot_data['tray_capacity']
			)
			split_panel = build_split_panel_data(
				computed,
				lot_data['lot_qty'],
				lot_data['jig_capacity'],
				lot_data.get('model_image_label', ''),
				lot_data['tray_capacity'],
				scanned_tray_ids=_scanned_tray_ids_upper
			)

		logging.info(json.dumps({
			'event': 'JIG_LOAD_UPDATE_UNIFIED_TABLE',
			'lot_id': state_lot_id,
			'batch_id': state_batch_id,
			'action': action,
			'rows': len(unified_tray_table),
			'is_multi_model': bool(multi_model_flag),
			'model_count': len(multi_model_allocation) if multi_model_allocation else 1,
		}))

		response = {
			'lot_id': state_lot_id,
			'batch_id': state_batch_id,
			'lot_qty': lot_data['lot_qty'],
			'total_qty': computed['total_qty'],
			'total_multi_model_qty': total_multi_model_qty,
			'tray_count': computed['tray_count'],
			'jig_capacity': lot_data['jig_capacity'],
			'original_capacity': lot_data['jig_capacity'],
			'broken_hooks': broken_hooks,
			'placeholders': {
				'jig_id': 'J{:03d}-0000'.format(int(lot_data.get('jig_capacity', 0) or 0)),
				'tray_scan': 'Scan or enter tray ID',
			},
			'effective_capacity': computed['effective_capacity'],
			'loaded_cases_qty': loaded_cases_qty,
			'empty_hooks': empty_hooks,
			'delink_tray_info': computed['delink_tray_info'],
			'delink_trays': computed['delink_tray_info'],
			'delink_tray_qty': computed['delink_tray_qty'],
			'excess_info': computed['excess_info'],
			'excess_qty': mm_excess_qty,
			'half_filled_tray_info': half_filled,
			'half_filled': half_filled,
			'half_filled_tray_qty': computed.get('half_filled_tray_qty', 0),
			'delink_completed': delink_completed,
			'tray_capacity': lot_data['tray_capacity'],
			'model_image_url': lot_data['model_image_url'],
			'model_image_label': lot_data['model_image_label'],
			'nickel_bath_type': lot_data['nickel_bath_type'],
			'tray_type': lot_data['tray_type'],
			'is_multi_model': bool(multi_model_flag),
			'multi_model_allocation': multi_model_allocation,
			'secondary_lots': secondary_lots,
			'unified_tray_table': unified_tray_table,
			'split_panel': split_panel,
			'validation': computed['validation'],
			'planned_empty_hooks': planned_empty_hooks,
			'bh_editable': True,
		}
		if scan_result:
			response.update(scan_result)
		return Response(response)


# JigLoadSubmitAPI REMOVED — all submit logic consolidated into JigSaveAPI (POST /api/jig/save/)
# JigSaveAPI handles both draft (action="draft") and submit (action="submit") in a single endpoint.
# Jig Loading - Complete Table View
@method_decorator(login_required, name='dispatch')
class JigCompletedTable(TemplateView):
	"""Completed jigs table — displays all submitted/finalized jigs."""
	template_name = "JigLoading/Jig_CompletedTable.html"

	def get_context_data(self, **kwargs):
		import time as _time
		from datetime import datetime
		_t0 = _time.time()
		context = super().get_context_data(**kwargs)
		
		# Extract date filter parameters from request
		from_date_str = self.request.GET.get('from_date', '')
		to_date_str = self.request.GET.get('to_date', '')
		
		# Build base query with draft_status='submitted'
		query = JigCompleted.objects.filter(
			draft_status='submitted'
		).select_related('user')
		
		# Apply date filters if provided
		if from_date_str:
			try:
				from_date = datetime.strptime(from_date_str, '%Y-%m-%d').date()
				query = query.filter(updated_at__date__gte=from_date)
				context['from_date'] = from_date_str
			except (ValueError, TypeError):
				context['from_date'] = ''
		else:
			context['from_date'] = ''
		
		if to_date_str:
			try:
				to_date = datetime.strptime(to_date_str, '%Y-%m-%d').date()
				query = query.filter(updated_at__date__lte=to_date)
				context['to_date'] = to_date_str
			except (ValueError, TypeError):
				context['to_date'] = ''
		else:
			context['to_date'] = ''
		
		# Fetch records with date filter applied
		jig_completed_records = list(query.order_by('-updated_at')[:200])

		_t1 = _time.time()
		print(f"[JIG COMPLETED PERF] query: {_t1 - _t0:.3f}s ({len(jig_completed_records)} rows)")

		# Bulk prefetch TotalStockModel and ModelMasterCreation to eliminate N+1 queries
		all_lot_ids = list({rec.lot_id for rec in jig_completed_records if rec.lot_id})
		all_batch_ids = list({rec.batch_id for rec in jig_completed_records if rec.batch_id})

		stock_map = {}
		if all_lot_ids:
			for s in TotalStockModel.objects.filter(lot_id__in=all_lot_ids):
				stock_map[s.lot_id] = s

		batch_map = {}
		if all_batch_ids:
			for b in ModelMasterCreation.objects.filter(batch_id__in=all_batch_ids).select_related('location'):
				batch_map[b.batch_id] = b

		_t2 = _time.time()
		print(f"[JIG COMPLETED PERF] bulk prefetch: {_t2 - _t1:.3f}s")
		
		# Process each record and enrich with pre-fetched data
		jig_details = []
		for jig_rec in jig_completed_records:
			try:
				# Use bulk-prefetched data (no per-row DB query)
				stock_model = stock_map.get(jig_rec.lot_id)
				batch_obj = batch_map.get(jig_rec.batch_id)

				def _safe_int(value, default=0):
					try:
						if value is None or value == '':
							return default
						return int(value)
					except (TypeError, ValueError):
						return default

				def _completed_delink_tray_info():
					draft_data = jig_rec.draft_data if isinstance(jig_rec.draft_data, dict) else {}
					split_snapshot = draft_data.get('split_panel_snapshot')
					if not isinstance(split_snapshot, dict):
						return jig_rec.delink_tray_info or []

					delink_panel = split_snapshot.get('delink_panel') or {}
					delink_rows = delink_panel.get('trays') if isinstance(delink_panel, dict) else []
					if not isinstance(delink_rows, list) or not delink_rows:
						return jig_rec.delink_tray_info or []

					excess_panel = split_snapshot.get('excess_panel') or {}
					top_scan_value = ''
					if isinstance(excess_panel, dict):
						top_tray = excess_panel.get('top_tray') or {}
						if isinstance(top_tray, dict):
							top_scan_value = str(top_tray.get('scan_value') or '').strip()

					completed_rows = []
					for row in delink_rows:
						if not isinstance(row, dict):
							continue
						delink_qty = _safe_int(row.get('delink_qty'))
						excess_qty = _safe_int(row.get('excess_qty'))
						if delink_qty <= 0 and excess_qty <= 0:
							continue

						is_split_top_row = bool(row.get('is_partial')) and excess_qty > 0 and top_scan_value
						physical_tray_id = top_scan_value if is_split_top_row else str(
							row.get('scan_value') or row.get('tray_id') or ''
						).strip()
						if not physical_tray_id:
							continue

						original_qty = _safe_int(row.get('original_qty'))
						if original_qty <= 0:
							original_qty = delink_qty + excess_qty

						completed_rows.append({
							'tray_id': physical_tray_id,
							'top_tray': bool(row.get('is_top_tray') or is_split_top_row),
							'delink_qty': delink_qty,
							'excess_qty': excess_qty,
							'model_code': row.get('model_code', ''),
							'original_qty': original_qty,
							'source_lot_id': row.get('lot_id') or row.get('source_lot_id') or '',
						})

					if not completed_rows:
						return jig_rec.delink_tray_info or []
					top_rows = [row for row in completed_rows if row.get('top_tray')]
					non_top_rows = [row for row in completed_rows if not row.get('top_tray')]
					return top_rows + non_top_rows
				
				# Build multi-model allocation string as comma-separated model_name:qty
				# Template expects: "model1:qty1,model2:qty2,model3:qty3" for split(",") and get_model_name/get_model_qty filters
				no_of_model_cases_str = ''
				if jig_rec.is_multi_model and jig_rec.multi_model_allocation:
					try:
						models_list = []
						for m in jig_rec.multi_model_allocation:
							if isinstance(m, dict):
								model_name = m.get('model_name', m.get('model', ''))
								qty = m.get('allocated_qty', 0)
								if model_name:
									models_list.append(f"{model_name}:{qty}")
						no_of_model_cases_str = ','.join(models_list) if models_list else ''
					except Exception as e:
						logging.warning(f"Failed to process multi_model_allocation: {e}")
				
				# Extract plating stock no — for multi-model, combine all model names
				plating_stock_num = jig_rec.plating_stock_num or ''
				if jig_rec.is_multi_model and jig_rec.multi_model_allocation:
					try:
						plating_models = []
						for m in jig_rec.multi_model_allocation:
							if isinstance(m, dict):
								model_name = m.get('model_name', m.get('model', ''))
								if model_name:
									plating_models.append(model_name)
						if plating_models:
							plating_stock_num = ', '.join(plating_models)
					except Exception:
						pass
				
				# Build enriched record
				# Use delink_tray_qty as total_cases_loaded (the actual loaded qty, not the stale loaded_cases_qty)
				# Polishing Stk No: prefer ModelMasterCreation (batch), fallback TotalStockModel
				polishing_stk_no = getattr(batch_obj, 'polishing_stk_no', '') if batch_obj else ''
				if not polishing_stk_no:
					polishing_stk_no = getattr(stock_model, 'lot_polishing_stk_nos', '') if stock_model else ''
				# Plating color / polish finish: prefer batch, fallback stock
				p_color = getattr(batch_obj, 'plating_color', '') if batch_obj else ''
				if not p_color:
					p_color = getattr(stock_model, 'plating_color', '') if stock_model else ''
				p_finish = getattr(batch_obj, 'polish_finish', '') if batch_obj else ''
				if not p_finish:
					p_finish = getattr(stock_model, 'polish_finish', '') if stock_model else ''
				# Source / Location: from ModelMasterCreation.location
				source_location = ''
				if batch_obj and getattr(batch_obj, 'location', None):
					source_location = str(batch_obj.location)
				if not source_location:
					source_location = getattr(stock_model, 'lot_version_names', '') if stock_model else ''

				# Current Stage: prefer the live current_stage SSOT (modelmasterapp/stage_service.py)
				# so this matches every other module's Completed Table for the same lot, instead of
				# always showing the hardcoded "Jig Loading".
				current_stage_display = (
					getattr(stock_model, 'current_stage', None)
					or getattr(stock_model, 'last_process_module', None)
					or 'Jig Loading'
				) if stock_model else 'Jig Loading'

				# A Jig Loading completed row is not released while the lot is
				# still at Jig Loading.  It is released only when IP Inspection
				# records a real action and advances current_stage.
				lot_status = (
					'Yet to Release'
					if current_stage_display == 'Jig Loading'
					else 'Released'
				)

				enriched = {
					'id': jig_rec.id,
					'lot_id': jig_rec.lot_id,
					'batch_id': jig_rec.batch_id,
					'jig_id': jig_rec.jig_id,
					'jig_loaded_date_time': jig_rec.updated_at,
					'is_multi_model': jig_rec.is_multi_model,
					'no_of_model_cases': no_of_model_cases_str,
					'lot_plating_stk_nos': plating_stock_num,
					'lot_polishing_stk_nos': polishing_stk_no or 'N/A',
					'plating_color': p_color or 'N/A',
					'polish_finish': p_finish or 'N/A',
					'lot_version_names': source_location or 'N/A',
					'tray_type': jig_rec.tray_type or 'N/A',
					'tray_capacity': jig_rec.tray_capacity or 0,
					'calculated_no_of_trays': jig_rec.delink_tray_count or 0,
					'total_cases_loaded': jig_rec.delink_tray_qty or jig_rec.loaded_cases_qty or 0,
					'jig_type': 'Jig',
					'jig_capacity': jig_rec.jig_capacity or 0,
					'jig_qr_id': jig_rec.jig_id or '',
					'half_filled_tray_qty': jig_rec.half_filled_tray_qty or 0,
					'draft_status': jig_rec.draft_status,
					'original_lot_qty': jig_rec.original_lot_qty or 0,
					'delink_tray_info': json.dumps(_completed_delink_tray_info()),
					'half_filled_tray_info': json.dumps(jig_rec.half_filled_tray_info or []),
					'excess_qty': jig_rec.excess_qty or 0,
					'multi_model_allocation': jig_rec.multi_model_allocation or [],
					'IP_jig_pick_remarks': jig_rec.remarks or '',
					'current_stage_display': current_stage_display,
					'lot_status': lot_status,
					'type_of_input': get_type_of_input_for_batch(batch_obj),
				}
				jig_details.append(enriched)
				
			except Exception as e:
				logging.exception(f"Error processing JigCompleted record {jig_rec.id}: {e}")
				continue
		
		# Pagination
		from django.core.paginator import Paginator
		page_number = self.request.GET.get('page', 1)
		paginator = Paginator(jig_details, 10)  # 10 records per page
		page_obj = paginator.get_page(page_number)
		
		context['jig_details'] = page_obj
		context['page_obj'] = page_obj
		context['completed_list'] = jig_details  # Keep for backwards compatibility
		
		print(f"[JIG COMPLETED PERF] TOTAL: {_time.time() - _t0:.3f}s ({len(jig_details)} records)")
		
		return context


# =============================================================================
# MODEL COMBINATION VALIDATION API
# =============================================================================
class ModelCombinationValidateAPI(APIView):
	"""POST /api/model-combination/validate/
	Validate which models can be added alongside the already-selected models.
	Always returns HTTP 200 — errors are in the response body.

	Input:  { "selected_models": ["2617SAA02"] }
	Output: { "eligible_models": [...], "non_eligible_models": [...],
	          "blocked_lookalike_plating_stk_nos": [...],
	          "warnings": [...], "errors": [...] }
	"""
	permission_classes = [IsAuthenticated]

	def post(self, request):
		try:
			body = request.data if hasattr(request, 'data') else {}
			selected_models = body.get('selected_models', [])
			if not isinstance(selected_models, list):
				selected_models = [str(selected_models)] if selected_models else []

			logging.info(f'[MODEL_COMBINATION_VALIDATE] POST from user={request.user} selected_models={selected_models}')

			from .model_combination_validator import validate_model_combination
			result = validate_model_combination(selected_models)

			logging.info(f'[MODEL_COMBINATION_VALIDATE] eligible={len(result["eligible_models"])} '
				f'non_eligible={len(result["non_eligible_models"])} '
				f'blocked_lookalike={len(result["blocked_lookalike_plating_stk_nos"])} '
				f'errors={result["errors"]}')

			return Response(result, status=status.HTTP_200_OK)

		except Exception as e:
			logging.exception(f'[MODEL_COMBINATION_VALIDATE] Unhandled exception: {e}')
			return Response({
				'eligible_models': [],
				'non_eligible_models': [],
				'blocked_lookalike_plating_stk_nos': [],
				'warnings': [],
				'errors': [f'Validation error: {str(e)}'],
			}, status=status.HTTP_200_OK)


# =============================================================================
# SINGLE UNIFIED API — DRAFT + SUBMIT (SINGLE SOURCE OF TRUTH: JigCompleted)
# =============================================================================
class JigSaveAPI(APIView):
	"""POST /api/jig/save/ — Unified API for Draft + Submit.

	Single source of truth: JigCompleted table.
	Differentiated by action = "draft" / "submit".
	No recomputation. Stores exactly what the frontend sends.
	"""
	permission_classes = [IsAuthenticated]

	@transaction.atomic
	def post(self, request):
		payload = request.data
		user = request.user
		action = payload.get('action', 'draft')  # "draft" or "submit"
		lot_id = payload.get('lot_id')
		batch_id = payload.get('batch_id')
		jig_id = (payload.get('jig_id', '') or '').strip().upper()

		logging.info(json.dumps({
			'event': 'JIG_SAVE_API',
			'action': action,
			'lot_id': lot_id, 'batch_id': batch_id, 'jig_id': jig_id,
			'user': user.username,
		}))

		if not lot_id or not batch_id or not action:
			return Response({'status': 'error', 'message': 'lot_id, batch_id, and action are required'}, status=status.HTTP_400_BAD_REQUEST)

		if action not in ('draft', 'submit'):
			return Response({'status': 'error', 'message': 'action must be "draft" or "submit"'}, status=status.HTTP_400_BAD_REQUEST)

		if TotalStockModel.objects.filter(lot_id=lot_id, jig_hold_lot=True).exists():
			return Response({'status': 'error', 'message': 'This lot is on hold and cannot be processed until released.'}, status=status.HTTP_400_BAD_REQUEST)

		# Extract EXACT UI values — no recalculation
		lot_qty = int(payload.get('lot_qty', 0) or 0)
		jig_capacity = int(payload.get('jig_capacity', 0) or 0)
		effective_capacity = int(payload.get('effective_capacity', 0) or 0)
		broken_hooks = int(payload.get('broken_hooks', 0) or 0)
		loaded_cases_qty = int(payload.get('loaded_cases_qty', 0) or 0)
		empty_hooks = int(payload.get('empty_hooks', 0) or 0)
		tray_data = payload.get('tray_data', [])
		total_delink_qty = int(payload.get('total_delink_qty', 0) or 0)
		total_excess_qty = int(payload.get('total_excess_qty', 0) or 0)
		scanned_trays = payload.get('scanned_trays', [])
		multi_model_allocation = payload.get('multi_model_allocation', [])
		half_filled_tray_info = payload.get('half_filled_tray_info', [])
		is_multi_model = bool(payload.get('is_multi_model', False))
		# Auto-detect: if allocation has multiple entries, treat as multi-model regardless of flag
		if len(multi_model_allocation) > 1:
			is_multi_model = True
		nickel_bath_type = payload.get('nickel_bath_type', '') or ''
		tray_type = payload.get('tray_type', '') or ''
		tray_capacity = int(payload.get('tray_capacity', 12) or 12)
		plating_stock_num = payload.get('plating_stock_num', '') or ''
		remarks = payload.get('remarks', '') or ''
		resolved_plating_color = payload.get('plating_color', '') or ''
		if not resolved_plating_color and batch_id:
			batch_master = ModelMasterCreation.objects.filter(batch_id=batch_id).only('plating_color', 'plating_stk_no').first()
			if batch_master:
				resolved_plating_color = batch_master.plating_color or ''
				if not plating_stock_num and batch_master.plating_stk_no:
					plating_stock_num = batch_master.plating_stk_no
					payload['plating_stock_num'] = plating_stock_num
		if resolved_plating_color:
			payload['plating_color'] = resolved_plating_color

		# Preserve existing remarks if payload sends empty (remarks saved via /api/update-remark/)
		if not remarks:
			existing_rec = JigCompleted.objects.filter(lot_id=lot_id, batch_id=batch_id, user=user).first()
			if existing_rec and existing_rec.remarks:
				remarks = existing_rec.remarks
			else:
				# Fallback: remark may have been saved with a different batch_id (e.g. 'null')
				# by UpdateRemarkAPI before the real batch_id was assigned during submit
				draft_with_remark = JigCompleted.objects.filter(
					lot_id=lot_id, user=user, draft_status__in=['draft', 'active']
				).exclude(remarks__isnull=True).exclude(remarks='').first()
				if draft_with_remark:
					remarks = draft_with_remark.remarks

		# Keep the exact preserved remark inside draft_data as well as the
		# dedicated model column.  Otherwise GET rehydration sees payload['remarks']
		# as an empty string and can hide a remark that was already saved.
		payload['remarks'] = remarks

		# === SUBMIT-SPECIFIC VALIDATIONS ===
		if action == 'submit':
			if not jig_id:
				return Response({'status': 'error', 'message': 'jig_id is required for submit'}, status=status.HTTP_400_BAD_REQUEST)

			# Jig ID format validation — always derive capacity fresh from the model's
			# JigLoadingMaster record (Golden Rule: backend decides). Do NOT pass the
			# frontend-supplied `jig_capacity` as an override here: that skips the DB
			# lookup entirely and validates against whatever value the client sent,
			# which can be stale (e.g. loaded before a JigLoadingMaster correction).
			lot_data = fetch_lot_data(lot_id, batch_id)
			jig_capacity_val = int(lot_data.get('jig_capacity', 0) or 0) or jig_capacity
			expected_jig_prefix = f'J{jig_capacity_val:03d}-'
			if not jig_id.startswith(expected_jig_prefix):
				return Response({'status': 'error', 'message': f'Invalid Jig ID. Expected format: {expected_jig_prefix}#### for capacity {jig_capacity_val}.'}, status=status.HTTP_400_BAD_REQUEST)
			if len(jig_id) != 9 or not jig_id[5:].isdigit():
				return Response({'status': 'error', 'message': f'Invalid Jig ID format. Must be 9 characters: J###-####.'}, status=status.HTTP_400_BAD_REQUEST)

			# Jig existence check — jig_id must exist in Jig master table.
			# select_for_update() takes the row lock immediately (cheap) so a
			# retried/duplicate submit (e.g. the client re-clicking Submit after
			# a network timeout) serializes HERE instead of after it has already
			# minted delink/excess-lot records — see the idempotency guard below.
			jig_obj = Jig.objects.select_for_update().filter(jig_qr_id=jig_id).first()
			if not jig_obj:
				return Response({'status': 'error', 'message': f'Jig {jig_id} does not exist. Please scan a valid Jig ID from the Jig master.'}, status=status.HTTP_400_BAD_REQUEST)

			# IDEMPOTENCY GUARD (CLAUDE.md Security Rules: "Protect against duplicate
			# submissions"). A slow request (e.g. the per-tray cross-module delink
			# propagation below taking too long on a loaded server) can time out on
			# the client while the transaction keeps running and commits server-side.
			# The user then retries. Because the lock above makes this request wait
			# for any in-flight submit of the same jig to finish, by the time we get
			# here we are guaranteed to see the committed outcome — so if this exact
			# submit already completed, replay its stored result instead of
			# re-running the write pipeline (which would create a second excess lot
			# / stock record for the same physical trays).
			existing_submitted = JigCompleted.objects.filter(
				lot_id=lot_id, batch_id=batch_id, user=user,
				draft_status='submitted', jig_id=jig_id,
			).first()
			if existing_submitted:
				prior_excess = ExcessLotRecord.objects.filter(jig_id=jig_id, parent_lot_id=lot_id).order_by('-id').first()
				logging.info(json.dumps({
					'event': 'JIG_SAVE_DUPLICATE_SUBMIT_SHORT_CIRCUIT',
					'lot_id': lot_id, 'batch_id': batch_id, 'jig_id': jig_id,
				}))
				return Response({
					'status': 'success',
					'message': 'Submitted successfully',
					'lot_status': 'Completed',
					'record_id': existing_submitted.id,
					'lot_id': lot_id,
					'batch_id': batch_id,
					'draft_status': 'submitted',
					'jig_id': jig_id,
					'loaded_cases_qty': existing_submitted.loaded_cases_qty,
					'effective_capacity': existing_submitted.effective_capacity,
					'total_delink_qty': existing_submitted.delink_tray_qty,
					'total_excess_qty': existing_submitted.excess_qty,
					'delink_records_created': JigDelinkRecord.objects.filter(jig_id=jig_id, lot_id=lot_id, batch_id=batch_id).count(),
					'excess_lot_id': prior_excess.new_lot_id if prior_excess else None,
					'excess_trays_created': ExcessLotTray.objects.filter(excess_lot=prior_excess).count() if prior_excess else 0,
					'model_image_label': existing_submitted.plating_stock_num,
					'lot_qty': existing_submitted.original_lot_qty,
					'no_of_model_cases': existing_submitted.no_of_model_cases,
				})

			# Jig reuse validation — prevent using loaded/drafted jigs
			cycle_info = get_next_jig_cycle(jig_id, lot_id)

			# Block only if jig is drafted by a DIFFERENT lot — same-lot draft → submit is allowed
			if cycle_info['drafted_by_other_lot']:
				return Response(
					{'status': 'error', 'message': 'This Jig is currently drafted by another lot. Cannot submit.'},
					status=status.HTTP_409_CONFLICT
				)

			# Block if jig is currently loaded
			if not cycle_info['can_reuse']:
				return Response(
					{'status': 'error', 'message': 'This Jig ID is already in use. Unload first before reuse.'},
					status=status.HTTP_409_CONFLICT
				)

			# Jig occupancy check: submitted JigCompleted rows are historical records.
			# Reuse is blocked only while the Jig master marks the jig as occupied/loaded.
			jig_obj_loaded = jig_obj.occupied_flag and not (
				jig_obj.current_user_id == user.id and jig_obj.batch_id == batch_id and jig_obj.lot_id == lot_id
			)
			if jig_obj_loaded:
				return Response({'status': 'error', 'message': f'Jig {jig_id} is already in use.'}, status=status.HTTP_409_CONFLICT)

			# Check jig not locked by another user
			if jig_obj.is_locked_by_other_user(user):
				return Response({'status': 'error', 'message': f'Jig {jig_id} is locked by another user.'}, status=status.HTTP_409_CONFLICT)

			# Loaded cases must be > 0 for submit (cannot submit an empty jig)
			if loaded_cases_qty <= 0:
				return Response({'status': 'error', 'message': 'Cannot submit: no cases loaded on jig.'}, status=status.HTTP_400_BAD_REQUEST)

			# Sum validation (strict for submit)
			# IMPORTANT: `original_qty` is the physical tray capacity/quantity and is
			# NOT the authoritative requested quantity in multi-model loading. A set
			# of physical trays can total more than the cases requested by the model
			# lots (for example, 156 physical tray capacity for 120 requested cases).
			#
			# For multi-model submissions, the backend allocation already carries
			# `requested_qty` for every model. That is the correct invariant:
			#       delink_qty + excess_qty == total requested model quantity
			#
			# For single-model submissions, the authoritative requested quantity is
			# `lot_qty`. Do NOT validate against sum(original_qty).
			validation_errors = []
			sum_orig = sum(int(t.get('original_qty', 0) or 0) for t in tray_data)
			sum_delink = sum(int(t.get('delink_qty', 0) or 0) for t in tray_data)
			sum_excess = sum(int(t.get('excess_qty', 0) or 0) for t in tray_data)

			def _norm_tray_id(value):
				return str(value or '').strip().upper()

			def _sum_qty_by_tray(rows, qty_key):
				qty_by_tray = {}
				for row in rows if isinstance(rows, list) else []:
					if not isinstance(row, dict):
						continue
					qty = int(row.get(qty_key, 0) or 0)
					if qty <= 0:
						continue
					tray_id_val = _norm_tray_id(row.get('tray_id'))
					if not tray_id_val:
						continue
					qty_by_tray[tray_id_val] = qty_by_tray.get(tray_id_val, 0) + qty
				return qty_by_tray

			tray_excess_by_id = _sum_qty_by_tray(tray_data, 'excess_qty')
			half_filled_by_id = _sum_qty_by_tray(half_filled_tray_info, 'qty')
			sum_half_filled = sum(half_filled_by_id.values())

			if is_multi_model and multi_model_allocation:
				# `requested_qty` is produced by the backend multi-model allocator
				# before the UI is rendered. `allocated_qty` is intentionally NOT
				# used here because it is capped to jig capacity (e.g. 98) and
				# therefore excludes overflow that belongs in the excess lot (e.g. 22).
				requested_model_qty = 0
				requested_model_qty_by_lot = []
				for model_alloc in multi_model_allocation:
					if not isinstance(model_alloc, dict):
						continue
					requested_qty = model_alloc.get('requested_qty', None)
					if requested_qty is None:
						# Compatibility with older saved allocations. Prefer an explicit
						# requested/model-lot field over allocated_qty because allocated
						# quantity may be capacity-capped.
						requested_qty = model_alloc.get('model_lot_qty', model_alloc.get('qty', 0))
					requested_qty = max(0, int(requested_qty or 0))
					requested_model_qty += requested_qty
					requested_model_qty_by_lot.append({
						'lot_id': model_alloc.get('lot_id', ''),
						'requested_qty': requested_qty,
						'allocated_qty': int(model_alloc.get('allocated_qty', 0) or 0),
					})

				expected_requested_qty = requested_model_qty
				logging.info(json.dumps({
					'event': 'JIG_SUBMIT_MULTI_MODEL_QUANTITY_VALIDATION',
					'expected_requested_qty': expected_requested_qty,
					'sum_delink': sum_delink,
					'sum_excess': sum_excess,
					'physical_tray_original_qty': sum_orig,
					'models': requested_model_qty_by_lot,
				}))
			else:
				expected_requested_qty = max(0, int(lot_qty or 0))

			if expected_requested_qty > 0 and (sum_delink + sum_excess) != expected_requested_qty:
				if is_multi_model and multi_model_allocation:
					validation_errors.append(
						f'sum(delink_qty)+sum(excess_qty)={sum_delink + sum_excess} '
						f'!= requested_multi_model_qty={expected_requested_qty}'
					)
				else:
					validation_errors.append(
						f'sum(delink_qty)+sum(excess_qty)={sum_delink + sum_excess} '
						f'!= lot_qty={expected_requested_qty}'
					)
			if sum_delink != total_delink_qty:
				validation_errors.append(f'sum(delink_qty)={sum_delink} != total_delink_qty={total_delink_qty}')
			if sum_excess != total_excess_qty:
				validation_errors.append(f'sum(excess_qty)={sum_excess} != total_excess_qty={total_excess_qty}')
			if total_excess_qty > 0:
				if sum_half_filled != total_excess_qty:
					validation_errors.append(
						f'sum(half_filled_tray_info.qty)={sum_half_filled} != total_excess_qty={total_excess_qty}'
					)
				if tray_excess_by_id != half_filled_by_id:
					validation_errors.append(
						'tray_data excess tray IDs/qty do not match half_filled_tray_info'
					)
			for tray in tray_data if isinstance(tray_data, list) else []:
				if not isinstance(tray, dict):
					continue
				orig_qty = int(tray.get('original_qty', 0) or 0)
				delink_qty = int(tray.get('delink_qty', 0) or 0)
				excess_qty = int(tray.get('excess_qty', 0) or 0)
				if delink_qty > 0 and excess_qty > 0 and orig_qty > 0 and (delink_qty + excess_qty) > orig_qty:
					validation_errors.append(
						f"tray {tray.get('tray_id', '')}: delink_qty + excess_qty exceeds original_qty"
					)
			if validation_errors:
				logging.error(json.dumps({'event': 'JIG_SUBMIT_VALIDATION_FAILED', 'errors': validation_errors}))
				return Response({'status': 'error', 'message': 'Validation failed: data integrity mismatch', 'errors': validation_errors}, status=status.HTTP_400_BAD_REQUEST)
		else:
			# DRAFT: warn but don't block
			sum_orig = sum(int(t.get('original_qty', 0) or 0) for t in tray_data)
			sum_delink = sum(int(t.get('delink_qty', 0) or 0) for t in tray_data)
			sum_excess = sum(int(t.get('excess_qty', 0) or 0) for t in tray_data)
			if sum_orig > 0 and (sum_delink + sum_excess) != sum_orig:
				logging.warning(json.dumps({'event': 'JIG_DRAFT_VALIDATION_WARN', 'sum_mismatch': True}))
			# DRAFT: validate jig_id against Jig master if provided
			if jig_id and not Jig.objects.filter(jig_qr_id=jig_id).exists():
				return Response({'status': 'error', 'message': f'Jig {jig_id} does not exist. Please scan a valid Jig ID from the Jig master.'}, status=status.HTTP_400_BAD_REQUEST)

		draft_status = 'draft' if action == 'draft' else 'submitted'

		# Build no_of_model_cases string for multi-model display
		no_of_model_cases_str = ''
		effective_plating_stock_num = plating_stock_num
		if is_multi_model and multi_model_allocation:
			parts = []
			model_names = []
			for m in multi_model_allocation:
				label = m.get('model_image_label') or m.get('model') or m.get('display_name', '')
				mlot = m.get('lot_id', '')
				mqty = m.get('allocated_qty') or m.get('qty', 0)
				parts.append(f"{label}({mlot}):{mqty}")
				model_name = m.get('model') or m.get('model_name') or m.get('display_name', '')
				if model_name:
					model_names.append(model_name)
			no_of_model_cases_str = ' | '.join(parts)
			if model_names:
				effective_plating_stock_num = ', '.join(model_names)

		# Compute half_filled_tray_qty from info
		half_filled_tray_qty = 0
		if isinstance(half_filled_tray_info, list):
			half_filled_tray_qty = sum(int(t.get('qty', 0) or 0) for t in half_filled_tray_info)
		elif isinstance(half_filled_tray_info, dict):
			half_filled_tray_qty = int(half_filled_tray_info.get('total_qty', 0) or 0)

		# ===== MULTI-MODEL: Inject per-model role + status (backend owns this logic) =====
		if is_multi_model and multi_model_allocation:
			for idx, m in enumerate(multi_model_allocation):
				m['role'] = 'primary' if idx == 0 else 'secondary'
				if action == 'draft':
					m['status'] = 'draft' if idx == 0 else 'partial_draft'
				else:
					m['status'] = 'submitted'

		# ===== SINGLE TABLE STORAGE: JigCompleted =====
		try:
			record, created = JigCompleted.objects.update_or_create(
				lot_id=lot_id, batch_id=batch_id, user=user,
				defaults={
					'draft_data': payload,  # FULL UI SNAPSHOT
					'jig_id': jig_id or None,
					'jig_capacity': jig_capacity,
					'effective_capacity': effective_capacity,
					'broken_hooks': broken_hooks,
					'loaded_cases_qty': total_delink_qty if action == 'submit' else loaded_cases_qty,
					'original_lot_qty': lot_qty,
					'delink_tray_info': [t for t in tray_data if int(t.get('delink_qty', 0) or 0) > 0],
					'delink_tray_qty': total_delink_qty,
					'delink_tray_count': sum(1 for t in tray_data if int(t.get('delink_qty', 0) or 0) > 0),
					'draft_status': draft_status,
					'plating_stock_num': effective_plating_stock_num,
					'remarks': remarks,
					'is_multi_model': is_multi_model,
					'tray_capacity': tray_capacity,
					'nickel_bath_type': nickel_bath_type,
					'tray_type': tray_type,
					'half_filled_tray_info': half_filled_tray_info,
					'half_filled_tray_qty': half_filled_tray_qty,
					'multi_model_allocation': multi_model_allocation,
					'no_of_model_cases': no_of_model_cases_str or None,
					'scanned_trays': scanned_trays,
					'empty_hooks': empty_hooks,
					'excess_qty': total_excess_qty,
				}
			)
			
			# CRITICAL: Set Jig.drafted flag when action='draft'
			if action == 'draft' and jig_id:
				try:
					jig_obj = Jig.objects.filter(jig_qr_id=jig_id).first()
					if jig_obj:
						jig_obj.drafted = True
						jig_obj.lot_id = lot_id
						jig_obj.batch_id = batch_id
						jig_obj.save()
						logging.info(f'JigSaveAPI: Set Jig.drafted=True for {jig_id} in lot {lot_id}')
				except Exception as e:
					logging.exception(f'JigSaveAPI: Failed to set drafted flag: {e}')
			logging.info(json.dumps({
				'event': 'JIG_SAVE_STORED',
				'action': action, 'draft_status': draft_status,
				'record_id': record.id, 'created': created,
				'lot_id': lot_id, 'batch_id': batch_id,
			}))

			# Real processing activity (draft save or submit) — advance the shared
			# current_stage SSOT so the previous module (Brass Audit) can show
			# "Jig Loading" as the Current Location instead of a stale value.
			try:
				from modelmasterapp.stage_service import update_stock_stage
				update_stock_stage(lot_id, 'Jig Loading')
				if is_multi_model and multi_model_allocation:
					for m in multi_model_allocation:
						m_lot_id = m.get('lot_id')
						if m_lot_id and m_lot_id != lot_id:
							update_stock_stage(m_lot_id, 'Jig Loading')
			except Exception:
				logging.exception('JigSaveAPI: current_stage update failed')

			# After submit: clear remarks from any other draft records for this lot
			# so excess lot rows in pick table don't display stale remarks
			if action == 'submit' and remarks:
				JigCompleted.objects.filter(
					lot_id=lot_id, user=user, draft_status__in=['draft', 'active']
				).exclude(id=record.id).exclude(remarks__isnull=True).exclude(remarks='').update(remarks='')
		except Exception as e:
			logging.exception(f'JigSaveAPI: save failed: {e}')
			return Response({'status': 'error', 'message': 'Failed to save'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

		# ===== SUBMIT-ONLY: Delink records, Excess lot, Jig lock =====
		delink_created = 0
		excess_lot_id = None
		excess_trays_created = 0

		if action == 'submit':
			# Create JigDelinkRecord entries
			try:
				JigDelinkRecord.objects.filter(
					jig_id=jig_id, lot_id=lot_id, batch_id=batch_id
				).delete()

				jig_loading_record_defaults = {
					'jig_id': jig_id,
					'lot_qty': lot_qty,
					'jig_capacity': jig_capacity,
					'effective_capacity': effective_capacity,
					'broken_hooks': broken_hooks,
					'loaded_cases_qty': total_delink_qty,
					'empty_hooks': empty_hooks,
					'tray_data': tray_data,
					'total_delink_qty': total_delink_qty,
					'total_excess_qty': total_excess_qty,
					'scanned_trays': scanned_trays,
					'status_flag': 'SUBMITTED',
					'is_multi_model': is_multi_model,
					'multi_model_allocation': multi_model_allocation,
					'half_filled_tray_info': half_filled_tray_info,
					'nickel_bath_type': nickel_bath_type,
					'tray_type': tray_type,
					'tray_capacity': tray_capacity,
					'plating_stock_num': effective_plating_stock_num,
					'remarks': remarks,
				}
				# JigLoadingRecord is a single FK target shared by every delink row for
				# this lot/batch/user — it was previously update_or_create'd on EVERY
				# tray iteration (identical `defaults` each time), turning an N-tray
				# submit into N redundant SELECT+UPDATE round trips. Resolve it once.
				jlr_for_submit, _ = JigLoadingRecord.objects.update_or_create(
					lot_id=lot_id, batch_id=batch_id, user=user,
					defaults=jig_loading_record_defaults
				)

				delink_record_objs = []
				propagate_tray_ids = []

				for tray in tray_data:
					d_qty = int(tray.get('delink_qty', 0) or 0)
					e_qty = int(tray.get('excess_qty', 0) or 0)
					if d_qty <= 0:
						continue
					tray_id_val = tray.get('tray_id', '')

					delink_record_objs.append(JigDelinkRecord(
						jig_loading_record=jlr_for_submit,
						jig_id=jig_id,
						lot_id=tray.get('source_lot_id', '') or lot_id,
						batch_id=batch_id,
						tray_id=tray_id_val,
						delink_qty=d_qty,
						original_qty=int(tray.get('original_qty', 0) or 0),
						model_code=tray.get('model_code', '') or effective_plating_stock_num,
						scanned_tray_id=tray_id_val,
					))
					delink_created += 1

					if e_qty > 0:
						logging.info(
							'JigSaveAPI: preserving active excess tray occupancy '
							'for split tray_id=%s delink_qty=%s excess_qty=%s',
							tray_id_val,
							d_qty,
							e_qty,
						)
						continue

					propagate_tray_ids.append(tray_id_val)

				if delink_record_objs:
					JigDelinkRecord.objects.bulk_create(delink_record_objs)

				# Propagate delink status to the shared tray master, Day Planning's tray
				# history, AND every other module's own tray mirror table, so the tray
				# reads as free everywhere — not just in Jig Loading's own records.
				# Mirrors the exact cross-module propagation IQF already does for its
				# own delink flow (IQF/views.py, LotDelinkAPIView: BrassTrayId /
				# BrassAuditTrayId / IPTrayId updates after freeing the TrayId master).
				# Jig Loading previously only updated the master + DPTrayId_History,
				# leaving stale delink_tray=False rows in IS/Brass QC/Brass Audit/IQF's
				# own tables — DayPlanning's validate_tray_cross_module_occupancy() reads
				# those directly and blocked reuse even though the master was already free.
				#
				# Batched across all trays in this submit (one query per table instead
				# of one per tray per table) — this loop previously issued ~7 queries
				# PER tray while holding the outer @transaction.atomic lock, which is
				# what let a large multi-tray/multi-model submit run long enough to hit
				# a client/gateway timeout under production load.
				if propagate_tray_ids:
					try:
						from DayPlanning.models import DPTrayId_History
						from InputScreening.models import IPTrayId
						from Brass_QC.models import BrassTrayId
						from BrassAudit.models import BrassAuditTrayId
						from IQF.models import IQFTrayId

						TrayId.objects.filter(tray_id__in=propagate_tray_ids).update(
							delink_tray=True, lot_id=None, batch_id=None,
							scanned=False, top_tray=False
						)

						# NOTE: all of the following are scoped to tray_id only (no lot_id
						# filter). By the time a tray reaches Jig Loading it has typically
						# moved through several modules, each minting a new child lot_id (see
						# LotMaster's strict-hierarchy design in modelmasterapp/models.py) —
						# a row scoped to the ORIGINAL lot_id would silently never match. Every
						# downstream availability check (DP's _is_tray_active_in_dp(),
						# validate_tray_cross_module_occupancy()) is likewise tray_id-only, so
						# this must mirror that scope or the delink never actually clears and
						# the tray stays permanently blocked from reuse.
						DPTrayId_History.objects.filter(
							tray_id__in=propagate_tray_ids, delink_tray=False
						).update(delink_tray=True)
						IPTrayId.objects.filter(
							tray_id__in=propagate_tray_ids, delink_tray=False
						).update(delink_tray=True)
						BrassTrayId.objects.filter(
							tray_id__in=propagate_tray_ids, delink_tray=False
						).update(delink_tray=True)
						BrassAuditTrayId.objects.filter(
							tray_id__in=propagate_tray_ids, delink_tray=False
						).update(delink_tray=True)
						IQFTrayId.objects.filter(
							tray_id__in=propagate_tray_ids, delink_tray=False
						).update(delink_tray=True)
					except Exception:
						logging.exception(f'JigSaveAPI: tray delink propagation failed for tray_ids={propagate_tray_ids}')
				logging.info(json.dumps({'event': 'JIG_DELINK_RECORDS_CREATED', 'count': delink_created}))
			except Exception as e:
				logging.exception(f'JigSaveAPI: delink records failed: {e}')
				return Response({'status': 'error', 'message': 'Failed to create delink records'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

			# Create ExcessLotRecord + ExcessLotTray
			if total_excess_qty > 0:
				try:
					import time
					ts = int(time.time() * 1000) % 100000
					# Strip any existing EX- prefix so re-excessing an excess lot never stacks prefixes (varchar(50) column)
					base_lot_id = lot_id[3:] if lot_id.startswith('EX-') else lot_id
					excess_lot_id = f'EX-{base_lot_id}-{ts:05d}'[:50]
					while ExcessLotRecord.objects.filter(new_lot_id=excess_lot_id).exists():
						ts = (ts + 1) % 100000
						excess_lot_id = f'EX-{base_lot_id}-{ts:05d}'[:50]

					if jlr_for_submit is None:
						jlr_for_submit, _ = JigLoadingRecord.objects.update_or_create(
							lot_id=lot_id, batch_id=batch_id, user=user,
							defaults=jig_loading_record_defaults
						)

					excess_lot = ExcessLotRecord.objects.create(
						jig_loading_record=jlr_for_submit,
						new_lot_id=excess_lot_id,
						parent_lot_id=lot_id,
						parent_batch_id=batch_id,
						lot_qty=total_excess_qty,
						jig_id=jig_id,
					)
					excess_batch_instance = ModelMasterCreation.objects.filter(batch_id=batch_id).first()
					for tray in tray_data:
						e_qty = int(tray.get('excess_qty', 0) or 0)
						if e_qty <= 0:
							continue
						excess_tray_id = tray.get('tray_id', '')
						ExcessLotTray.objects.create(
							excess_lot=excess_lot,
							lot_id=excess_lot_id,
							tray_id=excess_tray_id,
							qty=e_qty,
							original_qty=int(tray.get('original_qty', 0) or 0),
							model_code=tray.get('model_code', '') or effective_plating_stock_num,
						)
						try:
							tray_obj = TrayId.objects.filter(tray_id=excess_tray_id).first()
							if tray_obj:
								tray_obj.delink_tray = False
								tray_obj.lot_id = excess_lot_id
								tray_obj.scanned = True
								tray_obj.top_tray = bool(tray.get('top_tray', False))
								if excess_batch_instance:
									tray_obj.batch_id = excess_batch_instance
								tray_obj.save(update_fields=[
									'delink_tray', 'lot_id', 'batch_id', 'scanned', 'top_tray'
								])
						except Exception:
							logging.exception(
								'JigSaveAPI: failed to reserve excess tray_id=%s for lot_id=%s',
								excess_tray_id,
								excess_lot_id,
							)
						excess_trays_created += 1

					# ===== CREATE TotalStockModel for excess lot (makes it a REAL lot) =====
					try:
						parent_stock = TotalStockModel.objects.filter(lot_id=lot_id).select_related('batch_id', 'model_stock_no', 'version', 'polish_finish', 'plating_color').first()
						if parent_stock:
							TotalStockModel.objects.create(
								lot_id=excess_lot_id,
								batch_id=parent_stock.batch_id,
								model_stock_no=parent_stock.model_stock_no,
								version=parent_stock.version,
								total_stock=total_excess_qty,
								brass_audit_accepted_qty=total_excess_qty,
								brass_audit_physical_qty=total_excess_qty,
								brass_audit_accptance=True,
								polish_finish=parent_stock.polish_finish,
								plating_color=parent_stock.plating_color,
								brass_audit_last_process_date_time=timezone.now(),
								last_process_module='Jig Loading (Excess)',
								current_stage='Jig Loading',
							)
							logging.info(f'[EXCESS_LOT_STOCK] Created TotalStockModel for {excess_lot_id} (qty={total_excess_qty}) from parent {lot_id}')
						else:
							logging.warning(f'[EXCESS_LOT_STOCK] Parent TotalStockModel not found for {lot_id} — excess lot {excess_lot_id} will use ExcessLotRecord fallback')
					except Exception as e:
						logging.exception(f'[EXCESS_LOT_STOCK] TotalStockModel creation failed for {excess_lot_id}: {e}')
						# Non-fatal — ExcessLotRecord is the primary source, TotalStockModel is for broader integration

					logging.info(json.dumps({'event': 'JIG_EXCESS_LOT_CREATED', 'new_lot_id': excess_lot_id, 'qty': total_excess_qty}))
				except Exception as e:
					logging.exception(f'JigSaveAPI: excess lot creation failed: {e}')
					return Response({'status': 'error', 'message': 'Failed to create excess lot'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

			# Lock Jig (reuse the row-locked instance fetched above under
			# select_for_update() rather than issuing a fresh unlocked query)
			try:
				if jig_obj:
					jig_obj.is_loaded = True
					jig_obj.occupied_flag = True
					jig_obj.current_user = user
					jig_obj.locked_at = timezone.now()
					jig_obj.drafted = False
					jig_obj.batch_id = batch_id
					jig_obj.lot_id = lot_id
					jig_obj.save()
			except Exception:
				logging.exception('JigSaveAPI: jig lock failed')

			# Clear parent's excess row from pick table when submitting an excess lot
			# Also clear parent excess row for all secondary EX-* lots in multi-model
			try:
				excess_lot_ids_to_clear = set()
				# Primary lot
				if lot_id and lot_id.startswith('EX-'):
					excess_lot_ids_to_clear.add(lot_id)
				# Secondary lots in multi-model
				if is_multi_model and multi_model_allocation:
					for m in multi_model_allocation:
						mlot = m.get('lot_id', '') if isinstance(m, dict) else ''
						if mlot and mlot.startswith('EX-'):
							excess_lot_ids_to_clear.add(mlot)
				for ex_lot in excess_lot_ids_to_clear:
					elr = ExcessLotRecord.objects.filter(new_lot_id=ex_lot).first()
					if elr:
						updated = JigCompleted.objects.filter(
							lot_id=elr.parent_lot_id,
							draft_status='submitted',
							half_filled_tray_qty__gt=0
						).update(half_filled_tray_qty=0, excess_qty=0)
						if updated:
							logging.info(f'[EXCESS_SUBMIT_CLEANUP] Cleared parent {elr.parent_lot_id} half_filled_tray_qty (excess lot {ex_lot} submitted)')
			except Exception:
				logging.exception('JigSaveAPI: excess lot parent cleanup failed')

		elif action == 'draft' and jig_id:
			# Draft: mark jig as drafted (not fully loaded) and occupied
			try:
				Jig.objects.filter(jig_qr_id=jig_id).update(
					drafted=True,
					occupied_flag=True,
					lot_id=lot_id,
					batch_id=batch_id,
					current_user=user,
				)
			except Exception:
				logging.exception('JigSaveAPI: jig draft lock failed')

		# Build response
		message = 'Drafted successfully' if action == 'draft' else 'Submitted successfully'
		lot_status = 'Draft' if action == 'draft' else 'Completed'

		logging.info(json.dumps({
			'event': 'JIG_SAVE_COMPLETE',
			'action': action, 'lot_id': lot_id, 'draft_status': draft_status,
		}))

		response_data = {
			'status': 'success',
			'message': message,
			'lot_status': lot_status,
			'record_id': record.id,
			'lot_id': lot_id,
			'batch_id': batch_id,
			'draft_status': draft_status,
		}

		if action == 'submit':
			response_data.update({
				'jig_id': jig_id,
				'loaded_cases_qty': total_delink_qty,
				'effective_capacity': effective_capacity,
				'total_delink_qty': total_delink_qty,
				'total_excess_qty': total_excess_qty,
				'delink_records_created': delink_created,
				'excess_lot_id': excess_lot_id,
				'excess_trays_created': excess_trays_created,
				'model_image_label': effective_plating_stock_num,
				'lot_qty': lot_qty,
				'no_of_model_cases': no_of_model_cases_str if is_multi_model else None,
			})

		return Response(response_data)

	def get(self, request):
		"""GET /api/jig/save?lot_id=X&batch_id=Y — Fetch existing draft for rehydration."""
		lot_id = request.query_params.get('lot_id')
		batch_id = request.query_params.get('batch_id')

		if not lot_id or not batch_id:
			return Response({'status': 'error', 'message': 'lot_id and batch_id required'}, status=status.HTTP_400_BAD_REQUEST)

		record = JigCompleted.objects.filter(
			lot_id=lot_id, batch_id=batch_id, user=request.user, draft_status__in=['draft', 'active']
		).first()

		if not record:
			return Response({'status': 'not_found', 'message': 'No draft found'}, status=status.HTTP_404_NOT_FOUND)

		# Return the full draft_data snapshot for exact UI rehydration
		draft_data = record.draft_data or {}

		return Response({
			'status': 'success',
			'is_draft': True,
			'record_id': record.id,
			'draft_status': record.draft_status,
			'jig_id': record.jig_id or '',
			'lot_id': record.lot_id,
			'batch_id': record.batch_id,
			'lot_qty': draft_data.get('lot_qty', record.original_lot_qty or 0),
			'jig_capacity': draft_data.get('jig_capacity', record.jig_capacity or 0),
			'effective_capacity': draft_data.get('effective_capacity', record.effective_capacity or 0),
			'broken_hooks': draft_data.get('broken_hooks', record.broken_hooks or 0),
			'loaded_cases_qty': draft_data.get('loaded_cases_qty', record.loaded_cases_qty or 0),
			'empty_hooks': draft_data.get('empty_hooks', record.empty_hooks or 0),
			'nickel_bath_type': draft_data.get('nickel_bath_type', record.nickel_bath_type or ''),
			'tray_type': draft_data.get('tray_type', record.tray_type or ''),
			'tray_capacity': draft_data.get('tray_capacity', record.tray_capacity or 12),
			'plating_stock_num': draft_data.get('plating_stock_num', record.plating_stock_num or ''),
			'remarks': draft_data.get('remarks', record.remarks or ''),
			'is_multi_model': draft_data.get('is_multi_model', record.is_multi_model),
			'tray_data': draft_data.get('tray_data', []),
			'total_delink_qty': draft_data.get('total_delink_qty', record.delink_tray_qty or 0),
			'total_excess_qty': draft_data.get('total_excess_qty', record.excess_qty or 0),
			'scanned_trays': draft_data.get('scanned_trays', record.scanned_trays or []),
			'multi_model_allocation': draft_data.get('multi_model_allocation', record.multi_model_allocation or []),
			'half_filled_tray_info': draft_data.get('half_filled_tray_info', record.half_filled_tray_info or []),
			# Full immutable-at-save UI snapshot.  The frontend uses the
			# individual fields above for compatibility and this complete object
			# for exact draft rehydration.
			'draft_snapshot': draft_data,
			'updated_at': record.updated_at.isoformat() if record.updated_at else None,
		})


# =============================================================================
# JIG ID VALIDATION API — lightweight existence check
# =============================================================================
class JigValidateAPI(APIView):
	"""GET /api/jig/validate/?jig_id=J098-0001&lot_id=LID123
	Returns validation info including existence, cycle count, draft status, and reuse status.
	CRITICAL: lot_id parameter required for lot-aware draft checking.
	"""
	permission_classes = [IsAuthenticated]

	def get(self, request):
		jig_id = (request.GET.get('jig_id', '') or '').strip().upper()
		lot_id = (request.GET.get('lot_id', '') or '').strip()

		if not jig_id:
			return Response(
				{'exists': False, 'message': 'jig_id is required'},
				status=status.HTTP_400_BAD_REQUEST
			)

		if not lot_id:
			return Response(
				{'exists': False, 'message': 'lot_id is required'},
				status=status.HTTP_400_BAD_REQUEST
			)

		# Check if jig exists in master table
		exists = Jig.objects.filter(jig_qr_id=jig_id).exists()

		if not exists:
			return Response({
				'exists': False,
				'jig_id': jig_id,
				'lot_id': lot_id,
				'cycle_count': 1,
				'can_reuse': True,
				'is_loaded': False,
				'is_drafted': False,
				'drafted_by_other_lot': False,
				'message': 'Jig ID not found in master table'
			})

		# Get cycle count and reuse validation (NOW LOT-AWARE)
		cycle_info = get_next_jig_cycle(jig_id, lot_id)

		response_data = {
			'exists': True,
			'jig_id': jig_id,
			'lot_id': lot_id,
			'cycle_count': cycle_info['cycle_count'],
			'can_reuse': cycle_info['can_reuse'],
			'is_loaded': cycle_info['is_loaded'],
			'is_drafted': cycle_info['is_drafted'],
			'drafted_by_other_lot': cycle_info['drafted_by_other_lot']
		}

		# CRITICAL: Block jigs drafted by a DIFFERENT lot only
		if cycle_info['is_drafted'] and cycle_info['drafted_by_other_lot']:
			response_data['message'] = 'Drafted already'
			response_data['can_reuse'] = False
		elif not cycle_info['can_reuse'] and cycle_info['is_loaded']:
			response_data['message'] = 'This Jig ID is already in use. Unload first before reuse.'

		return Response(response_data)


# =============================================================================
# JIG LOADING HOLD/UNHOLD API
# =============================================================================
class JigHoldToggleAPI(APIView):
	"""
	POST /api/hold-toggle/ — Save hold/unhold reason for jig loading
	
	Request:
	{
		"lot_id": "LID070420260947350002",
		"batch_id": "BATCH-20260407094059434767-84",
		"action": "hold" or "unhold",
		"reason": "Quality issue" (required for hold, optional for unhold)
	}
	
	Response:
	{
		"success": true/false,
		"hold_status": true/false,
		"message": "Lot moved to hold" or "Lot released from hold"
	}
	"""
	permission_classes = [IsAuthenticated]

	def post(self, request):
		try:
			data = request.data if hasattr(request, 'data') else json.loads(request.body.decode('utf-8'))
			lot_id = data.get('lot_id', '').strip()
			batch_id = data.get('batch_id', '').strip()
			action = data.get('action', '').strip().lower()
			reason = data.get('reason', '').strip()

			# ===== VALIDATION =====
			if not lot_id:
				return Response({'success': False, 'error': 'lot_id is required'}, status=status.HTTP_400_BAD_REQUEST)
			if not batch_id:
				return Response({'success': False, 'error': 'batch_id is required'}, status=status.HTTP_400_BAD_REQUEST)
			if action not in ['hold', 'unhold']:
				return Response({'success': False, 'error': 'action must be hold or unhold'}, status=status.HTTP_400_BAD_REQUEST)
			if not reason:
				return Response({'success': False, 'error': f'reason is required for {action} action'}, status=status.HTTP_400_BAD_REQUEST)

			# ===== FETCH LOT =====
			lot_obj = TotalStockModel.objects.filter(lot_id=lot_id).first()
			if not lot_obj:
				return Response({'success': False, 'error': 'Lot not found'}, status=status.HTTP_404_NOT_FOUND)

			# ===== UPDATE LOT STATUS =====
			logging.info(json.dumps({
				'event': 'JIG_HOLD_TOGGLE',
				'lot_id': lot_id,
				'batch_id': batch_id,
				'action': action,
				'user': request.user.username,
			}))

			now = timezone.now()
			if action == 'hold':
				lot_obj.jig_hold_lot = True
				lot_obj.jig_holding_reason = reason
				lot_obj.jig_hold_by = request.user
				lot_obj.jig_hold_at = now
				# clear any previous release flags when newly holding
				lot_obj.jig_release_lot = False
				lot_obj.jig_release_reason = ''
				message = 'Lot moved to hold'
				hold_status = True
			else:  # unhold
				# mark as released; preserve previous holding reason and record release reason
				lot_obj.jig_hold_lot = False
				lot_obj.jig_release_lot = True
				lot_obj.jig_release_reason = reason
				lot_obj.jig_release_by = request.user
				lot_obj.jig_release_at = now
				message = 'Lot released from hold'
				hold_status = False

			lot_obj.save(update_fields=[
				'jig_hold_lot', 'jig_holding_reason', 'jig_hold_by', 'jig_hold_at',
				'jig_release_lot', 'jig_release_reason', 'jig_release_by', 'jig_release_at',
			])

			logging.info(json.dumps({
				'event': 'JIG_HOLD_TOGGLE_COMPLETE',
				'lot_id': lot_id,
				'action': action,
				'hold_status': hold_status,
			}))

			return Response({
				'success': True,
				'hold_status': hold_status,
				'jig_hold_lot': lot_obj.jig_hold_lot,
				'jig_holding_reason': lot_obj.jig_holding_reason or '',
				'jig_release_lot': lot_obj.jig_release_lot,
				'jig_release_reason': lot_obj.jig_release_reason or '',
				'holding_reason': lot_obj.jig_holding_reason or '',
				'release_reason': lot_obj.jig_release_reason or '',
				'hold_by': lot_obj.jig_hold_by.username if lot_obj.jig_hold_by else '',
				'hold_at': timezone.localtime(lot_obj.jig_hold_at).strftime("%d-%b-%Y %I:%M %p") if lot_obj.jig_hold_at else '',
				'release_by': lot_obj.jig_release_by.username if lot_obj.jig_release_by else '',
				'release_at': timezone.localtime(lot_obj.jig_release_at).strftime("%d-%b-%Y %I:%M %p") if lot_obj.jig_release_at else '',
				'message': message,
			}, status=status.HTTP_200_OK)

		except Exception as e:
			logging.error(f'JigHoldToggleAPI error: {str(e)}')
			return Response({
				'success': False,
				'error': 'Unable to process the request. Please verify the submitted data and try again.'
			}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


# =============================================================================
# DELETE JIG PICK RECORD API
# =============================================================================
class DeleteJigPickRecordAPI(APIView):
	"""POST /jig_loading/api/delete-pick-record/ — Delete a draft jig loading record.
	
	Deletes DRAFT status records only (not submitted).
	Request: { "lot_id": "...", "batch_id": "..." }
	Response: { "success": true, "message": "...", "deleted_count": 1 }
	"""
	permission_classes = [IsAuthenticated]

	def post(self, request):
		payload = request.data
		lot_id = payload.get('lot_id')
		batch_id = payload.get('batch_id')

		if not lot_id or not batch_id:
			return Response(
				{'success': False, 'error': 'lot_id and batch_id are required'},
				status=status.HTTP_400_BAD_REQUEST
			)

		logging.info(json.dumps({
			'event': 'DELETE_PICK_RECORD_REQUEST',
			'lot_id': lot_id,
			'batch_id': batch_id,
			'user': request.user.username,
		}))

		try:
			deleted_count = 0

			# Delete from JigCompleted (single source of truth — draft records)
			try:
				jc_draft = JigCompleted.objects.filter(
					lot_id=lot_id,
					batch_id=batch_id,
					user=request.user,
					draft_status__in=['draft', 'active']
				)
				if jc_draft.exists():
					deleted_count += jc_draft.count()
					jc_draft.delete()
					logging.info(f'[DELETE] Deleted {deleted_count} JigCompleted draft record(s)')
			except Exception as e:
				logging.exception(f'[DELETE] JigCompleted draft delete failed: {e}')

			# Delete JigLoadingRecord if any exist (backward compat)
			try:
				dr = JigLoadingRecord.objects.filter(
					lot_id=lot_id,
					batch_id=batch_id,
					user=request.user,
					status_flag='DRAFT'
				)
				if dr.exists():
					dr.delete()
					logging.info(f'[DELETE] Deleted JigLoadingRecord(s)')
			except Exception as e:
				logging.exception(f'[DELETE] JigLoadingRecord delete failed: {e}')

			# Also delete any JigCompleted entries that represent excess/half-filled trays
			# These entries are included in the pick table as excess lots — remove them on explicit delete
			try:
				jc_excess = JigCompleted.objects.filter(
					lot_id=lot_id, batch_id=batch_id,
					half_filled_tray_qty__gt=0
				)
				if jc_excess.exists():
					deleted_count += jc_excess.count()
					jc_excess.delete()
					logging.info(f'[DELETE] Deleted JigCompleted excess record(s)')
			except Exception as e:
				logging.exception(f'[DELETE] JigCompleted delete failed: {e}')

			# Create a JigCompleted placeholder with draft_status='submitted' to ensure the lot
			# is excluded from the pick table (JigView excludes submitted JigCompleted lot_ids).
			# This acts as a permanent removal from the pick table without altering TotalStockModel.
			try:
				exists_submitted = JigCompleted.objects.filter(lot_id=lot_id, batch_id=batch_id, draft_status='submitted').exists()
				if not exists_submitted:
					JigCompleted.objects.create(
						lot_id=lot_id,
						batch_id=batch_id,
						user=request.user,
						draft_status='submitted',
						draft_data={
							'deleted': True,
							'deleted_by': request.user.username,
							'deleted_at': timezone.now().isoformat()
						}
					)
			except Exception as e:
				logging.exception(f'[DELETE] JigCompleted create(submitted) failed: {e}')

			logging.info(json.dumps({
				'event': 'DELETE_PICK_RECORD_SUCCESS',
				'lot_id': lot_id,
				'batch_id': batch_id,
				'deleted_count': deleted_count,
				'user': request.user.username,
			}))

			return Response({
				'success': True,
				'message': 'Record deleted successfully',
				'deleted_count': deleted_count,
				'lot_id': lot_id,
				'batch_id': batch_id,
			}, status=status.HTTP_200_OK)

		except Exception as e:
			logging.exception(f'DeleteJigPickRecordAPI error: {str(e)}')
			return Response({
				'success': False,
				'error': 'Unable to process the request. Please verify the submitted data and try again.',
			}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


# =============================================================================
# UPDATE REMARK API
# =============================================================================
class UpdateRemarkAPI(APIView):
	"""POST /jig_loading/api/update-remark/ — Update/save remark for a pick table record.
	
	Request: {
		"lot_id": "...",
		"batch_id": "...",
		"remark_text": "user entered text",
		"remark_type": "text" or "audio"
	}
	Response: { "success": true, "message": "...", "remark_text": "..." }
	"""
	permission_classes = [IsAuthenticated]

	def post(self, request):
		payload = request.data
		lot_id = payload.get('lot_id')
		batch_id = payload.get('batch_id')
		remark_text = payload.get('remark_text', '').strip()
		remark_type = payload.get('remark_type', 'text')

		if not lot_id or not batch_id:
			return Response(
				{'success': False, 'error': 'lot_id and batch_id are required'},
				status=status.HTTP_400_BAD_REQUEST
			)

		logging.info(json.dumps({
			'event': 'UPDATE_REMARK_REQUEST',
			'lot_id': lot_id,
			'batch_id': batch_id,
			'remark_type': remark_type,
			'remark_length': len(remark_text),
			'user': request.user.username,
		}))

		try:
			# Update JigCompleted (single source of truth)
			# Include 'submitted' status to allow remarks on completed/submitted records
			record = JigCompleted.objects.filter(
				lot_id=lot_id,
				batch_id=batch_id,
				user=request.user,
				draft_status__in=['draft', 'active', 'submitted']
			).first()

			if record:
				record.remarks = remark_text
				record.save(update_fields=['remarks', 'updated_at'])
				logging.info(f'[UPDATE_REMARK] JigCompleted draft updated')
			else:
				# If no draft record exists, create one with the remark
				record, created = JigCompleted.objects.get_or_create(
					lot_id=lot_id,
					batch_id=batch_id,
					user=request.user,
					defaults={
						'jig_id': None,
						'jig_capacity': 0,
						'remarks': remark_text,
						'draft_status': 'draft',
					}
				)
				if created:
					logging.info(f'[UPDATE_REMARK] NEW JigCompleted draft created with remark')
				else:
					record.remarks = remark_text
					record.save(update_fields=['remarks', 'updated_at'])
					logging.info(f'[UPDATE_REMARK] Existing JigCompleted updated')

			logging.info(json.dumps({
				'event': 'UPDATE_REMARK_SUCCESS',
				'lot_id': lot_id,
				'batch_id': batch_id,
				'remark_length': len(remark_text),
			}))

			return Response({
				'success': True,
				'message': 'Remark saved successfully',
				'remark_text': remark_text,
				'lot_id': lot_id,
				'batch_id': batch_id,
			}, status=status.HTTP_200_OK)

		except Exception as e:
			logging.exception(f'UpdateRemarkAPI error: {str(e)}')
			return Response({
				'success': False,
				'error': 'Unable to process the request. Please verify the submitted data and try again.',
			}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class LotFetchAPI(APIView):
	"""GET /api/lots/ — Consolidated Lot Fetch API (backend-driven filtering).

	Modes:
	  ?mode=ADD&primary_lot_id=X&primary_batch_id=Y
	    → Returns lots available for Add Model, with dynamically computed available_qty.
	    → available_qty = total_lot_qty - qty already allocated in the CURRENT draft's
	      multi_model_allocation (active entries only).

	  Default (no mode)
	    → Returns all brass-audit-accepted lots for the pick table.

	RULE: Available qty is ALWAYS computed dynamically. Draft state never locks qty.
	"""
	permission_classes = [IsAuthenticated]

	def get(self, request):
		mode = (request.GET.get('mode', '') or '').strip().upper()
		primary_lot_id = request.GET.get('primary_lot_id', '') or ''
		primary_batch_id = request.GET.get('primary_batch_id', '') or ''

		logging.info(json.dumps({
			'event': 'LOT_FETCH_API',
			'mode': mode,
			'primary_lot_id': primary_lot_id,
			'primary_batch_id': primary_batch_id,
			'user': request.user.username,
		}))

		if mode == 'ADD':
			return self._handle_add_model_mode(request, primary_lot_id, primary_batch_id)

		# Default: return all brass-audit-accepted lots
		return self._handle_default_mode(request)

	def _handle_add_model_mode(self, request, primary_lot_id, primary_batch_id):
		"""Return lots available for Add Model with dynamically computed available_qty."""

		# 1. Exclude only the lots currently selected by the active Add Model UI.
		# Do not read the current draft's stored allocation here; it may contain a
		# model the user has removed before reopening the filter.
		allocated_lot_ids = set()
		if primary_lot_id:
			allocated_lot_ids.add(primary_lot_id)
		selected_lot_raw = request.GET.get('selected_lot_ids', '') or request.GET.get('exclude_lot_id', '')
		for selected_lot_id in [x.strip() for x in selected_lot_raw.split(',') if x.strip()]:
			allocated_lot_ids.add(selected_lot_id)

		# 2. Get lots already SUBMITTED (permanently consumed — exclude from results)
		submitted_lot_ids = set()
		try:
			submitted_records = JigCompleted.objects.filter(
				draft_status='submitted'
			).only('lot_id', 'is_multi_model', 'multi_model_allocation')
			for rec in submitted_records:
				submitted_lot_ids.add(rec.lot_id)
				if rec.is_multi_model and rec.multi_model_allocation:
					for m in rec.multi_model_allocation:
						if isinstance(m, dict):
							mlot = m.get('lot_id', '')
							if mlot:
								submitted_lot_ids.add(mlot)
		except Exception:
			logging.exception('[LOT_FETCH] Failed to read submitted lots')

		# 3. Get lots in OTHER drafts (not the current primary's draft)
		other_draft_lot_ids = set()
		try:
			other_drafts = JigCompleted.objects.filter(
				draft_status__in=['draft', 'active']
			).exclude(lot_id=primary_lot_id).only('lot_id', 'is_multi_model', 'multi_model_allocation')
			for rec in other_drafts:
				other_draft_lot_ids.add(rec.lot_id)
				if rec.is_multi_model and rec.multi_model_allocation:
					for m in rec.multi_model_allocation:
						if isinstance(m, dict):
							mlot = m.get('lot_id', '')
							if mlot:
								other_draft_lot_ids.add(mlot)
		except Exception:
			logging.exception('[LOT_FETCH] Failed to read other drafts')

		# 4. Fetch brass-audit-accepted lots, excluding submitted + other drafts + already allocated
		#    and restricting to backend-approved Add Model PSNs (micro group + same/ditto).
		exclude_ids = submitted_lot_ids | other_draft_lot_ids | allocated_lot_ids
		try:
			from django.db.models import Q as _Q
			from Jig_Loading.model_combination_validator import resolve_add_model_eligible_psns

			primary_psn = ''
			if primary_lot_id:
				primary_row = TotalStockModel.objects.filter(lot_id=primary_lot_id).values('batch_id__plating_stk_no').first()
				primary_psn = ((primary_row or {}).get('batch_id__plating_stk_no') or '').strip()
			if not primary_psn and primary_lot_id:
				primary_row = ModelMasterCreation.objects.filter(lot_id=primary_lot_id).values('plating_stk_no').first()
				primary_psn = ((primary_row or {}).get('plating_stk_no') or '').strip()

			eligible_psns = []
			if primary_psn:
				eligible_psns, invalid_selected = resolve_add_model_eligible_psns(primary_psn, [])
				if invalid_selected:
					eligible_psns = []
			base_qs = TotalStockModel.objects.filter(
				_Q(brass_audit_accptance=True) |
				_Q(brass_audit_few_cases_accptance=True, brass_audit_onhold_picking=False)
			).select_related('batch_id', 'batch_id__model_stock_no').exclude(jig_hold_lot=True)
			if primary_lot_id:
				if eligible_psns:
					base_qs = base_qs.filter(batch_id__plating_stk_no__in=eligible_psns)
				else:
					# Add-mode fails closed only when no backend-approved PSN can be resolved.
					base_qs = base_qs.none()
			if exclude_ids:
				base_qs = base_qs.exclude(lot_id__in=list(exclude_ids))
			lots = base_qs.order_by('-brass_audit_last_process_date_time')[:200]
		except Exception:
			logging.exception('[LOT_FETCH] Failed to query TotalStockModel')
			return Response({'status': 'error', 'message': 'Failed to fetch lots'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

		# 5. Build response with dynamically computed available_qty
		results = []
		for stock in lots:
			batch = getattr(stock, 'batch_id', None)
			total_qty = int(
				getattr(stock, 'brass_audit_accepted_qty', None)
				or getattr(stock, 'brass_audit_physical_qty', None)
				or getattr(stock, 'total_stock', 0) or 0
			)
			results.append({
				'lot_id': getattr(stock, 'lot_id', ''),
				'batch_id': getattr(batch, 'batch_id', '') if batch else '',
				'total_qty': total_qty,
				'used_qty': 0,
				'available_qty': total_qty,
				'plating_stk_no': getattr(batch, 'plating_stk_no', '') if batch else '',
				'polishing_stk_no': getattr(batch, 'polishing_stk_no', '') if batch else '',
				'plating_color': getattr(batch, 'plating_color', '') if batch else '',
				'polish_finish': getattr(batch, 'polish_finish', '') if batch else '',
				'model_stock_no': str(getattr(batch, 'model_stock_no', '')) if batch else '',
			})

		logging.info(json.dumps({
			'event': 'LOT_FETCH_API_RESULT',
			'mode': 'ADD',
			'primary_lot_id': primary_lot_id,
			'excluded_count': len(exclude_ids),
			'returned_count': len(results),
		}))

		return Response({
			'status': 'success',
			'mode': 'ADD',
			'lots': results,
		}, status=status.HTTP_200_OK)

	def _handle_default_mode(self, request):
		"""Default lot fetch — returns all available lots."""
		try:
			from django.db.models import Q as _Q
			base_qs = TotalStockModel.objects.filter(
				_Q(brass_audit_accptance=True) |
				_Q(brass_audit_few_cases_accptance=True, brass_audit_onhold_picking=False)
			).select_related('batch_id', 'batch_id__model_stock_no').exclude(jig_hold_lot=True)

			# Exclude submitted lots
			try:
				submitted_lot_ids = set(
					JigCompleted.objects.filter(draft_status='submitted').values_list('lot_id', flat=True)
				)
				if submitted_lot_ids:
					base_qs = base_qs.exclude(lot_id__in=list(submitted_lot_ids))
			except Exception:
				logging.exception('[LOT_FETCH] Failed to exclude submitted lots')

			lots = base_qs.order_by('-brass_audit_last_process_date_time')[:200]
			results = []
			for stock in lots:
				batch = getattr(stock, 'batch_id', None)
				total_qty = int(
					getattr(stock, 'brass_audit_accepted_qty', None)
					or getattr(stock, 'brass_audit_physical_qty', None)
					or getattr(stock, 'total_stock', 0) or 0
				)
				results.append({
					'lot_id': getattr(stock, 'lot_id', ''),
					'batch_id': getattr(batch, 'batch_id', '') if batch else '',
					'total_qty': total_qty,
					'available_qty': total_qty,
					'plating_stk_no': getattr(batch, 'plating_stk_no', '') if batch else '',
				})

			return Response({
				'status': 'success',
				'lots': results,
			}, status=status.HTTP_200_OK)
		except Exception as e:
			logging.exception(f'LotFetchAPI default mode error: {str(e)}')
			return Response({
				'status': 'error',
				'message': 'Unable to process the request. Please verify the submitted data and try again.',
			}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class ExcessTrayInfoAPI(APIView):
	"""GET /api/excess-tray-info/?batch_id=<batch_id>
	Returns half_filled_tray_info and half_filled_tray_qty from the most recent
	submitted JigCompleted record for the given batch_id.
	Used by the view icon on excess-flagged rows in the Jig Loading pick table.
	"""
	permission_classes = [IsAuthenticated]

	def get(self, request):
		batch_id = request.GET.get('batch_id', '').strip()
		if not batch_id:
			return JsonResponse({'success': False, 'error': 'batch_id is required'}, status=400)

		try:
			from django.db.models import Q
			jc = JigCompleted.objects.filter(
				batch_id=batch_id,
				draft_status='submitted',
			).filter(
				Q(half_filled_tray_qty__gt=0) | Q(excess_qty__gt=0)
			).order_by('-id').first()

			if not jc:
				return JsonResponse({
					'success': True, 'batch_id': batch_id,
					'trays': [], 'total_qty': 0,
					'message': 'No excess tray data found for this batch',
				})

			hf_info = jc.half_filled_tray_info or []
			total_qty = int(jc.half_filled_tray_qty or jc.excess_qty or 0)

			# Normalise: ensure each entry has expected keys
			trays = []
			for t in hf_info:
				if not isinstance(t, dict):
					continue
				trays.append({
					'tray_id': t.get('tray_id', ''),
					'qty': int(t.get('qty') or 0),
					'is_top_half_filled': bool(t.get('is_top_half_filled', False) or t.get('top_tray', False)),
					'model': t.get('model', ''),
				})

			# Fallback: if half_filled_tray_info is empty but excess data exists,
			# reconstruct from ExcessLotTray records (created during submit)
			if not trays and total_qty > 0:
				try:
					excess_lot_trays = ExcessLotTray.objects.filter(
						excess_lot__parent_batch_id=batch_id
					).order_by('id')
					for et in excess_lot_trays:
						trays.append({
							'tray_id': et.tray_id or '',
							'qty': int(et.qty or 0),
							'is_top_half_filled': bool(getattr(et, 'is_top_half_filled', False)),
							'model': getattr(et, 'model_code', '') or '',
						})
				except Exception:
					logging.exception('[EXCESS_TRAY_INFO] Fallback ExcessLotTray lookup failed')

			logging.info(f'[EXCESS_TRAY_INFO] batch_id={batch_id}, trays={len(trays)}, total_qty={total_qty}')
			return JsonResponse({
				'success': True,
				'batch_id': batch_id,
				'lot_id': jc.lot_id or '',
				'trays': trays,
				'total_qty': total_qty,
			})
		except Exception as e:
			logging.exception(f'[EXCESS_TRAY_INFO] Error for batch_id={batch_id}: {e}')
			return JsonResponse({'success': False, 'error': 'Server error'}, status=500)


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def jig_get_lot_id_for_tray(request):
	"""
	Get lot_id for a scanned tray_id or drafted jig_id in the Jig Loading pick table.
	Searches drafted Jig IDs first, then active tray drafts, JigLoadTrayId, BrassAudit tables, and TrayId as fallback.
	Only returns lot_ids that are brass-audit-accepted (visible in pick table).

	GET /jig_loading/api/get_lot_id_for_tray/?tray_id=<tray_id>
	"""
	scan_id = (request.GET.get('scan_id') or request.GET.get('tray_id') or '').strip()

	if not scan_id:
		return JsonResponse({'success': False, 'error': 'scan_id or tray_id parameter is required'})

	try:
		from django.db.models import Q as _Q
		from .selectors import (
			find_active_draft_by_jig_id,
			find_active_draft_by_scanned_tray,
			looks_like_jig_id,
			normalize_jig_id,
			normalize_tray_id,
		)

		tray_id = normalize_tray_id(scan_id)
		jig_id = normalize_jig_id(scan_id)

		def _lot_visible_in_jig_picktable(lot_id):
			return TotalStockModel.objects.filter(
				_Q(brass_audit_accptance=True) | _Q(brass_audit_few_cases_accptance=True),
				lot_id=lot_id
			).exists()

		if looks_like_jig_id(jig_id):
			active_jig_draft = find_active_draft_by_jig_id(jig_id, request.user)
			if active_jig_draft and active_jig_draft.lot_id and _lot_visible_in_jig_picktable(str(active_jig_draft.lot_id)):
				return JsonResponse({
					'success': True,
					'lot_id': str(active_jig_draft.lot_id),
					'batch_id': str(active_jig_draft.batch_id or ''),
					'source': getattr(active_jig_draft, 'source', active_jig_draft.__class__.__name__),
					'scan_type': 'jig_id',
					'jig_id': jig_id,
					'is_draft': True,
					'open_add_jig': True,
				})

		# Drafted delink scans may live only in JigLoadingManualDraft until submit.
		try:
			active_draft = find_active_draft_by_scanned_tray(tray_id, request.user)
			if active_draft and active_draft.lot_id and _lot_visible_in_jig_picktable(str(active_draft.lot_id)):
				return JsonResponse({
					'success': True,
					'lot_id': str(active_draft.lot_id),
					'batch_id': str(active_draft.batch_id or ''),
					'source': 'JigLoadingManualDraft',
					'is_draft': True,
					'open_add_jig': True,
				})
		except Exception as draft_lookup_error:
			logging.warning(f'jig_get_lot_id_for_tray draft lookup failed: {draft_lookup_error}')

		# Strategy 1: Check JigLoadTrayId table (most specific to Jig Loading)
		jig_tray = JigLoadTrayId.objects.filter(tray_id=tray_id).first()
		if jig_tray and jig_tray.lot_id:
			lot_id = str(jig_tray.lot_id)
			# Verify lot is in pick table (brass-audit-accepted)
			if _lot_visible_in_jig_picktable(lot_id):
				return JsonResponse({'success': True, 'lot_id': lot_id, 'source': 'JigLoadTrayId'})

		# Strategy 2: Check BrassAudit tables
		try:
			from BrassAudit.models import BrassAuditTrayId, BrassTrayId
			ba_tray = BrassAuditTrayId.objects.filter(tray_id=tray_id).first()
			if ba_tray and ba_tray.lot_id:
				lot_id = str(ba_tray.lot_id)
				if _lot_visible_in_jig_picktable(lot_id):
					return JsonResponse({'success': True, 'lot_id': lot_id, 'source': 'BrassAuditTrayId'})
			brass_tray = BrassTrayId.objects.filter(tray_id=tray_id).first()
			if brass_tray and brass_tray.lot_id:
				lot_id = str(brass_tray.lot_id)
				if _lot_visible_in_jig_picktable(lot_id):
					return JsonResponse({'success': True, 'lot_id': lot_id, 'source': 'BrassTrayId'})
		except ImportError:
			pass

		# Strategy 3: Check TrayId table (fallback)
		from modelmasterapp.models import TrayId
		tray_obj = TrayId.objects.filter(tray_id=tray_id).first()
		if tray_obj and tray_obj.lot_id:
			lot_id = str(tray_obj.lot_id)
			if _lot_visible_in_jig_picktable(lot_id):
				return JsonResponse({'success': True, 'lot_id': lot_id, 'source': 'TrayId'})

		return JsonResponse({'success': False, 'error': f'Scan ID {scan_id} not found in Jig Loading system'})

	except Exception as e:
		logging.exception(f'jig_get_lot_id_for_tray error: {str(e)}')
		return JsonResponse({'success': False, 'error': 'Unable to process the request. Please verify the submitted data and try again.'})

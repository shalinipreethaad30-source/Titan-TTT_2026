from datetime import timedelta
import json
from pathlib import Path
from unittest.mock import patch

from django.contrib.auth.models import User
from django.contrib.sessions.models import Session
from django.conf import settings
from django.core.cache import cache
from django.test import RequestFactory, SimpleTestCase, TestCase, override_settings
from django.utils import timezone

from . import global_scan as lookup
from Jig_Loading.models import JigCompleted, JigLoadingRecord, ExcessLotRecord, ExcessLotTray
from .global_scan import GlobalTraySearchView
from .models import UserActiveSession
from .services import _dashboard_cache_key, get_active_session_conflict_message, get_cached_dashboard_stats

@override_settings(
    CACHES={
        'default': {
            'BACKEND': 'django.core.cache.backends.locmem.LocMemCache',
            'LOCATION': 'adminportal-dashboard-tests',
        }
    }
)
class DashboardStatsCacheTests(SimpleTestCase):
    def setUp(self):
        cache.clear()

    def test_cache_only_mode_does_not_calculate_on_miss(self):
        with patch('adminportal.services.get_dashboard_stats_for_labels') as mocked_stats:
            stats = get_cached_dashboard_stats(
                allowed_module_names=['Data Upload'],
                calculate_on_miss=False,
            )

        self.assertEqual(stats, [])
        mocked_stats.assert_not_called()

    def test_cache_only_mode_returns_available_cached_stats(self):
        cached_stat = {
            'label': 'Day Planning',
            'total_lot': 5,
            'display_stats': [{'label': 'Total Batches', 'value': 5}],
        }
        cache.set(_dashboard_cache_key('Day Planning'), cached_stat, timeout=60)

        with patch('adminportal.services.get_dashboard_stats_for_labels') as mocked_stats:
            stats = get_cached_dashboard_stats(
                allowed_module_names=['Data Upload'],
                calculate_on_miss=False,
            )

        self.assertEqual(stats, [cached_stat])
        mocked_stats.assert_not_called()

    def test_cache_only_mode_skips_stale_cached_stats(self):
        cache.set(_dashboard_cache_key('Day Planning'), {'label': 'Day Planning'}, timeout=60)

        with patch('adminportal.services.get_dashboard_stats_for_labels') as mocked_stats:
            stats = get_cached_dashboard_stats(
                allowed_module_names=['Data Upload'],
                calculate_on_miss=False,
            )

        self.assertEqual(stats, [])
        mocked_stats.assert_not_called()


class ActiveSessionConflictMessageTests(TestCase):
    """
    Regression coverage for the false-positive "already active on another
    device" block on a genuinely first-ever login: login() always cycles the
    session key, so the pre-login anonymous key can never equal the
    just-created active session's key, even for the same tab. IP+User-Agent
    is the fallback signal that distinguishes a same-device retry (double
    submit / retried request) from an actual second device.
    """

    def setUp(self):
        self.user = User.objects.create_user(username='racer', password='X')
        self.factory = RequestFactory()
        Session.objects.create(
            session_key='freshly-cycled-key',
            session_data='',
            expire_date=timezone.now() + timedelta(minutes=15),
        )
        UserActiveSession.objects.create(
            user=self.user,
            session_key='freshly-cycled-key',
            ip_address='10.0.0.5',
            user_agent='TestBrowser/1.0',
            updated_at=timezone.now(),
        )

    def _request(self, ip='10.0.0.5', ua='TestBrowser/1.0'):
        req = self.factory.post('/accounts/login/')
        req.META['REMOTE_ADDR'] = ip
        req.META['HTTP_USER_AGENT'] = ua
        return req

    def test_same_device_double_submit_is_not_blocked(self):
        """Matching IP+UA (same browser retrying) must not be treated as a conflict."""
        message = get_active_session_conflict_message(
            self.user, current_session_key='pre-login-anon-key', request=self._request(),
        )
        self.assertIsNone(message)

    def test_different_device_is_still_blocked(self):
        """A different IP/User-Agent is a genuine second device and must still be rejected."""
        message = get_active_session_conflict_message(
            self.user,
            current_session_key='pre-login-anon-key',
            request=self._request(ip='203.0.113.9', ua='OtherBrowser/9.0'),
        )
        self.assertIsNotNone(message)

    def test_no_request_falls_back_to_prior_strict_behavior(self):
        """Without a request (e.g. non-HTTP callers), behavior is unchanged."""
        message = get_active_session_conflict_message(self.user, current_session_key='pre-login-anon-key')
        self.assertIsNotNone(message)


class GlobalTraySearchAccessTests(SimpleTestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.user = User(username='scan-user')
        self.user.id = 101

    def _post(self, payload=None):
        request = self.factory.post(
            '/adminportal/global_tray_search/',
            data=json.dumps(payload or {'tray_id': 'NB-A00045'}),
            content_type='application/json',
            HTTP_X_REQUESTED_WITH='XMLHttpRequest',
        )
        request.user = self.user
        return request

    def test_allowed_module_returns_navigation_payload(self):
        view = GlobalTraySearchView()
        resolved = {
            'module': 'Brass QC',
            'url': '/brass_qc/brass_picktable/',
            'lot_id': 'LOT-1',
            'batch_id': 'BATCH-1',
        }

        with patch.object(view, '_search_all_modules', return_value=resolved), \
             patch('adminportal.global_scan.is_admin_user', return_value=False), \
             patch('adminportal.global_scan.get_user_allowed_module_names', return_value=['Brass Qc Pick Table']):
            response = view.post(self._post())

        data = json.loads(response.content.decode())
        self.assertEqual(response.status_code, 200)
        self.assertTrue(data['success'])
        self.assertTrue(data['found'])
        self.assertEqual(data['url'], '/brass_qc/brass_picktable/')
        self.assertEqual(data['lot_id'], 'LOT-1')

    def test_inaccessible_module_returns_restricted_message_without_row_data(self):
        view = GlobalTraySearchView()
        resolved = {
            'module': 'Brass QC',
            'url': '/brass_qc/brass_picktable/',
            'lot_id': 'LOT-1',
            'batch_id': 'BATCH-1',
        }

        with patch.object(view, '_search_all_modules', return_value=resolved), \
             patch('adminportal.global_scan.is_admin_user', return_value=False), \
             patch('adminportal.global_scan.get_user_allowed_module_names', return_value=['Input Screening']):
            response = view.post(self._post())

        data = json.loads(response.content.decode())
        self.assertEqual(response.status_code, 403)
        self.assertFalse(data['success'])
        self.assertTrue(data['found'])
        self.assertTrue(data['restricted'])
        self.assertEqual(data['message'], "Currently it is available in 'Brass QC' module")
        self.assertNotIn('url', data)
        self.assertNotIn('lot_id', data)
        self.assertNotIn('batch_id', data)

    def test_unknown_tray_still_reports_not_exists(self):
        view = GlobalTraySearchView()

        with patch.object(view, '_search_all_modules', return_value=None):
            response = view.post(self._post({'tray_id': 'NB-DOES-NOT-EXIST'}))

        data = json.loads(response.content.decode())
        self.assertEqual(response.status_code, 200)
        self.assertFalse(data['success'])
        self.assertFalse(data['found'])
        self.assertEqual(data['message'], 'Not Exists')

    def test_jig_loading_completed_history_does_not_win_over_main_table(self):
        view = GlobalTraySearchView()

        with patch('adminportal.global_scan.find_active_excess_lot_by_tray', return_value=None), \
             patch.object(view, '_resolve_active_tray_lot_ids', return_value=({'LOT-1'}, set())), \
             patch.object(view, '_check_lot_in_inprocess_inspection', return_value={
                 'module': 'Inprocess Inspection',
                 'url': '/inprocess_inspection/main/',
                 'lot_id': 'LOT-1',
             }), \
             patch.object(view, '_check_lot_in_jig_loading', return_value={
                 'module': 'Jig Loading (Completed)',
                 'url': '/jig_loading/completed/',
                 'lot_id': 'LOT-1',
             }):
            result = view._search_all_modules(
                'NB-A00045',
                current_path='/inprocess_inspection/main/',
            )

        self.assertEqual(result['module'], 'Inprocess Inspection')
        self.assertEqual(result['url'], '/inprocess_inspection/main/')

    def test_released_tray_does_not_fall_back_to_historical_lifecycle(self):
        view = GlobalTraySearchView()

        with patch('adminportal.global_scan.find_active_excess_lot_by_tray', return_value=None), \
             patch.object(view, '_resolve_active_tray_lot_ids', return_value=(set(), set())), \
             patch.object(view, '_resolve_candidate_lot_ids') as historical_resolver:
            result = view._search_all_modules(
                'JB-A00431',
                current_path='/inprocess_inspection/main/',
            )

        self.assertIsNone(result)
        historical_resolver.assert_not_called()

    def test_reused_tray_uses_only_its_new_active_lot(self):
        view = GlobalTraySearchView()
        new_lot_result = {
            'module': 'Inprocess Inspection',
            'url': '/inprocess_inspection/main/',
            'lot_id': 'NEW-LOT',
        }

        with patch('adminportal.global_scan.find_active_excess_lot_by_tray', return_value=None), \
             patch.object(view, '_resolve_active_tray_lot_ids', return_value=({'NEW-LOT'}, set())), \
             patch.object(view, '_resolve_candidate_lot_ids') as historical_resolver, \
             patch.object(view, '_check_lot_in_inprocess_inspection', return_value=new_lot_result):
            result = view._search_all_modules(
                'JB-A00431',
                current_path='/inprocess_inspection/main/',
            )

        self.assertEqual(result, new_lot_result)
        historical_resolver.assert_not_called()

    def test_current_module_assignment_resolves_reused_tray(self):
        view = GlobalTraySearchView()
        active_jig_row = type('JigLoadTray', (), {
            'lot_id': 'CURRENT-LOT',
            'batch_id_id': 'BATCH-1',
        })()

        empty_models = (
            'modelmasterapp.models.TrayId.objects',
            'InputScreening.models.IPTrayId.objects',
            'Brass_QC.models.BrassTrayId.objects',
            'BrassAudit.models.BrassAuditTrayId.objects',
            'IQF.models.IQFTrayId.objects',
            'Jig_Unloading.models.JigUnload_TrayId.objects',
        )
        empty_patches = [patch(path) for path in empty_models]
        jig_loading_patch = patch('Jig_Loading.models.JigLoadTrayId.objects')
        mocked_models = [item.start() for item in empty_patches]
        jig_loading = jig_loading_patch.start()
        self.addCleanup(patch.stopall)

        for model in mocked_models:
            model.filter.return_value.exclude.return_value.only.return_value = []
        jig_loading.filter.return_value.exclude.return_value.only.return_value = [active_jig_row]

        lots, batches = view._resolve_active_tray_lot_ids('JB-A00431')

        self.assertEqual(lots, {'CURRENT-LOT'})
        self.assertEqual(batches, {'BATCH-1'})
        self.assertFalse(jig_loading.filter.call_args.kwargs['delink_tray'])
        self.assertFalse(jig_loading.filter.call_args.kwargs['rejected_tray'])

    def test_tray_scan_never_checks_nickel_wiping_history(self):
        view = GlobalTraySearchView()
        jig_loading_result = {
            'module': 'Jig Loading',
            'url': '/jig_loading/JigView/',
            'lot_id': 'CURRENT-LOT',
        }

        with patch('adminportal.global_scan.find_active_excess_lot_by_tray', return_value=None), \
             patch.object(view, '_resolve_active_tray_lot_ids', return_value=({'CURRENT-LOT'}, set())), \
             patch.object(view, '_check_lot_in_inprocess_inspection', return_value=None), \
             patch.object(view, '_check_lot_in_jig_unloading', return_value=None), \
             patch.object(view, '_check_lot_in_iqf', return_value=None), \
             patch.object(view, '_check_lot_in_brass_audit', return_value=None), \
             patch.object(view, '_check_lot_in_brass_qc', return_value=None), \
             patch.object(view, '_check_lot_in_input_screening', return_value=None), \
             patch.object(view, '_check_lot_in_day_planning', return_value=None), \
             patch.object(view, '_check_lot_in_jig_loading', return_value=jig_loading_result), \
             patch.object(view, '_check_lot_in_nickel_wiping') as nickel_wiping:
            result = view._search_all_modules(
                'JB-A00431', current_path='/jig_loading/JigView/',
            )

        self.assertEqual(result, jig_loading_result)
        nickel_wiping.assert_not_called()

    def test_jig_scan_keeps_its_active_jig_unloading_lookup(self):
        view = GlobalTraySearchView()
        active_result = {'module': 'Jig Unloading', 'lot_id': 'LOT-1'}

        with patch('adminportal.global_scan.find_active_excess_lot_by_tray', return_value=None), \
             patch.object(view, '_resolve_jig_unloading_by_jig_id', return_value=active_result) as resolver, \
             patch.object(view, '_resolve_active_tray_lot_ids') as tray_resolver:
            result = view._search_all_modules('J144-0001')

        self.assertEqual(result, active_result)
        resolver.assert_called_once_with('J144-0001')
        tray_resolver.assert_not_called()

    def test_completed_and_reject_tables_are_not_highlight_targets(self):
        view = GlobalTraySearchView()

        self.assertFalse(view._is_main_or_pick_result({
            'module': 'Brass QC (Completed)',
            'url': '/brass_qc/completed/',
        }))
        self.assertFalse(view._is_main_or_pick_result({
            'module': 'Input Screening (Reject)',
            'url': '/inputscreening/reject/',
        }))
        self.assertTrue(view._is_main_or_pick_result({
            'module': 'Input Screening',
            'url': '/inputscreening/picktable/',
        }))


class GlobalScanHighlightStyleTests(SimpleTestCase):
    def test_base_template_active_row_highlight_is_border_only(self):
        template_path = Path(settings.BASE_DIR) / 'static' / 'templates' / 'base.html'
        template = template_path.read_text(encoding='utf-8')
        highlight_block = template.split('Global active-row outline for scan/highlight classes.', 1)[1]
        highlight_block = highlight_block.split('/* Sidebar minimized', 1)[0]

        self.assertIn('border-top: 2px solid #e0a800', highlight_block)
        self.assertIn('border-bottom: 2px solid #e0a800', highlight_block)
        self.assertIn('border-left: 2px solid #e0a800', highlight_block)
        self.assertIn('border-right: 2px solid #e0a800', highlight_block)
        self.assertNotIn('background-color: #fff5bd', highlight_block)


class ExcessScanTests(TestCase):
    def setUp(self):
        self.user = User.objects.create(username='operator')
        self.source = JigCompleted.objects.create(
            user=self.user, lot_id='PARENT', batch_id='BATCH', jig_id='J144-0001',
            draft_status='submitted', half_filled_tray_qty=43,
            half_filled_tray_info=[
                {'tray_id': 'JB-A02552', 'qty': 7, 'is_top_half_filled': True},
                {'tray_id': 'JB-A02551', 'qty': 12},
                {'tray_id': 'JB-A02550', 'qty': 12},
                {'tray_id': 'JB-A02549', 'qty': 12},
            ],
        )
        record = JigLoadingRecord.objects.create(user=self.user, lot_id='PARENT', batch_id='BATCH')
        self.excess = ExcessLotRecord.objects.create(
            jig_loading_record=record, new_lot_id='EX-CURRENT', parent_lot_id='PARENT',
            parent_batch_id='BATCH', jig_id='J144-0001', lot_qty=43,
        )
        ExcessLotTray.objects.create(excess_lot=self.excess, lot_id='EX-CURRENT', tray_id='JB-A02552', qty=7)

    def test_top_and_full_excess_trays_resolve_to_excess_not_parent(self):
        for tray in ['JB-A02552', 'JB-A02551', 'JB-A02550', 'JB-A02549']:
            with self.subTest(tray=tray):
                result = lookup.find_active_excess_lot_by_tray([tray])
                self.assertEqual(result['lot_id'], 'EX-CURRENT')
                self.assertEqual(result['batch_id'], 'BATCH')

    def test_normalization_and_unknown_tray(self):
        self.assertIsNotNone(lookup.find_active_excess_lot_by_tray([' jb-a02552\r\n']))
        self.assertIsNone(lookup.find_active_excess_lot_by_tray(['JB-A99999']))

    def test_snapshot_without_excess_tray_row(self):
        ExcessLotTray.objects.all().delete()
        self.assertEqual(lookup.find_active_excess_lot_by_tray(['JB-A02552'])['lot_id'], 'EX-CURRENT')

    def test_table_fallback_when_snapshot_empty(self):
        self.source.half_filled_tray_info = []
        self.source.save()
        self.assertEqual(lookup.find_active_excess_lot_by_tray(['JB-A02552'])['lot_id'], 'EX-CURRENT')

    def test_consumed_primary_excess_is_not_active(self):
        JigCompleted.objects.create(user=self.user, lot_id='EX-CURRENT', batch_id='NEXT', draft_status='submitted')
        self.assertIsNone(lookup.find_active_excess_lot_by_tray(['JB-A02552']))

    def test_consumed_secondary_excess_is_not_active(self):
        JigCompleted.objects.create(user=self.user, lot_id='OTHER', batch_id='NEXT', draft_status='submitted',
            is_multi_model=True, multi_model_allocation=[{'lot_id': 'EX-CURRENT'}])
        self.assertIsNone(lookup.find_active_excess_lot_by_tray(['JB-A02552']))

    def test_draft_consumption_keeps_excess_active(self):
        JigCompleted.objects.create(user=self.user, lot_id='EX-CURRENT', batch_id='NEXT', draft_status='draft')
        self.assertIsNotNone(lookup.find_active_excess_lot_by_tray(['JB-A02552']))

    def test_source_must_be_submitted_with_excess(self):
        for changes in [{'draft_status': 'draft'}, {'draft_status': 'submitted', 'half_filled_tray_qty': 0}]:
            JigCompleted.objects.filter(pk=self.source.pk).update(**changes)
            self.assertIsNone(lookup.find_active_excess_lot_by_tray(['JB-A02552']))

    def test_legacy_snapshot_without_excess_record(self):
        ExcessLotTray.objects.all().delete()
        ExcessLotRecord.objects.all().delete()
        self.assertEqual(lookup.find_active_excess_lot_by_tray(['JB-A02552'])['lot_id'], 'PARENT')

    def test_legacy_multi_model_snapshot_uses_source_lot(self):
        ExcessLotTray.objects.all().delete()
        ExcessLotRecord.objects.all().delete()
        self.source.is_multi_model = True
        self.source.draft_data = {'tray_data': [{'tray_id': 'JB-A02552', 'source_lot_id': 'SECONDARY'}]}
        self.source.save()
        self.assertEqual(lookup.find_active_excess_lot_by_tray(['JB-A02552'])['lot_id'], 'SECONDARY')

    def test_global_scan_uses_excess_before_historical_candidates(self):
        result = GlobalTraySearchView()._search_all_modules('JB-A02552')
        self.assertEqual(result['lot_id'], 'EX-CURRENT')
        self.assertEqual(result['module'], 'Jig Loading')

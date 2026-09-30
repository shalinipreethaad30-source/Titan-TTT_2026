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
        # Keep selector tests isolated from the database unless a test explicitly
        # supplies a Nickel Audit lookup result.
        self.nickel_audit_resolver = patch.object(
            GlobalTraySearchView, '_resolve_nickel_audit_lot_ids', return_value=[]
        )
        self.nickel_audit_resolver.start()
        self.addCleanup(self.nickel_audit_resolver.stop)
        self.nickel_wiping_resolver = patch.object(
            GlobalTraySearchView, '_resolve_active_nickel_wiping_lot_ids', return_value=[]
        )
        self.nickel_wiping_resolver.start()
        self.addCleanup(self.nickel_wiping_resolver.stop)
        self.inprocess_jig_resolver = patch.object(
            GlobalTraySearchView, '_resolve_inprocess_by_jig_id', return_value=None
        )
        self.inprocess_jig_resolver.start()
        self.addCleanup(self.inprocess_jig_resolver.stop)
        self.spider_spindle_z1 = patch.object(GlobalTraySearchView, '_check_lot_in_ss_z1', return_value=None)
        self.spider_spindle_z2 = patch.object(GlobalTraySearchView, '_check_lot_in_ss_z2', return_value=None)
        self.spider_spindle_z1.start()
        self.spider_spindle_z2.start()
        self.addCleanup(self.spider_spindle_z1.stop)
        self.addCleanup(self.spider_spindle_z2.stop)

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
             patch.object(view, '_resolve_active_nickel_wiping_lot_ids', return_value=[]), \
             patch.object(view, '_resolve_active_brass_audit_lot_ids', return_value=[]), \
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
             patch.object(view, '_resolve_active_nickel_wiping_lot_ids', return_value=[]), \
             patch.object(view, '_resolve_active_brass_audit_lot_ids', return_value=[]), \
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
             patch.object(view, '_resolve_active_nickel_wiping_lot_ids', return_value=[]), \
             patch.object(view, '_resolve_active_brass_audit_lot_ids', return_value=[]), \
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
             patch.object(view, '_resolve_active_nickel_wiping_lot_ids', return_value=[]), \
             patch.object(view, '_resolve_active_brass_audit_lot_ids', return_value=[]), \
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

    def test_active_nickel_wiping_identifier_uses_its_pick_table(self):
        view = GlobalTraySearchView()
        nickel_result = {
            'module': 'Nickel Wiping',
            'url': '/nickel_inspection/Nickel_Inspection/',
            'lot_id': 'NR-CURRENT',
        }

        with patch('adminportal.global_scan.find_active_excess_lot_by_tray', return_value=None), \
             patch.object(view, '_resolve_active_nickel_wiping_lot_ids', return_value=['NR-CURRENT']), \
             patch.object(view, '_check_lot_in_nickel_wiping', return_value=nickel_result), \
             patch.object(view, '_check_lot_in_nickel_wiping_z2', return_value=None), \
             patch.object(view, '_resolve_active_tray_lot_ids') as normal_tray_resolver:
            result = view._search_all_modules(
                'NR-A00045', current_path='/nickel_inspection/Nickel_Inspection/',
            )

        self.assertEqual(result, nickel_result)
        normal_tray_resolver.assert_not_called()

    def test_jl_tray_in_nickel_wiping_uses_nickel_pick_table_not_jig_lookup(self):
        view = GlobalTraySearchView()
        nickel_result = {
            'module': 'Nickel Wiping Z2',
            'url': '/nickle_inspection_zone_two/NQ_Zone_PickTable/',
            'lot_id': 'NW-CURRENT',
        }

        with patch('adminportal.global_scan.find_active_excess_lot_by_tray', return_value=None), \
             patch.object(view, '_resolve_active_nickel_wiping_lot_ids', return_value=['NW-CURRENT']), \
             patch.object(view, '_check_lot_in_nickel_wiping', return_value=None), \
             patch.object(view, '_check_lot_in_nickel_wiping_z2', return_value=nickel_result), \
             patch.object(view, '_resolve_active_brass_audit_lot_ids', return_value=[]), \
             patch.object(view, '_resolve_active_tray_lot_ids', return_value=(set(), set())), \
             patch.object(view, '_resolve_jig_unloading_by_jig_id') as jig_lookup:
            result = view._search_all_modules('JL-A00121')

        self.assertEqual(result, nickel_result)
        jig_lookup.assert_not_called()

    def test_inactive_nickel_history_does_not_block_current_reused_tray(self):
        view = GlobalTraySearchView()
        current_result = {
            'module': 'Input Screening',
            'url': '/inputscreening/IS_PickTable/',
            'lot_id': 'REUSED-CURRENT',
        }

        with patch('adminportal.global_scan.find_active_excess_lot_by_tray', return_value=None), \
             patch.object(view, '_resolve_active_nickel_wiping_lot_ids', return_value=['NICKEL-OLD']), \
             patch.object(view, '_check_lot_in_nickel_wiping', return_value=None), \
             patch.object(view, '_check_lot_in_nickel_wiping_z2', return_value=None), \
             patch.object(view, '_resolve_active_brass_audit_lot_ids', return_value=[]), \
             patch.object(view, '_resolve_active_tray_lot_ids', return_value=({'REUSED-CURRENT'}, set())), \
             patch.object(view, '_check_lot_in_inprocess_inspection', return_value=None), \
             patch.object(view, '_check_lot_in_jig_unloading', return_value=None), \
             patch.object(view, '_check_lot_in_iqf', return_value=None), \
             patch.object(view, '_check_lot_in_brass_audit', return_value=None), \
             patch.object(view, '_check_lot_in_brass_qc', return_value=None), \
             patch.object(view, '_check_lot_in_input_screening', return_value=current_result):
            result = view._search_all_modules(
                'JB-A02667', current_path='/inputscreening/IS_PickTable/',
            )

        self.assertEqual(result, current_result)

    def test_reused_normal_tray_beats_prior_active_nickel_wiping_lot(self):
        view = GlobalTraySearchView()
        nickel_result = {
            'module': 'Nickel Wiping',
            'url': '/nickle_inspection/Nickel_Inspection/',
            'lot_id': 'NICKEL-OLD',
        }
        current_result = {
            'module': 'Input Screening',
            'url': '/inputscreening/IS_PickTable/',
            'lot_id': 'REUSED-CURRENT',
        }

        with patch('adminportal.global_scan.find_active_excess_lot_by_tray', return_value=None), \
             patch.object(view, '_resolve_active_nickel_wiping_lot_ids', return_value=['NICKEL-OLD']), \
             patch.object(view, '_check_lot_in_nickel_wiping', return_value=nickel_result), \
             patch.object(view, '_check_lot_in_nickel_wiping_z2', return_value=None), \
             patch.object(view, '_resolve_active_brass_audit_lot_ids', return_value=[]), \
             patch.object(view, '_resolve_active_tray_lot_ids', return_value=({'REUSED-CURRENT'}, set())), \
             patch.object(view, '_check_lot_in_inprocess_inspection', return_value=None), \
             patch.object(view, '_check_lot_in_jig_unloading', return_value=None), \
             patch.object(view, '_check_lot_in_iqf', return_value=None), \
             patch.object(view, '_check_lot_in_brass_audit', return_value=None), \
             patch.object(view, '_check_lot_in_brass_qc', return_value=None), \
             patch.object(view, '_check_lot_in_input_screening', return_value=current_result):
            result = view._search_all_modules(
                'JB-A00431', current_path='/inputscreening/IS_PickTable/',
            )

        self.assertEqual(result, current_result)

    def test_active_brass_audit_snapshot_tray_uses_its_pick_table(self):
        view = GlobalTraySearchView()
        brass_audit_result = {
            'module': 'Brass Audit',
            'url': '/brass_audit/brass_audit_picktable/',
            'lot_id': 'BA-SNAPSHOT-LOT',
        }

        with patch('adminportal.global_scan.find_active_excess_lot_by_tray', return_value=None), \
             patch.object(view, '_resolve_active_nickel_wiping_lot_ids', return_value=[]), \
             patch.object(view, '_resolve_active_brass_audit_lot_ids', return_value=['BA-SNAPSHOT-LOT']), \
             patch.object(view, '_check_lot_in_brass_audit', return_value=brass_audit_result), \
             patch.object(view, '_resolve_active_tray_lot_ids') as normal_tray_resolver:
            result = view._search_all_modules(
                'NB-A00101', current_path='/brass_audit/brass_audit_picktable/',
            )

        self.assertEqual(result, brass_audit_result)
        normal_tray_resolver.assert_not_called()

    def test_active_nickel_audit_tray_uses_its_pick_table(self):
        view = GlobalTraySearchView()
        nickel_audit_result = {
            'module': 'Nickel Audit',
            'url': '/nickel_audit/NA_PickTable/',
            'lot_id': 'NA-CURRENT',
        }

        with patch('adminportal.global_scan.find_active_excess_lot_by_tray', return_value=None), \
             patch.object(view, '_resolve_active_nickel_wiping_lot_ids', return_value=[]), \
             patch.object(view, '_resolve_nickel_audit_lot_ids', return_value=['NA-CURRENT']), \
             patch.object(view, '_check_lot_in_nickel_audit_z1', return_value=nickel_audit_result), \
             patch.object(view, '_check_lot_in_nickel_audit_z2', return_value=None), \
             patch.object(view, '_resolve_active_brass_audit_lot_ids', return_value=[]), \
             patch.object(view, '_resolve_active_tray_lot_ids', return_value=(set(), set())):
            result = view._search_all_modules(
                'NA-A00001', current_path='/nickel_audit/NA_PickTable/',
            )

        self.assertEqual(result, nickel_audit_result)

    def test_nickel_specific_tray_skips_completed_audit_record_for_active_pick_row(self):
        view = GlobalTraySearchView()
        completed_result = {
            'module': 'Nickel Audit (Completed)',
            'url': '/nickel_audit/NA_Completed/',
            'lot_id': 'OLD-LOT',
        }
        active_result = {
            'module': 'Nickel Audit',
            'url': '/nickel_audit/NA_PickTable/',
            'lot_id': 'CURRENT-LOT',
        }

        with patch('adminportal.global_scan.find_active_excess_lot_by_tray', return_value=None), \
             patch.object(view, '_resolve_active_nickel_wiping_lot_ids', return_value=[]), \
             patch.object(view, '_resolve_nickel_audit_lot_ids', return_value=['OLD-LOT', 'CURRENT-LOT']), \
             patch.object(view, '_check_lot_in_nickel_audit_z1', side_effect=[completed_result, active_result]), \
             patch.object(view, '_check_lot_in_nickel_audit_z2', return_value=None):
            result = view._search_all_modules('JR-A00121')

        self.assertEqual(result, active_result)

    def test_nickel_specific_tray_uses_active_spider_spindle_pick_table(self):
        view = GlobalTraySearchView()
        spider_result = {
            'module': 'Spider Spindle Z1',
            'url': '/spider_spindle/ss_z1_pick_table/',
            'lot_id': 'SS-CURRENT',
        }

        with patch('adminportal.global_scan.find_active_excess_lot_by_tray', return_value=None), \
             patch.object(view, '_resolve_active_nickel_wiping_lot_ids', return_value=[]), \
             patch.object(view, '_resolve_nickel_audit_lot_ids', return_value=['SS-CURRENT']), \
             patch.object(view, '_check_lot_in_nickel_audit_z1', return_value=None), \
             patch.object(view, '_check_lot_in_nickel_audit_z2', return_value=None), \
             patch.object(view, '_check_lot_in_ss_z1', return_value=spider_result), \
             patch.object(view, '_check_lot_in_ss_z2', return_value=None):
            result = view._search_all_modules('JR-A00121')

        self.assertEqual(result, spider_result)

    def test_jl_tray_in_spider_spindle_uses_spider_pick_table_not_jig_lookup(self):
        view = GlobalTraySearchView()
        spider_result = {
            'module': 'Spider Spindle Z1',
            'url': '/spider_spindle/ss_z1_pick_table/',
            'lot_id': 'SS-CURRENT',
        }

        with patch('adminportal.global_scan.find_active_excess_lot_by_tray', return_value=None), \
             patch.object(view, '_resolve_active_nickel_wiping_lot_ids', return_value=['SS-CURRENT']), \
             patch.object(view, '_check_lot_in_nickel_wiping', return_value=None), \
             patch.object(view, '_check_lot_in_nickel_wiping_z2', return_value=None), \
             patch.object(view, '_resolve_nickel_audit_lot_ids', return_value=['SS-CURRENT']), \
             patch.object(view, '_check_lot_in_nickel_audit_z1', return_value=None), \
             patch.object(view, '_check_lot_in_nickel_audit_z2', return_value=None), \
             patch.object(view, '_check_lot_in_ss_z1', return_value=spider_result), \
             patch.object(view, '_check_lot_in_ss_z2', return_value=None), \
             patch.object(view, '_resolve_active_brass_audit_lot_ids', return_value=[]), \
             patch.object(view, '_resolve_active_tray_lot_ids', return_value=(set(), set())), \
             patch.object(view, '_resolve_jig_unloading_by_jig_id') as jig_lookup:
            result = view._search_all_modules('JL-A00121')

        self.assertEqual(result, spider_result)
        jig_lookup.assert_not_called()

    def test_jig_loading_draft_is_used_when_jig_is_not_yet_in_unloading(self):
        view = GlobalTraySearchView()
        draft = type('JigDraft', (), {'lot_id': 'JIG-DRAFT-LOT'})()
        jig_loading_result = {
            'module': 'Jig Loading',
            'url': '/jig_loading/JigView/',
            'lot_id': 'JIG-DRAFT-LOT',
        }

        with patch('adminportal.global_scan.find_active_excess_lot_by_tray', return_value=None), \
             patch.object(view, '_resolve_jig_unloading_by_jig_id', return_value=None), \
             patch('Jig_Loading.selectors.find_active_draft_by_jig_id', return_value=draft) as draft_resolver, \
             patch.object(view, '_check_lot_in_jig_loading', return_value=jig_loading_result):
            result = view._search_all_modules('J144-0001', user=self.user)

        self.assertEqual(result, jig_loading_result)
        draft_resolver.assert_called_once_with('J144-0001', user=self.user)

    def test_jig_id_in_inprocess_uses_inprocess_pick_table_before_jig_unloading(self):
        view = GlobalTraySearchView()
        inprocess_result = {
            'module': 'Inprocess Inspection',
            'url': '/inprocess_inspection/inprocess_inspection_main/',
            'lot_id': 'IP-CURRENT',
            'jig_id': 'J144-0001',
        }

        with patch('adminportal.global_scan.find_active_excess_lot_by_tray', return_value=None), \
             patch.object(view, '_resolve_active_nickel_wiping_lot_ids', return_value=[]), \
             patch.object(view, '_resolve_inprocess_by_jig_id', return_value=inprocess_result), \
             patch.object(view, '_resolve_jig_unloading_by_jig_id') as jig_unloading_lookup:
            result = view._search_all_modules('J144-0001')

        self.assertEqual(result, inprocess_result)
        jig_unloading_lookup.assert_not_called()

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
    def test_nickel_wiping_scan_uses_exact_resolved_lot_for_highlight(self):
        template_path = Path(settings.BASE_DIR) / 'static' / 'templates' / 'base.html'
        template = template_path.read_text(encoding='utf-8')
        row_match_block = template.split('function rowMatchesContext(row, context)', 1)[1]
        row_match_block = row_match_block.split('function findRowInRoot', 1)[0]

        self.assertIn("context.responseData.module === 'Nickel Wiping'", row_match_block)
        self.assertIn("context.responseData.module === 'Nickel Wiping Z2'", row_match_block)
        self.assertIn("row.getAttribute('data-stock-lot-id')", row_match_block)

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

    def test_cross_module_highlighter_waits_for_rows_and_keeps_exact_lot(self):
        template_path = Path(settings.BASE_DIR) / 'static' / 'templates' / 'base.html'
        template = template_path.read_text(encoding='utf-8')
        auto_highlight_block = template.split('AUTO-HIGHLIGHT ROW: page-hunt', 1)[1]

        self.assertIn('Do not highlight a', auto_highlight_block)
        self.assertIn('renderWaitAttempts < 12', auto_highlight_block)
        self.assertIn('highlightObserver.observe(document.body', auto_highlight_block)
        self.assertIn("row.setAttribute('data-global-scan-active', '1')", auto_highlight_block)
        self.assertIn("window.addEventListener('load'", auto_highlight_block)


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

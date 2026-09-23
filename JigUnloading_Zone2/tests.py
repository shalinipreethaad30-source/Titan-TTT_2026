import json

from django.contrib.auth.models import User
from django.test import TestCase

from Jig_Unloading.models import JUSubmittedZ1
from modelmasterapp.models import TrayId


class JigUnloadingZone2TrayOccupancyRegressionTests(TestCase):
	def setUp(self):
		self.user = User.objects.create_user(username='zone2-user', password='pass')
		self.client.force_login(self.user)
		JUSubmittedZ1.objects.create(
			jig_completed_id=98,
			jig_qr_id='J098-0004',
			model_no='1805NAA02',
			lot_id='LID-SAME-FAMILY',
			total_qty=38,
			tray_data=[
				{'tray_id': 'JB-A00201', 'qty': 12, 'slot': 1},
				{'tray_id': 'JB-A00202', 'qty': 12, 'slot': 2},
				{'tray_id': 'JB-A00203', 'qty': 12, 'slot': 3},
				{'tray_id': 'JB-A00204', 'qty': 2, 'slot': 4},
			],
			is_draft=False,
		)
		TrayId.objects.create(
			tray_id='JB-A00202',
			lot_id=None,
			tray_quantity=12,
			scanned=False,
			delink_tray=True,
		)

	def test_validate_rejects_saved_tray_owned_by_same_lot_sibling_model(self):
		response = self.client.post(
			'/JigUnloading_Zone2/JU_Zone_validate_tray_id/',
			data=json.dumps({
				'tray_id': 'JB-A00202',
				'lot_id': 'LID-SAME-FAMILY',
				'model_no': '1805NAR02',
				'jig_completed_id': 98,
				'jig_id': 'J098-0004',
			}),
			content_type='application/json',
		)

		self.assertEqual(response.status_code, 200)
		payload = response.json()
		self.assertFalse(payload['success'])
		self.assertEqual(payload['validation_type'], 'tray_occupied')
		self.assertEqual(payload['source'], 'Jig Unloading model save')

	def test_save_and_back_rejects_saved_tray_when_frontend_validation_is_bypassed(self):
		response = self.client.post(
			'/jig_unloading/api/save_model_unload_z1/',
			data=json.dumps({
				'jig_completed_id': 98,
				'lot_id': 'LID-SAME-FAMILY',
				'model_no': '1805NAR02',
				'total_qty': 60,
				'missing_qty': 0,
				'tray_data': [
					{'tray_id': 'JB-A00202', 'qty': 60, 'slot': 1, 'is_top_tray': True},
				],
				'is_draft': False,
				'merged_lots': [],
			}),
			content_type='application/json',
		)

		self.assertEqual(response.status_code, 400)
		payload = response.json()
		self.assertEqual(payload['validation_type'], 'tray_occupied')
		self.assertEqual(payload['source'], 'Jig Unloading model save')

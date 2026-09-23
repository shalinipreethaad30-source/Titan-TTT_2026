from django.test import TestCase
from django.contrib.auth.models import User

from Jig_Unloading.models import JigUnloadAutoSave, JigUnloadDraft, JUSubmittedZ1
from Jig_Unloading.tray_utils import find_jig_unload_tray_conflict
from modelmasterapp.models import TrayId


class JigUnloadTrayOccupancyTests(TestCase):
	def test_model_save_reserves_tray_for_other_lots(self):
		JUSubmittedZ1.objects.create(
			jig_completed_id=2,
			jig_qr_id='J144-0001',
			model_no='2617SAA02',
			lot_id='LID180520260831540002',
			total_qty=144,
			tray_data=[{'tray_id': 'NR-A00001', 'qty': 4, 'slot': 1}],
			is_draft=False,
		)

		conflict = find_jig_unload_tray_conflict(
			'NR-A00001',
			allowed_lot_ids=['LID_OTHER'],
		)
		self.assertIsNotNone(conflict)
		self.assertEqual(conflict['linked_lot'], 'LID180520260831540002')

		same_lot_conflict = find_jig_unload_tray_conflict(
			'NR-A00001',
			allowed_lot_ids=['LID180520260831540002'],
		)
		self.assertIsNotNone(same_lot_conflict)
		self.assertEqual(same_lot_conflict['source'], 'Jig Unloading model save')

		same_assignment_conflict = find_jig_unload_tray_conflict(
			'NR-A00001',
			allowed_lot_ids=['LID180520260831540002'],
			current_assignment={
				'jig_completed_id': 2,
				'lot_id': 'LID180520260831540002',
				'model_no': '2617SAA02',
			},
		)
		self.assertIsNone(same_assignment_conflict)

	def test_saved_model_blocks_same_lot_different_model_regression(self):
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

		conflict = find_jig_unload_tray_conflict(
			'JB-A00202',
			allowed_lot_ids=['LID-SAME-FAMILY'],
			current_assignment={
				'jig_completed_id': 98,
				'lot_id': 'LID-SAME-FAMILY',
				'model_no': '1805NAR02',
			},
		)

		self.assertIsNotNone(conflict)
		self.assertEqual(conflict['source'], 'Jig Unloading model save')

	def test_saved_model_resume_allows_same_assignment(self):
		JUSubmittedZ1.objects.create(
			jig_completed_id=98,
			jig_qr_id='J098-0004',
			model_no='1805NAA02',
			lot_id='LID-SAME-FAMILY',
			total_qty=38,
			tray_data=[{'tray_id': 'JB-A00202', 'qty': 12, 'slot': 2}],
			is_draft=False,
		)

		conflict = find_jig_unload_tray_conflict(
			'JB-A00202',
			allowed_lot_ids=['LID-SAME-FAMILY'],
			current_assignment={
				'jig_completed_id': 98,
				'lot_id': 'LID-SAME-FAMILY',
				'model_no': '1805NAA02',
			},
		)

		self.assertIsNone(conflict)

	def test_draft_and_autosave_reserve_valid_trays(self):
		user = User.objects.create_user(username='autosave-user')
		JigUnloadDraft.objects.create(
			main_lot_id='LID_DRAFT',
			model_number='MODEL-D',
			total_quantity=20,
			draft_data={'tray_data': [{'tray_id': 'ND-A00002', 'tray_qty': 20}]},
			combined_lot_ids=['LID_DRAFT'],
		)
		JigUnloadAutoSave.objects.create(
			user=user,
			session_key='test-session',
			main_lot_id='LID_AUTOSAVE',
			model_number='MODEL-A',
			total_quantity=20,
			tray_data=[{'tray_id': 'JD-A00003', 'tray_qty': 20}],
			combined_lot_ids=['LID_AUTOSAVE'],
			jig_id='JIG-A',
		)

		draft_conflict = find_jig_unload_tray_conflict(
			'ND-A00002',
			allowed_lot_ids=['LID_DRAFT'],
			current_assignment={
				'main_lot_id': 'LID_DRAFT',
				'model_number': 'MODEL-X',
			},
		)
		autosave_conflict = find_jig_unload_tray_conflict(
			'JD-A00003',
			allowed_lot_ids=['LID_AUTOSAVE'],
			current_assignment={
				'main_lot_id': 'LID_AUTOSAVE',
				'model_number': 'MODEL-B',
				'jig_id': 'JIG-A',
				'user_id': user.id,
			},
		)

		self.assertEqual(draft_conflict['linked_lot'], 'LID_DRAFT')
		self.assertEqual(autosave_conflict['linked_lot'], 'LID_AUTOSAVE')

		same_draft = find_jig_unload_tray_conflict(
			'ND-A00002',
			allowed_lot_ids=['LID_DRAFT'],
			current_assignment={
				'main_lot_id': 'LID_DRAFT',
				'model_number': 'MODEL-D',
			},
		)
		same_autosave = find_jig_unload_tray_conflict(
			'JD-A00003',
			allowed_lot_ids=['LID_AUTOSAVE'],
			current_assignment={
				'main_lot_id': 'LID_AUTOSAVE',
				'model_number': 'MODEL-A',
				'jig_id': 'JIG-A',
				'user_id': user.id,
			},
		)

		self.assertIsNone(same_draft)
		self.assertIsNone(same_autosave)

	def test_saved_json_owner_blocks_even_when_master_is_delinked(self):
		JUSubmittedZ1.objects.create(
			jig_completed_id=2,
			jig_qr_id='J144-0001',
			model_no='2617SAA02',
			lot_id='LID_RELEASED',
			total_qty=20,
			tray_data=[{'tray_id': 'NR-A00999', 'qty': 20}],
			is_draft=False,
		)
		TrayId.objects.create(
			tray_id='NR-A00999',
			lot_id='LID_RELEASED',
			tray_quantity=20,
			delink_tray=True,
			scanned=False,
		)

		conflict = find_jig_unload_tray_conflict(
			'NR-A00999',
			allowed_lot_ids=['LID_OTHER'],
			include_tray_master=True,
			current_assignment={
				'jig_completed_id': 2,
				'lot_id': 'LID_OTHER',
				'model_no': '2617SAB02',
			},
		)

		self.assertIsNotNone(conflict)
		self.assertEqual(conflict['source'], 'Jig Unloading model save')

	def test_delinked_master_still_allows_exact_saved_assignment_resume(self):
		JUSubmittedZ1.objects.create(
			jig_completed_id=2,
			jig_qr_id='J144-0001',
			model_no='2617SAA02',
			lot_id='LID_RELEASED',
			total_qty=20,
			tray_data=[{'tray_id': 'NR-A00998', 'qty': 20}],
			is_draft=False,
		)
		TrayId.objects.create(
			tray_id='NR-A00998',
			lot_id=None,
			tray_quantity=20,
			delink_tray=True,
			scanned=False,
		)

		conflict = find_jig_unload_tray_conflict(
			'NR-A00998',
			allowed_lot_ids=['LID_RELEASED'],
			include_tray_master=True,
			current_assignment={
				'jig_completed_id': 2,
				'lot_id': 'LID_RELEASED',
				'model_no': '2617SAA02',
			},
		)

		self.assertIsNone(conflict)

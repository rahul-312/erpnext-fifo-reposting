"""Run: python3 -m unittest discover -s tests -t .   (from the erpnext-fifo-reposting folder)"""

import copy
import unittest

from fifo_repost.engine import FIFOQueue, NegativeStockError, build_assignment_scenario, naive_delta_patch


def sle(L, voucher_no, item_code="Milk"):
	return next(s for s in L.sles if s.voucher_no == voucher_no and s.item_code == item_code)


class TestFIFOQueue(unittest.TestCase):
	def test_consumes_oldest_first(self):
		q = FIFOQueue([[100, 50], [50, 80]])
		self.assertEqual(q.remove_stock(120), 100 * 50 + 20 * 80)
		self.assertEqual(q.queue, [[30, 80]])

	def test_negative_stock(self):
		with self.assertRaises(NegativeStockError):
			FIFOQueue([[1, 10]]).remove_stock(2)


class TestAssignmentScenario(unittest.TestCase):
	def setUp(self):
		self.before = build_assignment_scenario()
		self.after = copy.deepcopy(self.before)
		self.job = self.after.update_purchase_receipt_rate("PR-0110", "Milk", 80)
		self.after.process_repost_queue()

	def test_original_values(self):
		b = self.before
		self.assertEqual(sle(b, "DN-0115").stock_value_difference, -4000)
		self.assertEqual(sle(b, "SE-0121").stock_value_difference, -5800)
		self.assertEqual(sle(b, "SE-0121", "Milk Powder").incoming_rate, 580)
		self.assertEqual(b.cogs("DN-0125"), 5800)

	def test_pr_itself_revalued(self):
		self.assertEqual(sle(self.after, "PR-0110").stock_value, 9000)
		self.assertEqual(sle(self.after, "PR-0110").stock_queue, [[100, 50], [50, 80]])

	def test_dn_0115_value_unchanged_but_balance_rewritten(self):
		s = sle(self.after, "DN-0115")
		self.assertEqual(s.stock_value_difference, -4000)  # still the @50 opening batch
		self.assertEqual(s.stock_value, 5000)  # was 4000
		self.assertEqual(s.stock_queue, [[20, 50], [50, 80]])
		self.assertNotIn("DN-0115", self.job.affected_vouchers)  # GL untouched

	def test_pr_0120_balance_rewritten(self):
		self.assertEqual(sle(self.after, "PR-0120").stock_value, 8000)

	def test_manufacture_consumes_80_batch(self):
		self.assertEqual(sle(self.after, "SE-0121").stock_value_difference, -6800)  # 20@50 + 50@80 + 30@60
		self.assertEqual(sle(self.after, "SE-0121", "Milk Powder").incoming_rate, 680)

	def test_cross_item_dependency_and_cogs(self):
		self.assertEqual(self.after.cogs("DN-0125"), 6800)
		self.assertIn(("Milk Powder", "Stores"), [(d["item_code"], d["warehouse"]) for d in self.job.items_to_be_repost])

	def test_closing_milk_unchanged(self):
		last = [s for s in self.after.ledger_rows() if s.item_code == "Milk"][-1]
		self.assertEqual((last.qty_after_transaction, last.stock_value, last.stock_queue), (20, 1200, [[20, 60]]))

	def test_gl_reposted_only_for_changed_vouchers(self):
		self.assertEqual(self.job.affected_vouchers, {"PR-0110", "SE-0121", "DN-0125"})

	def test_idempotent(self):
		again = copy.deepcopy(self.after)
		again.enqueue_repost("Milk", "Stores", sle(again, "PR-0110").posting)
		again.process_repost_queue()
		self.assertEqual([(s.stock_value, s.stock_queue) for s in again.sles], [(s.stock_value, s.stock_queue) for s in self.after.sles])

	def test_resume_after_crash(self):
		L = copy.deepcopy(self.before)
		job = L.update_purchase_receipt_rate("PR-0110", "Milk", 80)
		with self.assertRaises(RuntimeError):
			L.run_repost(job, crash_after=1)
		self.assertEqual(job.current_index, 1)
		L.run_repost(job)
		self.assertEqual(L.cogs("DN-0125"), 6800)

	def test_naive_delta_patch_is_wrong(self):
		naive = naive_delta_patch(self.before, "PR-0110", "Milk", 80)
		self.assertNotEqual([s for s in naive.ledger_rows() if s.item_code == "Milk"][-1].stock_value, 1200)
		self.assertEqual(sle(naive, "SE-0121", "Milk Powder").incoming_rate, 580)  # cost never flowed


if __name__ == "__main__":
	unittest.main()

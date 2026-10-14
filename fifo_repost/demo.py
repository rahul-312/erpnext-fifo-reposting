"""
Run:  python3 -m fifo_repost.demo        (from the erpnext-fifo-reposting folder)

Walks through the assignment exactly as asked:
  - builds the January transactions
  - user edits the 10-Jan Purchase Receipt rate 60 -> 80
  - reposts FIFO valuation (incl. Milk -> Milk Powder dependency) and GL
  - prints Q1 (affected transactions), Q2 (recalculated values) and the
    Q4 anti-pattern (naive running-balance patch) for comparison
"""

import copy

from fifo_repost.engine import Ledger, build_assignment_scenario, naive_delta_patch


def fmt_queue(q):
	return "[" + ", ".join(f"{qty:g}@{rate:g}" for qty, rate in q) + "]"


def print_ledger(title: str, L: Ledger) -> None:
	print(f"\n{title}")
	print(
		f"{'Date':<6} {'Voucher':<9} {'Item':<12} {'Qty':>6} {'In rate':>8} "
		f"{'Val diff':>9} {'Bal qty':>7} {'Bal value':>9} {'Val rate':>8}  FIFO queue"
	)
	print("-" * 110)
	for s in L.ledger_rows():
		print(
			f"{s.posting:%d-%b} {s.voucher_no:<9} {s.item_code:<12} {s.actual_qty:>6g} "
			f"{s.incoming_rate if s.actual_qty > 0 else 0:>8g} {s.stock_value_difference:>9g} "
			f"{s.qty_after_transaction:>7g} {s.stock_value:>9g} {s.valuation_rate:>8.2f}  {fmt_queue(s.stock_queue)}"
		)


def print_impact(before: Ledger, after: Ledger) -> None:
	print("\nQ1. WHICH TRANSACTIONS ARE AFFECTED")
	print("-" * 110)
	b = {s.voucher_detail_no: s for s in before.sles}
	for s in after.ledger_rows():
		o = b[s.voucher_detail_no]
		value_changed = o.stock_value_difference != s.stock_value_difference
		balance_changed = (o.stock_value, o.stock_queue) != (s.stock_value, s.stock_queue)
		if value_changed:
			tag = f"VALUE CHANGED  {o.stock_value_difference:g} -> {s.stock_value_difference:g}  (GL reposted)"
		elif balance_changed:
			tag = "balance/queue rewritten only (its own value & GL unchanged)"
		else:
			tag = "not affected"
		print(f"{s.posting:%d-%b} {s.voucher_no:<9} {s.item_code:<12} {tag}")


def print_gl_diff(before: Ledger, after: Ledger) -> None:
	print("\nGL IMPACT")
	print("-" * 110)
	for v in after.vouchers:
		bg = {(g.account): (g.debit, g.credit) for g in before.gl_for(v)}
		ag = {(g.account): (g.debit, g.credit) for g in after.gl_for(v)}
		for acc in ag:
			if bg.get(acc) != ag[acc]:
				print(f"{v:<9} {acc:<32} Dr/Cr {bg.get(acc)} -> {ag[acc]}")


def main():
	before = build_assignment_scenario()
	print_ledger("BEFORE: PR-0110 @ 60", before)

	after = copy.deepcopy(before)
	job = after.update_purchase_receipt_rate("PR-0110", "Milk", 80)
	print(f"\nUser edits PR-0110 rate 60 -> 80  =>  queued {job.name} for Milk/{job.warehouse} from {job.posting:%d-%b}")
	after.process_repost_queue()
	print(
		f"{job.name}: status={job.status}, item-warehouses reposted="
		f"{[(d['item_code'], d['posting'].strftime('%d-%b')) for d in job.items_to_be_repost]}, "
		f"SLE rows rewritten={job.sles_rewritten}, vouchers whose GL was reposted={sorted(job.affected_vouchers)}"
	)

	print_ledger("AFTER: PR-0110 @ 80 (reposted)", after)
	print_impact(before, after)
	print_gl_diff(before, after)

	print("\nKEY NUMBERS")
	print("-" * 110)
	print(f"Milk Powder COGS (DN-0125): {before.cogs('DN-0125'):g} -> {after.cogs('DN-0125'):g}")
	print(f"Milk COGS        (DN-0115): {before.cogs('DN-0115'):g} -> {after.cogs('DN-0115'):g}  (consumed the @50 opening batch)")
	milk_close = [s for s in after.ledger_rows() if s.item_code == "Milk"][-1]
	print(f"Closing Milk: {milk_close.qty_after_transaction:g} L = {milk_close.stock_value:g}  {fmt_queue(milk_close.stock_queue)}")

	# ---- Q4: naive approach on stored running balances ----
	naive = naive_delta_patch(before, "PR-0110", "Milk", 80)
	n_close = [s for s in naive.ledger_rows() if s.item_code == "Milk"][-1]
	n_fg = next(s for s in naive.sles if s.voucher_no == "SE-0121" and s.item_code == "Milk Powder")
	print("\nQ4. NAIVE 'ADD DELTA TO STORED RUNNING BALANCES' (wrong for FIFO)")
	print("-" * 110)
	print(f"Closing Milk value  : naive={n_close.stock_value:g}   correct={milk_close.stock_value:g}")
	print(f"Milk Powder in rate : naive={n_fg.incoming_rate:g}   correct=680")
	print(
		f"Stored row is self-contradictory: queue {fmt_queue(n_close.stock_queue)} is worth "
		f"{sum(q * r for q, r in n_close.stock_queue):g} but stock_value says {n_close.stock_value:g}"
	)

	# ---- resumability ----
	crash = copy.deepcopy(before)
	job = crash.update_purchase_receipt_rate("PR-0110", "Milk", 80)
	try:
		crash.run_repost(job, crash_after=1)  # dies after Milk, before Milk Powder
	except RuntimeError as e:
		print(f"\nRESUME: {job.name} crashed ({e}) at current_index={job.current_index}; restarting worker...")
	crash.run_repost(job)
	ok = crash.cogs("DN-0125") == after.cogs("DN-0125")
	print(f"RESUME: {job.name} status={job.status}, Milk Powder COGS={crash.cogs('DN-0125'):g}  ({'matches' if ok else 'MISMATCH'})")


if __name__ == "__main__":
	main()

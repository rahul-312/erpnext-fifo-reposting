"""
End-to-end run of the assignment on a real ERPNext site.

    cd ~/frappe-bench
    env/bin/python erpnext-fifo-reposting/erpnext_demo/run_e2e.py                 # dry run: everything rolled back
    env/bin/python erpnext-fifo-reposting/erpnext_demo/run_e2e.py --commit        # keep the documents to inspect in Desk

What it does, exactly as the task is worded:
    Opening stock     Milk 100 L @ 50            (Stock Reconciliation, "Opening Stock")
    10-Jan            Purchase Receipt  Milk 50 L @ 60
    15-Jan            Delivery Note     Milk 80 L
    20-Jan            Purchase Receipt  Milk 50 L @ 60
    21-Jan            Stock Entry (Manufacture)  Milk 100 L -> Milk Powder 10 Kg
    25-Jan            Delivery Note     Milk Powder 10 Kg
    then              user changes the 10-Jan PR rate 60 -> 80  (rate_change.update_purchase_receipt_rate)
                      ERPNext reposts FIFO valuation + GL

It prints the stock ledger and GL before/after, and checks every number against the
framework-independent design in fifo_repost/engine.py.

Isolation: everything is created in its own company ("Milk Repost Demo") and a fresh
warehouse. Other custom apps' hooks are disabled *in this process
only*, so the run behaves like vanilla ERPNext. Nothing on disk or in site config changes.
"""

import argparse
import os
import sys
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)  # erpnext-fifo-reposting/
BENCH = os.path.dirname(ROOT)
sys.path[:0] = [HERE, ROOT]

import frappe  # noqa: E402
from frappe.utils import flt  # noqa: E402

COMPANY = "Milk Repost Demo"
ABBR = "MRD"
MILK, POWDER = "Milk", "Milk Powder"


# --------------------------------------------------------------------------- #
# environment
# --------------------------------------------------------------------------- #
def connect(site):
	sites_path = os.path.join(BENCH, "sites")
	os.chdir(sites_path)
	frappe.init(site=site, sites_path=sites_path)
	frappe.connect()
	frappe.set_user("Administrator")


def isolate_custom_apps(keep=("frappe", "erpnext")):
	"""Hide hooks of other installed apps for this process, so the demo runs on vanilla ERPNext."""
	custom = [a for a in frappe.get_installed_apps() if a not in keep]
	if not custom:
		return []
	prefixes = tuple(f"{a}." for a in custom)
	original = frappe.get_hooks

	def strip(value):
		if isinstance(value, str):
			return None if value.startswith(prefixes) else value
		if isinstance(value, list):
			return [v for v in (strip(v) for v in value) if v is not None]
		if isinstance(value, dict):
			return frappe._dict({k: strip(v) for k, v in value.items()})
		return value

	def get_hooks(hook=None, default="_KEEP_DEFAULT_LIST", app_name=None):
		return strip(original(hook, default, app_name))

	frappe.get_hooks = get_hooks
	frappe.local.doc_events_hooks = None
	frappe.controllers.pop(frappe.local.site, None)
	return custom


def tolerate_missing_contact_field():
	"""Some sites lack ERPNext's `Contact.is_billing_contact` custom field (created at install).
	Without it every PR/DN fails while looking up the party's default contact. The demo parties
	have no contacts, so for this process only we short-circuit that lookup instead of altering
	the site schema. Repair permanently with:
	    bench --site <site> execute erpnext.setup.install.create_address_and_contact_custom_fields
	"""
	if frappe.db.has_column("Contact", "is_billing_contact"):
		return False
	import erpnext.accounts.party as party

	party.get_default_contact = lambda doctype, name: None
	return True


def guard_transaction():
	"""Dry-run mode: make commits no-ops and fail loudly instead of silently rolling back mid-run."""
	db = frappe.db
	real_commit, real_rollback = db.commit, db.rollback

	def no_commit(*args, **kwargs):
		pass

	def loud_rollback(*args, save_point=None, **kwargs):
		if save_point:
			return real_rollback(save_point=save_point)
		raise RuntimeError("An error triggered a rollback during the demo (see the chained exception)")

	db.commit, db.rollback = no_commit, loud_rollback
	return real_commit, real_rollback


# --------------------------------------------------------------------------- #
# masters
# --------------------------------------------------------------------------- #
def leaf(doctype, preferred=None):
	if preferred and frappe.db.exists(doctype, {"name": preferred, "is_group": 0}):
		return preferred
	return frappe.db.get_value(doctype, {"is_group": 0}, "name")


def setup_masters(year):
	# fiscal year covering January
	jan = f"{year}-01-15"
	if not frappe.db.sql(
		"select name from `tabFiscal Year` where %s between year_start_date and year_end_date and disabled = 0", jan
	):
		frappe.get_doc(
			{
				"doctype": "Fiscal Year",
				"year": f"{year - 1}-{year}",
				"year_start_date": f"{year - 1}-04-01",
				"year_end_date": f"{year}-03-31",
			}
		).insert()

	if not frappe.db.exists("Company", COMPANY):
		frappe.get_doc(
			{
				"doctype": "Company",
				"company_name": COMPANY,
				"abbr": ABBR,
				"default_currency": "INR",
				"country": "India",
				"create_chart_of_accounts_based_on": "Standard Template",
				"chart_of_accounts": "Standard",
			}
		).insert()

	# fresh warehouse every run => an isolated stock ledger for this run
	wh = frappe.get_doc(
		{
			"doctype": "Warehouse",
			"warehouse_name": f"Milk Store {datetime.now():%H%M%S}",
			"parent_warehouse": f"All Warehouses - {ABBR}",
			"company": COMPANY,
		}
	).insert()

	for code, uom, group in ((MILK, "Litre", "Raw Material"), (POWDER, "Kg", "Finished Goods")):
		if frappe.db.exists("Item", code):
			item = frappe.get_doc("Item", code)
			if not item.is_stock_item or (item.valuation_method or "FIFO") != "FIFO":
				raise SystemExit(f"Existing item {code!r} must be a FIFO stock item")
		else:
			frappe.get_doc(
				{
					"doctype": "Item",
					"item_code": code,
					"item_name": code,
					"stock_uom": uom,
					"item_group": leaf("Item Group", group),
					"is_stock_item": 1,
					"valuation_method": "FIFO",
				}
			).insert()

	if not frappe.db.exists("Price List", "Milk Demo Selling"):
		frappe.get_doc(
			{"doctype": "Price List", "price_list_name": "Milk Demo Selling", "currency": "INR", "selling": 1}
		).insert()

	if not frappe.db.exists("Supplier", {"supplier_name": "Milk Demo Supplier"}):
		frappe.get_doc(
			{"doctype": "Supplier", "supplier_name": "Milk Demo Supplier", "supplier_group": leaf("Supplier Group")}
		).insert()
	if not frappe.db.exists("Customer", {"customer_name": "Milk Demo Customer"}):
		frappe.get_doc(
			{
				"doctype": "Customer",
				"customer_name": "Milk Demo Customer",
				"customer_group": leaf("Customer Group"),
				"territory": leaf("Territory"),
			}
		).insert()

	return frappe._dict(
		warehouse=wh.name,
		supplier=frappe.db.get_value("Supplier", {"supplier_name": "Milk Demo Supplier"}),
		customer=frappe.db.get_value("Customer", {"customer_name": "Milk Demo Customer"}),
		opening_account=frappe.db.get_value(
			"Account", {"company": COMPANY, "account_type": "Temporary", "is_group": 0}
		),
		cost_center=frappe.get_cached_value("Company", COMPANY, "cost_center"),
	)


# --------------------------------------------------------------------------- #
# transactions (the task, line by line)
# --------------------------------------------------------------------------- #
def submit(doc):
	doc = frappe.get_doc(doc)
	doc.insert()
	doc.submit()
	return doc.name


def posting(year, day, time="10:00:00"):
	return {"posting_date": f"{year}-01-{day:02d}", "posting_time": time, "set_posting_time": 1}


def create_transactions(m, year):
	wh = m.warehouse
	v = {}
	v["OPENING"] = submit(
		{
			"doctype": "Stock Reconciliation",
			"company": COMPANY,
			"purpose": "Opening Stock",
			"expense_account": m.opening_account,
			"cost_center": m.cost_center,
			**posting(year, 1, "09:00:00"),
			"items": [{"item_code": MILK, "warehouse": wh, "qty": 100, "valuation_rate": 50}],
		}
	)

	def purchase_receipt(day, qty, rate):
		return submit(
			{
				"doctype": "Purchase Receipt",
				"company": COMPANY,
				"supplier": m.supplier,
				"currency": "INR",
				"conversion_rate": 1,
				**posting(year, day),
				"items": [{"item_code": MILK, "qty": qty, "rate": rate, "warehouse": wh, "uom": "Litre"}],
			}
		)

	def delivery_note(day, item, qty, uom, rate):
		return submit(
			{
				"doctype": "Delivery Note",
				"company": COMPANY,
				"customer": m.customer,
				"currency": "INR",
				"conversion_rate": 1,
				"selling_price_list": "Milk Demo Selling",
				"plc_conversion_rate": 1,
				**posting(year, day),
				"items": [{"item_code": item, "qty": qty, "rate": rate, "warehouse": wh, "uom": uom}],
			}
		)

	v["PR-0110"] = purchase_receipt(10, 50, 60)
	v["DN-0115"] = delivery_note(15, MILK, 80, "Litre", 70)
	v["PR-0120"] = purchase_receipt(20, 50, 60)
	v["SE-0121"] = submit(
		{
			"doctype": "Stock Entry",
			"company": COMPANY,
			"stock_entry_type": "Manufacture",
			"purpose": "Manufacture",
			"fg_completed_qty": 10,
			**posting(year, 21),
			"items": [
				{"item_code": MILK, "s_warehouse": wh, "qty": 100, "uom": "Litre"},
				{"item_code": POWDER, "t_warehouse": wh, "qty": 10, "uom": "Kg", "is_finished_item": 1},
			],
		}
	)
	v["DN-0125"] = delivery_note(25, POWDER, 10, "Kg", 900)
	return v


# --------------------------------------------------------------------------- #
# snapshots / printing / verification
# --------------------------------------------------------------------------- #
def stock_ledger(warehouse):
	rows = frappe.get_all(
		"Stock Ledger Entry",
		filters={"warehouse": warehouse, "is_cancelled": 0},
		fields=[
			"name", "item_code", "voucher_type", "voucher_no", "posting_date", "posting_time", "creation",
			"actual_qty", "incoming_rate", "stock_value_difference", "qty_after_transaction",
			"stock_value", "valuation_rate", "stock_queue",
		],
	)  # fmt: skip
	rows.sort(key=lambda r: (r.item_code != MILK, r.posting_date, r.posting_time, r.creation))
	for r in rows:
		r.stock_queue = [[flt(q), flt(rt)] for q, rt in frappe.parse_json(r.stock_queue or "[]")]
	return rows


def gl(vouchers):
	rows = frappe.get_all(
		"GL Entry",
		filters={"voucher_no": ["in", list(vouchers)], "is_cancelled": 0},
		fields=["voucher_no", "account", "sum(debit) as debit", "sum(credit) as credit"],
		group_by="voucher_no, account",
	)
	return {(r.voucher_no, r.account): (flt(r.debit, 2), flt(r.credit, 2)) for r in rows}


def fmt_queue(q):
	return "[" + ", ".join(f"{flt(a):g}@{flt(b):g}" for a, b in q) + "]"


def print_ledger(title, rows, label):
	print(f"\n{title}")
	print(
		f"{'Date':<6} {'Step':<8} {'Voucher':<22} {'Item':<12} {'Qty':>6} {'In rate':>8} "
		f"{'Val diff':>9} {'Bal qty':>7} {'Bal value':>9}  FIFO queue"
	)
	print("-" * 118)
	for r in rows:
		print(
			f"{r.posting_date:%d-%b} {label[r.voucher_no]:<8} {r.voucher_no:<22} {r.item_code:<12} "
			f"{r.actual_qty:>6g} {r.incoming_rate:>8g} {r.stock_value_difference:>9g} "
			f"{r.qty_after_transaction:>7g} {r.stock_value:>9g}  {fmt_queue(r.stock_queue)}"
		)


def print_gl_diff(before, after, label):
	print("\nGL IMPACT (only lines that changed)")
	print("-" * 118)
	for key in sorted(set(before) | set(after), key=lambda k: (list(label).index(k[0]), k[1])):
		if before.get(key) != after.get(key):
			print(f"{label[key[0]]:<8} {key[0]:<22} {key[1]:<45} Dr/Cr {before.get(key)} -> {after.get(key)}")


def verify(rows, ledger, names, when):
	"""Compare ERPNext's SLEs with the design engine, row by row."""
	failures = 0
	expected = {(s.voucher_no, s.item_code): s for s in ledger.sles}
	for r in rows:
		e = expected[(names[r.voucher_no], r.item_code)]
		checks = {
			"stock_value_difference": (flt(r.stock_value_difference, 2), flt(e.stock_value_difference, 2)),
			"qty_after_transaction": (flt(r.qty_after_transaction, 3), flt(e.qty_after_transaction, 3)),
			"stock_value": (flt(r.stock_value, 2), flt(e.stock_value, 2)),
			# ERPNext keeps an empty [0, last_rate] batch when stock hits zero (to remember the rate)
			"stock_queue": ([b for b in r.stock_queue if b[0]], [[flt(q), flt(rt)] for q, rt in e.stock_queue]),
		}
		for fieldname, (got, want) in checks.items():
			if got != want:
				failures += 1
				print(f"  MISMATCH {when} {names[r.voucher_no]} {r.item_code} {fieldname}: erpnext={got} design={want}")
	print(f"\nVERIFY {when}: {'PASS' if not failures else f'{failures} MISMATCH(ES)'} - ERPNext ledger vs fifo_repost design engine")
	return failures


# --------------------------------------------------------------------------- #
def main():
	parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
	parser.add_argument("--site", default=None, help="site name (default: bench default_site)")
	parser.add_argument("--year", type=int, default=2026, help="year of the January transactions (default 2026)")
	parser.add_argument("--commit", action="store_true", help="persist the demo documents instead of rolling back")
	parser.add_argument("--keep-custom-apps", action="store_true", help="do not disable other apps' hooks")
	args = parser.parse_args()

	site = args.site or frappe.parse_json(open(os.path.join(BENCH, "sites", "common_site_config.json")).read()).get(
		"default_site"
	)
	connect(site)

	from fifo_repost.engine import build_assignment_scenario
	from rate_change import update_purchase_receipt_rate

	real_commit = real_rollback = None
	failures = 0
	completed = False
	try:
		if not args.keep_custom_apps:
			hidden = isolate_custom_apps()
			if hidden:
				print(f"[setup] hooks of {', '.join(hidden)} disabled for this process (vanilla ERPNext behaviour)")
		if tolerate_missing_contact_field():
			print(
				"[setup] WARNING: site is missing ERPNext custom field Contact.is_billing_contact; default-contact "
				"lookup skipped for this process. Repair: bench --site "
				f"{site} execute erpnext.setup.install.create_address_and_contact_custom_fields"
			)
		if not args.commit:
			real_commit, real_rollback = guard_transaction()
			print("[setup] dry run: all changes will be rolled back at the end (use --commit to keep them)")

		m = setup_masters(args.year)
		print(f"[setup] site={site} company={COMPANY} warehouse={m.warehouse}")

		names = create_transactions(m, args.year)  # step label -> ERPNext name
		label = {v: k for k, v in names.items()}  # ERPNext name -> step label
		print("[setup] transactions: " + ", ".join(f"{k}={v}" for k, v in names.items()))

		design_before = build_assignment_scenario(args.year)
		design_after = build_assignment_scenario(args.year)
		design_after.update_purchase_receipt_rate("PR-0110", MILK, 80)
		design_after.process_repost_queue()

		sle_before, gl_before = stock_ledger(m.warehouse), gl(names.values())
		print_ledger("BEFORE: 10-Jan PR @ 60", sle_before, label)
		failures += verify(sle_before, design_before, label, "BEFORE")

		# ------------- the user edit -------------
		print(f"\n>>> User changes {names['PR-0110']} ({MILK}) rate 60 -> 80")
		reposts = update_purchase_receipt_rate(names["PR-0110"], MILK, 80, process_now=True)
		for name in reposts:
			riv = frappe.db.get_value(
				"Repost Item Valuation", name, ["item_code", "warehouse", "posting_date", "status"], as_dict=True
			)
			print(f">>> {name}: {riv.item_code} / {riv.warehouse} from {riv.posting_date} -> {riv.status}")

		sle_after, gl_after = stock_ledger(m.warehouse), gl(names.values())
		print_ledger("AFTER: 10-Jan PR @ 80 (reposted by ERPNext)", sle_after, label)

		print("\nQ1. AFFECTED TRANSACTIONS")
		print("-" * 118)
		new_by_key = {(r.voucher_no, r.item_code): r for r in sle_after}
		for r in sle_before:
			n = new_by_key[(r.voucher_no, r.item_code)]
			if flt(n.stock_value_difference, 2) != flt(r.stock_value_difference, 2):
				what = f"VALUE CHANGED {r.stock_value_difference:g} -> {n.stock_value_difference:g} (GL reposted)"
			elif (flt(n.stock_value, 2), n.stock_queue) != (flt(r.stock_value, 2), r.stock_queue):
				what = "running balance / FIFO queue rewritten only"
			else:
				what = "not affected"
			print(f"{label[r.voucher_no]:<8} {r.voucher_no:<22} {r.item_code:<12} {what}")

		print_gl_diff(gl_before, gl_after, label)
		failures += verify(sle_after, design_after, label, "AFTER")

		cogs = lambda g, v: sum(d for (vn, acc), (d, c) in g.items() if vn == v and acc.startswith("Cost of Goods Sold"))  # noqa: E731
		print(
			f"\nMilk Powder COGS ({names['DN-0125']}): {cogs(gl_before, names['DN-0125']):g} -> "
			f"{cogs(gl_after, names['DN-0125']):g}   |   Milk COGS ({names['DN-0115']}): "
			f"{cogs(gl_before, names['DN-0115']):g} -> {cogs(gl_after, names['DN-0115']):g}"
		)
		completed = True
	finally:
		if real_rollback:
			frappe.db.commit, frappe.db.rollback = real_commit, real_rollback
			frappe.db.rollback()
			print("\n[teardown] rolled back - the site is unchanged")
		elif args.commit and completed:
			frappe.db.commit()
			print(f"\n[teardown] committed - open company '{COMPANY}' in Desk to inspect")
		elif args.commit:
			frappe.db.rollback()
			print("\n[teardown] run failed - uncommitted changes rolled back (ERPNext's repost may already have committed some)")
		frappe.destroy()

	sys.exit(1 if failures else 0)


if __name__ == "__main__":
	main()

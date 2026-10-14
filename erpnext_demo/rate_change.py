"""
The ERPNext code change: allow changing the rate of a *submitted* Purchase Receipt
and let ERPNext's own reposting engine carry the effect forward.

Out of the box ERPNext v15 does not let a user edit the rate of a submitted
Purchase Receipt. The only supported routes are "cancel + amend" (fails here: cancelling
the 10-Jan receipt would make the 21-Jan manufacture go negative) or a Landed
Cost Voucher (adds a charge; it does not change the purchase rate). This
function implements the "user edits the rate" step from the assignment by
re-using the exact revaluation path a Landed Cost Voucher already uses:

    1. update rate/amount/valuation_rate on the submitted PR (no cancel)
    2. replace the PR's own SLE + GL (cancel-state then submit-state, in place)
    3. queue Repost Item Valuation from the PR's posting date/time
    4. (optional) process the repost immediately

Step 3 is where FIFO is replayed for every later SLE of the item/warehouse,
dependent items (Milk Powder via the Manufacture entry) are added to the repost,
and GL is reposted for every voucher whose stock value changed.

To ship it, drop this into a custom app and expose it as an action on the
Purchase Receipt form; it is already @frappe.whitelist()-ed.
"""

import frappe
from frappe import _
from frappe.utils import flt, getdate


@frappe.whitelist()
def update_purchase_receipt_rate(purchase_receipt: str, item_code: str, new_rate: float, process_now: bool = False):
	doc = frappe.get_doc("Purchase Receipt", purchase_receipt)
	doc.check_permission("write")
	new_rate = flt(new_rate)

	_validate(doc, item_code, new_rate)

	rows = [d for d in doc.items if d.item_code == item_code]
	old_rate = rows[0].rate

	# 1. change the rate on the submitted document and recompute totals / valuation_rate
	for d in rows:
		d.rate = new_rate
		d.price_list_rate = 0
		d.discount_percentage = 0
		d.discount_amount = 0
	doc.calculate_taxes_and_totals()
	doc.update_valuation_rate(reset_outgoing_rate=False)
	doc.db_update()
	for child in doc.items + doc.get("taxes", []):
		child.db_update()

	# 2. rebuild this voucher's own stock & GL entries (same sequence as LandedCostVoucher.update_landed_cost)
	doc.docstatus = 2
	doc.update_stock_ledger(allow_negative_stock=True, via_landed_cost_voucher=True)
	doc.make_gl_entries_on_cancel(from_repost=True)

	doc.docstatus = 1
	doc.update_stock_ledger(allow_negative_stock=True, via_landed_cost_voucher=True)
	doc.make_gl_entries(via_landed_cost_voucher=True)

	# 3. queue reposting of every later transaction (and its dependants) for FIFO
	reposts_before = set(_queued_reposts())
	doc.repost_future_sle_and_gle(force=True, via_landed_cost_voucher=True)
	new_reposts = [r for r in _queued_reposts() if r not in reposts_before]

	doc.add_comment(
		"Edit",
		_("Rate of {0} changed from {1} to {2} after submission; valuation reposted via {3}").format(
			item_code, old_rate, new_rate, ", ".join(new_reposts) or "-"
		),
	)

	# 4. optionally process now instead of waiting for the scheduler (runs every hour by default)
	if frappe.parse_json(process_now):
		process_reposts(new_reposts)

	return new_reposts


def process_reposts(names):
	"""Run the given Repost Item Valuation docs synchronously, in creation order, via ERPNext's repost()."""
	from erpnext.stock.doctype.repost_item_valuation.repost_item_valuation import repost

	for name in names:
		riv = frappe.get_doc("Repost Item Valuation", name)
		if riv.status in ("Queued", "In Progress"):
			repost(riv)
		status, error = frappe.db.get_value("Repost Item Valuation", name, ["status", "error_log"])
		if status != "Completed":
			frappe.throw(_("Repost {0} ended with status {1}:<br>{2}").format(name, status, error))


def _queued_reposts():
	return frappe.get_all(
		"Repost Item Valuation",
		filters={"docstatus": 1, "status": ["in", ["Queued", "In Progress"]]},
		order_by="creation asc",
		pluck="name",
	)


def _validate(doc, item_code, new_rate):
	if doc.docstatus != 1:
		frappe.throw(_("Only submitted Purchase Receipts can be re-rated. Edit the draft instead."))
	if doc.is_return or doc.is_internal_supplier:
		frappe.throw(_("Re-rating returns or internal transfers is not supported"))
	if new_rate < 0:
		frappe.throw(_("Rate cannot be negative"))
	if not any(d.item_code == item_code for d in doc.items):
		frappe.throw(_("Item {0} not found in {1}").format(item_code, doc.name))
	if flt(doc.per_billed):
		# the Purchase Invoice would then disagree with the receipt; fix the PI instead
		# (Buying Settings > "Set Landed Cost Based on Purchase Invoice Rate")
		frappe.throw(_("{0} is already billed. Change the rate on the Purchase Invoice instead.").format(doc.name))

	frozen_upto = frappe.db.get_single_value("Stock Settings", "stock_frozen_upto")
	if frozen_upto and getdate(doc.posting_date) <= getdate(frozen_upto):
		frappe.throw(_("Stock transactions before {0} are frozen").format(frozen_upto))

	# a still-running repost for the same item/warehouse would race with ours. Voucher-based
	# reposts leave item_code empty, so any running one for the company blocks too (conservative).
	running = frappe.db.exists(
		"Repost Item Valuation",
		{"docstatus": 1, "status": "In Progress", "item_code": item_code, "company": doc.company},
	) or frappe.db.exists(
		"Repost Item Valuation",
		{"docstatus": 1, "status": "In Progress", "based_on": "Transaction", "company": doc.company},
	)
	if running:
		frappe.throw(_("Repost {0} is in progress for {1}; try again once it completes").format(running, item_code))

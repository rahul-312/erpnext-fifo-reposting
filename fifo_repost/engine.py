"""
FIFO stock ledger + backdated reposting engine (framework-independent design).

This is the "design" half of the assignment. It deliberately mirrors the way
ERPNext works so the ideas map 1:1 onto the real code:

    this module                       ERPNext v15
    -----------------------------     ----------------------------------------------
    StockLedgerEntry                  Stock Ledger Entry (tabStock Ledger Entry)
      .qty_after_transaction            qty_after_transaction   (running balance)
      .stock_value                      stock_value             (running balance)
      .stock_queue                      stock_queue             (FIFO batches, JSON)
    FIFOQueue                         erpnext.stock.valuation.FIFOValuation
    RepostJob                         Repost Item Valuation doctype
    Ledger.repost_item_warehouse()    stock_ledger.update_entries_after
    dependency propagation            update_entries_after.update_distinct_item_warehouses
    Ledger.repost_gl()                repost_item_valuation.repost_gl_entries

Key design rules (see README for the reasoning):
  1. Valuation is per (item, warehouse) and depends only on earlier entries.
  2. To repost from time T, restore the FIFO queue from the last SLE *before* T
     and replay every SLE at/after T in strict order. Never patch deltas.
  3. When an outgoing entry of a Manufacture/Repack changes value, the finished
     good's incoming rate changes -> enqueue (FG item, FG warehouse, voucher time).
     Only rows flagged is_finished_item absorb the cost.
     Material Transfers are not modelled here (ERPNext treats the target warehouse
     as a dependant in the same way).
  4. After stock is consistent, regenerate GL only for vouchers whose stock value
     actually changed.
  5. The job is resumable (current_index checkpoint) and idempotent.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from datetime import datetime

PRECISION = 6


def _r(v: float) -> float:
	return round(v + 0.0, PRECISION)


# --------------------------------------------------------------------------- #
# FIFO queue
# --------------------------------------------------------------------------- #
class FIFOQueue:
	"""List of [qty, rate] batches; outgoing stock is taken from the oldest batch first."""

	def __init__(self, queue: list[list[float]] | None = None):
		self.queue: list[list[float]] = copy.deepcopy(queue or [])

	def add_stock(self, qty: float, rate: float) -> None:
		# merge with last batch if same rate (ERPNext does the same)
		if self.queue and self.queue[-1][1] == rate:
			self.queue[-1][0] = _r(self.queue[-1][0] + qty)
		else:
			self.queue.append([qty, rate])

	def remove_stock(self, qty: float) -> float:
		"""Consume `qty` FIFO and return the value consumed."""
		value, remaining = 0.0, qty
		while remaining > 0:
			if not self.queue:
				raise NegativeStockError(f"Insufficient stock: short by {remaining}")
			batch = self.queue[0]
			take = min(batch[0], remaining)
			value += take * batch[1]
			batch[0] = _r(batch[0] - take)
			remaining = _r(remaining - take)
			if batch[0] <= 0:
				self.queue.pop(0)
		return _r(value)

	@property
	def qty(self) -> float:
		return _r(sum(q for q, _ in self.queue))

	@property
	def value(self) -> float:
		return _r(sum(q * r for q, r in self.queue))

	def snapshot(self) -> list[list[float]]:
		return copy.deepcopy(self.queue)


class NegativeStockError(Exception):
	pass


# --------------------------------------------------------------------------- #
# Documents (vouchers) and ledger rows
# --------------------------------------------------------------------------- #
@dataclass
class VoucherLine:
	item_code: str
	qty: float  # always positive
	warehouse: str
	direction: str  # "in" | "out"
	rate: float | None = None  # incoming rate; None = derived (FG) or FIFO (outgoing)
	is_finished_item: bool = False
	name: str = ""


@dataclass
class Voucher:
	voucher_type: str  # "Opening Stock" | "Purchase Receipt" | "Delivery Note" | "Stock Entry"
	voucher_no: str
	posting: datetime
	lines: list[VoucherLine]
	purpose: str | None = None  # "Manufacture" for stock entries
	selling_rate: float = 0.0


@dataclass
class StockLedgerEntry:
	seq: int  # creation order, tie-breaker for equal posting datetimes
	posting: datetime
	voucher_type: str
	voucher_no: str
	voucher_detail_no: str
	item_code: str
	warehouse: str
	actual_qty: float
	incoming_rate: float = 0.0
	# --- running balance fields (stored on every row, like ERPNext) ---
	qty_after_transaction: float = 0.0
	stock_value: float = 0.0
	valuation_rate: float = 0.0
	stock_queue: list[list[float]] = field(default_factory=list)
	stock_value_difference: float = 0.0

	@property
	def sort_key(self):
		return (self.posting, self.seq)

	@property
	def key(self):
		return (self.item_code, self.warehouse)


@dataclass
class GLEntry:
	voucher_no: str
	account: str
	debit: float
	credit: float


@dataclass
class RepostJob:
	"""Equivalent of a 'Repost Item Valuation' document."""

	name: str
	item_code: str
	warehouse: str
	posting: datetime
	status: str = "Queued"
	items_to_be_repost: list[dict] = field(default_factory=list)
	current_index: int = 0  # checkpoint, survives a crash
	affected_vouchers: set[str] = field(default_factory=set)
	sles_rewritten: int = 0


# --------------------------------------------------------------------------- #
# Ledger
# --------------------------------------------------------------------------- #
class Ledger:
	STOCK = "Stock In Hand"
	SRBNB = "Stock Received But Not Billed"
	COGS = "Cost of Goods Sold"
	OPENING = "Temporary Opening"

	def __init__(self):
		self.vouchers: dict[str, Voucher] = {}
		self.sles: list[StockLedgerEntry] = []
		self.gles: list[GLEntry] = []
		self.repost_jobs: list[RepostJob] = []
		self._seq = 0
		self._line_seq = 0

	# ---------------- posting -----------------
	def submit(self, voucher: Voucher) -> None:
		for line in voucher.lines:
			self._line_seq += 1
			line.name = line.name or f"{voucher.voucher_no}-row{self._line_seq}"
		self.vouchers[voucher.voucher_no] = voucher

		new_sles = []
		# outgoing rows first so a manufacture's FG rate can be derived from RM cost
		for line in sorted(voucher.lines, key=lambda l: l.direction != "out"):
			self._seq += 1
			sle = StockLedgerEntry(
				seq=self._seq,
				posting=voucher.posting,
				voucher_type=voucher.voucher_type,
				voucher_no=voucher.voucher_no,
				voucher_detail_no=line.name,
				item_code=line.item_code,
				warehouse=line.warehouse,
				actual_qty=line.qty if line.direction == "in" else -line.qty,
				incoming_rate=line.rate or 0.0,
			)
			self.sles.append(sle)
			new_sles.append(sle)

		# backdated entry? -> future rows of the same item/warehouse must be reposted.
		# Decide before valuing: only the new rows are valued here, later rows keep their
		# stored values so the repost job can detect what changed and follow dependants.
		backdated = [sle for sle in new_sles if any(s.key == sle.key and s.sort_key > sle.sort_key for s in self.sles)]

		# value the new rows, in order
		for sle in new_sles:
			self._repost_from(sle.item_code, sle.warehouse, sle.posting, job=None, stop_after=sle)

		self._make_gl(voucher.voucher_no)

		for sle in backdated:
			self.enqueue_repost(sle.item_code, sle.warehouse, sle.posting)

	# ---------------- the user edit -----------------
	def update_purchase_receipt_rate(self, voucher_no: str, item_code: str, new_rate: float) -> RepostJob:
		"""User edits a submitted Purchase Receipt's rate (the scenario in the task)."""
		voucher = self.vouchers[voucher_no]
		if voucher.voucher_type != "Purchase Receipt":
			raise ValueError("Only Purchase Receipts can be re-rated here")

		line = next(l for l in voucher.lines if l.item_code == item_code)
		line.rate = new_rate

		# 1. the voucher's own SLE gets the new incoming rate
		for sle in self.sles:
			if sle.voucher_detail_no == line.name:
				sle.incoming_rate = new_rate

		# 2. queue a repost starting at the voucher's posting time (inclusive)
		return self.enqueue_repost(item_code, line.warehouse, voucher.posting, voucher_no=voucher_no)

	def enqueue_repost(self, item_code, warehouse, posting, voucher_no=None) -> RepostJob:
		job = RepostJob(
			name=f"RIV-{len(self.repost_jobs) + 1:04d}",
			item_code=item_code,
			warehouse=warehouse,
			posting=posting,
		)
		job.items_to_be_repost = [{"item_code": item_code, "warehouse": warehouse, "posting": posting}]
		if voucher_no:
			job.affected_vouchers.add(voucher_no)
		self.repost_jobs.append(job)
		return job

	# ---------------- repost processing -----------------
	def process_repost_queue(self) -> None:
		"""Equivalent of the scheduler job `repost_entries()`."""
		for job in self.repost_jobs:
			if job.status in ("Queued", "In Progress"):
				self.run_repost(job)

	def run_repost(self, job: RepostJob, crash_after: int | None = None) -> None:
		job.status = "In Progress"
		# resumable: start from the checkpoint, not from 0
		while job.current_index < len(job.items_to_be_repost):
			if crash_after is not None and job.current_index >= crash_after:
				raise RuntimeError("simulated worker crash")

			args = job.items_to_be_repost[job.current_index]
			self._repost_from(args["item_code"], args["warehouse"], args["posting"], job)
			job.current_index += 1  # checkpoint (ERPNext commits here)

		self.repost_gl(job.affected_vouchers)
		job.status = "Completed"

	def _repost_from(
		self, item_code, warehouse, posting, job: RepostJob | None, stop_after: StockLedgerEntry | None = None
	) -> None:
		"""Replay FIFO for one (item, warehouse) from `posting` onwards (up to `stop_after`, if given)."""
		rows = sorted((s for s in self.sles if s.key == (item_code, warehouse)), key=lambda s: s.sort_key)

		previous = [s for s in rows if s.posting < posting]
		future = [s for s in rows if s.posting >= posting]

		# Rule 2: restore state from the last SLE before the change
		fifo = FIFOQueue(previous[-1].stock_queue if previous else [])
		prev_value = fifo.value

		for sle in future:
			old_value_diff = sle.stock_value_difference

			if sle.actual_qty > 0:
				if self._is_finished_good_row(sle):
					sle.incoming_rate = self._fg_rate(sle)
				fifo.add_stock(sle.actual_qty, sle.incoming_rate)
			else:
				fifo.remove_stock(-sle.actual_qty)

			sle.qty_after_transaction = fifo.qty
			sle.stock_value = fifo.value
			sle.valuation_rate = _r(fifo.value / fifo.qty) if fifo.qty else 0.0
			sle.stock_queue = fifo.snapshot()
			sle.stock_value_difference = _r(sle.stock_value - prev_value)
			prev_value = sle.stock_value

			if sle is stop_after:
				break
			if job is None:
				continue

			job.sles_rewritten += 1
			if sle.stock_value_difference != old_value_diff:
				job.affected_vouchers.add(sle.voucher_no)
				# Rule 3: outgoing raw material of a Manufacture changed -> FG must follow
				if sle.actual_qty < 0 and self._is_manufacture(sle.voucher_no):
					self._add_dependents(job, sle.voucher_no)

	def _add_dependents(self, job: RepostJob, voucher_no: str) -> None:
		voucher = self.vouchers[voucher_no]
		for line in voucher.lines:
			if line.direction == "in" and line.is_finished_item:
				dep = {"item_code": line.item_code, "warehouse": line.warehouse, "posting": voucher.posting}
				pending = job.items_to_be_repost[job.current_index + 1 :]
				# de-duplicate: keep only the earliest pending posting per item/warehouse
				existing = next(
					(d for d in pending if (d["item_code"], d["warehouse"]) == (line.item_code, line.warehouse)),
					None,
				)
				if existing:
					existing["posting"] = min(existing["posting"], voucher.posting)
				else:
					job.items_to_be_repost.append(dep)

	def _is_manufacture(self, voucher_no: str) -> bool:
		v = self.vouchers[voucher_no]
		return v.voucher_type == "Stock Entry" and v.purpose in ("Manufacture", "Repack")

	def _line(self, sle: StockLedgerEntry) -> VoucherLine:
		return next(l for l in self.vouchers[sle.voucher_no].lines if l.name == sle.voucher_detail_no)

	def _is_finished_good_row(self, sle: StockLedgerEntry) -> bool:
		return self._is_manufacture(sle.voucher_no) and self._line(sle).is_finished_item

	def _fg_rate(self, fg_sle: StockLedgerEntry) -> float:
		"""FG incoming rate = value of raw material consumed in the same voucher / FG qty."""
		rows = [s for s in self.sles if s.voucher_no == fg_sle.voucher_no]
		rm_value = sum(-s.stock_value_difference for s in rows if s.actual_qty < 0)
		fg_qty = sum(s.actual_qty for s in rows if s.actual_qty > 0 and self._is_finished_good_row(s))
		return _r(rm_value / fg_qty)

	# ---------------- GL -----------------
	def repost_gl(self, voucher_nos: set[str]) -> None:
		for v in voucher_nos:
			self._make_gl(v)

	def _make_gl(self, voucher_no: str) -> None:
		self.gles = [g for g in self.gles if g.voucher_no != voucher_no]  # cancel old
		v = self.vouchers[voucher_no]
		diff = sum(s.stock_value_difference for s in self.sles if s.voucher_no == voucher_no)
		if v.voucher_type == "Opening Stock":
			self._gl(voucher_no, self.STOCK, self.OPENING, diff)
		elif v.voucher_type == "Purchase Receipt":
			self._gl(voucher_no, self.STOCK, self.SRBNB, diff)
		elif v.voucher_type == "Delivery Note":
			self._gl(voucher_no, self.COGS, self.STOCK, -diff)
		elif v.voucher_type == "Stock Entry":
			# RM out and FG in net to zero for a manufacture without additional costs
			out_val = -sum(s.stock_value_difference for s in self.sles if s.voucher_no == voucher_no and s.actual_qty < 0)
			in_val = sum(s.stock_value_difference for s in self.sles if s.voucher_no == voucher_no and s.actual_qty > 0)
			self.gles.append(GLEntry(voucher_no, f"{self.STOCK} (FG)", _r(in_val), 0.0))
			self.gles.append(GLEntry(voucher_no, f"{self.STOCK} (RM)", 0.0, _r(out_val)))

	def _gl(self, voucher_no, debit_account, credit_account, amount):
		self.gles.append(GLEntry(voucher_no, debit_account, _r(amount), 0.0))
		self.gles.append(GLEntry(voucher_no, credit_account, 0.0, _r(amount)))

	# ---------------- reporting helpers -----------------
	def ledger_rows(self) -> list[StockLedgerEntry]:
		return sorted(self.sles, key=lambda s: (s.item_code != "Milk", s.sort_key))

	def gl_for(self, voucher_no: str) -> list[GLEntry]:
		return [g for g in self.gles if g.voucher_no == voucher_no]

	def cogs(self, voucher_no: str) -> float:
		return sum(g.debit for g in self.gl_for(voucher_no) if g.account == self.COGS)


# --------------------------------------------------------------------------- #
# The scenario from the assignment
# --------------------------------------------------------------------------- #
WH = "Stores"


def build_assignment_scenario(year: int = 2026) -> Ledger:
	def d(day, hh=10):
		return datetime(year, 1, day, hh, 0, 0)

	L = Ledger()
	L.submit(Voucher("Opening Stock", "OPENING", d(1, 9), [VoucherLine("Milk", 100, WH, "in", 50)]))
	L.submit(Voucher("Purchase Receipt", "PR-0110", d(10), [VoucherLine("Milk", 50, WH, "in", 60)]))
	L.submit(Voucher("Delivery Note", "DN-0115", d(15), [VoucherLine("Milk", 80, WH, "out")]))
	L.submit(Voucher("Purchase Receipt", "PR-0120", d(20), [VoucherLine("Milk", 50, WH, "in", 60)]))
	L.submit(
		Voucher(
			"Stock Entry",
			"SE-0121",
			d(21),
			[
				VoucherLine("Milk", 100, WH, "out"),
				VoucherLine("Milk Powder", 10, WH, "in", is_finished_item=True),
			],
			purpose="Manufacture",
		)
	)
	L.submit(Voucher("Delivery Note", "DN-0125", d(25), [VoucherLine("Milk Powder", 10, WH, "out")]))
	return L


# --------------------------------------------------------------------------- #
# Q4 illustration: what goes wrong if you "patch" stored running balances
# --------------------------------------------------------------------------- #
def naive_delta_patch(ledger: Ledger, voucher_no: str, item_code: str, new_rate: float) -> Ledger:
	"""Anti-pattern: add (new - old) * qty to every later stored balance of the same item.

	Looks cheap, but is wrong for FIFO: it never decides *which* outgoing entry
	consumed the re-rated batch, so the extra cost never reaches the Manufacture
	entry, Milk Powder, or COGS, and the closing Milk value becomes wrong.
	"""
	L = copy.deepcopy(ledger)
	src = next(s for s in L.sles if s.voucher_no == voucher_no and s.item_code == item_code)
	delta = (new_rate - src.incoming_rate) * src.actual_qty
	src.incoming_rate = new_rate
	for s in L.sles:
		if s.key == src.key and s.sort_key >= src.sort_key:
			s.stock_value = _r(s.stock_value + delta)
			s.valuation_rate = _r(s.stock_value / s.qty_after_transaction) if s.qty_after_transaction else 0.0
	src.stock_value_difference = _r(src.stock_value_difference + delta)
	return L

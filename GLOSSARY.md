# Glossary

The words this module uses for its own domain. Written for whoever — human or
agent — has to change the code and cannot afford to guess what a name means.
Where a term lives in the code, it says so.

## Payment concepts

**Terminal** — one Nexi contract as Odoo sees it: a `payment.provider` with
`code = 'nexi'`. A shop can have more than one, each with its own API key and
environment. The terminal decides where Nexi lives (`models/payment_provider.py`).

**Nexi order** — the payment attempt Nexi registers for a transaction, identified
by an order id. The transaction keeps it in `nexi_order_id`; when Nexi has not
answered yet, the transaction `reference` stands in for it.

**Buttons order** — in `build` mode the checkout creates a second Nexi order,
whose reference is the transaction reference plus `-m`, used only for the
alternative payment methods. Two orders, one payment: whoever reads the state
has to know which one it is looking at.

**Authorization** — the card is blocked, no money has moved. An authorized order
is not a paid one.

**Capture** — the money is taken. With automatic capture Nexi answers with both
an AUTHORIZATION and a CAPTURE; the capture is the one that says the payment
is in.

**Reversal** — a VOID or a REFUND after a capture. A capture already executed is
not undone by a later decline. A reversal that covers the whole capture, or
whose amount cannot be read, means the money is not in: the verdict cancels
(`reversed`). A partial reversal leaves the payment standing. Measured by
`reversed_after_capture` in `models/nexi_verdict.py`.

## Outcome concepts

**State** — Odoo's word for where the transaction stands: `draft`, `pending`,
`done`, `cancel`, `error`. Spelled in the code as `payment.transaction.state`.
Never call this "outcome" in new code: it has its own name.

**Verdict** — the decision about what a Nexi answer means: paid, authorized,
declined, undecided, or unusable, with the figures and whether a human is
needed. Produced by `models/nexi_verdict.py`, which owns no state and talks to
nothing. The model applies it.

**Reason** — the precise why inside a verdict (`paid`, `capture_incomplete`,
`amount_mismatch`, …). The model turns a reason into the message the customer
reads, so the policy carries codes and never prose.

**Uncertain** — the transaction carries `nexi_uncertain = True`: the outcome is
not known and the cron owns the follow-up. It is written on errors only; a
pending transaction is retried because of its state, not because of this flag.

**Retry** — the gateway has not decided yet (`PENDING`, `THREEDS_VALIDATED`, …)
and asking again in the same pass is worth it. Distinct from `uncertain`: one is
a wait, the other is a doubt.

**Amount mismatch** — more money was taken than the order asked for. Never
closed automatically: a human refunds it from the Nexi back office.

## Amounts

**Cents** — every figure compared inside the verdict is integer cents
(`10.00 EUR` is `1000`). Nexi writes integer cents in most fields but decimals
in some: `parse_cents` reads both, and never with a float.

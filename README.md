# payment_nexi — Nexi XPay Web for Odoo 18

Payment provider for **Nexi XPay Web** (REST API with `X-Api-Key`).
Two modes: **Hosted Page** (redirect to the Nexi page) and **Embedded Build v3**
(card fields inside the Odoo checkout). Odoo never sees the card number.

## What it does

- Creates the order on Nexi and shows the payment page (or the fields in the checkout).
- Always verifies the outcome **server-side** with `GET /orders/{reference}`: the browser return is never trusted.
- Marks the transaction `done` only with an `EXECUTED` outcome and the correct amount and currency.
- `AUTHORIZED` = only authorized, not captured.
- `CANCELED`, `THREEDS_FAILED`, `DENIED`, `ERROR`, `DECLINED` = transaction cancelled.
- Stores only the masked PAN (`**** 1234`), never the full number or the CVV.

## Included methods

| Method | Nexi code |
|---|---|
| Card | `CARDS` |
| Google Pay | `GOOGLEPAY` |
| Apple Pay | `APPLEPAY` |
| Bancomat Pay | `BANCOMATPAY` |
| MyBank | `MYBANK` |
| PayPal | `PAYPAL` |
| PayPal Pay in 3 | `PAYPAL_PAGAIN3` |

Methods not enabled on the terminal are hidden with the
**Check methods on Nexi** button in the provider form.

## Requirements

- Odoo 18 (`payment` module).
- Nexi XPay Web terminal with `X-Api-Key`.
- Currency **EUR** (Nexi rejects the others).
- The Embedded mode needs a **public URL**: Nexi does not accept `localhost`.

## Installation

1. Copy the `payment_nexi` folder into the Odoo addons.
2. Restart Odoo and install **Nexi XPay Web** from Apps.
3. Go to *Payment Providers → Nexi XPay Web* and fill in the fields.
4. Publish the provider.
5. Make sure a payment method line linked to the provider exists
   (*Accounting → Configuration → Payment Methods*), otherwise Odoo does not create the transaction.

## Configuration

| Field | What to enter |
|---|---|
| `X-Api-Key` | XPay Web terminal key |
| Environment | Test or Production |
| Nexi URL (test/production) | Endpoint `…/psp/api/v1/orders/hpp` |
| Mode | **Hosted Page** or **Embedded (Build v3)** |
| Shop public base | Empty = `web.base.url`. Enter the public domain (needed for returns and for Build) |
| Preselected method | Empty = all, or e.g. `GOOGLEPAY` |
| OneClick | Creates and reuses a contract for saved cards (Hosted Page) |
| Check methods on Nexi | Hides terminal-disabled methods at checkout |

The test keys are the ones published by Nexi in the XPay documentation
(*Introduction → API key*). They must not be committed to the repository.

## Sandbox testing

1. Set the provider to **Test** with the Nexi sandbox key.
2. Buy a product, choose Nexi and pay with one of the test cards published
   by Nexi (e.g. `4999340261977289`, expiry `12/30`, CVV `663`: fake sandbox data).
3. On the test 3DS page choose *AUTHENTICATION SUCCESSFUL*.
4. The transaction must end up `done` with `Nexi OK: EXECUTED`.

## Known limits

- No refunds and no manual capture: capture is always automatic.
- EUR only.
- PayPal Pay in 3 requires an order of at least €30 (Nexi limit).
- The Embedded mode card fields are Nexi iframes: inner border, height and width cannot be customized.
- The Embedded mode requires HTTPS and a domain reachable by Nexi.

## Layout

```
payment_nexi/
├── __init__.py                     # installation hook
├── __manifest__.py
├── controllers/main.py             # Nexi return + Build routes
├── data/payment_method_data.xml    # payment methods
├── models/
│   ├── payment_provider.py         # fields, API calls, URLs
│   ├── payment_method_nexi.py      # methods visible at checkout
│   └── payment_transaction.py      # order creation, outcome, OneClick
├── static/description/             # marketplace card and icon
└── views/payment_nexi_templates.xml
```

## Security

- Every transaction has a random code (`tok`): without the right code the routes
  answer "not found" without touching Nexi or the payment.
- The order to verify is decided by the transaction, never by the caller; the
  Nexi response must refer to the same order, and the amount is compared in
  cents (same unit on both sides).
- Nexi data (`contractId`, sessions, masked PAN) is written by the system only:
  external writes via RPC are rejected.
- Saved cards are visible only to their owner; contracts are random,
  not derived from IDs.
- Rate limits on public routes; no redirects carrying the API key;
  `Shop public base` (or `web.base.url`) for resultUrl and merchantUrl, never
  the Host header.
- Only generic messages to the browser; details stay in the server logs.

## License

LGPL-3. See [LICENSE](LICENSE).

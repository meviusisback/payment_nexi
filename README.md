<div align="center">

# payment_nexi

**Odoo 18 payment provider for Nexi XPay Web (REST API with `X-Api-Key`): Hosted Page redirect or embedded card fields at checkout. Odoo never sees the card number.**

[![License: LGPL-3](https://img.shields.io/github/license/meviusisback/payment_nexi)](LICENSE)
![Version](https://img.shields.io/badge/version-1.3.0-blue)
![Linux](https://img.shields.io/badge/linux-supported-1793d1)

![Module icon](static/description/icon.png)

</div>

## Why it exists

Selling online with Odoo on an Italian Nexi terminal means choosing between sending the customer to a hosted payment page or keeping them at checkout — without ever touching card data. This module covers both paths through the Nexi XPay Web REST API, then closes the loop the only safe way: it re-reads the order outcome **server-side** and marks the transaction `done` only when Nexi reports `EXECUTED` for the right amount and currency.

## Features

- **Two checkout modes**: **Hosted Page** (redirect to the Nexi page) and **Embedded Build v3** (card fields inside the Odoo checkout, rendered as Nexi iframes).
- **Server-side verification only**: the outcome is always re-checked with `GET /orders/{reference}` — the browser return is never trusted.
- **Strict close-out**: the transaction becomes `done` only with an `EXECUTED` outcome and matching amount (compared in cents, same unit on both sides) and currency.
- **Correct outcome with automatic capture**: reads the **last operation among `AUTHORIZATION` and `CAPTURE`** — with automatic capture Nexi sends both, and looking only at the authorization left paid orders stuck in `pending`.
- **Explicit outcome mapping**: `AUTHORIZED` stays authorized-but-not-captured; `CANCELED`, `THREEDS_FAILED`, `DENIED`, `DENIED_BY_RISK`, `ERROR`, `DECLINED`, `FAILED` and `VOIDED` cancel the transaction; `THREEDS_VALIDATED` (3DS passed, payment still running) waits briefly like `PENDING`.
- **Seven payment methods** with per-terminal availability:

| Method | Nexi code |
|---|---|
| Card | `CARDS` |
| Google Pay | `GOOGLEPAY` |
| Apple Pay | `APPLEPAY` |
| Bancomat Pay | `BANCOMATPAY` |
| MyBank | `MYBANK` |
| PayPal | `PAYPAL` |
| PayPal Pay in 3 | `PAYPAL_PAGAIN3` |

- **Per-terminal method check**: the **Check methods on Nexi** button tests the terminal and stores the result **for that provider only** — two Nexi providers in the same database can offer different methods. A timeout or broken configuration never hides a method, only an explicit refusal from Nexi (*service not enabled*) does. The `Hidden on every Nexi terminal` flag on the payment method hides it everywhere, by hand.
- **OneClick saved cards** (Hosted Page): creates and reuses a Nexi contract per terminal. At checkout the saved cards are listed, the card the customer picks wins over automatic reuse, and *Use a new card* really starts a new card without sending a stored contract.
- **No card data in Odoo**: only the masked PAN (`**** 1234`) is stored, never the full number or the CVV.
- **No refunds, no manual capture**: the module only reads `AUTHORIZATION` and `CAPTURE`, so a refund done from the Nexi back office leaves the transaction `done` — record it by hand in Odoo.

## Install

1. Copy the `payment_nexi` folder into the Odoo addons path.
2. Restart Odoo and install **Nexi XPay Web** from Apps.
3. Go to *Payment Providers → Nexi XPay Web* and fill in the fields (see [Configuration](#configuration)).
4. Publish the provider.
5. Make sure a payment method line linked to the provider exists (*Accounting → Configuration → Payment Methods*), otherwise Odoo does not create the transaction.

```bash
cp -r payment_nexi /path/to/odoo/addons/
# restart Odoo, then install "Nexi XPay Web" from Apps
```

## Usage

1. Set the provider to **Test** with the Nexi sandbox key.
2. Buy a product, choose Nexi and pay with one of the test cards published by Nexi (e.g. `4999340261977289`, expiry `12/30`, CVV `663`: fake sandbox data).
3. On the test 3DS page choose *AUTHENTICATION SUCCESSFUL*.
4. The transaction must end up `done` with `Nexi OK: EXECUTED`.

Notes:

- Use **Check methods on Nexi** after configuring a terminal to store which of the seven methods that terminal really supports.
- PayPal Pay in 3 requires an order of at least €30 (Nexi limit).
- The Embedded card fields are Nexi iframes: inner border, height and width cannot be customized, and the mode requires HTTPS on a domain reachable by Nexi.

## Requirements

- Odoo 18 with the `payment`, `website` and `website_sale` modules (the module's `depends`).
- Python package `requests` (external dependency).
- A Nexi XPay Web terminal with its `X-Api-Key` (the test keys are the ones published by Nexi in the XPay documentation — never commit them to the repository).
- Currency **EUR** (Nexi rejects the others).
- A **public URL** for Embedded mode: Nexi does not accept `localhost`.

## Configuration

| Field | What to enter |
|---|---|
| `X-Api-Key` | XPay Web terminal key |
| Environment | Test or Production |
| Nexi URL (test/production) | Endpoint `…/psp/api/v1/orders/hpp` |
| Mode | **Hosted Page** or **Embedded (Build v3)** |
| Shop public base | Empty = `web.base.url`. Enter the public domain (needed for return URLs and for Build) |
| Preselected method | Empty = all, or e.g. `GOOGLEPAY` |
| OneClick | Creates and reuses a contract for saved cards (Hosted Page) |
| Check methods on Nexi | Tests the terminal and stores the result **for this provider only** |

Cards are per terminal: a contract created on one Nexi provider is not offered on another one.

## How it works

Every transaction carries a random code (`tok`): without the right code the public routes answer "not found" without touching Nexi or the payment. The order to verify is decided by the transaction, never by the caller; the Nexi response must refer to the same order, and the amount is compared in cents. Nexi data (`contractId`, sessions, masked PAN) is written by the system only — external writes via RPC are rejected. Saved cards are visible to the owner **and to the other contacts of the same company** (the contract belongs to the commercial partner). Nexi notifications are counted per transaction, browser returns per IP; only generic messages reach the browser, details stay in the server logs.

```
payment_nexi/
├── __init__.py                     # installation hook
├── __manifest__.py                 # version 1.3.0, depends, data files
├── controllers/main.py             # Nexi return + Build routes
├── data/payment_method_data.xml    # payment methods
├── models/
│   ├── payment_provider.py         # fields, API calls, URLs
│   ├── payment_method_nexi.py      # methods visible at checkout
│   ├── payment_nexi_method_availability.py  # methods per terminal
│   └── payment_transaction.py      # order creation, outcome, OneClick
├── security/ir.model.access.csv
├── static/description/             # marketplace card and icon
├── tests/                          # Odoo tests + standalone tests
└── views/payment_nexi_templates.xml
```

## Docs

- [LICENSE](LICENSE)
- [__manifest__.py](__manifest__.py) — version, `depends`, data files
- [controllers/main.py](controllers/main.py) — Nexi return and Build routes
- [models/payment_provider.py](models/payment_provider.py) — provider fields and API calls
- [models/payment_transaction.py](models/payment_transaction.py) — order creation, outcome check, OneClick
- [views/payment_nexi_templates.xml](views/payment_nexi_templates.xml) — checkout templates
- [security/ir.model.access.csv](security/ir.model.access.csv) — access rights

## Contributing

```bash
# Pure logic, no Odoo: domains, outcome, limits, session
python3 -m unittest discover -s tests/standalone -t .

# With Odoo: install the module in a test database
odoo -d nexi_test -i payment_nexi --test-enable --test-tags /payment_nexi --stop-after-init
```

The Odoo suite covers the outcome with `AUTHORIZATION` + `CAPTURE`, the pending authorization, the amount mismatch, the per-terminal method visibility and the currency/method defaults.

## License

LGPL-3 — see [LICENSE](LICENSE).

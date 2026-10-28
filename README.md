# payment_nexi — Nexi XPay Web per Odoo

Paga con Nexi XPay Web. Solo redirect Hosted Page.
Odoo non vede mai la carta. Semplice e veloce.

## Cosa fa

- Crea ordine su Nexi con `X-Api-Key` + `Correlation-Id`.
- Ti manda su pagina Nexi, poi torni su Odoo.
- Verifica con `GET /orders/{ref}` prima di segnare pagato.
- `EXECUTED` + importo e valuta giusti = fatto.
- `AUTHORIZED` = solo bloccato, non incassato.
- `CANCELED`, `THREEDS_FAILED`, `DENIED`, `ERROR` = annullato.

## Cosa NON fa (v1)

- Niente token salvati.
- Niente rimborso (`support_refund=none`).
- Niente cattura manuale (`support_manual_capture=False`).
- Solo EUR.

## Installa

1. Copia `payment_nexi` in `addons` di Odoo 18.
2. Riavvia Odoo, Installa `Nexi XPay Web` da App.
3. Vai su Fornitori pagamento > Nexi:
   - Ambiente: Test
   - `X-Api-Key`: chiave test
   - `URL test Nexi`: es. `https://xpaysandbox.nexigroup.com/api/phoenix-0.0/psp/api/v1/orders/hpp`
   - Incasso: Automatico
   - Pubblica.
4. Collega metodo `Carta via Nexi`.
5. Collega riga pagamento Bank a Nexi (serve per creare pagamento).

## Prova

1. Shop > prodotto 1 €, cassa, scegli Nexi.
2. Carta test `4999340261977289` 12/30 663.
3. Al 3D clicca successo.
4. Torni su `/payment/status`. Vedi `done`.

Ordine test vero:
- Sandbox: `https://xpaysandbox.nexigroup.com/api/phoenix-0.0/psp/api/v1/orders/hpp`
- Chiave test auto: `8372f58a-3e55-45f4-a71b-b08b810757ae`
- Chiave test manuale: `9ceae112-ad7b-4051-96e3-bc02746bb068`

## Produzione

1. Da Nexi Business > Back Office XPay Web > Admin > APIKey crea chiave.
2. Metti Ambiente Produzione + URL produzione + chiave vera.
3. Prova con 1 € vero, poi rimborsa.

## Marketplace Odoo

- Carica su `apps.odoo.com/apps/upload` da repo Git.
- Prezzo in EUR/USD. Fee 30%.
- Niente review prima. Rimozione solo su abusi R1-R6.
- Manca `icon.png` 1024x1024: aggiungi in `static/description/` prima di pubblicare.

## Sicurezza

- Chiave solo per admin sistema, mai in log.
- URL solo `https` su domini Nexi.
- Ref con regex, errori generici.
- `cancel` in URL ignorato, vale solo verifica Nexi.
- Timeout corto, messaggi utente generici.

## Struttura

- `models/payment_provider.py`: credenziali, URL, chiamata Nexi.
- `models/payment_transaction.py`: crea ordine, redirect, verifica.
- `controllers/main.py`: ritorno `/payment/nexi/confirm`.
- `views/`: form redirect + campi provider.
- `tests/`: logica pura senza Odoo.

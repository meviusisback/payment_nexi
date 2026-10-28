# payment_nexi v1 — limiti

Solo redirect Hosted Page Nexi. Niente carte in Odoo.

Cosa fa:
- Crea ordine su Nexi sandbox/produzione con X-Api-Key + Correlation-Id.
- Redirect a hostedPage con controllo dominio https Nexi.
- Ritorno su /payment/nexi/confirm, verifica server GET /orders/{ref}.
- EXECUTED + importo e valuta giusti = done.
- AUTHORIZED = solo autorizzato, non incassato.
- KO = annullato.

Cosa NON fa in v1:
- Niente token salvati.
- Niente rimborso (support_refund=none, metodo alza errore).
- Niente cattura manuale (support_manual_capture=False).
- Solo EUR.

Test:
- Carte sandbox Nexi, mai carte vere.
- Chiavi test solo per sandbox.

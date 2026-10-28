# Stato attuale — passaggio a un altro sviluppatore

Ultimo commit: `0cc0775` (repo `meviusisback/payment_nexi`, branch `main`).
Ambiente di test: Odoo 18 in Docker (`docker-test-odoo-1`, db `odoo`, admin/admin),
modulo montato in `/mnt/extra-addons/payment_nexi`, provider Nexi id 18,
`nexi_mode = build`, `nexi_env = test`.

## Cosa funziona

- HPP (modalità `hpp`): pagamento completo end-to-end, verificato più volte
  (`S00001-5` done, `****7289`, esito `EXECUTED`).
- Metodi: Carta, Google Pay, Apple Pay, Bancomat Pay, MyBank, PayPal,
  PayPal Paga in 3. Ognuno ha il suo `paymentService`.
- Bottone `Verifica metodi su Nexi` sul provider: prova ogni metodo sul
  terminale e mette `nexi_disabled` su quelli rifiutati (nascondendoli in cassa).
- Contratto CIT morto: viene dimenticato e si riprova senza (`_nexi_post_order`).
- Errori Nexi leggibili: `_nexi_error_detail` riporta `description [code]`.
- Carte salvate nella riga Carta (solo HPP): `**** 7289`, radio di scelta e
  "Usa una nuova carta".
- `POST /payment/nexi/build/prepare` (JSON): crea/riusa la transazione draft,
  crea due ordini Build su Nexi (`REF` per i campi carta, `REF-m` per i bottoni
  di tutti gli APM) e restituisce `fields`, `buttons`, `sdk`, `finalize_url`.
  Verificato da shell e da curl: 5 campi carta + 15 bottoni.

## Stato Build in cassa (risolto)

Verificato con browser vero sulla cassa:

- riga Carta: numero carta, scadenza, CVV, intestatario, email, privacy;
- righe APM: solo il bottone Nexi (radio e testi nascosti col CSS);
- righe senza bottone (es. PayPal Paga in 3) nascoste da sole;
- la carta resta chiusa all'avvio e si apre al click;
- loghi circuiti 39x26 allineati.

Due cause risolte, da non reintrodurre:

1. i contenitori Build devono stare FUORI da `div[name=o_payment_inline_form]`
   (che Odoo tiene `d-none`), altrimenti i campi esistono ma sono invisibili;
2. `merchantUrl` deve essere l'host reale della richiesta
   (`request.httprequest.host_url`, con `X-Forwarded-Proto`), non
   `web.base.url`: Nexi confronta il dominio degli iframe e con localhost
   rifiuta di disegnarli.

## File chiave

- `models/payment_transaction.py` — payload Build, `_nexi_post_order`,
  `_nexi_render_build_values`, notifiche, carte salvate.
- `controllers/main.py` — `/payment/nexi/confirm`,
  `/payment/nexi/build/<ref>`, `/prepare`, `/fields`, `/finalize`,
  `/jserror`.
- `views/payment_nexi_templates.xml` — form HPP, pagina Build, logica inline
  nella riga Carta (è qui il problema aperto).
- `models/payment_method_nexi.py` — filtro `nexi_disabled`.
- `tests/test_nexi.py` — 9 test verdi, puri (senza Odoo né rete).

## Note

- Le credenziali di test sono nella doc Nexi (X-Api-Key `8372f58a-...`),
  carta `4999340261977289`, 12/30, CVV 663.
- Build non funziona su `localhost`: serve un URL pubblico (`web.base.url`
  puntato al tunnel cloudflared).
- Refund e cattura manuale sono disattivati (v1).

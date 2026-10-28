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

## Cosa non funziona (problema aperto)

In cassa, modalità Build, il browser **non renderizza** quello che il server
restituisce:

- la riga Carta resta vuota (nessun campo carta visibile);
- le righe APM mostrano solo il bottone Bancomat Pay (un iframe) e le altre
  righe restano vuote;
- il `POST /payment/nexi/build/prepare` arriva al server e risponde con i dati
  giusti (log: `prepare S00011-8 card_fields=5`), ma la pagina non cambia.

Cose già escluse:
- il server: la risposta è corretta, verificata con curl autenticato e da shell;
- il provider: `nexi_mode = build`, `state = test`, `is_published = True`;
- l'upgrade del modulo: un `XMLSyntaxError` in un commit precedente aveva
  impedito l'aggiornamento, ora risolto (`503c5cd`);
- `:has()` non supportato: Firefox 158 lo supporta.

Piste da guardare, in ordine:
1. ordine di esecuzione del JS dentro `views/payment_nexi_templates.xml`
   (`xpath //div[p[@name='o_payment_secured_by']] position="after"`): lo script
   gira quando le righe successive non esistono ancora;
2. la funzione `prepare()` viene chiamata tre volte (al load, sul change, sul
   pre-checked): c'è un `done` condiviso che potrebbe bloccare il secondo giro
   lasciando la UI a metà;
3. log browser: esiste `POST /payment/nexi/build/jserror` che registra gli
   errori JS (`docker logs docker-test-odoo-1 | grep browser`), ma finora non ha
   ricevuto nulla — se il renderer muore senza eccezione, cercare altrove;
4. verificare con la console del browser cosa contiene
   `#nexi-inline-fields` dopo il prepare.

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

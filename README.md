# payment_nexi — Nexi XPay Web per Odoo 18

Provider di pagamento per **Nexi XPay Web** (API REST con `X-Api-Key`).
Due modalità: **Hosted Page** (redirect alla pagina Nexi) ed **Embedded Build v3**
(campi carta dentro la cassa Odoo). Odoo non vede mai il numero di carta.

## Cosa fa

- Crea l'ordine su Nexi e mostra la pagina di pagamento (o i campi in cassa).
- Verifica sempre l'esito **lato server** con `GET /orders/{riferimento}`: il ritorno del browser non è mai credibile.
- Segna la transazione `done` solo con esito `EXECUTED`, importo e valuta corretti.
- `AUTHORIZED` = solo autorizzato, non incassato.
- `CANCELED`, `THREEDS_FAILED`, `DENIED`, `ERROR` = transazione annullata.
- Salva solo il PAN mascherato (`**** 1234`), mai il numero completo né il CVV.

## Metodi inclusi

| Metodo | Codice Nexi |
|---|---|
| Carta | `CARDS` |
| Google Pay | `GOOGLEPAY` |
| Apple Pay | `APPLEPAY` |
| Bancomat Pay | `BANCOMATPAY` |
| MyBank | `MYBANK` |
| PayPal | `PAYPAL` |
| PayPal Paga in 3 | `PAYPAL_PAGAIN3` |

I metodi non abilitati sul terminale si nascondono con il pulsante
**Verifica metodi su Nexi** nel form del provider.

## Requisiti

- Odoo 18 (modulo `payment`).
- Terminale Nexi XPay Web con `X-Api-Key`.
- Valuta **EUR** (Nexi rifiuta le altre).
- Per la modalità Embedded serve un **URL pubblico**: Nexi non accetta `localhost`.

## Installazione

1. Copia la cartella `payment_nexi` negli addons di Odoo.
2. Riavvia Odoo e installa **Nexi XPay Web** da App.
3. Vai su *Fornitori di pagamento → Nexi XPay Web* e compila i campi.
4. Pubblica il provider.
5. Controlla che esista una riga di pagamento collegata al provider
   (*Contabilità → Configurazione → Metodi di pagamento*), altrimenti Odoo non crea la transazione.

## Configurazione

| Campo | Cosa mettere |
|---|---|
| `X-Api-Key` | Chiave del terminale XPay Web |
| Ambiente | Test o Produzione |
| URL Nexi (test/produzione) | Endpoint `…/psp/api/v1/orders/hpp` |
| Modalità | **Hosted Page** o **Embedded (Build v3)** |
| Metodo preselezionato | Vuoto = tutti, oppure es. `GOOGLEPAY` |
| OneClick | Crea e riusa un contratto per le carte salvate (Hosted Page) |
| Verifica metodi su Nexi | Nasconde in cassa i metodi non attivi sul terminale |

Le chiavi di test sono quelle pubblicate da Nexi nella documentazione XPay
(*Introduzione → API key*). Non vanno inserite nel repository.

## Prova in sandbox

1. Metti il provider in **Test** con la chiave sandbox di Nexi.
2. Compra un prodotto, scegli Nexi e paga con una delle carte di test pubblicate
   da Nexi (es. `4999340261977289`, scadenza `12/30`, CVV `663`: dati finti di sandbox).
3. Sulla pagina 3DS di prova scegli *AUTENTICAZIONE RIUSCITA*.
4. La transazione deve risultare `done` con `Nexi OK: EXECUTED`.

## Limiti noti

- Nessun rimborso e nessuna cattura manuale: l'incasso è sempre automatico.
- Solo EUR.
- PayPal Paga in 3 richiede un ordine di almeno 30 € (limite Nexi).
- I campi carta della modalità Embedded sono iframe di Nexi: bordo, altezza e larghezza interni non sono personalizzabili.
- La modalità Embedded richiede HTTPS e un dominio raggiungibile da Nexi.

## Struttura

```
payment_nexi/
├── __init__.py                     # hook di installazione
├── __manifest__.py
├── controllers/main.py             # ritorno Nexi + rotte Build
├── data/payment_method_data.xml    # metodi di pagamento
├── models/
│   ├── payment_provider.py         # campi, chiamate API, URL
│   ├── payment_method_nexi.py      # metodi visibili in cassa
│   └── payment_transaction.py      # creazione ordine, esito, OneClick
├── static/description/             # scheda e icona per il marketplace
└── views/payment_nexi_templates.xml
```

## Licenza

LGPL-3. Vedi [LICENSE](LICENSE).

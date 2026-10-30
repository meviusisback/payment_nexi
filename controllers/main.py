# -*- coding: utf-8 -*-
"""Ritorno da Nexi. URL breve, una sola cosa: trova la transazione e falla verificare."""
import hashlib
import json
import logging
import time

from odoo import http
from odoo.http import request
from odoo.exceptions import ValidationError

from ..models.payment_transaction import _nexi_safe_fields

_logger = logging.getLogger(__name__)

# Limiti semplici per IP: niente abusi di conferma, ordini o log.
# Vivono nella memoria del processo: ogni worker ha i suoi contatori.
_NEXI_RL = {}

# Ritorno del browser: per IP va bene, è un cliente per volta.
BROWSER_LIMIT = 30
# Notifiche Nexi: arrivano tutte dallo stesso IP del terminale e riguardano
# clienti diversi, quindi si contano per transazione (tok) e non per IP.
NOTIFY_LIMIT = 60


def _nexi_rl_allow(key, limit, window=60):
    now = time.time()
    if len(_NEXI_RL) > 2000:
        # Butto solo i secchielli SCADUTI: prima si cancellavano i 1000 più
        # vecchi, così chiunque poteva azzerare i contatori degli altri
        # riempiendo il dizionario con chiavi nuove.
        expired = [k for k, v in _NEXI_RL.items() if not v or now - v[-1] >= window]
        for k in expired[:2000]:
            _NEXI_RL.pop(k, None)
        if len(_NEXI_RL) > 4000:
            # Troppe chiavi attive anche dopo la pulizia: meglio rifiutare che
            # crescere senza limite.
            return False
    lst = [t for t in (_NEXI_RL.get(key) or []) if now - t < window]
    if len(lst) >= limit:
        _NEXI_RL[key] = lst
        return False
    lst.append(now)
    _NEXI_RL[key] = lst
    return True


def _nexi_rl_bucket(value, size=32):
    """Parte di chiave a dimensione fissa: il chiamante non decide lo spazio."""
    return hashlib.sha256(str(value or '').encode('utf-8', 'replace')).hexdigest()[:size]


def _nexi_rl_key(name):
    try:
        ip = request.httprequest.remote_addr or '?'
    except Exception:
        ip = '?'
    return 'nexi:%s:%s' % (name, ip)


def _nexi_tok():
    """Codice della transazione: da query string o da body JSON."""
    try:
        tok = request.httprequest.args.get('tok')
    except Exception:
        tok = None
    return tok


def _nexi_confirm_limit(is_notify, kw):
    """(chiave, limite) del limitatore per /payment/nexi/confirm.

    Il terminale Nexi manda le notifiche dallo stesso IP per tutti i clienti:
    contarle per IP bloccherebbe un negozio trafficato. Il ritorno del browser
    invece resta per IP, perché lì è un cliente per volta.
    """
    if is_notify:
        # Chiave a dimensione fissa: il tok lo sceglie il chiamante, non deve
        # decidere quanto occupa in memoria né quanto vive il secchiello.
        return 'nexi:notify:%s' % _nexi_rl_bucket((kw or {}).get('tok')), NOTIFY_LIMIT
    return _nexi_rl_key('confirm'), BROWSER_LIMIT


def _nexi_tx_or_404(reference):
    """Transazione solo con il suo codice. Senza codice giusto: 404, senza chiamare Nexi."""
    tx = request.env['payment.transaction'].sudo().search([
        ('reference', '=', reference), ('provider_code', '=', 'nexi'),
    ], limit=1)
    if not tx:
        return None
    if not tx.nexi_access_token or _nexi_tok() != tx.nexi_access_token:
        return None
    return tx


def _nexi_lock_row(cr, tx_id, nowait=True):
    """Blocca la riga della transazione. Con nowait: False se è già presa.

    La verifica su Nexi può durare decine di secondi: chi arriva secondo non
    deve mettersi in coda dietro al primo, altrimenti un worker resta fermo
    quanto la rete. Il savepoint tiene la transazione usabile se il lock non
    c'è.
    """
    sql = (
        'SELECT id FROM payment_transaction WHERE id = %s FOR UPDATE'
        + (' NOWAIT' if nowait else '')
    )
    try:
        with cr.savepoint():
            cr.execute(sql, (tx_id,))
    except Exception:
        return False
    return True


def _nexi_landing_route(landing, has_order):
    """Dove mandare il browser dopo il pagamento.

    La rotta decisa dal flusso (cassa, portale, fattura) resta com'è: solo se
    manca o punta alla pagina di stato la sostituiamo, altrimenti il polling
    ricarica all'infinito. Per un ordine del negozio la meta è la validazione
    della cassa; per un pagamento senza ordine (fattura, portale) non c'è una
    cassa da validare, quindi si lascia la rotta originale.
    """
    if landing and not str(landing).startswith('/payment/status'):
        return landing
    return '/shop/payment/validate' if has_order else (landing or '/payment/confirmation')


def _nexi_public_error(e, source='Nexi'):
    """Al browser solo messaggi sicuri, il resto resta nei log."""
    msg = str(e) or ''
    safe = (
        'PayPal Paga in 3 richiede almeno 30,00',
        'Nexi attiva solo in EUR',
        'Manca la X-Api-Key',
        'Build vuole URL pubblico',
        'Metodo Nexi non valido',
        'Metodo non disponibile su questo terminale',
        'Metodo carta mancante',
        'Ordine non valido',
        'Ordine già pagato',
        'Build non attiva',
        'Troppe richieste',
        'Il tuo carrello non è pronto',
        'Your cart is not ready',
        'No shipping method is selected',
        'The delivery method is not compatible',
    )
    if msg.startswith(safe):
        return msg[:200]
    _logger.warning('%s errore interno: %s', source, msg[:300])
    return 'Pagamento non disponibile. Riprova.'


def _nexi_check_url(url):
    """URL da Nexi: solo https su domini Nexi, niente redirect a giro.

    La regola dei domini vive in un posto solo (models/payment_provider.py),
    così controller e provider non possono divergere: senza
    xpay.nexigroup.com la produzione veniva rifiutata.
    """
    from odoo.addons.payment_nexi.models.payment_provider import is_nexi_url
    return is_nexi_url(url)


class NexiController(http.Controller):
    # --- Aiutanti condivisi ---

    def _nexi_order_ready(self, order):
        """Le stesse precondizioni della cassa Odoo. None = si può pagare.

        Il vecchio prepare si fidava solo dell'importo: passava con un carrello
        senza spedizione scelta, con l'ordine annullato o già pagato.
        """
        if not order or not order.id:
            return 'Ordine non valido.'
        if order.state == 'cancel':
            return 'Ordine non valido.'
        try:
            # Metodo di website_sale: carrello pronto + spedizione valida.
            order._check_cart_is_ready_to_be_paid()
        except Exception as e:
            # Al browser solo i messaggi previsti: il resto (testo di un modulo
            # terzo, errore ORM) resta nel log, come per tutti gli altri errori.
            return _nexi_public_error(e, source='Carrello')
        compare = order.currency_id.compare_amounts
        if compare(order.amount_paid, order.amount_total) == 0:
            return 'Ordine già pagato.'
        if order.amount_total <= 0:
            return 'Ordine non valido.'
        return None

    def _nexi_lock_order(self, order):
        """Blocca l'ordine come fa Odoo: due schede non pagano due volte.

        Savepoint: se il lock non c'è (un altro pagamento è in corso) la
        transazione resta usabile e rispondiamo "riprova" invece di romperla.
        """
        try:
            with request.env.cr.savepoint():
                request.env.cr.execute(
                    'SELECT 1 FROM sale_order WHERE id = %s FOR NO KEY UPDATE NOWAIT',
                    (order.id,),
                )
        except Exception:
            return False
        return True

    def _nexi_lock_tx(self, tx):
        """Blocca la riga senza aspettare (vedi _nexi_lock_row)."""
        return _nexi_lock_row(request.env.cr, tx.id)

    def _nexi_finish(self, tx, is_notify):
        """Risposta finale: notifica -> OK, browser -> pagina di stato."""
        if is_notify:
            return 'OK'
        # Dillo alla pagina di stato: senza questa chiave in sessione
        # Odoo non trova il pagamento e mostra "in attesa".
        from odoo.addons.payment.controllers.post_processing import PaymentPostProcessing
        PaymentPostProcessing.monitor_transaction(tx)
        return request.redirect('/payment/status')

    def _nexi_provider(self, order, mode=None, provider_id=None):
        """Provider Nexi compatibile con l'ordine, l'azienda, il SITO e la valuta.

        Le regole sono quelle di Odoo (azienda, stato, pubblicazione, paese,
        valuta) più il sito: `website_payment` filtra per `provider.website_id`
        solo se riceve `website_id`, quindi senza quel parametro due siti della
        stessa azienda si rubavano il terminale.
        """
        providers = request.env['payment.provider'].sudo()._get_compatible_providers(
            order.company_id.id, order.partner_id.id, order.amount_total,
            order.currency_id.id,
            website_id=request.website.id if request.website else None,
        )
        providers = providers.filtered(lambda p: p.code == 'nexi')
        if mode:
            providers = providers.filtered(lambda p: p.nexi_mode == mode)
        if provider_id:
            # Il provider chiesto dal browser vale solo se è fra i compatibili:
            # un id inventato non deve scegliere il terminale.
            providers = providers.filtered(lambda p: p.id == provider_id)
        elif len(providers) > 1:
            _logger.warning(
                'Nexi: %s provider compatibili con l\'ordine %s (%s), uso il primo '
                'per sequenza. Configura un solo provider per sito.',
                len(providers), order.name, providers.ids,
            )
        return providers.sorted(lambda p: (p.sequence, p.id))[:1]

    @http.route('/payment/nexi/saved-card', type='json', auth='public', website=True, methods=['POST'], csrf=False)
    def nexi_saved_card(self, contract_id=None, **kw):
        """Ricorda quale carta salvata ha scelto il cliente per questo ordine.

        Il form di Odoo non porta campi extra fino al provider: la scelta
        viaggia in sessione e viene riletta in _get_specific_rendering_values.
        """
        if not _nexi_rl_allow(_nexi_rl_key('saved'), 30):
            return {'error': 'Troppe richieste. Riprova tra un minuto.'}
        order = request.website.sale_get_order()
        partner = (order.partner_invoice_id or order.partner_id) if order else request.env.user.partner_id
        if not partner:
            return {'ok': True}
        choice = str(contract_id or '').strip()
        if choice:
            # Vale per il partner, non per l'ordine: così funziona anche per una
            # fattura pagata dal portale, dove un carrello non esiste. Una sola
            # ricerca per i contratti, e solo i terminali dell'azienda giusta.
            company_id = order.company_id.id if order else request.env.company.id
            providers = request.env['payment.provider'].sudo().search([
                ('code', '=', 'nexi'),
                ('company_id', 'in', [False, company_id]),
            ])
            cards = {
                c['contract_id']
                for c in request.env['payment.transaction'].sudo().get_nexi_saved_cards(
                    partner.id, providers.ids
                )
            }
            # Contratto che non è del cliente: vale come "carta nuova".
            choice = choice if choice in cards else ''
        request.session['nexi_saved_contract'] = {
            'partner_id': partner.commercial_partner_id.id or partner.id,
            'contract_id': choice,
        }
        return {'ok': True}

    @http.route('/payment/nexi/confirm', type='http', auth='public', csrf=False, methods=['GET', 'POST'])
    def nexi_confirm(self, **kw):
        # Il terminale Nexi manda le notifiche dallo stesso IP per tutti i
        # clienti: contarle per IP bloccherebbe un negozio trafficato. Il
        # browser invece resta limitato per IP.
        is_notify = request.httprequest.method == 'POST'
        key, limit = _nexi_confirm_limit(is_notify, kw)
        if not _nexi_rl_allow(key, limit):
            return request.make_response('Troppe richieste. Riprova tra un minuto.', status=429)
        # kw = parametri che Nexi aggiunge al ritorno (ref, cancel, ...).
        if is_notify and not kw.get('ref'):
            # La notifica arriva come JSON, non come query string.
            try:
                body = json.loads(request.httprequest.get_data() or '{}')
            except Exception:
                body = {}
            if not isinstance(body, dict):
                body = {}
            order = body.get('orderId')
            if not order and isinstance(body.get('order'), dict):
                order = body['order'].get('orderId')
            if order:
                kw['ref'] = order
        try:
            tx = request.env['payment.transaction'].sudo()._get_tx_from_notification_data(
                'nexi', kw
            )
        except Exception:
            return request.not_found()
        # Solo chi ha il codice della transazione va avanti. Senza codice
        # giusto la risposta e' uguale a "non trovato", senza chiamare Nexi.
        if not tx.nexi_access_token or _nexi_tok() != tx.nexi_access_token:
            return request.not_found()
        # A Nexi passo solo ref/cancel/notify: l'ordine da controllare lo
        # decide la transazione, mai chi chiama. Il suffisso -m degli APM
        # e' l'unica eccezione (sempre validata nel modello).
        ref_in = str(kw.get('ref') or '')
        notify_data = {'cancel': kw.get('cancel'), 'notify': kw.get('notify')}
        if ref_in == tx.reference + '-m':
            notify_data['nexi_order_ref'] = ref_in
        # Serializzo sulla riga PRIMA di toccare lo stato: in Odoo 18
        # _update_state() riscrive is_post_processed=False a ogni transizione,
        # quindi un lock preso dopo lascerebbe passare una seconda
        # post-elaborazione (notifica + ritorno del browser insieme).
        #
        # NOWAIT: la verifica può durare decine di secondi (timeout di rete),
        # quindi chi arriva secondo non si mette in coda — esce e lascia finire
        # il primo flusso. Il cliente vede la pagina di stato, che rilegge la
        # transazione appena l'altro flusso l'ha chiusa.
        if not self._nexi_lock_tx(tx):
            _logger.info('Nexi %s: chiusura già in corso in un altro flusso.', tx.reference)
            return self._nexi_finish(tx, is_notify)
        tx.invalidate_recordset()
        if tx.is_post_processed and tx.state in ('done', 'cancel', 'error'):
            # Già chiusa e già post-elaborata da un altro flusso: non rifaccio
            # la verifica né la conferma.
            _logger.info('Nexi %s già chiusa e post-elaborata: salto.', tx.reference)
        else:
            tx._process_notification_data(notify_data)
            tx.invalidate_recordset()
            # Senza questo Odoo non conferma l'ordine ne' crea la fattura.
            if not tx.is_post_processed:
                tx._post_process()
        # Rotta finale: quella decisa dal flusso (cassa, portale, fattura) va
        # lasciata com'è. Si tocca solo se manca o se punta alla pagina di
        # stato, che altrimenti ricarica in loop.
        tx.landing_route = _nexi_landing_route(tx.landing_route, bool(tx.sale_order_ids))
        return self._nexi_finish(tx, is_notify)

    @http.route('/payment/nexi/build/<string:reference>', type='http', auth='public', website=True, methods=['GET'])
    def nexi_build(self, reference, **kw):
        tx = _nexi_tx_or_404(reference)
        if not tx or not tx.nexi_build_session:
            return request.not_found()
        host = 'https://xpaysandbox.nexigroup.com' if tx.provider_id.nexi_env == 'test' else 'https://xpay.nexigroup.com'
        tok = tx.nexi_access_token
        return request.render('payment_nexi.build_page', {
            'tx_reference': tx.reference,
            'nexi_sdk': host + '/monetaweb/resources/hfsdk.js',
            'build_session': tx.nexi_build_session,
            'build_token': tx.nexi_build_token or '',
            'build_state_url': f'/payment/nexi/build/{tx.reference}/fields?tok={tok}',
            'build_finalize_url': f'/payment/nexi/build/{tx.reference}/finalize?tok={tok}',
            'confirm_url': f'/payment/nexi/confirm?ref={tx.reference}&tok={tok}',
        })

    @http.route('/payment/nexi/build/prepare', type='json', auth='public', website=True, methods=['POST'], csrf=False)
    def nexi_build_prepare(self, method_code='card', tokenize=False, provider_id=None, **kw):
        """Crea transazione + ordine Build e ridà campi o URL APM."""
        if not _nexi_rl_allow(_nexi_rl_key('prepare'), 20):
            return {'error': 'Troppe richieste. Riprova tra un minuto.'}
        order = request.website.sale_get_order()
        # Le stesse porte che chiude la cassa Odoo: senza queste si poteva
        # creare un ordine Nexi per un carrello non pagabile.
        cart_error = self._nexi_order_ready(order)
        if cart_error:
            return {'error': cart_error}
        if (order.currency_id.name or '') != 'EUR':
            return {'error': 'Nexi attiva solo in EUR.'}
        provider = self._nexi_provider(order, mode='build', provider_id=provider_id)
        if not provider:
            return {'error': 'Build non attiva.'}
        if not self._nexi_lock_order(order):
            # Un altro pagamento sta già girando su questo ordine.
            return {'error': 'Pagamento già in corso. Riprova tra un istante.'}
        method = request.env['payment.method'].sudo().search(
            [('code', '=', method_code), ('is_primary', '=', True)], limit=1,
        )
        if not method:
            return {'error': 'Metodo carta mancante.'}
        # Il metodo deve essere di QUESTO terminale e non rifiutato da lui:
        # la riga in cassa è condivisa, il terminale no.
        if method not in provider.payment_method_ids or method._nexi_is_disabled(provider.id):
            return {'error': 'Metodo non disponibile su questo terminale.'}
        Tx = request.env['payment.transaction'].sudo()
        # Host con cui il browser naviga (tunnel compreso): ultima spiaggia
        # per merchantUrl, solo se https pubblico. Vale la base configurata.
        origin = (request.httprequest.host_url or '').rstrip('/')
        if request.httprequest.headers.get('X-Forwarded-Proto') == 'https' and origin.startswith('http://'):
            origin = 'https://' + origin[len('http://'):]
        extra = {'base_url': origin} if origin else {}
        tx = Tx.search([
            ('sale_order_ids', 'in', order.id),
            ('provider_id', '=', provider.id),
            ('payment_method_id', '=', method.id),
            ('state', '=', 'draft'),
        ], order='id desc', limit=1)
        if tx:
            # Il carrello puo' cambiare: la bozza deve seguire l'importo vero,
            # altrimenti si paga (o si rifiuta) la cifra vecchia.
            refresh = {}
            if tx.amount != order.amount_total:
                refresh['amount'] = order.amount_total
            if tx.currency_id != order.currency_id:
                refresh['currency_id'] = order.currency_id.id
            if tx.partner_id != order.partner_invoice_id:
                refresh['partner_id'] = order.partner_invoice_id.id
            if refresh:
                tx.write(refresh)
        if not tx:
            base = order.name or 'S'
            for _i in range(5):
                n = Tx.search_count([('sale_order_ids', 'in', order.id)])
                ref = base if not n else '%s-%s' % (base, n)
                try:
                    with request.env.cr.savepoint():
                        tx = Tx.create({
                            'provider_id': provider.id,
                            'payment_method_id': method.id,
                            'amount': order.amount_total,
                            'currency_id': order.currency_id.id,
                            'partner_id': order.partner_invoice_id.id,
                            'reference': ref,
                            'operation': 'online_redirect',
                            # Rotta finale: porta alla conferma d'ordine. Se resta vuota o
                            # punta a /payment/status, la pagina di stato ricarica in loop.
                            'landing_route': '/shop/payment/validate',
                            'sale_order_ids': [(6, 0, [order.id])],
                        })
                    break
                except Exception as e:
                    # Riferimento gia' preso da una richiesta parallela: riprovo.
                    # Loggo sempre: un errore nascosto qui spegne tutta la cassa.
                    _logger.warning(
                        'prepare: tx %s non creata (tentativo %s/5): %s',
                        ref, _i + 1, str(e)[:200])
                    tx = None
                    time.sleep(0.05 * (_i + 1))
                    continue
            if not tx:
                tx = Tx.search([
                    ('sale_order_ids', 'in', order.id),
                    ('provider_id', '=', provider.id),
                    ('payment_method_id', '=', method.id),
                    ('state', '=', 'draft'),
                ], order='id desc', limit=1)
            if not tx:
                _logger.warning('prepare: impossibile creare la transazione per %s', order.name)
                return {'error': 'Pagamento non disponibile. Riprova.'}
        try:
            if method_code and method_code != 'card':
                # APM: Nexi ridà subito l'URL della sua pagina di pagamento.
                res_apm = tx._get_specific_rendering_values(dict({
                    'amount': tx.amount,
                    'currency_id': tx.currency_id.id,
                    'partner_id': tx.partner_id.id,
                    'reference': tx.reference,
                    'should_tokenize': False,
                    'force_build': True,
                }, **extra))
                url = res_apm.get('build_url') or False
                if url and not _nexi_check_url(url):
                    _logger.warning('prepare: URL APM scartato per %s', tx.reference)
                    return {'error': 'Pagamento non disponibile. Riprova.'}
                return {
                    'tx': tx.reference,
                    'url': url,
                    'fields': [],
                    'buttons': [],
                }
            res_cards = tx._get_specific_rendering_values(dict({
                'amount': tx.amount,
                'currency_id': tx.currency_id.id,
                'partner_id': tx.partner_id.id,
                'reference': tx.reference,
                'should_tokenize': False,
                'force_build': True,
            }, **extra))
            res_buttons = tx._get_specific_rendering_values(dict({
                'amount': tx.amount,
                'currency_id': tx.currency_id.id,
                'partner_id': tx.partner_id.id,
                'reference': tx.reference,
                'should_tokenize': False,
                'force_build': True,
                'no_service': True,
                'order_suffix': '-m',
            }, **extra))
        except Exception as e:
            msg = str(e)
            # Paga in 3 ha un minimo di 30 € lato Nexi: dico il motivo vero
            # e faccio sparire la riga, invece di un errore generico.
            if method_code == 'paypal_bnpl' and ('PS0064' in msg or 'GW0043' in msg):
                return {
                    'error': 'PayPal Paga in 3 richiede almeno 30,00 € di ordine.',
                    'hide': 'paypal_bnpl',
                }
            return {'error': _nexi_public_error(e)}
        host = 'https://xpaysandbox.nexigroup.com' if provider.nexi_env == 'test' else 'https://xpay.nexigroup.com'
        tok = tx.nexi_access_token
        return {
            'tx': tx.reference,
            'fields': res_cards.get('build_fields') or [],
            'buttons': res_buttons.get('build_fields') or [],
            'sdk': host + '/monetaweb/resources/hfsdk.js',
            'finalize_url': f'/payment/nexi/build/{tx.reference}/finalize?tok={tok}',
            'confirm_url': f'/payment/nexi/confirm?ref={tx.reference}&tok={tok}',
        }

    @http.route('/payment/nexi/build/jserror', type='json', auth='public', website=True, methods=['POST'], csrf=False)
    def nexi_build_jserror(self, msg=None, **kw):
        if not _nexi_rl_allow(_nexi_rl_key('jserror'), 10):
            return True
        # Una riga sola, niente caratteri di controllo: i log restano puliti.
        clean = str(msg or '').replace('\r', ' ').replace('\n', ' ')[:300]
        _logger.warning('browser: %s', clean)
        return True

    @http.route('/payment/nexi/build/<string:reference>/fields', type='http', auth='public', website=True, methods=['GET'])
    def nexi_build_fields(self, reference, **kw):
        """Solo i campi da mostrare, mai la risposta grezza di Nexi."""
        tx = _nexi_tx_or_404(reference)
        if not tx or not tx.nexi_build_session:
            return request.not_found()
        # Anche questa rotta spende una chiamata al terminale: la conto come le
        # altre, altrimenti un cliente può far girare la chiave a vuoto.
        if not _nexi_rl_allow(_nexi_rl_key('fields'), 30):
            return request.make_response(
                json.dumps({'fields': []}), [('Content-Type', 'application/json')], status=429
            )
        provider = tx.provider_id
        base = provider._nexi_build_url().rsplit('/orders/build', 1)[0]
        try:
            data = provider._nexi_make_request(
                'GET', base + '/build/state',
                params={'sessionId': tx.nexi_build_session},
            )
        except Exception:
            return request.make_response(json.dumps({'fields': []}), [('Content-Type', 'application/json')])
        # Whitelist: solo id/classe/tipo/src degli iframe, niente operation, e
        # solo src su dominio Nexi (sono gli iframe in cui si digita la carta).
        out = []
        fs = (data or {}).get('fieldSet') or {}
        for f in _nexi_safe_fields(fs.get('fields')):
            if not f.get('src'):
                continue
            out.append({
                'id': f.get('id'),
                'class': f.get('class'),
                'type': f.get('type'),
                'src': f.get('src'),
            })
        return request.make_response(
            json.dumps({'state': (data or {}).get('state'), 'fields': out}),
            [('Content-Type', 'application/json')],
        )

    @http.route('/payment/nexi/build/<string:reference>/finalize', type='json', auth='public', methods=['POST'], csrf=False)
    def nexi_build_finalize(self, reference, **kw):
        """Chiama finalize su Nexi (la chiave resta sul server). Torna url o fine."""
        if not _nexi_rl_allow(_nexi_rl_key('finalize'), 30):
            return {'error': 'Troppe richieste. Riprova tra un minuto.'}
        tx = _nexi_tx_or_404(reference)
        if not tx or not tx.nexi_build_session:
            return {'error': 'not found'}
        provider = tx.provider_id
        base = provider._nexi_build_url().rsplit('/orders/build', 1)[0]
        try:
            data = provider._nexi_make_request(
                'POST', base + '/build/finalize_payment',
                payload={'sessionId': tx.nexi_build_session},
            )
        except Exception as e:
            msg = str(e)[:300]
            if 'GW0009' in msg or 'not allowed in the current state' in msg:
                # Sessione consumata o in stato sbagliato: la prossima volta
                # se ne crea una nuova invece di restare bloccati.
                tx.nexi_build_session = False
                tx.nexi_build_token = False
            return {'error': _nexi_public_error(e)}
        url = (data or {}).get('url')
        if url and not _nexi_check_url(url):
            _logger.warning('finalize: URL scartato per %s', tx.reference)
            return {'error': 'Pagamento non disponibile. Riprova.'}
        return {
            'state': (data or {}).get('state'),
            'url': url,
        }

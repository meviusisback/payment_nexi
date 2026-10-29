# -*- coding: utf-8 -*-
"""Ritorno da Nexi. URL breve, una sola cosa: trova la transazione e falla verificare."""
import json
import logging
import time

from odoo import http
from odoo.http import request

_logger = logging.getLogger(__name__)

# Limiti semplici per IP: niente abusi di conferma, ordini o log.
_NEXI_RL = {}


def _nexi_rl_allow(key, limit, window=60):
    now = time.time()
    lst = [t for t in (_NEXI_RL.get(key) or []) if now - t < window]
    if len(lst) >= limit:
        _NEXI_RL[key] = lst
        return False
    lst.append(now)
    _NEXI_RL[key] = lst
    return True


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


def _nexi_public_error(e):
    """Al browser solo messaggi sicuri, il resto resta nei log."""
    msg = str(e) or ''
    safe = (
        'PayPal Paga in 3 richiede almeno 30,00',
        'Nexi attiva solo in EUR',
        'Manca la X-Api-Key',
        'Build vuole URL pubblico',
        'Metodo Nexi non valido',
        'Ordine non valido',
        'Build non attiva',
        'Metodo carta mancante',
        'Troppe richieste',
    )
    if msg.startswith(safe):
        return msg[:200]
    _logger.warning('Nexi errore interno: %s', msg[:300])
    return 'Pagamento non disponibile. Riprova.'


def _nexi_check_url(url):
    """URL da Nexi: solo https su domini Nexi, niente redirect a giro."""
    if not url:
        return False
    try:
        from urllib.parse import urlparse
        from . import payment_provider as _pp
    except Exception:
        from odoo.addons.payment_nexi.models import payment_provider as _pp
    try:
        parts = urlparse(url)
    except Exception:
        return False
    if (parts.scheme or '').lower() != 'https':
        return False
    host = (parts.hostname or '').lower()
    return any(host == h or host.endswith('.' + h) for h in _pp.NEXI_HOSTS)


class NexiController(http.Controller):
    @http.route('/payment/nexi/confirm', type='http', auth='public', csrf=False, methods=['GET', 'POST'])
    def nexi_confirm(self, **kw):
        if not _nexi_rl_allow(_nexi_rl_key('confirm'), 30):
            return request.make_response('Troppe richieste. Riprova tra un minuto.', status=429)
        # kw = parametri che Nexi aggiunge al ritorno (ref, cancel, ...).
        is_notify = request.httprequest.method == 'POST'
        if is_notify and not kw.get('ref'):
            # La notifica arriva come JSON, non come query string.
            try:
                body = json.loads(request.httprequest.get_data() or '{}')
            except Exception:
                body = {}
            order = body.get('orderId') or (body.get('order') or {}).get('orderId')
            if order:
                kw['ref'] = order
        tx = request.env['payment.transaction'].sudo()._get_tx_from_notification_data(
            'nexi', kw
        )
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
        # Prima verifico su Nexi (rete), poi blocco la riga solo per la
        # post-elaborazione: il lock non resta aperto durante le chiamate.
        tx._process_notification_data(notify_data)
        # Notifica Nexi e ritorno del browser arrivano insieme: serializzo sulla
        # riga, altrimenti la post-elaborazione gira due volte (errore sui follower).
        request.env.cr.execute(
            'SELECT id FROM payment_transaction WHERE id = %s FOR UPDATE', (tx.id,)
        )
        tx.invalidate_recordset()
        # Senza questo Odoo non conferma l'ordine ne' crea la fattura.
        # Solo una volta: la notifica Nexi e il ritorno del browser arrivano insieme.
        if not tx.is_post_processed:
            tx._post_process()
        # Rotta finale: deve portare fuori dalla pagina di stato, altrimenti
        # il polling la ricarica all'infinito (flickering).
        if tx.landing_route != '/shop/payment/validate':
            tx.landing_route = '/shop/payment/validate'
        if is_notify:
            return 'OK'
        # Dillo alla pagina di stato: senza questa chiave in sessione
        # Odoo non trova il pagamento e mostra "in attesa".
        from odoo.addons.payment.controllers.post_processing import PaymentPostProcessing
        PaymentPostProcessing.monitor_transaction(tx)
        return request.redirect('/payment/status')

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
    def nexi_build_prepare(self, method_code='card', tokenize=False, **kw):
        """Crea transazione + ordine Build e ridà campi o URL APM."""
        if not _nexi_rl_allow(_nexi_rl_key('prepare'), 20):
            return {'error': 'Troppe richieste. Riprova tra un minuto.'}
        order = request.website.sale_get_order()
        if not order or not order.amount_total:
            return {'error': 'Ordine non valido.'}
        provider = request.env['payment.provider'].sudo().search(
            [('code', '=', 'nexi'), ('state', '!=', 'disabled'), ('is_published', '=', True)],
            limit=1,
        )
        if not provider or provider.nexi_mode != 'build':
            return {'error': 'Build non attiva.'}
        if (order.currency_id.name or '') != 'EUR':
            return {'error': 'Nexi attiva solo in EUR.'}
        method = request.env['payment.method'].sudo().search(
            [('code', '=', method_code), ('is_primary', '=', True)], limit=1,
        )
        if not method:
            return {'error': 'Metodo carta mancante.'}
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
                except Exception:
                    # Riferimento gia' preso da una richiesta parallela: riprovo.
                    tx = None
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
        provider = tx.provider_id
        base = provider._nexi_build_url().rsplit('/orders/build', 1)[0]
        try:
            data = provider._nexi_make_request(
                'GET', base + '/build/state',
                params={'sessionId': tx.nexi_build_session},
            )
        except Exception:
            return request.make_response(json.dumps({'fields': []}), [('Content-Type', 'application/json')])
        # Whitelist: solo id/classe/tipo/src degli iframe, niente operation.
        out = []
        fs = (data or {}).get('fieldSet') or {}
        for f in (fs.get('fields') or []):
            if not isinstance(f, dict) or not f.get('src'):
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

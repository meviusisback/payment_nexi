# -*- coding: utf-8 -*-
"""Ritorno da Nexi. URL breve, una sola cosa: trova la transazione e falla verificare."""
import json

from odoo import http
from odoo.http import request


class NexiController(http.Controller):
    @http.route('/payment/nexi/confirm', type='http', auth='public', csrf=False, methods=['GET', 'POST'])
    def nexi_confirm(self, **kw):
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
        tx._process_notification_data(kw)
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
        tx = request.env['payment.transaction'].sudo().search([
            ('reference', '=', reference), ('provider_code', '=', 'nexi'),
        ], limit=1)
        if not tx or not tx.nexi_build_session:
            return request.not_found()
        host = 'https://xpaysandbox.nexigroup.com' if tx.provider_id.nexi_env == 'test' else 'https://xpay.nexigroup.com'
        return request.render('payment_nexi.build_page', {
            'tx_reference': tx.reference,
            'nexi_sdk': host + '/monetaweb/resources/hfsdk.js',
            'build_session': tx.nexi_build_session,
            'build_token': tx.nexi_build_token or '',
            'build_state_url': f'/payment/nexi/build/{tx.reference}/fields',
            'build_finalize_url': f'/payment/nexi/build/{tx.reference}/finalize',
            'confirm_url': f'/payment/nexi/confirm?ref={tx.reference}',
        })

    @http.route('/payment/nexi/build/prepare', type='json', auth='public', website=True, methods=['POST'], csrf=False)
    def nexi_build_prepare(self, method_code='card', tokenize=False, **kw):
        """Crea transazione + ordine Build e ridà campi o URL APM."""
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
        # Host vero con cui il browser sta navigando (tunnel compreso): Nexi
        # confronta il dominio degli iframe con merchantUrl.
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
        if not tx:
            base = order.name or 'S'
            n = Tx.search_count([('sale_order_ids', 'in', order.id)])
            ref = base if not n else '%s-%s' % (base, n)
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
                return {
                    'tx': tx.reference,
                    'url': res_apm.get('build_url') or False,
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
            _logger = __import__('logging').getLogger(__name__ + '.prepare')
            _logger.warning('prepare %s card_fields=%s', tx.reference, len(res_cards.get('build_fields') or []))
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
            import logging as _logging
            _logging.getLogger(__name__ + '.prepare').warning('prepare KO: %s', str(e)[:200])
            return {'error': str(e)[:200]}
        host = 'https://xpaysandbox.nexigroup.com' if provider.nexi_env == 'test' else 'https://xpay.nexigroup.com'
        return {
            'tx': tx.reference,
            'fields': res_cards.get('build_fields') or [],
            'buttons': res_buttons.get('build_fields') or [],
            'sdk': host + '/monetaweb/resources/hfsdk.js',
            'finalize_url': f'/payment/nexi/build/{tx.reference}/finalize',
            'confirm_url': f'/payment/nexi/confirm?ref={tx.reference}',
        }

    @http.route('/payment/nexi/build/jserror', type='json', auth='public', website=True, methods=['POST'], csrf=False)
    def nexi_build_jserror(self, msg=None, **kw):
        import logging as _logging
        _logging.getLogger(__name__ + '.js').warning('browser: %s', str(msg)[:300])
        return True

    @http.route('/payment/nexi/build/<string:reference>/fields', type='http', auth='public', website=True, methods=['GET'])
    def nexi_build_fields(self, reference, **kw):
        """Ridate i bottoni metodi: li chiede a Nexi con la sessione salvata."""
        tx = request.env['payment.transaction'].sudo().search([
            ('reference', '=', reference), ('provider_code', '=', 'nexi'),
        ], limit=1)
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
        return request.make_response(json.dumps(data or {}), [('Content-Type', 'application/json')])

    @http.route('/payment/nexi/build/<string:reference>/finalize', type='json', auth='public', methods=['POST'], csrf=False)
    def nexi_build_finalize(self, reference, **kw):
        """Chiama finalize su Nexi (la chiave resta sul server). Torna url o fine."""
        tx = request.env['payment.transaction'].sudo().search([
            ('reference', '=', reference), ('provider_code', '=', 'nexi'),
        ], limit=1)
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
            return {'error': 'Nexi: ' + msg}
        return {
            'state': (data or {}).get('state'),
            'url': (data or {}).get('url'),
            'operation': (data or {}).get('operation'),
        }

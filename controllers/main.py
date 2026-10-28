# -*- coding: utf-8 -*-
"""Ritorno da Nexi. URL breve, una sola cosa: trova la transazione e falla verificare."""
import json

from odoo import http
from odoo.http import request


class NexiController(http.Controller):
    @http.route('/payment/nexi/confirm', type='http', auth='public', csrf=False, methods=['GET'])
    def nexi_confirm(self, **kw):
        # kw = parametri che Nexi aggiunge al ritorno (ref, cancel, ...).
        tx = request.env['payment.transaction'].sudo()._get_tx_from_notification_data(
            'nexi', kw
        )
        tx._process_notification_data(kw)
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
                'sale_order_ids': [(6, 0, [order.id])],
            })
        try:
            res = tx._get_specific_rendering_values({
                'amount': tx.amount,
                'currency_id': tx.currency_id.id,
                'partner_id': tx.partner_id.id,
                'reference': tx.reference,
                'should_tokenize': bool(tokenize),
                'force_build': True,
            })
        except Exception as e:
            return {'error': str(e)[:200]}
        host = 'https://xpaysandbox.nexigroup.com' if provider.nexi_env == 'test' else 'https://xpay.nexigroup.com'
        return {
            'tx': tx.reference,
            'fields': res.get('build_fields') or [],
            'url': res.get('build_url') or False,
            'sdk': host + '/monetaweb/resources/hfsdk.js',
            'finalize_url': f'/payment/nexi/build/{tx.reference}/finalize',
            'confirm_url': f'/payment/nexi/confirm?ref={tx.reference}',
        }

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
            return {'error': str(e)[:200]}
        return {
            'state': (data or {}).get('state'),
            'url': (data or {}).get('url'),
            'operation': (data or {}).get('operation'),
        }

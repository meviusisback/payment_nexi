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

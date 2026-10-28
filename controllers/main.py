# -*- coding: utf-8 -*-
"""Ritorno da Nexi. URL breve, una sola cosa: trova la transazione e falla verificare."""
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

    @http.route('/payment/nexi/build/<string:reference>', type='http', auth='public', methods=['GET'])
    def nexi_build(self, reference, **kw):
        tx = request.env['payment.transaction'].sudo().search([
            ('reference', '=', reference), ('provider_code', '=', 'nexi'),
        ], limit=1)
        if not tx:
            return request.not_found()
        return request.render('payment_nexi.build_page', {'tx_reference': tx.reference})

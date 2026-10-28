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

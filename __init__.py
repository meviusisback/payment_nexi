# -*- coding: utf-8 -*-
from . import models
from . import controllers
from odoo import api, SUPERUSER_ID


def post_init_hook(env):
    # Crea il provider Nexi se manca. Semplice: un record solo.
    # Collega tutti i metodi del modulo, così si vedono subito.
    Provider = env['payment.provider'].sudo()
    Method = env['payment.method'].sudo()
    methods = Method.search([('code', 'in', [
        'card', 'googlepay', 'applepay', 'bancomatpay', 'mybank', 'paypal', 'paypal_bnpl',
    ])])
    vals = {'payment_method_ids': [(6, 0, methods.ids)]} if methods else {}
    existing = Provider.search([('code', '=', 'nexi')])
    if not existing:
        vals.update({
            'name': 'Nexi XPay Web',
            'code': 'nexi',
            'state': 'disabled',
            'is_published': False,
        })
        Provider.create(vals)
    else:
        existing.write(vals)

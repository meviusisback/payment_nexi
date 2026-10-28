# -*- coding: utf-8 -*-
from . import models
from . import controllers
from odoo import api, SUPERUSER_ID


def post_init_hook(env):
    # Crea il provider Nexi se manca. Semplice: un record solo.
    # Collega tutti i metodi del modulo, così si vedono subito.
    # Mette i loghi del repo sui metodi, anche su installazioni già fatte.
    import base64
    import os
    Provider = env['payment.provider'].sudo()
    Method = env['payment.method'].sudo()
    codes = [
        'card', 'googlepay', 'applepay', 'bancomatpay', 'mybank', 'paypal',
        'visa', 'mastercard', 'amex',
    ]
    methods = Method.search([('code', 'in', codes)])
    vals = {'payment_method_ids': [(6, 0, methods.ids)]} if methods else {}
    # PayPal 3 rate tolto: Nexi ignora PAYPAL_BNPL e mostra la pagina carte.
    bad = Method.search([('code', '=', 'paypal_bnpl')])
    if bad:
        try:
            bad.unlink()
        except Exception:
            bad.write({'provider_ids': [(6, 0, [])]})
    existing = Provider.search([('code', '=', 'nexi')])
    if not existing:
        vals.update({
            'name': 'Nexi XPay Web',
            'code': 'nexi',
            'state': 'disabled',
            'is_published': False,
            'allow_tokenization': True,
        })
        Provider.create(vals)
    else:
        vals['allow_tokenization'] = True
        existing.write(vals)
    base = os.path.join(os.path.dirname(__file__), 'static', 'src', 'img')
    for code in codes:
        path = os.path.join(base, code + '.png')
        if not os.path.isfile(path):
            continue
        method = Method.search([('code', '=', code)], limit=1)
        if not method:
            continue
        with open(path, 'rb') as f:
            method.write({'image': base64.b64encode(f.read())})

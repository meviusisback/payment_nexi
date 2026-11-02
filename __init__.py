# -*- coding: utf-8 -*-
from . import models
from . import controllers
from odoo import api, SUPERUSER_ID


def post_init_hook(env):
    # Crea il provider Nexi se manca. Non tocca mai le scelte dell'admin:
    # aggiunge solo i metodi mancanti e le immagini vuote.
    import base64
    import os
    Provider = env['payment.provider'].sudo()
    Method = env['payment.method'].sudo()
    codes = [
        'card', 'googlepay', 'applepay', 'bancomatpay', 'mybank', 'paypal', 'paypal_bnpl',
        'visa', 'mastercard', 'amex',
    ]
    # I metodi del core (card, paypal, mybank, visa, mastercard, amex) restano
    # spenti finché l'admin non abilita il provider: è Odoo ad accenderli
    # (provider._activate_default_pms). Solo nomi/icone/flag del core.
    methods = Method.search([('code', 'in', codes)])
    existing = Provider.search([('code', '=', 'nexi')])
    if not existing:
        existing = Provider.create({
            'name': 'Nexi XPay Web',
            'code': 'nexi',
            'state': 'disabled',
            'is_published': False,
            'allow_tokenization': False,
            'payment_method_ids': [(6, 0, methods.ids)] if methods else False,
        })
    else:
        have = set(existing.payment_method_ids.ids)
        missing = [m.id for m in methods if m.id not in have]
        if missing:
            existing.write({'payment_method_ids': [(4, i) for i in missing]})
    base = os.path.join(os.path.dirname(__file__), 'static', 'src', 'img')
    for code in ('googlepay', 'applepay', 'bancomatpay', 'paypal_bnpl'):
        path = os.path.join(base, code + '.png')
        if not os.path.isfile(path):
            continue
        method = Method.search([('code', '=', code)], limit=1)
        if method and not method.image:
            with open(path, 'rb') as f:
                method.write({'image': base64.b64encode(f.read())})

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
    # (provider._activate_default_pms), e solo se sono già collegati. Su un DB
    # pulito `Method.search` con active_test=True li salterebbe tutti e la
    # nuova installazione non offrirebbe le carte: qui i record inattivi
    # devono essere collegati, poi sarà Odoo ad attivarli.
    methods = Method.with_context(active_test=False).search([('code', 'in', codes)])
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
        # Per provider, non sull'unione: un terminale già completo non deve
        # nascondere i metodi mancanti di un altro (search multi-record).
        for provider in existing.with_context(active_test=False):
            have = set(provider.payment_method_ids.ids)
            missing = [m.id for m in methods if m.id not in have]
            if missing:
                provider.write({'payment_method_ids': [(4, i) for i in missing]})
    for provider in existing:
        if provider.state != 'disabled':
            # Provider già attivo: accende i brand e mette per primi i loro
            # loghi. Uno per uno: possono esserci più terminali Nexi.
            provider._activate_default_pms()
    base = os.path.join(os.path.dirname(__file__), 'static', 'src', 'img')
    # Anche i brand core (visa/mastercard/amex): il file è lo stesso di Odoo,
    # ma su DB vecchi l'immagine può essere vuota e senza di essa la riga
    # Carta perde le icone dei circuiti.
    for code in codes:
        path = os.path.join(base, code + '.png')
        if not os.path.isfile(path):
            continue
        method = Method.with_context(active_test=False).search(
            [('code', '=', code)], limit=1)
        if method and not method.image:
            with open(path, 'rb') as f:
                method.write({'image': base64.b64encode(f.read())})

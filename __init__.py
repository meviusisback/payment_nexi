# -*- coding: utf-8 -*-
import base64
import os

from . import models
from . import controllers
from .models.payment_provider import NEXI_METHOD_CODES


def post_init_hook(env):
    # Create the Nexi provider if it is missing. It never touches the admin
    # choices: it only adds the missing methods and the empty images.
    Provider = env['payment.provider'].sudo()
    Method = env['payment.method'].sudo()
    codes = NEXI_METHOD_CODES
    # The core methods (card, paypal, mybank, visa, mastercard, amex) stay off
    # until the admin enables the provider: Odoo is the one that turns them on
    # (provider._activate_default_pms), and only if they are already linked. On
    # a clean database `Method.search` with active_test=True would skip them all
    # and a new install would not offer the cards: here the inactive records
    # must be linked, then Odoo activates them.
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
        # Per provider, not on the union: a complete terminal must not hide the
        # methods missing on another one (multi-record search).
        for provider in existing.with_context(active_test=False):
            have = set(provider.payment_method_ids.ids)
            missing = [m.id for m in methods if m.id not in have]
            if missing:
                provider.write({'payment_method_ids': [(4, i) for i in missing]})
    for provider in existing:
        if provider.state != 'disabled':
            # Provider already active: turn the brands on and put their logos
            # first. One by one: there can be several Nexi terminals.
            provider._activate_default_pms()
    base = os.path.join(os.path.dirname(__file__), 'static', 'src', 'img')
    # The core brands too (visa/mastercard/amex): the file is the same as
    # Odoo, but on old databases the image can be empty and without it the
    # Card row loses the scheme icons.
    for code in codes:
        path = os.path.join(base, code + '.png')
        if not os.path.isfile(path):
            continue
        method = Method.with_context(active_test=False).search(
            [('code', '=', code)], limit=1)
        if method and not method.image:
            with open(path, 'rb') as f:
                method.write({'image': base64.b64encode(f.read())})

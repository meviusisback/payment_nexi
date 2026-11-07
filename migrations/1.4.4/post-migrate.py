# -*- coding: utf-8 -*-
"""#10 regressione: i brand Visa/Mastercard/Amex non sono più visibili.

Il fix #7 ha tolto i duplicati del modulo e lasciato i record del core; ma
`_get_default_payment_method_codes` non conteneva i brand, quindi
`_activate_default_pms` non li accendeva e la riga Carta restava senza icone.
Questa migrazione li riaggancia ai provider Nexi esistenti, li accende e
riempie l'immagine se manca. Idempotente: si può rieseguire a vuoto.
"""
import base64
import logging
import os

from odoo import SUPERUSER_ID, api

_logger = logging.getLogger(__name__)

_BRANDS = ('visa', 'mastercard', 'amex')


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    Method = env['payment.method'].with_context(active_test=False)
    providers = env['payment.provider'].sudo().search([('code', '=', 'nexi')])
    base = os.path.join(
        os.path.dirname(__file__), '..', '..', 'static', 'src', 'img')
    brands = Method.browse()
    for code in _BRANDS:
        method = Method.search(
            [('code', '=', code)], order='active desc, id', limit=1)
        if not method:
            continue
        brands |= method
        path = os.path.join(base, code + '.png')
        if not method.image and os.path.isfile(path):
            with open(path, 'rb') as f:
                method.write({'image': base64.b64encode(f.read())})
            _logger.info('Nexi migrazione: icona %s ripristinata.', code)
    if not brands:
        return
    # Odoo accende un brand solo se il metodo primario (Carta) è collegato a
    # un provider abilitato: riaggancio entrambi, non solo il brand.
    methods = brands | brands.mapped('primary_payment_method_id')
    for provider in providers:
        missing = methods - provider.payment_method_ids
        if missing:
            provider.write({
                'payment_method_ids': [(4, method.id) for method in missing],
            })
        if provider.state != 'disabled':
            provider._activate_default_pms()

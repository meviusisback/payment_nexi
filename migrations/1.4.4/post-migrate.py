# -*- coding: utf-8 -*-
"""#10 regression: the Visa/Mastercard/Amex brands are no longer visible.

The #7 fix removed the module duplicates and left the core records; but
`_get_default_payment_method_codes` did not contain the brands, so
`_activate_default_pms` did not turn them on and the Card row stayed without
icons. This migration re-links them to the existing Nexi providers, turns them
on and fills the image if it is missing. Idempotent: it can be re-run with no
effect.
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
            _logger.info('Nexi migration: icon %s restored.', code)
    if not brands:
        return
    # Odoo turns a brand on only if the primary method (Card) is linked to an
    # enabled provider: both are re-linked, not just the brand.
    methods = brands | brands.mapped('primary_payment_method_id')
    for provider in providers:
        missing = methods - provider.payment_method_ids
        if missing:
            provider.write({
                'payment_method_ids': [(4, method.id) for method in missing],
            })
        if provider.state != 'disabled':
            provider._activate_default_pms()

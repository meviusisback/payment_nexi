# -*- coding: utf-8 -*-
"""1.4.11: bound the existing Nexi providers to EUR.

`available_currency_ids` is stored and recomputed only when `code` changes, so
a provider created before the EUR-only rule still listed every currency. The
checkout then offered Nexi for a USD cart and the redirect failed with
"Nexi supports EUR only.". Idempotent: it can be re-run with no effect.
"""
import logging

from odoo import SUPERUSER_ID, api

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    eur = env['res.currency'].with_context(active_test=False).search(
        [('name', '=', 'EUR')], limit=1)
    if not eur:
        _logger.warning(
            'Nexi migration: EUR currency not found, Nexi currencies left untouched.')
        return
    providers = env['payment.provider'].sudo().search([('code', '=', 'nexi')])
    for provider in providers:
        if set(provider.available_currency_ids.ids) == {eur.id}:
            continue
        provider.write({'available_currency_ids': [(6, 0, [eur.id])]})
        _logger.info(
            'Nexi migration: currencies of provider %s set to EUR.', provider.id)

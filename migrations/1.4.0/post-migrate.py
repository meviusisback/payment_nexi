# -*- coding: utf-8 -*-
"""#7 point 3: the card/paypal/mybank/visa/mastercard/amex methods of the module
were duplicates of the core ones. Re-link everything to the core records and
delete the duplicates. Idempotent: it can be re-run with no effect."""
import logging

from odoo import SUPERUSER_ID, api

_logger = logging.getLogger(__name__)

# method code -> xmlid of our duplicate (to remove).
_DUPLICATES = {
    'card': 'payment_nexi.payment_method_nexi_card',
    'paypal': 'payment_nexi.payment_method_nexi_paypal',
    'mybank': 'payment_nexi.payment_method_nexi_mybank',
    'visa': 'payment_nexi.payment_method_nexi_visa',
    'mastercard': 'payment_nexi.payment_method_nexi_mastercard',
    'amex': 'payment_nexi.payment_method_nexi_amex',
    # Codes removed from the module but left on old databases: orphans to clean up.
    'satispay': 'payment_nexi.payment_method_nexi_satispay',
    'klarna': 'payment_nexi.payment_method_nexi_klarna',
}


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    Method = env['payment.method'].with_context(active_test=False)
    IrModelData = env['ir.model.data'].sudo()
    # The inherited core methods start off: they must be turned on for the Nexi
    # providers already enabled (the installation hook does not run on upgrades).
    nexi_on = env['payment.provider'].sudo().search([
        ('code', '=', 'nexi'), ('state', '!=', 'disabled'),
    ])
    for code, dup_xmlid in _DUPLICATES.items():
        dup = env.ref(dup_xmlid, raise_if_not_found=False)
        if not dup:
            continue  # new installation, or already migrated: nothing to do
        core = Method.search(
            [('code', '=', code), ('id', '!=', dup.id)], limit=1,
        )
        IrModelData.search([
            ('module', '=', 'payment_nexi'), ('name', '=', dup_xmlid.split('.', 1)[1]),
        ]).unlink()
        if not core:
            # No core record (e.g. satispay removed from the module): archive the
            # orphan instead of deleting it, so the history stays.
            dup.write({'active': False})
            _logger.info('Nexi migration: method %s archived (orphan).', code)
            continue
        was_active = dup.active
        _move(cr, env, dup, core)
        dup.unlink()
        _logger.info('Nexi migration: method %s re-linked to core.', code)
        if was_active and nexi_on and code in nexi_on[:1]._get_default_payment_method_codes():
            core.write({'active': True})

def _move(cr, env, dup, core):
    """Move every relation from the duplicate to the core method."""
    # Transactions and tokens point to the record: rewriting the id is enough.
    env['payment.transaction'].sudo().search(
        [('payment_method_id', '=', dup.id)]
    ).write({'payment_method_id': core.id})
    env['payment.token'].sudo().search(
        [('payment_method_id', '=', dup.id)]
    ).write({'payment_method_id': core.id})
    # Nexi method check: unique(provider, method), so the rows that would clash
    # are removed first, then the rest is moved.
    Avail = env['payment.nexi.method.availability'].sudo()
    clash = Avail.search([('method_id', '=', core.id)]).mapped('provider_id')
    if clash:
        Avail.search([
            ('method_id', '=', dup.id), ('provider_id', 'in', clash.ids),
        ]).unlink()
    Avail.search([('method_id', '=', dup.id)]).write({'method_id': core.id})
    # Linked providers: INSERT ... ON CONFLICT because the row with the core may
    # already exist (a provider with both methods).
    cr.execute(
        """
        INSERT INTO payment_method_payment_provider_rel
            (payment_method_id, payment_provider_id)
        SELECT %s, payment_provider_id
          FROM payment_method_payment_provider_rel
         WHERE payment_method_id = %s
        ON CONFLICT DO NOTHING
        """,
        (core.id, dup.id),
    )
    cr.execute(
        'DELETE FROM payment_method_payment_provider_rel WHERE payment_method_id = %s',
        (dup.id,),
    )

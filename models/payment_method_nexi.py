# -*- coding: utf-8 -*-
"""Nexi methods: hide at checkout the ones this terminal does not accept."""
from odoo import fields, models


class PaymentMethodNexi(models.Model):
    _inherit = 'payment.method'

    # Switched off by hand: it applies to every Nexi terminal of the shop.
    # The outcome of the automatic check is per single terminal instead
    # (model payment.nexi.method.availability).
    nexi_disabled = fields.Boolean(
        string='Hidden on every Nexi terminal',
        default=False,
        help='When checked, the method is not offered at checkout on any Nexi '
             'provider. To disable it on a single terminal use the "Check '
             'methods on Nexi" button on the provider form.',
    )

    def _nexi_is_disabled(self, provider_id):
        """Has this terminal refused the method?"""
        return bool(self.env['payment.nexi.method.availability'].sudo().search_count([
            ('provider_id', '=', provider_id),
            ('method_id', 'in', self.ids),
            ('disabled', '=', True),
        ]))

    def _get_compatible_payment_methods(
        self, provider_ids, partner_id, currency_id=None, force_tokenization=False,
        is_express_checkout=False, report=None, **kwargs
    ):
        methods = super()._get_compatible_payment_methods(
            provider_ids, partner_id, currency_id=currency_id,
            force_tokenization=force_tokenization,
            is_express_checkout=is_express_checkout, report=report, **kwargs
        )
        # Providers compatible with THIS checkout: only these count. A method
        # offered by a distant provider must not keep it alive (nor the other
        # way round: another compatible provider keeps it visible).
        compatible = self.env['payment.provider'].browse(provider_ids)
        nexi_ids = set(compatible.filtered(lambda p: p.code == 'nexi').ids)
        if not nexi_ids or not methods:
            return methods
        # Who refused the method, terminal by terminal.
        refused = {}
        for rec in self.env['payment.nexi.method.availability'].sudo().search([
            ('provider_id', 'in', list(nexi_ids)),
            ('method_id', 'in', methods.ids),
            ('disabled', '=', True),
        ]):
            refused.setdefault(rec.method_id.id, set()).add(rec.provider_id.id)

        def _row_provider_id(method):
            """The provider Odoo will use in the row: the first compatible one.

            In `payment.method_form` the row takes
            `pm_sudo.provider_ids.filtered(lambda p: p in providers_sudo)[:1]`,
            that is the first compatible provider linked to the method. The
            `provider_ids` recordset is ordered by `payment.provider._order`
            (module_state, state desc, sequence, name), not by id: THIS order
            must be replicated, not the lowest id.
            """
            row = method.provider_ids.filtered(lambda p: p in compatible)[:1]
            return row.id if row else None

        def _keep(method):
            row_provider = _row_provider_id(method)
            if row_provider is None:
                return True
            if row_provider not in nexi_ids:
                # The terminal of the row is not Nexi: the row is not ours.
                return True
            if method.nexi_disabled:
                return False
            # The method is shown only if the terminal the checkout will really
            # use accepts it: otherwise the customer picks a row that fails.
            return row_provider not in refused.get(method.id, ())

        return methods.filtered(_keep)

# -*- coding: utf-8 -*-
"""Metodi Nexi: nascondi in cassa quelli non attivi sul terminale."""
from odoo import _, fields, models


class PaymentMethodNexi(models.Model):
    _inherit = 'payment.method'

    # Spento dal bottone "Verifica metodi" se Nexi dice "not enabled".
    # Lo riaccendi a mano da qui o riverifichi dopo averlo attivato su Nexi.
    nexi_disabled = fields.Boolean(
        string='Nascosto (Nexi non attivo)',
        default=False,
        help='Se spuntato, il metodo non esce in cassa finché Nexi non lo abilita sul terminale.',
    )

    def _get_compatible_payment_methods(
        self, provider_ids, partner_id, currency_id=None, force_tokenization=False,
        is_express_checkout=False, report=None, **kwargs
    ):
        methods = super()._get_compatible_payment_methods(
            provider_ids, partner_id, currency_id=currency_id,
            force_tokenization=force_tokenization,
            is_express_checkout=is_express_checkout, report=report, **kwargs
        )
        nexi_ids = self.env['payment.provider'].browse(provider_ids).filtered(
            lambda p: p.code == 'nexi'
        ).ids
        if nexi_ids:
            methods = methods.filtered(
                lambda m: not (
                    m.nexi_disabled and any(p.id in nexi_ids for p in m.provider_ids)
                )
            )
        return methods

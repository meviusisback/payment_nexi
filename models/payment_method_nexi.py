# -*- coding: utf-8 -*-
"""Metodi Nexi: nascondi in cassa quelli non attivi sul terminale.

La disponibilita e per coppia (metodo, provider): due terminali che
condividono un metodo possono avere esiti diversi dalla verifica.
"""
from odoo import fields, models


class PaymentMethodNexiAvailability(models.Model):
    _name = 'payment_nexi.method_availability'
    _description = 'Disponibilita metodo Nexi per terminale'
    _rec_name = 'payment_method_id'

    payment_method_id = fields.Many2one(
        'payment.method', required=True, ondelete='cascade', index=True,
    )
    provider_id = fields.Many2one(
        'payment.provider', required=True, ondelete='cascade', index=True,
    )
    # Spento dal bottone "Verifica metodi" se Nexi dice "not enabled".
    # Lo riaccendi riverificando dopo averlo attivato su Nexi.
    disabled = fields.Boolean(default=False)
    checked = fields.Datetime(readonly=True)

    _sql_constraints = [
        ('uniq_method_provider', 'unique(payment_method_id, provider_id)',
         'Disponibilita gia presente per questo terminale.'),
    ]


class PaymentMethodNexi(models.Model):
    _inherit = 'payment.method'

    nexi_availability_ids = fields.One2many(
        'payment_nexi.method_availability', 'payment_method_id',
        string='Disponibilita Nexi per terminale',
    )

    def _nexi_set_disabled(self, provider, disabled):
        """Segna il metodo acceso/spento per un solo terminale."""
        self.ensure_one()
        provider.ensure_one()
        from odoo import fields as odoo_fields
        avail = self.env['payment_nexi.method_availability'].search([
            ('payment_method_id', '=', self.id),
            ('provider_id', '=', provider.id),
        ], limit=1)
        vals = {'disabled': bool(disabled), 'checked': odoo_fields.Datetime.now()}
        if avail:
            avail.write(vals)
        else:
            avail = self.env['payment_nexi.method_availability'].create(dict(
                vals, payment_method_id=self.id, provider_id=provider.id,
            ))
        return avail

    def _nexi_is_disabled(self, provider):
        """Il metodo e spento su questo terminale?"""
        self.ensure_one()
        provider.ensure_one()
        avail = self.env['payment_nexi.method_availability'].search([
            ('payment_method_id', '=', self.id),
            ('provider_id', '=', provider.id),
        ], limit=1)
        return bool(avail and avail.disabled)

    def _get_compatible_payment_methods(
        self, provider_ids, partner_id, currency_id=None, force_tokenization=False,
        is_express_checkout=False, report=None, **kwargs
    ):
        methods = super()._get_compatible_payment_methods(
            provider_ids, partner_id, currency_id=currency_id,
            force_tokenization=force_tokenization,
            is_express_checkout=is_express_checkout, report=report, **kwargs
        )
        nexi_ids = set(self.env['payment.provider'].browse(provider_ids).filtered(
            lambda p: p.code == 'nexi'
        ).ids)
        if not nexi_ids:
            return methods
        disabled_pairs = set(self.env['payment_nexi.method_availability'].search([
            ('payment_method_id', 'in', methods.ids),
            ('provider_id', 'in', list(nexi_ids)),
            ('disabled', '=', True),
        ]).mapped(lambda a: (a.payment_method_id.id, a.provider_id.id)))

        def _kept(m):
            linked = [p.id for p in m.provider_ids if p.id in nexi_ids]
            if not linked:
                return True
            # Tieni il metodo se almeno un provider Nexi del checkout lo offre.
            return any((m.id, pid) not in disabled_pairs for pid in linked)

        return methods.filtered(_kept)

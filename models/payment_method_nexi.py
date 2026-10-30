# -*- coding: utf-8 -*-
"""Metodi Nexi: nascondi in cassa quelli che questo terminale non accetta."""
from odoo import fields, models


class PaymentMethodNexi(models.Model):
    _inherit = 'payment.method'

    # Spento a mano: vale per tutti i terminali Nexi del negozio.
    # L'esito della verifica automatica invece è per singolo terminale
    # (modello payment.nexi.method.availability).
    nexi_disabled = fields.Boolean(
        string='Nascosto su ogni terminale Nexi',
        default=False,
        help='Se spuntato, il metodo non esce in cassa su nessun provider Nexi. '
             'Per spegnerlo su un solo terminale usa il pulsante "Verifica metodi" '
             'nella scheda del provider.',
    )

    def _nexi_is_disabled(self, provider_id):
        """Il metodo è stato rifiutato da questo terminale?"""
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
        nexi_ids = set(self.env['payment.provider'].browse(provider_ids).filtered(
            lambda p: p.code == 'nexi'
        ).ids)
        if not nexi_ids or not methods:
            return methods
        # Provider compatibili con QUESTA cassa: solo questi contano. Un metodo
        # offerto da un provider lontano non deve tenerlo in vita (né il
        # contrario: un altro provider compatibile lo tiene visibile).
        compatible_ids = set(provider_ids)
        # Chi ha rifiutato il metodo, terminale per terminale.
        refused = {}
        for rec in self.env['payment.nexi.method.availability'].sudo().search([
            ('provider_id', 'in', list(nexi_ids)),
            ('method_id', 'in', methods.ids),
            ('disabled', '=', True),
        ]):
            refused.setdefault(rec.method_id.id, set()).add(rec.provider_id.id)

        def _row_provider_id(method):
            """Il provider che Odoo userà nella riga: il primo compatibile.

            In `payment.method_form` la riga prende
            `pm_sudo.provider_ids.filtered(lambda p: p in providers_sudo)[:1]`,
            cioè il primo provider compatibile collegato al metodo (ordine per
            id). Devo decidere su quello, non su "qualcuno lo accetta".
            """
            candidates = [p.id for p in method.provider_ids if p.id in compatible_ids]
            return min(candidates) if candidates else None

        def _keep(method):
            row_provider = _row_provider_id(method)
            if row_provider is None:
                return True
            if row_provider not in nexi_ids:
                # Il terminale della riga non è Nexi: la riga non è nostra.
                return True
            if method.nexi_disabled:
                return False
            # Mostro il metodo solo se il terminale che la cassa userà davvero
            # lo accetta: altrimenti il cliente sceglie una riga che fallisce.
            return row_provider not in refused.get(method.id, ())

        return methods.filtered(_keep)

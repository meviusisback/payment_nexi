# -*- coding: utf-8 -*-
"""Disponibilità dei metodi Nexi, separata per terminale (provider).

Un negozio può avere più terminali Nexi (es. uno per azienda o per ambiente) e
i metodi attivi non sono gli stessi su tutti: la verifica va salvata per
terminale, non sul metodo condiviso.
"""
from odoo import fields, models


class PaymentNexiMethodAvailability(models.Model):
    _name = 'payment.nexi.method.availability'
    _description = 'Metodi Nexi per terminale'
    _rec_name = 'method_id'

    provider_id = fields.Many2one(
        'payment.provider', string='Terminale', required=True,
        ondelete='cascade', index=True,
    )
    method_id = fields.Many2one(
        'payment.method', string='Metodo', required=True,
        ondelete='cascade', index=True,
    )
    disabled = fields.Boolean(
        string='Rifiutato dal terminale',
        help='La verifica su Nexi ha risposto "servizio non attivo".',
    )
    checked_at = fields.Datetime(string='Verifica del', readonly=True)
    detail = fields.Char(string='Esito', readonly=True)

    _sql_constraints = [
        ('provider_method_uniq', 'unique(provider_id, method_id)',
         'Un metodo può comparire una volta sola per terminale.'),
    ]

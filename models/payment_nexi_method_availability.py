# -*- coding: utf-8 -*-
"""Nexi method availability, kept per terminal (provider).

A shop can have several Nexi terminals (e.g. one per company or per
environment) and the active methods are not the same on all of them: the check
must be stored per terminal, not on the shared method.
"""
from odoo import fields, models


class PaymentNexiMethodAvailability(models.Model):
    _name = 'payment.nexi.method.availability'
    _description = 'Nexi methods per terminal'
    _rec_name = 'method_id'

    provider_id = fields.Many2one(
        'payment.provider', string='Terminal', required=True,
        ondelete='cascade', index=True,
    )
    method_id = fields.Many2one(
        'payment.method', string='Method', required=True,
        ondelete='cascade', index=True,
    )
    disabled = fields.Boolean(
        string='Refused by terminal',
        help='The Nexi check answered "service not enabled".',
    )
    checked_at = fields.Datetime(string='Checked on', readonly=True)
    detail = fields.Char(string='Outcome', readonly=True)

    _sql_constraints = [
        ('provider_method_uniq', 'unique(provider_id, method_id)',
         'A method can appear only once per terminal.'),
    ]

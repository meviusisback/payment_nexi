# -*- coding: utf-8 -*-
from . import models
from . import controllers
from odoo import api, SUPERUSER_ID


def post_init_hook(env):
    # Crea il provider Nexi se manca. Semplice: un record solo.
    Provider = env['payment.provider'].sudo()
    if not Provider.search([('code', '=', 'nexi')]):
        Provider.create({
            'name': 'Nexi XPay Web',
            'code': 'nexi',
            'state': 'disabled',
            'is_published': False,
        })

# -*- coding: utf-8 -*-
{
    'name': 'Nexi XPay Web',
    'version': '1.0.0',
    'category': 'Accounting/Payment Providers',
    'summary': 'Paga con Nexi XPay Web (Hosted Page). Semplice e veloce.',
    'description': 'Provider Odoo per Nexi XPay Web. Redirect alla pagina Nexi, niente dati carta in Odoo.',
    'author': 'Alberto',
    'website': 'https://developer.nexi.it',
    'license': 'LGPL-3',
    'depends': ['payment'],
    'data': [
        'views/payment_nexi_templates.xml',
        'data/payment_method_data.xml',
    ],
    'post_init_hook': 'post_init_hook',
    'application': False,
    'installable': True,
    'auto_install': False,
}

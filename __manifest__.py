# -*- coding: utf-8 -*-
{
    'name': 'Nexi XPay Web',
    'version': '1.4.10',
    'category': 'Accounting/Payment Providers',
    'summary': 'Nexi XPay Web payments: Hosted Page or card fields at checkout.',
    'description': """
Nexi XPay Web for Odoo 18.
Hosted Page (redirect to Nexi) or Embedded Build v3 with card fields inside the checkout.
No card data stored in Odoo: only the masked PAN.
Card, Google Pay, Apple Pay, Bancomat Pay, MyBank, PayPal and PayPal Pay in 3.
    """,
    'author': 'Alberto',
    'website': 'https://github.com/meviusisback/payment_nexi',
    'license': 'LGPL-3',
    'depends': ['payment', 'website', 'website_sale'],
    'external_dependencies': {'python': ['requests']},
    'data': [
        'security/ir.model.access.csv',
        'views/payment_nexi_templates.xml',
        'data/payment_method_data.xml',
        'data/cron.xml',
    ],
    'post_init_hook': 'post_init_hook',
    'application': False,
    'installable': True,
    'auto_install': False,
}

# -*- coding: utf-8 -*-
{
    'name': 'Nexi XPay Web',
    'version': '1.3.0',
    'category': 'Accounting/Payment Providers',
    'summary': 'Pagamenti Nexi XPay Web: Hosted Page o campi carta in cassa.',
    'description': """
Nexi XPay Web per Odoo 18.
Hosted Page (redirect a Nexi) oppure Embedded Build v3 con i campi carta dentro la cassa.
Nessun dato carta salvato in Odoo: solo il PAN mascherato.
Carta, Google Pay, Apple Pay, Bancomat Pay, MyBank, PayPal e PayPal Paga in 3.
    """,
    'author': 'Alberto',
    'website': 'https://github.com/meviusisback/payment_nexi',
    'license': 'LGPL-3',
    'depends': ['payment', 'website', 'website_sale'],
    'data': [
        'security/ir.model.access.csv',
        'views/payment_nexi_templates.xml',
        'data/payment_method_data.xml',
    ],
    'post_init_hook': 'post_init_hook',
    'application': False,
    'installable': True,
    'auto_install': False,
}

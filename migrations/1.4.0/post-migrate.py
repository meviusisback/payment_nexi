# -*- coding: utf-8 -*-
"""#7 punto 3: i metodi card/paypal/mybank/visa/mastercard/amex del modulo
erano duplicati del core. Riaggancia tutto sui record del core e cancella
i duplicati. Idempotente: si può rieseguire a vuoto."""
import logging

from odoo import SUPERUSER_ID, api

_logger = logging.getLogger(__name__)

# codice metodo -> xmlid del nostro duplicato (da togliere).
_DUPLICATES = {
    'card': 'payment_nexi.payment_method_nexi_card',
    'paypal': 'payment_nexi.payment_method_nexi_paypal',
    'mybank': 'payment_nexi.payment_method_nexi_mybank',
    'visa': 'payment_nexi.payment_method_nexi_visa',
    'mastercard': 'payment_nexi.payment_method_nexi_mastercard',
    'amex': 'payment_nexi.payment_method_nexi_amex',
    # Codici tolti dal modulo ma rimasti sui db vecchi: orfani da pulire.
    'satispay': 'payment_nexi.payment_method_nexi_satispay',
    'klarna': 'payment_nexi.payment_method_nexi_klarna',
}


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    Method = env['payment.method'].with_context(active_test=False)
    IrModelData = env['ir.model.data'].sudo()
    # I metodi core ereditati partono spenti: vanno accesi per i provider
    # Nexi già abilitati (l'hook di installazione non gira sugli upgrade).
    nexi_on = env['payment.provider'].sudo().search([
        ('code', '=', 'nexi'), ('state', '!=', 'disabled'),
    ])
    for code, dup_xmlid in _DUPLICATES.items():
        dup = env.ref(dup_xmlid, raise_if_not_found=False)
        if not dup:
            continue  # installazione nuova, o già migrata: niente da fare
        core = Method.search(
            [('code', '=', code), ('id', '!=', dup.id)], limit=1,
        )
        IrModelData.search([
            ('module', '=', 'payment_nexi'), ('name', '=', dup_xmlid.split('.', 1)[1]),
        ]).unlink()
        if not core:
            # Nessun record del core (es. satispay tolto dal modulo):
            # archivio l'orfano invece di cancellarlo, così lo storico resta.
            dup.write({'active': False})
            _logger.info('Nexi migrazione: metodo %s archiviato (orfano).', code)
            continue
        was_active = dup.active
        _move(cr, env, dup, core)
        dup.unlink()
        _logger.info('Nexi migrazione: metodo %s riagganciato sul core.', code)
        if was_active and nexi_on and code in nexi_on[:1]._get_default_payment_method_codes():
            core.write({'active': True})

def _move(cr, env, dup, core):
    """Sposta ogni relazione dal duplicato al metodo del core."""
    # Transazioni e token puntano al record: basta riscrivere l'id.
    env['payment.transaction'].sudo().search(
        [('payment_method_id', '=', dup.id)]
    ).write({'payment_method_id': core.id})
    env['payment.token'].sudo().search(
        [('payment_method_id', '=', dup.id)]
    ).write({'payment_method_id': core.id})
    # Verifica metodi Nexi: unique(provider, method), quindi prima si tolgono
    # le righe che andrebbero in conflitto, poi si sposta il resto.
    Avail = env['payment.nexi.method.availability'].sudo()
    clash = Avail.search([('method_id', '=', core.id)]).mapped('provider_id')
    if clash:
        Avail.search([
            ('method_id', '=', dup.id), ('provider_id', 'in', clash.ids),
        ]).unlink()
    Avail.search([('method_id', '=', dup.id)]).write({'method_id': core.id})
    # Provider collegati: INSERT ... ON CONFLICT perché la riga col core
    # potrebbe già esserci (provider con entrambi i metodi).
    cr.execute(
        """
        INSERT INTO payment_method_payment_provider_rel
            (payment_method_id, payment_provider_id)
        SELECT %s, payment_provider_id
          FROM payment_method_payment_provider_rel
         WHERE payment_method_id = %s
        ON CONFLICT DO NOTHING
        """,
        (core.id, dup.id),
    )
    cr.execute(
        'DELETE FROM payment_method_payment_provider_rel WHERE payment_method_id = %s',
        (dup.id,),
    )

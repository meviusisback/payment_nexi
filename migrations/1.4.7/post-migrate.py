# -*- coding: utf-8 -*-
"""#11: segna come avviate le draft con un ordine gia' creato su Nexi.

Il cron ora riconcilia anche le transazioni rimaste draft, ma solo quelle con
`nexi_order_started`: una draft senza ordine e' un carrello mai arrivato al
gateway e non va sondata. Per le installazioni esistenti l'unico caso
ricostruibile senza rischi e' la Build, che salva `nexi_build_session` appena
l'ordine esiste. Per l'HPP il gateway non lascia traccia locale: quelle draft
restano fuori dal cron, come prima. Idempotente.
"""


def migrate(cr, version):
    if not version:
        return
    cr.execute("""
        UPDATE payment_transaction t
           SET nexi_order_started = TRUE
          FROM payment_provider p
         WHERE t.provider_id = p.id
           AND p.code = 'nexi'
           AND t.state = 'draft'
           AND t.nexi_build_session IS NOT NULL
           AND t.nexi_order_started = FALSE
    """)

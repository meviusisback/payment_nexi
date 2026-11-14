# -*- coding: utf-8 -*-
"""#11: mark as started the drafts with an order already created on Nexi.

The cron now also reconciles the transactions left in draft, but only those
with `nexi_order_started`: a draft without an order is a cart that never
reached the gateway and must not be probed. For existing installations the only
case that can be reconstructed safely is Build, which saves
`nexi_build_session` as soon as the order exists. For HPP the gateway leaves no
local trace: those drafts stay out of the cron, as before. Idempotent.
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

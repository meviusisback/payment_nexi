# -*- coding: utf-8 -*-
"""Test Odoo per issue #1. Girano su Odoo vero (`odoo-bin -i payment_nexi --test-enable`).

Coprono il cablaggio ORM/QWeb che pytest senza runtime non puo toccare:
isolamento provider, disponibilita per terminale, contratti vincolati al
provider, landing route e override limitati a Nexi.
"""
from unittest.mock import patch

from odoo.tests.common import TransactionCase


class TestIssue001(TransactionCase):

    def _provider(self, **kw):
        vals = {
            'name': 'Nexi XPay Web',
            'code': 'nexi',
            'state': 'test',
            'is_published': True,
            'nexi_api_key': 'key',
            'nexi_env': 'test',
            'nexi_test_url': 'https://xpaysandbox.nexigroup.com/api/phoenix-0.0/psp/api/v1/orders/hpp',
        }
        vals.update(kw)
        return self.env['payment.provider'].create(vals)

    # 1. Whitelist: produzione documentata passa, estranei no.
    def test_whitelist_admits_documented_production_host(self):
        from odoo.addons.payment_nexi.models.payment_provider import NEXI_HOSTS
        self.assertIn('xpay.nexigroup.com', NEXI_HOSTS)
        p = self._provider(
            nexi_env='prod',
            nexi_prod_url='https://xpay.nexigroup.com/api/phoenix-0.0/psp/api/v1/orders/hpp',
        )
        self.assertTrue(p._nexi_base_url().startswith('https://xpay.nexigroup.com/'))
        p.nexi_prod_url = 'https://evil.example.com/pay'
        with self.assertRaises(Exception):
            p._nexi_base_url()

    # 6. Override limitati a Nexi.
    def test_supported_currencies_scoped_to_nexi(self):
        nexi = self._provider()
        other = self._provider(code='none', name='Altro')
        self.assertEqual(nexi._get_supported_currencies().mapped('name'), ['EUR'])
        self.assertTrue(len(other._get_supported_currencies()) > 1)
        self.assertIn('card', nexi._get_default_payment_method_codes())
        self.assertEqual(other._get_default_payment_method_codes(), set())

    # 8/9. Verifica metodi: importi minimi, solo "non abilitato" nasconde,
    # stato per terminale.
    def test_check_methods_per_terminal_and_outage_safe(self):
        from odoo.addons.payment_nexi.models import payment_provider as pp_mod
        p1 = self._provider()
        p2 = self._provider()
        method = self.env['payment.method'].create({
            'name': 'PayPal Paga in 3', 'code': 'paypal_bnpl', 'active': True,
            'provider_ids': [(6, 0, [p1.id, p2.id])],
        })
        seen = {}

        def fake_request(self, http_method, url, payload=None, params=None):
            service = (payload or {}).get('paymentSession', {}).get('paymentService')
            seen[service] = (payload or {}).get('order', {}).get('amount')
            # Paga in 3 attivo su p2, spento su p1.
            if self.id == p1.id:
                raise pp_mod.NexiApiError(
                    'Nexi: paymentService not enabled for terminal [PS001]',
                    status_code=400, gateway_code='PS001')
            return {}

        with patch.object(type(p1), '_nexi_make_request', fake_request):
            p1.button_nexi_check_methods()
            p2.button_nexi_check_methods()
        self.assertGreaterEqual(int(seen['PAYPAL_PAGAIN3']), 3000)
        self.assertTrue(method._nexi_is_disabled(p1))
        self.assertFalse(method._nexi_is_disabled(p2))
        # Il metodo resta offerto in checkout con p2.
        kept = method._get_compatible_payment_methods(
            [p2.id], self.env.user.partner_id.id)
        self.assertIn(method, kept)
        gone = method._get_compatible_payment_methods(
            [p1.id], self.env.user.partner_id.id)
        self.assertNotIn(method, gone)

    def test_check_methods_timeout_changes_nothing(self):
        from odoo.addons.payment_nexi.models import payment_provider as pp_mod
        p = self._provider()
        method = self.env['payment.method'].create({
            'name': 'Carta', 'code': 'card', 'active': True,
            'provider_ids': [(6, 0, [p.id])],
        })

        def boom(self, http_method, url, payload=None, params=None):
            raise pp_mod.ValidationError('Nexi non raggiungibile. Riprova.')

        with patch.object(type(p), '_nexi_make_request', boom):
            p.button_nexi_check_methods()
        self.assertFalse(method._nexi_is_disabled(p))
        self.assertIn('non verificato', p.nexi_methods_report)

    # 2. CAPTURE decide l'esito.
    def test_last_outcome_prefers_capture(self):
        Tx = self.env['payment.transaction']
        op = Tx._nexi_last_outcome({'operations': [
            {'operationType': 'AUTHORIZATION', 'operationResult': 'PENDING',
             'operationTime': '2026-01-01T10:00:00'},
            {'operationType': 'CAPTURE', 'operationResult': 'EXECUTED',
             'operationTime': '2026-01-01T10:01:00', 'operationAmount': '100'},
        ]})
        self.assertEqual(
            (op['operationType'], op['operationResult']), ('CAPTURE', 'EXECUTED'))
        self.assertEqual(Tx._nexi_last_outcome({}), {})

    # 10/11. Scelta carta esplicita + contratti vincolati al provider.
    def test_saved_card_choice_and_provider_scoping(self):
        p1 = self._provider(nexi_oneclick=True)
        p2 = self._provider(nexi_oneclick=True)
        partner = self.env.user.partner_id
        eur = self.env.ref('base.EUR')
        card = self.env['payment.method'].search([('code', '=', 'card')], limit=1)
        old = self.env['payment.transaction'].create({
            'provider_id': p1.id, 'payment_method_id': card.id,
            'amount': 10.0, 'currency_id': eur.id, 'partner_id': partner.id,
            'reference': 'NEXI-FIX-001', 'nexi_contract_id': 'NAAAAA1111111111',
            'nexi_masked_pan': '**** 1111',
        })
        old.state = 'done'
        Tx = self.env['payment.transaction']
        self.assertEqual(len(Tx.get_nexi_saved_cards(partner.id, p1.id)), 1)
        self.assertEqual(Tx.get_nexi_saved_cards(partner.id, p2.id), [])
        self.assertEqual(
            Tx._nexi_contract_for_partner(partner.id, p1.id), 'NAAAAA1111111111')
        self.assertFalse(Tx._nexi_contract_for_partner(partner.id, p2.id))

        def fake_post(self, provider, url, payload, want_save=False):
            fake_post.last = (provider, payload)
            return {'hostedPage': 'https://xpay.nexigroup.com/pay?x=1',
                    'securityToken': 't'}

        tx = self.env['payment.transaction'].create({
            'provider_id': p1.id, 'payment_method_id': card.id,
            'amount': 10.0, 'currency_id': eur.id, 'partner_id': partner.id,
            'reference': 'NEXI-FIX-002',
        })
        base_vals = {'amount': 10.0, 'currency_id': eur.id, 'partner_id': partner.id,
                     'reference': tx.reference, 'should_tokenize': False,
                     'base_url': 'https://shop.example.com'}
        with patch.object(type(tx), '_nexi_post_order', fake_post):
            # "Nuova carta" esplicita: niente recurrence.
            tx._get_specific_rendering_values(dict(base_vals, nexi_contract_choice='new'))
            self.assertNotIn('recurrence', fake_post.last[1]['paymentSession'])
            # Carta scelta: SUBSEQUENT con quel contratto.
            tx._get_specific_rendering_values(dict(
                base_vals, nexi_contract_choice='NAAAAA1111111111'))
            rec = fake_post.last[1]['paymentSession']['recurrence']
            self.assertEqual(
                (rec['action'], rec['contractId']),
                ('SUBSEQUENT_PAYMENT', 'NAAAAA1111111111'))
            # Contratto altrui: rifiutato.
            with self.assertRaises(Exception):
                tx._get_specific_rendering_values(dict(
                    base_vals, nexi_contract_choice='NFFFFF9999999999'))

    # 12. Confirm: chi paga dal negozio esce dallo status, chi paga fattura
    # resta sul suo documento. Regola pura, senza HTTP.
    def test_confirm_landing_route_policy(self):
        from odoo.addons.payment_nexi.controllers.main import _nexi_landing_route
        bill = '/my/invoices/42?tx_id=1'
        self.assertEqual(_nexi_landing_route('/payment/status', True), '/shop/payment/validate')
        self.assertEqual(_nexi_landing_route('', True), '/shop/payment/validate')
        self.assertEqual(_nexi_landing_route(bill, False), bill)
        self.assertEqual(_nexi_landing_route(bill, True), bill)
        self.assertEqual(_nexi_landing_route('', False), '')

    # 3/4. Prepare: provider compatibile con l'ordine + carrello pronto.
    def test_prepare_provider_and_cart_policy(self):
        from odoo.addons.payment_nexi.controllers import main as ctrl
        import inspect
        src = inspect.getsource(ctrl.NexiController.nexi_build_prepare)
        self.assertIn('_check_cart_is_ready_to_be_paid', src)
        self.assertIn('_get_compatible_providers', src)
        self.assertIn('Metodo non disponibile su questo terminale', src)

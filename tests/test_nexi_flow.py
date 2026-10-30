# -*- coding: utf-8 -*-
"""Test Odoo del modulo: girano con --test-enable.

    odoo -d <db> -i payment_nexi --test-enable --stop-after-init

Coprono le due cose che in cassa si rompono davvero: l'esito del pagamento
quando Nexi manda anche il CAPTURE, e i metodi nascosti per singolo terminale.
"""
from unittest.mock import patch

from odoo.tests import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestNexiReview(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # EUR può essere inattivo se l'azienda usa un'altra valuta: serve
        # comunque, perché Nexi lavora solo in EUR.
        cls.currency_eur = cls.env['res.currency'].with_context(
            active_test=False
        ).search([('name', '=', 'EUR')], limit=1)
        if cls.currency_eur:
            cls.currency_eur.active = True
        cls.method_card = cls.env['payment.method'].search([('code', '=', 'card')], limit=1)
        cls.partner = cls.env['res.partner'].create({'name': 'Cliente Nexi test'})
        cls.provider = cls.env['payment.provider'].create({
            'name': 'Nexi test',
            'code': 'nexi',
            'state': 'test',
            'is_published': True,
            'nexi_api_key': 'chiave-di-prova',
            'nexi_env': 'test',
            'nexi_mode': 'hpp',
            'nexi_test_url': 'https://xpaysandbox.nexigroup.com/orders/hpp',
            'payment_method_ids': [(6, 0, cls.method_card.ids)],
        })

    def _new_tx(self, amount=10.0):
        return self.env['payment.transaction'].create({
            'provider_id': self.provider.id,
            'payment_method_id': self.method_card.id,
            'reference': 'NEXITEST%d' % (self.env['payment.transaction'].search_count([]) + 1),
            'amount': amount,
            'currency_id': (self.currency_eur or self.provider.company_id.currency_id).id,
            'partner_id': self.partner.id,
            'operation': 'online_redirect',
            'landing_route': '/shop/payment/validate',
        })

    def _nexi_answer(self, order_ref, captured, *operations):
        """Risposta di GET /orders/{ref} con le operazioni indicate."""
        return {
            'orderId': order_ref,
            'orderStatus': {
                'order': {'orderId': order_ref},
                'capturedAmount': str(captured),
            },
            'operations': [
                {
                    'operationType': kind,
                    'operationResult': result,
                    'operationTime': when,
                    'operationAmount': str(captured),
                    'operationCurrency': 'EUR',
                    'paymentInstrumentInfo': '************1234',
                }
                for kind, result, when in operations
            ],
        }

    def test_capture_after_authorization_closes_payment(self):
        """Nexi manda AUTHORIZATION + CAPTURE: il pagamento deve chiudersi."""
        tx = self._new_tx()
        data = self._nexi_answer(
            tx.reference, 1000,
            ('AUTHORIZATION', 'EXECUTED', '2026-10-30T10:00:00'),
            ('CAPTURE', 'EXECUTED', '2026-10-30T10:00:05'),
        )
        with patch.object(type(self.provider), '_nexi_make_request', return_value=data):
            tx._process_notification_data({})
        self.assertEqual(tx.state, 'done')
        self.assertIn('1234', tx.nexi_masked_pan or '')

    def test_capture_only_closes_payment(self):
        """Nexi a volte manda solo il CAPTURE: senza guardarlo resta appeso."""
        tx = self._new_tx()
        data = self._nexi_answer(
            tx.reference, 1000,
            ('CAPTURE', 'EXECUTED', '2026-10-30T10:00:05'),
        )
        with patch.object(type(self.provider), '_nexi_make_request', return_value=data):
            tx._process_notification_data({})
        self.assertEqual(tx.state, 'done')

    def test_capture_without_operation_amount_closes_payment(self):
        """Sull'incasso l'importo può stare solo su capturedAmount."""
        tx = self._new_tx()
        data = self._nexi_answer(
            tx.reference, 1000,
            ('CAPTURE', 'EXECUTED', '2026-10-30T10:00:05'),
        )
        for operation in data['operations']:
            operation.pop('operationAmount', None)
        with patch.object(type(self.provider), '_nexi_make_request', return_value=data):
            tx._process_notification_data({})
        self.assertEqual(tx.state, 'done', 'incassato: non deve finire in errore')

    def test_wrong_order_id_is_refused(self):
        """La risposta deve riferirsi a QUESTO ordine."""
        tx = self._new_tx()
        data = self._nexi_answer(
            'ALTRO-ORDINE', 1000,
            ('CAPTURE', 'EXECUTED', '2026-10-30T10:00:05'),
        )
        with patch.object(type(self.provider), '_nexi_make_request', return_value=data):
            tx._process_notification_data({})
        self.assertEqual(tx.state, 'error')

    def test_missing_order_id_is_refused(self):
        """Senza orderId non posso dire che sia il mio: fail-closed."""
        tx = self._new_tx()
        data = self._nexi_answer(
            tx.reference, 1000,
            ('CAPTURE', 'EXECUTED', '2026-10-30T10:00:05'),
        )
        data.pop('orderId', None)
        data['orderStatus'].pop('order', None)
        with patch.object(type(self.provider), '_nexi_make_request', return_value=data):
            tx._process_notification_data({})
        self.assertEqual(tx.state, 'error')

    def test_authorization_only_stays_pending(self):
        tx = self._new_tx()
        data = self._nexi_answer(
            tx.reference, 0,
            ('AUTHORIZATION', 'AUTHORIZED', '2026-10-30T10:00:00'),
        )
        with patch.object(type(self.provider), '_nexi_make_request', return_value=data):
            tx._process_notification_data({})
        self.assertEqual(tx.state, 'pending')

    def test_wrong_amount_is_refused(self):
        tx = self._new_tx(amount=99.0)
        data = self._nexi_answer(
            tx.reference, 1000,
            ('AUTHORIZATION', 'EXECUTED', '2026-10-30T10:00:00'),
            ('CAPTURE', 'EXECUTED', '2026-10-30T10:00:05'),
        )
        with patch.object(type(self.provider), '_nexi_make_request', return_value=data):
            tx._process_notification_data({})
        self.assertEqual(tx.state, 'error', 'importo diverso: non deve chiudere')

    def test_method_hidden_only_on_the_terminal_that_refuses_it(self):
        other = self.provider.copy({'name': 'Nexi secondo terminale'})
        self.env['payment.nexi.method.availability'].create({
            'provider_id': self.provider.id,
            'method_id': self.method_card.id,
            'disabled': True,
        })
        Method = self.env['payment.method']
        here = Method._get_compatible_payment_methods([self.provider.id], self.partner.id)
        there = Method._get_compatible_payment_methods([other.id], self.partner.id)
        self.assertNotIn(self.method_card, here)
        self.assertIn(self.method_card, there)

    def test_manual_flag_hides_on_every_terminal(self):
        self.method_card.nexi_disabled = True
        Method = self.env['payment.method']
        methods = Method._get_compatible_payment_methods([self.provider.id], self.partner.id)
        self.assertNotIn(self.method_card, methods)

    def test_nexi_supports_only_eur(self):
        self.assertEqual(self.provider._get_supported_currencies().mapped('name'), ['EUR'])

    def test_nexi_default_methods(self):
        self.assertEqual(
            self.provider._get_default_payment_method_codes(),
            {'card', 'googlepay', 'applepay', 'bancomatpay', 'mybank', 'paypal', 'paypal_bnpl'},
        )

    def test_saved_cards_belong_to_one_terminal(self):
        partner = self.env.user.partner_id
        other = self.provider.copy({'name': 'Nexi terzo terminale'})
        tx = self.env['payment.transaction'].create({
            'provider_id': self.provider.id,
            'payment_method_id': self.method_card.id,
            'reference': 'NEXICONTRACT1',
            'amount': 10.0,
            'currency_id': (self.currency_eur or self.provider.company_id.currency_id).id,
            'partner_id': partner.id,
            'nexi_contract_id': 'NCONTRATTO1',
            'nexi_masked_pan': '************9999',
        })
        tx._set_done()
        Tx = self.env['payment.transaction']
        here = Tx.get_nexi_saved_cards(partner.id, self.provider.id)
        there = Tx.get_nexi_saved_cards(partner.id, other.id)
        self.assertEqual([c['contract_id'] for c in here], ['NCONTRATTO1'])
        self.assertEqual(there, [], 'la carta non vale per un altro terminale')

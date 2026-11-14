# -*- coding: utf-8 -*-
"""Odoo tests for the module: they run with --test-enable.

    odoo -d <db> -i payment_nexi --test-enable --stop-after-init

They cover the two things that really break at checkout: the payment outcome
when Nexi also sends the CAPTURE, and the methods hidden per single terminal.
"""
from unittest.mock import patch

from odoo.exceptions import ValidationError
from odoo.tests import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestNexiReview(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # EUR can be inactive if the company uses another currency: it is needed
        # anyway, because Nexi only works in EUR.
        cls.currency_eur = cls.env['res.currency'].with_context(
            active_test=False
        ).search([('name', '=', 'EUR')], limit=1)
        if cls.currency_eur:
            cls.currency_eur.active = True
        cls.method_card = cls.env['payment.method'].with_context(active_test=False).search([('code', '=', 'card')], limit=1)
        cls.partner = cls.env['res.partner'].create({'name': 'Nexi test customer'})
        cls.provider = cls.env['payment.provider'].create({
            'name': 'Nexi test',
            'code': 'nexi',
            'state': 'test',
            'is_published': True,
            'nexi_api_key': 'test-key',
            'nexi_env': 'test',
            'nexi_mode': 'hpp',
            'nexi_test_url': 'https://xpaysandbox.nexigroup.com/orders/hpp',
            'payment_method_ids': [(6, 0, cls.method_card.ids)],
        })
        # The test provider is born already enabled (create, not write): the
        # core turns the methods on only on a state change, so it is needed here.
        cls.provider._activate_default_pms()

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
        """Response of GET /orders/{ref} with the given operations."""
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
        """Nexi sends AUTHORIZATION + CAPTURE: the payment must close."""
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
        """Nexi sometimes sends only the CAPTURE: without looking at it the payment hangs."""
        tx = self._new_tx()
        data = self._nexi_answer(
            tx.reference, 1000,
            ('CAPTURE', 'EXECUTED', '2026-10-30T10:00:05'),
        )
        with patch.object(type(self.provider), '_nexi_make_request', return_value=data):
            tx._process_notification_data({})
        self.assertEqual(tx.state, 'done')

    def test_capture_without_operation_amount_closes_payment(self):
        """On the capture the amount can be only in capturedAmount."""
        tx = self._new_tx()
        data = self._nexi_answer(
            tx.reference, 1000,
            ('CAPTURE', 'EXECUTED', '2026-10-30T10:00:05'),
        )
        for operation in data['operations']:
            operation.pop('operationAmount', None)
        with patch.object(type(self.provider), '_nexi_make_request', return_value=data):
            tx._process_notification_data({})
        self.assertEqual(tx.state, 'done', 'captured: it must not end in error')

    def test_capture_without_any_captured_amount_is_refused(self):
        """CAPTURE executed but no amount anywhere: fail-closed."""
        tx = self._new_tx()
        data = self._nexi_answer(
            tx.reference, 1000,
            ('CAPTURE', 'EXECUTED', '2026-10-30T10:00:05'),
        )
        data['orderStatus'].pop('capturedAmount', None)
        for operation in data['operations']:
            operation.pop('operationAmount', None)
            operation.pop('capturedAmount', None)
        with patch.object(type(self.provider), '_nexi_make_request', return_value=data):
            tx._process_notification_data({})
        self.assertEqual(tx.state, 'error')

    def test_capture_with_decimal_amount_closes_payment(self):
        """Decimal format (euro): it is converted, not refused."""
        tx = self._new_tx()
        data = self._nexi_answer(
            tx.reference, 1000,
            ('CAPTURE', 'EXECUTED', '2026-10-30T10:00:05'),
        )
        data['orderStatus']['capturedAmount'] = '10.00'
        for operation in data['operations']:
            operation['operationAmount'] = '10.00'
        with patch.object(type(self.provider), '_nexi_make_request', return_value=data):
            tx._process_notification_data({})
        self.assertEqual(tx.state, 'done', '10.00 euro = 1000 cents')

    def test_capture_summing_operations_closes_payment(self):
        """Two partial captures that cover the figure: it closes."""
        tx = self._new_tx()
        data = self._nexi_answer(
            tx.reference, 0,
            ('CAPTURE', 'EXECUTED', '2026-10-30T10:00:05'),
            ('CAPTURE', 'EXECUTED', '2026-10-30T10:00:06'),
            ('CAPTURE', 'EXECUTED', '2026-10-30T10:00:07'),
        )
        data['orderStatus'].pop('capturedAmount', None)
        data['operations'][0]['operationAmount'] = '600'
        data['operations'][1]['operationAmount'] = '400'
        # The deciding operation (the latest) does not carry the amount: the sum
        # of the captures must count, not a false "not complete".
        data['operations'][2].pop('operationAmount', None)
        data['operations'][2].pop('capturedAmount', None)
        with patch.object(type(self.provider), '_nexi_make_request', return_value=data):
            tx._process_notification_data({})
        self.assertEqual(tx.state, 'done')

    def test_wrong_order_id_is_refused(self):
        """The response must refer to THIS order."""
        tx = self._new_tx()
        data = self._nexi_answer(
            'OTHER-ORDER', 1000,
            ('CAPTURE', 'EXECUTED', '2026-10-30T10:00:05'),
        )
        with patch.object(type(self.provider), '_nexi_make_request', return_value=data):
            tx._process_notification_data({})
        self.assertEqual(tx.state, 'error')

    def test_missing_order_id_is_refused(self):
        """Without orderId I cannot tell it is mine: fail-closed."""
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
        self.assertEqual(tx.state, 'error', 'different amount: it must not close')

    def test_auth_executed_without_capture_stays_pending(self):
        """H1: an AUTHORIZATION EXECUTED does not prove the capture."""
        tx = self._new_tx()
        data = {
            'orderId': tx.reference,
            'operations': [{
                'operationType': 'AUTHORIZATION',
                'operationResult': 'EXECUTED',
                'operationAmount': '1000',
                'operationCurrency': 'EUR',
                'operationTime': '2026-10-30T10:00:00',
                'paymentInstrumentInfo': '**** 1234',
            }],
        }
        with patch.object(type(self.provider), '_nexi_make_request', return_value=data):
            tx._process_notification_data({})
        self.assertEqual(tx.state, 'pending', 'authorized is not captured')

    def test_auth_with_zero_captured_stays_pending(self):
        """H1: explicit capturedAmount 0 -> no close."""
        tx = self._new_tx()
        data = self._nexi_answer(
            tx.reference, 0,
            ('AUTHORIZATION', 'EXECUTED', '2026-10-30T10:00:00'),
        )
        data['orderStatus']['capturedAmount'] = '0'
        with patch.object(type(self.provider), '_nexi_make_request', return_value=data):
            tx._process_notification_data({})
        self.assertEqual(tx.state, 'pending')

    def test_order_level_capture_proves_done_without_capture_operation(self):
        """H1: order capturedAmount >= expected is capture evidence."""
        tx = self._new_tx()
        data = self._nexi_answer(
            tx.reference, 1000,
            ('AUTHORIZATION', 'EXECUTED', '2026-10-30T10:00:00'),
        )
        with patch.object(type(self.provider), '_nexi_make_request', return_value=data):
            tx._process_notification_data({})
        self.assertEqual(tx.state, 'done')

    def test_later_declined_auth_does_not_cancel_capture(self):
        """H1: a later decline does not undo an executed CAPTURE."""
        tx = self._new_tx()
        data = {
            'orderId': tx.reference,
            'operations': [
                {
                    'operationType': 'CAPTURE',
                    'operationResult': 'EXECUTED',
                    'operationAmount': '1000',
                    'operationCurrency': 'EUR',
                    'operationTime': '2026-10-30T10:00:05',
                },
                {
                    'operationType': 'AUTHORIZATION',
                    'operationResult': 'DECLINED',
                    'operationAmount': '1000',
                    'operationCurrency': 'EUR',
                    'operationTime': '2026-10-30T10:00:06',
                },
            ],
        }
        with patch.object(type(self.provider), '_nexi_make_request', return_value=data):
            tx._process_notification_data({})
        self.assertEqual(tx.state, 'done', 'the money was taken')

    def test_refund_after_capture_still_cancels(self):
        """H1: a reversal after the capture wins over the sticky capture."""
        tx = self._new_tx()
        data = {
            'orderId': tx.reference,
            'operations': [
                {
                    'operationType': 'CAPTURE',
                    'operationResult': 'EXECUTED',
                    'operationAmount': '1000',
                    'operationCurrency': 'EUR',
                    'operationTime': '2026-10-30T10:00:05',
                },
                {
                    'operationType': 'REFUND',
                    'operationResult': 'EXECUTED',
                    'operationAmount': '1000',
                    'operationCurrency': 'EUR',
                    'operationTime': '2026-10-30T10:00:06',
                },
                {
                    'operationType': 'AUTHORIZATION',
                    'operationResult': 'DECLINED',
                    'operationAmount': '1000',
                    'operationCurrency': 'EUR',
                    'operationTime': '2026-10-30T10:00:07',
                },
            ],
        }
        with patch.object(type(self.provider), '_nexi_make_request', return_value=data):
            tx._process_notification_data({})
        self.assertEqual(tx.state, 'cancel')

    def test_method_hidden_only_on_the_terminal_that_refuses_it(self):
        other = self.provider.copy({'name': 'Nexi second terminal'})
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
            {'card', 'googlepay', 'applepay', 'bancomatpay', 'mybank', 'paypal',
             'paypal_bnpl', 'visa', 'mastercard', 'amex'},
        )

    def test_card_brand_icons_are_active_and_linked(self):
        """The core brands must be on, otherwise the Card row loses its icons."""
        Method = self.env['payment.method'].with_context(active_test=False)
        brands = Method.search([('code', 'in', ('visa', 'mastercard', 'amex'))])
        self.assertEqual(len(brands), 3)
        self.provider.write({
            'payment_method_ids': [(4, method.id) for method in brands],
        })
        self.provider._activate_default_pms()
        brands.invalidate_recordset()
        self.provider.invalidate_recordset()
        self.assertTrue(all(b.active for b in brands))
        self.assertTrue(all(b in self.provider.payment_method_ids for b in brands))
        self.assertEqual(
            set(self.method_card.brand_ids[:3].mapped('code')),
            {'visa', 'mastercard', 'amex'},
            'the Card row must show the three brands first',
        )
        # The icons are files of the module, they do not depend on the test filestore.
        import os
        from odoo.modules import get_module_path
        base = os.path.join(get_module_path('payment_nexi'), 'static', 'src', 'img')
        for code in ('visa', 'mastercard', 'amex'):
            self.assertTrue(
                os.path.isfile(os.path.join(base, code + '.png')),
                'icon %s is missing' % code,
            )

    def test_method_follows_the_terminal_the_row_will_use(self):
        """The row uses the FIRST compatible provider: if that one refuses the
        method, the method must not appear (the row would fail)."""
        first = self.provider                      # lowest id: this is the row
        second = self.provider.copy({'name': 'Nexi second'})
        self.env['payment.nexi.method.availability'].create({
            'provider_id': first.id, 'method_id': self.method_card.id, 'disabled': True,
        })
        Method = self.env['payment.method']
        both = Method._get_compatible_payment_methods([first.id, second.id], self.partner.id)
        self.assertNotIn(self.method_card, both, 'the row terminal refuses the method')
        # Refused only by the second one: the row uses the first, which accepts it.
        self.env['payment.nexi.method.availability'].search([
            ('provider_id', '=', first.id), ('method_id', '=', self.method_card.id),
        ]).unlink()
        self.env['payment.nexi.method.availability'].create({
            'provider_id': second.id, 'method_id': self.method_card.id, 'disabled': True,
        })
        both = Method._get_compatible_payment_methods([first.id, second.id], self.partner.id)
        self.assertIn(self.method_card, both)

    def test_reference_longer_than_nexi_limit_is_refused(self):
        """Nexi accepts orderIds up to 27 characters: a clear error is better."""
        tx = self.env['payment.transaction'].create({
            'provider_id': self.provider.id,
            'payment_method_id': self.method_card.id,
            'reference': 'REFERENCE-VERY-LONG-0123456789',
            'amount': 10.0,
            'currency_id': (self.currency_eur or self.provider.company_id.currency_id).id,
            'partner_id': self.partner.id,
            'operation': 'online_redirect',
        })
        vals = {'amount': 10.0, 'currency_id': tx.currency_id.id,
                'partner_id': self.partner.id, 'reference': tx.reference,
                'should_tokenize': False, 'base_url': 'https://shop.example.com'}
        with patch.object(type(tx), '_nexi_post_order') as post:
            with self.assertRaises(ValidationError):
                tx._get_specific_rendering_values(dict(vals))
            self.assertFalse(post.called, 'it must not even call Nexi')

    def test_saved_cards_belong_to_one_terminal(self):
        partner = self.env.user.partner_id
        other = self.provider.copy({'name': 'Nexi third terminal'})
        tx = self.env['payment.transaction'].create({
            'provider_id': self.provider.id,
            'payment_method_id': self.method_card.id,
            'reference': 'NEXICONTRACT1',
            'amount': 10.0,
            'currency_id': (self.currency_eur or self.provider.company_id.currency_id).id,
            'partner_id': partner.id,
            'nexi_contract_id': 'NCONTRACT1',
            'nexi_masked_pan': '************9999',
        })
        tx._set_done()
        Tx = self.env['payment.transaction']
        here = Tx.get_nexi_saved_cards(partner.id, self.provider.id)
        there = Tx.get_nexi_saved_cards(partner.id, other.id)
        self.assertEqual([c['contract_id'] for c in here], ['NCONTRACT1'])
        self.assertEqual(there, [], 'the card is not valid for another terminal')

    def test_reconcile_closes_stuck_payment(self):
        """Cron: old pending + capture on Nexi -> done, without the browser."""
        from datetime import timedelta
        from odoo import fields as _fields
        tx = self._new_tx()
        tx._set_pending(state_message='pending')
        old = _fields.Datetime.now() - timedelta(minutes=30)
        self.env.cr.execute(
            'UPDATE payment_transaction SET create_date = %s WHERE id = %s',
            (old, tx.id),
        )
        tx.invalidate_recordset()
        data = self._nexi_answer(
            tx.reference, 1000,
            ('CAPTURE', 'EXECUTED', '2026-10-30T10:00:05'),
        )
        Tx = self.env['payment.transaction']
        with patch.object(type(self.provider), '_nexi_make_request', return_value=data):
            with patch.object(type(Tx), '_post_process', return_value=True):
                found = Tx._nexi_reconcile_pending()
        tx.invalidate_recordset()
        self.assertEqual(found, 1)
        self.assertEqual(tx.state, 'done')
        self.assertEqual(tx.nexi_reconcile_count, 1)

    def test_reconcile_skips_fresh_payment(self):
        """Cron: a just created pending -> not touched, counter still."""
        tx = self._new_tx()
        tx._set_pending(state_message='pending')
        Tx = self.env['payment.transaction']
        with patch.object(type(self.provider), '_nexi_make_request') as req:
            found = Tx._nexi_reconcile_pending()
        self.assertEqual(found, 0)
        self.assertFalse(req.called, 'too fresh: Nexi is not called')
        self.assertEqual(tx.nexi_reconcile_count, 0)

    def test_reconcile_counts_attempts_and_gives_up(self):
        """Cron: always PENDING -> it stays pending, after 72 rounds it leaves the loop."""
        from datetime import timedelta
        from odoo import fields as _fields
        tx = self._new_tx()
        tx._set_pending(state_message='pending')
        old = _fields.Datetime.now() - timedelta(minutes=30)
        self.env.cr.execute(
            'UPDATE payment_transaction SET create_date = %s WHERE id = %s',
            (old, tx.id),
        )
        tx.invalidate_recordset()
        data = self._nexi_answer(
            tx.reference, 0,
            ('AUTHORIZATION', 'PENDING', '2026-10-30T10:00:00'),
        )
        Tx = self.env['payment.transaction']
        with patch.object(type(self.provider), '_nexi_make_request', return_value=data):
            Tx._nexi_reconcile_pending()
        tx.invalidate_recordset()
        self.assertEqual(tx.state, 'pending')
        self.assertEqual(tx.nexi_reconcile_count, 1)
        tx.sudo().write({'nexi_reconcile_count': 72})
        with patch.object(type(self.provider), '_nexi_make_request') as req:
            found = Tx._nexi_reconcile_pending()
        self.assertEqual(found, 0)
        self.assertFalse(req.called, 'past the limit: Nexi is not called')

    def test_no_duplicate_method_codes(self):
        """#7 point 3: one code = one method (no duplicate of ours)."""
        Method = self.env['payment.method'].with_context(active_test=False)
        for code in ('card', 'paypal', 'mybank', 'visa', 'mastercard', 'amex'):
            with self.subTest(code=code):
                self.assertEqual(
                    Method.search_count([('code', '=', code)]), 1,
                    'code %s must exist only once' % code,
                )

    def test_migration_moves_duplicate_to_core(self):
        """#7 point 3: the migration re-links and deletes, twice in a row."""
        Method = self.env['payment.method'].with_context(active_test=False)
        core = Method.search([('code', '=', 'card')], limit=1)
        dup = Method.create({
            'name': 'Duplicate Card', 'code': 'card', 'active': True,
        })
        tx = self._new_tx()
        tx.payment_method_id = dup
        self.provider.write({'payment_method_ids': [(4, dup.id)]})
        self.env['ir.model.data'].create({
            'module': 'payment_nexi', 'name': 'payment_method_nexi_card',
            'model': 'payment.method', 'res_id': dup.id,
        })
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            'nexi_migrate', self._migration_script(),
        )
        migrate_mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migrate_mod)
        migrate_mod.migrate(self.env.cr, '1.4.0')
        self.assertFalse(dup.exists(), 'the duplicate must be deleted')
        tx.invalidate_recordset()
        self.assertEqual(tx.payment_method_id, core)
        self.assertIn(core, self.provider.payment_method_ids)
        # Second run: no-op, without errors and without changes.
        migrate_mod.migrate(self.env.cr, '1.4.0')
        self.assertEqual(tx.payment_method_id, core)

    def _migration_script(self, version='1.4.0'):
        import os
        from odoo.modules import get_module_path
        return os.path.join(
            get_module_path('payment_nexi'), 'migrations', version, 'post-migrate.py')

    def test_migration_restores_brand_icons_and_links(self):
        """#10 regression: 1.4.4 turns the core brands back on and re-links them."""
        Method = self.env['payment.method'].with_context(active_test=False)
        brands = Method.search([('code', 'in', ('visa', 'mastercard', 'amex'))])
        self.provider.write({
            'payment_method_ids': [(3, method.id) for method in brands],
        })
        brands.write({'active': False})
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            'nexi_migrate_144', self._migration_script('1.4.4'),
        )
        migrate_mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migrate_mod)
        migrate_mod.migrate(self.env.cr, '1.4.4')
        brands.invalidate_recordset()
        self.provider.invalidate_recordset()
        self.assertTrue(all(b.active for b in brands))
        self.assertTrue(all(b in self.provider.payment_method_ids for b in brands))
        # Second run: no-op, without errors.
        migrate_mod.migrate(self.env.cr, '1.4.4')

    def test_disagreeing_operations_without_time_stay_pending(self):
        """#8 H1: AUTH paid + CAPTURE KO without dates -> pending, never done."""
        tx = self._new_tx()
        data = self._nexi_answer(
            tx.reference, 1000,
            ('AUTHORIZATION', 'EXECUTED', None),
            ('CAPTURE', 'DECLINED', None),
        )
        with patch.object(type(self.provider), '_nexi_make_request', return_value=data):
            tx._process_notification_data({})
        self.assertEqual(tx.state, 'pending')

    def test_transient_gateway_error_stays_pending(self):
        """#8 H2: timeout/5xx from Nexi -> pending (the cron rechecks)."""
        from odoo.addons.payment_nexi.models.payment_provider import (
            NexiApiError, NexiUnavailable,
        )
        tx = self._new_tx()
        with patch.object(
            type(self.provider), '_nexi_make_request', side_effect=NexiUnavailable('down')
        ):
            tx._process_notification_data({})
        self.assertEqual(tx.state, 'pending')
        tx2 = self._new_tx()
        err = NexiApiError('Nexi: error', status_code=503)
        with patch.object(
            type(self.provider), '_nexi_make_request', side_effect=err
        ):
            tx2._process_notification_data({})
        self.assertEqual(tx2.state, 'pending')

    def test_missing_currency_is_refused(self):
        """#8 M2: EXECUTED without currency -> error, never done."""
        tx = self._new_tx()
        data = self._nexi_answer(
            tx.reference, 1000,
            ('CAPTURE', 'EXECUTED', '2026-10-30T10:00:05'),
        )
        for operation in data['operations']:
            operation.pop('operationCurrency', None)
        with patch.object(type(self.provider), '_nexi_make_request', return_value=data):
            tx._process_notification_data({})
        self.assertEqual(tx.state, 'error')

    def test_partial_order_total_is_refused(self):
        """#8 M1: operation 1000 but order total 600 -> error."""
        tx = self._new_tx()
        data = self._nexi_answer(
            tx.reference, 600,
            ('CAPTURE', 'EXECUTED', '2026-10-30T10:00:05'),
        )
        data['operations'][0]['operationAmount'] = '1000'
        with patch.object(type(self.provider), '_nexi_make_request', return_value=data):
            tx._process_notification_data({})
        self.assertEqual(tx.state, 'error')

    def test_http_path_makes_a_single_call(self):
        """#8 M6: a single GET in the request path, no sleep."""
        tx = self._new_tx()
        data = self._nexi_answer(
            tx.reference, 0,
            ('AUTHORIZATION', 'PENDING', '2026-10-30T10:00:00'),
        )
        with patch.object(
            type(self.provider), '_nexi_make_request', return_value=data
        ) as req:
            tx._process_notification_data({})
        self.assertEqual(req.call_count, 1)
        self.assertEqual(tx.state, 'pending')

    def test_reconcile_retries_post_processing(self):
        """#8 M3: done not post-processed -> the cron retries _post_process."""
        from datetime import timedelta
        from odoo import fields as _fields
        tx = self._new_tx()
        tx._set_done()
        tx.write({'is_post_processed': False})
        # #9 M-A3: the second pass has the same cutoff as the first one.
        old = _fields.Datetime.now() - timedelta(minutes=30)
        self.env.cr.execute(
            'UPDATE payment_transaction SET create_date = %s WHERE id = %s',
            (old, tx.id),
        )
        tx.invalidate_recordset()
        Tx = self.env['payment.transaction']
        with patch.object(type(Tx), '_post_process') as post:
            found = Tx._nexi_reconcile_pending()
        self.assertTrue(post.called)
        self.assertGreaterEqual(found, 1)

    def test_hpp_token_is_encoded_not_concatenated(self):
        """#8 M5: a securityToken with '&' does not become an extra parameter."""
        tx = self._new_tx()
        answer = {
            'hostedPage': 'https://xpay.nexigroup.com/pay?x=1',
            'securityToken': 't&amount=1',
        }
        with patch.object(
            type(self.provider), '_nexi_make_request', return_value=answer
        ):
            res = tx._get_specific_rendering_values({
                'amount': 10.0, 'currency_id': tx.currency_id.id,
                'partner_id': self.partner.id, 'reference': tx.reference,
                'should_tokenize': False,
                'base_url': 'https://shop.example.com',
            })
        self.assertNotIn('amount', res['url_params'])
        self.assertEqual(res['url_params']['securityToken'], 't&amount=1')

    def test_hpp_lookalike_host_is_refused(self):
        """#8 M5: hostedPage su host sosia -> rifiutato dal validatore unico."""
        from odoo.exceptions import ValidationError
        tx = self._new_tx()
        answer = {
            'hostedPage': 'https://xpay.nexigroup.com.evil.example/pay',
            'securityToken': 't',
        }
        with patch.object(
            type(self.provider), '_nexi_make_request', return_value=answer
        ):
            with self.assertRaises(ValidationError):
                tx._get_specific_rendering_values({
                    'amount': 10.0, 'currency_id': tx.currency_id.id,
                    'partner_id': self.partner.id, 'reference': tx.reference,
                    'should_tokenize': False,
                    'base_url': 'https://shop.example.com',
                })

    def test_hpp_embedded_token_is_replaced(self):
        """#9 M-A1: a securityToken inside hostedPage does not beat the real one."""
        tx = self._new_tx()
        answer = {
            'hostedPage': 'https://xpay.nexigroup.com/pay?securityToken=EVIL',
            'securityToken': 'real-token',
        }
        with patch.object(
            type(self.provider), '_nexi_make_request', return_value=answer
        ):
            res = tx._get_specific_rendering_values({
                'amount': 10.0, 'currency_id': tx.currency_id.id,
                'partner_id': self.partner.id, 'reference': tx.reference,
                'should_tokenize': False,
                'base_url': 'https://shop.example.com',
            })
        self.assertEqual(res['url_params']['securityToken'], 'real-token')

    def test_reconcile_rescues_buttons_order(self):
        """#9 H-A1: empty card order + paid -m buttons order -> done."""
        from datetime import timedelta
        from odoo import fields as _fields
        self.provider.nexi_mode = 'build'
        tx = self._new_tx()
        tx._set_pending(state_message='pending')
        old = _fields.Datetime.now() - timedelta(minutes=30)
        self.env.cr.execute(
            'UPDATE payment_transaction SET create_date = %s WHERE id = %s',
            (old, tx.id),
        )
        tx.invalidate_recordset()
        empty = self._nexi_answer(tx.reference, 0)
        paid = self._nexi_answer(
            tx.reference + '-m', 1000,
            ('CAPTURE', 'EXECUTED', '2026-10-30T10:00:05'),
        )

        def fake_request(method, url, payload=None, params=None):
            if url.endswith('/orders/%s-m' % tx.reference):
                return paid
            return empty

        Tx = self.env['payment.transaction']
        with patch.object(type(self.provider), '_nexi_make_request', side_effect=fake_request):
            with patch.object(type(Tx), '_post_process', return_value=True):
                Tx._nexi_reconcile_pending()
        tx.invalidate_recordset()
        self.assertEqual(tx.state, 'done')

    def test_reconcile_probe_never_condemns(self):
        """#9 H-A1: -m order missing -> the pending stays pending, never error."""
        from datetime import timedelta
        from odoo import fields as _fields
        from odoo.addons.payment_nexi.models.payment_provider import NexiApiError
        self.provider.nexi_mode = 'build'
        tx = self._new_tx()
        tx._set_pending(state_message='pending')
        old = _fields.Datetime.now() - timedelta(minutes=30)
        self.env.cr.execute(
            'UPDATE payment_transaction SET create_date = %s WHERE id = %s',
            (old, tx.id),
        )
        tx.invalidate_recordset()
        empty = self._nexi_answer(tx.reference, 0)

        def fake_request(method, url, payload=None, params=None):
            if url.endswith('-m'):
                raise NexiApiError('Nexi: not found', status_code=404)
            return empty

        Tx = self.env['payment.transaction']
        with patch.object(type(self.provider), '_nexi_make_request', side_effect=fake_request):
            Tx._nexi_reconcile_pending()
        tx.invalidate_recordset()
        self.assertEqual(tx.state, 'pending')

    def test_reconcile_leftover_skips_fresh(self):
        """#9 M-A3: fresh closed-not-processed -> the cron does not touch it."""
        tx = self._new_tx()
        tx._set_done()
        tx.write({'is_post_processed': False})
        Tx = self.env['payment.transaction']
        with patch.object(type(Tx), '_post_process') as post:
            Tx._nexi_reconcile_pending()
        self.assertFalse(post.called, 'too fresh: like the first round')
        self.assertEqual(tx.nexi_reconcile_count, 0)

    def test_reconcile_leftover_alerts_at_exhaustion(self):
        """#9 M-A3: at the 72nd failed round -> error in the logs, once."""
        from datetime import timedelta
        from odoo import fields as _fields
        tx = self._new_tx()
        tx._set_done()
        tx.write({'is_post_processed': False})
        old = _fields.Datetime.now() - timedelta(minutes=30)
        self.env.cr.execute(
            'UPDATE payment_transaction SET create_date = %s, nexi_reconcile_count = 71 '
            'WHERE id = %s', (old, tx.id),
        )
        tx.invalidate_recordset()
        Tx = self.env['payment.transaction']
        with patch.object(type(Tx), '_post_process', side_effect=Exception('boom')):
            with self.assertLogs('odoo.addons.payment_nexi.models.payment_transaction',
                                 level='ERROR') as logs:
                Tx._nexi_reconcile_pending()
        self.assertEqual(tx.nexi_reconcile_count, 72)
        self.assertTrue(
            any('never post-processed' in line for line in logs.output),
            'the round that exhausts the attempts must log an error',
        )

    def test_agreeing_declines_without_time_cancel(self):
        """#9 Note: AUTH + CAPTURE DECLINED without dates -> cancel (agreeing)."""
        tx = self._new_tx()
        data = self._nexi_answer(
            tx.reference, 0,
            ('AUTHORIZATION', 'DECLINED', None),
            ('CAPTURE', 'DECLINED', None),
        )
        with patch.object(type(self.provider), '_nexi_make_request', return_value=data):
            tx._process_notification_data({})
        self.assertEqual(tx.state, 'cancel')

    def test_overpay_is_not_done(self):
        """#10 L-B10: captured more than expected -> error, not done without a ceiling."""
        tx = self._new_tx()
        data = self._nexi_answer(
            tx.reference, 2000,
            ('CAPTURE', 'EXECUTED', '2026-10-30T10:00:05'),
        )
        with patch.object(type(self.provider), '_nexi_make_request', return_value=data):
            with self.assertLogs(
                'odoo.addons.payment_nexi.models.payment_transaction',
                level='ERROR',
            ) as logs:
                tx._process_notification_data({})
        self.assertEqual(tx.state, 'error')
        self.assertFalse(tx.nexi_uncertain, 'a known overpay is not an uncertain outcome')
        self.assertTrue(
            any('captured 2000' in line and 'expected 1000' in line
                for line in logs.output),
            'the log must state captured vs expected for the manual refund',
        )

    def test_partial_capture_is_uncertain_and_reverified(self):
        """#10 M-B1: partial capture -> uncertain error, the cron recovers it."""
        from datetime import timedelta
        from odoo import fields as _fields
        tx = self._new_tx()
        short = self._nexi_answer(
            tx.reference, 600,
            ('CAPTURE', 'EXECUTED', '2026-10-30T10:00:05'),
        )
        with patch.object(type(self.provider), '_nexi_make_request', return_value=short):
            tx._process_notification_data({})
        self.assertEqual(tx.state, 'error')
        self.assertTrue(tx.nexi_uncertain)
        self.assertEqual(tx.nexi_last_captured, 600)
        old = _fields.Datetime.now() - timedelta(minutes=30)
        self.env.cr.execute(
            'UPDATE payment_transaction SET create_date = %s WHERE id = %s',
            (old, tx.id),
        )
        tx.invalidate_recordset()
        full = self._nexi_answer(
            tx.reference, 1000,
            ('CAPTURE', 'EXECUTED', '2026-10-30T10:00:05'),
        )
        Tx = self.env['payment.transaction']
        with patch.object(type(self.provider), '_nexi_make_request', return_value=full):
            with patch.object(type(Tx), '_post_process', return_value=True):
                found = Tx._nexi_reconcile_pending()
        tx.invalidate_recordset()
        self.assertGreaterEqual(found, 1)
        self.assertEqual(tx.state, 'done')
        self.assertEqual(tx.nexi_reverify_count, 1)

    def test_uncertain_error_gives_up_with_alert(self):
        """#10 M-B1: after 12 rechecks it stays error, with a log for the operator."""
        from datetime import timedelta
        from odoo import fields as _fields
        tx = self._new_tx()
        short = self._nexi_answer(
            tx.reference, 600,
            ('CAPTURE', 'EXECUTED', '2026-10-30T10:00:05'),
        )
        with patch.object(type(self.provider), '_nexi_make_request', return_value=short):
            tx._process_notification_data({})
        old = _fields.Datetime.now() - timedelta(minutes=30)
        self.env.cr.execute(
            'UPDATE payment_transaction SET create_date = %s, nexi_reverify_count = 11 '
            'WHERE id = %s', (old, tx.id),
        )
        tx.invalidate_recordset()
        Tx = self.env['payment.transaction']
        with patch.object(type(self.provider), '_nexi_make_request', return_value=short):
            with self.assertLogs(
                'odoo.addons.payment_nexi.models.payment_transaction',
                level='ERROR',
            ) as logs:
                Tx._nexi_reconcile_pending()
        tx.invalidate_recordset()
        self.assertEqual(tx.state, 'error')
        self.assertEqual(tx.nexi_reverify_count, 12)
        self.assertTrue(any('uncertain error' in line for line in logs.output))

    def test_pending_gives_up_with_alert(self):
        """#10 M-B2: the pending that leaves the loop does not vanish silently."""
        from datetime import timedelta
        from odoo import fields as _fields
        tx = self._new_tx()
        tx._set_pending(state_message='pending')
        old = _fields.Datetime.now() - timedelta(minutes=30)
        self.env.cr.execute(
            'UPDATE payment_transaction SET create_date = %s, nexi_reconcile_count = 71 '
            'WHERE id = %s', (old, tx.id),
        )
        tx.invalidate_recordset()
        data = self._nexi_answer(
            tx.reference, 0,
            ('AUTHORIZATION', 'PENDING', '2026-10-30T10:00:00'),
        )
        Tx = self.env['payment.transaction']
        with patch.object(type(self.provider), '_nexi_make_request', return_value=data):
            with self.assertLogs(
                'odoo.addons.payment_nexi.models.payment_transaction',
                level='ERROR',
            ) as logs:
                Tx._nexi_reconcile_pending()
        tx.invalidate_recordset()
        self.assertEqual(tx.state, 'pending')
        self.assertEqual(tx.nexi_reconcile_count, 72)
        self.assertTrue(any('still pending' in line for line in logs.output))

    def test_malformed_gateway_payload_does_not_raise(self):
        """#10 L-B1: orderStatus/operations di tipo strano -> pending, no 500."""
        tx = self._new_tx()
        data = {
            'orderId': tx.reference,
            'orderStatus': 'rotto',
            'operations': 'rotto',
        }
        with patch.object(type(self.provider), '_nexi_make_request', return_value=data):
            tx._process_notification_data({})
        self.assertEqual(tx.state, 'pending')

    def test_4xx_marks_the_error_as_uncertain(self):
        """#10 M-B1: a 4xx is not proof that the payment failed."""
        from odoo.addons.payment_nexi.models.payment_provider import NexiApiError
        tx = self._new_tx()
        err = NexiApiError('Nexi: request refused', status_code=400)
        with patch.object(type(self.provider), '_nexi_make_request', side_effect=err):
            tx._process_notification_data({})
        self.assertEqual(tx.state, 'error')
        self.assertTrue(tx.nexi_uncertain)

    def test_copy_does_not_clone_nexi_secrets(self):
        """#10 L-B8: copying a transaction does not carry the tok and session."""
        tx = self._new_tx()
        tx.sudo().write({
            'nexi_contract_id': 'NCONTRACT1',
            'nexi_build_session': 'SESSION1',
            'nexi_build_token': 'TOKEN1',
            'nexi_masked_pan': '****1234',
            'nexi_reconcile_count': 3,
            'nexi_reverify_count': 2,
            'nexi_uncertain': True,
        })
        clone = tx.copy({'reference': tx.reference + 'COPY'})
        clone.invalidate_recordset()
        self.assertNotEqual(clone.nexi_access_token, tx.nexi_access_token)
        self.assertFalse(clone.nexi_contract_id)
        self.assertFalse(clone.nexi_build_session)
        self.assertFalse(clone.nexi_build_token)
        self.assertFalse(clone.nexi_masked_pan)
        self.assertEqual(clone.nexi_reconcile_count, 0)
        self.assertEqual(clone.nexi_reverify_count, 0)
        self.assertFalse(clone.nexi_uncertain)

    def test_configured_public_base_wins_over_candidate(self):
        """#10 L-B6: the configured base wins over a candidate base_url."""
        tx = self._new_tx()
        self.provider.nexi_public_base = 'https://shop.example.com'
        answer = {
            'hostedPage': 'https://xpay.nexigroup.com/pay',
            'securityToken': 't',
        }
        with patch.object(
            type(self.provider), '_nexi_make_request', return_value=answer
        ) as req:
            tx._get_specific_rendering_values({
                'amount': 10.0, 'currency_id': tx.currency_id.id,
                'partner_id': self.partner.id, 'reference': tx.reference,
                'should_tokenize': False,
                'base_url': 'https://evil.example',
            })
        payload = req.call_args.kwargs.get('payload') or req.call_args.args[2]
        result_url = payload['paymentSession']['resultUrl']
        self.assertTrue(result_url.startswith('https://shop.example.com/'))
        self.assertNotIn('evil.example', result_url)

    def test_prepare_rechecks_cart_after_lock(self):
        """#10 L-B5: the cart verdict is recomputed after the lock."""
        from odoo.addons.payment_nexi.controllers import main as nexi_main
        from odoo.addons.payment_nexi.controllers.main import NexiController
        nexi_main._NEXI_RL.clear()
        self.provider.nexi_mode = 'build'
        eur = self.currency_eur or self.provider.company_id.currency_id
        pricelist = self.env['product.pricelist'].create({
            'name': 'Nexi EUR smoke', 'currency_id': eur.id,
        })
        order = self.env['sale.order'].create({
            'partner_id': self.partner.id,
            'company_id': self.provider.company_id.id,
            'pricelist_id': pricelist.id,
        })
        class FakeWebsite:
            id = None
            domain = ''
            def sale_get_order(self):
                return order
        class FakeRequest:
            env = self.env
            website = FakeWebsite()
            httprequest = None
        controller = NexiController()
        endpoint = NexiController.nexi_build_prepare.original_endpoint
        with patch.object(nexi_main, 'request', FakeRequest()):
            with patch.object(nexi_main, '_nexi_csrf_ok', return_value=True):
                with patch.object(
                    NexiController, '_nexi_order_ready',
                    side_effect=[None, 'Order already paid.'],
                ) as check:
                    res = endpoint(controller, method_code='card')
        self.assertEqual(res, {'error': 'Order already paid.'})
        self.assertEqual(check.call_count, 2, 'before and after the lock')

    def test_json_csrf_requires_a_valid_token(self):
        """#10 L-B9: Odoo 18 ignores csrf=True on JSON, the route validates it."""
        from odoo.addons.payment_nexi.controllers import main as nexi_main
        class FakeRequest:
            def __init__(self, token, valid):
                self.params = {'csrf_token': token}
                self._valid = valid
            def validate_csrf(self, token):
                return self._valid and token == 'good'
        with patch.object(nexi_main, 'request', FakeRequest('good', True)):
            self.assertTrue(nexi_main._nexi_csrf_ok())
        with patch.object(nexi_main, 'request', FakeRequest('bad', False)):
            self.assertFalse(nexi_main._nexi_csrf_ok())
        with patch.object(nexi_main, 'request', FakeRequest(None, True)):
            self.assertFalse(nexi_main._nexi_csrf_ok())

    def test_json_write_routes_require_csrf(self):
        """#10 L-B9: the JSON routes that write are no longer CSRF-exempt."""
        from odoo.addons.payment_nexi.controllers.main import NexiController
        for name in ('nexi_saved_card', 'nexi_build_prepare',
                     'nexi_build_jserror', 'nexi_build_finalize'):
            with self.subTest(route=name):
                routing = getattr(NexiController, name).original_routing
                self.assertIs(routing.get('csrf'), True)
        self.assertIs(NexiController.nexi_confirm.original_routing.get('csrf'), False)

    # --- #11: installation, reconciliation, Build redirect, provider order,
    # --- saved-card choice.

    def test_post_init_links_inactive_core_methods(self):
        """#11 P1: on a clean DB the core methods are inactive; the hook must link them."""
        from odoo.addons.payment_nexi import post_init_hook
        Method = self.env['payment.method'].with_context(active_test=False)
        paypal = Method.search([('code', '=', 'paypal')], limit=1)
        mybank = Method.search([('code', '=', 'mybank')], limit=1)
        self.assertTrue(paypal and mybank, 'the core methods must exist')
        # Provider off: the hook must not go through _activate_default_pms.
        self.provider.write({
            'state': 'disabled',
            'payment_method_ids': [(3, paypal.id), (3, mybank.id)],
        })
        (paypal | mybank).write({'active': False})
        post_init_hook(self.env)
        self.provider.invalidate_recordset()
        linked = self.provider.with_context(active_test=False).payment_method_ids
        self.assertIn(paypal.id, linked.ids, 'inactive paypal must be linked')
        self.assertIn(mybank.id, linked.ids, 'inactive mybank must be linked')

    def test_reconcile_draft_with_order_started(self):
        """#11 P1: return and webhook lost -> the draft with an order must be reconciled."""
        from datetime import timedelta
        from odoo import fields as _fields
        tx = self._new_tx()
        self.assertEqual(tx.state, 'draft')
        tx.sudo().write({'nexi_order_started': True})
        old = _fields.Datetime.now() - timedelta(minutes=30)
        self.env.cr.execute(
            'UPDATE payment_transaction SET create_date = %s WHERE id = %s',
            (old, tx.id),
        )
        tx.invalidate_recordset()
        data = self._nexi_answer(
            tx.reference, 1000,
            ('CAPTURE', 'EXECUTED', '2026-10-30T10:00:05'),
        )
        Tx = self.env['payment.transaction']
        with patch.object(type(self.provider), '_nexi_make_request', return_value=data):
            with patch.object(type(Tx), '_post_process', return_value=True):
                found = Tx._nexi_reconcile_pending()
        tx.invalidate_recordset()
        self.assertEqual(found, 1)
        self.assertEqual(tx.state, 'done')
        self.assertEqual(tx.nexi_reconcile_count, 1)

    def test_reconcile_skips_draft_never_sent_to_gateway(self):
        """#11 P1: a draft without an order on Nexi must not be probed."""
        from datetime import timedelta
        from odoo import fields as _fields
        tx = self._new_tx()
        old = _fields.Datetime.now() - timedelta(minutes=30)
        self.env.cr.execute(
            'UPDATE payment_transaction SET create_date = %s WHERE id = %s',
            (old, tx.id),
        )
        tx.invalidate_recordset()
        Tx = self.env['payment.transaction']
        with patch.object(type(self.provider), '_nexi_make_request') as req:
            found = Tx._nexi_reconcile_pending()
        self.assertEqual(found, 0)
        self.assertFalse(req.called, 'cart never started: Nexi is not called')
        self.assertEqual(tx.nexi_reconcile_count, 0)

    def test_reconcile_pending_already_post_processed(self):
        """#11 P1: is_post_processed does not prove a final outcome."""
        from datetime import timedelta
        from odoo import fields as _fields
        tx = self._new_tx()
        tx._set_pending(state_message='pending')
        tx.write({'is_post_processed': True})
        old = _fields.Datetime.now() - timedelta(minutes=30)
        self.env.cr.execute(
            'UPDATE payment_transaction SET create_date = %s WHERE id = %s',
            (old, tx.id),
        )
        tx.invalidate_recordset()
        data = self._nexi_answer(
            tx.reference, 1000,
            ('CAPTURE', 'EXECUTED', '2026-10-30T10:00:05'),
        )
        Tx = self.env['payment.transaction']
        with patch.object(type(self.provider), '_nexi_make_request', return_value=data):
            with patch.object(type(Tx), '_post_process', return_value=True):
                found = Tx._nexi_reconcile_pending()
        tx.invalidate_recordset()
        self.assertEqual(found, 1)
        self.assertEqual(tx.state, 'done')

    def test_order_creation_marks_started(self):
        """#11 P1: the order created on Nexi leaves the signal for the cron."""
        tx = self._new_tx()
        answer = {
            'hostedPage': 'https://xpay.nexigroup.com/pay?x=1',
            'securityToken': 't',
        }
        with patch.object(
            type(self.provider), '_nexi_make_request', return_value=answer
        ):
            tx._get_specific_rendering_values({
                'amount': 10.0, 'currency_id': tx.currency_id.id,
                'partner_id': self.partner.id, 'reference': tx.reference,
                'should_tokenize': False, 'base_url': 'https://shop.example.com',
            })
        tx.invalidate_recordset()
        self.assertTrue(tx.nexi_order_started)

    def test_build_redirect_renders_a_submittable_form(self):
        """#11 P2: _processRedirectFlow looks for a <form>: the Build must have one."""
        from lxml import html as lxml_html
        self.provider.nexi_mode = 'build'
        tx = self._new_tx()
        build_answer = {
            'sessionId': 'S1', 'securityToken': 'T1', 'fields': [],
            'url': 'https://xpay.nexigroup.com/build', 'state': 'PAYMENT_STARTED',
        }
        with patch.object(
            type(tx), '_nexi_post_order', return_value=build_answer
        ):
            values = tx._get_specific_rendering_values({
                'amount': 10.0, 'currency_id': tx.currency_id.id,
                'partner_id': self.partner.id, 'reference': tx.reference,
                'should_tokenize': False, 'base_url': 'https://shop.example.com',
            })
        form_html = self.env['ir.qweb']._render(
            self.env.ref('payment_nexi.redirect_build').id, values)
        doc = lxml_html.fromstring(form_html)
        form = doc if doc.tag == 'form' else doc.find('.//form')
        self.assertIsNotNone(form, 'the Build redirect must contain a form')
        self.assertEqual(form.get('method').upper(), 'GET')
        self.assertEqual(
            form.get('action'), '/payment/nexi/build/%s' % tx.reference)
        tok = form.find('.//input[@name="tok"]')
        self.assertIsNotNone(tok)
        self.assertEqual(tok.get('value'), tx.nexi_access_token)

    def test_method_row_uses_template_provider_order(self):
        """#11 P2: the row follows the provider _order, not the lowest id."""
        first = self.provider                        # lowest id
        second = first.copy({
            'name': 'Nexi second', 'state': 'test', 'sequence': 1,
        })
        first.write({'sequence': 50})
        Method = self.env['payment.method']
        row = self.method_card.provider_ids.filtered(
            lambda p: p in (first | second))[:1]
        self.assertEqual(row, second, 'the row must use sequence, not the id')
        Availability = self.env['payment.nexi.method.availability']
        Availability.create({
            'provider_id': second.id, 'method_id': self.method_card.id,
            'disabled': True,
        })
        both = Method._get_compatible_payment_methods(
            [first.id, second.id], self.partner.id)
        self.assertNotIn(
            self.method_card, both,
            'the row terminal refuses the card: it must not appear')
        Availability.search([
            ('provider_id', '=', second.id),
            ('method_id', '=', self.method_card.id),
        ]).unlink()
        Availability.create({
            'provider_id': first.id, 'method_id': self.method_card.id,
            'disabled': True,
        })
        both = Method._get_compatible_payment_methods(
            [first.id, second.id], self.partner.id)
        self.assertIn(
            self.method_card, both,
            'the row uses the second terminal, which accepts the card')

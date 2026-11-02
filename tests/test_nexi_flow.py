# -*- coding: utf-8 -*-
"""Test Odoo del modulo: girano con --test-enable.

    odoo -d <db> -i payment_nexi --test-enable --stop-after-init

Coprono le due cose che in cassa si rompono davvero: l'esito del pagamento
quando Nexi manda anche il CAPTURE, e i metodi nascosti per singolo terminale.
"""
from unittest.mock import patch

from odoo.exceptions import ValidationError
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
        cls.method_card = cls.env['payment.method'].with_context(active_test=False).search([('code', '=', 'card')], limit=1)
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
        # Il provider di prova nasce già abilitato (create, non write): il
        # core accende i metodi solo al passaggio di stato, quindi qui serve.
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

    def test_capture_without_any_captured_amount_is_refused(self):
        """CAPTURE eseguito ma nessun importo da nessuna parte: fail-closed."""
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
        """Formato decimale (euro): si converte, non si rifiuta."""
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
        self.assertEqual(tx.state, 'done', '10.00 euro = 1000 centesimi')

    def test_capture_summing_operations_closes_payment(self):
        """Due incassi parziali che coprono la cifra: chiude."""
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
        # L'operazione che decide (l'ultima) non porta l'importo: deve
        # valere la somma degli incassi, non un falso "non completo".
        data['operations'][2].pop('operationAmount', None)
        data['operations'][2].pop('capturedAmount', None)
        with patch.object(type(self.provider), '_nexi_make_request', return_value=data):
            tx._process_notification_data({})
        self.assertEqual(tx.state, 'done')

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

    def test_method_follows_the_terminal_the_row_will_use(self):
        """La riga usa il PRIMO provider compatibile: se quello rifiuta il
        metodo, il metodo non deve comparire (la riga fallirebbe)."""
        first = self.provider                      # id più basso: è lui la riga
        second = self.provider.copy({'name': 'Nexi secondo'})
        self.env['payment.nexi.method.availability'].create({
            'provider_id': first.id, 'method_id': self.method_card.id, 'disabled': True,
        })
        Method = self.env['payment.method']
        both = Method._get_compatible_payment_methods([first.id, second.id], self.partner.id)
        self.assertNotIn(self.method_card, both, 'il terminale della riga rifiuta il metodo')
        # Rifiutato solo dal secondo: la riga usa il primo, che lo accetta.
        self.env['payment.nexi.method.availability'].search([
            ('provider_id', '=', first.id), ('method_id', '=', self.method_card.id),
        ]).unlink()
        self.env['payment.nexi.method.availability'].create({
            'provider_id': second.id, 'method_id': self.method_card.id, 'disabled': True,
        })
        both = Method._get_compatible_payment_methods([first.id, second.id], self.partner.id)
        self.assertIn(self.method_card, both)

    def test_reference_longer_than_nexi_limit_is_refused(self):
        """Nexi accetta orderId fino a 27 caratteri: meglio un errore chiaro."""
        tx = self.env['payment.transaction'].create({
            'provider_id': self.provider.id,
            'payment_method_id': self.method_card.id,
            'reference': 'RIFERIMENTO-MOLTO-LUNGO-0123456789',
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
            self.assertFalse(post.called, 'non deve nemmeno chiamare Nexi')

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

    def test_reconcile_closes_stuck_payment(self):
        """Cron: pending vecchia + incasso su Nexi -> done, senza browser."""
        from datetime import timedelta
        from odoo import fields as _fields
        tx = self._new_tx()
        tx._set_pending(state_message='in attesa')
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
        """Cron: pending appena creata -> non toccata, contatore fermo."""
        tx = self._new_tx()
        tx._set_pending(state_message='in attesa')
        Tx = self.env['payment.transaction']
        with patch.object(type(self.provider), '_nexi_make_request') as req:
            found = Tx._nexi_reconcile_pending()
        self.assertEqual(found, 0)
        self.assertFalse(req.called, 'troppo fresca: Nexi non si chiama')
        self.assertEqual(tx.nexi_reconcile_count, 0)

    def test_reconcile_counts_attempts_and_gives_up(self):
        """Cron: sempre PENDING -> resta pending, dopo 72 giri esce dal giro."""
        from datetime import timedelta
        from odoo import fields as _fields
        tx = self._new_tx()
        tx._set_pending(state_message='in attesa')
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
        self.assertFalse(req.called, 'oltre il limite: Nexi non si chiama')

    def test_no_duplicate_method_codes(self):
        """#7 punto 3: un codice = un metodo (nessun doppione nostro)."""
        Method = self.env['payment.method'].with_context(active_test=False)
        for code in ('card', 'paypal', 'mybank', 'visa', 'mastercard', 'amex'):
            with self.subTest(code=code):
                self.assertEqual(
                    Method.search_count([('code', '=', code)]), 1,
                    'il codice %s deve esistere una volta sola' % code,
                )

    def test_migration_moves_duplicate_to_core(self):
        """#7 punto 3: la migrazione riaggancia e cancella, due volte di fila."""
        Method = self.env['payment.method'].with_context(active_test=False)
        core = Method.search([('code', '=', 'card')], limit=1)
        dup = Method.create({
            'name': 'Carta duplicata', 'code': 'card', 'active': True,
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
        self.assertFalse(dup.exists(), 'il duplicato va cancellato')
        tx.invalidate_recordset()
        self.assertEqual(tx.payment_method_id, core)
        self.assertIn(core, self.provider.payment_method_ids)
        # Seconda esecuzione: a vuoto, senza errori e senza cambi.
        migrate_mod.migrate(self.env.cr, '1.4.0')
        self.assertEqual(tx.payment_method_id, core)

    def _migration_script(self):
        import os
        from odoo.modules import get_module_path
        return os.path.join(
            get_module_path('payment_nexi'), 'migrations', '1.4.0', 'post-migrate.py')

    def test_disagreeing_operations_without_time_stay_pending(self):
        """#8 H1: AUTH pagata + CAPTURE KO senza date -> pending, mai done."""
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
        """#8 H2: timeout/5xx di Nexi -> pending (il cron riverifica)."""
        from odoo.addons.payment_nexi.models.payment_provider import (
            NexiApiError, NexiUnavailable,
        )
        tx = self._new_tx()
        with patch.object(
            type(self.provider), '_nexi_make_request', side_effect=NexiUnavailable('giù')
        ):
            tx._process_notification_data({})
        self.assertEqual(tx.state, 'pending')
        tx2 = self._new_tx()
        err = NexiApiError('Nexi: errore', status_code=503)
        with patch.object(
            type(self.provider), '_nexi_make_request', side_effect=err
        ):
            tx2._process_notification_data({})
        self.assertEqual(tx2.state, 'pending')

    def test_missing_currency_is_refused(self):
        """#8 M2: EXECUTED senza valuta -> error, mai done."""
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
        """#8 M1: operazione 1000 ma totale ordine 600 -> error."""
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
        """#8 M6: nel path request una sola GET, niente sleep."""
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
        """#8 M3: done non post-elaborata -> il cron ritenta _post_process."""
        tx = self._new_tx()
        tx._set_done()
        tx.write({'is_post_processed': False})
        Tx = self.env['payment.transaction']
        with patch.object(type(Tx), '_post_process') as post:
            found = Tx._nexi_reconcile_pending()
        self.assertTrue(post.called)
        self.assertGreaterEqual(found, 1)

    def test_hpp_token_is_encoded_not_concatenated(self):
        """#8 M5: securityToken con '&' non diventa un parametro in più."""
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

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
            {'card', 'googlepay', 'applepay', 'bancomatpay', 'mybank', 'paypal',
             'paypal_bnpl', 'visa', 'mastercard', 'amex'},
        )

    def test_card_brand_icons_are_active_and_linked(self):
        """I brand core devono essere accesi, altrimenti la riga Carta perde le icone."""
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
            'la riga Carta deve mostrare i tre marchi per primi',
        )
        # Le icone sono file del modulo, non dipendono dal filestore del test.
        import os
        from odoo.modules import get_module_path
        base = os.path.join(get_module_path('payment_nexi'), 'static', 'src', 'img')
        for code in ('visa', 'mastercard', 'amex'):
            self.assertTrue(
                os.path.isfile(os.path.join(base, code + '.png')),
                'manca l\'icona %s' % code,
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

    def _migration_script(self, version='1.4.0'):
        import os
        from odoo.modules import get_module_path
        return os.path.join(
            get_module_path('payment_nexi'), 'migrations', version, 'post-migrate.py')

    def test_migration_restores_brand_icons_and_links(self):
        """#10 regressione: la 1.4.4 riaccende e riaggancia i brand core."""
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
        # Seconda esecuzione: a vuoto, senza errori.
        migrate_mod.migrate(self.env.cr, '1.4.4')

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
        from datetime import timedelta
        from odoo import fields as _fields
        tx = self._new_tx()
        tx._set_done()
        tx.write({'is_post_processed': False})
        # #9 M-A3: il secondo passaggio ha lo stesso cutoff del primo.
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

    def test_hpp_embedded_token_is_replaced(self):
        """#9 M-A1: securityToken dentro hostedPage non vince su quello vero."""
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
        """#9 H-A1: ordine carte vuoto + ordine bottoni -m pagato -> done."""
        from datetime import timedelta
        from odoo import fields as _fields
        self.provider.nexi_mode = 'build'
        tx = self._new_tx()
        tx._set_pending(state_message='in attesa')
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
        """#9 H-A1: ordine -m assente -> la pending resta pending, mai error."""
        from datetime import timedelta
        from odoo import fields as _fields
        from odoo.addons.payment_nexi.models.payment_provider import NexiApiError
        self.provider.nexi_mode = 'build'
        tx = self._new_tx()
        tx._set_pending(state_message='in attesa')
        old = _fields.Datetime.now() - timedelta(minutes=30)
        self.env.cr.execute(
            'UPDATE payment_transaction SET create_date = %s WHERE id = %s',
            (old, tx.id),
        )
        tx.invalidate_recordset()
        empty = self._nexi_answer(tx.reference, 0)

        def fake_request(method, url, payload=None, params=None):
            if url.endswith('-m'):
                raise NexiApiError('Nexi: non trovato', status_code=404)
            return empty

        Tx = self.env['payment.transaction']
        with patch.object(type(self.provider), '_nexi_make_request', side_effect=fake_request):
            Tx._nexi_reconcile_pending()
        tx.invalidate_recordset()
        self.assertEqual(tx.state, 'pending')

    def test_reconcile_leftover_skips_fresh(self):
        """#9 M-A3: chiusa-non-processata fresca -> il cron non la tocca."""
        tx = self._new_tx()
        tx._set_done()
        tx.write({'is_post_processed': False})
        Tx = self.env['payment.transaction']
        with patch.object(type(Tx), '_post_process') as post:
            Tx._nexi_reconcile_pending()
        self.assertFalse(post.called, 'troppo fresca: come il primo giro')
        self.assertEqual(tx.nexi_reconcile_count, 0)

    def test_reconcile_leftover_alerts_at_exhaustion(self):
        """#9 M-A3: al 72esimo giro fallito -> errore nei log, una volta."""
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
            any('mai post-elaborata' in line for line in logs.output),
            'il giro che esaurisce i tentativi deve loggare error',
        )

    def test_agreeing_declines_without_time_cancel(self):
        """#9 Nota: AUTH + CAPTURE DECLINED senza date -> cancel (concordi)."""
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
        """#10 L-B10: incassato più dell'atteso -> error, non done senza tetto."""
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
        self.assertFalse(tx.nexi_uncertain, 'un overpay noto non è un esito incerto')
        self.assertTrue(
            any('incassato 2000' in line and 'atteso 1000' in line
                for line in logs.output),
            'il log deve dire incassato vs atteso per il rimborso manuale',
        )

    def test_partial_capture_is_uncertain_and_reverified(self):
        """#10 M-B1: incasso parziale -> error incerto, il cron lo recupera."""
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
        """#10 M-B1: dopo 12 riverifiche resta error, con un log per l'operatore."""
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
        self.assertTrue(any('errore incerto' in line for line in logs.output))

    def test_pending_gives_up_with_alert(self):
        """#10 M-B2: la pending che esce dal giro non sparisce in silenzio."""
        from datetime import timedelta
        from odoo import fields as _fields
        tx = self._new_tx()
        tx._set_pending(state_message='in attesa')
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
        self.assertTrue(any('ancora pending' in line for line in logs.output))

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
        """#10 M-B1: un 4xx non è la prova che il pagamento sia fallito."""
        from odoo.addons.payment_nexi.models.payment_provider import NexiApiError
        tx = self._new_tx()
        err = NexiApiError('Nexi: richiesta rifiutata', status_code=400)
        with patch.object(type(self.provider), '_nexi_make_request', side_effect=err):
            tx._process_notification_data({})
        self.assertEqual(tx.state, 'error')
        self.assertTrue(tx.nexi_uncertain)

    def test_copy_does_not_clone_nexi_secrets(self):
        """#10 L-B8: duplicare una transazione non porta con sé tok e sessione."""
        tx = self._new_tx()
        tx.sudo().write({
            'nexi_contract_id': 'NCONTRATTO1',
            'nexi_build_session': 'SESSIONE1',
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
        """#10 L-B6: la base configurata vince su un base_url candidato."""
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
        """#10 L-B5: il verdetto carrello viene ricalcolato dopo il lock."""
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
                    side_effect=[None, 'Ordine già pagato.'],
                ) as check:
                    res = endpoint(controller, method_code='card')
        self.assertEqual(res, {'error': 'Ordine già pagato.'})
        self.assertEqual(check.call_count, 2, 'prima e dopo il lock')

    def test_json_csrf_requires_a_valid_token(self):
        """#10 L-B9: Odoo 18 ignora csrf=True sui JSON, la rotta lo valida."""
        from odoo.addons.payment_nexi.controllers import main as nexi_main
        class FakeRequest:
            def __init__(self, token, valid):
                self.params = {'csrf_token': token}
                self._valid = valid
            def validate_csrf(self, token):
                return self._valid and token == 'buono'
        with patch.object(nexi_main, 'request', FakeRequest('buono', True)):
            self.assertTrue(nexi_main._nexi_csrf_ok())
        with patch.object(nexi_main, 'request', FakeRequest('cattivo', False)):
            self.assertFalse(nexi_main._nexi_csrf_ok())
        with patch.object(nexi_main, 'request', FakeRequest(None, True)):
            self.assertFalse(nexi_main._nexi_csrf_ok())

    def test_json_write_routes_require_csrf(self):
        """#10 L-B9: le rotte JSON che scrivono non sono più esenti da CSRF."""
        from odoo.addons.payment_nexi.controllers.main import NexiController
        for name in ('nexi_saved_card', 'nexi_build_prepare',
                     'nexi_build_jserror', 'nexi_build_finalize'):
            with self.subTest(route=name):
                routing = getattr(NexiController, name).original_routing
                self.assertIs(routing.get('csrf'), True)
        self.assertIs(NexiController.nexi_confirm.original_routing.get('csrf'), False)

    # --- #11: installazione, riconciliazione, redirect Build, ordine provider,
    # --- scelta carta salvata.

    def test_post_init_links_inactive_core_methods(self):
        """#11 P1: su DB pulito i metodi core sono inattivi; l'hook deve linkarli."""
        from odoo.addons.payment_nexi import post_init_hook
        Method = self.env['payment.method'].with_context(active_test=False)
        paypal = Method.search([('code', '=', 'paypal')], limit=1)
        mybank = Method.search([('code', '=', 'mybank')], limit=1)
        self.assertTrue(paypal and mybank, 'i metodi core devono esistere')
        # Provider spento: l'hook non deve passare da _activate_default_pms.
        self.provider.write({
            'state': 'disabled',
            'payment_method_ids': [(3, paypal.id), (3, mybank.id)],
        })
        (paypal | mybank).write({'active': False})
        post_init_hook(self.env)
        self.provider.invalidate_recordset()
        linked = self.provider.with_context(active_test=False).payment_method_ids
        self.assertIn(paypal.id, linked.ids, 'paypal inattivo deve essere collegato')
        self.assertIn(mybank.id, linked.ids, 'mybank inattivo deve essere collegato')

    def test_reconcile_draft_with_order_started(self):
        """#11 P1: ritorno e webhook persi -> la draft con ordine va riconciliata."""
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
        """#11 P1: una draft senza ordine su Nexi non va sondata."""
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
        self.assertFalse(req.called, 'carrello mai partito: Nexi non si chiama')
        self.assertEqual(tx.nexi_reconcile_count, 0)

    def test_reconcile_pending_already_post_processed(self):
        """#11 P1: is_post_processed non prova l'esito definitivo."""
        from datetime import timedelta
        from odoo import fields as _fields
        tx = self._new_tx()
        tx._set_pending(state_message='in attesa')
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
        """#11 P1: l'ordine creato su Nexi lascia il segnale per il cron."""
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
        """#11 P2: _processRedirectFlow cerca un <form>: la Build deve averlo."""
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
        self.assertIsNotNone(form, 'il redirect Build deve contenere un form')
        self.assertEqual(form.get('method').upper(), 'GET')
        self.assertEqual(
            form.get('action'), '/payment/nexi/build/%s' % tx.reference)
        tok = form.find('.//input[@name="tok"]')
        self.assertIsNotNone(tok)
        self.assertEqual(tok.get('value'), tx.nexi_access_token)

    def test_method_row_uses_template_provider_order(self):
        """#11 P2: la riga segue _order dei provider, non l'id minimo."""
        first = self.provider                        # id più basso
        second = first.copy({
            'name': 'Nexi secondo', 'state': 'test', 'sequence': 1,
        })
        first.write({'sequence': 50})
        Method = self.env['payment.method']
        row = self.method_card.provider_ids.filtered(
            lambda p: p in (first | second))[:1]
        self.assertEqual(row, second, 'la riga deve usare sequence, non l\'id')
        Availability = self.env['payment.nexi.method.availability']
        Availability.create({
            'provider_id': second.id, 'method_id': self.method_card.id,
            'disabled': True,
        })
        both = Method._get_compatible_payment_methods(
            [first.id, second.id], self.partner.id)
        self.assertNotIn(
            self.method_card, both,
            'il terminale della riga rifiuta la carta: non deve comparire')
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
            'la riga usa il secondo terminale, che la carta la accetta')

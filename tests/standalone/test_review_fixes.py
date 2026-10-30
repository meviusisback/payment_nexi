# -*- coding: utf-8 -*-
"""Test delle correzioni della review #1, senza server Odoo.

Girano con un finto modulo `odoo` (solo le poche cose che il modulo usa), così
si possono lanciare su una macchina qualsiasi:

    python3 -m unittest discover -s tests/standalone -t .

Provano la logica pura: domini Nexi, classificazione della verifica metodi,
scelta dell'operazione che decide l'esito, limiti di frequenza, sessione.
La parte che parla con Nexi e con il database va provata in cassa.
"""
import os
import pathlib
import sys
import tempfile
import textwrap
import unittest

ADDON_ROOT = pathlib.Path(__file__).resolve().parents[2]


def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text), encoding='utf-8')


def _build_stubs(base):
    """Finto pacchetto `odoo`: classi e campi che non fanno niente."""
    odoo = base / 'odoo'
    _write(odoo / '__init__.py', '''
        SUPERUSER_ID = 1


        def _(msg, *args, **kwargs):
            return (msg % args) if args else msg


        from . import api, exceptions, fields, models, http  # noqa: E402
    ''')
    _write(odoo / 'api.py', '''
        def model(fn):
            return fn


        def model_create_multi(fn):
            return fn


        def depends(*args, **kwargs):
            def deco(fn):
                return fn
            return deco


        def constrains(*args, **kwargs):
            def deco(fn):
                return fn
            return deco


        def onchange(*args, **kwargs):
            def deco(fn):
                return fn
            return deco
    ''')
    _write(odoo / 'exceptions.py', '''
        class UserError(Exception):
            pass


        class ValidationError(Exception):
            pass


        class AccessError(Exception):
            pass
    ''')
    _write(odoo / 'fields.py', '''
        class _Field:
            def __init__(self, *args, **kwargs):
                self.args = args
                self.kwargs = kwargs


        class Boolean(_Field):
            pass


        class Char(_Field):
            pass


        class Text(_Field):
            pass


        class Integer(_Field):
            pass


        class Datetime(_Field):
            pass


        class Selection(_Field):
            pass


        class Many2one(_Field):
            pass


        class One2many(_Field):
            pass


        class Many2many(_Field):
            pass


        def Command(*args):
            return args
    ''')
    _write(odoo / 'models.py', '''
        class BaseModel:
            _name = None
            _description = None


        class Model(BaseModel):
            pass


        class AbstractModel(BaseModel):
            pass


        class TransientModel(BaseModel):
            pass
    ''')
    _write(odoo / 'http.py', '''
        class Controller:
            pass


        def route(*args, **kwargs):
            def deco(fn):
                return fn
            return deco


        class _Request:
            """Richiesta finta: i test scrivono in `session` quando serve."""

            def __init__(self):
                self.session = {}
                self.httprequest = None


        request = _Request()
    ''')


def _install_addon():
    """Rende importabile l'addon come pacchetto `payment_nexi`."""
    if 'payment_nexi' not in sys.modules:
        tmp = pathlib.Path(tempfile.mkdtemp(prefix='nexi_stub_'))
        _build_stubs(tmp)
        os.symlink(str(ADDON_ROOT), str(tmp / 'payment_nexi'))
        sys.path.insert(0, str(tmp))
        import payment_nexi  # noqa: F401
    from odoo import http as odoo_http
    from payment_nexi.controllers import main as controller
    from payment_nexi.models import payment_provider, payment_transaction
    return odoo_http, controller, payment_provider, payment_transaction


ODOO_HTTP, CONTROLLER, PROVIDER, TRANSACTION = _install_addon()


class NexiHostsTest(unittest.TestCase):
    """#1: il dominio di produzione deve passare, il resto no."""

    def test_production_host_is_accepted(self):
        self.assertTrue(PROVIDER.is_nexi_url('https://xpay.nexigroup.com/orders/hpp'))

    def test_sandbox_host_is_accepted(self):
        self.assertTrue(PROVIDER.is_nexi_url('https://xpaysandbox.nexigroup.com/orders/hpp'))

    def test_http_is_refused(self):
        self.assertFalse(PROVIDER.is_nexi_url('http://xpay.nexigroup.com/orders/hpp'))

    def test_lookalike_host_is_refused(self):
        self.assertFalse(PROVIDER.is_nexi_url('https://xpay.nexigroup.com.evil.example/orders/hpp'))

    def test_other_host_is_refused(self):
        self.assertFalse(PROVIDER.is_nexi_url('https://example.com/orders/hpp'))

    def test_empty_is_refused(self):
        self.assertFalse(PROVIDER.is_nexi_url(''))
        self.assertFalse(PROVIDER.is_nexi_url(None))


class CheckServiceTest(unittest.TestCase):
    """#8: un timeout non deve spegnere un metodo che funziona."""

    def _provider(self, exc):
        class FakeProvider(PROVIDER.PaymentProviderNexi):
            def _nexi_make_request(self, *args, **kwargs):
                raise exc
        return FakeProvider()

    def test_timeout_is_not_verified(self):
        enabled, detail = self._provider(
            PROVIDER.NexiUnavailable('Nexi non raggiungibile. Riprova.')
        )._nexi_check_service('https://xpay.nexigroup.com/orders/hpp', 'CARDS')
        self.assertIsNone(enabled, 'un timeout non deve disabilitare il metodo')
        self.assertIn('non verificato', detail)

    def test_service_not_enabled_is_a_rejection(self):
        enabled, detail = self._provider(
            PROVIDER.ValidationError('Nexi: Payment service not enabled [PS0064]')
        )._nexi_check_service('https://xpay.nexigroup.com/orders/hpp', 'CARDS')
        self.assertIs(enabled, False)
        self.assertIn('rifiutato', detail)

    def test_unknown_error_is_not_a_rejection(self):
        enabled, detail = self._provider(
            PROVIDER.ValidationError('Nexi: Internal error [GW9999]')
        )._nexi_check_service('https://xpay.nexigroup.com/orders/hpp', 'CARDS')
        self.assertIsNone(enabled, 'un errore ignoto non deve spegnere il metodo')
        self.assertIn('errore', detail)

    def test_working_service_is_enabled(self):
        class OkProvider(PROVIDER.PaymentProviderNexi):
            def _nexi_make_request(self, *args, **kwargs):
                return {'orderId': 'x'}
        enabled, detail = OkProvider()._nexi_check_service(
            'https://xpay.nexigroup.com/orders/hpp', 'CARDS'
        )
        self.assertIs(enabled, True)
        self.assertIn('attivo', detail)

    def test_bnpl_check_amount_is_compatible(self):
        # Sotto i 30 € Nexi rifiuta Paga in 3: la verifica direbbe "non attivo"
        # anche su un terminale che lo ha attivo.
        self.assertGreaterEqual(int(PROVIDER.CHECK_AMOUNTS['PAYPAL_PAGAIN3']), 3000)

    def test_other_services_use_the_default_amount(self):
        self.assertNotIn('CARDS', PROVIDER.CHECK_AMOUNTS)


class DecideOperationTest(unittest.TestCase):
    """#2: con l'incasso automatico l'esito sta sull'ultima operazione."""

    def _data(self, *ops):
        return {'operations': [
            dict(operationType=t, operationResult=r, operationTime=ts,
                 operationAmount='1000', operationCurrency='EUR')
            for t, r, ts in ops
        ]}

    def test_capture_after_authorization_wins(self):
        data = self._data(
            ('AUTHORIZATION', 'EXECUTED', '2026-10-30T10:00:00'),
            ('CAPTURE', 'EXECUTED', '2026-10-30T10:00:05'),
        )
        op, kind = TRANSACTION._nexi_decide_operation(data)
        self.assertEqual(kind, 'capture')
        self.assertEqual(op['operationResult'], 'EXECUTED')
        self.assertTrue(TRANSACTION.is_paid(op['operationResult']))

    def test_capture_only_is_not_lost(self):
        data = self._data(('CAPTURE', 'EXECUTED', '2026-10-30T10:00:05'))
        op, kind = TRANSACTION._nexi_decide_operation(data)
        self.assertEqual(kind, 'capture')
        self.assertTrue(TRANSACTION.is_paid(op['operationResult']))

    def test_pending_capture_waits(self):
        data = self._data(
            ('AUTHORIZATION', 'AUTHORIZED', '2026-10-30T10:00:00'),
            ('CAPTURE', 'PENDING', '2026-10-30T10:00:05'),
        )
        op, _kind = TRANSACTION._nexi_decide_operation(data)
        self.assertIn(op['operationResult'], TRANSACTION.WAIT_RESULTS)

    def test_latest_authorization_wins(self):
        data = self._data(
            ('AUTHORIZATION', 'EXECUTED', '2026-10-30T10:00:00'),
            ('AUTHORIZATION', 'DECLINED', '2026-10-30T10:05:00'),
        )
        op, kind = TRANSACTION._nexi_decide_operation(data)
        self.assertEqual(kind, 'auth')
        self.assertEqual(op['operationResult'], 'DECLINED')
        self.assertFalse(TRANSACTION.is_paid(op['operationResult']))

    def test_no_operations(self):
        op, _kind = TRANSACTION._nexi_decide_operation({})
        self.assertEqual(op, {})


class RateLimitTest(unittest.TestCase):
    """#7: le notifiche Nexi sono per transazione, non per IP."""

    def setUp(self):
        CONTROLLER._NEXI_RL.clear()

    def test_notify_key_does_not_depend_on_ip(self):
        key_client_a, limit = CONTROLLER._nexi_confirm_limit(True, {'tok': 'aaa'})
        key_client_b, _ = CONTROLLER._nexi_confirm_limit(True, {'tok': 'bbb'})
        self.assertNotEqual(key_client_a, key_client_b, 'clienti diversi, secchielli diversi')
        self.assertEqual(limit, CONTROLLER.NOTIFY_LIMIT)

    def test_one_terminal_can_notify_many_customers(self):
        # 200 notifiche dallo stesso IP, 200 transazioni diverse: nessun blocco.
        for i in range(200):
            key, limit = CONTROLLER._nexi_confirm_limit(True, {'tok': 'tok%d' % i})
            self.assertTrue(CONTROLLER._nexi_rl_allow(key, limit), 'bloccato al giro %s' % i)

    def test_burst_on_one_transaction_is_limited(self):
        key, limit = CONTROLLER._nexi_confirm_limit(True, {'tok': 'same'})
        allowed = sum(
            1 for _ in range(limit + 5) if CONTROLLER._nexi_rl_allow(key, limit)
        )
        self.assertEqual(allowed, limit)

    def test_browser_return_stays_per_ip(self):
        key, limit = CONTROLLER._nexi_confirm_limit(False, {'tok': 'aaa'})
        self.assertIn('confirm', key)
        allowed = sum(
            1 for _ in range(limit + 5) if CONTROLLER._nexi_rl_allow(key, limit)
        )
        self.assertEqual(allowed, limit)


class SessionChoiceTest(unittest.TestCase):
    """#10: la carta scelta in cassa viaggia in sessione."""

    def tearDown(self):
        ODOO_HTTP.request.session = {}

    def test_no_choice_without_session_value(self):
        ODOO_HTTP.request.session = {}
        self.assertIsNone(TRANSACTION._nexi_session_choice())

    def test_choice_is_read_back(self):
        ODOO_HTTP.request.session = {
            'nexi_saved_contract': {'order_id': 5, 'contract_id': 'NABC'},
        }
        self.assertEqual(TRANSACTION._nexi_session_choice()['contract_id'], 'NABC')

    def test_garbage_in_session_is_ignored(self):
        ODOO_HTTP.request.session = {'nexi_saved_contract': 'stringa a caso'}
        self.assertIsNone(TRANSACTION._nexi_session_choice())


class ContractDomainTest(unittest.TestCase):
    """#11: i contratti valgono per il terminale che li ha creati."""

    def test_domain_filters_by_provider(self):
        model = TRANSACTION.PaymentTransactionNexi()
        domain = model._nexi_contract_domain([1, 2], 7)
        self.assertIn(('provider_id', '=', 7), domain)
        self.assertIn(('partner_id', 'in', [1, 2]), domain)

    def test_domain_without_provider_keeps_all(self):
        model = TRANSACTION.PaymentTransactionNexi()
        domain = model._nexi_contract_domain([1])
        self.assertFalse([d for d in domain if d[0] == 'provider_id'])


if __name__ == '__main__':
    unittest.main()

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

    def test_bad_credentials_stop_the_check(self):
        """401/403 non è un metodo spento: è la chiave sbagliata."""
        err = PROVIDER.NexiApiError('Nexi: unauthorized', status_code=401)
        self.assertEqual(PROVIDER._classify_nexi_check_error(err), 'config')
        with self.assertRaises(PROVIDER.NexiUnavailable):
            self._provider(err)._nexi_check_service(
                'https://xpay.nexigroup.com/orders/hpp', 'CARDS'
            )

    def test_server_error_is_not_a_rejection(self):
        err = PROVIDER.NexiApiError('Nexi: boom', status_code=500)
        self.assertEqual(PROVIDER._classify_nexi_check_error(err), 'unknown')

    def test_missing_configuration_is_a_config_error(self):
        err = PROVIDER.ValidationError('Manca URL Nexi. Metti URL test/produzione.')
        self.assertEqual(PROVIDER._classify_nexi_check_error(err), 'config')

    def test_network_error_is_not_a_config_error(self):
        err = PROVIDER.NexiUnavailable('Nexi non raggiungibile. Riprova.')
        self.assertEqual(PROVIDER._classify_nexi_check_error(err), 'unknown')


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


class LandingRouteTest(unittest.TestCase):
    """#12: la rotta del flusso resta quella, senza loop di polling."""

    def test_shop_order_goes_to_checkout_validate(self):
        self.assertEqual(
            CONTROLLER._nexi_landing_route('/payment/status', True),
            '/shop/payment/validate',
        )
        self.assertEqual(
            CONTROLLER._nexi_landing_route('', True), '/shop/payment/validate'
        )

    def test_portal_route_is_kept(self):
        invoice = '/my/invoices/42?tx_id=1&access_token=x'
        self.assertEqual(CONTROLLER._nexi_landing_route(invoice, False), invoice)
        self.assertEqual(CONTROLLER._nexi_landing_route(invoice, True), invoice)

    def test_portal_without_route_does_not_jump_to_the_shop(self):
        # Pagamento senza ordine: non esiste una cassa da validare.
        self.assertEqual(
            CONTROLLER._nexi_landing_route('', False), '/payment/confirmation'
        )


class UrlAllowlistTest(unittest.TestCase):
    """Audit SEC1: il parser di Python e quello del client HTTP devono
    leggere lo stesso host, altrimenti la X-Api-Key esce dal dominio Nexi."""

    def test_backslash_before_at_is_refused(self):
        # urlparse legge xpay.nexigroup.com, requests chiama evil.tld: rifiuto.
        for url in (
            'https://evil.tld\\@xpay.nexigroup.com/psp/api/v1/orders/hpp',
            'https://evil.tld:443\\@xpay.nexigroup.com/x',
            'https://evil\\@xpay.nexigroup.com/x',
        ):
            self.assertFalse(PROVIDER.is_nexi_url(url), url)

    def test_userinfo_is_refused(self):
        self.assertFalse(PROVIDER.is_nexi_url('https://user:pw@xpay.nexigroup.com/x'))

    def test_odd_port_is_refused(self):
        self.assertFalse(PROVIDER.is_nexi_url('https://xpay.nexigroup.com:8443/x'))

    def test_plain_hosts_still_pass(self):
        self.assertTrue(PROVIDER.is_nexi_url('https://xpay.nexigroup.com/x'))
        self.assertTrue(PROVIDER.is_nexi_url('https://xpay.nexigroup.com:443/x'))

    def test_request_guard_refuses_to_send_the_key(self):
        # _nexi_make_request non deve nemmeno partire con un URL non Nexi.
        class Fake(PROVIDER.PaymentProviderNexi):
            nexi_api_key = 'chiave-di-prova'

            def ensure_one(self):
                return self
        with self.assertRaises(PROVIDER.ValidationError):
            Fake()._nexi_make_request('GET', 'https://evil.tld\\@xpay.nexigroup.com/x')


class SafeFieldsTest(unittest.TestCase):
    """Audit D1/B6: gli iframe dei campi carta solo da host Nexi."""

    def test_foreign_src_is_dropped(self):
        fields = [
            {'id': 'CARD_NUMBER', 'src': 'https://xpay.nexigroup.com/field/1'},
            {'id': 'SECURITY_CODE', 'src': 'https://attacker.tld/card.html'},
            {'id': 'BAD', 'src': 'data:text/html,<form>'},
            {'id': 'NO_SRC'},
            'non un dict',
        ]
        kept = TRANSACTION._nexi_safe_fields(fields)
        self.assertEqual([f['id'] for f in kept], ['CARD_NUMBER'])

    def test_empty_is_fine(self):
        self.assertEqual(TRANSACTION._nexi_safe_fields(None), [])


class OperationTimeTest(unittest.TestCase):
    """Audit B5: operationTime non si confronta come stringa."""

    def _data(self, *ops):
        return {'operations': [dict(operationType=t, operationResult=r, operationTime=ts)
                               for t, r, ts in ops]}

    def test_unpadded_hour_does_not_beat_later_time(self):
        # 'T9:00:00' > 'T10:00:00' come stringa: qui deve vincere l'autorizzazione.
        data = self._data(
            ('AUTHORIZATION', 'EXECUTED', '2026-10-30T10:00:00'),
            ('CAPTURE', 'CANCELED', '2026-10-30T9:00:00'),
        )
        op, kind = TRANSACTION._nexi_decide_operation(data)
        self.assertEqual(kind, 'auth')
        self.assertEqual(op['operationResult'], 'EXECUTED')

    def test_missing_time_prefers_the_authorization(self):
        data = self._data(
            ('AUTHORIZATION', 'AUTHORIZED', None),
            ('CAPTURE', 'EXECUTED', None),
        )
        op, kind = TRANSACTION._nexi_decide_operation(data)
        self.assertEqual(kind, 'auth')
        self.assertEqual(op['operationResult'], 'AUTHORIZED')

    def test_capture_only_still_decides(self):
        op, kind = TRANSACTION._nexi_decide_operation(
            self._data(('CAPTURE', 'EXECUTED', None))
        )
        self.assertEqual(kind, 'capture')
        self.assertEqual(op['operationResult'], 'EXECUTED')


class RateLimitBucketTest(unittest.TestCase):
    """Audit A1: la chiave del secchiello non la detta il chiamante."""

    def setUp(self):
        CONTROLLER._NEXI_RL.clear()

    def test_bucket_has_fixed_size(self):
        big = CONTROLLER._nexi_rl_bucket('x' * 10000)
        self.assertEqual(len(big), 32)
        self.assertNotEqual(big, CONTROLLER._nexi_rl_bucket('y' * 10000))

    def test_notify_key_does_not_contain_the_raw_token(self):
        key, _limit = CONTROLLER._nexi_confirm_limit(True, {'tok': 'segreto-in-chiaro'})
        self.assertNotIn('segreto', key)

    def test_flooding_keys_does_not_wipe_a_live_bucket(self):
        now = CONTROLLER.time.time()
        CONTROLLER._NEXI_RL['nexi:prepare:mio-ip'] = [now]
        for i in range(2500):
            CONTROLLER._NEXI_RL['nexi:notify:%032d' % i] = [now]
        CONTROLLER._nexi_rl_allow('nexi:fields:altro', 30)
        self.assertIn('nexi:prepare:mio-ip', CONTROLLER._NEXI_RL,
                      'un flood non deve azzerare i contatori degli altri')

if __name__ == '__main__':
    unittest.main()

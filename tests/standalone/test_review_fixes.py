# -*- coding: utf-8 -*-
"""Tests for the review #1 fixes, without an Odoo server.

They run against a fake `odoo` module (only the few things the module uses), so
they can be launched on any machine:

    python3 -m unittest discover -s tests/standalone -t .

They cover the pure logic: Nexi domains, method-check classification, choice of
the operation that decides the outcome, rate limits, session.
The part that talks to Nexi and to the database has to be tested at checkout.
"""
import json
import os
import pathlib
import re
import sys
import tempfile
import textwrap
import time
import unittest
from unittest.mock import patch

ADDON_ROOT = pathlib.Path(__file__).resolve().parents[2]


def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text), encoding='utf-8')


def _build_stubs(base):
    """Fake `odoo` package: classes and fields that do nothing."""
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
            """Fake request: the tests write in `session` when needed."""

            def __init__(self):
                self.session = {}
                self.httprequest = None


        request = _Request()
    ''')
    # `payment.controllers.post_processing`, imported by the controller.
    addons = odoo / 'addons'
    payment = addons / 'payment'
    controllers = payment / 'controllers'
    _write(addons / '__init__.py', 'from . import payment  # noqa: F401')
    _write(payment / '__init__.py', 'from . import controllers  # noqa: F401')
    _write(controllers / '__init__.py',
           'from . import post_processing  # noqa: F401')
    _write(controllers / 'post_processing.py', '''
        class PaymentPostProcessing:
            @staticmethod
            def monitor_transaction(tx):
                return None
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
    """#1: the production domain must pass, the rest must not."""

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

    def test_retired_nexi_it_host_is_refused(self):
        """#9 H-A2: xpay.nexi.it no longer resolves, the server refuses it like the JS does."""
        self.assertFalse(PROVIDER.is_nexi_url('https://xpay.nexi.it/orders/hpp'))

    def test_empty_is_refused(self):
        self.assertFalse(PROVIDER.is_nexi_url(''))
        self.assertFalse(PROVIDER.is_nexi_url(None))


class CheckServiceTest(unittest.TestCase):
    """#8: a timeout must not disable a working method."""

    def _provider(self, exc):
        class FakeProvider(PROVIDER.PaymentProviderNexi):
            def _nexi_make_request(self, *args, **kwargs):
                raise exc
        return FakeProvider()

    def test_timeout_is_not_verified(self):
        enabled, detail = self._provider(
            PROVIDER.NexiUnavailable('Nexi is unreachable. Try again.')
        )._nexi_check_service('https://xpay.nexigroup.com/orders/hpp', 'CARDS')
        self.assertIsNone(enabled, 'a timeout must not disable the method')
        self.assertIn('not checked', detail)

    def test_service_not_enabled_is_a_rejection(self):
        enabled, detail = self._provider(
            PROVIDER.ValidationError('Nexi: Payment service not enabled [PS0064]')
        )._nexi_check_service('https://xpay.nexigroup.com/orders/hpp', 'CARDS')
        self.assertIs(enabled, False)
        self.assertIn('refused', detail)

    def test_unknown_error_is_not_a_rejection(self):
        enabled, detail = self._provider(
            PROVIDER.ValidationError('Nexi: Internal error [GW9999]')
        )._nexi_check_service('https://xpay.nexigroup.com/orders/hpp', 'CARDS')
        self.assertIsNone(enabled, 'an unknown error must not disable the method')
        self.assertIn('error', detail)

    def test_working_service_is_enabled(self):
        class OkProvider(PROVIDER.PaymentProviderNexi):
            def _nexi_make_request(self, *args, **kwargs):
                return {'orderId': 'x'}
        enabled, detail = OkProvider()._nexi_check_service(
            'https://xpay.nexigroup.com/orders/hpp', 'CARDS'
        )
        self.assertIs(enabled, True)
        self.assertIn('enabled', detail)

    def test_bnpl_check_amount_is_compatible(self):
        # Below EUR 30 Nexi refuses Pay in 3: the check would say "not enabled"
        # even on a terminal that has it enabled.
        self.assertGreaterEqual(int(PROVIDER.CHECK_AMOUNTS['PAYPAL_PAGAIN3']), 3000)

    def test_other_services_use_the_default_amount(self):
        self.assertNotIn('CARDS', PROVIDER.CHECK_AMOUNTS)

    def test_temporary_outage_is_not_a_rejection(self):
        # A 503 "Service not available" must not disable a valid method.
        for status in (408, 429, 500, 502, 503):
            err = PROVIDER.NexiApiError('Nexi: Service not available', status_code=status)
            self.assertEqual(
                PROVIDER._classify_nexi_check_error(err), 'unknown',
                'status %s classified as a refusal' % status,
            )

    def test_rejection_on_400_is_still_a_rejection(self):
        err = PROVIDER.NexiApiError('Nexi: paymentService not enabled', status_code=400)
        self.assertEqual(PROVIDER._classify_nexi_check_error(err), 'disabled')

    def test_check_order_id_fits_nexi_limit(self):
        """Nexi accepts orderIds up to 27 characters: the check must stay under."""
        class Ok(PROVIDER.PaymentProviderNexi):
            def _nexi_make_request(self, http_method, url, payload=None, params=None):
                Ok.last = payload
                return {}
        for service in ('CARDS', 'PAYPAL_PAGAIN3', 'BANCOMATPAY', 'GOOGLEPAY'):
            Ok()._nexi_check_service('https://xpay.nexigroup.com/orders/hpp', service)
            ref = Ok.last['order']['orderId']
            self.assertLessEqual(len(ref), 27, '%s: %s' % (service, ref))
    def test_bad_credentials_stop_the_check(self):
        """401/403 is not a disabled method: it is the wrong key."""
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
        err = PROVIDER.ValidationError(
            'Nexi URL is missing. Enter the test/production URL from the API page.')
        self.assertEqual(PROVIDER._classify_nexi_check_error(err), 'config')

    def test_network_error_is_not_a_config_error(self):
        err = PROVIDER.NexiUnavailable('Nexi is unreachable. Try again.')
        self.assertEqual(PROVIDER._classify_nexi_check_error(err), 'unknown')


class DecideOperationTest(unittest.TestCase):
    """#2: with automatic capture the outcome is on the latest operation."""

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
    """#7: Nexi notifications are per transaction, not per IP."""

    def setUp(self):
        CONTROLLER._NEXI_RL.clear()

    def test_notify_key_does_not_depend_on_ip(self):
        key_client_a, limit = CONTROLLER._nexi_confirm_limit(True, {'tok': 'aaa'})
        key_client_b, _ = CONTROLLER._nexi_confirm_limit(True, {'tok': 'bbb'})
        self.assertNotEqual(key_client_a, key_client_b, 'different clients, different buckets')
        self.assertEqual(limit, CONTROLLER.NOTIFY_LIMIT)

    def test_one_terminal_can_notify_many_customers(self):
        # 200 notifiche dallo stesso IP, 200 transazioni diverse: nessun blocco.
        for i in range(200):
            key, limit = CONTROLLER._nexi_confirm_limit(True, {'tok': 'tok%d' % i})
            self.assertTrue(CONTROLLER._nexi_rl_allow(key, limit), 'blocked at round %s' % i)

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
    """#10: the card chosen at checkout travels in the session."""

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
        ODOO_HTTP.request.session = {'nexi_saved_contract': 'random string'}
        self.assertIsNone(TRANSACTION._nexi_session_choice())


class ContractDomainTest(unittest.TestCase):
    """#11: contracts belong to the terminal that created them."""

    def _provider_clause(self, domain):
        return [d for d in domain if d[0] == 'provider_id']

    def test_domain_restricts_to_the_requested_terminal(self):
        """The invariant: the domain limits the contracts to the requested terminals."""
        model = TRANSACTION.PaymentTransactionNexi()
        domain = model._nexi_contract_domain([1, 2], 7)
        clause = self._provider_clause(domain)
        self.assertEqual(len(clause), 1, 'a single clause on the provider')
        self.assertEqual(list(clause[0][2]), [7])
        self.assertIn(('partner_id', 'in', [1, 2]), domain)

    def test_domain_accepts_several_terminals(self):
        model = TRANSACTION.PaymentTransactionNexi()
        clause = self._provider_clause(model._nexi_contract_domain([1], [3, 7]))
        self.assertEqual(sorted(clause[0][2]), [3, 7])

    def test_domain_without_provider_keeps_all(self):
        model = TRANSACTION.PaymentTransactionNexi()
        domain = model._nexi_contract_domain([1])
        self.assertFalse([d for d in domain if d[0] == 'provider_id'])


class LandingRouteTest(unittest.TestCase):
    """#12: the flow route stays as it is, without a polling loop."""

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
        # Payment without an order: there is no checkout to validate.
        self.assertEqual(
            CONTROLLER._nexi_landing_route('', False), '/payment/confirmation'
        )


class UrlAllowlistTest(unittest.TestCase):
    """Audit SEC1: the Python parser and the HTTP client parser must read the
    same host, otherwise the X-Api-Key leaves the Nexi domain."""

    def test_backslash_before_at_is_refused(self):
        # urlparse reads xpay.nexigroup.com, requests calls evil.tld: refuse.
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
        # _nexi_make_request must not even start with a non-Nexi URL.
        class Fake(PROVIDER.PaymentProviderNexi):
            nexi_api_key = 'test-key'

            def ensure_one(self):
                return self
        with self.assertRaises(PROVIDER.ValidationError):
            Fake()._nexi_make_request('GET', 'https://evil.tld\\@xpay.nexigroup.com/x')


class SafeFieldsTest(unittest.TestCase):
    """Audit D1/B6: card-field iframes only from Nexi hosts."""

    def test_foreign_src_is_dropped(self):
        fields = [
            {'id': 'CARD_NUMBER', 'src': 'https://xpay.nexigroup.com/field/1'},
            {'id': 'SECURITY_CODE', 'src': 'https://attacker.tld/card.html'},
            {'id': 'BAD', 'src': 'data:text/html,<form>'},
            {'id': 'NO_SRC'},
            'not a dict',
        ]
        kept = TRANSACTION._nexi_safe_fields(fields)
        self.assertEqual([f['id'] for f in kept], ['CARD_NUMBER'])

    def test_empty_is_fine(self):
        self.assertEqual(TRANSACTION._nexi_safe_fields(None), [])


class ParseCentsTest(unittest.TestCase):
    """#7 punto 2: intero = centesimi, decimale = euro, resto = None."""

    def test_integer_is_cents(self):
        self.assertEqual(TRANSACTION._parse_cents('1000'), 1000)
        self.assertEqual(TRANSACTION._parse_cents(1000), 1000)

    def test_decimal_is_euro(self):
        self.assertEqual(TRANSACTION._parse_cents('10.00'), 1000)
        self.assertEqual(TRANSACTION._parse_cents('12.34'), 1234)

    def test_garbage_is_none(self):
        self.assertIsNone(TRANSACTION._parse_cents(None))
        self.assertIsNone(TRANSACTION._parse_cents(''))
        self.assertIsNone(TRANSACTION._parse_cents('abc'))


class CaptureTotalTest(unittest.TestCase):
    """#8 M1: order total -> operation -> sum of captures."""

    def _op(self, **kw):
        op = {'operationType': 'CAPTURE', 'operationResult': 'EXECUTED'}
        op.update(kw)
        return op

    def test_order_total_wins_over_operation(self):
        data = {'orderStatus': {'capturedAmount': '600'},
                'operations': [self._op(operationAmount='600')]}
        self.assertEqual(
            TRANSACTION._nexi_capture_total(data, self._op(operationAmount='1000')), 600)

    def test_operation_amount_is_fallback(self):
        data = {'orderStatus': {}, 'operations': []}
        self.assertEqual(
            TRANSACTION._nexi_capture_total(data, self._op(operationAmount='1000')), 1000)

    def test_operation_captured_amount_is_fallback(self):
        data = {'orderStatus': {}, 'operations': []}
        self.assertEqual(
            TRANSACTION._nexi_capture_total(data, self._op(capturedAmount='1000')), 1000)

    def test_order_status_captured_amount_is_first(self):
        data = {'orderStatus': {'capturedAmount': '1000'}, 'operations': []}
        self.assertEqual(TRANSACTION._nexi_capture_total(data, self._op()), 1000)

    def test_sum_skips_other_currencies(self):
        data = {'orderStatus': {}, 'operations': [
            self._op(operationAmount='600', operationCurrency='EUR'),
            self._op(operationAmount='400', operationCurrency='USD'),
        ]}
        self.assertEqual(
            TRANSACTION._nexi_capture_total(data, self._op(), currency='EUR'), 600)

    def test_sum_of_captures_is_last_resort(self):
        data = {'orderStatus': {}, 'operations': [
            self._op(operationAmount='600'),
            self._op(operationAmount='400'),
            {'operationType': 'AUTHORIZATION', 'operationResult': 'EXECUTED',
             'operationAmount': '9999'},
            {'operationType': 'CAPTURE', 'operationResult': 'DECLINED',
             'operationAmount': '9999'},
        ]}
        self.assertEqual(TRANSACTION._nexi_capture_total(data, self._op()), 1000)

    def test_no_amount_anywhere_is_none(self):
        data = {'orderStatus': {}, 'operations': [self._op()]}
        self.assertIsNone(TRANSACTION._nexi_capture_total(data, self._op()))
        self.assertIsNone(TRANSACTION._nexi_capture_total({}, {}))



    def _data(self, *ops):
        return {'operations': [dict(operationType=t, operationResult=r, operationTime=ts)
                               for t, r, ts in ops]}

    def test_unpadded_hour_does_not_beat_later_time(self):
        # 'T9:00:00' > 'T10:00:00' as a string: here the authorization must win.
        data = self._data(
            ('AUTHORIZATION', 'EXECUTED', '2026-10-30T10:00:00'),
            ('CAPTURE', 'EXECUTED', '2026-10-30T9:00:00'),
        )
        op, kind = TRANSACTION._nexi_decide_operation(data)
        self.assertEqual(kind, 'auth')
        self.assertEqual(op['operationResult'], 'EXECUTED')

    def test_disagreeing_without_time_is_uncertain(self):
        # AUTH and CAPTURE disagree without dates: the latest cannot be known,
        # never close.
        data = self._data(
            ('AUTHORIZATION', 'EXECUTED', None),
            ('CAPTURE', 'DECLINED', None),
        )
        op, kind = TRANSACTION._nexi_decide_operation(data)
        self.assertEqual(kind, 'uncertain')
        self.assertEqual(op, {})

    def test_agreeing_without_time_prefers_the_authorization(self):
        data = self._data(
            ('AUTHORIZATION', 'AUTHORIZED', None),
            ('CAPTURE', 'DECLINED', None),
        )
        op, kind = TRANSACTION._nexi_decide_operation(data)
        self.assertEqual(kind, 'auth')
        self.assertEqual(op['operationResult'], 'AUTHORIZED')

    def test_same_instant_disagreeing_is_uncertain(self):
        data = self._data(
            ('AUTHORIZATION', 'EXECUTED', '2026-10-30T10:00:00'),
            ('CAPTURE', 'DECLINED', '2026-10-30T10:00:00'),
        )
        op, kind = TRANSACTION._nexi_decide_operation(data)
        self.assertEqual(kind, 'uncertain')

    def test_capture_only_still_decides(self):
        op, kind = TRANSACTION._nexi_decide_operation(
            self._data(('CAPTURE', 'EXECUTED', None))
        )
        self.assertEqual(kind, 'capture')
        self.assertEqual(op['operationResult'], 'EXECUTED')


class RateLimitBucketTest(unittest.TestCase):
    """Audit A1: the bucket key is not dictated by the caller."""

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
        CONTROLLER._NEXI_RL['nexi:prepare:my-ip'] = [now]
        for i in range(2500):
            CONTROLLER._NEXI_RL['nexi:notify:%032d' % i] = [now]
        CONTROLLER._nexi_rl_allow('nexi:fields:other', 30)
        self.assertIn('nexi:prepare:my-ip', CONTROLLER._NEXI_RL,
                      'a flood must not reset the counters of the others')

    def test_full_table_rejects_only_new_keys(self):
        now = CONTROLLER.time.time()
        CONTROLLER._NEXI_RL['nexi:prepare:my-ip'] = [now]
        for i in range(4100):
            CONTROLLER._NEXI_RL['nexi:notify:%032d' % i] = [now]
        self.assertTrue(CONTROLLER._nexi_rl_allow('nexi:prepare:my-ip', 30),
                        'existing buckets keep counting')
        self.assertFalse(CONTROLLER._nexi_rl_allow('nexi:prepare:new-key', 30),
                         'whoever asks for a new key is refused')

    def test_notify_flood_hits_the_ip_cap(self):
        # 600 notifications from the same IP with different toks: the IP guard
        # fires before the per-ref bucket (the controller applies it first).
        allowed = 0
        for i in range(700):
            ip = CONTROLLER._nexi_rl_key('notify-ip')
            if not CONTROLLER._nexi_rl_allow(ip, CONTROLLER.NOTIFY_LIMIT * 10):
                continue
            key, limit = CONTROLLER._nexi_confirm_limit(True, {'tok': 'tok-%d' % i})
            if CONTROLLER._nexi_rl_allow(key, limit):
                allowed += 1
        self.assertLessEqual(allowed, 600)
        self.assertGreater(allowed, 500)

class ParseCentsNonFiniteTest(unittest.TestCase):
    """#10 L-B1: Infinity/NaN must not become a 500."""

    def test_non_finite_is_none(self):
        for value in ('Infinity', '-Infinity', 'NaN', 'sNaN'):
            with self.subTest(value=value):
                self.assertIsNone(TRANSACTION._parse_cents(value))
                self.assertEqual(TRANSACTION.to_cents(value), 0)

    def test_finite_still_works(self):
        self.assertEqual(TRANSACTION._parse_cents('1000'), 1000)
        self.assertEqual(TRANSACTION._parse_cents('10.00'), 1000)


class MalformedPayloadTest(unittest.TestCase):
    """#10 L-B1: unexpected gateway shapes, not exceptions."""

    def test_order_status_is_not_a_dict(self):
        self.assertIsNone(TRANSACTION._nexi_capture_total(
            {'orderStatus': 'broken', 'operations': 'broken'}, {}))
        self.assertIsNone(TRANSACTION._nexi_capture_total('broken', []))

    def test_operations_is_not_a_list(self):
        op, kind = TRANSACTION._nexi_decide_operation({'operations': 'broken'})
        self.assertEqual(op, {})
        self.assertEqual(kind, 'capture')

    def test_order_id_extraction_ignores_bad_shapes(self):
        self.assertEqual(TRANSACTION._nexi_got_order_id(None), '')
        self.assertEqual(TRANSACTION._nexi_got_order_id({'orderStatus': 'x'}), '')
        self.assertEqual(
            TRANSACTION._nexi_got_order_id(
                {'orderStatus': {'order': {'orderId': 'REF1'}}}),
            'REF1',
        )


class SanitizeTextTest(unittest.TestCase):
    """#10 L-B14: one-line logs, without ANSI/bidi/control."""

    def test_control_sequences_are_removed(self):
        clean = PROVIDER._nexi_sanitize_text(
            'error\n\x1b[31mred\x1b[0m\u202eNewline\r\tend')
        self.assertNotIn('\n', clean)
        self.assertNotIn('\x1b', clean)
        self.assertNotIn('\u202e', clean)
        self.assertIn('red', clean)
        self.assertIn('end', clean)

    def test_text_is_capped(self):
        self.assertLessEqual(len(PROVIDER._nexi_sanitize_text('x' * 1000)), 300)


class BodyGuardTest(unittest.TestCase):
    """#10 L-B2: bounded body and overall deadline."""

    class _Resp:
        def __init__(self, chunks, headers=None):
            self._chunks = chunks
            self.headers = headers or {}

        def iter_content(self, size):
            for chunk in self._chunks:
                yield chunk

    def test_content_length_over_cap_is_refused(self):
        with self.assertRaises(PROVIDER.NexiUnavailable):
            PROVIDER._nexi_read_limited(
                self._Resp([], {'Content-Length': '200'}), max_bytes=100)

    def test_stream_over_cap_is_refused(self):
        with self.assertRaises(PROVIDER.NexiUnavailable):
            PROVIDER._nexi_read_limited(
                self._Resp([b'x' * 60, b'y' * 60]), max_bytes=100)

    def test_deadline_stops_slow_drip(self):
        clock = iter([0, 11])
        with self.assertRaises(PROVIDER.NexiUnavailable):
            PROVIDER._nexi_read_limited(
                self._Resp([b'a', b'b']), max_bytes=100,
                deadline=10, now=lambda: next(clock))

    def test_small_body_is_returned(self):
        self.assertEqual(
            PROVIDER._nexi_read_limited(self._Resp([b'a', b'b']), max_bytes=100),
            b'ab',
        )

    def test_error_detail_survives_recursion(self):
        class Bad:
            def json(self):
                raise RecursionError('too deep')
        self.assertEqual(PROVIDER._nexi_error_detail(Bad()), 'error. Try again.')


class RequestGuardTest(unittest.TestCase):
    """#10 L-B2: the request uses stream and the body goes through the cap."""

    class _Resp:
        status_code = 200
        headers = {'Content-Length': '2'}
        def __init__(self):
            self._content = False
        def iter_content(self, size):
            yield b'{}'
        def close(self):
            pass
        def raise_for_status(self):
            pass
        @property
        def text(self):
            return (self._content or b'').decode('utf-8') if isinstance(self._content, bytes) else ''
        def json(self):
            return {'ok': True}

    class _Provider(PROVIDER.PaymentProviderNexi):
        nexi_api_key = 'key'
        def ensure_one(self):
            return self

    def test_stream_is_used(self):
        response = self._Resp()
        with patch.object(PROVIDER.requests, 'request', return_value=response) as req:
            data = self._Provider()._nexi_make_request(
                'GET', 'https://xpay.nexigroup.com/orders/x')
        self.assertEqual(data, {'ok': True})
        self.assertIs(req.call_args.kwargs.get('stream'), True)

    def test_oversized_content_length_is_refused(self):
        response = self._Resp()
        response.headers = {'Content-Length': str(PROVIDER.NEXI_MAX_BODY + 1)}
        with patch.object(PROVIDER.requests, 'request', return_value=response):
            with self.assertRaises(PROVIDER.NexiUnavailable):
                self._Provider()._nexi_make_request(
                    'GET', 'https://xpay.nexigroup.com/orders/x')


class TotalDeadlineTest(unittest.TestCase):
    """#15 M3: a dripping gateway does not hold the worker past the budget."""

    class _Provider(PROVIDER.PaymentProviderNexi):
        nexi_api_key = 'key'

        def ensure_one(self):
            return self

    def test_hung_request_is_abandoned(self):
        def hang(*args, **kwargs):
            time.sleep(1.5)

        with patch.object(PROVIDER.requests, 'request', side_effect=hang), \
                patch.object(PROVIDER, 'NEXI_TOTAL_TIMEOUT', 0.2):
            started = time.monotonic()
            with self.assertRaises(PROVIDER.NexiUnavailable):
                self._Provider()._nexi_make_request(
                    'GET', 'https://xpay.nexigroup.com/orders/x')
            self.assertLess(time.monotonic() - started, 1.0)

    def test_errors_cross_the_guard(self):
        def boom(*args, **kwargs):
            raise PROVIDER.NexiUnavailable('down')

        with patch.object(PROVIDER.requests, 'request', side_effect=boom):
            with self.assertRaises(PROVIDER.NexiUnavailable):
                self._Provider()._nexi_make_request(
                    'GET', 'https://xpay.nexigroup.com/orders/x')


class BaseUrlTest(unittest.TestCase):
    """#10 L-B6: public base only https and site host."""

    def test_valid_https(self):
        self.assertEqual(
            PROVIDER._nexi_clean_base_url('https://shop.example.com/'),
            'https://shop.example.com',
        )

    def test_localhost_and_http_are_refused(self):
        for value in ('http://shop.example.com', 'https://localhost:8069',
                      'https://127.0.0.1', 'https://user@shop.example.com'):
            with self.subTest(value=value):
                self.assertEqual(PROVIDER._nexi_clean_base_url(value), '')

    def test_host_must_be_configured(self):
        self.assertTrue(PROVIDER._nexi_base_allowed(
            'https://shop.example.com', {'shop.example.com'}))
        self.assertFalse(PROVIDER._nexi_base_allowed(
            'https://evil.example', {'shop.example.com'}))

    def test_controller_uses_configured_base(self):
        class P:
            nexi_public_base = 'https://shop.example.com'
            def get_base_url(self):
                return 'https://fallback.example'

        class W:
            domain = 'shop.example'

        self.assertEqual(
            CONTROLLER._nexi_site_base(P(), W()), 'https://shop.example.com')
        P.nexi_public_base = ''
        self.assertEqual(CONTROLLER._nexi_site_base(P(), W()), 'https://shop.example')


class GlobalRateLimitTest(unittest.TestCase):
    """#10 L-B3: rotating XFF must not avoid the global cap."""

    def setUp(self):
        CONTROLLER._NEXI_RL.clear()

    def test_global_bucket_is_a_ceiling(self):
        allowed = sum(
            1 for _ in range(CONTROLLER.GLOBAL_LIMIT + 5)
            if CONTROLLER._nexi_global_allow()
        )
        self.assertEqual(allowed, CONTROLLER.GLOBAL_LIMIT)



class RateLimitOrderTest(unittest.TestCase):
    """M1: the global bucket is only charged to admitted requests."""

    def setUp(self):
        CONTROLLER._NEXI_RL.clear()

    def tearDown(self):
        CONTROLLER._NEXI_RL.clear()

    def test_rejected_client_does_not_spend_global(self):
        self.assertTrue(CONTROLLER._nexi_admit('k', 1))
        self.assertFalse(CONTROLLER._nexi_admit('k', 1))
        self.assertEqual(
            len(CONTROLLER._NEXI_RL.get(CONTROLLER._GLOBAL_KEY, [])), 1,
            'the request refused by the per-client bucket does not touch the global one')

    def test_admitted_client_spends_global(self):
        self.assertTrue(CONTROLLER._nexi_admit('a', 1))
        self.assertTrue(CONTROLLER._nexi_admit('b', 1))
        self.assertEqual(
            len(CONTROLLER._NEXI_RL.get(CONTROLLER._GLOBAL_KEY, [])), 2)


class NotifyBucketTest(unittest.TestCase):
    """M5: notifications do not share the bucket when the tok is missing."""

    def test_ref_gets_its_own_bucket(self):
        first, _ = CONTROLLER._nexi_confirm_limit(True, {'ref': 'S1'})
        second, _ = CONTROLLER._nexi_confirm_limit(True, {'ref': 'S2'})
        self.assertNotEqual(first, second)
        self.assertIn(CONTROLLER._nexi_rl_bucket('S1'), first)

    def test_missing_ident_uses_fixed_no_ref_bucket(self):
        key, limit = CONTROLLER._nexi_confirm_limit(True, {})
        self.assertIn(CONTROLLER._nexi_rl_bucket('no-ref'), key)
        self.assertEqual(limit, CONTROLLER.NOTIFY_LIMIT)

    def test_tok_wins_over_ref(self):
        key, _ = CONTROLLER._nexi_confirm_limit(
            True, {'tok': 'tok-1', 'ref': 'S1'})
        self.assertIn(CONTROLLER._nexi_rl_bucket('tok-1'), key)


class LandingRouteSecurityTest(unittest.TestCase):
    """M4: relative paths only, no open redirect / javascript:."""

    def test_relative_route_is_preserved(self):
        self.assertEqual(
            CONTROLLER._nexi_landing_route('/my/invoices/42', False),
            '/my/invoices/42')

    def test_absolute_url_is_rejected(self):
        self.assertEqual(
            CONTROLLER._nexi_landing_route('https://evil.example', True),
            '/shop/payment/validate')

    def test_javascript_url_is_rejected(self):
        self.assertEqual(
            CONTROLLER._nexi_landing_route('javascript:alert(1)', False),
            '/payment/confirmation')

    def test_protocol_relative_is_rejected(self):
        self.assertEqual(
            CONTROLLER._nexi_landing_route('//evil.example/x', False),
            '/payment/confirmation')

    def test_status_route_falls_back(self):
        self.assertEqual(
            CONTROLLER._nexi_landing_route('/payment/status', True),
            '/shop/payment/validate')


class TokExtractionTest(unittest.TestCase):
    """#10 M-B3: tok from header, body, params or query."""

    class _Http:
        def __init__(self):
            self.headers = {}
            self.args = {}

    def _json_data(self):
        if isinstance(self._json, Exception):
            raise self._json
        return self._json

    def setUp(self):
        self.old_http = ODOO_HTTP.request.httprequest
        self.old_params = getattr(ODOO_HTTP.request, 'params', None)
        self.old_json = getattr(ODOO_HTTP.request, 'get_json_data', None)
        self._json = None
        ODOO_HTTP.request.httprequest = self._Http()
        ODOO_HTTP.request.params = {}
        ODOO_HTTP.request.get_json_data = self._json_data

    def tearDown(self):
        ODOO_HTTP.request.httprequest = self.old_http
        ODOO_HTTP.request.params = self.old_params
        if self.old_json is None:
            del ODOO_HTTP.request.get_json_data
        else:
            ODOO_HTTP.request.get_json_data = self.old_json

    def test_header_wins(self):
        ODOO_HTTP.request.httprequest.headers = {'X-Nexi-Token': 'header'}
        self._json = {'params': {'tok': 'body'}}
        self.assertEqual(CONTROLLER._nexi_tok(), 'header')

    def test_json_params_body(self):
        self._json = {'params': {'tok': 'body'}}
        self.assertEqual(CONTROLLER._nexi_tok(), 'body')

    def test_raw_json_body(self):
        self._json = {'tok': 'raw'}
        self.assertEqual(CONTROLLER._nexi_tok(), 'raw')

    def test_query_fallback(self):
        self._json = ValueError('no json')
        ODOO_HTTP.request.httprequest.args = {'tok': 'query'}
        self.assertEqual(CONTROLLER._nexi_tok(), 'query')

    def test_missing_is_none(self):
        self._json = ValueError('no json')
        self.assertIsNone(CONTROLLER._nexi_tok())



class CaptureEvidenceTest(unittest.TestCase):
    """H1: an authorization is not a capture."""

    AUTH = {
        'operationType': 'AUTHORIZATION',
        'operationResult': 'EXECUTED',
        'operationAmount': '1000',
        'operationCurrency': 'EUR',
        'operationTime': '2026-10-30T10:00:00',
    }
    CAPTURE = {
        'operationType': 'CAPTURE',
        'operationResult': 'EXECUTED',
        'operationAmount': '1000',
        'operationCurrency': 'EUR',
        'operationTime': '2026-10-30T10:00:05',
    }

    def test_authorization_amount_is_not_capture(self):
        data = {'operations': [self.AUTH]}
        self.assertIsNone(
            TRANSACTION._nexi_capture_total(data, self.AUTH, 'EUR'))

    def test_capture_operation_amount_is_capture(self):
        data = {'operations': [self.CAPTURE]}
        self.assertEqual(
            TRANSACTION._nexi_capture_total(data, self.CAPTURE, 'EUR'), 1000)

    def test_order_level_capture_wins(self):
        data = {'orderStatus': {'capturedAmount': '1000'},
                'operations': [self.AUTH]}
        self.assertEqual(
            TRANSACTION._nexi_capture_total(data, self.AUTH, 'EUR'), 1000)

    def test_reversal_after_capture_is_detected(self):
        refund = {
            'operationType': 'REFUND',
            'operationResult': 'EXECUTED',
            'operationAmount': '1000',
            'operationTime': '2026-10-30T10:00:06',
        }
        data = {'operations': [self.CAPTURE, refund]}
        self.assertTrue(
            TRANSACTION._nexi_reversal_after_capture(data, self.CAPTURE))

    def test_reversal_before_capture_does_not_count(self):
        refund = {
            'operationType': 'REFUND',
            'operationResult': 'EXECUTED',
            'operationAmount': '1000',
            'operationTime': '2026-10-30T10:00:00',
        }
        data = {'operations': [refund, self.CAPTURE]}
        self.assertFalse(
            TRANSACTION._nexi_reversal_after_capture(data, self.CAPTURE))

    def test_reversal_without_dates_is_fail_closed(self):
        refund = {'operationType': 'REFUND', 'operationResult': 'EXECUTED'}
        data = {'operations': [self.CAPTURE, refund]}
        self.assertTrue(
            TRANSACTION._nexi_reversal_after_capture(data, self.CAPTURE))


class UiStringsTest(unittest.TestCase):
    """Shop-language shopper texts: one dict, one JSON copy, template keys."""

    def _strings(self):
        return TRANSACTION.PaymentTransactionNexi()._nexi_ui_strings()

    def test_every_string_is_non_empty(self):
        strings = self._strings()
        self.assertTrue(strings, 'the UI strings dict must not be empty')
        empty = sorted(k for k, v in strings.items() if not str(v or '').strip())
        self.assertEqual(empty, [], 'empty shopper texts: %s' % empty)

    def test_json_round_trips(self):
        model = TRANSACTION.PaymentTransactionNexi()
        self.assertEqual(
            json.loads(model._nexi_ui_strings_json()), model._nexi_ui_strings())

    def test_template_keys_exist(self):
        raw = (ADDON_ROOT / 'views' / 'payment_nexi_templates.xml').read_text(
            encoding='utf-8')
        used = set(re.findall(r"nexi_ui\['([a-z_]+)'\]", raw))
        used |= set(re.findall(r"T\('([a-z_]+)'", raw))
        self.assertTrue(used, 'no UI key found in the template')
        self.assertEqual(sorted(used - set(self._strings())), [])


if __name__ == '__main__':
    unittest.main()

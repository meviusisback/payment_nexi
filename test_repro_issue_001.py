# -*- coding: utf-8 -*-
"""Regression suite for issue #1 (pytest, senza runtime Odoo).

Carica i moduli con `odoo` finto e verifica la logica pura: whitelist host,
scelta dell'esito CAPTURE, separazione dei rate limit, importi di verifica e
classificazione degli errori. Il cablaggio ORM/QWeb è coperto da
`tests/test_issue_001.py` (gira su Odoo vero).
"""
import os
import sys
import types

REPO = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, REPO)

import odoo_stub  # noqa: E402

odoo_stub.install()
from odoo_stub import load_addon_module  # noqa: E402

try:
    import requests  # noqa: F401
except ImportError:
    import sys
    import types
    _rq = types.ModuleType('requests')
    _rq.request = lambda *a, **k: (_ for _ in ()).throw(
        AssertionError('requests stub: network calls must be mocked'))
    _rq.exceptions = types.SimpleNamespace(
        RequestException=Exception, HTTPError=Exception)
    sys.modules['requests'] = _rq

pp = load_addon_module(REPO, 'models.payment_provider')
pt = load_addon_module(REPO, 'models.payment_transaction')

# `controllers/main.py` importa il provider come in Odoo vero.
for _name in ('odoo.addons.payment_nexi', 'odoo.addons.payment_nexi.models'):
    _mod = types.ModuleType(_name)
    _mod.__path__ = []
    sys.modules[_name] = _mod
sys.modules['odoo.addons.payment_nexi.models.payment_provider'] = pp
sys.modules['odoo.addons.payment_nexi.models'].payment_provider = pp
main = load_addon_module(REPO, 'controllers.main')


def test_production_host_allowed_foreign_rejected():
    assert 'xpay.nexigroup.com' in pp.NEXI_HOSTS
    assert main._nexi_check_url(
        'https://xpay.nexigroup.com/api/phoenix-0.0/psp/api/v1/orders/hpp')
    assert main._nexi_check_url(
        'https://xpaysandbox.nexigroup.com/api/phoenix-0.0/psp/api/v1/orders/hpp')
    assert not main._nexi_check_url('https://evil.example.com/pay')
    assert not main._nexi_check_url(
        'https://xpay.nexigroup.com.evil.example.com/pay')
    assert not main._nexi_check_url(
        'http://xpay.nexigroup.com/api/phoenix-0.0/psp/api/v1/orders/hpp')
    assert not main._nexi_check_url('')


def _op(typ, res, t, amount='100'):
    return {'operationType': typ, 'operationResult': res,
            'operationTime': t, 'operationAmount': amount}


def test_outcome_prefers_latest_capture_over_auth():
    data = {'operations': [
        _op('AUTHORIZATION', 'AUTHORIZED', '2026-01-01T10:00:00'),
        _op('CAPTURE', 'EXECUTED', '2026-01-01T10:01:00'),
    ]}
    op = pt.PaymentTransactionNexi._nexi_last_outcome(None, data)
    assert (op['operationType'], op['operationResult']) == ('CAPTURE', 'EXECUTED')


def test_outcome_capture_only_is_seen():
    data = {'operations': [_op('CAPTURE', 'EXECUTED', '2026-01-01T10:00:00')]}
    op = pt.PaymentTransactionNexi._nexi_last_outcome(None, data)
    assert (op['operationType'], op['operationResult']) == ('CAPTURE', 'EXECUTED')


def test_outcome_ignores_unknown_types_and_empty():
    data = {'operations': [
        _op('VOID', 'EXECUTED', '2026-01-01T10:02:00'),
        _op('AUTHORIZATION', 'DENIED', '2026-01-01T10:01:00'),
    ]}
    op = pt.PaymentTransactionNexi._nexi_last_outcome(None, data)
    assert (op['operationType'], op['operationResult']) == ('AUTHORIZATION', 'DENIED')
    assert pt.PaymentTransactionNexi._nexi_last_outcome(None, {}) == {}
    assert pt.PaymentTransactionNexi._nexi_last_outcome(None, None) == {}


def test_amount_helpers():
    assert pt.to_cents(12.34) == 1234
    assert pt._parse_cents('100') == 100
    assert pt._parse_cents('10.5') is None
    assert pt._parse_cents('abc') is None
    assert pt.is_paid('EXECUTED')
    assert not pt.is_paid('AUTHORIZED')


def test_notify_and_browser_rate_limits_are_independent():
    scope_n, limit_n = main._nexi_confirm_scope(True)
    scope_c, limit_c = main._nexi_confirm_scope(False)
    assert scope_n != scope_c and limit_n > limit_c
    main._NEXI_RL.clear()
    for _ in range(limit_n):
        assert main._nexi_rl_allow('nexi:%s:9.9.9.9' % scope_n, limit_n)
    assert not main._nexi_rl_allow('nexi:%s:9.9.9.9' % scope_n, limit_n)
    assert main._nexi_rl_allow('nexi:%s:9.9.9.9' % scope_c, limit_c)


def test_check_amount_fits_paypal_paga_in_3_minimum():
    assert int(pp._nexi_check_amount_cents('PAYPAL_PAGAIN3')) >= 3000
    assert pp._nexi_check_amount_cents('CARDS') == '100'

def test_landing_route_policy():
    bill = '/my/invoices/42?tx_id=1'
    assert main._nexi_landing_route('/payment/status', True) == '/shop/payment/validate'
    assert main._nexi_landing_route('', True) == '/shop/payment/validate'
    assert main._nexi_landing_route(bill, False) == bill
    assert main._nexi_landing_route(bill, True) == bill
    assert main._nexi_landing_route('', False) == ''


def test_check_error_classification():
    assert pp._classify_nexi_check_error(
        pp.NexiApiError('k', status_code=401)) == 'config'
    assert pp._classify_nexi_check_error(
        pp.NexiApiError('k', status_code=403)) == 'config'
    assert pp._classify_nexi_check_error(
        pp.NexiApiError('not enabled', status_code=400)) == 'disabled'
    assert pp._classify_nexi_check_error(
        pp.NexiApiError('boom', status_code=500)) == 'unknown'
    assert pp._classify_nexi_check_error(
        pp.NexiApiError('slow', status_code=429)) == 'unknown'
    assert pp._classify_nexi_check_error(
        odoo_stub.ValidationError('Nexi non raggiungibile. Riprova.')) == 'unknown'
    assert pp._classify_nexi_check_error(
        odoo_stub.ValidationError('Nexi risposta non valida. Riprova.')) == 'unknown'

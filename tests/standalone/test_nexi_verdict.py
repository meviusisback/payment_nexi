# -*- coding: utf-8 -*-
"""Tests for the Nexi outcome policy, without Odoo and without a server.

They run on any machine with a plain python3:

    python3 -m unittest discover -s tests/standalone -t .

The cases come from the behaviour recorded on the live module before the policy
was moved into ``models/nexi_verdict.py``: same payloads, same verdicts.
"""
import importlib.util
import pathlib
import unittest

_MODULE_PATH = pathlib.Path(__file__).resolve().parents[2] / 'models' / 'nexi_verdict.py'
_spec = importlib.util.spec_from_file_location('nexi_verdict', _MODULE_PATH)
assert _spec and _spec.loader, 'nexi_verdict.py not found'
V = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(V)

ORDER = 'GOLDN1PAIDCAPTU'
AMOUNT = 1000  # EUR 10.00 in cents


def answer(captured, ops, order_ref=ORDER):
    """A GET /orders/{ref} answer, the way the gateway writes it."""
    data = {
        'orderId': order_ref,
        'orderStatus': {'order': {'orderId': order_ref}},
        'operations': [
            {
                'operationType': kind,
                'operationResult': result,
                'operationTime': when,
                'operationAmount': str(amount),
                'operationCurrency': currency,
                'paymentInstrumentInfo': '************1234',
            }
            for kind, result, when, amount, currency in ops
        ],
    }
    if captured is not None:
        data['orderStatus']['capturedAmount'] = str(captured)
    return data


def op(kind, result, when, amount='1000', currency='EUR'):
    return (kind, result, when, amount, currency)


def strip_op_amount(data):
    for item in data['operations']:
        item.pop('operationAmount', None)
    return data


def strip_amounts(data):
    data['orderStatus'].pop('capturedAmount', None)
    for item in data['operations']:
        item.pop('operationAmount', None)
        item.pop('capturedAmount', None)
    return data


def strip_order_id(data):
    data.pop('orderId', None)
    data['orderStatus']['order'].pop('orderId', None)
    return data


def strip_dates(data):
    for item in data['operations']:
        item.pop('operationTime', None)
    return data


def strip_reversal_amount(data):
    """Drop the figures of the REFUND/VOID operations only."""
    for item in data['operations']:
        if item['operationType'] in ('REFUND', 'VOID'):
            item.pop('operationAmount', None)
            item.pop('capturedAmount', None)
    return data


PAN = '************1234'

CASES = [
    # (name, payload, kind, reason, uncertain, captured, masked, retry)
    ('paid_capture', answer(1000, [op('CAPTURE', 'EXECUTED', '2026-11-14T10:00:05')]),
     V.DONE, V.PAID, False, 1000, PAN, False),
    ('auth_then_capture', answer(1000, [
        op('AUTHORIZATION', 'EXECUTED', '2026-11-14T10:00:00'),
        op('CAPTURE', 'EXECUTED', '2026-11-14T10:00:05')]),
     V.DONE, V.PAID, False, 1000, PAN, False),
    ('auth_only_no_capture', answer(None, [
        op('AUTHORIZATION', 'EXECUTED', '2026-11-14T10:00:00')]),
     V.PENDING, V.CAPTURE_WAIVED, False, None, None, False),
    ('auth_with_order_captured', answer(1000, [
        op('AUTHORIZATION', 'EXECUTED', '2026-11-14T10:00:00')]),
     V.DONE, V.PAID, False, 1000, PAN, False),
    ('authorized_result_only', answer(None, [
        op('AUTHORIZATION', 'AUTHORIZED', '2026-11-14T10:00:00')]),
     V.PENDING, V.AUTHORIZED, False, None, PAN, False),
    ('decimal_amount', answer('10.00', [
        op('CAPTURE', 'EXECUTED', '2026-11-14T10:00:05', amount='10.00')]),
     V.DONE, V.PAID, False, 1000, PAN, False),
    ('capture_no_op_amount', strip_op_amount(answer(1000, [
        op('CAPTURE', 'EXECUTED', '2026-11-14T10:00:05')])),
     V.DONE, V.PAID, False, 1000, PAN, False),
    ('declined_after_capture', answer(1000, [
        op('CAPTURE', 'EXECUTED', '2026-11-14T10:00:05'),
        op('AUTHORIZATION', 'DENIED_BY_RISK', '2026-11-14T10:01:00')]),
     V.DONE, V.ALREADY_CAPTURED, False, 1000, None, False),
    ('capture_partial', answer(900, [
        op('CAPTURE', 'EXECUTED', '2026-11-14T10:00:05', amount='900')]),
     V.ERROR, V.CAPTURE_INCOMPLETE, True, 900, None, False),
    ('capture_excess', answer(1200, [
        op('CAPTURE', 'EXECUTED', '2026-11-14T10:00:05', amount='1200')]),
     V.ERROR, V.AMOUNT_MISMATCH, False, 1200, None, False),
    ('declined_after_capture_excess', answer(1200, [
        op('CAPTURE', 'EXECUTED', '2026-11-14T10:00:05', amount='1200'),
        op('AUTHORIZATION', 'DENIED_BY_RISK', '2026-11-14T10:01:00')]),
     V.ERROR, V.AMOUNT_MISMATCH, False, 1200, None, False),
    ('capture_no_amounts', strip_amounts(answer(1000, [
        op('CAPTURE', 'EXECUTED', '2026-11-14T10:00:05')])),
     V.ERROR, V.CAPTURE_INCOMPLETE, True, None, None, False),
    ('wrong_currency', answer(1000, [
        op('CAPTURE', 'EXECUTED', '2026-11-14T10:00:05', currency='USD')]),
     V.ERROR, V.INVALID_CURRENCY, True, 1000, None, False),
    ('declined_auth', answer(None, [
        op('AUTHORIZATION', 'DENIED_BY_RISK', '2026-11-14T10:00:00')]),
     V.CANCEL, V.DECLINED, False, None, None, False),
    ('declined_capture', answer(None, [
        op('CAPTURE', 'DECLINED', '2026-11-14T10:00:00')]),
     V.CANCEL, V.DECLINED, False, 1000, None, False),
    ('refunded_after_capture', answer(1000, [
        op('CAPTURE', 'EXECUTED', '2026-11-14T10:00:05'),
        op('REFUND', 'EXECUTED', '2026-11-14T11:00:00')]),
     V.CANCEL, V.REVERSED, False, 1000, None, False),
    ('voided_after_capture', answer(1000, [
        op('CAPTURE', 'EXECUTED', '2026-11-14T10:00:05'),
        op('VOID', 'VOIDED', '2026-11-14T11:00:00')]),
     V.CANCEL, V.REVERSED, False, 1000, None, False),
    ('partial_refund_after_capture', answer(1000, [
        op('CAPTURE', 'EXECUTED', '2026-11-14T10:00:05'),
        op('REFUND', 'EXECUTED', '2026-11-14T11:00:00', amount='300')]),
     V.DONE, V.PAID, False, 1000, PAN, False),
    ('refund_without_amount', strip_reversal_amount(answer(1000, [
        op('CAPTURE', 'EXECUTED', '2026-11-14T10:00:05'),
        op('REFUND', 'EXECUTED', '2026-11-14T11:00:00')])),
     V.CANCEL, V.REVERSED, False, 1000, None, False),
    ('refund_hidden_by_later_declined_capture', answer(1000, [
        op('CAPTURE', 'EXECUTED', '2026-11-14T10:00:05'),
        op('REFUND', 'EXECUTED', '2026-11-14T11:00:00'),
        op('CAPTURE', 'DECLINED', '2026-11-14T12:00:00')]),
     V.CANCEL, V.DECLINED, False, 1000, None, False),
    ('negative_refund', answer(1000, [
        op('CAPTURE', 'EXECUTED', '2026-11-14T10:00:05'),
        op('REFUND', 'EXECUTED', '2026-11-14T11:00:00', amount='-1000')]),
     V.CANCEL, V.REVERSED, False, 1000, None, False),
    ('partial_refund_then_decline', answer(1000, [
        op('CAPTURE', 'EXECUTED', '2026-11-14T10:00:05'),
        op('REFUND', 'EXECUTED', '2026-11-14T11:00:00', amount='300'),
        op('AUTHORIZATION', 'DENIED_BY_RISK', '2026-11-14T12:00:00')]),
     V.DONE, V.ALREADY_CAPTURED, False, 1000, None, False),
    ('capture_without_amount_then_decline', answer(None, [
        op('CAPTURE', 'EXECUTED', '2026-11-14T10:00:05', amount=''),
        op('AUTHORIZATION', 'DENIED_BY_RISK', '2026-11-14T12:00:00')]),
     V.ERROR, V.CAPTURE_INCOMPLETE, True, None, None, False),
    ('capture_pending', answer(None, [
        op('CAPTURE', 'PENDING', '2026-11-14T10:00:05')]),
     V.PENDING, V.SCHEME_PENDING, False, 1000, None, True),
    ('threeds_validated', answer(None, [
        op('AUTHORIZATION', 'THREEDS_VALIDATED', '2026-11-14T10:00:00')]),
     V.PENDING, V.SCHEME_PENDING, False, None, None, True),
    ('wrong_order_id', dict(answer(1000, [
        op('CAPTURE', 'EXECUTED', '2026-11-14T10:00:05')]), orderId='OTHERORDER1'),
     V.ERROR, V.INVALID_ORDER, True, 1000, None, False),
    ('no_order_id', strip_order_id(answer(1000, [
        op('CAPTURE', 'EXECUTED', '2026-11-14T10:00:05')])),
     V.ERROR, V.INVALID_ORDER, True, 1000, None, False),
    ('auth_capture_disagree_no_dates', strip_dates(answer(1000, [
        op('AUTHORIZATION', 'DENIED_BY_RISK', '2026-11-14T10:00:00'),
        op('CAPTURE', 'EXECUTED', '2026-11-14T10:00:05')])),
     V.PENDING, V.UNDECIDED, False, 1000, None, False),
    ('empty_payload', {}, V.ERROR, V.INVALID_ORDER, True, None, None, False),
    ('not_a_dict', ['altered'], V.ERROR, V.INVALID_ORDER, True, None, None, False),
]


class VerdictCasesTest(unittest.TestCase):
    """The recorded outcomes, replayed on the pure policy."""

    def test_recorded_outcomes(self):
        for (name, payload, kind, reason, uncertain, captured,
             masked, retry) in CASES:
            with self.subTest(case=name):
                got = V.verdict(ORDER, payload, AMOUNT, 'EUR')
                self.assertEqual(got.kind, kind, 'kind')
                self.assertEqual(got.reason, reason, 'reason')
                self.assertEqual(got.uncertain, uncertain, 'uncertain')
                self.assertEqual(got.captured_cents, captured, 'captured cents')
                self.assertEqual(got.masked_pan, masked, 'masked pan')
                self.assertEqual(got.retry, retry, 'retry')

    def test_every_reason_is_known(self):
        known = {
            V.PAID, V.ALREADY_CAPTURED, V.CAPTURE_WAIVED, V.AUTHORIZED,
            V.UNDECIDED, V.SCHEME_PENDING, V.DECLINED, V.REVERSED,
            V.INVALID_ORDER, V.INVALID_CURRENCY, V.CAPTURE_INCOMPLETE,
            V.AMOUNT_MISMATCH,
        }
        for (name, payload, *_rest) in CASES:
            with self.subTest(case=name):
                self.assertIn(V.verdict(ORDER, payload, AMOUNT, 'EUR').reason, known)

    def test_only_pending_is_retried(self):
        for (name, payload, *_rest) in CASES:
            with self.subTest(case=name):
                got = V.verdict(ORDER, payload, AMOUNT, 'EUR')
                if got.retry:
                    self.assertEqual(got.kind, V.PENDING)

    def test_uncertain_only_on_error(self):
        """The cron flag is written on errors only: a pending is retried by state."""
        for (name, payload, *_rest) in CASES:
            with self.subTest(case=name):
                got = V.verdict(ORDER, payload, AMOUNT, 'EUR')
                if got.uncertain:
                    self.assertEqual(got.kind, V.ERROR)


class AmountsTest(unittest.TestCase):
    """The arithmetic that decides whether money is in."""

    def test_cents_from_euro(self):
        self.assertEqual(V.to_cents(10.0), 1000)
        # Decimal rounds half-to-even: 12.345 * 100 = 1234.5 -> 1234.
        self.assertEqual(V.to_cents('12.345'), 1234)
        self.assertEqual(V.to_cents('Infinity'), 0)
        self.assertEqual(V.to_cents(None), 0)

    def test_integer_cents(self):
        self.assertEqual(V.parse_cents('1000'), 1000)
        self.assertEqual(V.parse_cents(1000), 1000)

    def test_decimal_is_euro(self):
        self.assertEqual(V.parse_cents('10.00'), 1000)
        self.assertEqual(V.parse_cents('10.5'), 1050)
        self.assertEqual(V.parse_cents('1e2'), 10000)

    def test_junk_is_none(self):
        for value in (None, '', 'abc', 'Infinity', 'NaN', '10.005.1',
                      '1e1000000', '1e999999999', '9' * 40):
            with self.subTest(value=value):
                self.assertIsNone(V.parse_cents(value))

    def test_currency_mixing_is_refused(self):
        data = answer(1000, [op('CAPTURE', 'EXECUTED', '2026-11-14T10:00:05')])
        data['orderStatus'].pop('capturedAmount')
        data['operations'][0]['operationAmount'] = '500'
        data['operations'].append(  # a capture in another currency is not summed
            {
                'operationType': 'CAPTURE', 'operationResult': 'EXECUTED',
                'operationAmount': '500', 'operationCurrency': 'USD',
            })
        self.assertEqual(V.capture_total(data, None, currency='EUR'), 500)

    def test_only_executed_captures_are_summed(self):
        data = answer(None, [
            op('CAPTURE', 'EXECUTED', '2026-11-14T10:00:05', amount='400'),
            op('CAPTURE', 'DECLINED', '2026-11-14T10:00:06', amount='600'),
        ])
        self.assertEqual(V.capture_total(data, None, currency='EUR'), 400)

    def test_authorization_is_not_a_capture(self):
        data = answer(None, [op('AUTHORIZATION', 'EXECUTED', '2026-11-14T10:00:00')])
        self.assertIsNone(V.capture_total(data, None, currency='EUR'))


class OperationChoiceTest(unittest.TestCase):
    """Which operation decides, when the gateway sends more than one."""

    def test_capture_after_authorization_decides(self):
        auth = op('AUTHORIZATION', 'EXECUTED', '2026-11-14T10:00:00')
        capture = op('CAPTURE', 'EXECUTED', '2026-11-14T10:00:05')
        data = answer(1000, [auth, capture])
        _op, kind = V.decide_operation(data)
        self.assertEqual(kind, 'capture')

    def test_dates_are_read_as_dates(self):
        """The latest operation wins by date, not by string order."""
        early = op('CAPTURE', 'EXECUTED', '2026-11-14T09:00:00')
        late = op('AUTHORIZATION', 'DENIED_BY_RISK', '2026-11-14T10:00:00')
        data = answer(1000, [early, late])
        _op, kind = V.decide_operation(data)
        self.assertEqual(kind, 'auth')

    def test_unreadable_date_counts_as_no_date(self):
        """'T9:00' is not an ISO time: unreadable, so never trusted."""
        early = op('CAPTURE', 'EXECUTED', '2026-11-14T9:00:00')
        late = op('AUTHORIZATION', 'DENIED_BY_RISK', '2026-11-14T10:00:00')
        data = answer(1000, [early, late])
        _op, kind = V.decide_operation(data)
        self.assertEqual(kind, 'uncertain')

    def test_disagreement_without_dates_is_uncertain(self):
        data = strip_dates(answer(1000, [
            op('AUTHORIZATION', 'DENIED_BY_RISK', '2026-11-14T10:00:00'),
            op('CAPTURE', 'EXECUTED', '2026-11-14T10:00:05'),
        ]))
        _op, kind = V.decide_operation(data)
        self.assertEqual(kind, 'uncertain')

    def test_agreement_without_dates_is_not_uncertain(self):
        data = strip_dates(answer(1000, [
            op('CAPTURE', 'EXECUTED', '2026-11-14T10:00:05'),
        ]))
        _op, kind = V.decide_operation(data)
        self.assertEqual(kind, 'capture')

    def test_hostile_shapes_do_not_raise(self):
        for data in (None, [], 'x', {'operations': 'nope'}, {'operations': [1, None]}):
            with self.subTest(data=data):
                self.assertEqual(V.decide_operation(data)[1], 'capture')
                self.assertEqual(V.got_order_id(data), '')


class ReversalTest(unittest.TestCase):
    """A capture already executed survives a later decline, not a reversal."""

    def test_reversal_after_capture_counts(self):
        data = answer(1000, [
            op('CAPTURE', 'EXECUTED', '2026-11-14T10:00:05'),
            op('REFUND', 'EXECUTED', '2026-11-14T11:00:00'),
        ])
        capture = V.latest_operation(data, V.CAPTURE_OPS)
        self.assertEqual(V.reversed_after_capture(data, capture, 'EUR'), 1000)

    def test_partial_reversal_is_measured(self):
        data = answer(1000, [
            op('CAPTURE', 'EXECUTED', '2026-11-14T10:00:05'),
            op('REFUND', 'EXECUTED', '2026-11-14T11:00:00', amount='300'),
        ])
        capture = V.latest_operation(data, V.CAPTURE_OPS)
        self.assertEqual(V.reversed_after_capture(data, capture, 'EUR'), 300)

    def test_decline_after_capture_is_not_a_reversal(self):
        data = answer(1000, [
            op('CAPTURE', 'EXECUTED', '2026-11-14T10:00:05'),
            op('AUTHORIZATION', 'DENIED_BY_RISK', '2026-11-14T10:01:00'),
        ])
        capture = V.latest_operation(data, V.CAPTURE_OPS)
        self.assertEqual(V.reversed_after_capture(data, capture, 'EUR'), 0)

    def test_reversal_without_dates_is_assumed(self):
        """Without dates the reversal cannot be excluded: it is counted."""
        data = strip_dates(answer(1000, [
            op('CAPTURE', 'EXECUTED', '2026-11-14T10:00:05'),
            op('VOID', 'VOIDED', '2026-11-14T11:00:00'),
        ]))
        capture = V.latest_operation(data, V.CAPTURE_OPS)
        self.assertEqual(V.reversed_after_capture(data, capture, 'EUR'), 1000)

    def test_reversal_without_amount_is_unknown(self):
        data = strip_reversal_amount(answer(1000, [
            op('CAPTURE', 'EXECUTED', '2026-11-14T10:00:05'),
            op('REFUND', 'EXECUTED', '2026-11-14T11:00:00'),
        ]))
        capture = V.latest_operation(data, V.CAPTURE_OPS)
        self.assertIsNone(V.reversed_after_capture(data, capture, 'EUR'))

    def test_reversal_in_another_currency_is_unknown(self):
        data = answer(1000, [
            op('CAPTURE', 'EXECUTED', '2026-11-14T10:00:05'),
            op('REFUND', 'EXECUTED', '2026-11-14T11:00:00', currency='USD'),
        ])
        capture = V.latest_operation(data, V.CAPTURE_OPS)
        self.assertIsNone(V.reversed_after_capture(data, capture, 'EUR'))


class RefundableOperationTest(unittest.TestCase):
    """Which operation a refund must address, and how much is left to give back.

    The figures here are the ones that decide whether money can leave twice:
    what was captured minus what was already given back.
    """

    def test_capture_is_the_operation_to_refund(self):
        """A refund is addressed to the CAPTURE, not to the order."""
        data = answer(1000, [
            op('AUTHORIZATION', 'EXECUTED', '2026-11-14T10:00:00'),
            op('CAPTURE', 'EXECUTED', '2026-11-14T10:00:05'),
        ])
        operation, available = V.refundable_operation(data, ORDER, 'EUR')
        self.assertEqual(operation.get('operationType'), 'CAPTURE')
        self.assertEqual(available, 1000)

    def test_authorization_only_is_not_refundable(self):
        """An authorization is not money taken: giving it back is a void."""
        data = answer(None, [
            op('AUTHORIZATION', 'EXECUTED', '2026-11-14T10:00:00'),
        ])
        operation, available = V.refundable_operation(data, ORDER, 'EUR')
        self.assertEqual(operation, {})
        self.assertEqual(available, 0)

    def test_partial_refund_leaves_the_rest(self):
        data = answer(1000, [
            op('CAPTURE', 'EXECUTED', '2026-11-14T10:00:05'),
            op('REFUND', 'EXECUTED', '2026-11-14T11:00:00', amount='300'),
        ])
        _operation, available = V.refundable_operation(data, ORDER, 'EUR')
        self.assertEqual(available, 700)

    def test_full_refund_leaves_nothing(self):
        """Nothing left: the caller must refuse instead of refunding again."""
        data = answer(1000, [
            op('CAPTURE', 'EXECUTED', '2026-11-14T10:00:05'),
            op('REFUND', 'VOIDED', '2026-11-14T11:00:00'),
        ])
        _operation, available = V.refundable_operation(data, ORDER, 'EUR')
        self.assertEqual(available, 0)

    def test_unsizable_reversal_is_none_not_zero(self):
        """A reversal whose amount cannot be read is unknown, never 'all free'."""
        data = strip_reversal_amount(answer(1000, [
            op('CAPTURE', 'EXECUTED', '2026-11-14T10:00:05'),
            op('REFUND', 'EXECUTED', '2026-11-14T11:00:00'),
        ]))
        _operation, available = V.refundable_operation(data, ORDER, 'EUR')
        self.assertIsNone(available)

    def test_another_order_is_refused(self):
        data = answer(1000, [
            op('CAPTURE', 'EXECUTED', '2026-11-14T10:00:05'),
        ], order_ref='SOMEONE-ELSE')
        operation, available = V.refundable_operation(data, ORDER, 'EUR')
        self.assertEqual(operation, {})
        self.assertEqual(available, 0)

    def test_no_order_id_is_refused(self):
        data = strip_order_id(answer(1000, [
            op('CAPTURE', 'EXECUTED', '2026-11-14T10:00:05'),
        ]))
        operation, available = V.refundable_operation(data, ORDER, 'EUR')
        self.assertEqual(operation, {})
        self.assertEqual(available, 0)

    def test_declined_capture_is_not_refundable(self):
        """A capture that never executed took no money: nothing to give back."""
        data = answer(None, [
            op('CAPTURE', 'CANCELED', '2026-11-14T10:00:05'),
        ])
        operation, available = V.refundable_operation(data, ORDER, 'EUR')
        self.assertEqual(operation, {})
        self.assertEqual(available, 0)

    def test_hostile_shapes_do_not_raise(self):
        for data in ({}, [], 'x', 42, None, {'operations': 'nope'}):
            operation, available = V.refundable_operation(data, ORDER, 'EUR')
            self.assertEqual(operation, {})
            self.assertEqual(available, 0)


if __name__ == '__main__':
    unittest.main()

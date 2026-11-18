# -*- coding: utf-8 -*-
"""What a Nexi answer means, with no Odoo in it.

The gateway answers with an order and a list of operations. Deciding whether
the money is in is not a matter of reading one field: the currency must match,
the captured figure must cover the expected one, a capture already executed
survives a later decline but not a reversal that gives it back, an
authorization without capture is not a closed payment, and an answer the
gateway itself is unsure about must never close anything.

This module owns that policy. Given the answer, the expected figure in cents
and the currency, it returns a :class:`Verdict`: the outcome, why, whether the
cron should ask again, whether a human has to look, and the figures that go
with it. It touches no database, no network and no translation: the model
applies the verdict and turns the reason into a message the customer can read.

Kept free of ``odoo`` imports on purpose: the policy is the part that decides
about money, so it must be testable with a plain python and a dict.
"""
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

# Nexi outcomes. EXECUTED = paid (automatic). AUTHORIZED = only blocked.
OK_RESULTS = {'EXECUTED'}
AUTH_RESULTS = {'AUTHORIZED'}
# THREEDS_VALIDATED = 3DS ok but payment still running: wait.
WAIT_RESULTS = {'PENDING', 'AUTH-REQUEST', 'AUTHORIZED_PENDING', 'THREEDS_VALIDATED'}
# VOIDED = reversal: nothing captured. REFUNDED = refund: nothing captured.
KO_RESULTS = {'CANCELED', 'THREEDS_FAILED', 'DENIED', 'DENIED_BY_RISK', 'ERROR',
              'DECLINED', 'FAILED', 'VOIDED', 'REFUNDED'}

# Operations that decide how it went. With automatic capture Nexi returns
# AUTHORIZATION *and* CAPTURE: looking only at the authorization leaves the
# payment hanging even when the money was already taken.
AUTH_OPS = {'AUTHORIZATION'}
CAPTURE_OPS = {'CAPTURE'}

# Outcomes the model applies.
DONE = 'done'
PENDING = 'pending'
CANCEL = 'cancel'
ERROR = 'error'

# Why. The model turns these into the message the customer reads.
PAID = 'paid'                          # captured, figures match: closed
ALREADY_CAPTURED = 'already_captured'  # a later decline cannot undo the capture
CAPTURE_WAIVED = 'capture_waived'      # authorized, no capture evidence yet
AUTHORIZED = 'authorized'              # blocked only: no money taken
UNDECIDED = 'undecided'                # auth and capture disagree, no dates
SCHEME_PENDING = 'scheme_pending'      # the gateway has not decided yet
DECLINED = 'declined'
REVERSED = 'reversed'                  # the capture was given back: money is not in
INVALID_ORDER = 'invalid_order'        # not the order we asked for
INVALID_CURRENCY = 'invalid_currency'
CAPTURE_INCOMPLETE = 'capture_incomplete'
AMOUNT_MISMATCH = 'amount_mismatch'    # more money than expected: a human

# Reasons that leave the transaction to the cron: it rechecks and tries again.
# A wrong figure or a currency mismatch is not one of them: asking twice does
# not change the answer, and a human has to look anyway.
UNCERTAIN_REASONS = frozenset({
    INVALID_ORDER, INVALID_CURRENCY, CAPTURE_INCOMPLETE,
})


@dataclass(frozen=True)
class Verdict:
    """What the gateway answer means, in one object."""

    kind: str                 # DONE | PENDING | CANCEL | ERROR
    reason: str               # one of the reason constants above
    expected_cents: int
    captured_cents: int | None = None    # what the gateway says it took, in cents
    decided_by: str = 'none'             # 'auth' | 'capture' | 'none'
    operation_result: str = ''           # the gateway vocabulary, for the log fields
    masked_pan: str | None = None        # raw masked figure as Nexi writes it
    retry: bool = False                  # ask the gateway again in this same pass

    @property
    def uncertain(self):
        """True when the cron owns the follow-up (never guessed, never closed)."""
        return self.reason in UNCERTAIN_REASONS


def to_cents(amount):
    """Euro -> integer cents. Example: 12.34 -> 1234."""
    try:
        value = Decimal(str(amount))
        if not value.is_finite():
            return 0
        return int((value * 100).to_integral_value())
    except (ArithmeticError, ValueError, TypeError):
        # ArithmeticError covers decimal.InvalidOperation and decimal.Overflow,
        # which is NOT an OverflowError.
        return 0


def is_paid(operation_result):
    return operation_result in OK_RESULTS


# Money figures above this are not amounts (10^12 cents = 10 billion EUR) and
# are what a malformed answer uses to blow up Decimal: refused before use.
MAX_CENTS = 10 ** 12


def parse_cents(value):
    """Integer cents from a Nexi response.

    XPay writes the amounts in integer cents (``"1000"`` = EUR 10.00), but some
    flows send a decimal (``"10.00"``): the text with a dot is read as euro and
    converted with Decimal (never float), otherwise a real capture would end up
    in error. ``None`` if it is not a number.
    """
    try:
        text = str(value).strip()
        d = Decimal(text)
    except (ArithmeticError, ValueError, TypeError):
        return None
    # Decimal accepts 'Infinity' and 'NaN': they are not amounts, and int() on
    # Infinity would raise OverflowError up to a 500.
    if not d.is_finite():
        return None
    # '1e1000000' is finite but its *100 overflows the decimal context with an
    # exception the route does not catch: refuse it before converting.
    if d.adjusted() > 15:
        return None
    if '.' in text or 'e' in text.lower():
        try:
            got = int((d * 100).to_integral_value())
        except (ArithmeticError, ValueError, TypeError):
            return None
    else:
        try:
            if d != d.to_integral_value():
                return None
            got = int(d)
        except (ArithmeticError, ValueError, TypeError):
            return None
    return got if abs(got) <= MAX_CENTS else None


def op_time(op):
    """Nexi operationTime as a datetime, or None if missing/unreadable."""
    op = op if isinstance(op, dict) else {}
    raw = str(op.get('operationTime') or '').strip()
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace('Z', '+00:00'))
    except ValueError:
        return None


def op_ts(op):
    """operationTime as a number (for sorting), or None."""
    dt = op_time(op)
    if dt is None:
        return None
    try:
        return dt.timestamp()
    except (OverflowError, OSError, ValueError):
        return None


def latest_operation(data, types, results=None):
    """Latest operation of the given type, by date, in the Nexi response.

    With ``results`` given, only operations whose result is in that set count
    (e.g. the latest CAPTURE that really executed).
    """
    data = data if isinstance(data, dict) else {}
    operations = data.get('operations')
    ops = [
        o for o in (operations if isinstance(operations, list) else [])
        if isinstance(o, dict) and str(o.get('operationType') or '').upper() in types
        and (results is None
             or str(o.get('operationResult') or '').upper() in results)
    ]
    if not ops:
        return {}
    # Sort on the parsed date, not on the string: 'T9:00' would come after
    # 'T10:00' in a lexicographic comparison.
    return max(ops, key=lambda o: (
        op_ts(o) is not None,
        op_ts(o) or 0.0,
        str(o.get('operationTime') or ''),
    ))


def decide_operation(data):
    """The operation that decides the outcome: the latest of auth and capture.

    :return: (operation, 'auth' | 'capture' | 'uncertain'). Empty operation if
             Nexi has not returned any yet; 'uncertain' if AUTH and CAPTURE
             disagree but the dates do not say which is the latest (the caller
             must stay pending, never close).
    """
    auth = latest_operation(data, AUTH_OPS)
    capture = latest_operation(data, CAPTURE_OPS)
    if not auth:
        return capture, 'capture'
    if not capture:
        return auth, 'auth'
    auth_paid = is_paid(str(auth.get('operationResult') or '').upper())
    capture_paid = is_paid(str(capture.get('operationResult') or '').upper())
    auth_ts, capture_ts = op_ts(auth), op_ts(capture)
    if auth_ts is None or capture_ts is None:
        # Uncertain date: if they agree (both paid or both unpaid) the
        # authorization decides; if they disagree the latest is unknown:
        # uncertain, never close.
        if auth_paid == capture_paid:
            return auth, 'auth'
        return {}, 'uncertain'
    if capture_ts > auth_ts:
        return capture, 'capture'
    if capture_ts < auth_ts:
        return auth, 'auth'
    # Same instant: if they disagree, uncertain as above.
    if auth_paid == capture_paid:
        return capture, 'capture'
    return {}, 'uncertain'


def got_order_id(data):
    """orderId from the Nexi response, also in nested/hostile shapes."""
    data = data if isinstance(data, dict) else {}
    got = data.get('orderId')
    if not got:
        status = data.get('orderStatus')
        if isinstance(status, dict):
            order = status.get('order')
            if isinstance(order, dict):
                got = order.get('orderId')
    return got or ''


def capture_total(data, op, currency=None):
    """How much Nexi says it captured, in cents.

    First the order-level total (``orderStatus.capturedAmount``): it is the only
    one that says how much was really taken. The single-operation figures count
    only when the total is missing; as a last resort the captures in
    ``operations[]`` are summed. ``None`` if there is no number anywhere. With
    ``currency`` given, items in another currency are not summed (never mix
    currencies).
    """
    data = data if isinstance(data, dict) else {}
    status = data.get('orderStatus')
    status = status if isinstance(status, dict) else {}
    cap = status.get('capturedAmount')
    if cap not in (None, ''):
        got = parse_cents(cap)
        if got is not None:
            return got
    op = op if isinstance(op, dict) else {}
    # Only a CAPTURE carries a capture: the amount of an AUTHORIZATION is an
    # authorization, not money taken.
    if str(op.get('operationType') or '').upper() == 'CAPTURE':
        for raw in (op.get('operationAmount'), op.get('capturedAmount')):
            got = parse_cents(raw) if raw not in (None, '') else None
            if got is not None:
                return got
    total = 0
    found = False
    operations = data.get('operations')
    for item in (operations if isinstance(operations, list) else []):
        if not isinstance(item, dict):
            continue
        if str(item.get('operationType') or '').upper() != 'CAPTURE':
            continue
        if not is_paid(str(item.get('operationResult') or '').upper()):
            continue
        if currency and (item.get('operationCurrency') or '') != currency:
            continue
        got = parse_cents(item.get('operationAmount'))
        if got is None:
            got = parse_cents(item.get('capturedAmount'))
        if got is not None:
            total += got
            found = True
    return total if found else None


def reversed_after_capture(data, capture_op, currency=None):
    """How much of the capture was given back after it, in cents.

    A VOID or a REFUND that arrived after the capture undoes it: one that
    covers the whole capture means the money is not in, and one that cannot be
    sized must be treated the same way (fail-closed). A partial refund leaves
    the payment standing. Without dates the reversal cannot be excluded.

    :return: 0 when there is no reversal; the summed cents of the reversals
             after the capture; ``None`` when a reversal exists but its amount
             or currency cannot be read.
    """
    data = data if isinstance(data, dict) else {}
    capture_ts = op_ts(capture_op) if capture_op else None
    operations = data.get('operations')
    total = 0
    for item in (operations if isinstance(operations, list) else []):
        if not isinstance(item, dict):
            continue
        kind = str(item.get('operationType') or '').upper()
        result = str(item.get('operationResult') or '').upper()
        reversal_result = result in ('VOIDED', 'REFUNDED')
        reversal_kind = kind in ('VOID', 'REFUND', 'CANCELLATION', 'CANCEL_ORDER')
        if not (reversal_result or reversal_kind):
            continue
        if kind in ('AUTHORIZATION', 'CAPTURE') and not reversal_result:
            continue
        ts = op_ts(item)
        if not (capture_ts is None or ts is None or ts >= capture_ts):
            continue
        got_cur = item.get('operationCurrency')
        if currency and got_cur and got_cur != currency:
            # Another currency: it cannot be netted against this capture.
            return None
        amount = parse_cents(item.get('operationAmount'))
        if amount is None:
            amount = parse_cents(item.get('capturedAmount'))
        if amount is None or amount <= 0:
            # A reversal with no readable (or non-positive) figure: the capture
            # cannot be considered live.
            return None
        total += amount
    return total


def refundable_operation(data, order_ref, currency=None):
    """The operation a refund must reference, and how much can still go back.

    Nexi refunds are addressed to the operation that took the money
    (``POST /operations/{operationId}/refunds``), not to the order: the order is
    only read here to work out what is left. That operation is the CAPTURE; an
    AUTHORIZATION is not money taken, so it is not refundable and giving it back
    is a void, a different call the caller decides about.

    :param data: the gateway answer about ``order_ref``.
    :param order_ref: the order this must be about: an answer about another
                      order (or with no id) is not trusted.
    :param currency: the transaction currency, so a refund in another currency
                     cannot be netted against this capture.
    :return: ``(operation, available_cents)``. ``({}, 0)`` when nothing was
             taken or everything was already given back, so the caller refuses
             instead of sending a refund that cannot be honoured.
             ``(operation, None)`` when a reversal exists but cannot be sized:
             fail-closed, the caller must not refund on a guess.
    """
    data = data if isinstance(data, dict) else {}
    if not got_order_id(data) or got_order_id(data) != order_ref:
        return {}, 0
    operation = latest_operation(data, CAPTURE_OPS, OK_RESULTS)
    operation = operation if isinstance(operation, dict) else {}
    if not operation:
        return {}, 0
    taken = capture_total(data, operation, currency=currency)
    if not taken or taken <= 0:
        return {}, 0
    given_back = reversed_after_capture(data, operation, currency=currency)
    if given_back is None:
        # A reversal is already there and its amount cannot be read: what is
        # left to give back is unknown, and guessing would refund twice.
        return operation, None
    available = taken - given_back
    return operation, (available if available > 0 else 0)


def _masked_pan(op):
    op = op if isinstance(op, dict) else {}
    return (op.get('paymentInstrumentInfo')
            or op.get('maskedPan')
            or op.get('maskedPanAlias')
            or None)


def verdict(order_ref, data, expected_cents, currency):
    """Decide the outcome of a Nexi order.

    :param order_ref: the order this transaction asked about: the answer must
                      talk about this one, or it is not trusted.
    :param data: the gateway answer (any shape: a hostile one is not trusted).
    :param expected_cents: what the transaction expects, in integer cents.
    :param currency: the currency name the transaction is in.
    :return: a :class:`Verdict`.
    """
    data = data if isinstance(data, dict) else {}
    op, decided_by = decide_operation(data)
    op = op if isinstance(op, dict) else {}
    # The figures of the operation that decided, as the diagnostic fields want
    # them: written even when the answer turns out to be unusable.
    captured = capture_total(data, op, currency=currency)
    got_order = got_order_id(data)
    if not got_order or got_order != order_ref:
        # The order to verify is decided by the transaction; an answer about
        # another order (or with no id at all) cannot be read: fail-closed.
        return Verdict(ERROR, INVALID_ORDER, expected_cents, captured,
                       'none', '', None)
    result = str(op.get('operationResult') or '').upper()
    if decided_by == 'uncertain':
        # AUTHORIZATION and CAPTURE disagree and the dates do not say which is
        # the latest: never close, never decide.
        return Verdict(PENDING, UNDECIDED, expected_cents, captured,
                       'uncertain', '', None)
    if is_paid(result):
        return _paid(data, op, decided_by, result, expected_cents, currency,
                     captured)
    if result in AUTH_RESULTS:
        # Authorized but not captured: the card is blocked, no money moved.
        auth_op = latest_operation(data, AUTH_OPS)
        return Verdict(PENDING, AUTHORIZED, expected_cents, captured, 'auth',
                       result, _masked_pan(auth_op))
    if result in KO_RESULTS:
        return _declined(data, result, expected_cents, currency, captured)
    # The gateway has not decided yet: the caller may ask again in this pass.
    return Verdict(PENDING, SCHEME_PENDING, expected_cents, captured,
                   decided_by, result, None,
                   retry=not result or result in WAIT_RESULTS)


def _paid(data, op, decided_by, result, expected_cents, currency, captured):
    """The operation says paid: check the figures before closing anything."""
    got_cur = op.get('operationCurrency')
    if not got_cur or got_cur != currency:
        # Currency mismatch: money may have moved in another currency. Never
        # close on a guess.
        return Verdict(ERROR, INVALID_CURRENCY, expected_cents, captured,
                       decided_by, result, None)
    amount_raw = op.get('operationAmount')
    if amount_raw in (None, ''):
        # On the capture the amount can be in capturedAmount.
        amount_raw = op.get('capturedAmount')
    got = parse_cents(amount_raw)
    masked = _masked_pan(op)
    # The amount that counts is the captured one: order total, operation figures
    # or the sum of the captures in the same currency. If the operation that
    # decided carries less than the total, the lower one wins: it closes only
    # when every number seen covers the expected one.
    total = capture_total(data, op, currency=currency)
    if decided_by != 'capture' and (total is None or total <= 0):
        # Authorization without capture evidence: not a closed payment. It
        # stays pending, the cron rechecks it.
        return Verdict(PENDING, CAPTURE_WAIVED, expected_cents, captured,
                       decided_by, result, None)
    if total is None:
        total = got
    if got is not None and total is not None and got < total:
        total = got
    if total is None or total < expected_cents:
        # Captured less than the order (or nothing readable): not closed, the
        # cron tries again.
        return Verdict(ERROR, CAPTURE_INCOMPLETE, expected_cents, captured,
                       decided_by, result, None)
    if total != expected_cents:
        # Captured more than the order (e.g. a stale session paid twice). It is
        # not closed without a ceiling: a human refunds it from the Nexi
        # back office. Asking again would not change the answer.
        return Verdict(ERROR, AMOUNT_MISMATCH, expected_cents, captured,
                       decided_by, result, None)
    returned = reversed_after_capture(
        data, latest_operation(data, CAPTURE_OPS, OK_RESULTS),
        currency=currency)
    if returned is None or returned >= total:
        # The capture was given back, or the reversal cannot be sized: the
        # money is not in. The verdict cannot depend on whether a later
        # attempt happened to be declined, so this is decided here too.
        return Verdict(CANCEL, REVERSED, expected_cents, captured,
                       decided_by, result, None)
    return Verdict(DONE, PAID, expected_cents, captured, decided_by, result,
                   masked)


def _declined(data, result, expected_cents, currency, captured):
    """The operation says no: check whether the money was taken anyway."""
    # Here only the captures really executed count: a declined CAPTURE carries
    # an amount that was never taken.
    taken = capture_total(data, None, currency=currency)
    # The reversal window is anchored to the last capture that really took
    # money: a later *declined* capture must not hide a refund that came
    # between the two.
    capture_op = latest_operation(data, CAPTURE_OPS, OK_RESULTS)
    executed_capture = bool(capture_op)
    returned = reversed_after_capture(data, capture_op, currency=currency)
    # A partial refund leaves the capture standing, a full (or unsizable) one
    # does not: the same rule the paid path uses, so the outcome cannot depend
    # on whether a later attempt happened to be declined.
    live_capture = (
        returned is not None and taken is not None and returned < taken)
    if result not in ('REFUNDED', 'VOIDED'):
        if taken is None and executed_capture:
            # A capture says EXECUTED but its figure cannot be read: the money
            # may be in. Never a silent cancel, the cron rechecks and then a
            # human looks (same policy as the paid path).
            return Verdict(ERROR, CAPTURE_INCOMPLETE, expected_cents, captured,
                           'capture', result, None)
        if taken is not None and live_capture:
            if taken == expected_cents:
                # Capture already executed: a later decline (a new attempt of
                # the same order) does not undo the money taken.
                return Verdict(DONE, ALREADY_CAPTURED, expected_cents, captured,
                               'capture', result, None)
            if taken > expected_cents:
                return Verdict(ERROR, AMOUNT_MISMATCH, expected_cents, captured,
                               'capture', result, None)
    return Verdict(CANCEL, DECLINED, expected_cents, captured, 'capture',
                   result, None)

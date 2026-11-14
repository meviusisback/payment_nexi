# -*- coding: utf-8 -*-
"""Nexi XPay Web transaction: order creation, Hosted Page redirect, Build fields and outcome verification."""
import json
import logging
import re
import secrets
import time
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from urllib.parse import parse_qsl, quote, urlencode, urlparse

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, ValidationError

from .payment_provider import (
    NexiApiError,
    NexiUnavailable,
    _nexi_base_allowed,
    _nexi_clean_base_url,
    _nexi_sanitize_text,
    is_nexi_url,
)

_logger = logging.getLogger(__name__)

# Nexi outcomes. EXECUTED = paid (automatic). AUTHORIZED = only blocked.
OK_RESULTS = {'EXECUTED'}
AUTH_RESULTS = {'AUTHORIZED'}
# THREEDS_VALIDATED = 3DS ok but payment still running: wait.
WAIT_RESULTS = {'PENDING', 'AUTH-REQUEST', 'AUTHORIZED_PENDING', 'THREEDS_VALIDATED'}
# VOIDED = reversal: nothing captured. REFUNDED = refund: nothing captured (v1 without refunds).
KO_RESULTS = {'CANCELED', 'THREEDS_FAILED', 'DENIED', 'DENIED_BY_RISK', 'ERROR',
              'DECLINED', 'FAILED', 'VOIDED', 'REFUNDED'}

# Odoo method -> Nexi paymentService. Empty = all. Values from the Nexi table.
METHOD_SERVICE = {
    'card': 'CARDS',
    'googlepay': 'GOOGLEPAY',
    'applepay': 'APPLEPAY',
    'bancomatpay': 'BANCOMATPAY',
    'mybank': 'MYBANK',
    'paypal': 'PAYPAL',
    'paypal_bnpl': 'PAYPAL_PAGAIN3',  # real Nexi code; PAYPAL_BNPL is ignored
}

def _nexi_short(masked):
    """Show only the last 4 digits: **** 7289. Never the real card."""
    digits = re.sub(r'\D', '', str(masked or ''))
    if len(digits) >= 4:
        return '**** ' + digits[-4:]
    return str(masked or 'Saved card')[:32]


REF_RE = re.compile(r'^[A-Za-z0-9_-]{4,64}$')


def to_cents(amount):
    """Euro -> integer cents. Example: 12.34 -> 1234."""
    try:
        value = Decimal(str(amount))
        if not value.is_finite():
            return 0
        return int((value * 100).to_integral_value())
    except (InvalidOperation, ValueError, TypeError, OverflowError):
        return 0


def is_paid(operation_result):
    return operation_result in OK_RESULTS


def _parse_cents(value):
    """Integer cents from a Nexi response.

    XPay writes the amounts in integer cents (``"1000"`` = EUR 10.00), but some
    flows send a decimal (``"10.00"``): the text with a dot is read as euro and
    converted with Decimal (never float), otherwise a real capture would end up
    in error. ``None`` if it is not a number.
    """
    try:
        text = str(value).strip()
        d = Decimal(text)
    except (InvalidOperation, ValueError, TypeError, OverflowError):
        return None
    # Decimal accepts 'Infinity' and 'NaN': they are not amounts, and int() on
    # Infinity would raise OverflowError up to a 500.
    if not d.is_finite():
        return None
    if '.' in text or 'e' in text.lower():
        try:
            return int((d * 100).to_integral_value())
        except (InvalidOperation, ValueError, TypeError, OverflowError):
            return None
    try:
        if d != d.to_integral_value():
            return None
        return int(d)
    except (InvalidOperation, ValueError, TypeError, OverflowError):
        return None

def _nexi_capture_total(data, op, currency=None):
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
        got = _parse_cents(cap)
        if got is not None:
            return got
    op = op if isinstance(op, dict) else {}
    # Only a CAPTURE carries a capture: the amount of an AUTHORIZATION is an
    # authorization, not money taken.
    if str(op.get('operationType') or '').upper() == 'CAPTURE':
        for raw in (op.get('operationAmount'), op.get('capturedAmount')):
            got = _parse_cents(raw) if raw not in (None, '') else None
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
        got = _parse_cents(item.get('operationAmount'))
        if got is None:
            got = _parse_cents(item.get('capturedAmount'))
        if got is not None:
            total += got
            found = True
    return total if found else None


def _nexi_reversal_after_capture(data, capture_op):
    """True if a reversal/refund was executed after the capture.

    An executed CAPTURE must not be undone by a later decline (e.g. a new
    attempt of the same order), but a VOID/REFUND that arrived after the
    capture must. Without dates the reversal cannot be excluded: fail-closed.
    """
    data = data if isinstance(data, dict) else {}
    capture_ts = _nexi_op_ts(capture_op) if capture_op else None
    operations = data.get('operations')
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
        ts = _nexi_op_ts(item)
        if capture_ts is None or ts is None or ts >= capture_ts:
            return True
    return False


def _nexi_safe_fields(fields):
    """Keep only the card fields with an https src on a Nexi domain.

    The card-field iframes are where the customer types PAN and CVV: a src
    coming from an altered response (or from a new gateway field) must not be
    able to become a third-party iframe inside the checkout.
    """
    out = []
    for f in (fields or []):
        if not isinstance(f, dict):
            continue
        if not is_nexi_url(f.get('src')):
            _logger.warning(
                'Nexi: card field dropped (src not Nexi): %s',
                _nexi_sanitize_text(f.get('id'), 40))
            continue
        out.append(f)
    return out


def _nexi_new_contract_id():
    """Unpredictable contractId: N + 16 hexadecimal digits."""
    return 'N' + secrets.token_hex(8).upper()


# Nexi operations that decide how it went. With automatic capture Nexi returns
# AUTHORIZATION *and* CAPTURE: looking only at the authorization leaves the
# payment hanging even when the money was already taken.
AUTH_OPS = {'AUTHORIZATION'}
CAPTURE_OPS = {'CAPTURE'}

# The customer who picks a card at checkout leaves the choice here.
_NEXI_SESSION_KEY = 'nexi_saved_contract'


def _nexi_op_time(op):
    """Nexi operationTime as a datetime, or None if missing/unreadable."""
    op = op if isinstance(op, dict) else {}
    raw = str(op.get('operationTime') or '').strip()
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace('Z', '+00:00'))
    except ValueError:
        return None


def _nexi_op_ts(op):
    """operationTime as a number (for sorting), or None."""
    dt = _nexi_op_time(op)
    if dt is None:
        return None
    try:
        return dt.timestamp()
    except (OverflowError, OSError, ValueError):
        return None


def _nexi_latest_operation(data, types):
    """Latest operation of the given type, by date, in the Nexi response."""
    data = data if isinstance(data, dict) else {}
    operations = data.get('operations')
    ops = [
        o for o in (operations if isinstance(operations, list) else [])
        if isinstance(o, dict) and str(o.get('operationType') or '').upper() in types
    ]
    if not ops:
        return {}
    # Sort on the parsed date, not on the string: 'T9:00' would come after
    # 'T10:00' in a lexicographic comparison.
    return max(ops, key=lambda o: (
        _nexi_op_ts(o) is not None,
        _nexi_op_ts(o) or 0.0,
        str(o.get('operationTime') or ''),
    ))


def _nexi_decide_operation(data):
    """The operation that decides the outcome: the latest of auth and capture.

    :return: (operation, 'auth' | 'capture' | 'uncertain'). Empty operation if
             Nexi has not returned any yet; 'uncertain' if AUTH and CAPTURE
             disagree but the dates do not say which is the latest (the caller
             must stay pending, never close).
    """
    auth = _nexi_latest_operation(data, AUTH_OPS)
    capture = _nexi_latest_operation(data, CAPTURE_OPS)
    if not auth:
        return capture, 'capture'
    if not capture:
        return auth, 'auth'
    auth_paid = is_paid(str(auth.get('operationResult') or '').upper())
    capture_paid = is_paid(str(capture.get('operationResult') or '').upper())
    auth_ts, capture_ts = _nexi_op_ts(auth), _nexi_op_ts(capture)
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


def _nexi_got_order_id(data):
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


def _nexi_verify_url(provider, order_ref):
    """Verification URL of an order: terminal base + /orders/{ref}."""
    create_url = provider._nexi_base_url()
    if '/orders/hpp' in create_url:
        return create_url.replace('/orders/hpp', f'/orders/{quote(order_ref, safe="")}')
    return create_url.rstrip('/') + f'/orders/{quote(order_ref, safe="")}'


def _nexi_session_choice():
    """Saved-card choice made at checkout, if this request has a session.

    :return: dict {'order_id': int, 'contract_id': str} or None.
    """
    try:
        from odoo.http import request
        choice = request.session.get(_NEXI_SESSION_KEY)
    except Exception:  # no HTTP request (cron, RPC, tests)
        return None
    return choice if isinstance(choice, dict) else None


def _nexi_lock_row(cr, tx_id, nowait=True):
    """Lock the transaction row. With nowait: False if it is already taken.

    The verification on Nexi can take tens of seconds: whoever arrives second
    must not queue behind the first, otherwise a worker stays blocked as long
    as the network. The savepoint keeps the transaction usable when the lock is
    not available. One place for confirmation and cron, so they cannot diverge.
    """
    sql = (
        'SELECT id FROM payment_transaction WHERE id = %s FOR UPDATE'
        + (' NOWAIT' if nowait else '')
    )
    try:
        with cr.savepoint():
            cr.execute(sql, (tx_id,))
    except Exception:
        return False
    return True


class PaymentTransactionNexi(models.Model):
    _inherit = 'payment.transaction'

    nexi_order_id = fields.Char(
        string='Nexi order ID', readonly=True, copy=False)
    nexi_contract_id = fields.Char(
        string='OneClick contract', readonly=True, groups='base.group_system',
        copy=False,
        help='CIT contractId created on the first payment, reused afterwards.',
    )
    nexi_masked_pan = fields.Char(
        string='Saved card', readonly=True, copy=False,
        help='Masked number from Nexi (e.g. ****7289). Never the real card.',
    )
    nexi_build_session = fields.Char(
        string='Build session', readonly=True, groups='base.group_system',
        copy=False)
    nexi_build_token = fields.Char(
        string='Build token', readonly=True, groups='base.group_system',
        copy=False)
    # True as soon as the order was created on Nexi. The cron needs it: a draft
    # without this flag is a cart that never reached the gateway, and must not
    # be probed.
    nexi_order_started = fields.Boolean(
        string='Nexi order started', readonly=True, default=False, copy=False,
        help='True once the order was created on Nexi. From then on the cron may '
             'reconcile even a draft transaction.',
    )
    # Random code per transaction: whoever does not have it cannot touch the payment.
    nexi_access_token = fields.Char(
        string='Nexi access token', readonly=True, groups='base.group_system',
        copy=False)
    nexi_reconcile_count = fields.Integer(
        string='Reconciliation attempts', readonly=True, default=0,
        copy=False,
        help='How many times the cron rechecked this transaction on Nexi. '
             'Past the limit it is left alone.',
    )
    # Non-terminal outcome (4xx, mismatched orderId, missing currency, partial
    # capture): the cron rechecks it instead of leaving it in error forever.
    nexi_uncertain = fields.Boolean(
        string='Uncertain outcome', readonly=True, default=False, copy=False)
    nexi_reverify_count = fields.Integer(
        string='Error recheck attempts', readonly=True, default=0,
        copy=False,
        help='How many times the cron rechecked an uncertain error. '
             'Separate counter from nexi_reconcile_count.',
    )
    nexi_last_result = fields.Char(
        string='Last Nexi outcome', readonly=True, copy=False)
    nexi_last_captured = fields.Integer(
        string='Last captured (cents)', readonly=True, default=0, copy=False)

    def _nexi_family_ids(self, partner_id):
        """Partner + linked contacts: the contract is valid for the whole family."""
        partner = self.env['res.partner'].browse(partner_id)
        if not partner.exists():
            return []
        commercial = partner.commercial_partner_id.id or partner.id
        return self.env['res.partner'].search(
            [('id', 'child_of', commercial)]
        ).ids or [partner_id]

    def _nexi_contract_domain(self, partner_ids, provider_id=None):
        """Domain for the saved cards.

        The contract belongs to one terminal: a contractId created on a provider
        does not exist on the others, and reusing it would make the payment fail.
        """
        domain = [
            ('provider_code', '=', 'nexi'),
            ('partner_id', 'in', list(partner_ids or [])),
            ('nexi_contract_id', '!=', False),
        ]
        if provider_id:
            # Accepts an id, a list or a recordset: it allows a single search
            # when there is more than one terminal.
            if hasattr(provider_id, 'ids'):
                ids = list(provider_id.ids)
            elif isinstance(provider_id, (list, tuple, set)):
                ids = list(provider_id)
            else:
                ids = [provider_id]
            if ids:
                domain.append(('provider_id', 'in', ids))
        return domain

    def _nexi_contract_for_partner(self, partner_id, provider_id=None):
        """Latest paid CIT contract of this partner, if any."""
        family = self._nexi_family_ids(partner_id)
        if not family:
            return False
        tx = self.search(
            self._nexi_contract_domain(family, provider_id)
            + [('state', 'in', ['done', 'authorized', 'pending'])],
            order='id desc', limit=1,
        )
        return tx.nexi_contract_id or False

    def _nexi_chosen_contract(self, provider):
        """Card the customer chose at checkout, if there is a choice.

        The choice comes from the session (see /payment/nexi/saved-card) because
        the Odoo form does not carry extra fields down to the provider.

        :return: None  = no choice, the automatic one decides
                 False = the customer wants a new card
                 str   = the chosen contractId
        """
        choice = _nexi_session_choice()
        if not choice:
            return None
        # Choice tied to the partner (it also works for payments without a cart).
        chosen_for = choice.get('partner_id')
        if chosen_for not in self._nexi_family_ids(self.partner_id.id):
            return None
        contract = str(choice.get('contract_id') or '').strip()
        if not contract:
            return False
        cards = {
            c['contract_id']
            for c in self.get_nexi_saved_cards(self.partner_id.id, provider.id)
        }
        # A contract that is not the customer's or not of this terminal: ignore.
        return contract if contract in cards else None

    def _nexi_can_see_partner(self, partner_id):
        """I only see my own cards, unless this is the system."""
        if self.env.su or self.env.user.has_group('base.group_system'):
            return True
        try:
            mine = set(self._nexi_family_ids(self.env.user.partner_id.id))
        except Exception:
            return False
        return partner_id in mine

    @api.model
    def get_nexi_saved_cards(self, partner_id, provider_id=None):
        """Customer saved cards (masked numbers only). For the checkout."""
        if not self._nexi_can_see_partner(partner_id):
            return []
        family = self._nexi_family_ids(partner_id)
        if not family:
            return []
        txs = self.search(
            self._nexi_contract_domain(family, provider_id)
            + [('state', 'in', ['done', 'authorized', 'pending'])],
            order='id desc',
        )
        seen = set()
        cards = []
        for tx in txs:
            if tx.nexi_contract_id in seen:
                continue
            seen.add(tx.nexi_contract_id)
            cards.append({
                'masked': _nexi_short(tx.nexi_masked_pan),
                'contract_id': tx.nexi_contract_id,
            })
        return cards

    @api.model
    def _nexi_ui_strings(self):
        """Shopper-facing texts, translated in the current shop language."""
        return {
            # -- Build page / redirect --
            'pay_with_nexi': _('Pay with Nexi'),
            'build_intro': _('Pay without leaving the site. Card data stays with Nexi.'),
            'loading_methods': _('Loading payment methods…'),
            'loading_fields': _('Loading card fields…'),
            'pay_now': _('Pay now'),
            'back_to_checkout': _('Back to checkout'),
            'redirect_intro': _('You are being redirected to Nexi…'),
            'pay_on_nexi': _('Pay on Nexi'),
            'redirect_fallback': _('click here if it does not start'),
            'build_redirect_intro': _('You are about to pay with Nexi…'),
            'continue': _('Continue'),
            # -- Privacy --
            'privacy_prefix': _('I have read the Nexi privacy policy ('),
            'privacy_suffix': ').',
            'privacy_required': _('Tick the privacy box to continue.'),
            # -- Card fields --
            'field_card': _('Card field'),
            'field_number': _('Card number'),
            'field_expiry': _('Expiry date'),
            'field_cvv': _('Security code'),
            'field_name': _('First and last name'),
            'field_surname': _('Last name'),
            'field_email': _('Email'),
            'field_invalid': _('Check this field.'),
            # -- Saved cards --
            'your_cards': _('Your cards'),
            'use_new_card': _('Use a new card'),
            'save_card_failed': _('Could not save the selected card. Try again.'),
            # -- Errors --
            'no_method': _('No payment method available on this terminal.'),
            'methods_failed': _('Payment methods could not be loaded. Try again.'),
            'form_failed': _('Form could not be loaded. Try again.'),
            'no_card_field': _('No card field available.'),
            'session_stale': _('Session expired: fields refreshed, fill them in again and press Pay now.'),
            'invalid_response': _('Invalid Nexi response.'),
            'invalid_data': _('Invalid data. Check and try again.'),
            'sdk_failed': _('Nexi SDK could not be loaded. Try again.'),
            'confirm_failed': _('Confirmation failed. Try again.'),
            'nexi_error': _('Nexi error. Try again.'),
            'method_unavailable': _('Method not available. Try again.'),
            'bnpl_minimum': _('PayPal Pay in 3 requires an order of at least €30.00.'),
            'nexi_prefix': _('Nexi: '),
        }

    @api.model
    def _nexi_ui_strings_json(self):
        """Same strings as JSON, for template data attributes."""
        return json.dumps(self._nexi_ui_strings())

    @api.model
    def get_last_used_method_id(self, partner_id, method_ids):
        """Last method used by the customer, if still available. For preselection."""
        if not self._nexi_can_see_partner(partner_id):
            return False
        family = self._nexi_family_ids(partner_id)
        if not family or not method_ids:
            return False
        tx = self.search([
            ('partner_id', 'in', family),
            ('payment_method_id', 'in', list(method_ids)),
            ('state', 'in', ['done', 'authorized', 'pending', 'draft']),
        ], order='id desc', limit=1)
        return tx.payment_method_id.id if tx else False

    @api.model_create_multi
    def create(self, vals_list):
        for vals in (vals_list or []):
            if not self.env.su and not self.env.user.has_group('base.group_system'):
                # The customer does not choose tokens or contracts: only the
                # system creates them.
                for f in ('nexi_contract_id', 'nexi_build_session', 'nexi_build_token',
                          'nexi_order_started', 'nexi_masked_pan', 'nexi_order_id',
                          'nexi_access_token',
                          'nexi_reconcile_count', 'nexi_uncertain',
                          'nexi_reverify_count', 'nexi_last_result',
                          'nexi_last_captured'):
                    vals.pop(f, None)
            if not vals.get('nexi_access_token'):
                vals['nexi_access_token'] = secrets.token_urlsafe(32)
        return super().create(vals_list)

    def write(self, vals):
        if not self.env.su and not self.env.user.has_group('base.group_system'):
            bad = [f for f in ('nexi_contract_id', 'nexi_build_session', 'nexi_build_token',
                               'nexi_order_started', 'nexi_masked_pan', 'nexi_order_id',
                               'nexi_access_token',
                               'nexi_reconcile_count', 'nexi_uncertain',
                               'nexi_reverify_count', 'nexi_last_result',
                               'nexi_last_captured')
                   if f in (vals or {})]
            if bad:
                raise AccessError(_('Only the system can write Nexi data.'))
        return super().write(vals)

    def _get_specific_create_values(self, provider_code, values):
        vals = super()._get_specific_create_values(provider_code, values)
        if provider_code == 'nexi':
            vals['nexi_order_id'] = values.get('reference')
        return vals

    def _nexi_mark_order_started(self):
        """The order exists on Nexi: from here the cron may reconcile the draft."""
        if not self.nexi_order_started:
            self.sudo().write({'nexi_order_started': True})

    def _nexi_post_order(self, provider, url, payload, want_save=False):
        """Create the order on Nexi. If the contract is dead, forget it and retry."""
        try:
            data = provider._nexi_make_request('POST', url, payload=payload)
            self._nexi_mark_order_started()
            return data
        except ValidationError as e:
            session = (payload or {}).get('paymentSession') or {}
            rec = session.get('recurrence') or {}
            msg = str(e)
            dead_code = 'PS0064' in msg or 'Recurring contract does not exist' in msg
            if not dead_code or rec.get('action') != 'SUBSEQUENT_PAYMENT':
                raise
            dead = rec.get('contractId')
            _logger.warning(
                'Nexi dead contract %s, retrying without it.',
                _nexi_sanitize_text(dead, 64))
            if dead and self.nexi_contract_id == dead:
                self.sudo().nexi_contract_id = False
            session = dict(session)
            session.pop('recurrence', None)
            if want_save:
                contract = _nexi_new_contract_id()
                session['recurrence'] = {
                    'action': 'CONTRACT_CREATION',
                    'contractId': contract,
                    'contractType': 'CIT',
                }
                self.nexi_contract_id = contract
            payload = dict(payload, paymentSession=session)
            try:
                data = provider._nexi_make_request('POST', url, payload=payload)
                self._nexi_mark_order_started()
                return data
            except ValidationError as e2:
                # The Nexi detail stays in the logs: state_message gets only a
                # generic message (the customer reads it too).
                _logger.warning(
                    'Nexi order %s KO: %s', self.reference,
                    _nexi_sanitize_text(e2, 200))
                self._set_error(_('Nexi error. Try again.'))
                raise

    def _nexi_candidate_base_allowed(self, candidate):
        """A base_url candidate is acceptable only if its host is the site's.

        Outside an HTTP request (tests, internal calls) there is no Host to
        spoof: the candidate stays valid.
        """
        try:
            from odoo.http import request
        except Exception:
            return True
        if not request:
            return True
        allowed = set()
        try:
            website = getattr(request, 'website', None)
            domain = (getattr(website, 'domain', '') or '').strip()
            if domain:
                allowed.add(
                    (urlparse(domain).hostname or domain.split(':')[0]).lower())
            wbu = _nexi_clean_base_url(self.get_base_url())
            if wbu:
                allowed.add(urlparse(wbu).hostname or '')
        except Exception:
            return False
        return _nexi_base_allowed(candidate, allowed)

    def _get_specific_rendering_values(self, processing_values):
        """Values for the redirect form. The Nexi order is created here."""
        res = super()._get_specific_rendering_values(processing_values)
        if self.provider_code != 'nexi':
            return res
        self.ensure_one()
        if self.currency_id.name != 'EUR':
            raise ValidationError(_('Nexi supports EUR only.'))
        provider = self.provider_id
        if not provider.nexi_api_key:
            raise ValidationError(_('Nexi X-Api-Key is missing in the provider.'))
        # Shopper texts in the current shop language, ready for the templates
        # (redirect forms and Build page are rendered from these values).
        res.update(nexi_ui=self._nexi_ui_strings(), nexi_ui_json=self._nexi_ui_strings_json())
        # Pay in 3 has a EUR 30 minimum on the Nexi side: say why before calling
        # the API, instead of letting a technical gateway error out.
        if (
            (self.payment_method_code or '') == 'paypal_bnpl'
            and self.amount
            and self.amount < 30
        ):
            raise ValidationError(
                _('PayPal Pay in 3 requires an order of at least €30.00.')
            )
        # Public base: dedicated field, then web.base.url. The request Host
        # never decides the return URLs: at most an internal candidate
        # (test/direct call) is accepted if its host is among the ones
        # configured for the site.
        self = self.sudo()
        base = _nexi_clean_base_url(provider.nexi_public_base)
        if not base:
            base = _nexi_clean_base_url(self.get_base_url())
        if not base:
            cand = processing_values.get('base_url')
            if _nexi_clean_base_url(cand) and self._nexi_candidate_base_allowed(cand):
                _logger.warning(
                    'Nexi uses a candidate base: set the Shop public base.')
                base = _nexi_clean_base_url(cand)
        if not base:
            base = (self.get_base_url() or '').rstrip('/')
        suffix = processing_values.get('order_suffix') or ''
        if not self.nexi_access_token:
            self.sudo().nexi_access_token = secrets.token_urlsafe(32)
        tok = self.nexi_access_token
        session = {
            'actionType': 'VERIFY' if (provider.nexi_oneclick and not self.amount) else 'PAY',
            'amount': str(to_cents(self.amount)),
            'language': 'ITA',
            'resultUrl': f'{base}/payment/nexi/confirm?ref={self.reference}{suffix}&tok={tok}',
            'cancelUrl': f'{base}/payment/nexi/confirm?ref={self.reference}{suffix}&cancel=1&tok={tok}',
            'notificationUrl': f'{base}/payment/nexi/confirm?ref={self.reference}{suffix}&notify=1&tok={tok}',
            # Always automatic: manual capture broke the payments (500).
            'captureType': 'IMPLICIT',
        }
        # Method preselection: the provider field wins, if empty the chosen
        # method is used.
        # no_service (shared prepare): no filter, Nexi returns all the buttons.
        if processing_values.get('no_service'):
            service = False
        else:
            service = (provider.nexi_payment_service or '').strip().upper()
            if not service:
                service = METHOD_SERVICE.get(self.payment_method_code or '', False) or False
        if service:
            if not re.match(r'^[A-Z0-9_]{2,30}$', service):
                raise ValidationError(_('Invalid Nexi method.'))
            session['paymentService'] = service
        # OneClick CIT: cards only, and only if the customer ticks "save card".
        # CIT contracts are born from a card: reusing them with an APM gives a
        # Nexi error.
        # First payment with the tick = CONTRACT_CREATION.
        # Later ones reuse the contractId with SUBSEQUENT_PAYMENT via HPP.
        # no_service (shared buttons): no recurrence, only buttons.
        contract = False
        is_card = (self.payment_method_code or '') == 'card'
        want_save = bool(processing_values.get('should_tokenize')) if provider.nexi_oneclick else False
        # In Build the customer must be able to enter a new card: if a saved
        # contract were reused, Nexi would show the masked card and the fields
        # would not be editable (error while filling them in).
        is_build = provider.nexi_mode == 'build'
        if provider.nexi_oneclick and is_card and not processing_values.get('no_service'):
            chosen = self._nexi_chosen_contract(provider)
            if is_build:
                contract = False
            elif chosen is not None:
                # The customer chose: the choice wins over the automatic one.
                # False = "use a new card".
                contract = chosen or False
            else:
                contract = self.nexi_contract_id or self._nexi_contract_for_partner(
                    self.partner_id.id, provider.id
                )
            if contract:
                session['recurrence'] = {
                    'action': 'SUBSEQUENT_PAYMENT',
                    'contractId': contract,
                    'contractType': 'CIT',
                }
            elif want_save:
                contract = _nexi_new_contract_id()
                session['recurrence'] = {
                    'action': 'CONTRACT_CREATION',
                    'contractId': contract,
                    'contractType': 'CIT',
                }
        order_id = self.reference + (processing_values.get('order_suffix') or '')
        # Nexi accepts orderIds up to 27 characters (doc POST /orders/hpp): with
        # a long reference prefix the order would be refused in the dark.
        if len(order_id) > 27:
            _logger.error(
                'Nexi: orderId too long (%s): %s',
                len(order_id), _nexi_sanitize_text(order_id, 64))
            raise ValidationError(_(
                'Order reference too long for Nexi (max 27 characters): '
                'shorten the payment reference prefix.'
            ))
        payload = {
            'order': {
                'orderId': order_id,
                'amount': str(to_cents(self.amount)),
                'currency': self.currency_id.name or 'EUR',
                'description': (self.reference or '')[:100],
                # In Build the customerId makes the saved card reappear on the
                # Nexi side (locked fields and old expiry): it is sent only when
                # the customer really wants to save the card.
                **({'customerId': 'C%010d' % (self.partner_id.id % 10000000000)}
                   if (not is_build or want_save) else {}),
                'customerInfo': {
                    'cardHolderName': (self.partner_id.name or '')[:255],
                    'cardHolderEmail': (self.partner_id.email or '')[:255],
                },
            },
            'paymentSession': session,
        }
        if contract and not self.nexi_contract_id:
            self.nexi_contract_id = contract
        # Build v3 mode: embedded card and APMs via Build; the rest via HPP.
        if provider.nexi_mode == 'build' and (
            (self.payment_method_code or '') == 'card' or processing_values.get('force_build')
        ):
            processing_values = dict(processing_values or {}, base_url=base)
            return self._nexi_render_build_values(res, provider, payload, want_save, processing_values)
        url = provider._nexi_base_url()
        try:
            data = self._nexi_post_order(provider, url, payload, want_save)
        except ValidationError as e:
            _logger.warning(
                'Nexi order %s KO: %s', self.reference,
                _nexi_sanitize_text(e, 200))
            self._set_error(_('Nexi error. Try again.'))
            raise
        data = data if isinstance(data, dict) else {}

        hosted = data.get('hostedPage') or data.get('hostedpage')
        token = data.get('securityToken') or data.get('securitytoken')
        if not hosted:
            self._set_error(_('Nexi did not return the payment page.'))
            raise ValidationError(_('Nexi error. Try again.'))
        # Nexi gives hostedPage + securityToken separately: they must be joined.
        # The token belongs to the gateway: it is encoded as a value, never
        # concatenated, otherwise one of its '&' becomes an extra form parameter.
        if token:
            parts = urlparse(hosted)
            query = parse_qsl(parts.query, keep_blank_values=True)
            embedded = [v for k, v in query if k.lower() == 'securitytoken']
            if embedded and embedded[0] != str(token):
                # The page already carried a different token: the dedicated
                # field wins, never the one inside the URL.
                _logger.warning('Nexi order %s: securityToken in the page dropped.', self.reference)
            query = [(k, v) for k, v in query if k.lower() != 'securitytoken']
            query.append(('securityToken', str(token)))
            hosted = parts._replace(query=urlencode(query)).geturl()
        _logger.info('Nexi order %s created.', self.reference)
        # A single validator for the Nexi domains (see payment_provider): it is
        # not reimplemented here, it is reused.
        if not is_nexi_url(hosted):
            raise ValidationError(_('Nexi error. Try again.'))
        # Split base and parameters: the GET form puts them back by itself.
        parts = urlparse(hosted)
        base = f'{parts.scheme}://{parts.netloc}{parts.path}'
        params = dict(parse_qsl(parts.query))
        res.update(redirect_url=hosted, api_url=base, url_params=params)
        return res

    def _nexi_render_build_values(self, res, provider, payload, want_save=False, processing_values=None):
        """Build v3 embedded order: create the order and point to the iframe template."""
        self.ensure_one()
        processing_values = processing_values or {}
        base = (
            _nexi_clean_base_url(processing_values.get('base_url'))
            or _nexi_clean_base_url(self.get_base_url())
        )
        if not base:
            raise ValidationError(_('Build requires a public URL (Shop public base or web.base.url); localhost is not accepted. Use HPP for local tests.'))
        build_url = provider._nexi_build_url()
        build_payload = dict(payload)
        build_payload['version'] = '3'
        parts = urlparse(base)
        build_payload['merchantUrl'] = f'{parts.scheme}://{parts.netloc}'
        # The card goes with CARDS: Nexi returns the 5 fields right away, no buttons.
        try:
            data = self._nexi_post_order(provider, build_url, build_payload, want_save)
        except ValidationError as e:
            _logger.warning(
                'Nexi order %s KO: %s', self.reference,
                _nexi_sanitize_text(e, 200))
            self._set_error(_('Nexi error. Try again.'))
            raise
        data = data if isinstance(data, dict) else {}
        # The buttons-only order (-m) must not overwrite the card-field session:
        # finalize has to use the right one.
        if not processing_values.get('no_service'):
            self.nexi_build_session = data.get('sessionId') or False
            self.nexi_build_token = data.get('securityToken') or False
            _logger.debug(
                'Card Build %s -> session %s', self.reference,
                _nexi_sanitize_text(self.nexi_build_session, 64))
        if not self.nexi_access_token:
            self.sudo().nexi_access_token = secrets.token_urlsafe(32)
        tok = self.nexi_access_token
        try:
            from odoo.http import request as _request
            csrf = _request.csrf_token() if _request else ''
        except Exception:
            csrf = ''
        res.update(
            nexi_build=True,
            build_data=data,
            build_fields=_nexi_safe_fields((data or {}).get('fields')),
            build_url=(data or {}).get('url') or False,
            build_state=(data or {}).get('state') or False,
            nexi_sdk=provider._nexi_sdk_url(),
            # finalize is a JSON route: the tok goes in the header, not in the query.
            build_finalize_url=f'/payment/nexi/build/{self.reference}/finalize',
            build_tok=tok,
            build_page_url=f'/payment/nexi/build/{self.reference}',
            csrf_token=csrf,
            confirm_url=f'/payment/nexi/confirm?ref={self.reference}&tok={tok}',
            redirect_url=f'/payment/nexi/build/{self.reference}?tok={tok}',
        )
        return res

    def _get_tx_from_notification_data(self, provider_code, notification_data):
        tx = super()._get_tx_from_notification_data(provider_code, notification_data)
        if provider_code != 'nexi' or tx:
            return tx
        notification_data = (
            notification_data if isinstance(notification_data, dict) else {})
        ref = notification_data.get('ref') or notification_data.get('orderId')
        if not ref or not REF_RE.match(str(ref)):
            raise ValidationError(_('Invalid Nexi return.'))
        full_ref = str(ref)
        if full_ref.endswith('-m'):
            notification_data['nexi_order_ref'] = full_ref
            ref = full_ref[:-2]
        tx = self.search([('reference', '=', ref), ('provider_code', '=', 'nexi')])
        if not tx:
            raise ValidationError(_('Invalid Nexi return.'))
        return tx

    def _nexi_probe_paid(self, order_ref):
        """True if the Nexi order is paid, without touching the state.

        Read-only probe for the reconciliation: an order that does not exist or
        is not paid counts as False, never as an error.
        """
        self.ensure_one()
        try:
            data = self.provider_id._nexi_make_request(
                'GET', _nexi_verify_url(self.provider_id, order_ref))
        except Exception:
            return False
        got_order = _nexi_got_order_id(data)
        if got_order != order_ref:
            return False
        op, _kind = _nexi_decide_operation(data)
        op = op if isinstance(op, dict) else {}
        return is_paid(str(op.get('operationResult') or '').upper())

    @api.model
    def _nexi_reconcile_pending(self, limit=20):
        """Recheck the Nexi transactions left hanging.

        If the customer closes the tab before the return and the notification
        is lost, the transaction would stay pending forever with the money
        taken: the cron rechecks it on Nexi with the same code as the
        confirmation, so state and amounts are decided in one place only. A
        second pass retries the post-processing of the closed ones left behind.
        """
        cutoff = fields.Datetime.now() - timedelta(minutes=10)
        # A pending already has an order on Nexi (it gets there from a
        # notification). A draft can be a cart that never left: it counts only
        # if the order was created on the gateway. `is_post_processed` does NOT
        # prove a final outcome: the flow above sets it to True even on a
        # PENDING, and that transaction still has to be reconciled.
        stuck = self.search([
            ('provider_code', '=', 'nexi'),
            ('state', 'in', ['draft', 'pending']),
            ('create_date', '<', cutoff),
            ('nexi_reconcile_count', '<', 72),
            '|',
            ('state', '=', 'pending'),
            ('nexi_order_started', '=', True),
        ], order='nexi_reconcile_count, create_date', limit=limit)
        for tx in stuck:
            if not _nexi_lock_row(self.env.cr, tx.id):
                _logger.info('Nexi %s: closing already in progress in another flow.', tx.reference)
                continue
            tx.invalidate_recordset()
            if tx.state not in ('draft', 'pending'):
                continue
            if tx.state == 'draft' and not tx.nexi_order_started:
                continue
            try:
                with self.env.cr.savepoint():
                    # The cron can afford retries with a pause (nexi_retry): the
                    # HTTP path does a single GET to keep the worker free.
                    tx._process_notification_data({'nexi_retry': True})
                    tx.invalidate_recordset()
                    if tx.state == 'pending' and tx.provider_id.nexi_mode == 'build':
                        # The Build prepare also creates the APM buttons order
                        # (reference-m): if that one is the paid one, it closes
                        # it. The probe never touches the state: if the buttons
                        # order is not paid (or does not exist) nothing happens.
                        if tx._nexi_probe_paid(tx.reference + '-m'):
                            tx._process_notification_data({
                                'nexi_retry': True,
                                'nexi_order_ref': tx.reference + '-m',
                            })
                            tx.invalidate_recordset()
                    if tx.state in ('done', 'cancel', 'error') and not tx.is_post_processed:
                        tx._post_process()
                        tx.invalidate_recordset()
            except Exception:
                _logger.exception('Nexi reconciliation %s KO', tx.reference)
            finally:
                # The counter grows at every pass: after 72 rounds (~12 hours)
                # the transaction leaves the loop and stays pending for a manual
                # follow-up.
                count = (tx.nexi_reconcile_count or 0) + 1
                tx.sudo().write({'nexi_reconcile_count': count})
                if count >= 72 and tx.state in ('draft', 'pending'):
                    # Same alert as the leftover: a pending that leaves the loop
                    # with money held must not disappear silently.
                    _logger.error(
                        'Nexi %s: still pending after %s attempts '
                        '(last outcome: %s, last captured: %s cents, expected: %s cents) '
                        '- manual check.',
                        tx.reference, count, tx.nexi_last_result or tx.state_message or 'unknown',
                        tx.nexi_last_captured or 0, to_cents(tx.amount),
                    )
            if tx.state in ('done', 'error', 'cancel'):
                _logger.info(
                    'Nexi reconciliation %s -> %s (attempt %s).',
                    tx.reference, tx.state, tx.nexi_reconcile_count,
                )
        # Uncertain errors: 4xx, mismatched orderId, missing currency and partial
        # capture are not a final KO. The cron rechecks them with a separate
        # counter, so a capture that completes later does not stay in error
        # forever with the money taken.
        recent_from = fields.Datetime.now() - timedelta(days=7)
        errors = self.search([
            ('provider_code', '=', 'nexi'),
            ('state', '=', 'error'),
            ('nexi_uncertain', '=', True),
            ('create_date', '<', cutoff),
            ('create_date', '>', recent_from),
            ('nexi_reverify_count', '<', 12),
        ], order='nexi_reverify_count, create_date', limit=min(limit, 10))
        for tx in errors:
            if not _nexi_lock_row(self.env.cr, tx.id):
                continue
            tx.invalidate_recordset()
            if tx.state != 'error' or not tx.nexi_uncertain:
                continue
            try:
                with self.env.cr.savepoint():
                    tx._process_notification_data({'nexi_retry': True})
                    tx.invalidate_recordset()
                    # A transition to done/cancel reopens is_post_processed: here
                    # the post-processing must be done once more.
                    if tx.state in ('done', 'cancel') and not tx.is_post_processed:
                        tx._post_process()
                        tx.invalidate_recordset()
            except Exception:
                _logger.exception('Nexi error recheck %s KO', tx.reference)
            finally:
                count = (tx.nexi_reverify_count or 0) + 1
                tx.sudo().write({'nexi_reverify_count': count})
                if count >= 12 and tx.state == 'error':
                    _logger.error(
                        'Nexi %s: uncertain error after %s checks '
                        '(last outcome: %s, last captured: %s cents, expected: %s cents) '
                        '- manual check.',
                        tx.reference, count,
                        tx.nexi_last_result or tx.state_message or 'unknown',
                        tx.nexi_last_captured or 0, to_cents(tx.amount),
                    )
            if tx.state in ('done', 'cancel'):
                _logger.info(
                    'Nexi error recheck %s -> %s (attempt %s).',
                    tx.reference, tx.state, tx.nexi_reverify_count,
                )
        # Closed but not post-processed (_post_process failed after _set_done):
        # without this pass they would stay put forever, because the search above
        # only covers the pending ones. The ones handled just now are excluded:
        # their _post_process was already tried in the first round. Same cutoff
        # as the first round: on the first deploy the cron must not chew months
        # of history before the fresh transactions.
        leftovers = self.search([
            ('provider_code', '=', 'nexi'),
            ('state', 'in', ['done', 'cancel', 'error']),
            ('is_post_processed', '=', False),
            ('create_date', '<', cutoff),
            ('nexi_reconcile_count', '<', 72),
            ('id', 'not in', (stuck | errors).ids),
        ], order='nexi_reconcile_count, create_date', limit=limit)
        for tx in leftovers:
            if not _nexi_lock_row(self.env.cr, tx.id):
                continue
            tx.invalidate_recordset()
            if tx.is_post_processed or tx.state not in ('done', 'cancel', 'error'):
                continue
            try:
                with self.env.cr.savepoint():
                    tx._post_process()
                    tx.invalidate_recordset()
            except Exception:
                _logger.exception('Nexi post-processing %s KO', tx.reference)
            finally:
                count = (tx.nexi_reconcile_count or 0) + 1
                tx.sudo().write({'nexi_reconcile_count': count})
                if count >= 72 and not tx.is_post_processed:
                    # Last round: from here the transaction leaves the loop on
                    # its own, closed but never post-processed. An error in the
                    # logs, once, instead of silence.
                    _logger.error(
                        'Nexi %s: closed (%s) but never post-processed after %s attempts, '
                        'a manual follow-up is needed.', tx.reference, tx.state, count,
                    )
        return len(stuck) + len(errors) + len(leftovers)

    def _nexi_uncertain_error(self, message, data=None, op=None):
        """Non-final error: log captured/expected and leave it to the cron.

        A 4xx, a mismatched orderId, a missing currency or a partial capture do
        not prove the payment failed: they must be rechecked.
        """
        expected = to_cents(self.amount)
        try:
            captured = _nexi_capture_total(
                data, op, currency=self.currency_id.name)
        except Exception:
            captured = None
        _logger.error(
            'Nexi %s: uncertain outcome (%s): captured=%s cents, expected=%s cents, '
            'order=%s',
            self.reference, _nexi_sanitize_text(message),
            captured, expected, self.nexi_order_id or self.reference,
        )
        self.sudo().write({
            'nexi_uncertain': True,
            'nexi_last_captured': captured or 0,
        })
        self._set_error(message)

    def _process_notification_data(self, notification_data):
        super()._process_notification_data(notification_data)
        if self.provider_code != 'nexi':
            return
        notification_data = (
            notification_data if isinstance(notification_data, dict) else {})
        # Do not trust cancel in the URL: always verify on Nexi.
        provider = self.provider_id
        # The order to check is decided by the transaction, never by the caller.
        raw_ref = notification_data.get('nexi_order_ref')
        if raw_ref and raw_ref != self.reference and raw_ref != self.reference + '-m':
            self._set_error(_('Invalid Nexi return.'))
            return
        order_ref = str(raw_ref or self.reference)
        verify_url = _nexi_verify_url(provider, order_ref)
        data = {}
        result = ''
        op = {}
        op_kind = 'auth'
        # A single GET in the request path: the retries with sleep live in the
        # cron (_nexi_reconcile_pending passes attempts=3), otherwise a confirm
        # keeps a worker busy for up to ~60s and the self-inflicted 429s close it
        # with an error.
        attempts = 3 if (notification_data or {}).get('nexi_retry') else 1
        for attempt in range(attempts):
            try:
                data = provider._nexi_make_request('GET', verify_url)
            except ValidationError as e:
                transient = isinstance(e, NexiUnavailable)
                if isinstance(e, NexiApiError):
                    status = getattr(e, 'status_code', None) or 0
                    transient = status == 429 or status >= 500
                if transient:
                    # Temporary failure (network, timeout, 429/5xx): pending,
                    # the cron rechecks. Never error on a maybe-paid.
                    _logger.warning(
                        'Nexi verification %s postponed: %s', self.reference,
                        _nexi_sanitize_text(e, 200))
                    self._set_pending(state_message=_('Nexi is unreachable. Try again.'))
                    return
                _logger.warning(
                    'Nexi verification %s KO: %s', self.reference,
                    _nexi_sanitize_text(e, 200))
                self._nexi_uncertain_error(_('Nexi is unreachable. Try again.'), data, op)
                return
            # A response that is not an object is an altered gateway: the guards
            # of _nexi_decide_operation treat it as "uncertain".
            data = data if isinstance(data, dict) else {}
            # The latest operation decides: with automatic capture Nexi sends
            # AUTHORIZATION + CAPTURE and the real outcome sits on the capture.
            op, op_kind = _nexi_decide_operation(data)
            op = op if isinstance(op, dict) else {}
            # The order to verify is decided by the transaction; the response
            # must however refer to THIS order. If the id is missing I cannot
            # tell: fail-closed, not "trust it".
            got_order = _nexi_got_order_id(data)
            if not got_order or got_order != order_ref:
                self._nexi_uncertain_error(_('Invalid Nexi return.'), data, op)
                return
            result = str(op.get('operationResult') or '').upper()
            if op_kind == 'uncertain':
                # AUTH and CAPTURE disagree without reliable dates: the latest
                # cannot be known. Pending, never close.
                result = ''
                break
            # The scheme sometimes answers PENDING for a few seconds: retry
            # instead of declaring "pending" right away.
            if result and result not in WAIT_RESULTS:
                break
            if attempt < attempts - 1:
                time.sleep(1.0)
        last_total = _nexi_capture_total(data, op, currency=self.currency_id.name)
        self.sudo().write({
            'nexi_last_result': result[:32] or False,
            'nexi_last_captured': last_total or 0,
        })
        if is_paid(result):
            # Check amount and currency of the operation that decided, then that
            # the capture really covers the figure: all in cents.
            expected = to_cents(self.amount)
            want_cur = self.currency_id.name
            got_cur = op.get('operationCurrency')
            if not got_cur or got_cur != want_cur:
                self._nexi_uncertain_error(_('Invalid Nexi amount.'), data, op)
                return
            amount_raw = op.get('operationAmount')
            if amount_raw in (None, ''):
                # On the capture the amount can be in capturedAmount.
                amount_raw = op.get('capturedAmount')
            got = _parse_cents(amount_raw)
            masked = (
                op.get('paymentInstrumentInfo')
                or op.get('maskedPan')
                or op.get('maskedPanAlias')
                or False
            )
            # The amount that counts is the captured one: order total, operation
            # figures or the sum of the captures in the same currency. If the
            # operation that decided carries less than the total, the lower one
            # wins: it closes only when every number seen covers the expected one.
            total = _nexi_capture_total(data, op, currency=want_cur)
            if op_kind != 'capture' and (total is None or total <= 0):
                # Authorization without capture evidence: not a closed payment.
                # It stays pending, the cron rechecks it.
                self._set_pending(state_message=_('Nexi authorized, waiting for capture.'))
                return
            if total is None:
                total = got
            if got is not None and total is not None and got < total:
                total = got
            if total is None or total < expected:
                self._nexi_uncertain_error(_('Nexi capture incomplete.'), data, op)
                return
            if total != expected:
                # Capture higher than expected: money taken in excess (e.g. a
                # stale session paid twice). It is not closed as done without a
                # ceiling: the operator refunds it from the Nexi back office.
                _logger.error(
                    'Nexi %s: captured %s cents > expected %s cents '
                    '(order=%s): manual check.',
                    self.reference, total, expected,
                    self.nexi_order_id or self.reference,
                )
                self.sudo().write({'nexi_uncertain': False})
                self._set_error(_('Nexi captured amount does not match. Manual check required.'))
                return
            if masked:
                self.nexi_masked_pan = _nexi_short(masked)
            self.sudo().write({'nexi_uncertain': False})
            self._set_done(state_message=f'Nexi OK: {result} ({op_kind})')
        elif result in AUTH_RESULTS:
            # Authorized but not captured: save the card and leave it pending.
            operations = data.get('operations')
            for auth_op in (operations if isinstance(operations, list) else []):
                if not isinstance(auth_op, dict):
                    continue
                if auth_op.get('operationType') == 'AUTHORIZATION':
                    masked_auth = (
                        auth_op.get('paymentInstrumentInfo')
                        or auth_op.get('maskedPan')
                        or False
                    )
                    if masked_auth:
                        self.nexi_masked_pan = _nexi_short(masked_auth)
                    break
            # No manual capture: stay pending instead of going to error.
            self._set_pending(state_message=f'Nexi authorized: {result}')
        elif result in KO_RESULTS:
            expected = to_cents(self.amount)
            captured = _nexi_capture_total(data, None, currency=self.currency_id.name)
            capture_op = _nexi_latest_operation(data, CAPTURE_OPS)
            if result not in ('REFUNDED', 'VOIDED') and captured is not None:
                if captured == expected and not _nexi_reversal_after_capture(data, capture_op):
                    # Capture already executed: a later decline (new attempt)
                    # does not undo the money taken.
                    self.sudo().write({'nexi_uncertain': False})
                    self._set_done(
                        state_message=f'Nexi OK: capture already executed ({result} later)')
                    return
                if captured > expected and not _nexi_reversal_after_capture(data, capture_op):
                    _logger.error(
                        'Nexi %s: captured %s cents > expected %s cents '
                        '(order=%s): manual check.',
                        self.reference, captured, expected,
                        self.nexi_order_id or self.reference,
                    )
                    self.sudo().write({'nexi_uncertain': False})
                    self._set_error(_('Nexi captured amount does not match. Manual check required.'))
                    return
            self.sudo().write({'nexi_uncertain': False})
            self._set_canceled(state_message=f'Nexi KO: {result}')
        else:
            self._set_pending(state_message=f'Nexi pending: {result or "unknown"}')

    def _send_refund_request(self, amount_to_refund=None, create_refund_transaction=True):
        self.ensure_one()
        if self.provider_code != 'nexi':
            return super()._send_refund_request(
                amount_to_refund=amount_to_refund,
                create_refund_transaction=create_refund_transaction,
            )
        raise ValidationError(_(
            'Nexi refund is not enabled: the back-office URL from developer.nexi.it is missing.'
        ))

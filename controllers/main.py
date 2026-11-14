# -*- coding: utf-8 -*-
"""Nexi return and Build routes: locate the transaction and verify it server-side."""
import hashlib
import json
import logging
import time

from odoo import _
from odoo import http
from odoo.http import request
from odoo.addons.payment.controllers.post_processing import PaymentPostProcessing

from ..models.payment_provider import (
    _nexi_clean_base_url,
    _nexi_sanitize_text,
    is_nexi_url,
)
from ..models.payment_transaction import _nexi_lock_row, _nexi_safe_fields

_logger = logging.getLogger(__name__)

# Simple per-IP limits: no confirmation, order or log abuse.
# They live in process memory: every worker has its own counters.
_NEXI_RL = {}

# Browser return: per IP is fine, there is one customer at a time.
BROWSER_LIMIT = 30
# Nexi notifications: they all arrive from the terminal IP and concern
# different customers, so they are counted per transaction (tok), not per IP.
NOTIFY_LIMIT = 60
# Global cap per worker: rotating X-Forwarded-For must not turn the per-IP
# buckets into a way of never being limited.
GLOBAL_LIMIT = 1200
_GLOBAL_KEY = 'nexi:global'


def _nexi_global_allow():
    return _nexi_rl_allow(_GLOBAL_KEY, GLOBAL_LIMIT)


def _nexi_admit(key, limit):
    """Per-client limit before the global cap.

    The global cap is only charged to admitted requests: a request already
    refused by the per-client bucket must not be able to exhaust the shared
    budget and turn Nexi notifications into 429s.
    """
    if not _nexi_rl_allow(key, limit):
        return False
    return _nexi_global_allow()


def _nexi_rl_allow(key, limit, window=60):
    now = time.time()
    if len(_NEXI_RL) > 2000:
        # Drop only EXPIRED buckets: before, the 1000 oldest were deleted, so
        # anyone could reset the counters of the others by filling the
        # dictionary with new keys.
        expired = [k for k, v in _NEXI_RL.items() if not v or now - v[-1] >= window]
        for k in expired[:2000]:
            _NEXI_RL.pop(k, None)
        if len(_NEXI_RL) > 4000 and key not in _NEXI_RL:
            # Too many active keys even after the cleanup: only those asking
            # for a new one are refused, existing buckets keep counting.
            return False
    lst = [t for t in (_NEXI_RL.get(key) or []) if now - t < window]
    if len(lst) >= limit:
        _NEXI_RL[key] = lst
        return False
    lst.append(now)
    _NEXI_RL[key] = lst
    return True


def _nexi_rl_bucket(value, size=32):
    """Fixed-size key part: the caller does not decide the space it takes."""
    return hashlib.sha256(str(value or '').encode('utf-8', 'replace')).hexdigest()[:size]


def _nexi_rl_key(name):
    try:
        ip = request.httprequest.remote_addr or '?'
    except Exception:
        ip = '?'
    return 'nexi:%s:%s' % (name, ip)


def _nexi_tok():
    """Transaction code: header, JSON body, params or query string.

    The header is the preferred form on JSON routes (it does not end up in the
    access log); query and body stay for the Nexi return URLs and old links.
    """
    tok = None
    try:
        tok = request.httprequest.headers.get('X-Nexi-Token')
    except Exception:
        pass
    if not tok:
        try:
            data = request.get_json_data()
        except Exception:
            data = None
        if isinstance(data, dict):
            tok = data.get('tok')
            params = data.get('params')
            if not tok and isinstance(params, dict):
                tok = params.get('tok')
    if not tok:
        try:
            tok = request.params.get('tok')
        except Exception:
            pass
    if not tok:
        try:
            tok = request.httprequest.args.get('tok')
        except Exception:
            pass
    tok = str(tok or '').strip()
    return tok[:128] or None


def _nexi_csrf_ok():
    """Odoo 18 does not validate CSRF on type='json' routes: done here.

    The token lives in request.params (JSON-RPC params) and is compared with
    the session secret. Without a token the route must have no effect.
    """
    try:
        token = request.params.get('csrf_token')
        return bool(token) and request.validate_csrf(token)
    except Exception:
        return False


def _nexi_site_base(provider, website=None):
    """Configured public base: never Host or X-Forwarded-Proto."""
    base = _nexi_clean_base_url(provider.nexi_public_base)
    if not base and website is not None:
        domain = (getattr(website, 'domain', '') or '').strip()
        if domain:
            if '://' not in domain:
                domain = 'https://' + domain
            base = _nexi_clean_base_url(domain)
    if not base:
        base = _nexi_clean_base_url(provider.get_base_url())
    return base


def _nexi_confirm_limit(is_notify, kw):
    """(key, limit) of the limiter for /payment/nexi/confirm.

    The Nexi terminal sends the notifications from the same IP for every
    customer: counting them per IP would block a busy shop. The browser return
    stays per IP instead, because there it is one customer at a time.
    """
    if is_notify:
        # Fixed-size key: the caller does not decide how much memory it takes.
        # The token or the ref are parsed BEFORE this call (the body too), so
        # notifications of different transactions no longer share a 60/min
        # bucket.
        kw = kw or {}
        ident = kw.get('tok') or kw.get('ref') or 'no-ref'
        return 'nexi:notify:%s' % _nexi_rl_bucket(ident), NOTIFY_LIMIT
    return _nexi_rl_key('confirm'), BROWSER_LIMIT


def _nexi_tx_or_404(reference):
    """Transaction only with its own code. No right code: 404, no Nexi call."""
    tx = request.env['payment.transaction'].sudo().search([
        ('reference', '=', reference), ('provider_code', '=', 'nexi'),
    ], limit=1)
    if not tx:
        return None
    if not tx.nexi_access_token or _nexi_tok() != tx.nexi_access_token:
        return None
    return tx


def _nexi_landing_route(landing, has_order):
    """Where to send the browser after the payment.

    The route decided by the flow (checkout, portal, invoice) stays as it is:
    only when it is missing or points to the status page do we replace it,
    otherwise the polling reloads forever. For a shop order the target is the
    checkout validation; for a payment without an order (invoice, portal)
    there is no checkout to validate, so the original route is kept.
    """
    text = str(landing or '')
    # Only relative paths on the same site: a landing_route also comes from the
    # payment URLs and must not turn the confirmation into an open redirect
    # (or a javascript:) on the shop domain.
    if (text.startswith('/') and not text.startswith('//')
            and '\\' not in text
            and not any(ch in text for ch in ('\r', '\n', '\t', '\x00'))
            and not text.startswith('/payment/status')):
        return text
    return '/shop/payment/validate' if has_order else '/payment/confirmation'


def _nexi_public_error(e, source='Nexi'):
    """Only safe messages to the browser, the rest stays in the logs."""
    msg = str(e) or ''
    # Our own messages, translated exactly as the raiser did: with the same
    # _() a match holds in every shop language. Prefix matching would fail as
    # soon as the message stops being English.
    safe = (
        _('PayPal Pay in 3 requires an order of at least €30.00.'),
        _('Nexi supports EUR only.'),
        _('Nexi X-Api-Key is missing in the provider.'),
        _('Build requires a public URL (Shop public base or web.base.url); '
          'localhost is not accepted. Use HPP for local tests.'),
        _('Invalid Nexi method.'),
        _('Method not available on this terminal.'),
        _('Card method missing.'),
        _('Invalid order.'),
        _('Order already paid.'),
        _('Embedded checkout is not enabled.'),
        _('Too many requests. Try again in one minute.'),
        _('Session expired. Reload the page.'),
    )
    # Odoo/website_sale messages, English and Italian core terms: they are not
    # our text and are not translated by this module.
    core = (
        'Your cart is not ready',
        'Il tuo carrello non è pronto',
        'No shipping method is selected',
        'The delivery method is not compatible',
    )
    if msg in safe or msg.startswith(core):
        return msg[:200]
    _logger.warning('%s internal error: %s', source, _nexi_sanitize_text(msg))
    return _('Payment unavailable. Try again.')


class NexiController(http.Controller):
    # --- Shared helpers ---

    def _nexi_order_ready(self, order):
        """Same preconditions as the Odoo checkout. None = payment allowed.

        The old prepare only trusted the amount: it went through with a cart
        without a selected shipping method, with a cancelled or already paid
        order.
        """
        if not order or not order.id:
            return _('Invalid order.')
        if order.state == 'cancel':
            return _('Invalid order.')
        try:
            # website_sale method: cart ready + valid shipping.
            order._check_cart_is_ready_to_be_paid()
        except Exception as e:
            # Only the expected messages reach the browser: the rest (text of a
            # third-party module, ORM error) stays in the log, as for every
            # other error.
            return _nexi_public_error(e, source='Cart')
        compare = order.currency_id.compare_amounts
        if compare(order.amount_paid, order.amount_total) == 0:
            return _('Order already paid.')
        if order.amount_total <= 0:
            return _('Invalid order.')
        return None

    def _nexi_lock_order(self, order):
        """Lock the order the way Odoo does: two tabs do not pay twice.

        Savepoint: if the lock is missing (another payment is running) the
        transaction stays usable and we answer "try again" instead of breaking
        it.
        """
        try:
            with request.env.cr.savepoint():
                request.env.cr.execute(
                    'SELECT 1 FROM sale_order WHERE id = %s FOR NO KEY UPDATE NOWAIT',
                    (order.id,),
                )
        except Exception:
            return False
        return True

    def _nexi_lock_tx(self, tx):
        """Lock the row without waiting (see _nexi_lock_row)."""
        return _nexi_lock_row(request.env.cr, tx.id)

    def _nexi_finish(self, tx, is_notify):
        """Final answer: notification -> OK, browser -> status page."""
        if is_notify:
            return 'OK'
        # Tell the status page: without this key in the session Odoo does not
        # find the payment and shows "pending".
        PaymentPostProcessing.monitor_transaction(tx)
        return request.redirect('/payment/status')

    def _nexi_provider(self, order, mode=None, provider_id=None):
        """Nexi provider compatible with the order, company, SITE and currency.

        The rules are Odoo's (company, state, publication, country, currency)
        plus the site: `website_payment` filters on `provider.website_id` only
        when it receives `website_id`, so without that parameter two sites of
        the same company stole the terminal from each other.
        """
        providers = request.env['payment.provider'].sudo()._get_compatible_providers(
            order.company_id.id, order.partner_id.id, order.amount_total,
            order.currency_id.id,
            website_id=request.website.id if request.website else None,
        )
        providers = providers.filtered(lambda p: p.code == 'nexi')
        if mode:
            providers = providers.filtered(lambda p: p.nexi_mode == mode)
        if provider_id:
            # The provider asked for by the browser only counts if it is among
            # the compatible ones: an invented id must not choose the terminal.
            providers = providers.filtered(lambda p: p.id == provider_id)
        elif len(providers) > 1:
            _logger.warning(
                'Nexi: %s providers compatible with order %s (%s), using the first '
                'by sequence. Configure a single provider per site.',
                len(providers), order.name, providers.ids,
            )
        return providers.sorted(lambda p: (p.sequence, p.id))[:1]

    @http.route('/payment/nexi/saved-card', type='json', auth='public', website=True, methods=['POST'], csrf=True)
    def nexi_saved_card(self, contract_id=None, **kw):
        """Remember which saved card the customer chose for this order.

        The Odoo form does not carry extra fields down to the provider: the
        choice travels in the session and is read back in
        _get_specific_rendering_values.
        """
        if not _nexi_csrf_ok():
            return {'error': _('Session expired. Reload the page.')}
        if not _nexi_admit(_nexi_rl_key('saved'), 30):
            return {'error': _('Too many requests. Try again in one minute.')}
        order = request.website.sale_get_order()
        partner = (order.partner_invoice_id or order.partner_id) if order else request.env.user.partner_id
        if not partner:
            return {'ok': True}
        choice = str(contract_id or '').strip()
        if choice:
            # It is valid for the partner, not for the order: that way it also
            # works for an invoice paid from the portal, where there is no cart.
            # One search for the contracts, and only the terminals of the right
            # company.
            company_id = order.company_id.id if order else request.env.company.id
            providers = request.env['payment.provider'].sudo().search([
                ('code', '=', 'nexi'),
                ('company_id', 'in', [False, company_id]),
            ])
            cards = {
                c['contract_id']
                for c in request.env['payment.transaction'].sudo().get_nexi_saved_cards(
                    partner.id, providers.ids
                )
            }
            # A contract that is not the customer's counts as "new card".
            choice = choice if choice in cards else ''
        request.session['nexi_saved_contract'] = {
            'partner_id': partner.commercial_partner_id.id or partner.id,
            'contract_id': choice,
        }
        return {'ok': True}

    @http.route('/payment/nexi/confirm', type='http', auth='public', csrf=False, methods=['GET', 'POST'])
    def nexi_confirm(self, **kw):
        # The Nexi terminal sends the notifications from the same IP for every
        # customer: counting them per IP would block a busy shop. The browser
        # instead stays limited per IP.
        is_notify = request.httprequest.method == 'POST'
        # The Nexi terminal sends the notifications from the same IP: the IP
        # limit stays in front of the body, so an anonymous burst does not even
        # reach the JSON parsing. Then the bucket is per ref/token, not shared.
        if is_notify and not _nexi_rl_allow(
                _nexi_rl_key('notify-ip'), NOTIFY_LIMIT * 10):
            return request.make_response(
                _('Too many requests. Try again in one minute.'), status=429)
        # kw = parameters Nexi adds to the return (ref, cancel, ...).
        if is_notify and not kw.get('ref'):
            # The notification arrives as JSON, not as a query string.
            try:
                body = json.loads(request.httprequest.get_data() or '{}')
            except Exception:
                body = {}
            if not isinstance(body, dict):
                body = {}
            order = body.get('orderId')
            if not order and isinstance(body.get('order'), dict):
                order = body['order'].get('orderId')
            if order:
                kw['ref'] = order
        key, limit = _nexi_confirm_limit(is_notify, kw)
        if not _nexi_admit(key, limit):
            return request.make_response(
                _('Too many requests. Try again in one minute.'), status=429)
        try:
            tx = request.env['payment.transaction'].sudo()._get_tx_from_notification_data(
                'nexi', kw
            )
        except Exception:
            return request.not_found()
        # Only whoever holds the transaction code goes on. Without the right
        # code the answer is the same as "not found", without calling Nexi.
        if not tx.nexi_access_token or _nexi_tok() != tx.nexi_access_token:
            return request.not_found()
        # Only ref/cancel/notify are passed to Nexi: the order to check is
        # decided by the transaction, never by the caller. The -m suffix of the
        # APMs is the only exception (always validated in the model).
        ref_in = str(kw.get('ref') or '')
        notify_data = {'cancel': kw.get('cancel'), 'notify': kw.get('notify')}
        if ref_in == tx.reference + '-m':
            notify_data['nexi_order_ref'] = ref_in
        # Serialize on the row BEFORE touching the state: in Odoo 18
        # _update_state() writes is_post_processed=False at every transition,
        # so a lock taken later would let a second post-processing through
        # (notification + browser return at the same time).
        #
        # NOWAIT: the verification can take tens of seconds (network timeouts),
        # so whoever arrives second does not queue up — it leaves and lets the
        # first flow finish. The customer sees the status page, which re-reads
        # the transaction as soon as the other flow has closed it.
        if not self._nexi_lock_tx(tx):
            _logger.info('Nexi %s: closing already in progress in another flow.', tx.reference)
            return self._nexi_finish(tx, is_notify)
        tx.invalidate_recordset()
        if tx.is_post_processed and tx.state in ('done', 'cancel', 'error'):
            # Already closed and post-processed by another flow: neither the
            # verification nor the confirmation is repeated.
            _logger.info('Nexi %s already closed and post-processed: skipping.', tx.reference)
        else:
            try:
                tx._process_notification_data(notify_data)
            except (ValueError, TypeError, KeyError, AttributeError,
                    RecursionError, OverflowError):
                # Unexpected gateway shapes: not a 500, but an uncertain
                # transition that the cron can still recover.
                _logger.exception(
                    'Nexi confirmation %s: unreadable response', tx.reference)
                tx._set_pending(state_message=_('Invalid Nexi response. Try again.'))
            tx.invalidate_recordset()
            # Without this Odoo neither confirms the order nor creates the invoice.
            if not tx.is_post_processed:
                tx._post_process()
        # Final route: the one decided by the flow (checkout, portal, invoice)
        # must be left alone. It is touched only when missing or pointing to the
        # status page, which would otherwise reload in a loop.
        tx.landing_route = _nexi_landing_route(tx.landing_route, bool(tx.sale_order_ids))
        return self._nexi_finish(tx, is_notify)

    @http.route('/payment/nexi/build/<string:reference>', type='http', auth='public', website=True, methods=['GET'])
    def nexi_build(self, reference, **kw):
        if not _nexi_admit(_nexi_rl_key('build'), 30):
            return request.make_response(
                _('Too many requests. Try again in one minute.'), status=429)
        tx = _nexi_tx_or_404(reference)
        if not tx or not tx.nexi_build_session:
            return request.not_found()
        tok = tx.nexi_access_token
        response = request.render('payment_nexi.build_page', {
            'tx_reference': tx.reference,
            'nexi_ui': tx._nexi_ui_strings(),
            'nexi_ui_json': tx._nexi_ui_strings_json(),
            'nexi_sdk': tx.provider_id._nexi_sdk_url(),
            'build_session': tx.nexi_build_session,
            'build_token': tx.nexi_build_token or '',
            # The JSON routes take the tok from the header, not from the query.
            'build_state_url': f'/payment/nexi/build/{tx.reference}/fields',
            'build_finalize_url': f'/payment/nexi/build/{tx.reference}/finalize',
            'build_tok': tok,
            'csrf_token': request.csrf_token(),
            'confirm_url': f'/payment/nexi/confirm?ref={tx.reference}&tok={tok}',
        })
        response.headers['Referrer-Policy'] = 'no-referrer'
        return response

    @http.route('/payment/nexi/build/prepare', type='json', auth='public', website=True, methods=['POST'], csrf=True)
    def nexi_build_prepare(self, method_code='card', tokenize=False, provider_id=None, **kw):
        """Create transaction + Build order and return fields or the APM URL."""
        # The browser sends JSON: an id/method of a strange type must not blow
        # up the ORM search (500) but be refused.
        if not _nexi_csrf_ok():
            return {'error': _('Session expired. Reload the page.')}
        method_code = str(method_code or 'card').strip().lower()[:32] or 'card'
        if provider_id not in (None, False, ''):
            try:
                provider_id = int(provider_id)
            except (TypeError, ValueError):
                return {'error': _('Method not available on this terminal.')}
        else:
            provider_id = None
        if not _nexi_admit(_nexi_rl_key('prepare'), 20):
            return {'error': _('Too many requests. Try again in one minute.')}
        order = request.website.sale_get_order()
        # The same gates the Odoo checkout closes: without these a Nexi order
        # could be created for a cart that cannot be paid.
        cart_error = self._nexi_order_ready(order)
        if cart_error:
            return {'error': cart_error}
        if (order.currency_id.name or '') != 'EUR':
            return {'error': _('Nexi supports EUR only.')}
        provider = self._nexi_provider(order, mode='build', provider_id=provider_id)
        if not provider:
            return {'error': _('Embedded checkout is not enabled.')}
        if not self._nexi_lock_order(order):
            # Another payment is already running on this order.
            return {'error': _('A payment is already in progress. Try again in a moment.')}
        # The verdict read before the lock can be stale: after locking the order
        # it is recomputed, otherwise two requests can create two Nexi orders on
        # the same cart.
        order.invalidate_recordset()
        cart_error = self._nexi_order_ready(order)
        if cart_error:
            return {'error': cart_error}
        method = request.env['payment.method'].sudo().search(
            [('code', '=', method_code), ('is_primary', '=', True)], limit=1,
        )
        if not method:
            return {'error': _('Card method missing.')}
        # The method must belong to THIS terminal and must not be refused by it:
        # the row at checkout is shared, the terminal is not.
        if method not in provider.payment_method_ids or method._nexi_is_disabled(provider.id):
            return {'error': _('Method not available on this terminal.')}
        Tx = request.env['payment.transaction'].sudo()
        # Configured public base (provider field, site domain, web.base.url).
        # Host and X-Forwarded-Proto never decide the merchantUrl.
        origin = _nexi_site_base(provider, request.website)
        extra = {'base_url': origin} if origin else {}
        tx = Tx.search([
            ('sale_order_ids', 'in', order.id),
            ('provider_id', '=', provider.id),
            ('payment_method_id', '=', method.id),
            ('state', '=', 'draft'),
        ], order='id desc', limit=1)
        if tx:
            # The cart can change: the draft must follow the real amount,
            # otherwise the old figure is paid (or refused).
            refresh = {}
            if tx.amount != order.amount_total:
                refresh['amount'] = order.amount_total
            if tx.currency_id != order.currency_id:
                refresh['currency_id'] = order.currency_id.id
            if tx.partner_id != order.partner_invoice_id:
                refresh['partner_id'] = order.partner_invoice_id.id
            if refresh:
                tx.write(refresh)
        if not tx:
            base = order.name or 'S'
            for _i in range(5):
                n = Tx.search_count([('sale_order_ids', 'in', order.id)])
                ref = base if not n else '%s-%s' % (base, n)
                try:
                    with request.env.cr.savepoint():
                        tx = Tx.create({
                            'provider_id': provider.id,
                            'payment_method_id': method.id,
                            'amount': order.amount_total,
                            'currency_id': order.currency_id.id,
                            'partner_id': order.partner_invoice_id.id,
                            'reference': ref,
                            'operation': 'online_redirect',
                            # Final route: it leads to the order confirmation.
                            # If it stays empty or points to /payment/status, the
                            # status page reloads in a loop.
                            'landing_route': '/shop/payment/validate',
                            'sale_order_ids': [(6, 0, [order.id])],
                        })
                    break
                except Exception as e:
                    # Reference already taken by a parallel request: retry.
                    # Always log: a hidden error here turns the whole checkout off.
                    _logger.warning(
                        'prepare: tx %s not created (attempt %s/5): %s',
                        ref, _i + 1, _nexi_sanitize_text(e, 200))
                    tx = None
                    time.sleep(0.05 * (_i + 1))
                    continue
            if not tx:
                tx = Tx.search([
                    ('sale_order_ids', 'in', order.id),
                    ('provider_id', '=', provider.id),
                    ('payment_method_id', '=', method.id),
                    ('state', '=', 'draft'),
                ], order='id desc', limit=1)
            if not tx:
                _logger.warning('prepare: cannot create the transaction for %s', order.name)
                return {'error': _('Payment unavailable. Try again.')}
        try:
            if method_code and method_code != 'card':
                # APM: Nexi returns the URL of its payment page right away.
                res_apm = tx._get_specific_rendering_values(dict({
                    'amount': tx.amount,
                    'currency_id': tx.currency_id.id,
                    'partner_id': tx.partner_id.id,
                    'reference': tx.reference,
                    'should_tokenize': False,
                    'force_build': True,
                }, **extra))
                url = res_apm.get('build_url') or False
                if url and not is_nexi_url(url):
                    _logger.warning('prepare: APM URL dropped for %s', tx.reference)
                    return {'error': _('Payment unavailable. Try again.')}
                return {
                    'tx': tx.reference,
                    'url': url,
                    'fields': [],
                    'buttons': [],
                }
            res_cards = tx._get_specific_rendering_values(dict({
                'amount': tx.amount,
                'currency_id': tx.currency_id.id,
                'partner_id': tx.partner_id.id,
                'reference': tx.reference,
                'should_tokenize': False,
                'force_build': True,
            }, **extra))
            res_buttons = tx._get_specific_rendering_values(dict({
                'amount': tx.amount,
                'currency_id': tx.currency_id.id,
                'partner_id': tx.partner_id.id,
                'reference': tx.reference,
                'should_tokenize': False,
                'force_build': True,
                'no_service': True,
                'order_suffix': '-m',
            }, **extra))
        except Exception as e:
            msg = str(e)
            # Pay in 3 has a EUR 30 minimum on the Nexi side: state the real
            # reason and make the row disappear, instead of a generic error.
            if method_code == 'paypal_bnpl' and ('PS0064' in msg or 'GW0043' in msg):
                return {
                    'error': _('PayPal Pay in 3 requires an order of at least €30.00.'),
                    'hide': 'paypal_bnpl',
                }
            return {'error': _nexi_public_error(e)}
        tok = tx.nexi_access_token
        return {
            'tx': tx.reference,
            'fields': res_cards.get('build_fields') or [],
            'buttons': res_buttons.get('build_fields') or [],
            'sdk': provider._nexi_sdk_url(),
            # finalize is JSON: tok in the header, not in the query.
            'finalize_url': f'/payment/nexi/build/{tx.reference}/finalize',
            'tok': tok,
            'confirm_url': f'/payment/nexi/confirm?ref={tx.reference}&tok={tok}',
        }

    @http.route('/payment/nexi/build/jserror', type='json', auth='public', website=True, methods=['POST'], csrf=True)
    def nexi_build_jserror(self, msg=None, **kw):
        # Without a token nothing is logged: the token comes from our JS handlers.
        if not _nexi_csrf_ok():
            return True
        if not _nexi_admit(_nexi_rl_key('jserror'), 10):
            return True
        # One line, no ANSI/bidi/control: the logs stay clean.
        clean = _nexi_sanitize_text(msg)
        _logger.warning('browser: %s', clean)
        return True

    @http.route('/payment/nexi/build/<string:reference>/fields', type='http', auth='public', website=True, methods=['GET'])
    def nexi_build_fields(self, reference, **kw):
        """Only the fields to show, never the raw Nexi response."""
        # Limit BEFORE the lookup: otherwise a flood without a tok still runs
        # one sudo search per request.
        if not _nexi_admit(_nexi_rl_key('fields'), 30):
            return request.make_response(
                json.dumps({'fields': []}), [('Content-Type', 'application/json')], status=429
            )
        tx = _nexi_tx_or_404(reference)
        if not tx or not tx.nexi_build_session:
            return request.not_found()
        provider = tx.provider_id
        base = provider._nexi_build_url().rsplit('/orders/build', 1)[0]
        try:
            data = provider._nexi_make_request(
                'GET', base + '/build/state',
                params={'sessionId': tx.nexi_build_session},
            )
        except Exception:
            return request.make_response(json.dumps({'fields': []}), [('Content-Type', 'application/json')])
        data = data if isinstance(data, dict) else {}
        # Whitelist: only id/class/type/src of the iframes, no operation, and
        # only src on a Nexi domain (those are the iframes where the card is
        # typed).
        out = []
        fs = data.get('fieldSet')
        fs = fs if isinstance(fs, dict) else {}
        for f in _nexi_safe_fields(fs.get('fields')):
            if not f.get('src'):
                continue
            out.append({
                'id': f.get('id'),
                'class': f.get('class'),
                'type': f.get('type'),
                'src': f.get('src'),
            })
        return request.make_response(
            json.dumps({'state': (data or {}).get('state'), 'fields': out}),
            [('Content-Type', 'application/json')],
        )

    @http.route('/payment/nexi/build/<string:reference>/finalize', type='json', auth='public', methods=['POST'], csrf=True)
    def nexi_build_finalize(self, reference, **kw):
        """Call finalize on Nexi (the key stays on the server). Returns url or end."""
        if not _nexi_csrf_ok():
            return {'error': _('Session expired. Reload the page.')}
        if not _nexi_admit(_nexi_rl_key('finalize'), 30):
            return {'error': _('Too many requests. Try again in one minute.')}
        tx = _nexi_tx_or_404(reference)
        if not tx or not tx.nexi_build_session:
            return {'error': 'not found'}
        provider = tx.provider_id
        base = provider._nexi_build_url().rsplit('/orders/build', 1)[0]
        try:
            data = provider._nexi_make_request(
                'POST', base + '/build/finalize_payment',
                payload={'sessionId': tx.nexi_build_session},
            )
        except Exception as e:
            msg = str(e)[:300]
            if 'GW0009' in msg or 'not allowed in the current state' in msg:
                # Session consumed or in the wrong state: next time a new one is
                # created instead of staying stuck.
                tx.nexi_build_session = False
                tx.nexi_build_token = False
            return {'error': _nexi_public_error(e)}
        data = data if isinstance(data, dict) else {}
        url = data.get('url')
        if url and not is_nexi_url(url):
            _logger.warning('finalize: URL dropped for %s', tx.reference)
            return {'error': _('Payment unavailable. Try again.')}
        return {
            'state': (data or {}).get('state'),
            'url': url,
        }

# -*- coding: utf-8 -*-
"""Nexi XPay Web provider: Hosted Page redirect or embedded Build v3 card fields. No card data in Odoo."""
import logging
import re
import threading
import time
import uuid
from urllib.parse import urlparse

import requests

from odoo import _, fields, models
from odoo.exceptions import ValidationError

_logger = logging.getLogger(__name__)

# A sync worker cannot be kept hanging on a dripping gateway: the requests
# timeout is per read, not for the whole response.
NEXI_TOTAL_TIMEOUT = 20
# Nexi responses are small: a huge body is an attack, not an order.
NEXI_MAX_BODY = 1024 * 1024

_ANSI_RE = re.compile(r'\x1b\[[0-?]*[ -/]*[@-~]')
_BIDI_RE = re.compile('[\u202a-\u202e\u2066-\u2069\u200e\u200f]')
_CTRL_RE = re.compile(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]')


def _nexi_sanitize_text(value, limit=300):
    """Gateway text ready for a log: one line, no ANSI/bidi/control chars."""
    text = _ANSI_RE.sub('', str(value or ''))
    text = _BIDI_RE.sub('', text)
    text = _CTRL_RE.sub(' ', text)
    text = text.replace('\r', ' ').replace('\n', ' ').replace('\t', ' ')
    return re.sub(r'\s+', ' ', text).strip()[:limit]


def _nexi_clean_base_url(value):
    """Valid public https base, or an empty string.

    The request Host header is never used: a proxy that does not sanitize the
    headers could send the return URLs to a domain chosen by the attacker.
    """
    text = str(value or '').strip().rstrip('/')
    if not text:
        return ''
    if any(ch in text for ch in ('\\', ' ', '\t', '\r', '\n', '\x00')):
        return ''
    try:
        parts = urlparse(text)
        host = (parts.hostname or '').lower()
        port = parts.port
    except ValueError:
        return ''
    if (parts.scheme or '').lower() != 'https' or not host:
        return ''
    if parts.username or parts.password or '@' in (parts.netloc or ''):
        return ''
    if port not in (None, 443):
        return ''
    if host in ('localhost', '127.0.0.1', '::1') or host.endswith('.localhost'):
        return ''
    return ('https://' + parts.netloc + (parts.path or '')).rstrip('/')


def _nexi_base_allowed(value, allowed_hosts):
    """True if the base host is one of the hosts configured for the site."""
    try:
        host = (urlparse(str(value or '')).hostname or '').lower()
    except ValueError:
        return False
    return bool(host) and host in {str(h or '').lower() for h in allowed_hosts}


def _nexi_read_limited(resp, max_bytes=NEXI_MAX_BODY, deadline=None, now=time.monotonic):
    """Reads the body in chunks, with a byte cap and an overall deadline."""
    length = resp.headers.get('Content-Length')
    if length:
        try:
            if int(length) > max_bytes:
                raise NexiUnavailable(_('Invalid Nexi response. Try again.'))
        except (TypeError, ValueError):
            pass
    chunks = []
    total = 0
    for chunk in resp.iter_content(8192):
        if deadline is not None and now() > deadline:
            raise NexiUnavailable(_('Nexi is unreachable. Try again.'))
        if not chunk:
            continue
        total += len(chunk)
        if total > max_bytes:
            raise NexiUnavailable(_('Invalid Nexi response. Try again.'))
        chunks.append(chunk)
    return b''.join(chunks)


def _nexi_bounded(call, timeout=None):
    """Runs `call` within `timeout` seconds, otherwise NexiUnavailable.

    `requests` has no overall timeout: the per-read one does not stop a
    gateway that sends one header byte every few seconds. The thread that runs
    the call is a daemon, so the sync worker is free again at the deadline and
    a process that stops does not wait for it. The connection stays open until
    the gateway stops: that is the price for not keeping the worker hanging.
    """
    timeout = NEXI_TOTAL_TIMEOUT if timeout is None else timeout
    box = {}

    def runner():
        try:
            box['value'] = call()
        except BaseException as exc:  # re-raised in the caller
            box['error'] = exc

    worker = threading.Thread(target=runner, name='nexi-http', daemon=True)
    worker.start()
    worker.join(timeout)
    if worker.is_alive():
        _logger.warning('Nexi: total deadline of %ss exceeded', timeout)
        raise NexiUnavailable(_('Nexi is unreachable. Try again.'))
    error = box.get('error')
    if error is not None:
        raise error.with_traceback(error.__traceback__)
    return box.get('value')


NEXI_PROD_BASE = 'https://xpay.nexigroup.com'
NEXI_SANDBOX_BASE = 'https://xpaysandbox.nexigroup.com'
# Allowed Nexi domains. Only these: production and sandbox.
# (xpay.nexi.it no longer resolves: removed, the JS never accepted it.)
NEXI_HOSTS = tuple(
    urlparse(u).hostname for u in (NEXI_PROD_BASE, NEXI_SANDBOX_BASE)
)
# Odoo payment.method codes linked to a Nexi provider.
NEXI_METHOD_CODES = (
    'card', 'googlepay', 'applepay', 'bancomatpay', 'mybank', 'paypal',
    'paypal_bnpl', 'visa', 'mastercard', 'amex',
)

# Test amount for "Check methods". Some services have their own minimum:
# PayPal Pay in 3 below EUR 30 is refused by Nexi (PS0064/GW0043), so the check
# would see an amount error instead of the real availability.
CHECK_AMOUNTS = {'PAYPAL_PAGAIN3': '5000'}
CHECK_AMOUNT_DEFAULT = '100'

# Texts with which Nexi says "this service is not enabled on this terminal".
# Only these disable a method: a timeout or a wrong key does not.
REJECTION_MARKERS = (
    'not enabled', 'not_enabled', 'not allowed', 'not_allowed',
    'not configured', 'not_configured', 'not available', 'not_available',
    'service not', 'payment service not',
    'ps0064', 'gw0043',
)


class NexiUnavailable(ValidationError):
    """Nexi did not answer (network, timeout, unreadable response).

    It says nothing about the method that was tried: the receiver must not
    disable anything, because the problem is the connection or the
    configuration.
    """


class NexiApiError(ValidationError):
    """Nexi answered with an error of its own: HTTP status and details.

    They tell a method disabled on the terminal (400 with
    "paymentService not enabled") from a wrong key or URL (401/403) and from a
    temporary failure (429/5xx).
    """

    def __init__(self, message, status_code=None):
        super().__init__(message)
        self.status_code = status_code


def _classify_nexi_check_error(err):
    """'config' | 'disabled' | 'unknown' for the method check.

    - ``config``: wrong key or URL (401/403, or our own configuration
      messages) -> stop the check and tell the admin.
    - ``disabled``: Nexi said the service is not enabled on this terminal ->
      disable the method.
    - ``unknown``: temporary failure or a response never seen before -> touch
      nothing.
    """
    status = getattr(err, 'status_code', None)
    if status in (401, 403):
        return 'config'
    if status and (status in (408, 429) or status >= 500):
        # Temporary failure (rate limit, gateway down): it does not speak about
        # the method. It must be checked BEFORE the markers, otherwise a
        # "Service not available" on a 503 disables methods that work.
        return 'unknown'
    msg = str(err) or ''
    low = msg.lower()
    if any(k in low for k in (
        'missing url', 'url is missing', 'x-api-key', 'invalid nexi url',
        'https on a nexi domain',
    )):
        return 'config'
    if any(marker in low for marker in REJECTION_MARKERS):
        return 'disabled'
    return 'unknown'


# Characters an HTTP client can read as an authority separator but urlparse
# treats as plain text: with these a URL "looks" like Nexi and ends up
# elsewhere.
_URL_FORBIDDEN_CHARS = ('\\', ' ', '\t', '\r', '\n', '\x00', '\x0b', '\x0c')


def is_nexi_url(url):
    """True only for https on a Nexi domain, with a "clean" authority.

    The only place where the domain is checked: both the provider and the
    controller use it, so the two rules cannot diverge.

    `urlparse().hostname` is not enough: with a backslash or a userinfo the
    Python parser and the HTTP client parser (urllib3) read two different
    hosts. For `https://evil.tld\\@xpay.nexigroup.com/...` urlparse says
    `xpay.nexigroup.com` while requests calls `evil.tld`, and the X-Api-Key
    would leave towards the attacker. Here the netloc must be exactly the
    expected host (or `host:443`), with no userinfo, backslash or odd port.
    """
    if not url:
        return False
    text = str(url).strip()
    if any(ch in text for ch in _URL_FORBIDDEN_CHARS):
        return False
    try:
        parts = urlparse(text)
        host = (parts.hostname or '').lower()
        netloc = (parts.netloc or '').lower()
        port = parts.port
    except ValueError:
        return False
    if (parts.scheme or '').lower() != 'https' or not host:
        return False
    if parts.username or parts.password or '@' in netloc:
        return False
    if port not in (None, 443):
        return False
    # The netloc must be the bare host: no userinfo, ports or odd characters.
    if netloc not in (host, host + ':443'):
        return False
    return any(host == h or host.endswith('.' + h) for h in NEXI_HOSTS)


def _nexi_error_detail(resp):
    """First useful description from Nexi, without sensitive data."""
    try:
        payload = resp.json() or {}
        errs = payload.get('errors') if isinstance(payload, dict) else None
        if isinstance(errs, list) and errs and isinstance(errs[0], dict):
            desc = _nexi_sanitize_text(errs[0].get('description'), 160)
            code = _nexi_sanitize_text(errs[0].get('code'), 16)
            if desc:
                return '%s [%s]' % (desc, code) if code else desc
    except (ValueError, AttributeError, RecursionError, OverflowError):
        pass
    return 'error. Try again.'


class PaymentProviderNexi(models.Model):
    _inherit = 'payment.provider'

    code = fields.Selection(selection_add=[('nexi', 'Nexi XPay Web')], ondelete={'nexi': 'set default'})
    # API key = terminal password. You find it in Nexi Business > Back Office > Admin > APIKey.
    nexi_api_key = fields.Char(
        string='X-Api-Key', help='XPay Web terminal key.',
        groups='base.group_system',
    )
    # Test or Production. Always try Test first with the fake keys.
    nexi_env = fields.Selection(
        [('test', 'Test'), ('prod', 'Production')],
        default='test', string='Environment',
    )
    # How the checkout is shown: redirect to the Nexi page (HPP) or a form
    # inside the site (Build v3).
    nexi_mode = fields.Selection(
        [('hpp', 'Hosted Page (redirect)'), ('build', 'Embedded Build v3 (beta)')],
        default='hpp', string='Checkout mode',
        help='HPP = redirect to the Nexi page. Build = form on your site with Nexi iframes (needs a public URL; localhost is not accepted).',
    )
    # Method preselection on the Nexi page. Empty = all. Values from the Nexi
    # table (e.g. GOOGLEPAY).
    nexi_payment_service = fields.Char(
        string='Preselected method',
        help='Passed to Nexi as paymentService. Empty = all methods. Values from the PaymentService code table (e.g. GOOGLEPAY). Uppercase letters, digits and underscores only.',
    )
    # Public base of the shop (https://...). Used for resultUrl and merchantUrl.
    # If empty, web.base.url is used. The request Host header is never used.
    nexi_public_base = fields.Char(
        string='Shop public base',
        help='e.g. https://shop.example.com. If empty, web.base.url is used.',
        groups='base.group_system',
    )
    # OneClick CIT: save the card with a contract and reuse it later.
    nexi_oneclick = fields.Boolean(
        string='OneClick CIT',
        default=False,
        help='When enabled: the first payment creates a CIT contract, later ones reuse the contractId via HPP.',
    )

    # --- Base URLs. Do not type them by hand: copy them from the API page on developer.nexi.it ---
    nexi_test_url = fields.Char(
        string='Nexi test URL',
        help='Full test URL from the API page on developer.nexi.it (e.g. the Payments page).',
        groups='base.group_system',
    )
    nexi_prod_url = fields.Char(
        string='Nexi production URL',
        help='Full production URL from the API page on developer.nexi.it.',
        groups='base.group_system',
    )
    # Outcome of the last method check on Nexi. Read only.
    nexi_methods_report = fields.Text(
        string='Checked methods', readonly=True,
        help='Last check against Nexi: which paymentServices the terminal accepts.',
    )
    nexi_methods_checked = fields.Datetime(string='Checked on', readonly=True)
    # Outcome per terminal: two Nexi providers can have different methods.
    nexi_method_availability_ids = fields.One2many(
        'payment.nexi.method.availability', 'provider_id',
        string='Terminal methods', readonly=True, groups='base.group_system',
    )

    def button_nexi_check_methods(self):
        """Test every method on Nexi and store the outcome for THIS terminal."""
        self.ensure_one()
        from .payment_transaction import METHOD_SERVICE
        if not self.nexi_api_key:
            raise ValidationError(_(
                'X-Api-Key is missing: methods cannot be checked without it.'
            ))
        service_by_method = {}
        for m in self.payment_method_ids.filtered(lambda x: x.is_primary):
            if m.code in METHOD_SERVICE:
                service_by_method[m] = METHOD_SERVICE[m.code]
        if not service_by_method:
            raise ValidationError(_('No Nexi method to check.'))
        base = self._nexi_base_url()
        lines = []
        for method, service in sorted(service_by_method.items(), key=lambda x: x[0].name):
            enabled, detail = self._nexi_check_service(base, service)
            # The outcome belongs to the terminal, not to the method in general.
            self._nexi_store_availability(method, enabled, detail)
            lines.append('%s (%s): %s' % (method.name, service, detail))
        self.nexi_methods_report = '\n'.join(lines)
        self.nexi_methods_checked = fields.Datetime.now()
        return True

    def _nexi_check_service(self, base, service):
        """Test one paymentService on Nexi with a compatible amount.

        :return: (True | False | None, description). None = not checked:
                 Nexi did not answer or the configuration is broken, so the
                 method keeps its previous state.
        """
        amount = CHECK_AMOUNTS.get(service, CHECK_AMOUNT_DEFAULT)
        # Nexi: orderId is at most 27 characters. "CK" + a short service name
        # + randomness stays under the limit for every method.
        ref = 'CK%s%s' % (service.replace('_', '')[:10], uuid.uuid4().hex[:6].upper())
        payload = {
            'order': {
                'orderId': ref, 'amount': amount, 'currency': 'EUR',
                'description': ref,
            },
            'paymentSession': {
                'actionType': 'PAY', 'amount': amount, 'language': 'ITA',
                'resultUrl': 'https://example.com/nexi/check',
                'cancelUrl': 'https://example.com/nexi/check',
                'captureType': 'IMPLICIT', 'paymentService': service,
            },
        }
        try:
            self._nexi_make_request('POST', base, payload)
        except NexiUnavailable as e:
            # Network or timeout: it says nothing about the method.
            return None, 'not checked (%s)' % str(e)[:80]
        except ValidationError as e:
            kind = _classify_nexi_check_error(e)
            if kind == 'config':
                # Wrong key or URL: every method would fail for the same
                # reason, so the check stops instead of disabling them all.
                raise NexiUnavailable(_(
                    'Method check aborted: check X-Api-Key and Nexi URL (%s).'
                ) % str(e)[:120])
            if kind == 'disabled':
                return False, 'refused by the terminal'
            # An error that does not speak about the method: do not disable it.
            return None, 'error (%s)' % str(e)[:80]
        return True, 'enabled'

    def _nexi_store_availability(self, method, enabled, detail):
        """Store the check outcome for this terminal."""
        Avail = self.env['payment.nexi.method.availability'].sudo()
        values = {
            'checked_at': fields.Datetime.now(),
            'detail': (detail or '')[:120],
        }
        if enabled is not None:
            values['disabled'] = not enabled
        rec = Avail.search([
            ('provider_id', '=', self.id), ('method_id', '=', method.id),
        ], limit=1)
        if rec:
            rec.write(values)
        else:
            Avail.create(dict(values, provider_id=self.id, method_id=method.id))

    def _nexi_base_url(self):
        self.ensure_one()
        url = self.nexi_prod_url if self.nexi_env == 'prod' else self.nexi_test_url
        if not url:
            raise ValidationError(_(
                'Nexi URL is missing. Enter the test/production URL from the API page on developer.nexi.it.'
            ))
        url = url.strip().rstrip('/')
        # https + Nexi domain: the key must never leave there.
        if not is_nexi_url(url):
            raise ValidationError(_('Invalid Nexi URL: HTTPS on a Nexi domain is required.'))
        return url

    def _nexi_build_url(self):
        """Build v3 orders URL: same host as HPP, path /orders/build."""
        base = self._nexi_base_url()
        if base.endswith('/orders/hpp'):
            return base[: -len('/orders/hpp')] + '/orders/build'
        parts = urlparse(base)
        host_base = f'{parts.scheme}://{parts.netloc}'
        prefix = parts.path
        if '/orders/' in prefix:
            prefix = prefix.split('/orders/')[0]
        return host_base + prefix + '/orders/build'

    def _nexi_sdk_url(self):
        """Nexi Build SDK URL for this terminal's environment."""
        self.ensure_one()
        base = NEXI_SANDBOX_BASE if self.nexi_env == 'test' else NEXI_PROD_BASE
        return base + '/monetaweb/resources/hfsdk.js'

    def _get_supported_currencies(self):
        # The Nexi sandbox wants EUR. Keep EUR only, but only for Nexi: the
        # other providers on the same shop must not be limited.
        self.ensure_one()
        if self.code != 'nexi':
            return super()._get_supported_currencies()
        return self.env['res.currency'].with_context(active_test=False).search([('name', '=', 'EUR')])

    def _get_default_payment_method_codes(self):
        self.ensure_one()
        if self.code != 'nexi':
            return super()._get_default_payment_method_codes()
        # The core brands (Visa/Mastercard/Amex) are not primary methods:
        # without these codes _activate_default_pms leaves them off and the
        # Card row no longer shows their icons.
        return set(NEXI_METHOD_CODES)

    def _activate_default_pms(self):
        result = super()._activate_default_pms()
        if not self.filtered(lambda p: p.code == 'nexi'):
            return result
        # The Card row shows the first 4 active brands by sequence/name: the
        # core brands have sequence 1000 and would end up after other schemes.
        brands = self.env['payment.method'].with_context(active_test=False).search([
            ('code', 'in', ('visa', 'mastercard', 'amex')),
        ])
        if brands and any(m.sequence != 1 for m in brands):
            brands.write({'sequence': 1})
        return result

    def _compute_feature_support_fields(self):
        super()._compute_feature_support_fields()
        # Simple v1: no refund, no manual capture.
        # Token only with OneClick on: it shows "save card" at checkout.
        for p in self.filtered(lambda r: r.code == 'nexi'):
            p.support_refund = 'none'
            p.support_tokenization = bool(p.nexi_oneclick)
            p.support_manual_capture = False

    def _get_redirect_form_view(self, is_validation=False):
        self.ensure_one()
        if self.code == 'nexi':
            if self.nexi_mode == 'build':
                return self.env.ref('payment_nexi.redirect_build')
            return self.env.ref('payment_nexi.redirect_form')
        return super()._get_redirect_form_view(is_validation=is_validation)

    def _nexi_make_request(self, method, url, payload=None, params=None):
        """Call Nexi over REST JSON. Simple, with timeouts.

        :raise NexiUnavailable: Nexi did not answer or the response is unreadable.
        :raise ValidationError: Nexi answered with an error of its own.
        """
        self.ensure_one()
        # Last line of defence: the key does not leave unless the URL is https
        # on a "clean" Nexi domain, whoever built the URL.
        if not is_nexi_url(url):
            raise ValidationError(_('Invalid Nexi URL: HTTPS on a Nexi domain is required.'))
        headers = {
            'Content-Type': 'application/json',
            'X-Api-Key': self.nexi_api_key or '',
            'Correlation-Id': str(uuid.uuid4()),
        }
        started = time.monotonic()
        deadline = started + NEXI_TOTAL_TIMEOUT

        def call():
            try:
                # No redirects: the key must never travel to another host.
                # stream=True + _nexi_read_limited: the requests timeout is per
                # read, it does not stop a gateway that sends one byte at a time.
                resp = requests.request(
                    method, url, json=payload, params=params, headers=headers,
                    timeout=(5, 10), allow_redirects=False, stream=True,
                )
            except requests.exceptions.RequestException:
                _logger.exception('Nexi unreachable: %s', url)
                raise NexiUnavailable(_('Nexi is unreachable. Try again.'))
            # Second level: the budget may already be gone here, in which case
            # there is no point in reading the body.
            if time.monotonic() > deadline:
                resp.close()
                _logger.warning('Nexi: response past the deadline: %s', url)
                raise NexiUnavailable(_('Nexi is unreachable. Try again.'))
            try:
                if 300 <= (resp.status_code or 0) < 400:
                    _logger.warning(
                        'Nexi unexpected redirect %s -> %s',
                        url, _nexi_sanitize_text(resp.headers.get('Location'), 300),
                    )
                    raise NexiUnavailable(_('Invalid Nexi response. Try again.'))
                try:
                    raw = _nexi_read_limited(resp, deadline=deadline)
                except requests.exceptions.RequestException:
                    _logger.exception('Nexi read interrupted: %s', url)
                    raise NexiUnavailable(_('Nexi is unreachable. Try again.'))
                # The rest of the method uses resp.text/resp.json(): keep the
                # body already read (and capped) in the Response.
                resp._content = raw
                resp._content_consumed = True
                try:
                    resp.raise_for_status()
                except requests.exceptions.HTTPError:
                    _logger.error(
                        'Nexi %s %s -> %s', method, url,
                        _nexi_sanitize_text(resp.text, 500),
                    )
                    raise NexiApiError(
                        _('Nexi: %s') % _nexi_error_detail(resp),
                        status_code=resp.status_code,
                    )
                try:
                    return resp.json() if resp.text else {}
                except (ValueError, RecursionError, OverflowError):
                    raise NexiUnavailable(_('Invalid Nexi response. Try again.'))
            finally:
                resp.close()

        # One deadline across connect+TLS+headers+body: the first level is
        # here, the per-read timeout above stays as the second level.
        return _nexi_bounded(call, NEXI_TOTAL_TIMEOUT)

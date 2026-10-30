# -*- coding: utf-8 -*-
"""Provider Nexi XPay Web. Solo redirect (Hosted Page). Niente dati carta in Odoo."""
import logging
import re

import requests

from odoo import _, fields, models
from odoo.exceptions import ValidationError

_logger = logging.getLogger(__name__)

# Domini Nexi ammessi. Solo questi, resto rifiutato.
# Produzione documentata: https://xpay.nexigroup.com (scheda API developer.nexi.it).
NEXI_HOSTS = ('xpaysandbox.nexigroup.com', 'xpay.nexigroup.com', 'xpay.nexi.it')

# Importi di verifica in centesimi, per paymentService. La verifica crea un
# vero ordine di prova: deve rispettare i minimi del metodo, altrimenti un
# metodo sano risulta "rifiutato".
NEXI_CHECK_AMOUNTS = {
    'PAYPAL_PAGAIN3': '3000',  # Paga in 3: minimo Nexi 30 euro.
}
NEXI_CHECK_AMOUNT_DEFAULT = '100'


def _nexi_check_amount_cents(service):
    """Importo di verifica per un paymentService (stringa in centesimi)."""
    return NEXI_CHECK_AMOUNTS.get(service or '', NEXI_CHECK_AMOUNT_DEFAULT)


class NexiApiError(ValidationError):
    """Errore HTTP di Nexi con stato e codice gateway: serve a distinguere
    metodo disabilitato, configurazione sbagliata e guasto temporaneo."""

    def __init__(self, message, status_code=None, gateway_code=None):
        super().__init__(message)
        self.status_code = status_code
        self.gateway_code = gateway_code


# Parole che dicono "metodo non abilitato sul terminale" (non un guasto).
_NEXI_DISABLED_HINTS = (
    'not enabled', 'not active', 'disabled', 'not allowed', 'not available',
    'unknown paymentservice', 'invalid paymentservice',
)


def _classify_nexi_check_error(err):
    """'disabled' = metodo spento sul terminale; 'config' = chiavi/URL
    sbagliati; 'unknown' = guasto temporaneo o risposta mai vista.
    Solo 'disabled' cambia la visibilita in cassa: un timeout o un 500 non
    devono mai spegnere un metodo valido."""
    status = getattr(err, 'status_code', None)
    if status in (401, 403):
        return 'config'
    msg = str(err) or ''
    low = msg.lower()
    if any(k in low for k in (
        'manca url', 'manca la x-api-key', 'x-api-key', 'serve https',
        'dominio non nexi',
    )):
        return 'config'
    if 'paymentservice' in low and any(k in low for k in (
        'not', 'invalid', 'unknown', 'disabled', 'enable', 'support',
    )):
        return 'disabled'
    if any(k in low for k in _NEXI_DISABLED_HINTS):
        return 'disabled'
    return 'unknown'


def _nexi_first_error(resp):
    """(descrizione, codice) del primo errore Nexi, senza dati sensibili."""
    try:
        errs = (resp.json() or {}).get('errors') or []
        if errs and isinstance(errs[0], dict):
            return (
                str(errs[0].get('description') or ''),
                str(errs[0].get('code') or ''),
            )
    except (ValueError, AttributeError):
        pass
    return '', ''


def _nexi_error_detail(resp):
    """Prima descrizione utile da Nexi, senza dati sensibili."""
    desc, code = _nexi_first_error(resp)
    if desc:
        return '%s [%s]' % (desc[:160], code[:16]) if code else desc[:160]
    return 'errore. Riprova.'


class PaymentProviderNexi(models.Model):
    _inherit = 'payment.provider'

    code = fields.Selection(selection_add=[('nexi', 'Nexi XPay Web')], ondelete={'nexi': 'set default'})
    # Chiave API = password del terminale. La trovi in Nexi Business > Back Office > Admin > APIKey.
    nexi_api_key = fields.Char(
        string='X-Api-Key', help='Chiave del terminale XPay Web.',
        groups='base.group_system',
    )
    # Test o Produzione. Usa sempre Test prima con le chiavi finte.
    nexi_env = fields.Selection(
        [('test', 'Test'), ('prod', 'Produzione')],
        default='test', string='Ambiente',
    )
    # Come incassi: automatico (subito) o manuale (autorizzi ora, incassi dopo).
    nexi_capture = fields.Selection(
        [('implicit', 'Automatico'), ('explicit', 'Manuale')],
        default='implicit', string='Incasso',
    )
    # Come mostri la cassa: redirect pagina Nexi (HPP) o form dentro il sito (Build v3).
    nexi_mode = fields.Selection(
        [('hpp', 'Hosted Page (redirect)'), ('build', 'Embedded Build v3 (beta)')],
        default='hpp', string='Modalità',
        help='HPP = vai su pagina Nexi. Build = form nel tuo sito con iframe Nexi (serve URL pubblico, localhost non va).',
    )
    # Preselezione metodo su pagina Nexi. Vuoto = tutti. Valori da tabella Nexi (es. GOOGLEPAY).
    nexi_payment_service = fields.Char(
        string='Metodo preselezionato',
        help='Passato come paymentService a Nexi. Vuoto = tutti i metodi. Valori da tabella codifica PaymentService (es. GOOGLEPAY). Solo maiuscole, cifre, underscore.',
    )
    # Base pubblica del negozio (https://...). Usata per resultUrl e merchantUrl.
    # Se vuota, usa web.base.url. Non si usa mai l'header Host della richiesta.
    nexi_public_base = fields.Char(
        string='Base pubblica negozio',
        help='Es. https://negozio.example.com. Se vuota, usa web.base.url.',
        groups='base.group_system',
    )
    # OneClick CIT: salva carta con contratto e riusala dopo.
    nexi_oneclick = fields.Boolean(
        string='OneClick CIT',
        default=False,
        help='Se attivo: primo pagamento crea contratto CIT, i dopo riusano contractId via HPP.',
    )

    # --- URL base. Non li metto a mano: li prendi dalla scheda API su developer.nexi.it ---
    nexi_test_url = fields.Char(
        string='URL test Nexi',
        help='URL test completo dalla scheda API su developer.nexi.it (es. pagina Pagamenti).',
        groups='base.group_system',
    )
    nexi_prod_url = fields.Char(
        string='URL produzione Nexi',
        help='URL produzione completo dalla scheda API su developer.nexi.it.',
        groups='base.group_system',
    )
    # Esito ultima verifica metodi su Nexi. Solo lettura.
    nexi_methods_report = fields.Text(
        string='Metodi verificati', readonly=True,
        help='Ultimo controllo su Nexi: quali paymentService il terminale accetta.',
    )
    nexi_methods_checked = fields.Datetime(string='Verifica del', readonly=True)

    def button_nexi_check_methods(self):
        """Prova ogni metodo su Nexi e nasconde quelli rifiutati dal terminale.

        Nasconde un metodo solo se Nexi dice chiaramente "non abilitato".
        Errori di configurazione (chiave/URL) fermano la verifica con un
        messaggio; guasti temporanei (timeout, 5xx, risposte mai viste)
        lasciano tutto com'e: non si spegne un metodo valido per un guasto.
        """
        self.ensure_one()
        from .payment_transaction import METHOD_SERVICE
        service_by_method = {}
        for m in self.payment_method_ids.filtered(lambda x: x.is_primary):
            if m.code in METHOD_SERVICE:
                service_by_method[m] = METHOD_SERVICE[m.code]
        if not service_by_method:
            raise ValidationError(_('Nessun metodo Nexi da verificare.'))
        base = self._nexi_base_url()
        import time
        lines = []
        for method, service in sorted(service_by_method.items(), key=lambda x: x[0].name):
            ref = 'CHECK-%s-%d' % (service, int(time.time()) % 100000)
            amount = _nexi_check_amount_cents(service)
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
            except ValidationError as e:
                kind = _classify_nexi_check_error(e)
                if kind == 'config':
                    raise ValidationError(_(
                        'Verifica metodi: controlla X-Api-Key e URL Nexi (%s).'
                    ) % service)
                if kind == 'unknown':
                    lines.append('%s (%s): non verificato (%s)' % (
                        method.name, service, str(e)[:80]))
                    continue
                method._nexi_set_disabled(self, True)
                lines.append('%s (%s): rifiutato dal terminale' % (method.name, service))
                continue
            method._nexi_set_disabled(self, False)
            lines.append('%s (%s): attivo' % (method.name, service))
        self.nexi_methods_report = '\n'.join(lines)
        from odoo import fields as odoo_fields
        self.nexi_methods_checked = odoo_fields.Datetime.now()
        return True

    def _nexi_base_url(self):
        self.ensure_one()
        url = self.nexi_prod_url if self.nexi_env == 'prod' else self.nexi_test_url
        if not url:
            raise ValidationError(_(
                'Manca URL Nexi. Metti URL test/produzione dalla scheda API su developer.nexi.it.'
            ))
        url = url.strip().rstrip('/')
        parts = re.split(r'://', url, maxsplit=1)
        if len(parts) != 2 or parts[0].lower() != 'https':
            raise ValidationError(_('URL Nexi non valido: serve https.'))
        host = parts[1].split('/')[0].lower()
        if not any(host == h or host.endswith('.' + h) for h in NEXI_HOSTS):
            raise ValidationError(_('URL Nexi non valido: dominio non Nexi.'))
        return url

    def _nexi_build_url(self):
        """URL ordini Build v3: stesso host di HPP, path /orders/build."""
        from urllib.parse import urlparse
        base = self._nexi_base_url()
        if base.endswith('/orders/hpp'):
            return base[: -len('/orders/hpp')] + '/orders/build'
        parts = urlparse(base)
        host_base = f'{parts.scheme}://{parts.netloc}'
        prefix = parts.path
        if '/orders/' in prefix:
            prefix = prefix.split('/orders/')[0]
        return host_base + prefix + '/orders/build'

    def _get_supported_currencies(self):
        # Upstream chiede ensure_one + super() fuori da Nexi: senza, anche gli
        # altri provider perderebbero le loro valute.
        self.ensure_one()
        if self.code != 'nexi':
            return super()._get_supported_currencies()
        # Nexi sandbox vuole EUR. Tieni solo EUR.
        return self.env['res.currency'].search([('name', '=', 'EUR')])

    def _get_default_payment_method_codes(self):
        self.ensure_one()
        if self.code != 'nexi':
            return super()._get_default_payment_method_codes()
        return {'card', 'googlepay', 'applepay', 'bancomatpay', 'mybank', 'paypal', 'paypal_bnpl'}

    def _compute_feature_support_fields(self):
        super()._compute_feature_support_fields()
        # v1 semplice: niente rimborso, niente cattura manuale.
        # Token solo se OneClick acceso: mostra "salva carta" in cassa.
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
        """Chiama Nexi in REST JSON. Semplice, con timeout."""
        self.ensure_one()
        import uuid
        headers = {
            'Content-Type': 'application/json',
            'X-Api-Key': self.nexi_api_key or '',
            'Correlation-Id': str(uuid.uuid4()),
        }
        try:
            # Niente redirect: la chiave non deve mai viaggiare su un altro host.
            resp = requests.request(method, url, json=payload, params=params, headers=headers, timeout=(5, 15), allow_redirects=False)
            if 300 <= (resp.status_code or 0) < 400:
                _logger.warning('Nexi redirect inatteso %s -> %s', url, resp.headers.get('Location'))
                raise ValidationError(_('Nexi risposta non valida. Riprova.'))
            try:
                resp.raise_for_status()
            except requests.exceptions.HTTPError:
                _logger.error('Nexi %s %s -> %s', method, url, (resp.text or '')[:500])
                _desc, _code = _nexi_first_error(resp)
                raise NexiApiError(
                    _('Nexi: %s') % _nexi_error_detail(resp),
                    status_code=getattr(resp, 'status_code', None),
                    gateway_code=_code or None,
                )
        except requests.exceptions.RequestException:
            _logger.exception('Nexi non raggiungibile: %s', url)
            raise ValidationError(_('Nexi non raggiungibile. Riprova.'))
        try:
            return resp.json() if resp.text else {}
        except ValueError:
            raise ValidationError(_('Nexi risposta non valida. Riprova.'))

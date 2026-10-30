# -*- coding: utf-8 -*-
"""Provider Nexi XPay Web. Solo redirect (Hosted Page). Niente dati carta in Odoo."""
import logging
import uuid
from urllib.parse import urlparse

import requests

from odoo import _, fields, models
from odoo.exceptions import ValidationError

_logger = logging.getLogger(__name__)

# Domini Nexi ammessi. Solo questi, resto rifiutato.
# xpay.nexigroup.com = produzione, xpaysandbox.nexigroup.com = test.
NEXI_HOSTS = ('xpay.nexigroup.com', 'xpaysandbox.nexigroup.com', 'xpay.nexi.it')

# Importo di prova per "Verifica metodi". Alcuni servizi hanno minimi propri:
# PayPal Paga in 3 sotto i 30 € viene rifiutato da Nexi (PS0064/GW0043), quindi
# la verifica userebbe un errore di importo al posto della disponibilità vera.
CHECK_AMOUNTS = {'PAYPAL_PAGAIN3': '5000'}
CHECK_AMOUNT_DEFAULT = '100'

# Testi con cui Nexi dice "questo servizio non è attivo su questo terminale".
# Solo questi spengono un metodo: un timeout o una chiave sbagliata no.
REJECTION_MARKERS = (
    'not enabled', 'not_enabled', 'not allowed', 'not_allowed',
    'not configured', 'not_configured', 'not available', 'not_available',
    'service not', 'payment service not',
    'ps0064', 'gw0043',
)


class NexiUnavailable(ValidationError):
    """Nexi non ha risposto (rete, timeout, risposta illeggibile).

    Non dice niente sul metodo provato: chi la riceve non deve disabilitare
    niente, perché il problema è la connessione o la configurazione.
    """


def is_nexi_url(url):
    """True solo per https su un dominio Nexi.

    Unico posto dove si controlla il dominio: lo usano sia il provider sia il
    controller, così le due regole non possono divergere.
    """
    if not url:
        return False
    try:
        parts = urlparse(str(url).strip())
        host = (parts.hostname or '').lower()
    except ValueError:
        return False
    if (parts.scheme or '').lower() != 'https' or not host:
        return False
    return any(host == h or host.endswith('.' + h) for h in NEXI_HOSTS)


def _nexi_error_detail(resp):
    """Prima descrizione utile da Nexi, senza dati sensibili."""
    try:
        errs = (resp.json() or {}).get('errors') or []
        if errs and isinstance(errs[0], dict):
            desc = str(errs[0].get('description') or '')
            code = str(errs[0].get('code') or '')
            if desc:
                return '%s [%s]' % (desc[:160], code[:16]) if code else desc[:160]
    except (ValueError, AttributeError):
        pass
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
    # Esito per singolo terminale: due provider Nexi possono avere metodi diversi.
    nexi_method_availability_ids = fields.One2many(
        'payment.nexi.method.availability', 'provider_id',
        string='Metodi del terminale', readonly=True, groups='base.group_system',
    )

    def button_nexi_check_methods(self):
        """Prova ogni metodo su Nexi e salva l'esito per QUESTO terminale."""
        self.ensure_one()
        from .payment_transaction import METHOD_SERVICE
        if not self.nexi_api_key:
            raise ValidationError(_(
                'Manca la X-Api-Key: senza chiave non posso verificare i metodi.'
            ))
        service_by_method = {}
        for m in self.payment_method_ids.filtered(lambda x: x.is_primary):
            if m.code in METHOD_SERVICE:
                service_by_method[m] = METHOD_SERVICE[m.code]
        if not service_by_method:
            raise ValidationError(_('Nessun metodo Nexi da verificare.'))
        base = self._nexi_base_url()
        lines = []
        for method, service in sorted(service_by_method.items(), key=lambda x: x[0].name):
            enabled, detail = self._nexi_check_service(base, service)
            # L'esito è del terminale, non del metodo in generale.
            self._nexi_store_availability(method, enabled, detail)
            lines.append('%s (%s): %s' % (method.name, service, detail))
        self.nexi_methods_report = '\n'.join(lines)
        self.nexi_methods_checked = fields.Datetime.now()
        return True

    def _nexi_check_service(self, base, service):
        """Prova un paymentService su Nexi con un importo compatibile.

        :return: (True | False | None, descrizione). None = non verificato:
                 Nexi non ha risposto o la configurazione è rotta, quindi lo
                 stato del metodo resta quello di prima.
        """
        amount = CHECK_AMOUNTS.get(service, CHECK_AMOUNT_DEFAULT)
        ref = 'CHECK-%s-%s' % (service, uuid.uuid4().hex[:10])
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
            # Rete o timeout: non dice niente sul metodo.
            return None, 'non verificato (%s)' % str(e)[:80]
        except ValidationError as e:
            msg = str(e).lower()
            if any(marker in msg for marker in REJECTION_MARKERS):
                return False, 'rifiutato dal terminale'
            # Errore che non parla del metodo: non lo spengo.
            return None, 'errore (%s)' % str(e)[:80]
        return True, 'attivo'

    def _nexi_store_availability(self, method, enabled, detail):
        """Salva l'esito della verifica per questo terminale."""
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
                'Manca URL Nexi. Metti URL test/produzione dalla scheda API su developer.nexi.it.'
            ))
        url = url.strip().rstrip('/')
        # https + dominio Nexi: la chiave non deve mai uscire da lì.
        if not is_nexi_url(url):
            raise ValidationError(_('URL Nexi non valido: serve https su un dominio Nexi.'))
        return url

    def _nexi_build_url(self):
        """URL ordini Build v3: stesso host di HPP, path /orders/build."""
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
        # Nexi sandbox vuole EUR. Tieni solo EUR, ma solo per Nexi: gli altri
        # provider sullo stesso negozio non devono essere limitati.
        self.ensure_one()
        if self.code != 'nexi':
            return super()._get_supported_currencies()
        return self.env['res.currency'].with_context(active_test=False).search([('name', '=', 'EUR')])

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
        """Chiama Nexi in REST JSON. Semplice, con timeout.

        :raise NexiUnavailable: Nexi non ha risposto o la risposta è illeggibile.
        :raise ValidationError: Nexi ha risposto con un errore suo.
        """
        self.ensure_one()
        headers = {
            'Content-Type': 'application/json',
            'X-Api-Key': self.nexi_api_key or '',
            'Correlation-Id': str(uuid.uuid4()),
        }
        try:
            # Niente redirect: la chiave non deve mai viaggiare su un altro host.
            resp = requests.request(method, url, json=payload, params=params, headers=headers, timeout=(5, 15), allow_redirects=False)
        except requests.exceptions.RequestException:
            _logger.exception('Nexi non raggiungibile: %s', url)
            raise NexiUnavailable(_('Nexi non raggiungibile. Riprova.'))
        if 300 <= (resp.status_code or 0) < 400:
            _logger.warning('Nexi redirect inatteso %s -> %s', url, resp.headers.get('Location'))
            raise NexiUnavailable(_('Nexi risposta non valida. Riprova.'))
        try:
            resp.raise_for_status()
        except requests.exceptions.HTTPError:
            _logger.error('Nexi %s %s -> %s', method, url, (resp.text or '')[:500])
            raise ValidationError(_('Nexi: %s') % _nexi_error_detail(resp))
        try:
            return resp.json() if resp.text else {}
        except ValueError:
            raise NexiUnavailable(_('Nexi risposta non valida. Riprova.'))

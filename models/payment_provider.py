# -*- coding: utf-8 -*-
"""Provider Nexi XPay Web. Solo redirect (Hosted Page). Niente dati carta in Odoo."""
import logging
import re

import requests

from odoo import _, fields, models
from odoo.exceptions import ValidationError

_logger = logging.getLogger(__name__)

# Domini Nexi ammessi. Solo questi, resto rifiutato.
NEXI_HOSTS = ('xpaysandbox.nexigroup.com', 'xpay.nexi.it')


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
        # Nexi sandbox vuole EUR. Tieni solo EUR.
        return self.env['res.currency'].search([('name', '=', 'EUR')])

    def _get_default_payment_method_codes(self):
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

    def _nexi_make_request(self, method, url, payload=None):
        """Chiama Nexi in REST JSON. Semplice, con timeout."""
        self.ensure_one()
        import uuid
        headers = {
            'Content-Type': 'application/json',
            'X-Api-Key': self.nexi_api_key or '',
            'Correlation-Id': str(uuid.uuid4()),
        }
        try:
            resp = requests.request(method, url, json=payload, headers=headers, timeout=(5, 15))
            try:
                resp.raise_for_status()
            except requests.exceptions.HTTPError:
                _logger.exception('Nexi errore HTTP su %s', url)
                raise ValidationError(_('Nexi errore. Riprova.'))
        except requests.exceptions.RequestException:
            _logger.exception('Nexi non raggiungibile: %s', url)
            raise ValidationError(_('Nexi non raggiungibile. Riprova.'))
        try:
            return resp.json() if resp.text else {}
        except ValueError:
            raise ValidationError(_('Nexi risposta non valida. Riprova.'))

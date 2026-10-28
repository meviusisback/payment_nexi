# -*- coding: utf-8 -*-
"""Transazione Nexi XPay Web. Un solo flusso: crea ordine -> redirect -> verifica."""
import logging
import re
from decimal import Decimal, InvalidOperation

from odoo import _, api, fields, models
from odoo.exceptions import ValidationError

_logger = logging.getLogger(__name__)

# Esiti Nexi. EXECUTED = pagato (automatico). AUTHORIZED = solo bloccato.
OK_RESULTS = {'EXECUTED'}
AUTH_RESULTS = {'AUTHORIZED'}
KO_RESULTS = {'CANCELED', 'THREEDS_FAILED', 'DENIED', 'ERROR'}

# Metodo Odoo -> paymentService Nexi. Vuoto = tutti. Valori da tabella Nexi.
METHOD_SERVICE = {
    'card': 'CARDS',
    'googlepay': 'GOOGLEPAY',
    'applepay': 'APPLEPAY',
    'bancomatpay': 'BANCOMATPAY',
    'mybank': 'MYBANK',
    'paypal': 'PAYPAL',
    'paypal_bnpl': 'PAYPAL_BNPL',
}

REF_RE = re.compile(r'^[A-Za-z0-9_-]{4,64}$')


def to_cents(amount):
    """Euro -> centesimi interi. Esempio: 12.34 -> 1234."""
    try:
        return int((Decimal(str(amount)) * 100).to_integral_value())
    except (InvalidOperation, ValueError, TypeError):
        return 0


def is_paid(operation_result):
    return operation_result in OK_RESULTS


def _parse_amount(value):
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return Decimal('0')


class PaymentTransactionNexi(models.Model):
    _inherit = 'payment.transaction'

    nexi_order_id = fields.Char(string='ID ordine Nexi', readonly=True)
    nexi_contract_id = fields.Char(
        string='Contratto OneClick', readonly=True,
        help='contractId CIT creato al primo pagamento, riusato dopo.',
    )
    nexi_masked_pan = fields.Char(
        string='Carta salvata', readonly=True,
        help='Numero mascherato da Nexi (es. ****7289). Mai la carta vera.',
    )

    def _nexi_family_ids(self, partner_id):
        """Partner + contatti collegati: il contratto vale per tutta la famiglia."""
        partner = self.env['res.partner'].browse(partner_id)
        if not partner.exists():
            return []
        commercial = partner.commercial_partner_id.id or partner.id
        return self.env['res.partner'].search(
            [('id', 'child_of', commercial)]
        ).ids or [partner_id]

    def _nexi_contract_for_partner(self, partner_id):
        """Ultimo contratto CIT pagato di questo partner, se esiste."""
        family = self._nexi_family_ids(partner_id)
        if not family:
            return False
        tx = self.search([
            ('provider_code', '=', 'nexi'),
            ('partner_id', 'in', family),
            ('nexi_contract_id', '!=', False),
            ('state', 'in', ['done', 'authorized', 'pending']),
        ], order='id desc', limit=1)
        return tx.nexi_contract_id or False

    @api.model
    def get_nexi_saved_cards(self, partner_id):
        """Carte salvate del cliente (solo numeri mascherati). Per la cassa."""
        family = self._nexi_family_ids(partner_id)
        if not family:
            return []
        txs = self.search([
            ('provider_code', '=', 'nexi'),
            ('partner_id', 'in', family),
            ('nexi_contract_id', '!=', False),
            ('state', 'in', ['done', 'authorized', 'pending']),
        ], order='id desc')
        seen = set()
        cards = []
        for tx in txs:
            if tx.nexi_contract_id in seen:
                continue
            seen.add(tx.nexi_contract_id)
            cards.append({
                'masked': tx.nexi_masked_pan or 'Carta salvata',
                'contract_id': tx.nexi_contract_id,
            })
        return cards

    @api.model
    def get_last_used_method_id(self, partner_id, method_ids):
        """Ultimo metodo usato dal cliente, se ancora disponibile. Per preselezione."""
        family = self._nexi_family_ids(partner_id)
        if not family or not method_ids:
            return False
        tx = self.search([
            ('partner_id', 'in', family),
            ('payment_method_id', 'in', list(method_ids)),
            ('state', 'in', ['done', 'authorized', 'pending', 'draft']),
        ], order='id desc', limit=1)
        return tx.payment_method_id.id if tx else False

    def _get_specific_create_values(self, provider_code, values):
        vals = super()._get_specific_create_values(provider_code, values)
        if provider_code == 'nexi':
            vals['nexi_order_id'] = values.get('reference')
        return vals

    def _get_specific_rendering_values(self, processing_values):
        """Valori per il form redirect. Qui creiamo l'ordine su Nexi."""
        res = super()._get_specific_rendering_values(processing_values)
        if self.provider_code != 'nexi':
            return res
        self.ensure_one()
        if self.currency_id.name != 'EUR':
            raise ValidationError(_('Nexi attiva solo in EUR.'))
        provider = self.provider_id
        if not provider.nexi_api_key:
            raise ValidationError(_('Manca la X-Api-Key Nexi nel provider.'))
        base = self.get_base_url()
        session = {
            'actionType': 'VERIFY' if (provider.nexi_oneclick and not self.amount) else 'PAY',
            'amount': str(to_cents(self.amount)),
            'language': 'ITA',
            'resultUrl': f'{base}/payment/nexi/confirm?ref={self.reference}',
            'cancelUrl': f'{base}/payment/nexi/confirm?ref={self.reference}&cancel=1',
            'captureType': 'EXPLICIT' if provider.nexi_capture == 'explicit' else 'IMPLICIT',
        }
        # Preselezione metodo: campo provider vince, se vuoto usa metodo scelto.
        service = (provider.nexi_payment_service or '').strip().upper()
        if not service:
            service = METHOD_SERVICE.get(self.payment_method_code or '', False) or False
        if service:
            if not re.match(r'^[A-Z0-9_]{2,30}$', service):
                raise ValidationError(_('Metodo Nexi non valido.'))
            session['paymentService'] = service
        # OneClick CIT: solo carte e solo se il cliente spunta "salva carta".
        # I contratti CIT nascono dalla carta: riusarli con APM dà errore Nexi.
        # Primo pagamento con spunta = CONTRACT_CREATION.
        # Dopo riusa contractId con SUBSEQUENT_PAYMENT via HPP.
        contract = False
        is_card = (self.payment_method_code or '') == 'card'
        want_save = bool(processing_values.get('should_tokenize')) if provider.nexi_oneclick else False
        if provider.nexi_oneclick and is_card:
            contract = self.nexi_contract_id or self._nexi_contract_for_partner(self.partner_id.id)
            if contract:
                session['recurrence'] = {
                    'action': 'SUBSEQUENT_PAYMENT',
                    'contractId': contract,
                    'contractType': 'CIT',
                }
            elif want_save:
                contract = 'N%06d%011d' % (self.partner_id.id % 1000000, self.id % 100000000000)
                session['recurrence'] = {
                    'action': 'CONTRACT_CREATION',
                    'contractId': contract,
                    'contractType': 'CIT',
                }
        payload = {
            'order': {
                'orderId': self.reference,
                'amount': str(to_cents(self.amount)),
                'currency': self.currency_id.name or 'EUR',
                'description': (self.reference or '')[:100],
                'customerId': 'C%010d' % (self.partner_id.id % 10000000000),
                'customerInfo': {
                    'cardHolderName': (self.partner_id.name or '')[:255],
                    'cardHolderEmail': (self.partner_id.email or '')[:255],
                },
            },
            'paymentSession': session,
        }
        if contract and not self.nexi_contract_id:
            self.nexi_contract_id = contract
        # Modalità Build v3: ordine embed invece di HPP.
        if provider.nexi_mode == 'build':
            return self._nexi_render_build_values(res, provider, payload)
        url = provider._nexi_base_url()
        try:
            data = provider._nexi_make_request('POST', url, payload=payload)
        except ValidationError as e:
            self._set_error(str(e))
            raise

        hosted = (data or {}).get('hostedPage') or (data or {}).get('hostedpage')
        token = (data or {}).get('securityToken') or (data or {}).get('securitytoken')
        if not hosted:
            self._set_error('Nexi non ha dato la pagina di pagamento.')
            raise ValidationError(_('Nexi errore. Riprova.'))
        # Nexi dà hostedPage + securityToken separati: vanno uniti.
        if token and 'securityToken' not in hosted:
            sep = '&' if '?' in hosted else '?'
            hosted = f'{hosted}{sep}securityToken={token}'
        _logger.info('Nexi ordine %s creato.', self.reference)
        # Dividi base e parametri: il form GET li rimette da solo.
        from urllib.parse import urlparse, parse_qsl
        parts = urlparse(hosted)
        if parts.scheme.lower() != 'https':
            raise ValidationError(_('Nexi errore. Riprova.'))
        from .payment_provider import NEXI_HOSTS
        host = (parts.netloc or '').lower()
        if not any(host == h or host.endswith('.' + h) for h in NEXI_HOSTS):
            raise ValidationError(_('Nexi errore. Riprova.'))
        base = f'{parts.scheme}://{parts.netloc}{parts.path}'
        params = dict(parse_qsl(parts.query))
        res.update(redirect_url=hosted, api_url=base, url_params=params)
        return res

    def _nexi_render_build_values(self, res, provider, payload):
        """Ordine Build v3 embed: crea ordine e rimanda a template iframe."""
        self.ensure_one()
        base = self.get_base_url()
        if 'localhost' in (base or '') or '127.0.0.1' in (base or ''):
            raise ValidationError(_('Build vuole URL pubblico, localhost non va. Usa HPP per i test locali.'))
        build_url = provider._nexi_build_url()
        build_payload = dict(payload)
        build_payload['version'] = '3'
        build_payload['merchantUrl'] = base
        try:
            data = provider._nexi_make_request('POST', build_url, payload=build_payload)
        except ValidationError as e:
            self._set_error(str(e))
            raise
        res.update(
            nexi_build=True,
            build_data=data,
            redirect_url=f'/payment/nexi/build/{self.reference}',
        )
        return res

    def _get_tx_from_notification_data(self, provider_code, notification_data):
        tx = super()._get_tx_from_notification_data(provider_code, notification_data)
        if provider_code != 'nexi' or tx:
            return tx
        ref = (notification_data or {}).get('ref') or (notification_data or {}).get('orderId')
        if not ref or not REF_RE.match(str(ref)):
            raise ValidationError(_('Ritorno Nexi non valido.'))
        tx = self.search([('reference', '=', ref), ('provider_code', '=', 'nexi')])
        if not tx:
            raise ValidationError(_('Ritorno Nexi non valido.'))
        return tx

    def _process_notification_data(self, notification_data):
        super()._process_notification_data(notification_data)
        if self.provider_code != 'nexi':
            return
        # Non fidarti di cancel in URL: verifica sempre su Nexi.
        provider = self.provider_id
        create_url = provider._nexi_base_url()
        if '/orders/hpp' in create_url:
            verify_url = create_url.replace('/orders/hpp', f'/orders/{self.reference}')
        else:
            from urllib.parse import quote
            verify_url = create_url.rstrip('/') + f'/orders/{quote(self.reference, safe="")}'
        try:
            data = provider._nexi_make_request('GET', verify_url)
        except ValidationError as e:
            self._set_error(str(e))
            return
        result = ''
        for op in (data or {}).get('operations', []):
            if op.get('operationType') == 'AUTHORIZATION':
                result = op.get('operationResult') or ''
                break
        if is_paid(result):
            # Controlla importo e valuta prima di chiudere.
            expected = _parse_amount(self.amount)
            got = Decimal('0')
            cur_ok = True
            masked = False
            for op in (data or {}).get('operations', []):
                if op.get('operationType') == 'AUTHORIZATION':
                    got = _parse_amount(op.get('operationAmount', '0'))
                    if op.get('operationCurrency') and op.get('operationCurrency') != self.currency_id.name:
                        cur_ok = False
                    masked = (
                        op.get('paymentInstrumentInfo')
                        or op.get('maskedPan')
                        or op.get('maskedPanAlias')
                        or False
                    )
                    break
            status = (data or {}).get('orderStatus', {}) or {}
            if not cur_ok or got < expected:
                self._set_error('Nexi importo non valido.')
                return
            cap = _parse_amount(status.get('capturedAmount', '0'))
            if cap < expected:
                self._set_error('Nexi incasso non completo.')
                return
            if masked:
                self.nexi_masked_pan = str(masked)[:32]
            self._set_done(state_message=f'Nexi OK: {result}')
        elif result in AUTH_RESULTS:
            for op in (data or {}).get('operations', []):
                if op.get('operationType') == 'AUTHORIZATION':
                    masked_auth = (
                        op.get('paymentInstrumentInfo')
                        or op.get('maskedPan')
                        or False
                    )
                    if masked_auth:
                        self.nexi_masked_pan = str(masked_auth)[:32]
                    break
            self._set_authorized(state_message=f'Nexi autorizzato: {result}')
        elif result in KO_RESULTS:
            self._set_canceled(state_message=f'Nexi KO: {result}')
        else:
            self._set_pending(state_message=f'Nexi in attesa: {result or "sconosciuto"}')

    def _send_refund_request(self, amount_to_refund=None, create_refund_transaction=True):
        self.ensure_one()
        if self.provider_code != 'nexi':
            return super()._send_refund_request(
                amount_to_refund=amount_to_refund,
                create_refund_transaction=create_refund_transaction,
            )
        raise ValidationError(_(
            'Rimborso Nexi non attivo: manca URL back office da developer.nexi.it.'
        ))

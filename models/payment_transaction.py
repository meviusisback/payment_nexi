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
    'paypal_bnpl': 'PAYPAL_PAGAIN3',  # codice vero Nexi; PAYPAL_BNPL viene ignorato
}

def _nexi_short(masked):
    """Mostra solo ultime 4 cifre: **** 7289. Mai la carta vera."""
    digits = re.sub(r'\D', '', str(masked or ''))
    if len(digits) >= 4:
        return '**** ' + digits[-4:]
    return str(masked or 'Carta salvata')[:32]


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
    nexi_build_session = fields.Char(string='Sessione Build', readonly=True)
    nexi_build_token = fields.Char(string='Token Build', readonly=True)

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
                'masked': _nexi_short(tx.nexi_masked_pan),
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

    def _nexi_post_order(self, provider, url, payload, want_save=False):
        """Crea ordine su Nexi. Se il contratto è morto, lo dimentica e riprova."""
        try:
            return provider._nexi_make_request('POST', url, payload=payload)
        except ValidationError as e:
            session = (payload or {}).get('paymentSession') or {}
            rec = session.get('recurrence') or {}
            if 'contract' not in str(e).lower() or rec.get('action') != 'SUBSEQUENT_PAYMENT':
                raise
            dead = rec.get('contractId')
            _logger.warning('Nexi contratto morto %s, riprovo senza.', dead)
            if dead:
                self.search([
                    ('provider_code', '=', 'nexi'),
                    ('partner_id', 'in', self._nexi_family_ids(self.partner_id.id)),
                    ('nexi_contract_id', '=', dead),
                ]).write({'nexi_contract_id': False})
                if self.nexi_contract_id == dead:
                    self.nexi_contract_id = False
            session = dict(session)
            session.pop('recurrence', None)
            if want_save:
                contract = 'N%06d%011d' % (
                    self.partner_id.id % 1000000, self.id % 100000000000)
                session['recurrence'] = {
                    'action': 'CONTRACT_CREATION',
                    'contractId': contract,
                    'contractType': 'CIT',
                }
                self.nexi_contract_id = contract
            payload = dict(payload, paymentSession=session)
            try:
                return provider._nexi_make_request('POST', url, payload=payload)
            except ValidationError as e2:
                self._set_error(str(e2))
                raise

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
        # Paga in 3 ha un minimo di 30 € lato Nexi: dico il motivo prima di
        # chiamare l'API, invece di far uscire un errore tecnico del gateway.
        if (
            (self.payment_method_code or '') == 'paypal_bnpl'
            and self.amount
            and self.amount < 30
        ):
            raise ValidationError(
                _('PayPal Paga in 3 richiede almeno 30,00 € di ordine.')
            )
        base = processing_values.get('base_url') or self.get_base_url()
        suffix = processing_values.get('order_suffix') or ''
        session = {
            'actionType': 'VERIFY' if (provider.nexi_oneclick and not self.amount) else 'PAY',
            'amount': str(to_cents(self.amount)),
            'language': 'ITA',
            'resultUrl': f'{base}/payment/nexi/confirm?ref={self.reference}{suffix}',
            'cancelUrl': f'{base}/payment/nexi/confirm?ref={self.reference}{suffix}&cancel=1',
            'notificationUrl': f'{base}/payment/nexi/confirm?ref={self.reference}{suffix}&notify=1',
            'captureType': 'EXPLICIT' if provider.nexi_capture == 'explicit' else 'IMPLICIT',
        }
        # Preselezione metodo: campo provider vince, se vuoto usa metodo scelto.
        # no_service (prepare condiviso): niente filtro, Nexi ridà tutti i bottoni.
        if processing_values.get('no_service'):
            service = False
        else:
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
        # no_service (bottoni condivisi): niente recurrence, solo bottoni.
        contract = False
        is_card = (self.payment_method_code or '') == 'card'
        want_save = bool(processing_values.get('should_tokenize')) if provider.nexi_oneclick else False
        # In Build il cliente deve poter inserire una carta nuova: se riusassimo
        # un contratto salvato, Nexi mostrerebbe la carta mascherata e i campi
        # non sarebbero compilabili (errore in fase di compilazione).
        is_build = provider.nexi_mode == 'build'
        if provider.nexi_oneclick and is_card and not processing_values.get('no_service'):
            contract = False if is_build else (
                self.nexi_contract_id or self._nexi_contract_for_partner(self.partner_id.id)
            )
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
                'orderId': self.reference + (processing_values.get('order_suffix') or ''),
                'amount': str(to_cents(self.amount)),
                'currency': self.currency_id.name or 'EUR',
                'description': (self.reference or '')[:100],
                # In Build il customerId fa riapparire la carta salvata lato
                # Nexi (campi bloccati e scadenza vecchia): lo mando solo se
                # il cliente vuole davvero salvare la carta.
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
        # Modalità Build v3: carta embed e APM via Build; resto HPP.
        if provider.nexi_mode == 'build' and (
            (self.payment_method_code or '') == 'card' or processing_values.get('force_build')
        ):
            return self._nexi_render_build_values(res, provider, payload, want_save, processing_values)
        url = provider._nexi_base_url()
        try:
            data = self._nexi_post_order(provider, url, payload, want_save)
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

    def _nexi_render_build_values(self, res, provider, payload, want_save=False, processing_values=None):
        """Ordine Build v3 embed: crea ordine e rimanda a template iframe."""
        self.ensure_one()
        processing_values = processing_values or {}
        base = processing_values.get('base_url') or self.get_base_url()
        if 'localhost' in (base or '') or '127.0.0.1' in (base or ''):
            raise ValidationError(_('Build vuole URL pubblico, localhost non va. Usa HPP per i test locali.'))
        build_url = provider._nexi_build_url()
        build_payload = dict(payload)
        build_payload['version'] = '3'
        from urllib.parse import urlparse
        parts = urlparse(base)
        build_payload['merchantUrl'] = f'{parts.scheme}://{parts.netloc}'
        # La carta va con CARDS: Nexi ridà subito i 5 campi, niente bottoni.
        try:
            data = self._nexi_post_order(provider, build_url, build_payload, want_save)
        except ValidationError as e:
            self._set_error(str(e))
            raise
        # L'ordine dei soli bottoni (-m) non deve sovrascrivere la sessione
        # dei campi carta: finalize deve usare quella giusta.
        if not processing_values.get('no_service'):
            self.nexi_build_session = (data or {}).get('sessionId') or False
            self.nexi_build_token = (data or {}).get('securityToken') or False
            _logger.debug('Build carta %s -> sessione %s', self.reference, self.nexi_build_session)
        host = 'https://xpaysandbox.nexigroup.com' if provider.nexi_env == 'test' else 'https://xpay.nexigroup.com'
        res.update(
            nexi_build=True,
            build_data=data,
            build_fields=(data or {}).get('fields') or [],
            build_url=(data or {}).get('url') or False,
            build_state=(data or {}).get('state') or False,
            nexi_sdk=host + '/monetaweb/resources/hfsdk.js',
            build_finalize_url=f'/payment/nexi/build/{self.reference}/finalize',
            confirm_url=f'/payment/nexi/confirm?ref={self.reference}',
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
        full_ref = str(ref)
        if full_ref.endswith('-m'):
            notification_data['nexi_order_ref'] = full_ref
            ref = full_ref[:-2]
        tx = self.search([('reference', '=', ref), ('provider_code', '=', 'nexi')])
        if not tx:
            raise ValidationError(_('Ritorno Nexi non valido.'))
        return tx

    @api.model
    def _nexi_last_auth(self, data):
        """L'ordine puo' avere piu' tentativi: conta solo l'ultimo."""
        ops = [o for o in ((data or {}).get('operations') or [])
               if o.get('operationType') == 'AUTHORIZATION']
        if not ops:
            return {}
        return max(ops, key=lambda o: str(o.get('operationTime') or ''))

    def _process_notification_data(self, notification_data):
        super()._process_notification_data(notification_data)
        if self.provider_code != 'nexi':
            return
        # Non fidarti di cancel in URL: verifica sempre su Nexi.
        provider = self.provider_id
        create_url = provider._nexi_base_url()
        order_ref = (notification_data or {}).get('nexi_order_ref') or self.reference
        if '/orders/hpp' in create_url:
            verify_url = create_url.replace('/orders/hpp', f'/orders/{order_ref}')
        else:
            from urllib.parse import quote
            verify_url = create_url.rstrip('/') + f'/orders/{quote(order_ref, safe="")}'
        import time as _time
        data = {}
        result = ''
        for attempt in range(6):
            try:
                data = provider._nexi_make_request('GET', verify_url)
            except ValidationError as e:
                self._set_error(str(e))
                return
            auth_op = self._nexi_last_auth(data)
            result = (auth_op or {}).get('operationResult') or ''
            # Il circuito a volte risponde PENDING per qualche secondo:
            # riprovo invece di dichiarare subito "in attesa".
            if result and result.upper() not in ('PENDING', 'AUTH-REQUEST', 'AUTHORIZED_PENDING'):
                break
            if attempt < 5:
                _time.sleep(1.5)
        if is_paid(result):
            # Controlla importo e valuta prima di chiudere.
            expected = _parse_amount(self.amount)
            got = Decimal('0')
            cur_ok = True
            masked = False
            op = auth_op or {}
            got = _parse_amount(op.get('operationAmount', '0'))
            if op.get('operationCurrency') and op.get('operationCurrency') != self.currency_id.name:
                cur_ok = False
            masked = (
                op.get('paymentInstrumentInfo')
                or op.get('maskedPan')
                or op.get('maskedPanAlias')
                or False
            )
            status = (data or {}).get('orderStatus', {}) or {}
            if not cur_ok or got < expected:
                self._set_error('Nexi importo non valido.')
                return
            cap = _parse_amount(status.get('capturedAmount', '0'))
            if cap < expected:
                self._set_error('Nexi incasso non completo.')
                return
            if masked:
                self.nexi_masked_pan = _nexi_short(masked)
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
                        self.nexi_masked_pan = _nexi_short(masked_auth)
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

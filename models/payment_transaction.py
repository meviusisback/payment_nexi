# -*- coding: utf-8 -*-
"""Transazione Nexi XPay Web. Un solo flusso: crea ordine -> redirect -> verifica."""
import logging
import re
from datetime import datetime
from decimal import Decimal, InvalidOperation

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, ValidationError

from .payment_provider import is_nexi_url

_logger = logging.getLogger(__name__)

# Esiti Nexi. EXECUTED = pagato (automatico). AUTHORIZED = solo bloccato.
OK_RESULTS = {'EXECUTED'}
AUTH_RESULTS = {'AUTHORIZED'}
# THREEDS_VALIDATED = 3DS ok ma pagamento ancora in corso: si aspetta.
WAIT_RESULTS = {'PENDING', 'AUTH-REQUEST', 'AUTHORIZED_PENDING', 'THREEDS_VALIDATED'}
# VOIDED = storno: niente incasso. REFUNDED = reso: niente incasso (v1 senza rimborsi).
KO_RESULTS = {'CANCELED', 'THREEDS_FAILED', 'DENIED', 'DENIED_BY_RISK', 'ERROR',
              'DECLINED', 'FAILED', 'VOIDED', 'REFUNDED'}

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


def _parse_cents(value):
    """Centesimi interi da risposta Nexi. O numero intero, o niente."""
    try:
        d = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    if d != d.to_integral_value():
        return None
    return int(d)


def _nexi_safe_fields(fields):
    """Tiene solo i campi carta con src https su un dominio Nexi.

    Gli iframe dei campi carta sono il punto in cui il cliente digita PAN e CVV:
    un src che arriva da una risposta alterata (o da un campo nuovo del gateway)
    non deve poter diventare un iframe di terzi dentro il checkout.
    """
    out = []
    for f in (fields or []):
        if not isinstance(f, dict):
            continue
        if not is_nexi_url(f.get('src')):
            _logger.warning('Nexi: campo carta scartato (src non Nexi): %s', str(f.get('id'))[:40])
            continue
        out.append(f)
    return out


def _nexi_new_contract_id():
    """contractId imprevedibile: N + 16 cifre esadecimali."""
    import secrets as _secrets
    return 'N' + _secrets.token_hex(8).upper()


# Operazioni Nexi che decidono com'è andata. Con l'incasso automatico Nexi
# restituisce AUTHORIZATION *e* CAPTURE: guardare solo l'autorizzazione lascia
# il pagamento appeso anche quando i soldi sono già stati presi.
AUTH_OPS = {'AUTHORIZATION'}
CAPTURE_OPS = {'CAPTURE'}

# Il cliente che sceglie una carta in cassa lascia la scelta qui.
_NEXI_SESSION_KEY = 'nexi_saved_contract'


def _nexi_op_time(op):
    """operationTime di Nexi come datetime, o None se manca/illeggibile."""
    raw = str((op or {}).get('operationTime') or '').strip()
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace('Z', '+00:00'))
    except ValueError:
        return None


def _nexi_op_ts(op):
    """operationTime come numero (per ordinare), o None."""
    dt = _nexi_op_time(op)
    if dt is None:
        return None
    try:
        return dt.timestamp()
    except (OverflowError, OSError, ValueError):
        return None


def _nexi_latest_operation(data, types):
    """Ultima operazione del tipo indicato, per data, nella risposta Nexi."""
    ops = [
        o for o in ((data or {}).get('operations') or [])
        if isinstance(o, dict) and (o.get('operationType') or '').upper() in types
    ]
    if not ops:
        return {}
    # Ordino sulla data interpretata, non sulla stringa: 'T9:00' verrebbe dopo
    # 'T10:00' a confronto lessicografico.
    return max(ops, key=lambda o: (
        _nexi_op_ts(o) is not None,
        _nexi_op_ts(o) or 0.0,
        str(o.get('operationTime') or ''),
    ))


def _nexi_decide_operation(data):
    """L'operazione che decide l'esito: l'ultima fra autorizzazione e incasso.

    :return: (operazione, 'auth' | 'capture'). Operazione vuota se Nexi non
             ne ha ancora restituita nessuna.
    """
    auth = _nexi_latest_operation(data, AUTH_OPS)
    capture = _nexi_latest_operation(data, CAPTURE_OPS)
    if not auth:
        return capture, 'capture'
    if not capture:
        return auth, 'auth'
    auth_ts, capture_ts = _nexi_op_ts(auth), _nexi_op_ts(capture)
    if auth_ts is None or capture_ts is None:
        # Data incerta: decide l'autorizzazione, non un incasso di cui non
        # posso dimostrare che sia più recente.
        return auth, 'auth'
    if capture_ts >= auth_ts:
        return capture, 'capture'
    return auth, 'auth'


def _nexi_session_choice():
    """Scelta carta salvata fatta in cassa, se questa richiesta ha una sessione.

    :return: dict {'order_id': int, 'contract_id': str} oppure None.
    """
    try:
        from odoo.http import request
        choice = request.session.get(_NEXI_SESSION_KEY)
    except Exception:  # niente richiesta HTTP (cron, RPC, test)
        return None
    return choice if isinstance(choice, dict) else None


class PaymentTransactionNexi(models.Model):
    _inherit = 'payment.transaction'

    nexi_order_id = fields.Char(string='ID ordine Nexi', readonly=True)
    nexi_contract_id = fields.Char(
        string='Contratto OneClick', readonly=True, groups='base.group_system',
        help='contractId CIT creato al primo pagamento, riusato dopo.',
    )
    nexi_masked_pan = fields.Char(
        string='Carta salvata', readonly=True,
        help='Numero mascherato da Nexi (es. ****7289). Mai la carta vera.',
    )
    nexi_build_session = fields.Char(
        string='Sessione Build', readonly=True, groups='base.group_system')
    nexi_build_token = fields.Char(
        string='Token Build', readonly=True, groups='base.group_system')
    # Codice casuale per transazione: chi non ce l'ha non tocca il pagamento.
    nexi_access_token = fields.Char(
        string='Codice accesso Nexi', readonly=True, groups='base.group_system')

    def _nexi_family_ids(self, partner_id):
        """Partner + contatti collegati: il contratto vale per tutta la famiglia."""
        partner = self.env['res.partner'].browse(partner_id)
        if not partner.exists():
            return []
        commercial = partner.commercial_partner_id.id or partner.id
        return self.env['res.partner'].search(
            [('id', 'child_of', commercial)]
        ).ids or [partner_id]

    def _nexi_contract_domain(self, partner_ids, provider_id=None):
        """Dominio per le carte salvate.

        Il contratto vale per un terminale: un contractId creato su un provider
        non esiste sugli altri, e riusarlo farebbe fallire il pagamento.
        """
        domain = [
            ('provider_code', '=', 'nexi'),
            ('partner_id', 'in', list(partner_ids or [])),
            ('nexi_contract_id', '!=', False),
        ]
        if provider_id:
            domain.append(('provider_id', '=', provider_id))
        return domain

    def _nexi_contract_for_partner(self, partner_id, provider_id=None):
        """Ultimo contratto CIT pagato di questo partner, se esiste."""
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
        """Carta che il cliente ha scelto in cassa, se l'ha scelta.

        La scelta arriva dalla sessione (vedi /payment/nexi/saved-card) perché
        il form di Odoo non porta campi extra fino al provider.

        :return: None  = nessuna scelta, decide l'automatico
                 False = il cliente vuole una carta nuova
                 str   = il contractId scelto
        """
        choice = _nexi_session_choice()
        if not choice:
            return None
        # Scelta fatta per un altro ordine: non vale per questo pagamento.
        if choice.get('order_id') not in self.sale_order_ids.ids:
            return None
        contract = str(choice.get('contract_id') or '').strip()
        if not contract:
            return False
        cards = {
            c['contract_id']
            for c in self.get_nexi_saved_cards(self.partner_id.id, provider.id)
        }
        # Contratto che non è del cliente o non è di questo terminale: ignoro.
        return contract if contract in cards else None

    def _nexi_can_see_partner(self, partner_id):
        """Vedo solo le mie carte, a meno che sia il sistema."""
        if self.env.su or self.env.user.has_group('base.group_system'):
            return True
        try:
            mine = set(self._nexi_family_ids(self.env.user.partner_id.id))
        except Exception:
            return False
        return partner_id in mine

    @api.model
    def get_nexi_saved_cards(self, partner_id, provider_id=None):
        """Carte salvate del cliente (solo numeri mascherati). Per la cassa."""
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
    def get_last_used_method_id(self, partner_id, method_ids):
        """Ultimo metodo usato dal cliente, se ancora disponibile. Per preselezione."""
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
                # Il cliente non sceglie token ne' contratti: li fa solo il sistema.
                for f in ('nexi_contract_id', 'nexi_build_session', 'nexi_build_token',
                          'nexi_masked_pan', 'nexi_order_id', 'nexi_access_token'):
                    vals.pop(f, None)
            if not vals.get('nexi_access_token'):
                import secrets as _secrets
                vals['nexi_access_token'] = _secrets.token_urlsafe(32)
        return super().create(vals_list)

    def write(self, vals):
        if not self.env.su and not self.env.user.has_group('base.group_system'):
            bad = [f for f in ('nexi_contract_id', 'nexi_build_session', 'nexi_build_token',
                               'nexi_masked_pan', 'nexi_order_id', 'nexi_access_token')
                   if f in (vals or {})]
            if bad:
                raise AccessError(_('Solo il sistema puo scrivere i dati Nexi.'))
        return super().write(vals)

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
            msg = str(e)
            dead_code = 'PS0064' in msg or 'Recurring contract does not exist' in msg
            if not dead_code or rec.get('action') != 'SUBSEQUENT_PAYMENT':
                raise
            dead = rec.get('contractId')
            _logger.warning('Nexi contratto morto %s, riprovo senza.', dead)
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
                return provider._nexi_make_request('POST', url, payload=payload)
            except ValidationError as e2:
                # Il dettaglio di Nexi resta nei log: in state_message va solo
                # un messaggio generico (lo legge anche il cliente).
                _logger.warning('Nexi ordine %s KO: %s', self.reference, str(e2)[:200])
                self._set_error(_('Nexi errore. Riprova.'))
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
        # Base pubblica: campo dedicato, poi web.base.url. L'Host della
        # richiesta vale solo come ultima spiaggia (tunnel di prova): in
        # produzione imposta sempre la base esplicita.
        self = self.sudo()
        base = (provider.nexi_public_base or '').strip().rstrip('/')
        if not base:
            from urllib.parse import urlparse as _up
            wbu = (self.get_base_url() or '').rstrip('/')
            try:
                _whost = (_up(wbu).hostname or '').lower()
                _wok = _up(wbu).scheme == 'https' and _whost not in ('', 'localhost', '127.0.0.1')
            except Exception:
                _wok = False
            if _wok:
                base = wbu
        if not base:
            from urllib.parse import urlparse as _up2
            cand = (processing_values.get('base_url') or '').rstrip('/')
            try:
                _chost = (_up2(cand).hostname or '').lower()
                _cok = _up2(cand).scheme == 'https' and _chost not in ('', 'localhost', '127.0.0.1')
            except Exception:
                _cok = False
            if _cok:
                _logger.warning('Nexi usa Host della richiesta: imposta Base pubblica negozio.')
                base = cand
        if not base:
            base = self.get_base_url()
        suffix = processing_values.get('order_suffix') or ''
        if not self.nexi_access_token:
            import secrets as _secrets
            self.sudo().nexi_access_token = _secrets.token_urlsafe(32)
        tok = self.nexi_access_token
        session = {
            'actionType': 'VERIFY' if (provider.nexi_oneclick and not self.amount) else 'PAY',
            'amount': str(to_cents(self.amount)),
            'language': 'ITA',
            'resultUrl': f'{base}/payment/nexi/confirm?ref={self.reference}{suffix}&tok={tok}',
            'cancelUrl': f'{base}/payment/nexi/confirm?ref={self.reference}{suffix}&cancel=1&tok={tok}',
            'notificationUrl': f'{base}/payment/nexi/confirm?ref={self.reference}{suffix}&notify=1&tok={tok}',
            # Sempre automatico: la cattura manuale rompeva i pagamenti (500).
            'captureType': 'IMPLICIT',
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
            chosen = self._nexi_chosen_contract(provider)
            if is_build:
                contract = False
            elif chosen is not None:
                # Il cliente ha scelto: la sua scelta vince sull'automatico.
                # False = "usa una nuova carta".
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
            processing_values = dict(processing_values or {}, base_url=base)
            return self._nexi_render_build_values(res, provider, payload, want_save, processing_values)
        url = provider._nexi_base_url()
        try:
            data = self._nexi_post_order(provider, url, payload, want_save)
        except ValidationError as e:
            _logger.warning('Nexi ordine %s KO: %s', self.reference, str(e)[:200])
            self._set_error(_('Nexi errore. Riprova.'))
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
            raise ValidationError(_('Build vuole URL pubblico (Base pubblica negozio o web.base.url), localhost non va. Usa HPP per i test locali.'))
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
            _logger.warning('Nexi ordine %s KO: %s', self.reference, str(e)[:200])
            self._set_error(_('Nexi errore. Riprova.'))
            raise
        # L'ordine dei soli bottoni (-m) non deve sovrascrivere la sessione
        # dei campi carta: finalize deve usare quella giusta.
        if not processing_values.get('no_service'):
            self.nexi_build_session = (data or {}).get('sessionId') or False
            self.nexi_build_token = (data or {}).get('securityToken') or False
            _logger.debug('Build carta %s -> sessione %s', self.reference, self.nexi_build_session)
        if not self.nexi_access_token:
            import secrets as _secrets
            self.sudo().nexi_access_token = _secrets.token_urlsafe(32)
        tok = self.nexi_access_token
        host = 'https://xpaysandbox.nexigroup.com' if provider.nexi_env == 'test' else 'https://xpay.nexigroup.com'
        res.update(
            nexi_build=True,
            build_data=data,
            build_fields=_nexi_safe_fields((data or {}).get('fields')),
            build_url=(data or {}).get('url') or False,
            build_state=(data or {}).get('state') or False,
            nexi_sdk=host + '/monetaweb/resources/hfsdk.js',
            build_finalize_url=f'/payment/nexi/build/{self.reference}/finalize?tok={tok}',
            confirm_url=f'/payment/nexi/confirm?ref={self.reference}&tok={tok}',
            redirect_url=f'/payment/nexi/build/{self.reference}?tok={tok}',
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
        return _nexi_latest_operation(data, AUTH_OPS)

    @api.model
    def _nexi_last_capture(self, data):
        """Ultimo incasso (CAPTURE) dell'ordine, se Nexi l'ha già fatto."""
        return _nexi_latest_operation(data, CAPTURE_OPS)

    def _process_notification_data(self, notification_data):
        super()._process_notification_data(notification_data)
        if self.provider_code != 'nexi':
            return
        # Non fidarti di cancel in URL: verifica sempre su Nexi.
        provider = self.provider_id
        create_url = provider._nexi_base_url()
        # L'ordine da controllare lo decide la transazione, mai chi chiama.
        raw_ref = (notification_data or {}).get('nexi_order_ref')
        if raw_ref and raw_ref != self.reference and raw_ref != self.reference + '-m':
            self._set_error('Ritorno Nexi non valido.')
            return
        order_ref = raw_ref or self.reference
        from urllib.parse import quote
        if '/orders/hpp' in create_url:
            verify_url = create_url.replace('/orders/hpp', f'/orders/{quote(order_ref, safe="")}')
        else:
            verify_url = create_url.rstrip('/') + f'/orders/{quote(order_ref, safe="")}'
        import time as _time
        data = {}
        result = ''
        op = {}
        op_kind = 'auth'
        for attempt in range(3):
            try:
                data = provider._nexi_make_request('GET', verify_url)
            except ValidationError as e:
                _logger.warning('Nexi verifica %s KO: %s', self.reference, str(e)[:200])
                self._set_error('Nexi non raggiungibile. Riprova.')
                return
            # Decide l'ultima operazione: con l'incasso automatico Nexi manda
            # AUTHORIZATION + CAPTURE e l'esito vero sta sull'incasso.
            op, op_kind = _nexi_decide_operation(data)
            # L'ordine da verificare lo decide la transazione; la risposta deve
            # però riferirsi a QUESTO ordine. Se manca l'id non posso dirlo:
            # fail-closed, non "fidati".
            got_order = (data.get('orderId')
                         or (data.get('orderStatus') or {}).get('order', {}).get('orderId')
                         or '')
            if not got_order or got_order != order_ref:
                self._set_error('Ritorno Nexi non valido.')
                return
            result = ((op or {}).get('operationResult') or '').upper()
            # Il circuito a volte risponde PENDING per qualche secondo:
            # riprovo invece di dichiarare subito "in attesa".
            if result and result not in WAIT_RESULTS:
                break
            if attempt < 2:
                _time.sleep(1.0)
        if is_paid(result):
            # Controlla importo e valuta dell'operazione che ha deciso, poi
            # che l'incasso copra davvero la cifra: tutto in centesimi.
            expected = to_cents(self.amount)
            got_cur = op.get('operationCurrency')
            if got_cur and got_cur != self.currency_id.name:
                self._set_error('Nexi importo non valido.')
                return
            amount_raw = op.get('operationAmount')
            if amount_raw in (None, ''):
                # Sull'incasso l'importo può stare in capturedAmount.
                amount_raw = op.get('capturedAmount')
            got = _parse_cents(amount_raw)
            masked = (
                op.get('paymentInstrumentInfo')
                or op.get('maskedPan')
                or op.get('maskedPanAlias')
                or False
            )
            status = (data or {}).get('orderStatus', {}) or {}
            # Se l'operazione non porta l'importo, il controllo sotto su
            # capturedAmount resta comunque obbligatorio.
            if got is not None and got < expected:
                self._set_error('Nexi importo non valido.')
                return
            cap = _parse_cents(status.get('capturedAmount', '0'))
            if cap is None or cap < expected:
                self._set_error('Nexi incasso non completo.')
                return
            if masked:
                self.nexi_masked_pan = _nexi_short(masked)
            self._set_done(state_message=f'Nexi OK: {result} ({op_kind})')
        elif result in AUTH_RESULTS:
            # Autorizzato ma non incassato: salvo la carta e lascio in attesa.
            for auth_op in (data or {}).get('operations', []):
                if auth_op.get('operationType') == 'AUTHORIZATION':
                    masked_auth = (
                        auth_op.get('paymentInstrumentInfo')
                        or auth_op.get('maskedPan')
                        or False
                    )
                    if masked_auth:
                        self.nexi_masked_pan = _nexi_short(masked_auth)
                    break
            # Niente cattura manuale: resta in attesa invece di andare in errore.
            self._set_pending(state_message=f'Nexi autorizzato: {result}')
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

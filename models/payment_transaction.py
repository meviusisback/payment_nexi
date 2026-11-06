# -*- coding: utf-8 -*-
"""Transazione Nexi XPay Web. Un solo flusso: crea ordine -> redirect -> verifica."""
import logging
import re
from datetime import datetime
from decimal import Decimal, InvalidOperation

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, ValidationError

from .payment_provider import (
    _nexi_base_allowed,
    _nexi_clean_base_url,
    _nexi_sanitize_text,
    is_nexi_url,
)

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
        value = Decimal(str(amount))
        if not value.is_finite():
            return 0
        return int((value * 100).to_integral_value())
    except (InvalidOperation, ValueError, TypeError, OverflowError):
        return 0


def is_paid(operation_result):
    return operation_result in OK_RESULTS


def _parse_cents(value):
    """Centesimi interi da risposta Nexi.

    XPay scrive gli importi in centesimi interi (``"1000"`` = 10,00 €), ma
    alcuni flussi mandano un decimale (``"10.00"``): il testo con il punto
    si legge come euro e si converte con Decimal (mai float), altrimenti
    un incasso vero finirebbe in errore. ``None`` se non è un numero.
    """
    try:
        text = str(value).strip()
        d = Decimal(text)
    except (InvalidOperation, ValueError, TypeError, OverflowError):
        return None
    # Decimal accetta 'Infinity' e 'NaN': non sono importi, e int() su
    # Infinity alzerebbe OverflowError fino a un 500.
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
    """Quanto Nexi dice di aver incassato, in centesimi.

    Prima il totale di livello ordine (``orderStatus.capturedAmount``): è
    l'unico che dice quanto è stato preso davvero. Le cifre di singola
    operazione valgono solo se il totale manca; come ultima spiaggia si
    sommano gli incassi in ``operations[]``. ``None`` se da nessuna parte
    c'è un numero. Con ``currency`` data, gli item in valuta diversa non
    si sommano (mai mischiare valute).
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
            _logger.warning(
                'Nexi: campo carta scartato (src non Nexi): %s',
                _nexi_sanitize_text(f.get('id'), 40))
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
    op = op if isinstance(op, dict) else {}
    raw = str(op.get('operationTime') or '').strip()
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
    data = data if isinstance(data, dict) else {}
    operations = data.get('operations')
    ops = [
        o for o in (operations if isinstance(operations, list) else [])
        if isinstance(o, dict) and str(o.get('operationType') or '').upper() in types
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

    :return: (operazione, 'auth' | 'capture' | 'uncertain'). Operazione vuota
             se Nexi non ne ha ancora restituita nessuna; 'uncertain' se AUTH
             e CAPTURE discordano ma le date non dicono qual è l'ultima (chi
             chiama deve restare in attesa, mai chiudere).
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
        # Data incerta: se concordano (entrambe pagate o entrambe non
        # pagate) decide l'autorizzazione; se discordano non si può sapere
        # qual è l'ultima: incerta, mai chiudere.
        if auth_paid == capture_paid:
            return auth, 'auth'
        return {}, 'uncertain'
    if capture_ts > auth_ts:
        return capture, 'capture'
    if capture_ts < auth_ts:
        return auth, 'auth'
    # Stesso istante: se discordano, incerta come sopra.
    if auth_paid == capture_paid:
        return capture, 'capture'
    return {}, 'uncertain'


def _nexi_got_order_id(data):
    """orderId della risposta Nexi, anche nelle forme annidate/hostili."""
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
    """URL di verifica di un ordine: base del terminale + /orders/{ref}."""
    from urllib.parse import quote
    create_url = provider._nexi_base_url()
    if '/orders/hpp' in create_url:
        return create_url.replace('/orders/hpp', f'/orders/{quote(order_ref, safe="")}')
    return create_url.rstrip('/') + f'/orders/{quote(order_ref, safe="")}'


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


def _nexi_lock_row(cr, tx_id, nowait=True):
    """Blocca la riga della transazione. Con nowait: False se è già presa.

    La verifica su Nexi può durare decine di secondi: chi arriva secondo non
    deve mettersi in coda dietro al primo, altrimenti un worker resta fermo
    quanto la rete. Il savepoint tiene la transazione usabile se il lock non
    c'è. Un posto solo per conferma e cron, così non divergono.
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
        string='ID ordine Nexi', readonly=True, copy=False)
    nexi_contract_id = fields.Char(
        string='Contratto OneClick', readonly=True, groups='base.group_system',
        copy=False,
        help='contractId CIT creato al primo pagamento, riusato dopo.',
    )
    nexi_masked_pan = fields.Char(
        string='Carta salvata', readonly=True, copy=False,
        help='Numero mascherato da Nexi (es. ****7289). Mai la carta vera.',
    )
    nexi_build_session = fields.Char(
        string='Sessione Build', readonly=True, groups='base.group_system',
        copy=False)
    nexi_build_token = fields.Char(
        string='Token Build', readonly=True, groups='base.group_system',
        copy=False)
    # Codice casuale per transazione: chi non ce l'ha non tocca il pagamento.
    nexi_access_token = fields.Char(
        string='Codice accesso Nexi', readonly=True, groups='base.group_system',
        copy=False)
    nexi_reconcile_count = fields.Integer(
        string='Tentativi riconciliazione', readonly=True, default=0,
        copy=False,
        help='Quante volte il cron ha riverificato questa transazione su Nexi. '
             'Oltre il limite la transazione viene lasciata stare.',
    )
    # Esito non terminale (4xx, orderId discordante, valuta mancante, incasso
    # parziale): il cron lo riverifica invece di lasciarlo in error per sempre.
    nexi_uncertain = fields.Boolean(
        string='Esito incerto', readonly=True, default=False, copy=False)
    nexi_reverify_count = fields.Integer(
        string='Tentativi riverifica errori', readonly=True, default=0,
        copy=False,
        help='Quante volte il cron ha riverificato un errore incerto. '
             'Contatore separato da nexi_reconcile_count.',
    )
    nexi_last_result = fields.Char(
        string='Ultimo esito Nexi', readonly=True, copy=False)
    nexi_last_captured = fields.Integer(
        string='Ultimo incassato (cent)', readonly=True, default=0, copy=False)

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
            # Accetta un id, una lista o un recordset: serve a fare una sola
            # ricerca quando i terminali sono più di uno.
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
        # Scelta legata al partner (vale anche per i pagamenti senza carrello).
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
                          'nexi_masked_pan', 'nexi_order_id', 'nexi_access_token',
                          'nexi_reconcile_count', 'nexi_uncertain',
                          'nexi_reverify_count', 'nexi_last_result',
                          'nexi_last_captured'):
                    vals.pop(f, None)
            if not vals.get('nexi_access_token'):
                import secrets as _secrets
                vals['nexi_access_token'] = _secrets.token_urlsafe(32)
        return super().create(vals_list)

    def write(self, vals):
        if not self.env.su and not self.env.user.has_group('base.group_system'):
            bad = [f for f in ('nexi_contract_id', 'nexi_build_session', 'nexi_build_token',
                               'nexi_masked_pan', 'nexi_order_id', 'nexi_access_token',
                               'nexi_reconcile_count', 'nexi_uncertain',
                               'nexi_reverify_count', 'nexi_last_result',
                               'nexi_last_captured')
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
            _logger.warning(
                'Nexi contratto morto %s, riprovo senza.',
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
                return provider._nexi_make_request('POST', url, payload=payload)
            except ValidationError as e2:
                # Il dettaglio di Nexi resta nei log: in state_message va solo
                # un messaggio generico (lo legge anche il cliente).
                _logger.warning(
                    'Nexi ordine %s KO: %s', self.reference,
                    _nexi_sanitize_text(e2, 200))
                self._set_error(_('Nexi errore. Riprova.'))
                raise

    def _nexi_candidate_base_allowed(self, candidate):
        """Un candidato base_url è accettabile solo se l'host è del sito.

        Fuori da una richiesta HTTP (test, chiamate interne) non c'è un Host
        da spoofare: il candidato resta valido.
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
            from urllib.parse import urlparse
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
        # richiesta non decide mai i return URL: al massimo un candidato
        # interno (test/direct call) viene accettato se il suo host è fra
        # quelli configurati per il sito.
        self = self.sudo()
        base = _nexi_clean_base_url(provider.nexi_public_base)
        if not base:
            base = _nexi_clean_base_url(self.get_base_url())
        if not base:
            cand = processing_values.get('base_url')
            if _nexi_clean_base_url(cand) and self._nexi_candidate_base_allowed(cand):
                _logger.warning(
                    'Nexi usa una base candidata: imposta Base pubblica negozio.')
                base = _nexi_clean_base_url(cand)
        if not base:
            base = (self.get_base_url() or '').rstrip('/')
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
        order_id = self.reference + (processing_values.get('order_suffix') or '')
        # Nexi accetta orderId fino a 27 caratteri (doc POST /orders/hpp): con un
        # prefisso di riferimento lungo l'ordine verrebbe rifiutato al buio.
        if len(order_id) > 27:
            _logger.error(
                'Nexi: orderId troppo lungo (%s): %s',
                len(order_id), _nexi_sanitize_text(order_id, 64))
            raise ValidationError(_(
                'Riferimento ordine troppo lungo per Nexi (max 27 caratteri): '
                'accorcia il prefisso dei riferimenti di pagamento.'
            ))
        payload = {
            'order': {
                'orderId': order_id,
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
            _logger.warning(
                'Nexi ordine %s KO: %s', self.reference,
                _nexi_sanitize_text(e, 200))
            self._set_error(_('Nexi errore. Riprova.'))
            raise
        data = data if isinstance(data, dict) else {}

        hosted = data.get('hostedPage') or data.get('hostedpage')
        token = data.get('securityToken') or data.get('securitytoken')
        if not hosted:
            self._set_error('Nexi non ha dato la pagina di pagamento.')
            raise ValidationError(_('Nexi errore. Riprova.'))
        # Nexi dà hostedPage + securityToken separati: vanno uniti. Il token
        # è del gateway: va encodato come valore, mai concatenato, altrimenti
        # un '&' suo diventa un parametro in più nel form.
        from urllib.parse import urlencode, urlparse, parse_qsl
        if token:
            parts = urlparse(hosted)
            query = parse_qsl(parts.query, keep_blank_values=True)
            embedded = [v for k, v in query if k.lower() == 'securitytoken']
            if embedded and embedded[0] != str(token):
                # La pagina portava già un token diverso: vale quello del
                # campo dedicato, mai quello dentro l'URL.
                _logger.warning('Nexi ordine %s: securityToken nella pagina scartato.', self.reference)
            query = [(k, v) for k, v in query if k.lower() != 'securitytoken']
            query.append(('securityToken', str(token)))
            hosted = parts._replace(query=urlencode(query)).geturl()
        _logger.info('Nexi ordine %s creato.', self.reference)
        # Un solo validatore per i domini Nexi (vedi payment_provider):
        # qui non si reimplementa, si riusa.
        from .payment_provider import is_nexi_url
        if not is_nexi_url(hosted):
            raise ValidationError(_('Nexi errore. Riprova.'))
        # Dividi base e parametri: il form GET li rimette da solo.
        parts = urlparse(hosted)
        base = f'{parts.scheme}://{parts.netloc}{parts.path}'
        params = dict(parse_qsl(parts.query))
        res.update(redirect_url=hosted, api_url=base, url_params=params)
        return res

    def _nexi_render_build_values(self, res, provider, payload, want_save=False, processing_values=None):
        """Ordine Build v3 embed: crea ordine e rimanda a template iframe."""
        self.ensure_one()
        processing_values = processing_values or {}
        base = (
            _nexi_clean_base_url(processing_values.get('base_url'))
            or _nexi_clean_base_url(self.get_base_url())
        )
        if not base:
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
            _logger.warning(
                'Nexi ordine %s KO: %s', self.reference,
                _nexi_sanitize_text(e, 200))
            self._set_error(_('Nexi errore. Riprova.'))
            raise
        data = data if isinstance(data, dict) else {}
        # L'ordine dei soli bottoni (-m) non deve sovrascrivere la sessione
        # dei campi carta: finalize deve usare quella giusta.
        if not processing_values.get('no_service'):
            self.nexi_build_session = data.get('sessionId') or False
            self.nexi_build_token = data.get('securityToken') or False
            _logger.debug(
                'Build carta %s -> sessione %s', self.reference,
                _nexi_sanitize_text(self.nexi_build_session, 64))
        if not self.nexi_access_token:
            import secrets as _secrets
            self.sudo().nexi_access_token = _secrets.token_urlsafe(32)
        tok = self.nexi_access_token
        try:
            from odoo.http import request as _request
            csrf = _request.csrf_token() if _request else ''
        except Exception:
            csrf = ''
        host = 'https://xpaysandbox.nexigroup.com' if provider.nexi_env == 'test' else 'https://xpay.nexigroup.com'
        res.update(
            nexi_build=True,
            build_data=data,
            build_fields=_nexi_safe_fields((data or {}).get('fields')),
            build_url=(data or {}).get('url') or False,
            build_state=(data or {}).get('state') or False,
            nexi_sdk=host + '/monetaweb/resources/hfsdk.js',
            # finalize è una rotta JSON: il tok va nell'header, non in query.
            build_finalize_url=f'/payment/nexi/build/{self.reference}/finalize',
            build_tok=tok,
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
            raise ValidationError(_('Ritorno Nexi non valido.'))
        full_ref = str(ref)
        if full_ref.endswith('-m'):
            notification_data['nexi_order_ref'] = full_ref
            ref = full_ref[:-2]
        tx = self.search([('reference', '=', ref), ('provider_code', '=', 'nexi')])
        if not tx:
            raise ValidationError(_('Ritorno Nexi non valido.'))
        return tx

    def _nexi_probe_paid(self, order_ref):
        """True se l'ordine Nexi risulta pagato, senza toccare lo stato.

        Sonda di sola lettura per la riconciliazione: un ordine che non
        esiste o non e' pagato vale False, mai un errore.
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
        """Riverifica le transazioni Nexi rimaste appese.

        Se il cliente chiude la scheda prima del ritorno e la notifica si
        perde, la transazione resterebbe pending per sempre con i soldi
        incassati: il cron la riverifica su Nexi con lo stesso codice della
        conferma, quindi stato e importi li decide un posto solo. Un secondo
        passaggio ritenta la post-elaborazione delle chiuse rimaste indietro.
        """
        from datetime import timedelta
        from odoo import fields as _fields
        cutoff = _fields.Datetime.now() - timedelta(minutes=10)
        stuck = self.search([
            ('provider_code', '=', 'nexi'),
            ('state', '=', 'pending'),
            ('is_post_processed', '=', False),
            ('create_date', '<', cutoff),
            ('nexi_reconcile_count', '<', 72),
        ], order='nexi_reconcile_count, create_date', limit=limit)
        for tx in stuck:
            if not _nexi_lock_row(self.env.cr, tx.id):
                _logger.info('Nexi %s: chiusura già in corso in un altro flusso.', tx.reference)
                continue
            tx.invalidate_recordset()
            if tx.state != 'pending' or tx.is_post_processed:
                continue
            try:
                with self.env.cr.savepoint():
                    # Il cron può permettersi i retry con pausa (nexi_retry):
                    # il path HTTP fa una sola GET per non tenere il worker.
                    tx._process_notification_data({'nexi_retry': True})
                    tx.invalidate_recordset()
                    if tx.state == 'pending' and tx.provider_id.nexi_mode == 'build':
                        # Il prepare Build crea anche l'ordine dei bottoni APM
                        # (reference-m): se e' lui quello pagato, chiude lui.
                        # La sonda non tocca mai lo stato: se l'ordine bottoni
                        # non e' pagato (o non esiste) non succede niente.
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
                _logger.exception('Nexi riconciliazione %s KO', tx.reference)
            finally:
                # Il contatore cresce a ogni passaggio: dopo 72 giri (~12 ore)
                # la transazione esce dal giro e resta pending da gestire a mano.
                count = (tx.nexi_reconcile_count or 0) + 1
                tx.sudo().write({'nexi_reconcile_count': count})
                if count >= 72 and tx.state == 'pending':
                    # Stesso alert del leftover: una pending che esce dal giro
                    # con soldi trattenuti non deve sparire in silenzio.
                    _logger.error(
                        'Nexi %s: ancora pending dopo %s tentativi '
                        '(ultimo esito: %s, ultimo incassato: %s cent, atteso: %s cent) '
                        '- verifica manuale.',
                        tx.reference, count, tx.nexi_last_result or tx.state_message or 'n/d',
                        tx.nexi_last_captured or 0, to_cents(tx.amount),
                    )
            if tx.state in ('done', 'error', 'cancel'):
                _logger.info(
                    'Nexi riconciliazione %s -> %s (tentativo %s).',
                    tx.reference, tx.state, tx.nexi_reconcile_count,
                )
        # Errori incerti: 4xx, orderId discordante, valuta mancante e incasso
        # parziale non sono un KO definitivo. Il cron li riverifica con un
        # contatore separato, così un incasso che si completa dopo non resta
        # in error per sempre con i soldi presi.
        from datetime import timedelta
        recent_from = _fields.Datetime.now() - timedelta(days=7)
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
                    # Una transizione a done/cancel riapre is_post_processed:
                    # qui la post-elaborazione va rifatta una volta sola.
                    if tx.state in ('done', 'cancel') and not tx.is_post_processed:
                        tx._post_process()
                        tx.invalidate_recordset()
            except Exception:
                _logger.exception('Nexi riverifica errore %s KO', tx.reference)
            finally:
                count = (tx.nexi_reverify_count or 0) + 1
                tx.sudo().write({'nexi_reverify_count': count})
                if count >= 12 and tx.state == 'error':
                    _logger.error(
                        'Nexi %s: errore incerto dopo %s verifiche '
                        '(ultimo esito: %s, ultimo incassato: %s cent, atteso: %s cent) '
                        '- verifica manuale.',
                        tx.reference, count,
                        tx.nexi_last_result or tx.state_message or 'n/d',
                        tx.nexi_last_captured or 0, to_cents(tx.amount),
                    )
            if tx.state in ('done', 'cancel'):
                _logger.info(
                    'Nexi riverifica %s -> %s (tentativo %s).',
                    tx.reference, tx.state, tx.nexi_reverify_count,
                )
        # Chiuse ma non post-elaborate (_post_process fallita dopo _set_done):
        # senza questo passaggio resterebbero ferme per sempre, perché la
        # search sopra copre solo le pending. Escluse quelle appena trattate:
        # il loro _post_process è già stato tentato nel primo giro. Stesso
        # cutoff del primo giro: al primo deploy il cron non deve masticare
        # mesi di history prima delle transazioni fresche.
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
                _logger.exception('Nexi post-elaborazione %s KO', tx.reference)
            finally:
                count = (tx.nexi_reconcile_count or 0) + 1
                tx.sudo().write({'nexi_reconcile_count': count})
                if count >= 72 and not tx.is_post_processed:
                    # Ultimo giro: da qui la transazione esce dal giro da
                    # sola, chiusa ma mai post-elaborata. Un errore nei log,
                    # una volta sola, invece del silenzio.
                    _logger.error(
                        'Nexi %s: chiusa (%s) ma mai post-elaborata dopo %s tentativi, '
                        'serve un intervento manuale.', tx.reference, tx.state, count,
                    )
        return len(stuck) + len(errors) + len(leftovers)


    @api.model
    def _nexi_last_auth(self, data):
        """L'ordine puo' avere piu' tentativi: conta solo l'ultimo."""
        return _nexi_latest_operation(data, AUTH_OPS)

    @api.model
    def _nexi_last_capture(self, data):
        """Ultimo incasso (CAPTURE) dell'ordine, se Nexi l'ha già fatto."""
        return _nexi_latest_operation(data, CAPTURE_OPS)

    def _nexi_uncertain_error(self, message, data=None, op=None):
        """Errore non definitivo: logga incassato/atteso e lascia il cron.

        Un 4xx, un orderId discordante, una valuta mancante o un incasso
        parziale non provano che il pagamento sia fallito: vanno rivisti.
        """
        expected = to_cents(self.amount)
        try:
            captured = _nexi_capture_total(
                data, op, currency=self.currency_id.name)
        except Exception:
            captured = None
        _logger.error(
            'Nexi %s: esito incerto (%s): incassato=%s cent, atteso=%s cent, '
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
        # Non fidarti di cancel in URL: verifica sempre su Nexi.
        provider = self.provider_id
        # L'ordine da controllare lo decide la transazione, mai chi chiama.
        raw_ref = notification_data.get('nexi_order_ref')
        if raw_ref and raw_ref != self.reference and raw_ref != self.reference + '-m':
            self._set_error('Ritorno Nexi non valido.')
            return
        order_ref = str(raw_ref or self.reference)
        verify_url = _nexi_verify_url(provider, order_ref)
        import time as _time
        data = {}
        result = ''
        op = {}
        op_kind = 'auth'
        # Una sola GET nel path request: i retry con sleep vivono nel cron
        # (_nexi_reconcile_pending passa attempts=3), altrimenti una confirm
        # tiene un worker fino a ~60s e i 429 auto-indotti chiudono in errore.
        attempts = 3 if (notification_data or {}).get('nexi_retry') else 1
        for attempt in range(attempts):
            try:
                data = provider._nexi_make_request('GET', verify_url)
            except ValidationError as e:
                from .payment_provider import NexiApiError, NexiUnavailable
                transient = isinstance(e, NexiUnavailable)
                if isinstance(e, NexiApiError):
                    status = getattr(e, 'status_code', None) or 0
                    transient = status == 429 or status >= 500
                if transient:
                    # Guasto temporaneo (rete, timeout, 429/5xx): in attesa,
                    # il cron riverifica. Mai error su un forse-pagato.
                    _logger.warning(
                        'Nexi verifica %s rimandata: %s', self.reference,
                        _nexi_sanitize_text(e, 200))
                    self._set_pending(state_message='Nexi non raggiungibile. Riprova.')
                    return
                _logger.warning(
                    'Nexi verifica %s KO: %s', self.reference,
                    _nexi_sanitize_text(e, 200))
                self._nexi_uncertain_error('Nexi non raggiungibile. Riprova.', data, op)
                return
            # Una risposta che non è un oggetto è un gateway alterato: le
            # guardie di _nexi_decide_operation la trattano come "incerta".
            data = data if isinstance(data, dict) else {}
            # Decide l'ultima operazione: con l'incasso automatico Nexi manda
            # AUTHORIZATION + CAPTURE e l'esito vero sta sull'incasso.
            op, op_kind = _nexi_decide_operation(data)
            op = op if isinstance(op, dict) else {}
            # L'ordine da verificare lo decide la transazione; la risposta deve
            # però riferirsi a QUESTO ordine. Se manca l'id non posso dirlo:
            # fail-closed, non "fidati".
            got_order = _nexi_got_order_id(data)
            if not got_order or got_order != order_ref:
                self._nexi_uncertain_error('Ritorno Nexi non valido.', data, op)
                return
            result = str(op.get('operationResult') or '').upper()
            if op_kind == 'uncertain':
                # AUTH e CAPTURE discordano senza date affidabili: non si può
                # sapere qual è l'ultima. In attesa, mai chiudere.
                result = ''
                break
            # Il circuito a volte risponde PENDING per qualche secondo:
            # riprovo invece di dichiarare subito "in attesa".
            if result and result not in WAIT_RESULTS:
                break
            if attempt < attempts - 1:
                _time.sleep(1.0)
        last_total = _nexi_capture_total(data, op, currency=self.currency_id.name)
        self.sudo().write({
            'nexi_last_result': result[:32] or False,
            'nexi_last_captured': last_total or 0,
        })
        if is_paid(result):
            # Controlla importo e valuta dell'operazione che ha deciso, poi
            # che l'incasso copra davvero la cifra: tutto in centesimi.
            expected = to_cents(self.amount)
            want_cur = self.currency_id.name
            got_cur = op.get('operationCurrency')
            if not got_cur or got_cur != want_cur:
                self._nexi_uncertain_error('Nexi importo non valido.', data, op)
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
            # L'importo che conta è quello incassato: totale ordine, cifre
            # dell'operazione o somma degli incassi in stessa valuta. Se
            # l'operazione che ha deciso porta meno del totale, vale il meno:
            # si chiude solo quando ogni numero visto copre l'atteso.
            total = _nexi_capture_total(data, op, currency=want_cur)
            if total is None:
                total = got
            if got is not None and total is not None and got < total:
                total = got
            if total is None or total < expected:
                self._nexi_uncertain_error('Nexi incasso non completo.', data, op)
                return
            if total != expected:
                # Incasso più alto dell'atteso: soldi presi in eccesso (es.
                # sessione stantia pagata due volte). Non si chiude done
                # senza tetto: l'operatore rimborsa dal back office Nexi.
                _logger.error(
                    'Nexi %s: incassato %s cent > atteso %s cent '
                    '(order=%s): verifica manuale.',
                    self.reference, total, expected,
                    self.nexi_order_id or self.reference,
                )
                self.sudo().write({'nexi_uncertain': False})
                self._set_error('Nexi incasso non corrispondente. Verifica manuale.')
                return
            if masked:
                self.nexi_masked_pan = _nexi_short(masked)
            self.sudo().write({'nexi_uncertain': False})
            self._set_done(state_message=f'Nexi OK: {result} ({op_kind})')
        elif result in AUTH_RESULTS:
            # Autorizzato ma non incassato: salvo la carta e lascio in attesa.
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
            # Niente cattura manuale: resta in attesa invece di andare in errore.
            self._set_pending(state_message=f'Nexi autorizzato: {result}')
        elif result in KO_RESULTS:
            self.sudo().write({'nexi_uncertain': False})
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

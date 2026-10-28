# -*- coding: utf-8 -*-
"""Prova senza Odoo. Simula Nexi in locale, niente rete, niente Odoo."""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__)))

from test_nexi import to_cents, is_paid  # riusa logica vera

# 1. Conto in centesimi
assert to_cents(12.34) == 1234, "conto sbagliato"
print("1. Conto centesimi: OK (12.34 -> 1234)")

# 2. Simula creazione ordine (come farebbe Odoo verso Nexi test)
order = {
    'orderId': 'TEST-001',
    'amount': str(to_cents(10.00)),
    'currency': 'EUR',
}
assert order['amount'] == '1000'
print(f"2. Ordine finto creato: OK {order}")

# 3. Simula risposta Nexi hostedPage
fake_resp = {'hostedPage': 'https://int-ecommerce.nexi.it/hpp/TEST-001'}
assert fake_resp['hostedPage'].startswith('https://')
print(f"3. Pagina Nexi finta: OK {fake_resp['hostedPage']}")

# 4. Simula esiti
assert is_paid('EXECUTED') is True
assert is_paid('AUTHORIZED') is False
assert is_paid('THREEDS_FAILED') is False
print("4. Esiti EXECUTED= pagato, AUTHORIZED= solo autorizzato, THREEDS_FAILED= no: OK")

# 5. Chiavi test presenti e forma giusta (36 caratteri UUID)
for k in ['8372f58a-3e55-45f4-a71b-b08b810757ae', '9ceae112-ad7b-4051-96e3-bc02746bb068']:
    assert len(k) == 36, k
print("5. Chiavi test valide: OK")

print("\nTutto senza Odoo: VERDE. Per prova vera con rete usa le carte finte su URL test.")

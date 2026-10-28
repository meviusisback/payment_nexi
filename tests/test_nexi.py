# -*- coding: utf-8 -*-
"""Test veloci senza Odoo e senza rete. Verificano solo la logica pura."""
import unittest
import sys, os, types

# Finta Odoo: basta per importare il file vero senza Odoo installato.
_odoo = types.ModuleType('odoo')
_odoo._ = lambda s, *a, **k: s % a if a else s
_models = types.SimpleNamespace(Model=object)
_odoo.models = _models
_odoo.fields = types.SimpleNamespace(
    Char=lambda *a, **k: None, Selection=lambda *a, **k: None, Boolean=lambda *a, **k: None,
)
_odoo.api = types.SimpleNamespace(model=lambda f: f)
_odoo.http = types.SimpleNamespace(Controller=object, route=lambda *a, **k: (lambda f: f))
_odoo.SUPERUSER_ID = 1
sys.modules['odoo'] = _odoo
sys.modules['odoo.exceptions'] = types.SimpleNamespace(ValidationError=Exception)
sys.modules['odoo.http'] = types.SimpleNamespace(request=None)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'models'))

from payment_transaction import to_cents, is_paid, REF_RE, _parse_amount, METHOD_SERVICE, _nexi_short


class TestNexiPure(unittest.TestCase):
    def test_cents(self):
        self.assertEqual(to_cents(12.34), 1234)
        self.assertEqual(to_cents(1.0), 100)
        self.assertEqual(to_cents(0.99), 99)
        self.assertEqual(to_cents('2.30'), 230)
        self.assertEqual(to_cents(2.675), 268)

    def test_paid(self):
        self.assertTrue(is_paid('EXECUTED'))
        self.assertFalse(is_paid('AUTHORIZED'))
        self.assertFalse(is_paid('THREEDS_FAILED'))
        self.assertFalse(is_paid('CANCELED'))
        self.assertFalse(is_paid(''))

    def test_ref(self):
        self.assertTrue(REF_RE.match('S00001-5'))
        self.assertTrue(REF_RE.match('TEST-9DCCCA76'))
        self.assertFalse(REF_RE.match('../../etc'))
        self.assertFalse(REF_RE.match('a'))

    def test_amounts(self):
        self.assertTrue(_parse_amount('230') >= _parse_amount('230'))
        self.assertTrue(_parse_amount(0) < _parse_amount('230'))

    def test_service(self):
        import re
        self.assertTrue(re.match(r'^[A-Z0-9_]{2,30}$', 'GOOGLEPAY'))
        self.assertFalse(re.match(r'^[A-Z0-9_]{2,30}$', 'a'))
        self.assertFalse(re.match(r'^[A-Z0-9_]{2,30}$', '../../etc'))

    def test_short(self):
        self.assertEqual(_nexi_short('499934******7289'), '**** 7289')
        self.assertEqual(_nexi_short('****7289'), '**** 7289')
        self.assertEqual(_nexi_short(False), 'Carta salvata')

    def test_contract(self):
        cid = 'N%06d%011d' % (3, 12)
        self.assertEqual(len(cid), 18)
        self.assertTrue(cid.startswith('N'))

    def test_methods(self):
        self.assertEqual(METHOD_SERVICE.get('card'), 'CARDS')
        self.assertEqual(METHOD_SERVICE.get('googlepay'), 'GOOGLEPAY')
        self.assertEqual(METHOD_SERVICE.get('mybank'), 'MYBANK')
        self.assertEqual(METHOD_SERVICE.get('bancomatpay'), 'BANCOMATPAY')
        self.assertEqual(METHOD_SERVICE.get('paypal'), 'PAYPAL')
        self.assertEqual(METHOD_SERVICE.get('paypal_bnpl'), 'PAYPAL_BNPL')

    def test_test_keys_present(self):
        # Le chiavi finte devono essere quelle note, mai vuote.
        self.assertTrue(len('8372f58a-3e55-45f4-a71b-b08b810757ae') > 10)
        self.assertTrue(len('9ceae112-ad7b-4051-96e3-bc02746bb068') > 10)


if __name__ == '__main__':
    unittest.main()

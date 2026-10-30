# -*- coding: utf-8 -*-
"""Stubs minimi di odoo.* per i test pytest senza runtime Odoo.

Né `payment_nexi` né questi stub toccano il vero Odoo: qui carichiamo solo
le funzioni pure (whitelist host, esiti CAPTURE, rate limit) verificando la
logica con un `odoo` finto in `sys.modules`.
"""
import sys
import types
from contextlib import contextmanager


class _Field:
    def __init__(self, *a, **k):
        self.args = a
        self.kwargs = k

    def __call__(self, *a, **k):
        return _Field(*a, **k)


class _FieldsModule:
    def __getattr__(self, name):
        def maker(*a, **k):
            return _Field(*a, **k)
        return maker

    def Datetime(self, *a, **k):
        return _Field(*a, **k)


class _ModelsModule:
    class Model:
        pass

    def check_company_domain_parent_of(self, *a, **k):
        return []


class _ApiModule:
    @staticmethod
    def model(f):
        return f

    @staticmethod
    def model_create_multi(f):
        return f

    @staticmethod
    def depends(*a, **k):
        def deco(f):
            return f
        return deco

    @staticmethod
    def onchange(*a, **k):
        def deco(f):
            return f
        return deco

    @staticmethod
    def ondelete(*a, **k):
        def deco(f):
            return f
        return deco


class _HttpModule:
    class Controller:
        pass

    class request:
        httprequest = None
        env = None

    @staticmethod
    def route(*a, **k):
        def deco(f):
            return f
        return deco


class ValidationError(Exception):
    pass


class AccessError(Exception):
    pass


class UserError(Exception):
    pass


def install():
    """Installa i moduli `odoo*` finti. Idempotente."""
    if 'odoo' in sys.modules and getattr(sys.modules['odoo'], '_nexi_stub', False):
        return
    odoo = types.ModuleType('odoo')
    odoo._nexi_stub = True
    odoo._ = lambda s, *a, **k: s % a if a else (s % k if k else s)
    odoo.api = _ApiModule()
    odoo.fields = _FieldsModule()
    odoo.models = _ModelsModule()
    odoo.http = _HttpModule()
    odoo.SUPERUSER_ID = 1
    exc = types.ModuleType('odoo.exceptions')
    exc.ValidationError = ValidationError
    exc.AccessError = AccessError
    exc.UserError = UserError
    http_mod = types.ModuleType('odoo.http')
    http_mod.Controller = _HttpModule.Controller
    http_mod.request = _HttpModule.request
    http_mod.route = _HttpModule.route
    addons = types.ModuleType('odoo.addons')
    addons.__path__ = []
    sys.modules['odoo'] = odoo
    sys.modules['odoo.exceptions'] = exc
    sys.modules['odoo.http'] = http_mod
    sys.modules['odoo.addons'] = addons


@contextmanager
def blocked_import(name):
    """Blocca un import per simulare dipendenze mancanti nel report."""
    import builtins
    real_import = builtins.__import__

    def fake(n, *a, **k):
        if n == name or n.startswith(name + '.'):
            raise ImportError('blocked for test: %s' % n)
        return real_import(n, *a, **k)

    builtins.__import__ = fake
    try:
        yield
    finally:
        builtins.__import__ = real_import


def load_addon_module(repo_root, dotted):
    """Load an addon module by path (no Odoo loader, no sys.path games)."""
    import importlib.util
    import os
    install()
    path = os.path.join(repo_root, *dotted.split('.')) + '.py'
    spec = importlib.util.spec_from_file_location('nexi_under_test_' + dotted.replace('.', '_'), path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod

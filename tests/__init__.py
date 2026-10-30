# -*- coding: utf-8 -*-
"""Test del modulo.

La riga `from . import test_nexi_flow` serve a Odoo: il suo runner importa il
pacchetto `tests` e cerca qui gli attributi che iniziano con `test_`.
Se Odoo non c'è (test standalone) l'import salta e restano solo i test puri:
    python3 -m unittest discover -s tests/standalone -t .
"""
try:
    from . import test_nexi_flow  # noqa: F401
except ImportError:  # senza Odoo installato: solo i test standalone
    pass

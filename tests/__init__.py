# -*- coding: utf-8 -*-
"""Module tests.

The line `from . import test_nexi_flow` is for Odoo: its runner imports the
`tests` package and looks here for the attributes starting with `test_`.
Without Odoo (standalone tests) the import is skipped and only the pure tests
remain:
    python3 -m unittest discover -s tests/standalone -t .
"""
try:
    from . import test_nexi_flow  # noqa: F401
except ImportError:  # no Odoo installed: standalone tests only
    pass

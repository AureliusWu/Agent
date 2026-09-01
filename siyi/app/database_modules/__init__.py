"""Focused SQLite building blocks behind :mod:`app.database`.

The public ``app.database`` module remains the compatibility facade.  These
modules deliberately receive that facade at call time so existing tests and
extensions that monkeypatch ``app.database.connect`` or ``now_iso`` continue
to observe repository work.
"""

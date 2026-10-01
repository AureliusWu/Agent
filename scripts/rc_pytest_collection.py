"""Explicitly loaded pytest evidence hook; never an auto-discovered plugin."""
from __future__ import annotations

import json
from pathlib import Path

from _pytest.junitxml import mangle_test_address

_items: list[dict[str, str]] = []
_deselected: list[str] = []


def pytest_addoption(parser) -> None:
    parser.addoption("--rc-collection-output", required=True)


def pytest_itemcollected(item) -> None:
    names = mangle_test_address(item.nodeid)
    _items.append({"nodeid": item.nodeid, "classname": ".".join(names[:-1]), "name": names[-1]})


def pytest_deselected(items) -> None:
    _deselected.extend(item.nodeid for item in items)


def pytest_sessionfinish(session, exitstatus) -> None:
    output = Path(session.config.getoption("--rc-collection-output"))
    with output.open("x", encoding="utf-8") as stream:
        json.dump({"schema_version": 1, "exit_code": int(exitstatus), "collected": _items, "deselected": _deselected}, stream)

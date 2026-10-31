"""Explicitly loaded pytest evidence hook; never an auto-discovered plugin."""
from __future__ import annotations

import json
import hashlib
from pathlib import Path

from _pytest.junitxml import mangle_test_address

_items: list[dict[str, str]] = []
_deselected: list[str] = []


def public_case_id(nodeid: str) -> str:
    """Give parameter cases opaque IDs before execution and JUnit capture.

    Parameters may intentionally contain hostile paths or credential-shaped
    fixtures. Neither a collection manifest nor JUnit should copy those values
    into case names. Actual callspec parameters and test outcomes are unchanged;
    old reports are never edited or relabelled.
    """
    prefix, separator, parameters = nodeid.partition("[")
    if not separator:
        return nodeid
    digest = hashlib.sha256(parameters.encode("utf-8", errors="surrogatepass")).hexdigest()
    return prefix + "[param-" + digest + "]"


def pytest_addoption(parser) -> None:
    parser.addoption("--rc-collection-output", required=True)


def pytest_itemcollected(item) -> None:
    # Pytest 9.1.1 is locked. Mutate identity at initial collection, before
    # selection, execution, reporting or our pre-selection inventory capture.
    # Keep originalname/callspec intact: no test, assertion or input is removed.
    item._nodeid = public_case_id(item.nodeid)
    names = mangle_test_address(item.nodeid)
    _items.append({"nodeid": item.nodeid, "classname": ".".join(names[:-1]), "name": names[-1]})


def pytest_deselected(items) -> None:
    _deselected.extend(item.nodeid for item in items)


def pytest_sessionfinish(session, exitstatus) -> None:
    output = Path(session.config.getoption("--rc-collection-output"))
    with output.open("x", encoding="utf-8") as stream:
        json.dump({"schema_version": 1, "case_id_protocol": "opaque-parameter-sha256-v1",
                   "exit_code": int(exitstatus), "collected": _items, "deselected": _deselected}, stream)

"""Check the uninstall hook without executing an installer or using the registry.

The small interpreter below runs the checked-in hook against an in-memory model.
It is a focused control-flow test, not Windows installed-lifecycle acceptance.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[3]
HOOK = ROOT / "desktop" / "src-tauri" / "windows" / "installer-hooks.nsh"
INSTALL_DIR = r"C:\Users\test\测试\司忆"


def postuninstall_lines() -> list[str]:
    source = HOOK.read_text(encoding="utf-8")
    match = re.search(
        r"^!macro NSIS_HOOK_POSTUNINSTALL\s*\n(?P<body>.*?)^!macroend\s*$",
        source,
        re.MULTILINE | re.DOTALL,
    )
    assert match is not None, "the product needs a bounded POSTUNINSTALL hook"
    return [
        line.strip()
        for line in match.group("body").splitlines()
        if line.strip() and not line.lstrip().startswith(";")
    ]


@dataclass
class RegistryModel:
    exists: bool = True
    values: dict[str, str] = field(default_factory=dict)
    subkeys: set[str] = field(default_factory=set)
    read_error: bool = False
    delete_error: bool = False
    actions: list[str] = field(default_factory=list)


def run_hook(
    registry: RegistryModel, *, update_mode: str = "", install_dir: str = INSTALL_DIR
) -> tuple[RegistryModel, str]:
    """Interpret only the bounded statements allowed in this product hook."""

    conditions: list[bool] = []
    stack: list[str] = []
    register = "preserved R8"
    errors = False

    def condition(expression: str) -> bool:
        if expression == "$UpdateMode <> 1":
            return update_mode != "1"
        if expression == "${Errors}":
            return errors
        if expression == '$R8 S!= ""':
            return register != ""
        if expression == '$R8 S== "$INSTDIR"':
            return register == install_dir
        raise AssertionError(f"unexpected hook condition: {expression}")

    for line in postuninstall_lines():
        if line.startswith("${IfNot} "):
            conditions.append(all(conditions) and not condition(line[9:]))
        elif line.startswith("${If} "):
            conditions.append(all(conditions) and condition(line[6:]))
        elif line.startswith("${AndIf} "):
            assert conditions
            conditions[-1] = conditions[-1] and condition(line[9:])
        elif line == "${EndIf}":
            assert conditions
            conditions.pop()
        elif not all(conditions):
            continue
        elif line == "Push $R8":
            stack.append(register)
        elif line == "Pop $R8":
            register = stack.pop()
        elif line == "ClearErrors":
            errors = False
        elif line == 'ReadRegStr $R8 SHCTX "${MANUPRODUCTKEY}" ""':
            errors = registry.read_error or not registry.exists or "" not in registry.values
            register = "" if errors else registry.values[""]
        elif line == 'DeleteRegValue SHCTX "${MANUPRODUCTKEY}" ""':
            registry.actions.append("delete_default")
            errors = registry.delete_error
            if not errors:
                registry.values.pop("", None)
        elif line == 'DeleteRegKey /ifempty SHCTX "${MANUPRODUCTKEY}"':
            registry.actions.append("delete_leaf_if_empty")
            if not registry.values and not registry.subkeys:
                registry.exists = False
        else:
            raise AssertionError(f"unexpected hook instruction: {line}")

    assert not conditions, "unbalanced hook condition"
    assert not stack, "unbalanced register preservation"
    return registry, register


def test_matching_default_value_is_removed_and_empty_product_leaf_is_removed() -> None:
    registry, register = run_hook(RegistryModel(values={"": INSTALL_DIR}))
    assert registry.exists is False
    assert registry.values == {}
    assert registry.actions == ["delete_default", "delete_leaf_if_empty"]
    assert register == "preserved R8"


@pytest.mark.parametrize(
    ("extra_values", "subkeys"),
    [
        ({"Installer Language": "2052"}, set()),
        ({"preference": ""}, set()),
        ({}, {"other"}),
        ({"preference": "keep"}, {"other"}),
    ],
)
def test_matching_value_does_not_remove_other_values_or_subkeys(
    extra_values: dict[str, str], subkeys: set[str]
) -> None:
    registry, register = run_hook(
        RegistryModel(values={"": INSTALL_DIR, **extra_values}, subkeys=set(subkeys))
    )
    assert registry.exists is True
    assert registry.values == extra_values
    assert registry.subkeys == subkeys
    assert register == "preserved R8"


@pytest.mark.parametrize(
    "stored_path",
    [
        INSTALL_DIR.replace("测试", "other"),
        INSTALL_DIR.lower(),
        INSTALL_DIR + "\\",
        '"' + INSTALL_DIR + '"',
    ],
)
def test_non_exact_install_path_is_untouched(stored_path: str) -> None:
    registry, register = run_hook(RegistryModel(values={"": stored_path}))
    assert registry.exists is True
    assert registry.values == {"": stored_path}
    assert registry.actions == []
    assert register == "preserved R8"


def test_update_uninstall_preserves_the_previous_install_path() -> None:
    registry, register = run_hook(
        RegistryModel(values={"": INSTALL_DIR}), update_mode="1"
    )
    assert registry.exists is True
    assert registry.values == {"": INSTALL_DIR}
    assert registry.actions == []
    assert register == "preserved R8"


@pytest.mark.parametrize(
    "registry",
    [
        RegistryModel(exists=False),
        RegistryModel(),
        RegistryModel(values={"preference": "keep"}),
        RegistryModel(values={"": ""}),
        RegistryModel(values={"": INSTALL_DIR}, read_error=True),
    ],
)
def test_missing_empty_or_unreadable_default_value_is_untouched(
    registry: RegistryModel,
) -> None:
    before = (registry.exists, dict(registry.values), set(registry.subkeys))
    registry, register = run_hook(registry)
    assert (registry.exists, registry.values, registry.subkeys) == before
    assert registry.actions == []
    assert register == "preserved R8"


def test_empty_installdir_never_confers_ownership_of_an_empty_default() -> None:
    registry, register = run_hook(RegistryModel(values={"": ""}), install_dir="")
    assert registry.exists is True
    assert registry.values == {"": ""}
    assert registry.actions == []
    assert register == "preserved R8"


def test_failed_default_deletion_cannot_delete_a_nonempty_product_key() -> None:
    registry, register = run_hook(
        RegistryModel(values={"": INSTALL_DIR}, delete_error=True)
    )
    assert registry.exists is True
    assert registry.values == {"": INSTALL_DIR}
    assert register == "preserved R8"


def test_hook_has_only_owned_default_value_and_empty_leaf_deletions() -> None:
    lines = postuninstall_lines()
    deletions = [line for line in lines if line.startswith("Delete")]
    assert deletions == [
        'DeleteRegValue SHCTX "${MANUPRODUCTKEY}" ""',
        'DeleteRegKey /ifempty SHCTX "${MANUPRODUCTKEY}"',
    ]
    assert lines.index("${If} $UpdateMode <> 1") < lines.index(deletions[0])
    assert lines.index('${AndIf} $R8 S== "$INSTDIR"') < lines.index(deletions[0])
    assert not any("${MANUKEY}" in line for line in lines)
    assert not any("APPDATA" in line or "LOCALAPPDATA" in line for line in lines)
    assert not any(line.startswith(("Exec", "RmDir", "System::", "Delete ")) for line in lines)


def test_tauri_configuration_uses_the_checked_in_product_hook() -> None:
    config = json.loads(
        (ROOT / "desktop" / "src-tauri" / "tauri.conf.json").read_text(encoding="utf-8")
    )
    assert config["bundle"]["windows"]["nsis"]["installerHooks"] == "windows/installer-hooks.nsh"

"""Fresh public case identities, never edited historical test receipts."""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from types import SimpleNamespace
import xml.etree.ElementTree as ET

import pytest

ROOT = Path(__file__).resolve().parents[3]
spec = importlib.util.spec_from_file_location("public_case_ids_tested", ROOT / "scripts/rc_pytest_collection.py")
hook = importlib.util.module_from_spec(spec)
assert spec and spec.loader
spec.loader.exec_module(hook)


def transport():
    spec = importlib.util.spec_from_file_location("public_case_ids_privacy", ROOT / "scripts/export-accepted-rc.py")
    value = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(value)
    return value


def test_plain_function_names_and_mandatory_native_probe_names_stay_exact():
    name = "tests/backend/tools/test_file_recovery_review_v160.py::test_parent_junction_swap_at_commit_must_not_write_outside_workspace"
    assert hook.public_case_id(name) == name


def test_parameter_identity_is_deterministic_distinct_and_does_not_copy_values():
    prefix = "tests/backend/test_example.py::test_fixture"
    first = hook.public_case_id(prefix + "[one-private-fixture]")
    assert first == hook.public_case_id(prefix + "[one-private-fixture]")
    assert first != hook.public_case_id(prefix + "[other-private-fixture]")
    assert first.startswith(prefix + "[param-") and len(first.rsplit("param-", 1)[1][:-1]) == 64
    assert "private-fixture" not in first


@pytest.mark.parametrize("fail", [False, True], ids=["passed_cases", "failed_case_retained"])
def test_real_pytest_inventory_and_junit_keep_all_cases_and_failures_without_fixture_ids(tmp_path, fail):
    """Run only a synthetic test module; no product model or installer executes."""
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    shutil.copyfile(ROOT / "scripts/rc_pytest_collection.py", scripts / "rc_pytest_collection.py")
    fixtures = [chr(67) + ":/" + "/".join(("Users", "synthetic-test-only", "private.json")),
                "sk-" + "synthetic_fixture_" + "0" * 20,
                "13" + "0" * 9]
    test = tmp_path / "test_fixture.py"
    test.write_text("import pytest\n" +
        "@pytest.mark.parametrize('value', " + repr(fixtures) + ", ids=" + repr(fixtures) + ")\n" +
        "def test_parameter(value):\n" +
        ("    assert value != " + repr(fixtures[1]) + "\n" if fail else "    assert value in " + repr(fixtures) + "\n"),
        encoding="utf-8")
    environment = {key: value for key, value in os.environ.items()
                   if not key.startswith(("PYTEST_", "COVERAGE_", "COV_CORE_"))}
    environment.update(PYTEST_DISABLE_PLUGIN_AUTOLOAD="1", PYTHONPATH=str(scripts))
    completed = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-p", "rc_pytest_collection",
                               "-o", "addopts=", "--rootdir=.", "test_fixture.py", "--junitxml=junit.xml",
                               "--rc-collection-output=collection.json"],
                              cwd=tmp_path, env=environment, capture_output=True, text=True, timeout=45)
    assert completed.returncode == (1 if fail else 0), completed.stdout + completed.stderr
    inventory = json.loads((tmp_path / "collection.json").read_text())
    assert inventory["case_id_protocol"] == "opaque-parameter-sha256-v1"
    assert len(inventory["collected"]) == 3 and inventory["deselected"] == []
    cases = ET.parse(tmp_path / "junit.xml").findall(".//testcase")
    assert len(cases) == 3
    assert {(row["classname"], row["name"]) for row in inventory["collected"]} == {
        (row.get("classname"), row.get("name")) for row in cases}
    assert sum(row.find("failure") is not None for row in cases) == int(fail)
    assert len({row["nodeid"] for row in inventory["collected"]}) == 3
    public = transport()
    public.public_content("collection.json", (tmp_path / "collection.json").read_bytes())
    if not fail:
        public.public_content("junit.xml", (tmp_path / "junit.xml").read_bytes())
    else:
        # Failure messages may contain the actual inputs. Retain the original
        # failure, and keep the upload privacy gate strict rather than editing it.
        with pytest.raises(ValueError):
            public.public_content("junit.xml", (tmp_path / "junit.xml").read_bytes())


def test_real_skipped_junit_captures_source_relative_location_without_hiding_skip(tmp_path):
    # The source checkout deliberately has a private-shaped ancestor. The
    # actual test location must be relative at creation, not edited afterwards.
    checkout = tmp_path / "home" / "synthetic-test-only" / "checkout"
    checkout.mkdir(parents=True)
    scripts = checkout / "scripts"
    scripts.mkdir()
    shutil.copyfile(ROOT / "scripts/rc_pytest_collection.py", scripts / "rc_pytest_collection.py")
    (checkout / "test_fixture.py").write_text(
        "\n".join(["import pytest", "@pytest.mark.skip(reason='requires explicit device selection')",
                    "def test_device():", "    assert False", ""]),
        encoding="utf-8")
    environment = {key: value for key, value in os.environ.items()
                   if not key.startswith(("PYTEST_", "COVERAGE_", "COV_CORE_"))}
    environment.update(PYTEST_DISABLE_PLUGIN_AUTOLOAD="1", PYTHONPATH=str(scripts))
    completed = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-p", "rc_pytest_collection",
                               "-o", "addopts=", "--rootdir=.", "test_fixture.py", "--junitxml=junit.xml",
                               "--rc-collection-output=collection.json"],
                              cwd=checkout, env=environment, capture_output=True, text=True, timeout=45)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    cases = ET.parse(checkout / "junit.xml").findall(".//testcase")
    assert len(cases) == 1
    skipped = cases[0].find("skipped")
    assert skipped is not None
    assert skipped.get("message") == "requires explicit device selection"
    assert skipped.text.startswith("test_fixture.py:")
    assert "synthetic-test-only" not in skipped.text
    inventory = json.loads((checkout / "collection.json").read_text())
    assert inventory["location_protocol"] == "source-relative-skip-location-v1"
    assert len(inventory["collected"]) == 1 and inventory["deselected"] == []
    transport().public_content("junit.xml", (checkout / "junit.xml").read_bytes())


def test_skip_capture_never_hides_external_locations_reasons_or_failure_details(tmp_path):
    root = tmp_path / "checkout"
    root.mkdir()
    item = SimpleNamespace(config=SimpleNamespace(rootpath=root))
    original = (str(tmp_path / "outside.py"), 17, "Skipped: original device requirement")
    for report in (SimpleNamespace(skipped=True, longrepr=original),
                   SimpleNamespace(skipped=False, longrepr="original failure details")):
        before = report.longrepr
        wrapper = hook.pytest_runtest_makereport(item, None)
        next(wrapper)
        with pytest.raises(StopIteration) as completed:
            wrapper.send(report)
        assert completed.value.value is report
        assert report.longrepr == before

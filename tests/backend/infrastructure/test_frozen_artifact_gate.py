from __future__ import annotations

import importlib.util
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "check-frozen-artifacts.py"
SPEC = importlib.util.spec_from_file_location("frozen_artifact_gate", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_inventory_requires_all_artifact_runtime_groups() -> None:
    listing = "\n".join(
        [
            "app.artifacts.docx",
            "app.artifacts.pdf",
            "app.artifacts.pptx",
            "app.artifacts.render",
            "app.artifacts.service",
            "app.artifacts.validation",
            "docx\\templates\\default.docx",
            "pptx\\templates\\default.pptx",
            "pypdfium2_raw\\pdfium.dll",
            "pypdfium2",
            "lxml.etree",
            "reportlab.pdfgen",
            "PIL.JpegImagePlugin",
            "PIL.PngImagePlugin",
            "PIL.WebPImagePlugin",
        ]
    )
    assert MODULE.inventory_decision(listing)["passed"] is True


def test_inventory_rejects_missing_native_payload_or_node_modules() -> None:
    missing = MODULE.inventory_decision("app.artifacts.docx")
    assert "pdfium_binary" in missing["missing_groups"]
    assert missing["passed"] is False

    duplicated = MODULE.inventory_decision("node_modules\n" + "\n".join(sum(MODULE.REQUIRED_GROUPS.values(), ())))
    assert duplicated["duplicate_web_payload"] is True
    assert duplicated["passed"] is False

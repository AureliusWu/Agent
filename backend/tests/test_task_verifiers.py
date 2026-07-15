from app.task_verifiers import VERIFIERS, classify_command, detect_verifier_domains, verify_domain


def test_domain_detection_and_registry_cover_release_contract() -> None:
    domains = detect_verifier_domains(
        "修复 FastAPI 权限接口和前端页面，并更新说明文档",
        ("backend/app/routes/auth.py", "frontend/src/App.tsx", "README.md"),
    )

    assert {"code", "api", "ui", "security", "document"} <= set(domains)
    assert set(VERIFIERS) == {"code", "api", "ui", "database", "security", "document", "multimodal"}


def test_command_classification_keeps_languages_and_domains_separate() -> None:
    assert {"code", "python", "test"} <= classify_command("python -m pytest backend/tests/test_api.py")
    assert {"code", "javascript", "ui"} <= classify_command("npm run build")
    assert "database" in classify_command("python -m pytest backend/tests/test_database.py")
    assert "security" in classify_command("python -m pytest backend/tests/test_security.py")
    assert "verification" not in classify_command("echo test completed")


def test_multimodal_verifier_is_explicitly_reserved() -> None:
    report = verify_domain("multimodal", "verify_multimodal", "验证图片输出", {"verification_commands": []})

    assert report["status"] == "unavailable"
    assert report["required"] is False
    assert report["verifier"] == "MultimodalVerifier"

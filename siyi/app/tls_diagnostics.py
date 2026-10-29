"""Offline TLS construction check, without app startup, data or credentials."""
from __future__ import annotations

import json
import ssl
import sys

import certifi
import httpx


def tls_self_check() -> dict[str, object]:
    result: dict[str, object] = {
        "status": "failed",
        "frozen": bool(getattr(sys, "frozen", False)),
        "network_requests": 0,
        "credential_reads": 0,
        "application_started": False,
    }
    try:
        context = ssl.create_default_context(cafile=certifi.where())
        if context.verify_mode != ssl.CERT_REQUIRED or not context.check_hostname:
            result["error_type"] = "tls_verification_disabled"
            return result
        # Build the same default HTTPX TLS transport used by the Provider. No
        # request is sent, and proxy/environment credentials are not consulted.
        with httpx.Client(trust_env=False):
            pass
        result.update({
            "status": "passed",
            "ssl_context_constructed": True,
            "httpx_transport_constructed": True,
            "certificate_verification": True,
            "hostname_verification": True,
        })
    except FileNotFoundError:
        result["error_type"] = "tls_resource_missing"
    except Exception:
        # No exception text: TLS libraries can include local paths or settings.
        result["error_type"] = "tls_initialization_failed"
    return result


def main() -> int:
    result = tls_self_check()
    print(json.dumps(result, ensure_ascii=True, sort_keys=True))
    return 0 if result["status"] == "passed" else 1

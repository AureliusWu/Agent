from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import uuid

import pytest


ROOT = Path(__file__).resolve().parents[3]
spec = importlib.util.spec_from_file_location("rc_desktop_evidence_tested", ROOT / "scripts/rc_gate.py")
assert spec and spec.loader
rc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rc)


def ref(root: Path, name: str, value):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(value if isinstance(value, bytes) else json.dumps(value).encode())
    return {"path": name, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def identity(label="candidate"):
    return {"source_version": "16.0.0", "source_commit": ("a" if label == "candidate" else "b") * 40,
            "source_tree_fingerprint": ("C" if label == "candidate" else "D") * 64, "workspace_clean": True}


def manifest(label="candidate"):
    source = identity(label)
    build = ("1" if label == "candidate" else "2") * 24
    return {"manifest_version": 1, "product_version": source["source_version"], "git_commit": source["source_commit"],
            "source_fingerprint": source["source_tree_fingerprint"], "build_id": build, "database_schema_version": 46,
            "component_build_ids": {key: f"{key}-{build}" for key in rc.COMPONENT_OBSERVERS}, "build_type": "Release", "workspace_state": "CLEAN"}


def pe_bytes(label: str):
    # A synthetic test-only PE-shaped fixture, never executed or installer evidence.
    data = bytearray(1024 * 1024)
    data[:2] = b"MZ"
    data[60:64] = (64).to_bytes(4, "little")
    data[64:68] = b"PE\0\0"
    data[100:100 + len(label)] = label.encode()
    return bytes(data)


def desktop_fixture(root: Path, label="candidate"):
    source, build = identity(label), manifest(label)
    binaries = {key: ref(root, f"{label}/{key}.exe", pe_bytes(label + key)) for key in ("desktop", "sidecar")}
    hashes = {key: value["sha256"] for key, value in binaries.items()}
    ref(root, f"{label}/_internal/build-info.json", build)
    ref(root, f"{label}/_internal/synthetic-code.bin", b"isolated payload fixture " + label.encode())
    inventory = rc.module("rc_payload_inventory").inventory(root, binaries["sidecar"])
    payload_ref = ref(root, f"{label}/payload-inventory.json", inventory)
    receipt = {"schema_version": 1, "report_type": "rc_desktop_runtime_observation", "protocol_version": "desktop-render-ready-v1",
               "actual_run": True, "status": "PASS", "acceptance_nonce": str(uuid.uuid4()), "desktop_render_ready": True,
               "sidecar_ready": True, "isolated_test_data": True, "readiness_ms": 90, "process_ids": {"desktop": 100, "sidecar": 200},
               "components": {key: copy.deepcopy(build) for key in rc.COMPONENT_OBSERVERS}}
    observation = {"schema_version": 1, "report_type": "rc_desktop_startup_observation", "actual_run": True, "status": "PASS",
                   "source": source, "source_after": source, "binary_sha256": hashes, "build_id": build["build_id"],
                   "measurement_object": "desktop", "measurement_protocol": "desktop-render-ready-v1", "startup_path": "portable-desktop",
                   "cache_state": "warm", "host_fingerprint": "synthetic-host", "readiness_ms": 100, "runtime_readiness_ms": 90,
                   "desktop_render_ready": True, "sidecar_ready": True, "isolated_test_data": True, "application_receipt": receipt,
                   "sidecar_payload": payload_ref}
    return binaries, hashes, observation


def test_adjacent_correct_manifests_do_not_bind_arbitrary_executable_bytes(tmp_path):
    binaries, _, observation = desktop_fixture(tmp_path)
    with pytest.raises(rc.GateError, match="observations"):
        rc.validate_component_observations(tmp_path, {"binaries": binaries, "sidecar_payload": observation["sidecar_payload"]}, identity(), manifest())


@pytest.mark.parametrize("mutation", [None, "binary", "react", "layer", "nonce", "command"])
def test_actual_component_observation_chain_rejects_wrong_binary_identity_or_capture(tmp_path, mutation):
    binaries, hashes, observation = desktop_fixture(tmp_path)
    bundle = {"binaries": binaries, "component_observations": {}, "sidecar_payload": observation["sidecar_payload"]}
    if mutation == "react":
        observation["application_receipt"]["components"]["react"]["build_id"] = "f" * 24
    elif mutation == "nonce":
        observation["application_receipt"].pop("acceptance_nonce")
    elif mutation == "binary":
        observation["binary_sha256"]["desktop"] = "f" * 64
    observation_ref = ref(tmp_path, "runtime-observation.json", observation)
    for component, protocol in rc.COMPONENT_OBSERVERS.items():
        proof = {"schema_version": 1, "report_type": "rc_component_observation", "target_version": "16.0.0", "kind": "automated",
                 "actual_run": True, "status": "PASS", "source": identity(), "source_after": identity(), "component": component,
                 "observer_protocol": protocol, "build_id": manifest()["build_id"], "binary_sha256": hashes,
                 "observation": observation_ref, "command": rc.desktop_sample_command(binaries, observation_ref["path"], observation),
                 "cwd": ".", "exit_code": 0, "timed_out": False}
        if mutation == "layer": proof["observer_protocol"] = "unit-test"
        elif mutation == "command": proof["command"] = ["python", "-c", "exit(0)"]
        bundle["component_observations"][component] = ref(tmp_path, f"{component}-envelope.json", proof)
    if mutation is None:
        rc.validate_component_observations(tmp_path, bundle, identity(), manifest())
    else:
        with pytest.raises(rc.GateError):
            rc.validate_component_observations(tmp_path, bundle, identity(), manifest())


def performance_fixture(root: Path):
    comparison = {"output": "performance.json"}
    hashes = None
    for label in ("baseline", "candidate"):
        binaries, hashes, observation = desktop_fixture(root, label)
        series_id = str(uuid.uuid4())
        series = {key: observation[key] for key in ("measurement_object", "measurement_protocol", "startup_path", "cache_state", "host_fingerprint", "build_id")}
        series.update(run_id=series_id, binaries=binaries, samples_ms=[100] * 5, sample_reports=[])
        for index in range(5):
            sample_observation = copy.deepcopy(observation)
            sample_observation["application_receipt"]["acceptance_nonce"] = str(uuid.uuid4())
            observation_ref = ref(root, f"{label}/observation-{index}.json", sample_observation)
            proof = {"schema_version": 1, "report_type": "rc_startup_sample", "actual_run": True, "status": "PASS",
                     "source": identity(label), "source_after": identity(label), "series_run_id": series_id, "run_id": str(uuid.uuid4()),
                     "sample_index": index, "binary_sha256": hashes, "observation": observation_ref,
                     "command": rc.desktop_sample_command(binaries, observation_ref["path"], series), "cwd": ".", "exit_code": 0, "timed_out": False,
                     **{key: series[key] for key in ("measurement_object", "measurement_protocol", "startup_path", "cache_state", "host_fingerprint", "build_id")}}
            series["sample_reports"].append(ref(root, f"{label}/sample-{index}.json", proof))
        comparison[label] = series
    return comparison, hashes


@pytest.mark.parametrize("mutation", [None, "sidecar", "argv", "same_observation", "tamper", "host", "old_build"])
def test_five_actual_desktop_samples_are_bound_to_process_observations(tmp_path, mutation):
    comparison, hashes = performance_fixture(tmp_path)
    series = comparison["candidate"]
    proof = rc.attachment(tmp_path, series["sample_reports"][1])
    if mutation == "sidecar":
        comparison["candidate"]["measurement_object"] = "sidecar"
    elif mutation == "argv":
        proof["command"] = ["powershell", "-Command", "Write-Output ok", "scripts/record-rc-desktop-startup.py"]
    elif mutation == "same_observation":
        other = rc.attachment(tmp_path, series["sample_reports"][0])
        proof["observation"] = other["observation"]
        proof["command"] = other["command"]
    elif mutation == "tamper":
        (tmp_path / proof["observation"]["path"]).write_text("{}", encoding="utf-8")
    elif mutation == "host":
        proof["host_fingerprint"] = "different-host"
    elif mutation == "old_build":
        proof["source"] = proof["source_after"] = identity("baseline")
    series["sample_reports"][1] = ref(tmp_path, "candidate/sample-1.json", proof)
    if mutation is None:
        rc.performance_comparison(comparison, manifest()["build_id"])
        rc.validate_performance_evidence(tmp_path, comparison, identity(), hashes)
    else:
        with pytest.raises(rc.GateError):
            rc.performance_comparison(comparison, manifest()["build_id"])
            rc.validate_performance_evidence(tmp_path, comparison, identity(), hashes)


def test_only_the_fixed_collector_argv_is_performance_execution(tmp_path):
    comparison, _ = performance_fixture(tmp_path)
    raw = {"command": ["python", *rc.performance_collection_arguments(comparison)], "cwd": ".", "performance_comparison": comparison}
    rc.automated_command(raw, "startup_performance_comparison")
    raw["command"] = ["powershell", "-Command", "Write-Output ok", "scripts/record-rc-performance.py"]
    with pytest.raises(rc.GateError):
        rc.automated_command(raw, "startup_performance_comparison")


def test_unchanged_exe_with_modified_internal_payload_invalidates_evidence(tmp_path):
    binaries, _, observation = desktop_fixture(tmp_path)
    rc.validate_sidecar_payload(tmp_path, observation["sidecar_payload"], binaries["sidecar"])
    (tmp_path / "candidate/_internal/synthetic-code.bin").write_bytes(b"changed native/application payload")
    with pytest.raises(rc.GateError, match="payload differs"):
        rc.validate_sidecar_payload(tmp_path, observation["sidecar_payload"], binaries["sidecar"])

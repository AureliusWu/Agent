from __future__ import annotations

import ast
import hashlib
import importlib.util
import runpy
from pathlib import Path
import sys

import pytest


ROOT = Path(__file__).resolve().parents[3]
HOOK = ROOT / "scripts" / "pyinstaller-hooks" / "hook-PIL.Image.py"


def test_sidecar_packages_only_supported_pillow_image_plugins() -> None:
    module = ast.parse(HOOK.read_text(encoding="utf-8"))
    assignment = next(
        node
        for node in module.body
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == "hiddenimports" for target in node.targets)
    )
    assert ast.literal_eval(assignment.value) == [
        "PIL.JpegImagePlugin",
        "PIL.PngImagePlugin",
        "PIL.WebPImagePlugin",
    ]


def test_all_sidecar_build_paths_use_the_bounded_pillow_hook() -> None:
    for relative_path in ("scripts/build-desktop.ps1", "scripts/build-runtime.ps1"):
        source = (ROOT / relative_path).read_text(encoding="utf-8")
        assert "scripts\\pyinstaller-hooks" in source
        assert "--additional-hooks-dir $hookDirectory" in source


def test_sidecar_compresses_at_installer_level_without_removing_runtime_features(monkeypatch) -> None:
    from PyInstaller.archive.writers import ZlibArchiveWriter
    import PyInstaller.__main__

    wrapper = runpy.run_path(str(ROOT / "scripts" / "build-frozen-sidecar.py"))
    lock = (ROOT / "siyi" / "requirements.lock").read_text(encoding="utf-8")
    assert f'pyinstaller=={wrapper["VALIDATED_PYINSTALLER_VERSION"]}' in lock.splitlines()
    collected = []
    original_writer = vars(ZlibArchiveWriter)["_write_entry"]

    def fake_run(args):
        assert ZlibArchiveWriter._COMPRESSION_LEVEL == 0
        assert vars(ZlibArchiveWriter)["_write_entry"] is not original_writer
        collected.extend(args)

    monkeypatch.setattr(PyInstaller.__main__, "run", fake_run)
    assert wrapper["main"](["--onedir", "run_server.py"]) == 0
    assert ZlibArchiveWriter._COMPRESSION_LEVEL == 6
    assert vars(ZlibArchiveWriter)["_write_entry"] is original_writer
    for module in ("fsspec.conftest", "pytest", "_pytest", "httpx._main"):
        index = collected.index(module)
        assert collected[index - 1] == "--exclude-module"
    for module in ("PIL.ImageDraw", "PIL.ImageDraw2", "PIL.ImagePath", "PIL.ImageText"):
        index = collected.index(module)
        assert collected[index - 1] == "--hidden-import"
    assert collected[-2:] == ["--onedir", "run_server.py"]
    for relative_path in ("scripts/build-desktop.ps1", "scripts/build-runtime.ps1"):
        source = (ROOT / relative_path).read_text(encoding="utf-8")
        assert "build-frozen-sidecar.py" in source
        assert "& $python $sidecarBuildScript --noconfirm --clean --onedir" in source


@pytest.mark.skipif(sys.platform != "win32", reason="locked Windows wheel metadata")
def test_frozen_numpy_redacts_only_verified_diagnostic_paths(tmp_path: Path) -> None:
    wrapper = runpy.run_path(str(ROOT / "scripts" / "build-frozen-sidecar.py"))
    package = importlib.util.find_spec("numpy")
    assert package is not None and package.origin
    source = Path(package.origin).with_name("__config__.py")
    original = source.read_bytes()
    compiled = compile(original, "numpy/__config__.py", "exec")
    sanitized = wrapper["scrub_numpy_build_paths"](source, compiled)
    paths = wrapper["NUMPY_BUILD_PATHS"]
    changes = 0
    assert len(sanitized.co_consts) == len(compiled.co_consts)
    for before, after in zip(compiled.co_consts, sanitized.co_consts, strict=True):
        identity = hashlib.sha256(before.encode("utf-8")).hexdigest() if type(before) is str else None
        if identity in paths:
            assert after == paths[identity]
            changes += 1
        else:
            assert after == before
    assert changes == 4
    assert sanitized.co_code == compiled.co_code and sanitized.co_names == compiled.co_names
    assert source.read_bytes() == original

    different = tmp_path / "__config__.py"
    different.write_bytes(original + b"\n")
    with pytest.raises(SystemExit, match="exact NumPy Windows build metadata"):
        wrapper["scrub_numpy_build_paths"](different, compiled)
    with pytest.raises(SystemExit, match="locked bytecode contract"):
        wrapper["scrub_numpy_build_paths"](source, compile("pass", "numpy/__config__.py", "exec"))


def test_onnx_hook_prunes_only_locked_standalone_c_api(monkeypatch) -> None:
    import importlib.metadata
    import PyInstaller.utils.hooks

    c_api = ("wheel/onnxruntime/capi/onnxruntime.dll", "onnxruntime/capi")
    required = ("wheel/onnxruntime/capi/onnxruntime_providers_shared.dll", "onnxruntime/capi")
    other_dir = ("wheel/another/onnxruntime.dll", "another")
    hook = ROOT / "scripts" / "pyinstaller-hooks" / "hook-onnxruntime.py"

    def fake_data(package, *, include_py_files, excludes):
        assert package == "onnxruntime" and include_py_files is False
        assert excludes in (None, ["capi/onnxruntime.dll"])
        return [required] if excludes else [c_api, required]

    monkeypatch.setattr(PyInstaller.utils.hooks, "collect_data_files", fake_data)
    monkeypatch.setattr(PyInstaller.utils.hooks, "collect_dynamic_libs", lambda *args: [c_api, required, other_dir])
    monkeypatch.setattr(importlib.metadata, "version", lambda name: "1.28.0")
    locked = runpy.run_path(str(hook))
    assert locked["binaries"] == [required, other_dir]
    assert locked["datas"] == [required]
    monkeypatch.setattr(importlib.metadata, "version", lambda name: "1.29.0")
    newer = runpy.run_path(str(hook))
    assert newer["binaries"] == [c_api, required, other_dir]
    assert newer["datas"] == [c_api, required]


def test_nsis_dictionary_is_set_before_install_macros() -> None:
    installer_hooks = (ROOT / "desktop" / "src-tauri" / "windows" / "installer-hooks.nsh").read_text(encoding="utf-8")
    first_macro = installer_hooks.index("!macro NSIS_HOOK_PREINSTALL")
    assert "\nSetCompressorDictSize 64\n" in installer_hooks[:first_macro]

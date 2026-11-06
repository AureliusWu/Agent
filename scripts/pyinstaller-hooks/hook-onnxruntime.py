"""Bundle the CPU ONNX Runtime used only when Faster Whisper VAD is enabled."""

from importlib.metadata import version
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs


# The generic collector walks optional quantization modules that require the
# separate `onnx` package.  VAD only needs this CPU inference path; enumerate
# it so a release build neither omits native code nor emits a misleading
# optional-dependency warning.
hiddenimports = [
    "onnxruntime",
    "onnxruntime.capi",
    "onnxruntime.capi._ld_preload",
    "onnxruntime.capi._pybind_state",
    "onnxruntime.capi.onnxruntime_inference_collection",
    "onnxruntime.capi.onnxruntime_validation",
]
omit_redundant_c_api = version("onnxruntime") == "1.28.0"
datas = collect_data_files(
    "onnxruntime", include_py_files=False,
    # PyInstaller can reclassify a DLL from datas into BINARY during Analysis.
    # Suppress this exact entry in both explicit collectors, not in bindepend.
    excludes=["capi/onnxruntime.dll"] if omit_redundant_c_api else None,
)
binaries = collect_dynamic_libs("onnxruntime")
if omit_redundant_c_api:
    # The locked Windows wheel embeds the runtime in the Python extension. No
    # bundled PE imports the separate C API DLL. Do not suppress auto-discovered
    # imports, omit the Python extension, or change another wheel's closure.
    binaries = [
        (source, destination)
        for source, destination in binaries
        if not (
            Path(source).name.casefold() == "onnxruntime.dll"
            and Path(destination).as_posix().casefold() == "onnxruntime/capi"
        )
    ]

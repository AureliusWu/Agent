"""Bundle the CPU ONNX Runtime used only when Faster Whisper VAD is enabled."""

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
datas = collect_data_files("onnxruntime", include_py_files=False)
binaries = collect_dynamic_libs("onnxruntime")

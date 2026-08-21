"""Collect Hugging Face Tokenizers' native extension for offline STT decoding."""

from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs, collect_submodules


hiddenimports = collect_submodules("tokenizers")
datas = collect_data_files("tokenizers", include_py_files=False)
binaries = collect_dynamic_libs("tokenizers")

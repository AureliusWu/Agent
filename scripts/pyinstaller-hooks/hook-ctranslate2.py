"""Keep CTranslate2's native runtime beside the frozen Faster Whisper worker."""

from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs, collect_submodules


hiddenimports = collect_submodules("ctranslate2")
datas = collect_data_files("ctranslate2", include_py_files=False)
binaries = collect_dynamic_libs("ctranslate2")

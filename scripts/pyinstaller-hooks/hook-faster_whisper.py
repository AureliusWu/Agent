"""Collect the offline Faster Whisper runtime used by the supervised STT worker.

The provider is imported lazily so that normal sidecar startup does not load
native STT dependencies.  PyInstaller therefore needs this explicit hook for
the worker-only modules and package data.
"""

from PyInstaller.utils.hooks import collect_data_files, collect_submodules


hiddenimports = collect_submodules("faster_whisper")
datas = collect_data_files("faster_whisper", include_py_files=False)

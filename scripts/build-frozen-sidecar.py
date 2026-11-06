"""Build the Windows sidecar with one shared, size-bounded packaging policy."""
from __future__ import annotations

import sys
from importlib.metadata import version
from typing import Sequence


VALIDATED_PYINSTALLER_VERSION = "6.21.0"
EXCLUDED_DEVELOPMENT_MODULES = ("fsspec.conftest", "pytest", "_pytest", "httpx._main")
# The optional HTTPX CLI pulls in these Pillow helpers through Pygments. Keep
# them available to document/image code even when that CLI is not bundled.
RETAINED_PILLOW_HELPERS = ("PIL.ImageDraw", "PIL.ImageDraw2", "PIL.ImagePath", "PIL.ImageText")


def main(arguments: Sequence[str]) -> int:
    if version("pyinstaller") != VALIDATED_PYINSTALLER_VERSION:
        raise SystemExit("Revalidate the frozen sidecar compression policy for the locked PyInstaller version.")

    from PyInstaller.__main__ import run
    from PyInstaller.archive.writers import ZlibArchiveWriter

    prior_compression_level = ZlibArchiveWriter._COMPRESSION_LEVEL
    if prior_compression_level != 6:
        raise SystemExit("Unexpected locked PyInstaller PYZ writer compression configuration.")

    policy_arguments = [arg for name in EXCLUDED_DEVELOPMENT_MODULES for arg in ("--exclude-module", name)]
    policy_arguments.extend(arg for name in RETAINED_PILLOW_HELPERS for arg in ("--hidden-import", name))
    # A zlib level-0 entry remains valid for the stock bootloader. Leave the
    # onedir layout, module finder, and the surrounding executable unchanged;
    # the outer installer can compress across the adjacent Python modules.
    ZlibArchiveWriter._COMPRESSION_LEVEL = 0
    try:
        run([*policy_arguments, *arguments])
    finally:
        ZlibArchiveWriter._COMPRESSION_LEVEL = prior_compression_level
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

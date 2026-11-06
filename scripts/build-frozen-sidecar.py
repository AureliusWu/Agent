"""Build the Windows sidecar with one shared, size-bounded packaging policy."""
from __future__ import annotations

import hashlib
import sys
from importlib.metadata import version
from pathlib import Path
from types import CodeType
from typing import Sequence


VALIDATED_PYINSTALLER_VERSION = "6.21.0"
VALIDATED_WINDOWS_NUMPY_VERSION = "2.5.1"
# NumPy's locked cp312 win_amd64 wheel includes the upstream CI account
# in show_config() metadata. Verify the exact module and remove only its
# four build-only path literals, not feature/library or runtime paths.
NUMPY_CONFIG_SOURCE_SHA256 = "029b248d71de70aa1d63b16dd87fee5e746a285f44ad46e85e8a49c93e63c6d8"
NUMPY_BUILD_PATHS = {
    "43a6c104c1617b920bcb8b2e07370993cf66bd1efea3112cc421c52ce4fb3231": "[wheel build include directory]",
    "77a99257eba9c1e297876363dec5df216506c112386a91c0445d9ab48b160f98": "[wheel build library directory]",
    "11b7bfa0743c63d8789fec1e93432677b71e35391663e853f293a9860cf36de5": "[wheel build pkg-config directory]",
    "2918c79f8461e8510593f769ef686a57529a6ee6d5f42df84a2cd3af0c027669": "[wheel build Python executable]",
}
EXCLUDED_DEVELOPMENT_MODULES = ("fsspec.conftest", "pytest", "_pytest", "httpx._main")
# The optional HTTPX CLI pulls in these Pillow helpers through Pygments. Keep
# them available to document/image code even when that CLI is not bundled.
RETAINED_PILLOW_HELPERS = ("PIL.ImageDraw", "PIL.ImageDraw2", "PIL.ImagePath", "PIL.ImageText")


def scrub_numpy_build_paths(source: Path, code: CodeType) -> CodeType:
    if hashlib.sha256(source.read_bytes()).hexdigest() != NUMPY_CONFIG_SOURCE_SHA256:
        raise SystemExit("Revalidate the exact NumPy Windows build metadata before bundling.")
    replaced = []
    constants = []
    for constant in code.co_consts:
        if type(constant) is str:
            identity = hashlib.sha256(constant.encode("utf-8")).hexdigest()
            if identity in NUMPY_BUILD_PATHS:
                replaced.append(identity)
                constant = NUMPY_BUILD_PATHS[identity]
        constants.append(constant)
    if sorted(replaced) != sorted(NUMPY_BUILD_PATHS):
        raise SystemExit("NumPy diagnostic build paths do not match the locked bytecode contract.")
    return code.replace(co_consts=tuple(constants))


def main(arguments: Sequence[str]) -> int:
    if version("pyinstaller") != VALIDATED_PYINSTALLER_VERSION:
        raise SystemExit("Revalidate the frozen sidecar compression policy for the locked PyInstaller version.")

    from PyInstaller.__main__ import run
    from PyInstaller.archive.writers import ZlibArchiveWriter

    prior_compression_level = ZlibArchiveWriter._COMPRESSION_LEVEL
    if prior_compression_level != 6:
        raise SystemExit("Unexpected locked PyInstaller PYZ writer compression configuration.")
    prior_entry_writer = vars(ZlibArchiveWriter).get("_write_entry")
    if not isinstance(prior_entry_writer, classmethod):
        raise SystemExit("Unexpected locked PyInstaller PYZ entry writer.")

    def write_entry(cls, stream, entry, code_dict):
        if sys.platform == "win32" and entry[0] == "numpy.__config__":
            if version("numpy") != VALIDATED_WINDOWS_NUMPY_VERSION:
                raise SystemExit("Revalidate the frozen NumPy Windows version.")
            code_dict = {**code_dict, entry[0]: scrub_numpy_build_paths(Path(entry[1]), code_dict[entry[0]])}
        return prior_entry_writer.__func__(cls, stream, entry, code_dict)

    policy_arguments = [arg for name in EXCLUDED_DEVELOPMENT_MODULES for arg in ("--exclude-module", name)]
    policy_arguments.extend(arg for name in RETAINED_PILLOW_HELPERS for arg in ("--hidden-import", name))
    # A zlib level-0 entry remains valid for the stock bootloader. Leave the
    # onedir layout, module finder, and the surrounding executable unchanged;
    # the outer installer can compress across the adjacent Python modules.
    ZlibArchiveWriter._COMPRESSION_LEVEL = 0
    ZlibArchiveWriter._write_entry = classmethod(write_entry)
    try:
        run([*policy_arguments, *arguments])
    finally:
        ZlibArchiveWriter._write_entry = prior_entry_writer
        ZlibArchiveWriter._COMPRESSION_LEVEL = prior_compression_level
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

# PyInstaller spec for Floe (SPEC §11). Build with:
#   pyinstaller packaging/Floe.spec --noconfirm
#
# Produces a onedir build under dist/Floe/, and on macOS also a Floe.app bundle
# under dist/Floe.app (the BUNDLE step is macOS-only so this spec still runs,
# for local sanity-checking, on Linux/Windows).

import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_dynamic_libs, collect_submodules

block_cipher = None

# Paths are relative to this spec file, regardless of the invoking cwd.
# `SPECPATH` is injected into the spec's namespace by PyInstaller itself.
SPEC_DIR = Path(SPECPATH).resolve()  # noqa: F821
ROOT_DIR = SPEC_DIR.parent
ENTRY_POINT = str(ROOT_DIR / "src" / "floe" / "__main__.py")

# --------------------------------------------------------------------------- version

try:
    sys.path.insert(0, str(ROOT_DIR / "src"))
    from floe._build_info import __version__ as _bundle_version  # type: ignore
except ImportError:
    _bundle_version = "0.0.0"

# --------------------------------------------------------------------------- data files

binaries = collect_dynamic_libs("duckdb")
hiddenimports = (
    collect_submodules("duckdb")
    + collect_submodules("pandas")
    + [
        "keyring.backends",
        "keyring.backends.macOS",
    ]
)

datas = []

# Test/dev-only packages the app never imports at runtime (grep of src/ confirms no
# `pyarrow` usage either: DuckDB's `.df()` needs only pandas/numpy, and nothing here
# calls `fetch_arrow_table()`/`.arrow()`, so pyarrow is excluded too).
excludes = [
    "pyarrow",
    "pyiceberg",
    "sqlalchemy",
    "pytest",
    "pytestqt",
    "_pytest",
    "ruff",
    "IPython",
    "matplotlib",
    "tkinter",
]

# DuckDB extensions are NOT passed to Analysis(datas=...): PyInstaller would reclassify
# these Mach-O files as BINARY and, on macOS, rewrite + re-codesign them (breaking the
# metadata footer DuckDB appends after the Mach-O image, or failing the build) and move
# them under Contents/Frameworks with dots in `v<version>` rewritten. They are appended
# to `a.datas` after Analysis instead (typecodes added there are trusted), so they stay
# byte-identical under Contents/Resources/duckdb_extensions/v<ver>/<platform>/, reached
# from `sys._MEIPASS` (Contents/Frameworks) through PyInstaller's directory symlink.
BUNDLED_EXTENSIONS_DIR = SPEC_DIR / "duckdb_extensions"
extension_datas = []
if BUNDLED_EXTENSIONS_DIR.is_dir():
    for _path in sorted(BUNDLED_EXTENSIONS_DIR.rglob("*")):
        if _path.is_file():
            _dest = Path("duckdb_extensions") / _path.relative_to(BUNDLED_EXTENSIONS_DIR)
            extension_datas.append((_dest.as_posix(), str(_path), "DATA"))

ICON_PATH = SPEC_DIR / "icon.icns"

# --------------------------------------------------------------------------- analysis

a = Analysis(
    [ENTRY_POINT],
    pathex=[str(ROOT_DIR / "src")],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
    cipher=block_cipher,
)

a.datas += extension_datas

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Floe",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(ICON_PATH) if ICON_PATH.exists() else None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="Floe",
)

# BUNDLE (the .app) only makes sense on macOS; guard it so this spec still runs
# (for local sanity checks) on Linux/Windows, producing just the onedir build above.
if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name="Floe.app",
        icon=str(ICON_PATH) if ICON_PATH.exists() else None,
        bundle_identifier="com.tcookie.floe",
        info_plist={
            "CFBundleIdentifier": "com.tcookie.floe",
            "CFBundleName": "Floe",
            "CFBundleShortVersionString": _bundle_version,
            "NSHighResolutionCapable": True,
            "LSMinimumSystemVersion": "12.0",
        },
    )

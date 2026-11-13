# -*- mode: python ; coding: utf-8 -*-
"""目录版打包。Chromium 不打进 EXE，运行机器仍需安装 Playwright 浏览器。"""

import os
import re

from PyInstaller.utils.hooks import collect_data_files, collect_submodules
from PyInstaller.utils.win32.versioninfo import (
    FixedFileInfo,
    StringFileInfo,
    StringStruct,
    StringTable,
    VarFileInfo,
    VarStruct,
    VSVersionInfo,
)

root = os.path.abspath(os.path.join(SPECPATH, ".."))
with open(os.path.join(root, "zhihu_collector", "__init__.py"), encoding="utf-8") as fh:
    app_version = re.search(r'__version__ = "([^"]+)"', fh.read()).group(1)
version_tuple = tuple(int(part) for part in app_version.split(".")) + (0,)
version_info = VSVersionInfo(
    ffi=FixedFileInfo(filevers=version_tuple, prodvers=version_tuple),
    kids=[
        StringFileInfo([
            StringTable("080404B0", [
                StringStruct("FileDescription", "知乎内容搜集器"),
                StringStruct("FileVersion", app_version),
                StringStruct("ProductName", "知乎内容搜集器"),
                StringStruct("ProductVersion", app_version),
                StringStruct("OriginalFilename", "知乎内容搜集器.exe"),
            ])
        ]),
        VarFileInfo([VarStruct("Translation", [0x0804, 1200])]),
    ],
)
hidden = collect_submodules("zhihu_collector")
datas = collect_data_files("playwright")

a = Analysis(
    [os.path.join(root, "zhihu_collector", "__main__.py")],
    pathex=[root],
    binaries=[],
    datas=datas,
    hiddenimports=hidden,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="知乎内容搜集器",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    version=version_info,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="知乎内容搜集器",
)

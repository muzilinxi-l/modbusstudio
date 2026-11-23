# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_all

datas = [('app.ico', '.')]
binaries = []
hiddenimports = [
    'modbusstudio',
    'modbusstudio.ui',
    'modbusstudio.ui.dialogs',
    'modbusstudio.services',
    'modbusstudio.models',
    'modbusstudio.ui.slave_panel',
    'modbusstudio.ui.poll_panel',
]
tmp_ret = collect_all('pymodbus')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tb_ret = collect_all('ttkbootstrap')
datas += tb_ret[0]; binaries += tb_ret[1]; hiddenimports += tb_ret[2]


a = Analysis(
    ['modbusstudio/__main__.py'],
    pathex=['.'],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        'setuptools',
        'wheel',
        'pip',
        'pytest',
        'pydoc',
        'doctest',
        'unittest',
        'tkinter.test',
        'pymodbus.repl',
    ],
    noarchive=False,
    optimize=1,
)
pyz = PYZ(a.pure)

# 目录版 (--onedir)：免去每次双击全量解压至 %TEMP%\_MEI 与杀软重复查杀，冷启动提速显著
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='ModbusStudio',
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
    icon=['app.ico'],
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='ModbusStudio',
)

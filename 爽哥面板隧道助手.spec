# -*- mode: python ; coding: utf-8 -*-
import os

# V2.3: 赞助二维码随 exe 打包(解包到 _MEIPASS/assets/, 由 sgtunnel.asset_path 第一档命中)
# V2.3: 图标改为原图直出(源: 用户提供的 图标设计需求.jpeg), 仍把 app.ico 设为 exe 文件图标
_spec_dir = globals().get("SPECPATH") or os.getcwd()
_datas = []
for _name in ("sponsor_qr.png", "app.ico", "app.png"):
    _src = os.path.join(_spec_dir, "assets", _name)
    if os.path.isfile(_src):
        _datas.append((_src, "assets"))

_icon = os.path.join(_spec_dir, "assets", "app.ico")
if not os.path.isfile(_icon):
    _icon = None


a = Analysis(
    ['sgtunnel.py'],
    pathex=[],
    binaries=[],
    datas=_datas,
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['PIL', 'PIL.Image', 'PIL.ImageTk', 'numpy'],   # V2.3: 保持零第三方依赖, 二维码走 tk 通道
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='爽哥面板隧道助手',
    icon=_icon,          # V2.3: exe 文件图标(资源管理器/任务栏)
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

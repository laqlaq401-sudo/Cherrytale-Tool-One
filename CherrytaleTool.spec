# -*- mode: python ; coding: utf-8 -*-

block_cipher = None

# 显式声明动态/反射导入的子模块，避免打包后缺少加密算法与原生窗口依赖
hidden_imports = [
    'pydantic',
    'pydantic_core',
    'cryptography',
    'cryptography.hazmat.primitives.ciphers',
    'cryptography.hazmat.primitives.ciphers.algorithms',
    'cryptography.hazmat.primitives.ciphers.modes',
    'truststore',
    'webview',
    'clr_loader',
    'pythonnet',
]

# 静态资源：
#  - web/            Web UI（打包即用）
#  - config_local.py 本机配置（含 vCode 密钥 GAME_V_CODE_KEY）
#    这个文件不入版本库（.gitignore 排除），但**发布产物必须有它** ——
#    否则 EXE 启动后 vCode 算不出来，登录直接被服务端拒（ABNORMAL_PACKET）。
#    缺失时不报错，交由运行时显式提示；下方 _resolve_config_local 负责探测。
import os as _os


def _resolve_config_local():
    """返回可打包的 config_local.py 数据项；不存在则返回空列表。

    同时尝试「项目根」与「上一级」两个位置 —— 后者兼容在 build/ 目录下
    直接调用 spec 的情形。
    """
    for candidate in (
        _os.path.join('.', 'config_local.py'),
        _os.path.join(_os.path.dirname(_os.path.abspath(SPECPATH)), 'config_local.py'),
    ):
        if _os.path.isfile(candidate):
            return [(candidate, '.')]
    print(
        '[WARN] 未找到 config_local.py —— 发布产物将无法生成 vCode，'
        '登录会被服务端拒绝。请先在本机创建该文件（见 config.py 注释）。'
    )
    return []


datas = [
    ('web', 'web'),
    *_resolve_config_local(),
]

# 坚决排除开发素材与巨型逆向目录（节省约 800MB 体积）
excludes = [
    'pytest',
    'tests',
    # protocol_samples/ 内嵌真实抓包字节（含脱敏前的登录报文结构），
    # 属**开发期验证资产**，不应进发布产物。tools/show_servers 的「离线退路」
    # 在打包版会失效 —— 这是刻意的取舍：发布产物里不携带任何抓包数据。
    'protocol_samples',
    'tools',
    'captures',
    'Cherrytale Asset',
    'Cherrytale IL2CPP',
    'tkinter',
    'unittest',
    'matplotlib',
    'scipy',
]

a = Analysis(
    ['main_gui.py'],
    pathex=['.'],
    binaries=[],
    datas=datas,
    hiddenimports=hidden_imports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

# 生成内测版可执行文件（无黑色命令行窗口）
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='CherrytaleTool',
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
    version='file_version_info.txt',
    # 应用图标：多尺寸 Windows ICO（由 tools/build_icons.py 从 assets/icon_source.png 生成）
    icon='assets/app.ico',
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='CherrytaleTool',
)


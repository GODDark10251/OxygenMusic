# -*- mode: python ; coding: utf-8 -*-

block_cipher = None

a = Analysis(
    ['oxygen_music.py'],
    pathex=[],
    binaries=[],
    datas=[
        ('oxygenmusic.svg', '.'),
    ],
    hiddenimports=['scipy.special.cython_special', 'numpy', 'yt_dlp'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='OxygenMusic',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,  # Hides the black terminal window on Windows for a clean GUI app experience
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_env=None,
)

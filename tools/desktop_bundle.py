#!/usr/bin/env python3
"""Create a local macOS development app around the compiled Tauri binary."""
from pathlib import Path
import plistlib
import os
import shutil
import sys

root = Path(__file__).resolve().parents[1]
if sys.platform != 'darwin':
    raise SystemExit('Use cargo run on this platform; this development bundle is macOS-only')
target = root / 'desktop/src-tauri/target/debug'
contents = target / 'Labradour.app/Contents'
(contents / 'MacOS').mkdir(parents=True, exist_ok=True)
# Replace the executable inode; overwriting an already launched signed Mach-O
# can leave macOS's code-signature cache rejecting subsequent launches.
staged = contents / 'MacOS/labradour-desktop.new'
shutil.copy2(target / 'labradour-desktop', staged)
os.replace(staged, contents / 'MacOS/labradour-desktop')
with (contents / 'Info.plist').open('wb') as stream:
    plistlib.dump({'CFBundleExecutable': 'labradour-desktop', 'CFBundleIdentifier': 'dev.labradour.desktop',
                  'CFBundleName': 'Labradour', 'CFBundleDisplayName': 'Labradour',
                  'CFBundlePackageType': 'APPL', 'CFBundleVersion': '0.1.0',
                  'CFBundleShortVersionString': '0.1.0', 'NSHighResolutionCapable': True}, stream)
print(contents.parent)

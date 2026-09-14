# -*- coding: utf-8 -*-
r"""NEO Scavenger - seed the current save into a NEO Save Manager slot.

Purpose: on a fresh install all three slots read "Empty Slot". This script writes
the current save into a slot ahead of time so the save is protected immediately.

Reproduces the batch behaviour:
    copy "%savedir%nsSGv1.sol" "%gamedir%\NSM\nsSGv1_g<slot>.sol"
    echo <label>> "%gamedir%\NSM\nsSGv1_g<slot>.ini"

Usage:
    python seed_slot.py                       # auto-detect paths, seed slot 1, auto label
    python seed_slot.py --slot 2
    python seed_slot.py --label "Chapter 3 - before the hospital"
    python seed_slot.py --game "D:\Games\NEO Scavenger" --saves "<save dir>"
    python seed_slot.py --dry-run             # check only, write nothing

Slot convention:
    0 = Quicksave slot (nsSGv1_g0.sol, menu entry R/S)
    1..3 = the three named slots (nsSGv1_g1..g3.sol, menu entries 1/2/3 and A/B/C)

Note: this script carries its own path detection (find_savedir / find_gamedir),
which overlaps nsg_paths.py. The standalone implementation is kept so this file
can be taken away and used on its own. If the two ever disagree,
**nsg_paths.py is authoritative**.
"""
import argparse
import hashlib
import os
import subprocess
import sys
import time
from nsg_i18n import tr

ANCHORS = ('nsTest.sol', 'nsSGv1.sol')          # anchor files used to locate the save dir
SAVE_NAME = 'nsSGv1.sol'
# cmd special characters: the label is wrapped by echo ... Game1 (%game1name%),
# so a ) would break the statement
FORBIDDEN = set('&|<>^%()')


def appdata_dir():
    r"""Resolve %APPDATA%.

    Note: Python launched from a sandboxed Bash shell may **lack the APPDATA
    environment variable**, so several fallbacks are required (measured on this
    machine: APPDATA is None under the Bash channel).
    """
    v = os.environ.get('APPDATA')
    if v and os.path.isdir(v):
        return v
    try:
        import ctypes
        buf = ctypes.create_unicode_buffer(260)
        ctypes.windll.shell32.SHGetFolderPathW(None, 0x001a, None, 0, buf)  # CSIDL_APPDATA
        if buf.value and os.path.isdir(buf.value):
            return buf.value
    except Exception:
        pass
    for base in (os.environ.get('USERPROFILE'), os.path.expanduser('~')):
        if base:
            cand = os.path.join(base, 'AppData', 'Roaming')
            if os.path.isdir(cand):
                return cand
    return None


def find_savedir(explicit=None):
    """Locate the Flash SharedObject save dir (match anchor files, never guess the random dir name)"""
    if explicit:
        return explicit if os.path.isdir(explicit) else None
    appdata = appdata_dir()
    if not appdata:
        return None
    base = os.path.join(appdata, r'Macromedia\Flash Player\#SharedObjects')
    if not os.path.isdir(base):
        return None
    hits = []
    for root, _dirs, files in os.walk(base):
        if any(a in files for a in ANCHORS):
            hits.append(root)
    if not hits:
        return None
    # prefer the one that holds the real save
    for h in hits:
        if os.path.isfile(os.path.join(h, SAVE_NAME)):
            return h
    return hits[0]


def find_gamedir(savedir):
    r"""Reverse-derive the game directory from the save directory.

    Flash's SharedObject path looks like:
        %APPDATA%\...\#SharedObjects\<random>\localhost\<game path without drive>\
    Note "without drive" — `C:` has been stripped, so the parts cannot simply be
    concatenated; every drive letter must be probed.
    e.g. ...\localhost\Program Files (x86)\Steam\steamapps\common\NEO Scavenger\NEOScavenger.exe
    """
    parts = savedir.split(os.sep)
    try:
        i = next(i for i, p in enumerate(parts) if p.startswith('#SharedObjects'))
    except StopIteration:
        return None
    rest = parts[i + 3:]                      # skip #SharedObjects\<random>\localhost
    if len(rest) < 2:
        return None
    exe_name = rest[-1]                       # last segment is the exe filename
    rel_dir = os.sep.join(rest[:-1])          # the rest is the exe's directory (no drive)
    for drive in 'CDEFGHIJKLMNOPQRSTUVWXYZAB':
        cand = drive + ':' + os.sep + rel_dir
        if os.path.isfile(os.path.join(cand, exe_name)):
            return cand
    return None


def sha256(path):
    with open(path, 'rb') as f:
        return hashlib.sha256(f.read()).hexdigest()


def validate_label(label):
    bad = sorted(FORBIDDEN & set(label))
    if bad:
        raise SystemExit(tr('The label contains cmd special character(s) %s, which breaks the batch echo statement; please change it') % ''.join(bad))
    try:
        label.encode('gbk')
    except UnicodeEncodeError:
        raise SystemExit(tr('The label contains characters that GBK cannot represent; the console will show mojibake'))


def game_running():
    try:
        r = subprocess.run(
            ['tasklist', '/FI', 'IMAGENAME eq NEOScavenger.exe', '/FO', 'CSV', '/NH'],
            capture_output=True, text=True, errors='replace')
        return 'NEOScavenger.exe' in (r.stdout or '')
    except Exception:
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--slot', type=int, default=1, choices=[0, 1, 2, 3])
    ap.add_argument('--label', default=None, help=tr('slot label; defaults to "Save YYYY-MM-DD HH:MM"'))
    ap.add_argument('--game', default=None, help=tr('game directory (overrides auto-detection)'))
    ap.add_argument('--saves', default=None, help=tr('save directory (overrides auto-detection)'))
    ap.add_argument('--dry-run', action='store_true')
    a = ap.parse_args()

    print(tr(' === 1. Locate directories ==='))
    savedir = find_savedir(a.saves)
    if not savedir:
        raise SystemExit(tr('Could not locate the save directory; specify it with --saves'))
    gamedir = a.game or find_gamedir(savedir)
    if not gamedir or not os.path.isdir(gamedir):
        raise SystemExit(tr('Could not locate the game directory (derived %r); specify it with --game') % gamedir)
    print(tr('   save dir    :'), savedir)
    print(tr('   game dir    :'), gamedir)

    src = os.path.join(savedir, SAVE_NAME)
    if not os.path.isfile(src):
        raise SystemExit(tr('Current save not found: %s (did the game "Quit and Save"?)') % src)
    nsm = os.path.join(gamedir, 'NSM')
    if not os.path.isdir(nsm):
        raise SystemExit(tr('NSM directory does not exist: %s (is NEO Save Manager installed?)') % nsm)

    print()
    print(tr(' === 2. Prerequisites ==='))
    st = os.stat(src)
    print(tr('   source save : %d B  mtime %s') % (
        st.st_size, time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(st.st_mtime))))
    running = game_running()
    print(tr('   game process: %s') % (tr('running ⚠') if running else tr('not running ✓') if running is False else tr('detection failed')))
    if running:
        print(tr('     !! Fully quit the game first, otherwise you may copy a half-written save'))

    dst = os.path.join(nsm, 'nsSGv1_g%d.sol' % a.slot)
    lbl = os.path.join(nsm, 'nsSGv1_g%d.ini' % a.slot)
    print(tr('   target slot :'), os.path.basename(dst))
    for p in (dst, lbl):
        print('    %-22s %s' % (os.path.basename(p),
              (tr('exists, %d B (will be overwritten)') % os.path.getsize(p)) if os.path.exists(p) else tr('missing')))

    label = a.label or time.strftime(tr('Save %Y-%m-%d %H:%M'), time.localtime(st.st_mtime))
    validate_label(label)
    print(tr('   label       : %r  -> GBK %d bytes') % (label, len(label.encode('gbk'))))

    if a.dry_run:
        print()
        print(tr(' --dry-run: nothing was written'))
        return

    print()
    print(tr(' === 3. Seeding ==='))
    src_h = sha256(src)
    with open(src, 'rb') as f:
        data = f.read()
    with open(dst, 'wb') as f:
        f.write(data)
    dst_h = sha256(dst)
    print(tr('   source SHA256 :'), src_h)
    print(tr('   slot SHA256 :'), dst_h)
    print(tr('   hash match  :'), src_h == dst_h)
    if src_h != dst_h:
        raise SystemExit(tr('hash mismatch, copy failed'))

    with open(lbl, 'wb') as f:
        f.write((label + '\r\n').encode('gbk'))
    raw = open(lbl, 'rb').read()
    print('  %s = %r  (BOM=%s)' % (os.path.basename(lbl), raw, raw[:3] == b'\xef\xbb\xbf'))

    print()
    print(tr(' === 4. Final contents of the NSM directory ==='))
    for fn in sorted(os.listdir(nsm)):
        print('  %8d B  %s' % (os.path.getsize(os.path.join(nsm, fn)), fn))
    print()
    print(tr('Done. Launch NEOSaveManager.bat and press %d to load from this slot.') % a.slot)


if __name__ == '__main__':
    main()

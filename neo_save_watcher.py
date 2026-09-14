#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""NEO Scavenger save auto-backup watcher

Run resident by a Windows scheduled task at logon with a hidden window
(pythonw.exe).

Responsibilities:
  1. NEOScavenger.exe process starts   -> snapshot the current save at once (reason=start)
  2. nsSGv1.sol content changes        -> snapshot at once (reason=save, i.e. in-game Quit and Save)
  3. Optional: mirror the newest snapshot into NEO Save Manager's Quicksave slot (nsSGv1_g0.sol)
  4. Prune old snapshots using a time-layered retention policy

Design notes:
  · Backups land outside the game directory (under the user's Documents), so the
    in-game D) / W) menu entries cannot delete them
  · Every read goes through read_stable(): two reads compared on mtime/size/content,
    so a half-written file is never captured while the game is saving
  · No third-party dependencies (no psutil); process detection uses a Win32
    Toolhelp32 snapshot
  · Pure polling every POLL_SEC seconds; it costs almost nothing while sleeping
  · Paths are not hard-coded: env vars -> config.json -> auto-detection (see nsg_paths.py)

Usage:
  pythonw.exe neo_save_watcher.py            # resident (what the scheduled task uses)
  python    neo_save_watcher.py --once       # one cycle only, handy for testing
  python    neo_save_watcher.py --once --dry-run
  python    neo_save_watcher.py --check      # print path resolution + probe, then exit
  python    neo_save_watcher.py --config D:\my\config.json
"""

import argparse
import ctypes
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime

# ---------------------------------------------------------------- path resolution
# Paths are no longer hard-coded: nsg_paths resolves them in the order
# [environment variable -> config.json -> auto-detection].
# This must happen at module level, because STATE_FILE / LOG_FILE below are
# derived directly from it.
import nsg_paths
from nsg_i18n import tr

# Scan for --config by hand first (argparse has not run yet; see
# nsg_paths.cli_config_arg for the rationale)
_CONFIG_ARG = nsg_paths.cli_config_arg()
PATHS = nsg_paths.resolve(_CONFIG_ARG)
_CFG = nsg_paths.load_config(_CONFIG_ARG)

SAVE_DIR = PATHS['save_dir']          # may be None; main() reports it readably
GAME_DIR = PATHS['game_dir']          # may be None (only affects NSM slot sync)
NSM_DIR = PATHS['nsm_dir']
BACKUP_ROOT = PATHS['backup_root']

SAVE_NAME = nsg_paths.SAVE_NAME
GAME_EXE = nsg_paths.GAME_EXE

# Runtime parameters: use config.json when it provides them, else these defaults
POLL_SEC = int(_CFG.get('poll_sec', 10))                     # poll interval (seconds)
REFRESH_G0 = bool(_CFG.get('refresh_g0', True))              # mirror into NSM's Quicksave slot
SNAP_ON_GAME_START = bool(_CFG.get('snap_on_game_start', True))
LAUNCH_GUI_ON_GAME_START = bool(_CFG.get('launch_gui_on_game_start', True))
LOG_MAX_BYTES = 1 << 20  # log rotation threshold (1 MiB)

HERE = os.path.dirname(os.path.abspath(__file__))
# The rollback GUI ships next to this script. The legacy Chinese filename is
# still accepted so that deployments migrated from v1.0.0 keep working.
GUI_SCRIPT = os.path.join(HERE, 'save_rollback_gui.pyw')
if not os.path.isfile(GUI_SCRIPT):
    GUI_SCRIPT = os.path.join(HERE, '存档回档器.pyw')

# --- Cooldown: disabled by default (0)
# It used to be 30 seconds, but measurement showed the design is harmful: a save
# change falling inside the cooldown defers the snapshot, and "death" deletes the
# save file inside that deferral window -- so that state is lost permanently.
# Measured evidence: 198,511 B detected at 17:02:04, deferred because only 21 s
# had passed since the last snapshot (< 30 s); the file was gone 10 s later and
# that state could never be recovered.
# Retention is now time-layered with a hard total cap, so dropping the cooldown
# carries no disk-usage risk.
MIN_SNAPSHOT_GAP_SEC = 0

# --- Time-layered retention (more intuitive than "keep the last N files")
KEEP_ALL_SEC = 2 * 3600      # last 2 hours: keep every snapshot
HOURLY_UNTIL_SEC = 48 * 3600  # 2h ~ 48h: keep one per hour
DAILY_AFTER_SEC = 48 * 3600   # beyond 48h: keep one per day
MAX_TOTAL_FILES = 600         # hard cap: number of files
MAX_TOTAL_MB = 300            # hard cap: total size (MB)

STATE_FILE = os.path.join(BACKUP_ROOT, 'watcher_state.json')
LOG_FILE = os.path.join(BACKUP_ROOT, 'watcher.log')


# ---------------------------------------------------------------- utilities
if sys.stdout is None:                       # stdout is None under pythonw.exe
    sys.stdout = open(os.devnull, 'w')
if sys.stderr is None:
    sys.stderr = open(os.devnull, 'w')


def log(msg):
    line = '[%s] %s' % (datetime.now().strftime('%Y-%m-%d %H:%M:%S'), msg)
    try:
        os.makedirs(BACKUP_ROOT, exist_ok=True)
        if os.path.isfile(LOG_FILE) and os.path.getsize(LOG_FILE) > LOG_MAX_BYTES:
            bak = LOG_FILE + '.1'
            if os.path.exists(bak):
                os.remove(bak)
            os.replace(LOG_FILE, bak)
        with open(LOG_FILE, 'a', encoding='utf-8') as f:
            f.write(line + '\n')
    except Exception:
        pass
    print(line, flush=True)


def sha256_bytes(b):
    return hashlib.sha256(b).hexdigest()


def read_stable(path, tries=6, delay=0.25):
    r"""Stabilised read: return only when two consecutive reads agree on
    (mtime, size, content).

    While the game is running Flash may be writing nsSGv1.sol, so a plain read
    risks capturing a truncated file. Returns None on failure -- it is better to
    skip a cycle than to write a corrupt snapshot.
    """
    for _ in range(tries):
        try:
            s1 = os.stat(path)
            with open(path, 'rb') as f:
                d1 = f.read()
            time.sleep(delay)
            s2 = os.stat(path)
            with open(path, 'rb') as f:
                d2 = f.read()
        except FileNotFoundError:
            return None
        except PermissionError:
            time.sleep(delay)
            continue
        if (s1.st_mtime == s2.st_mtime and s1.st_size == s2.st_size
                and len(d1) == len(d2) and d1 == d2):
            return d2
    log(tr('   !! read_stable failed (file keeps changing), skipping this cycle: %s') % path)
    return None


# ---------------------------------------------------------------- process detection
TH32CS_SNAPPROCESS = 0x00000002


class PROCESSENTRY32(ctypes.Structure):
    _fields_ = [
        ('dwSize', ctypes.c_ulong),
        ('cntUsage', ctypes.c_ulong),
        ('th32ProcessID', ctypes.c_ulong),
        ('th32DefaultHeapID', ctypes.c_size_t),   # ULONG_PTR
        ('th32ModuleID', ctypes.c_ulong),
        ('cntThreads', ctypes.c_ulong),
        ('th32ParentProcessID', ctypes.c_ulong),
        ('pcPriClassBase', ctypes.c_long),
        ('dwFlags', ctypes.c_ulong),
        ('szExeFile', ctypes.c_char * 260),
    ]


def process_running(exe_name=GAME_EXE):
    """Detect a process via a Toolhelp32 snapshot, avoiding repeated tasklist spawns"""
    k32 = ctypes.windll.kernel32
    snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snap == -1 or snap == 0xFFFFFFFF:
        return None
    try:
        entry = PROCESSENTRY32()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32)
        ok = k32.Process32First(snap, ctypes.byref(entry))
        while ok:
            name = entry.szExeFile.decode('mbcs', errors='replace')
            if name.lower() == exe_name.lower():
                return True
            ok = k32.Process32Next(snap, ctypes.byref(entry))
        return False
    finally:
        k32.CloseHandle(snap)


# ---------------------------------------------------------------- launching the GUI
def _pythonw():
    r"""Pick a console-less interpreter to run the GUI.

    This script is hosted by pythonw.exe through the scheduled task, so
    sys.executable is normally already pythonw.exe. But if someone runs it by
    hand with python.exe, switch to the sibling pythonw.exe -- otherwise a
    console window pops up as a side effect. Note that this must be the
    *host* Python (the one with tkinter); WorkBuddy's managed Python inside the
    sandbox has no tkinter.
    """
    exe = sys.executable or ''
    d, n = os.path.split(exe)
    if n.lower() == 'python.exe':
        cand = os.path.join(d, 'pythonw.exe')
        if os.path.isfile(cand):
            return cand
    return exe


def launch_gui():
    r"""Bring up the rollback GUI. Single-instance protection lives on the GUI
    side (a named mutex), so this can spawn unconditionally -- when it is already
    running the existing window is focused instead.
    """
    if not os.path.isfile(GUI_SCRIPT):
        log(tr('   !! Cannot launch the GUI: script not found %s') % GUI_SCRIPT)
        return False
    exe = _pythonw()
    if not os.path.isfile(exe):
        log(tr('   !! Cannot launch the GUI: interpreter not found %s') % exe)
        return False
    try:
        DETACHED_PROCESS = 0x00000008
        CREATE_NEW_PROCESS_GROUP = 0x00000200
        subprocess.Popen([exe, GUI_SCRIPT],
                         creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP,
                         close_fds=True)
        log(tr('   ~~ Rollback GUI launched (%s)') % os.path.basename(exe))
        return True
    except Exception as e:
        log(tr('   !! Failed to launch the GUI: %r') % e)
        return False


# ---------------------------------------------------------------- state
def load_state():
    try:
        with open(STATE_FILE, encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {'last_hash': None, 'game_was_running': None}


def save_state(st):
    try:
        os.makedirs(BACKUP_ROOT, exist_ok=True)
        with open(STATE_FILE, 'w', encoding='utf-8') as f:
            json.dump(st, f, ensure_ascii=False, indent=1)
    except Exception as e:
        log(tr('   !! Failed to write state: %s') % e)


# ---------------------------------------------------------------- backup
def snapshot(data, reason, dry_run=False):
    """Write one timestamped snapshot and return its path"""
    os.makedirs(BACKUP_ROOT, exist_ok=True)
    ts = datetime.now().strftime('%Y%m%d-%H%M%S')
    dst = os.path.join(BACKUP_ROOT, 'nsSGv1_%s_%s.sol' % (ts, reason))
    if os.path.exists(dst):
        dst = os.path.join(BACKUP_ROOT, 'nsSGv1_%s_%s_%d.sol' % (ts, reason, os.getpid()))
    if dry_run:
        log(tr('   [dry-run] would write snapshot %s (%d B)') % (os.path.basename(dst), len(data)))
        return dst
    with open(dst, 'wb') as f:
        f.write(data)
    log(tr('   >> snapshot %s  (%d B, %s)') % (os.path.basename(dst), len(data), reason))
    return dst


def prune(dry_run=False):
    r"""Clean up snapshots inside auto\ and never touch any other directory.

    Time-layered retention:
      within KEEP_ALL_SEC                  -- keep every snapshot
      KEEP_ALL_SEC ~ HOURLY_UNTIL_SEC      -- keep one per hour
      beyond DAILY_AFTER_SEC               -- keep one per day
    Then a hard cap layer (MAX_TOTAL_FILES / MAX_TOTAL_MB) drops the oldest
    entries when exceeded.
    """
    try:
        names = [n for n in os.listdir(BACKUP_ROOT)
                 if n.startswith('nsSGv1_') and n.endswith('.sol')]
    except FileNotFoundError:
        return
    if not names:
        return

    def ts_of(n):
        m = re.match(r'nsSGv1_(\d{8})-(\d{6})_', n)
        if not m:
            return 0
        try:
            return time.mktime(time.strptime(m.group(1) + m.group(2), '%Y%m%d%H%M%S'))
        except Exception:
            return 0

    entries = sorted(((ts_of(n), n) for n in names), reverse=True)
    now = time.time()
    keep = set()
    seen_bucket = set()
    for ts, n in entries:
        age = now - ts if ts else 1 << 40
        if age <= KEEP_ALL_SEC:
            keep.add(n)
            continue
        if age <= HOURLY_UNTIL_SEC:
            bucket = ('h', int(ts // 3600))
        else:
            bucket = ('d', int(ts // 86400))
        if bucket not in seen_bucket:
            seen_bucket.add(bucket)
            keep.add(n)

    # Hard cap: first by file count
    ordered = [n for _ts, n in entries]
    if len(keep) > MAX_TOTAL_FILES:
        newest_first = [n for n in ordered if n in keep]
        keep = set(newest_first[:MAX_TOTAL_FILES])
    # ...then by total size
    size_of = lambda n: os.path.getsize(os.path.join(BACKUP_ROOT, n))
    total = sum(size_of(n) for n in keep)
    limit = MAX_TOTAL_MB * 1024 * 1024
    if total > limit:
        for n in [x for x in ordered if x in keep][::-1]:   # drop oldest first
            if total <= limit:
                break
            keep.discard(n)
            total -= size_of(n)

    removed = 0
    for n in names:
        if n in keep:
            continue
        p = os.path.join(BACKUP_ROOT, n)
        if dry_run:
            log(tr('   [dry-run] would delete old snapshot %s') % n)
        else:
            try:
                os.remove(p)
                removed += 1
            except Exception as e:
                log(tr('   !! Prune failed %s: %s') % (n, e))
    if removed:
        log(tr('   -- pruned %d old snapshot(s), %d kept') % (removed, len(keep)))


def newest_snapshot_hash():
    r"""Hash of the newest snapshot in auto\, used to de-duplicate across restarts"""
    try:
        names = sorted([n for n in os.listdir(BACKUP_ROOT)
                        if n.startswith('nsSGv1_') and n.endswith('.sol')], reverse=True)
    except FileNotFoundError:
        return None
    for n in names[:3]:
        try:
            with open(os.path.join(BACKUP_ROOT, n), 'rb') as f:
                return sha256_bytes(f.read())
        except Exception:
            continue
    return None


def refresh_quicksave(snapshot_path, dry_run=False):
    """Mirror the newest snapshot into NEO Save Manager's Quicksave slot (g0)"""
    if not REFRESH_G0:
        return
    if not NSM_DIR:
        return                     # game dir not detected; skip (non-fatal)
    nsm = NSM_DIR
    if not os.path.isdir(nsm):
        return
    dst = os.path.join(nsm, 'nsSGv1_g0.sol')
    if dry_run:
        log(tr('   [dry-run] would refresh the Quicksave slot -> %s') % dst)
        return
    try:
        with open(snapshot_path, 'rb') as s, open(dst, 'wb') as d:
            d.write(s.read())
        log(tr('   ~~ Quicksave slot refreshed (g0)'))
    except Exception as e:
        log(tr('   !! Failed to refresh the Quicksave slot: %s') % e)


# ---------------------------------------------------------------- main loop
def cycle(st, dry_run=False):
    """One polling cycle"""
    src = os.path.join(SAVE_DIR, SAVE_NAME)
    new_snapshot = None

    running = process_running()
    if running is None:
        log(tr('   !! Process detection failed, skipping process logic'))

    # De-duplicate across restarts: fall back to the hash of the newest snapshot
    baseline = st.get('last_hash') or newest_snapshot_hash()

    # --- 1) Game just started -> bring up the GUI + snapshot
    # Judge on the state *transition*, not on "is it running now":
    # st['game_was_running'] is only updated at the end of each cycle, so one
    # launch matches exactly one cycle and cannot spawn repeatedly.
    if running and st.get('game_was_running') is False:
        if LAUNCH_GUI_ON_GAME_START and not dry_run:
            launch_gui()
        if SNAP_ON_GAME_START:
            data = read_stable(src)
            if data:
                h = sha256_bytes(data)
                if h != baseline:
                    log(tr('Game start detected -> snapshot the current save'))
                    new_snapshot = snapshot(data, 'start', dry_run)
                    st['last_hash'] = h
                    st['last_snapshot_ts'] = time.time()
                else:
                    log(tr('Game start detected, but the save matches the last snapshot; skipping'))

    # --- 2) Save content changed -> snapshot
    # Cooldown is off by default: deferring a snapshot loses that state for good
    # in the "death deletes the save" scenario -- see the constant above.
    data = read_stable(src)
    if data is None:
        # Log once per transition only; otherwise a missing save spams the log
        # every 10 seconds.
        if not st.get('save_missing'):
            log(tr(' !! Save is unreadable or missing (the game may have deleted it)'))
            st['save_missing'] = True
    else:
        if st.get('save_missing'):
            log(tr('Save reappeared'))
            st['save_missing'] = False
        h = sha256_bytes(data)
        baseline = st.get('last_hash') or newest_snapshot_hash()
        if h != baseline:
            since = time.time() - st.get('last_snapshot_ts', 0)
            if MIN_SNAPSHOT_GAP_SEC and since < MIN_SNAPSHOT_GAP_SEC:
                # Do not update last_hash; retry next cycle. Defer only, never drop.
                log(tr('Save changed (%d B) only %.0fs after the last snapshot < cooldown %ds, deferring this cycle')
                    % (len(data), since, MIN_SNAPSHOT_GAP_SEC))
            else:
                log(tr('Save changed (%d B) -> snapshot') % len(data))
                p = snapshot(data, 'save', dry_run)
                new_snapshot = p or new_snapshot
                st['last_hash'] = h
                st['last_snapshot_ts'] = time.time()

    if new_snapshot and not dry_run:
        refresh_quicksave(new_snapshot)
        prune(dry_run=False)

    if running is not None:
        st['game_was_running'] = running
    return st


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--once', action='store_true', help=tr('run one cycle then exit'))
    ap.add_argument('--dry-run', action='store_true', help=tr('write nothing'))
    ap.add_argument('--check', action='store_true', help=tr('print the environment probe results and exit'))
    ap.add_argument('--config', metavar='FILE', help=tr('path to config.json'))
    ap.add_argument('--interval', type=int, default=POLL_SEC)
    a = ap.parse_args()

    if a.check:
        print(nsg_paths.describe(PATHS))
        print(tr(' === Directories and processes ==='))
        print(tr('save dir exists   :'), bool(SAVE_DIR) and os.path.isdir(SAVE_DIR))
        print(tr('save file exists  :'), bool(SAVE_DIR) and os.path.isfile(
            os.path.join(SAVE_DIR, SAVE_NAME)) if SAVE_DIR else False)
        print(tr('game dir exists   :'), bool(GAME_DIR) and os.path.isdir(GAME_DIR))
        print(tr('NSM dir exists    :'), bool(NSM_DIR) and os.path.isdir(NSM_DIR))
        print(tr('backup root       :'), BACKUP_ROOT, '->',
              tr('exists') if os.path.isdir(BACKUP_ROOT) else tr('to be created'))
        print(tr('game running      :'), process_running())
        if SAVE_DIR:
            p = os.path.join(SAVE_DIR, SAVE_NAME)
            if os.path.isfile(p):
                s = os.stat(p)
                print(tr('current save      : %d B  %s') % (
                    s.st_size,
                    datetime.fromtimestamp(s.st_mtime).strftime('%Y-%m-%d %H:%M:%S')))
        return

    if not SAVE_DIR:
        print(tr(' !! Could not locate the save directory, cannot continue.'))
        print(nsg_paths.describe(PATHS))
        print(tr('\nEdit config.json and set save_dir explicitly, for example:'))
        print('  {')
        print(tr('    "save_dir": "C:\\\\Users\\\\<you>\\\\AppData\\\\Roaming\\\\Macromedia\\\\Flash Player\\\\#SharedObjects\\\\XXXXXXXX\\\\localhost\\\\Program Files (x86)\\\\Steam\\\\steamapps\\\\common\\\\NEO Scavenger\\\\NEOScavenger.exe"'))
        print('  }')
        sys.exit(2)

    st = load_state()
    if a.once:
        cycle(st, a.dry_run)
        if not a.dry_run:
            save_state(st)
        return

    log(tr(' === Watcher started (interval=%ds, cooldown=%ds, retention: all within 2h / hourly up to 48h / daily after, g0=%s, launchGUI=%s) ===')
        % (a.interval, MIN_SNAPSHOT_GAP_SEC, REFRESH_G0, LAUNCH_GUI_ON_GAME_START))
    log(tr('     save dir  : %s [%s]') % (SAVE_DIR, PATHS['source'].get('save_dir')))
    log(tr('     backup dir: %s [%s]') % (BACKUP_ROOT, PATHS['source'].get('backup_root')))
    st = cycle(st, False)          # do one right away on startup
    save_state(st)
    while True:
        try:
            time.sleep(a.interval)
            st = cycle(st, False)
            save_state(st)
        except KeyboardInterrupt:
            log(tr('Interrupted, exiting'))
            return
        except Exception as e:
            log(tr(' !! Cycle error: %r') % e)
            time.sleep(a.interval)


if __name__ == '__main__':
    main()

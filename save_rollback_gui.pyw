#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""NEO Scavenger save rollback tool (GUI)

Features:
  · List every snapshot in auto\ (time / size / trigger reason)
  · One click to roll any snapshot back over the current save (nsSGv1.sol)
  · Optionally write a snapshot into a NEO Save Manager named slot (g1/g2/g3)
    or the Quicksave slot (g0)
  · Open the backup folder, inspect watcher status

Safety design (important):
  1. If the game is running, a rollback is refused. Flash keeps the save in
     memory and overwrites nsSGv1.sol when the game exits, so your rollback
     would be wiped. The UI offers an "end the game process" button and only
     continues after confirmation.
  2. **The current save is copied aside first** (reason=prerestore), so the
     rollback itself can be reverted.
  3. A SHA-256 check runs after writing; a mismatch is reported as an error.

Command-line mode (for scripting / automation, no GUI):
  save_rollback_gui.pyw --list
  save_rollback_gui.pyw --restore <filename|latest> [--yes]
  save_rollback_gui.pyw --restore-slot 2 [--snapshot <filename|latest>] [--label TEXT]
  save_rollback_gui.pyw --status

Double-clicking it (no arguments) opens the graphical interface.
"""

import argparse
import ctypes
import glob
import hashlib
import os
import re
import subprocess
import sys
import time
from datetime import datetime

# ---------------------------------------------------------------- path resolution
# Shares the watcher's resolution chain (env vars -> config.json -> auto-detection)
# so the logic is not duplicated in two places.
import nsg_paths
from nsg_i18n import tr

_CONFIG_ARG = nsg_paths.cli_config_arg()
PATHS = nsg_paths.resolve(_CONFIG_ARG)
GAME_DIR = PATHS['game_dir']
NSM_DIR = PATHS['nsm_dir']
SAVE_DIR = PATHS['save_dir']
BACKUP_ROOT = PATHS['backup_root']
SAVE_NAME = nsg_paths.SAVE_NAME
GAME_EXE = nsg_paths.GAME_EXE
TASK_NAME = 'NEO Scavenger 存档自动备份'
APP_TITLE = tr('NEO Scavenger Save Rollback')
MUTEX_NAME = 'NEOScavengerSaveRollbackGUI'

# ---------------------------------------------------------------- single instance
# The watcher brings this GUI up whenever it detects a game launch. Without
# single-instance protection every launch would add another window and the
# screen would fill up quickly.
_MUTEX_HANDLE = None


def try_acquire_single_instance():
    r"""True = this process is the only instance; False = one is already running.

    Uses a named kernel object (mutex) rather than scanning the process list:
    Toolhelp32 only exposes the exe name (pythonw.exe), not the script name, so a
    scan cannot tell this GUI apart from any other Python script.
    """
    global _MUTEX_HANDLE
    try:
        k32 = ctypes.WinDLL('kernel32', use_last_error=True)
        _MUTEX_HANDLE = k32.CreateMutexW(None, False, MUTEX_NAME)
        err = ctypes.get_last_error()
        if not _MUTEX_HANDLE:
            return True                      # fail open rather than blocking the user
        return err != 183                    # ERROR_ALREADY_EXISTS = 183
    except Exception:
        return True


def focus_existing_window():
    r"""Bring an already-running rollback window to the foreground"""
    try:
        u32 = ctypes.windll.user32
        hwnd = u32.FindWindowW(None, APP_TITLE)
        if hwnd:
            u32.ShowWindow(hwnd, 9)          # SW_RESTORE
            u32.SetForegroundWindow(hwnd)
            return True
    except Exception:
        pass
    return False

# ---------------------------------------------------------------- basic utilities


def sha256_bytes(b):
    return hashlib.sha256(b).hexdigest()


def sol_integrity(path):
    r"""Validate the .sol structure. Returns (ok, explanation).

    Measured SOL header layout (cross-checked over 14 snapshots at 2 points in time):
      offset 0: 2-byte magic 0x00 0xBF
      offset 2: 4-byte big-endian unsigned int = length of the data that follows
      offset 6: start of the payload ("TCSO" etc.)
    Therefore the length field always equals filesize - 6.
    Measured: 200996 bytes -> 0x0003111E = 200990; 285198 bytes -> 0x00045A08 = 285192.
    If that does not hold, the file is truncated or corrupt -- the game cannot read
    such a snapshot, so it must be blocked *before* the rollback rather than
    discovered later in game.
    """
    try:
        size = os.path.getsize(path)
        with open(path, 'rb') as f:
            h = f.read(6)
        if len(h) < 6:
            return False, tr('file too small (<6 bytes)')
        if h[:2] != b'\x00\xbf':
            return False, tr('bad magic (expected 00 bf, got %s)') % h[:2].hex(' ')
        n = int.from_bytes(h[2:6], 'big')
        if n == size - 6:
            return True, tr('Structure')
        return False, tr('length field %d != filesize-6 = %d (likely truncated)') % (n, size - 6)
    except Exception as e:
        return False, tr('read failed: %s') % e


def read_stable(path, tries=6, delay=0.2):
    r"""Return the content only when two consecutive reads agree; else None
    (so a half-written file is never captured)"""
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
    return None


def list_snapshots():
    """Return [(mtime, timestamp string, size, reason, full path, filename)], newest first"""
    out = []
    for p in glob.glob(os.path.join(BACKUP_ROOT, 'nsSGv1_*.sol')):
        n = os.path.basename(p)
        m = re.match(r'nsSGv1_(\d{8})-(\d{6})_(.+)\.sol$', n)
        if not m:
            continue
        try:
            dt = datetime.strptime(m.group(1) + m.group(2), '%Y%m%d%H%M%S')
        except ValueError:
            continue
        out.append((os.path.getmtime(p), dt.strftime('%Y-%m-%d %H:%M:%S'),
                    os.path.getsize(p), m.group(3), p, n))
    out.sort(key=lambda x: x[1], reverse=True)
    return out


TASK_PROCS = None


def process_running(exe_name=GAME_EXE):
    k32 = ctypes.windll.kernel32
    snap = k32.CreateToolhelp32Snapshot(0x2, 0)
    if not snap or snap == 0xFFFFFFFF:
        return None

    class PE32(ctypes.Structure):
        _fields_ = [('dwSize', ctypes.c_ulong), ('cntUsage', ctypes.c_ulong),
                    ('th32ProcessID', ctypes.c_ulong), ('th32DefaultHeapID', ctypes.c_size_t),
                    ('th32ModuleID', ctypes.c_ulong), ('cntThreads', ctypes.c_ulong),
                    ('th32ParentProcessID', ctypes.c_ulong), ('pcPriClassBase', ctypes.c_long),
                    ('dwFlags', ctypes.c_ulong), ('szExeFile', ctypes.c_char * 260)]
    try:
        e = PE32()
        e.dwSize = ctypes.sizeof(PE32)
        ok = k32.Process32First(snap, ctypes.byref(e))
        while ok:
            # Match on the exe name; never parse wmic text output.
            if e.szExeFile.decode('mbcs', errors='replace').lower() == exe_name.lower():
                return True
            ok = k32.Process32Next(snap, ctypes.byref(e))
        return False
    finally:
        k32.CloseHandle(snap)


def kill_game():
    try:
        r = subprocess.run(['taskkill', '/IM', GAME_EXE, '/F'],
                           capture_output=True, text=True, errors='replace')
        return r.returncode == 0
    except Exception:
        return False


def task_status():
    """Return (exists, short status text)"""
    try:
        r = subprocess.run(['schtasks', '/Query', '/TN', TASK_NAME, '/FO', 'LIST'],
                           capture_output=True)
        if r.returncode != 0:
            return False, tr('not registered')
        txt = r.stdout.decode('gbk', errors='replace')
        for line in txt.splitlines():
            # '计划任务状态' / 'Status' are matched verbatim: they follow the OS
            # locale, and inventing a translation would break task lookup.
            if '计划任务状态' in line or 'Status' in line:
                return True, line.split(':', 1)[-1].strip()
        return True, tr('registered')
    except Exception as e:
        return False, tr('query failed: %s') % e


def restart_watcher():
    for act in ('/End', '/Run'):
        subprocess.run(['schtasks', act, '/TN', TASK_NAME], capture_output=True)


# ---------------------------------------------------------------- core actions

def current_save_info():
    p = os.path.join(SAVE_DIR, SAVE_NAME)
    if not os.path.isfile(p):
        return None
    st = os.stat(p)
    return {'path': p, 'size': st.st_size, 'mtime': st.st_mtime,
            'sha256': sha256_bytes(open(p, 'rb').read())}


def restore_to_save(snapshot_path, allow_kill=False, log=print):
    """Roll a snapshot back over the current save. Returns (ok, message)"""
    if not os.path.isfile(snapshot_path):
        return False, tr('Snapshot file does not exist: %s') % snapshot_path

    running = process_running()
    if running:
        if not allow_kill:
            return False, (tr('The game is running, so the rollback was refused.\nFlash keeps the save in memory and overwrites nsSGv1.sol when the game exits,\nso a rollback now would be wiped. Fully quit the game first (or choose to end the process in the UI).'))
        log(tr('Ending the game process...'))
        if not kill_game():
            return False, tr('Failed to end the game process; close it manually and try again.')
        time.sleep(2)

    data = read_stable(snapshot_path)
    if data is None:
        return False, tr('Failed to read the snapshot (the file keeps changing or is unreadable)')

    ok_i, why = sol_integrity(snapshot_path)
    if not ok_i:
        return False, (tr('This snapshot failed the structure check, so the rollback was refused:\n%s\n\nRestoring a corrupt snapshot would leave the game unable to read the save,\nso it is blocked. Please pick another snapshot.') % why)

    if not os.path.isdir(SAVE_DIR):
        return False, tr('Save directory does not exist: %s') % SAVE_DIR

    # Copy the current save aside first, so the rollback itself can be reverted
    cur = os.path.join(SAVE_DIR, SAVE_NAME)
    if os.path.isfile(cur):
        try:
            curdata = open(cur, 'rb').read()
            ts = datetime.now().strftime('%Y%m%d-%H%M%S')
            keep = os.path.join(BACKUP_ROOT, 'nsSGv1_%s_prerestore.sol' % ts)
            os.makedirs(BACKUP_ROOT, exist_ok=True)
            with open(keep, 'wb') as f:
                f.write(curdata)
            log(tr('Backed up the pre-rollback save -> %s') % os.path.basename(keep))
        except Exception as e:
            return False, tr('Failed to back up the current save; aborted for safety: %s') % e

    try:
        with open(cur, 'wb') as f:
            f.write(data)
    except Exception as e:
        return False, tr('Failed to write the save: %s') % e

    got = sha256_bytes(open(cur, 'rb').read())
    want = sha256_bytes(data)
    if got != want:
        return False, tr('Post-write verification failed! source %s / target %s') % (want[:16], got[:16])

    return True, (tr('Rollback complete.\nTarget : %s\nSize   : %d bytes\nSHA-256: %s\n\nStart the game and choose Continue on the main menu to resume this progress.')
                  % (cur, len(data), want))


def restore_to_slot(snapshot_path, slot, label=None, log=print):
    """Write a snapshot into a NEO Save Manager slot (slot=0 is the Quicksave slot)"""
    if not os.path.isdir(NSM_DIR):
        return False, tr('NSM directory does not exist: %s') % NSM_DIR
    data = read_stable(snapshot_path)
    if data is None:
        return False, tr('Failed to read the snapshot')
    dst = os.path.join(NSM_DIR, 'nsSGv1_g%d.sol' % slot)
    try:
        with open(dst, 'wb') as f:
            f.write(data)
        msg = tr('Wrote slot %d: %s (%d bytes)') % (slot, os.path.basename(dst), len(data))
        if slot != 0:
            if label is None:
                m = re.search(r'nsSGv1_(\d{8})-(\d{6})_', os.path.basename(snapshot_path))
                label = (tr('Save %s-%s-%s %s:%s') % (m.group(1)[:4], m.group(1)[4:6],
                                                  m.group(1)[6:8], m.group(2)[:2],
                                                  m.group(2)[2:4])) if m else tr('Save')
            lbl = os.path.join(NSM_DIR, 'nsSGv1_g%d.ini' % slot)
            with open(lbl, 'wb') as f:
                f.write((label + '\r\n').encode('gbk'))
            msg += tr('\nLabel: %s') % label
        return True, msg
    except Exception as e:
        return False, tr('write failed: %s') % e


# ---------------------------------------------------------------- command-line mode

def human_size(n):
    return '%.1f KB' % (n / 1024.0) if n < 1048576 else '%.2f MB' % (n / 1048576.0)


def cli_list():
    cur = current_save_info()
    print(tr('current save:'), ('%d B  %s' % (cur['size'],
          datetime.fromtimestamp(cur['mtime']).strftime('%Y-%m-%d %H:%M:%S')))
          if cur else tr(' **MISSING**'))
    print(tr('game process:'), tr('running') if process_running() else tr('not running'))
    print()
    snaps = list_snapshots()
    print(tr('%d snapshot(s):') % len(snaps))
    for i, (_mt, ts, sz, reason, p, name) in enumerate(snaps):
        ok_i, why = sol_integrity(p)
        print('  %2d) %s  %9s  %-4s  %-10s  %s'
              % (i + 1, ts, human_size(sz), tr('OK') if ok_i else tr('CORRUPT'), reason, name))
    return 0


def resolve_snapshot(token):
    snaps = list_snapshots()
    if not snaps:
        return None
    if token in (None, 'latest', tr('newest')):
        return snaps[0][4]
    if token.isdigit():
        i = int(token) - 1
        return snaps[i][4] if 0 <= i < len(snaps) else None
    for s in snaps:
        if s[5] == token or s[5].startswith(token):
            return s[4]
    return None


def cli_main(argv):
    ap = argparse.ArgumentParser()
    ap.add_argument('--list', action='store_true')
    ap.add_argument('--status', action='store_true')
    ap.add_argument('--restore', metavar=tr('Snapshot'))
    ap.add_argument('--restore-slot', type=int, metavar=tr('Slot number'))
    ap.add_argument('--snapshot', metavar=tr('Snapshot'))
    ap.add_argument('--label')
    ap.add_argument('--yes', action='store_true')
    ap.add_argument('--kill-game', action='store_true')
    ap.add_argument('--config', metavar='FILE', help=tr('path to config.json'))
    a = ap.parse_args(argv)

    if a.list:
        return cli_list()

    if a.status:
        ok, s = task_status()
        print(tr('scheduled task:'), (tr('exists, status %s') % s) if ok else s)
        cur = current_save_info()
        print(tr('current save:'), ('%d B' % cur['size']) if cur else tr(' **MISSING**'))
        print(tr('game process:'), tr('running') if process_running() else tr('not running'))
        print(tr('snapshots:'), len(list_snapshots()))
        return 0

    if a.restore_slot is not None:
        p = resolve_snapshot(a.snapshot)
        if not p:
            print(tr('no snapshot found'))
            return 1
        ok, msg = restore_to_slot(p, a.restore_slot, a.label)
        print(msg)
        return 0 if ok else 1

    if a.restore:
        p = resolve_snapshot(a.restore)
        if not p:
            print(tr('No snapshot found: %s') % a.restore)
            return 1
        if not a.yes:
            print(tr('%s will be restored as the current save.') % os.path.basename(p))
            print(tr('Press Enter to continue, type anything else to cancel.'))
            try:
                if input().strip():
                    print(tr('Cancelled'))
                    return 1
            except EOFError:
                print(tr('non-interactive environment; add --yes'))
                return 1
        ok, msg = restore_to_save(p, allow_kill=a.kill_game)
        print(msg)
        return 0 if ok else 1

    return None      # no CLI argument -> fall through to the GUI


# ---------------------------------------------------------------- GUI

def run_gui(selftest_ms=None):
    r"""Start the graphical interface.

    When selftest_ms is not None: after building the full interface, close
    automatically after the given number of milliseconds. Purpose: verify that
    the UI can be constructed at all. A `.pyw` started through pyw.exe has no
    console, so any tkinter error fails silently (the user double-clicks and
    nothing happens) -- hence this self-test entry point that can run unattended.
    """
    import tkinter as tk
    from tkinter import ttk, messagebox, simpledialog

    root = tk.Tk()
    root.title(APP_TITLE)
    root.geometry('980x600')
    root.minsize(820, 480)

    # top: current status
    top = ttk.Frame(root, padding=(10, 8))
    top.pack(fill='x')
    cur_var = tk.StringVar()
    watch_var = tk.StringVar()
    ttk.Label(top, textvariable=cur_var, font=('Consolas', 10)).pack(anchor='w')
    ttk.Label(top, textvariable=watch_var, font=('Consolas', 9),
              foreground='#666').pack(anchor='w', pady=(2, 0))

    # middle: snapshot list
    mid = ttk.Frame(root, padding=(10, 0))
    mid.pack(fill='both', expand=True)
    cols = ('time', 'size', 'integrity', 'reason', 'name')
    tv = ttk.Treeview(mid, columns=cols, show='headings', height=18)
    for c, t, w in (('time', tr('Time'), 180), ('size', tr('Size'), 85), ('integrity', tr('Integrity'), 60),
                    ('reason', tr('Reason'), 100), ('name', tr('File'), 470)):
        tv.heading(c, text=t)
        tv.column(c, width=w, anchor='w' if c in ('name', 'time') else 'center')
    vs = ttk.Scrollbar(mid, orient='vertical', command=tv.yview)
    tv.configure(yscrollcommand=vs.set)
    tv.pack(side='left', fill='both', expand=True)
    vs.pack(side='right', fill='y')
    tv.tag_configure('newest', background='#2d4d2d', foreground='#ffffff')
    tv.tag_configure('bad', background='#5a2020', foreground='#ffffff')

    # bottom: actions
    bot = ttk.Frame(root, padding=(10, 8))
    bot.pack(fill='x')
    hint_var = tk.StringVar(value='')
    ttk.Label(bot, textvariable=hint_var, foreground='#444').pack(anchor='w', pady=(0, 6))

    btns = ttk.Frame(bot)
    btns.pack(fill='x')

    state = {'snaps': []}

    def refresh():
        tv.delete(*tv.get_children())
        state['snaps'] = list_snapshots()
        n_bad = 0
        for i, (_mt, ts, sz, reason, p, name) in enumerate(state['snaps']):
            ok_i, _why = sol_integrity(p)
            if not ok_i:
                n_bad += 1
            tags = ['bad'] if not ok_i else (['newest'] if i == 0 else [])
            tv.insert('', 'end', iid=name,
                      values=(ts, human_size(sz), tr('OK') if ok_i else tr('CORRUPT'), reason, name),
                      tags=tags)
        cur = current_save_info()
        if cur:
            cur_var.set(tr('Current save: %d bytes  %s    SHA256 %s') % (
                cur['size'],
                datetime.fromtimestamp(cur['mtime']).strftime('%Y-%m-%d %H:%M:%S'),
                cur['sha256'][:16]))
        else:
            cur_var.set(tr('Current save: **MISSING** (deleted; a rollback is needed)'))
        ok, s = task_status()
        watch_var.set(tr('Auto-backup watcher: %s') % ((tr('running / %s') % s) if ok else tr('not registered'))
                      + tr('  |  game process: ') + (tr('running') if process_running() else tr('not running'))
                      + tr('  |  %d snapshot(s)') % len(state['snaps']))
        if state['snaps']:
            tv.selection_set(state['snaps'][0][5])
            if n_bad:
                hint_var.set(tr('Note: %d snapshot(s) failed the structure check (red rows). They are refused on rollback, so pick another one.')
                             % n_bad)
            else:
                hint_var.set(tr('Tip: the first row is the last snapshot before death, and every snapshot passed the structure check. Double-click any row to roll back.'))

    def selected():
        sel = tv.selection()
        if not sel:
            messagebox.showinfo(tr('nothing selected'), tr('Select a snapshot in the list first.'))
            return None
        for _mt, _ts, _sz, _r, p, name in state['snaps']:
            if name == sel[0]:
                return p
        return None

    def do_restore():
        p = selected()
        if not p:
            return
        info = [s for s in state['snaps'] if s[4] == p][0]
        running = process_running()
        kill = False
        if running:
            if not messagebox.askyesno(
                    tr('The game is running'),
                    tr('NEOScavenger.exe is running.\n\nFlash keeps the save in memory and overwrites nsSGv1.sol when the game exits,\nso a rollback now would be wiped.\n\nEnd the game process and continue with the rollback?')):
                return
            kill = True
        if not messagebox.askyesno(
                tr('Confirm rollback'),
                tr('Restore the snapshot below as the current save?\n\nTime  : %s\nSize  : %s\nReason: %s\nFile  : %s\n\n(The current save is backed up automatically before the rollback.)') % (
                    info[1], human_size(info[2]), info[3], info[5])):
            return
        ok, msg = restore_to_save(p, allow_kill=kill)
        (messagebox.showinfo if ok else messagebox.showerror)(
            tr('Rollback complete') if ok else tr('Rollback failed'), msg)
        refresh()

    def do_slot(slot):
        p = selected()
        if not p:
            return
        info = [s for s in state['snaps'] if s[4] == p][0]
        label = None
        if slot != 0:
            label = simpledialog.askstring(
                tr('Slot label'), tr('Enter a label for slot %d (leave empty to generate one from the time):') % slot,
                initialvalue=tr('Save %s') % info[1][5:16], parent=root)
            if label is None:
                return
            label = label.strip() or None
        ok, msg = restore_to_slot(p, slot, label)
        (messagebox.showinfo if ok else messagebox.showerror)(
            tr('Slot written') if ok else tr('write failed'), msg)
        refresh()

    def open_folder():
        try:
            os.startfile(BACKUP_ROOT)
        except Exception as e:
            messagebox.showerror(tr('failed to open'), str(e))

    def restart_watch():
        restart_watcher()
        time.sleep(1)
        refresh()
        messagebox.showinfo(tr('restarted'), tr('Auto-backup watcher restarted.'))

    ttk.Button(btns, text=tr('⏪ Restore as current save'), command=do_restore).pack(side='left')
    ttk.Button(btns, text=tr('Copy to slot 1'), command=lambda: do_slot(1)).pack(side='left', padx=4)
    ttk.Button(btns, text=tr('Copy to slot 2'), command=lambda: do_slot(2)).pack(side='left')
    ttk.Button(btns, text=tr('Copy to slot 3'), command=lambda: do_slot(3)).pack(side='left', padx=4)
    ttk.Button(btns, text=tr('Copy to Quicksave slot'), command=lambda: do_slot(0)).pack(side='left')
    ttk.Button(btns, text=tr('Refresh'), command=refresh).pack(side='right')
    ttk.Button(btns, text=tr('Open backup folder'), command=open_folder).pack(side='right', padx=4)
    ttk.Button(btns, text=tr('Restart watcher'), command=restart_watch).pack(side='right')

    tv.bind('<Double-1>', lambda e: do_restore())
    refresh()

    if selftest_ms is not None:
        # Self-test: build the whole widget tree, run refresh() once, then close
        root.update_idletasks()
        root.update()
        print(tr('[selftest] UI built successfully'))
        print(tr('[selftest] snapshot rows = %d') % len(tv.get_children()))
        print(tr('[selftest] window size = %dx%d') % (root.winfo_width(), root.winfo_height()))
        root.after(int(selftest_ms), root.destroy)

    root.mainloop()
    return 0


def main():
    rc = cli_main(sys.argv[1:])
    if rc is None:
        if not try_acquire_single_instance():
            if focus_existing_window():
                print(tr('A rollback GUI is already running; focused its window.'))
            else:
                print(tr('A rollback GUI is already running (its window was not found yet; it may still be starting).'))
            return 0
        ms = os.environ.get('NSM_GUI_SELFTEST')
        return run_gui(int(ms) if ms else None)
    return rc


if __name__ == '__main__':
    sys.exit(main())

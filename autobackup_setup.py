# -*- coding: utf-8 -*-
r"""NEO Scavenger auto-backup — install / uninstall / status / seed

Subcommands:
    install    generate the scheduled-task XML and register it (hidden watcher at
               logon), then start it immediately
    uninstall  delete the scheduled task (touches no backup files)
    status     show task state, backup count, newest snapshots
    seed       sync the newest snapshot into NEO Save Manager slots (default g1 + g0)
    run        run one watcher cycle manually

Design notes:
  · The watcher is hosted by pythonw.exe -> no console window
  · The task XML is written as UTF-16LE + BOM (a schtasks /XML requirement)
  · The task name is Chinese first; if registration fails it falls back to plain
    ASCII, which removes the encoding variable
"""

import argparse
import os
import re
import subprocess
import sys
import time
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
WATCHER = os.path.join(HERE, 'neo_save_watcher.py')
XML_PATH = os.path.join(HERE, 'autobackup_task.xml')

# Paths are not hard-coded: this shares the watcher's resolution chain
# (environment variables -> config.json -> auto-detection)
import nsg_paths
from nsg_i18n import tr

PATHS = nsg_paths.resolve()
GAME_DIR = PATHS['game_dir']
NSM_DIR = PATHS['nsm_dir']
BACKUP_ROOT = PATHS['backup_root']


def _sibling(name):
    """Another executable next to the current interpreter (python.exe <-> pythonw.exe)"""
    d = os.path.dirname(sys.executable or '')
    cand = os.path.join(d, name)
    return cand if os.path.isfile(cand) else None


# The Python path is no longer hard-coded: it follows the interpreter running
# this very script.
PYTHON = sys.executable or ''
PYTHONW = _sibling('pythonw.exe') or PYTHON

TASK_NAME_CN = 'NEO Scavenger 存档自动备份'
TASK_NAME_EN = 'NEO Scavenger AutoBackup'


def account():
    u = os.environ.get('USERNAME') or os.environ.get('USER') or ''
    c = os.environ.get('COMPUTERNAME') or ''
    if c and u:
        return '%s\\%s' % (c, u)
    return u or c


def build_xml(task_name):
    acct = account()
    return '''<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Author>%s</Author>
    <Description>监视 NEO Scavenger 存档文件，游戏启动与游戏内存档时自动快照备份。</Description>
    <URI>\\%s</URI>
  </RegistrationInfo>
  <Triggers>
    <LogonTrigger>
      <Enabled>true</Enabled>
      <UserId>%s</UserId>
      <Delay>PT20S</Delay>
    </LogonTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>%s</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <IdleSettings>
      <StopOnIdleEnd>false</StopOnIdleEnd>
      <RestartOnIdle>false</RestartOnIdle>
    </IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <WakeToRun>false</WakeToRun>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <Priority>7</Priority>
    <RestartOnFailure>
      <Interval>PT1M</Interval>
      <Count>3</Count>
    </RestartOnFailure>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>%s</Command>
      <Arguments>"%s"</Arguments>
      <WorkingDirectory>%s</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
''' % (acct, task_name, acct, acct, PYTHONW, WATCHER, HERE)


def write_xml(task_name):
    data = build_xml(task_name)
    # schtasks /XML requires Unicode; UTF-16LE + BOM
    with open(XML_PATH, 'wb') as f:
        f.write(b'\xff\xfe')
        f.write(data.encode('utf-16-le'))
    return XML_PATH


def schtasks(args, quiet=False):
    r = subprocess.run(['schtasks'] + args, capture_output=True)
    out = r.stdout.decode('gbk', errors='replace') if r.stdout else ''
    err = r.stderr.decode('gbk', errors='replace') if r.stderr else ''
    if not quiet:
        print((out + err).strip())
    return r.returncode, out + err


def query(task_name):
    rc, out = schtasks(['/Query', '/TN', task_name, '/FO', 'LIST', '/V'], quiet=True)
    return rc, out


def cmd_install(a):
    print(tr(' === Preflight checks ==='))
    if not account():
        print(tr('   !! Cannot determine the current user (USERNAME / COMPUTERNAME are empty), aborting.'))
        return 1
    hard = [
        (tr('watcher script'), os.path.isfile(WATCHER), WATCHER),
        ('pythonw.exe', os.path.isfile(PYTHONW), PYTHONW),
        (tr('Save dir'), bool(PATHS['save_dir']) and os.path.isdir(PATHS['save_dir']),
         PATHS['save_dir']),
    ]
    soft = [
        (tr('Game dir'), bool(GAME_DIR) and os.path.isdir(GAME_DIR), GAME_DIR),
        (tr('NSM dir'), bool(NSM_DIR) and os.path.isdir(NSM_DIR), NSM_DIR),
    ]
    ok = True
    for label, cond, p in hard:
        print('  %-12s %-4s %s' % (label, 'OK' if cond else 'FAIL', p))
        ok = ok and cond
    for label, cond, p in soft:
        print(tr('   %-12s %-4s %s   (optional; only affects NSM slot sync)')
              % (label, 'OK' if cond else 'SKIP', p))
    if not ok:
        print(tr('\nRequired conditions not met, aborting.'))
        print(tr('Run `python neo_save_watcher.py --check` first to see the path resolution details.'))
        return 1

    print(tr('\n=== Registering scheduled task ==='))
    for name in (TASK_NAME_CN, TASK_NAME_EN):
        xml = write_xml(name)
        rc, out = schtasks(['/Create', '/TN', name, '/XML', xml, '/F'])
        if rc == 0:
            print(tr('   task name: %s') % name)
            print('  XML  : %s' % xml)
            print(tr('\n=== Starting now (no need to log off and on) ==='))
            rc2, out2 = schtasks(['/Run', '/TN', name])
            print(tr('   /Run return code:'), rc2)
            print(tr('\n=== Task status ==='))
            cmd_status(a)
            return 0
        else:
            print(tr('   Registering task name %r failed, trying the next one...') % name)
    print(tr('   All task names failed to register'))
    return 1


def cmd_uninstall(a):
    for name in (TASK_NAME_CN, TASK_NAME_EN):
        rc, out = schtasks(['/Query', '/TN', name], quiet=True)
        if rc == 0:
            print(tr(' === Deleting task: %s ===') % name)
            schtasks(['/End', '/TN', name], quiet=True)
            schtasks(['/Delete', '/TN', name, '/F'])
    print(tr('\nBackup files were not deleted; they are still in: %s') % BACKUP_ROOT)
    return 0


def cmd_status(a):
    print(tr(' === Scheduled task ==='))
    found = False
    for name in (TASK_NAME_CN, TASK_NAME_EN):
        rc, out = query(name)
        if rc == 0:
            found = True
            print(tr('   task: %s') % name)
            for line in out.splitlines():
                ls = line.strip()
                if not ls:
                    continue
                # NB: these match words are intentionally left untranslated -- they
                # must match whatever locale schtasks actually emits.
                if any(k in ls for k in ('状态', 'Status', '上次运行', 'Last Run',
                                         '上次结果', 'Last Result', '下次运行', 'Next Run',
                                         '要运行的任务', 'Task To Run', '作为用户', 'Run As User')):
                    print('    ', ls)
    if not found:
        print(tr('   not registered (use install)'))

    print(tr('\n=== Watcher processes ==='))
    import ctypes
    found_py = False
    k32 = ctypes.windll.kernel32
    TH32CS_SNAPPROCESS = 0x2
    snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snap and snap != 0xFFFFFFFF:
        class PE32(ctypes.Structure):
            _fields_ = [('dwSize', ctypes.c_ulong), ('cntUsage', ctypes.c_ulong),
                        ('th32ProcessID', ctypes.c_ulong), ('th32DefaultHeapID', ctypes.c_size_t),
                        ('th32ModuleID', ctypes.c_ulong), ('cntThreads', ctypes.c_ulong),
                        ('th32ParentProcessID', ctypes.c_ulong), ('pcPriClassBase', ctypes.c_long),
                        ('dwFlags', ctypes.c_ulong), ('szExeFile', ctypes.c_char * 260)]
        e = PE32()
        e.dwSize = ctypes.sizeof(PE32)
        okk = k32.Process32First(snap, ctypes.byref(e))
        while okk:
            # Match on the exe name, never on wmic text output: wmic emits GBK and
            # mis-decodes non-ASCII, which used to report "process absent" wrongly.
            nm = e.szExeFile.decode('mbcs', errors='replace')
            if nm.lower() in ('pythonw.exe', 'python.exe'):
                found_py = True
                print('    %s  PID=%d' % (nm, e.th32ProcessID))
            okk = k32.Process32Next(snap, ctypes.byref(e))
        k32.CloseHandle(snap)
    if not found_py:
        print(tr('     No python/pythonw process found (the watcher may not be running)'))

    print(tr('\n=== Snapshots ==='))
    if not os.path.isdir(BACKUP_ROOT):
        print(tr('   directory does not exist yet:'), BACKUP_ROOT)
    else:
        names = sorted([n for n in os.listdir(BACKUP_ROOT)
                        if n.startswith('nsSGv1_') and n.endswith('.sol')], reverse=True)
        total = sum(os.path.getsize(os.path.join(BACKUP_ROOT, n)) for n in names)
        print(tr('   dir         : %s') % BACKUP_ROOT)
        print(tr('   snapshots   : %d   %.2f MB total') % (len(names), total / 1048576.0))
        for n in names[:5]:
            p = os.path.join(BACKUP_ROOT, n)
            print(tr('     newest %9d B  %s') % (os.path.getsize(p), n))
        if len(names) > 5:
            print(tr('     ...   %d total') % len(names))

    print(tr('\n=== NEO Save Manager slots ==='))
    if os.path.isdir(NSM_DIR):
        for n in sorted(os.listdir(NSM_DIR)):
            if n.startswith('nsSGv1_g'):
                p = os.path.join(NSM_DIR, n)
                extra = ''
                if n.endswith('.ini'):
                    try:
                        # Slot labels are written as GBK; decode accordingly.
                        extra = '  -> ' + open(p, 'rb').read().decode('gbk').strip()
                    except Exception:
                        pass
                print('    %9d B  %s%s' % (os.path.getsize(p), n, extra))
    return 0


def cmd_seed(a):
    names = sorted([n for n in os.listdir(BACKUP_ROOT)
                    if n.startswith('nsSGv1_') and n.endswith('.sol')], reverse=True)
    if not names:
        print(tr('No snapshots in auto\\, run the watcher once first'))
        return 1
    src = os.path.join(BACKUP_ROOT, names[0])
    with open(src, 'rb') as f:
        data = f.read()

    m = re.match(r'nsSGv1_(\d{8})-(\d{6})_', names[0])
    label = tr('Save %s-%s-%s %s:%s') % (
        m.group(1)[:4], m.group(1)[4:6], m.group(1)[6:8],
        m.group(2)[:2], m.group(2)[2:4]) if m else tr('Save')

    targets = [int(x) for x in a.slots.split(',')] if a.slots else [1, 0]
    print(tr('source snapshot: %s (%d B)') % (names[0], len(data)))
    print(tr('label : %s') % label)
    for slot in targets:
        dst = os.path.join(NSM_DIR, 'nsSGv1_g%d.sol' % slot)
        with open(dst, 'wb') as f:
            f.write(data)
        print(tr('   wrote slot %d: %s (%d B)') % (slot, os.path.basename(dst), len(data)))
        if slot != 0:
            lbl = os.path.join(NSM_DIR, 'nsSGv1_g%d.ini' % slot)
            with open(lbl, 'wb') as f:
                f.write((label + '\r\n').encode('gbk'))
            print(tr('   wrote label: %s -> %s') % (os.path.basename(lbl), label))
    return 0


def cmd_run(a):
    for py in (PYTHON, sys.executable):
        if py and os.path.isfile(py):
            subprocess.run([py, WATCHER, '--once'])
            return 0
    print(tr('python not found'))
    return 1


def cmd_restart(a):
    """Restart the watcher: required after editing neo_save_watcher.py"""
    name = None
    for cand in (TASK_NAME_CN, TASK_NAME_EN):
        rc, _ = schtasks(['/Query', '/TN', cand], quiet=True)
        if rc == 0:
            name = cand
            break
    if not name:
        print(tr('Scheduled task not found; run install first'))
        return 1
    schtasks(['/End', '/TN', name], quiet=True)
    time.sleep(1)
    rc, out = schtasks(['/Run', '/TN', name])
    print(tr('Task restarted: %s (return code %s)') % (name, rc))
    time.sleep(2)
    cmd_status(a)
    return 0


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest='cmd')
    sub.add_parser('install')
    sub.add_parser('uninstall')
    sub.add_parser('status')
    p_seed = sub.add_parser('seed')
    p_seed.add_argument('--slots', default=None, help=tr('comma separated, default 1,0'))
    sub.add_parser('run')
    sub.add_parser('restart')
    a = ap.parse_args()
    if a.cmd == 'install':
        return cmd_install(a)
    if a.cmd == 'uninstall':
        return cmd_uninstall(a)
    if a.cmd == 'status':
        return cmd_status(a)
    if a.cmd == 'seed':
        return cmd_seed(a)
    if a.cmd == 'run':
        return cmd_run(a)
    if a.cmd == 'restart':
        return cmd_restart(a)
    ap.print_help()
    return 0


if __name__ == '__main__':
    sys.exit(main())

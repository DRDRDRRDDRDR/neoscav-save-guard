# -*- coding: utf-8 -*-
r"""NEO Scavenger 自动备份 —— 安装 / 卸载 / 状态 / 播种

子命令：
    install    生成计划任务 XML 并注册（登录时隐藏启动监视器），随后立即启动
    uninstall  删除计划任务（不动任何备份文件）
    status     查看任务状态、备份数量、最新快照
    seed       把最新快照同步进 NEO Save Manager 的槽位（默认 g1 + g0）
    run        手动跑一轮监视器

设计说明：
  · 监视器由 pythonw.exe 承载 → 无控制台窗口
  · 任务 XML 用 UTF-16LE + BOM 写出（schtasks /XML 的要求）
  · 任务名先用中文，若注册失败自动回退纯 ASCII，排除编码变量
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

# 路径不硬编码：与监视器共用同一套解析（环境变量 → config.json → 自动探测）
import nsg_paths

PATHS = nsg_paths.resolve()
GAME_DIR = PATHS['game_dir']
NSM_DIR = PATHS['nsm_dir']
BACKUP_ROOT = PATHS['backup_root']


def _sibling(name):
    """与当前解释器同目录的另一个可执行文件（python.exe ↔ pythonw.exe）"""
    d = os.path.dirname(sys.executable or '')
    cand = os.path.join(d, name)
    return cand if os.path.isfile(cand) else None


# 不再硬编码 Python 路径：跟随「正在运行本脚本的解释器」
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
    # schtasks /XML 要求 Unicode；UTF-16LE + BOM
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
    print('=== 前置校验 ===')
    if not account():
        print('  !! 无法确定当前用户名（USERNAME / COMPUTERNAME 均为空），终止。')
        return 1
    hard = [
        ('监视器脚本', os.path.isfile(WATCHER), WATCHER),
        ('pythonw.exe', os.path.isfile(PYTHONW), PYTHONW),
        ('存档目录', bool(PATHS['save_dir']) and os.path.isdir(PATHS['save_dir']),
         PATHS['save_dir']),
    ]
    soft = [
        ('游戏目录', bool(GAME_DIR) and os.path.isdir(GAME_DIR), GAME_DIR),
        ('NSM 目录', bool(NSM_DIR) and os.path.isdir(NSM_DIR), NSM_DIR),
    ]
    ok = True
    for label, cond, p in hard:
        print('  %-12s %-4s %s' % (label, 'OK' if cond else 'FAIL', p))
        ok = ok and cond
    for label, cond, p in soft:
        print('  %-12s %-4s %s   (可选，缺失只影响 NSM 槽位同步)'
              % (label, 'OK' if cond else 'SKIP', p))
    if not ok:
        print('\n必需条件不满足，终止。')
        print('可先运行 `python neo_save_watcher.py --check` 查看路径解析明细。')
        return 1

    print('\n=== 注册计划任务 ===')
    for name in (TASK_NAME_CN, TASK_NAME_EN):
        xml = write_xml(name)
        rc, out = schtasks(['/Create', '/TN', name, '/XML', xml, '/F'])
        if rc == 0:
            print('  任务名: %s' % name)
            print('  XML  : %s' % xml)
            print('\n=== 立即启动（无需等待重新登录） ===')
            rc2, out2 = schtasks(['/Run', '/TN', name])
            print('  /Run 返回码:', rc2)
            print('\n=== 任务状态 ===')
            cmd_status(a)
            return 0
        else:
            print('  用任务名 %r 注册失败，尝试下一个…' % name)
    print('  全部任务名均注册失败')
    return 1


def cmd_uninstall(a):
    for name in (TASK_NAME_CN, TASK_NAME_EN):
        rc, out = schtasks(['/Query', '/TN', name], quiet=True)
        if rc == 0:
            print('=== 删除任务: %s ===' % name)
            schtasks(['/End', '/TN', name], quiet=True)
            schtasks(['/Delete', '/TN', name, '/F'])
    print('\n备份文件未被删除，仍在: %s' % BACKUP_ROOT)
    return 0


def cmd_status(a):
    print('=== 计划任务 ===')
    found = False
    for name in (TASK_NAME_CN, TASK_NAME_EN):
        rc, out = query(name)
        if rc == 0:
            found = True
            print('  任务: %s' % name)
            for line in out.splitlines():
                ls = line.strip()
                if not ls:
                    continue
                if any(k in ls for k in ('状态', 'Status', '上次运行', 'Last Run',
                                         '上次结果', 'Last Result', '下次运行', 'Next Run',
                                         '要运行的任务', 'Task To Run', '作为用户', 'Run As User')):
                    print('    ', ls)
    if not found:
        print('  未注册（用 install 注册）')

    print('\n=== 监视器进程 ===')
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
            nm = e.szExeFile.decode('mbcs', errors='replace')
            if nm.lower() in ('pythonw.exe', 'python.exe'):
                found_py = True
                print('    %s  PID=%d' % (nm, e.th32ProcessID))
            okk = k32.Process32Next(snap, ctypes.byref(e))
        k32.CloseHandle(snap)
    if not found_py:
        print('    未发现 python/pythonw 进程（监视器可能未运行）')

    print('\n=== 备份快照 ===')
    if not os.path.isdir(BACKUP_ROOT):
        print('  目录尚不存在:', BACKUP_ROOT)
    else:
        names = sorted([n for n in os.listdir(BACKUP_ROOT)
                        if n.startswith('nsSGv1_') and n.endswith('.sol')], reverse=True)
        total = sum(os.path.getsize(os.path.join(BACKUP_ROOT, n)) for n in names)
        print('  目录    : %s' % BACKUP_ROOT)
        print('  快照份数: %d   合计 %.2f MB' % (len(names), total / 1048576.0))
        for n in names[:5]:
            p = os.path.join(BACKUP_ROOT, n)
            print('    最新  %9d B  %s' % (os.path.getsize(p), n))
        if len(names) > 5:
            print('    ...   共 %d 份' % len(names))

    print('\n=== NEO Save Manager 槽位 ===')
    if os.path.isdir(NSM_DIR):
        for n in sorted(os.listdir(NSM_DIR)):
            if n.startswith('nsSGv1_g'):
                p = os.path.join(NSM_DIR, n)
                extra = ''
                if n.endswith('.ini'):
                    try:
                        extra = '  -> ' + open(p, 'rb').read().decode('gbk').strip()
                    except Exception:
                        pass
                print('    %9d B  %s%s' % (os.path.getsize(p), n, extra))
    return 0


def cmd_seed(a):
    names = sorted([n for n in os.listdir(BACKUP_ROOT)
                    if n.startswith('nsSGv1_') and n.endswith('.sol')], reverse=True)
    if not names:
        print('auto\\ 中没有快照，先跑一轮监视器')
        return 1
    src = os.path.join(BACKUP_ROOT, names[0])
    with open(src, 'rb') as f:
        data = f.read()

    m = re.match(r'nsSGv1_(\d{8})-(\d{6})_', names[0])
    label = '存档 %s-%s-%s %s:%s' % (
        m.group(1)[:4], m.group(1)[4:6], m.group(1)[6:8],
        m.group(2)[:2], m.group(2)[2:4]) if m else '存档'

    targets = [int(x) for x in a.slots.split(',')] if a.slots else [1, 0]
    print('源快照: %s (%d B)' % (names[0], len(data)))
    print('标签  : %s' % label)
    for slot in targets:
        dst = os.path.join(NSM_DIR, 'nsSGv1_g%d.sol' % slot)
        with open(dst, 'wb') as f:
            f.write(data)
        print('  写入槽%d: %s (%d B)' % (slot, os.path.basename(dst), len(data)))
        if slot != 0:
            lbl = os.path.join(NSM_DIR, 'nsSGv1_g%d.ini' % slot)
            with open(lbl, 'wb') as f:
                f.write((label + '\r\n').encode('gbk'))
            print('  写入标签: %s -> %s' % (os.path.basename(lbl), label))
    return 0


def cmd_run(a):
    for py in (PYTHON, sys.executable):
        if py and os.path.isfile(py):
            subprocess.run([py, WATCHER, '--once'])
            return 0
    print('未找到 python')
    return 1


def cmd_restart(a):
    """重启监视器：改动 neo_save_watcher.py 后必须重启才生效"""
    name = None
    for cand in (TASK_NAME_CN, TASK_NAME_EN):
        rc, _ = schtasks(['/Query', '/TN', cand], quiet=True)
        if rc == 0:
            name = cand
            break
    if not name:
        print('未找到计划任务，请先 install')
        return 1
    schtasks(['/End', '/TN', name], quiet=True)
    time.sleep(1)
    rc, out = schtasks(['/Run', '/TN', name])
    print('已重启任务: %s (返回码 %s)' % (name, rc))
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
    p_seed.add_argument('--slots', default=None, help='逗号分隔，默认 1,0')
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

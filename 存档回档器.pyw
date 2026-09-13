#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""NEO Scavenger 存档回档器（GUI）

功能：
  · 列出 auto\ 里的全部快照（时间 / 大小 / 触发原因）
  · 一键把任意快照回档成当前存档（nsSGv1.sol）
  · 也可以把快照写入 NEO Save Manager 的命名槽（g1/g2/g3）或 Quicksave 槽（g0）
  · 打开备份文件夹、查看监视器状态

安全设计（重要）：
  1. 回档前若游戏正在运行 → 拒绝执行。因为 Flash 把存档握在内存里，
     游戏退出时会用内存内容覆盖 nsSGv1.sol，你的回档会被抹掉。
     界面上提供"结束游戏进程"按钮，确认后才继续。
  2. 覆盖前**自动把当前存档另存一份**（reason=prerestore），回档本身也可回退。
  3. 写完后做 SHA-256 校验，不一致就报错。

命令行模式（便于脚本化 / 自动化，无 GUI）：
  存档回档器.pyw --list
  存档回档器.pyw --restore <文件名|latest> [--yes]
  存档回档器.pyw --restore-slot 2 [--snapshot <文件名|latest>] [--label 文字]
  存档回档器.pyw --status

双击运行（无参数）即为图形界面。
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

# ---------------------------------------------------------------- 路径解析
# 与监视器共用同一套解析（环境变量 → config.json → 自动探测），避免两处各写一份。
import nsg_paths

_CONFIG_ARG = nsg_paths.cli_config_arg()
PATHS = nsg_paths.resolve(_CONFIG_ARG)
GAME_DIR = PATHS['game_dir']
NSM_DIR = PATHS['nsm_dir']
SAVE_DIR = PATHS['save_dir']
BACKUP_ROOT = PATHS['backup_root']
SAVE_NAME = nsg_paths.SAVE_NAME
GAME_EXE = nsg_paths.GAME_EXE
TASK_NAME = 'NEO Scavenger 存档自动备份'
APP_TITLE = 'NEO Scavenger 存档回档器'
MUTEX_NAME = 'NEOScavengerSaveRollbackGUI'

# ---------------------------------------------------------------- 单实例保护
# 监视器会在"检测到游戏启动"时带起本 GUI。若不做单实例保护，
# 每启动一次游戏就会多开一个窗口，很快满屏。
_MUTEX_HANDLE = None


def try_acquire_single_instance():
    r"""返回 True = 本进程是唯一实例；False = 已有实例在运行。

    用命名内核对象（mutex）而不是"查进程列表"：Toolhelp32 只能拿到
    exe 名（pythonw.exe），拿不到脚本名，无法区分是本 GUI 还是别的 Python 脚本。
    """
    global _MUTEX_HANDLE
    try:
        k32 = ctypes.WinDLL('kernel32', use_last_error=True)
        _MUTEX_HANDLE = k32.CreateMutexW(None, False, MUTEX_NAME)
        err = ctypes.get_last_error()
        if not _MUTEX_HANDLE:
            return True                      # 拿不到就放行，不阻塞使用
        return err != 183                    # ERROR_ALREADY_EXISTS = 183
    except Exception:
        return True


def focus_existing_window():
    r"""把已在运行的回档器窗口调到前台"""
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

# ---------------------------------------------------------------- 基础工具


def sha256_bytes(b):
    return hashlib.sha256(b).hexdigest()


def sol_integrity(path):
    r"""校验 .sol 结构完整性。返回 (ok, 说明)。

    SOL 文件头实测结构（14 份快照 + 2 个时间点交叉验证）：
      偏移 0: 2 字节魔数 0x00 0xBF
      偏移 2: 4 字节大端无符号整数 = 其后数据的长度
      偏移 6: 载荷起始（"TCSO" 等）
    因此恒有：长度字段 == 文件大小 - 6。
    实测：200996 字节 -> 0x0003111E = 200990；285198 字节 -> 0x00045A08 = 285192。
    若不符，说明文件被截断或损坏 —— 这种快照回档后游戏会读不出来，
    所以必须在回档【之前】就拦住，而不是等用户进游戏才发现。
    """
    try:
        size = os.path.getsize(path)
        with open(path, 'rb') as f:
            h = f.read(6)
        if len(h) < 6:
            return False, '文件过小（<6 字节）'
        if h[:2] != b'\x00\xbf':
            return False, '魔数异常（期望 00 bf，实际 %s）' % h[:2].hex(' ')
        n = int.from_bytes(h[2:6], 'big')
        if n == size - 6:
            return True, '结构完整'
        return False, '长度字段 %d ≠ 文件大小-6 = %d（疑被截断）' % (n, size - 6)
    except Exception as e:
        return False, '读取失败：%s' % e


def read_stable(path, tries=6, delay=0.2):
    r"""连续两次读取一致才返回内容；否则返回 None（避免拿到半截文件）"""
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
    """返回 [(mtime, 时间戳字符串, 大小, 原因, 完整路径, 文件名)]，新的在前"""
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
    """返回 (是否存在, 简要状态文本)"""
    try:
        r = subprocess.run(['schtasks', '/Query', '/TN', TASK_NAME, '/FO', 'LIST'],
                           capture_output=True)
        if r.returncode != 0:
            return False, '未注册'
        txt = r.stdout.decode('gbk', errors='replace')
        for line in txt.splitlines():
            if '计划任务状态' in line or 'Status' in line:
                return True, line.split(':', 1)[-1].strip()
        return True, '已注册'
    except Exception as e:
        return False, '查询失败: %s' % e


def restart_watcher():
    for act in ('/End', '/Run'):
        subprocess.run(['schtasks', act, '/TN', TASK_NAME], capture_output=True)


# ---------------------------------------------------------------- 核心动作

def current_save_info():
    p = os.path.join(SAVE_DIR, SAVE_NAME)
    if not os.path.isfile(p):
        return None
    st = os.stat(p)
    return {'path': p, 'size': st.st_size, 'mtime': st.st_mtime,
            'sha256': sha256_bytes(open(p, 'rb').read())}


def restore_to_save(snapshot_path, allow_kill=False, log=print):
    """把快照回档成当前存档。返回 (ok, 消息)"""
    if not os.path.isfile(snapshot_path):
        return False, '快照文件不存在：%s' % snapshot_path

    running = process_running()
    if running:
        if not allow_kill:
            return False, ('游戏正在运行，已拒绝回档。\n'
                           'Flash 会把存档保存在内存里，游戏退出时会覆盖 nsSGv1.sol，\n'
                           '此时回档会被抹掉。请先完全退出游戏（或在界面中选择结束进程）。')
        log('正在结束游戏进程…')
        if not kill_game():
            return False, '结束游戏进程失败，请手动关闭后重试。'
        time.sleep(2)

    data = read_stable(snapshot_path)
    if data is None:
        return False, '读取快照失败（文件持续变化或不可读）'

    ok_i, why = sol_integrity(snapshot_path)
    if not ok_i:
        return False, ('该快照结构校验未通过，已拒绝回档：\n%s\n\n'
                       '回档一个损坏的快照会让游戏读不出存档，\n'
                       '所以宁可先拦住。请换一份快照。' % why)

    if not os.path.isdir(SAVE_DIR):
        return False, '存档目录不存在：%s' % SAVE_DIR

    # 覆盖前先保存"当前存档"，让回档本身也可回退
    cur = os.path.join(SAVE_DIR, SAVE_NAME)
    if os.path.isfile(cur):
        try:
            curdata = open(cur, 'rb').read()
            ts = datetime.now().strftime('%Y%m%d-%H%M%S')
            keep = os.path.join(BACKUP_ROOT, 'nsSGv1_%s_prerestore.sol' % ts)
            os.makedirs(BACKUP_ROOT, exist_ok=True)
            with open(keep, 'wb') as f:
                f.write(curdata)
            log('已备份回档前的当前存档 → %s' % os.path.basename(keep))
        except Exception as e:
            return False, '备份当前存档失败，为安全起见已中止：%s' % e

    try:
        with open(cur, 'wb') as f:
            f.write(data)
    except Exception as e:
        return False, '写入存档失败：%s' % e

    got = sha256_bytes(open(cur, 'rb').read())
    want = sha256_bytes(data)
    if got != want:
        return False, '写入后校验失败！源 %s / 目标 %s' % (want[:16], got[:16])

    return True, ('回档成功。\n'
                  '目标：%s\n'
                  '大小：%d 字节\n'
                  'SHA-256：%s\n\n'
                  '现在启动游戏，在主菜单选 Continue 即可续上这份进度。'
                  % (cur, len(data), want))


def restore_to_slot(snapshot_path, slot, label=None, log=print):
    """把快照写入 NEO Save Manager 的槽位（slot=0 为 Quicksave）"""
    if not os.path.isdir(NSM_DIR):
        return False, 'NSM 目录不存在：%s' % NSM_DIR
    data = read_stable(snapshot_path)
    if data is None:
        return False, '读取快照失败'
    dst = os.path.join(NSM_DIR, 'nsSGv1_g%d.sol' % slot)
    try:
        with open(dst, 'wb') as f:
            f.write(data)
        msg = '已写入槽 %d：%s（%d 字节）' % (slot, os.path.basename(dst), len(data))
        if slot != 0:
            if label is None:
                m = re.search(r'nsSGv1_(\d{8})-(\d{6})_', os.path.basename(snapshot_path))
                label = ('存档 %s-%s-%s %s:%s' % (m.group(1)[:4], m.group(1)[4:6],
                                                  m.group(1)[6:8], m.group(2)[:2],
                                                  m.group(2)[2:4])) if m else '存档'
            lbl = os.path.join(NSM_DIR, 'nsSGv1_g%d.ini' % slot)
            with open(lbl, 'wb') as f:
                f.write((label + '\r\n').encode('gbk'))
            msg += '\n标签：%s' % label
        return True, msg
    except Exception as e:
        return False, '写入失败：%s' % e


# ---------------------------------------------------------------- 命令行模式

def human_size(n):
    return '%.1f KB' % (n / 1024.0) if n < 1048576 else '%.2f MB' % (n / 1048576.0)


def cli_list():
    cur = current_save_info()
    print('当前存档:', ('%d B  %s' % (cur['size'],
          datetime.fromtimestamp(cur['mtime']).strftime('%Y-%m-%d %H:%M:%S')))
          if cur else '**不存在**')
    print('游戏进程:', '运行中' if process_running() else '未运行')
    print()
    snaps = list_snapshots()
    print('快照共 %d 份：' % len(snaps))
    for i, (_mt, ts, sz, reason, p, name) in enumerate(snaps):
        ok_i, why = sol_integrity(p)
        print('  %2d) %s  %9s  %-4s  %-10s  %s'
              % (i + 1, ts, human_size(sz), '正常' if ok_i else '损坏', reason, name))
    return 0


def resolve_snapshot(token):
    snaps = list_snapshots()
    if not snaps:
        return None
    if token in (None, 'latest', '最新'):
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
    ap.add_argument('--restore', metavar='快照')
    ap.add_argument('--restore-slot', type=int, metavar='槽号')
    ap.add_argument('--snapshot', metavar='快照')
    ap.add_argument('--label')
    ap.add_argument('--yes', action='store_true')
    ap.add_argument('--kill-game', action='store_true')
    ap.add_argument('--config', metavar='FILE', help='指定 config.json 路径')
    a = ap.parse_args(argv)

    if a.list:
        return cli_list()

    if a.status:
        ok, s = task_status()
        print('计划任务:', ('存在，状态 %s' % s) if ok else s)
        cur = current_save_info()
        print('当前存档:', ('%d B' % cur['size']) if cur else '**不存在**')
        print('游戏进程:', '运行中' if process_running() else '未运行')
        print('快照份数:', len(list_snapshots()))
        return 0

    if a.restore_slot is not None:
        p = resolve_snapshot(a.snapshot)
        if not p:
            print('找不到快照')
            return 1
        ok, msg = restore_to_slot(p, a.restore_slot, a.label)
        print(msg)
        return 0 if ok else 1

    if a.restore:
        p = resolve_snapshot(a.restore)
        if not p:
            print('找不到快照: %s' % a.restore)
            return 1
        if not a.yes:
            print('将把 %s 回档为当前存档。' % os.path.basename(p))
            print('回车继续，输入其他内容取消。')
            try:
                if input().strip():
                    print('已取消')
                    return 1
            except EOFError:
                print('非交互环境，需加 --yes')
                return 1
        ok, msg = restore_to_save(p, allow_kill=a.kill_game)
        print(msg)
        return 0 if ok else 1

    return None      # 无 CLI 参数 → 走 GUI


# ---------------------------------------------------------------- GUI

def run_gui(selftest_ms=None):
    r"""启动图形界面。

    selftest_ms 不为 None 时：构建完整界面后，在指定毫秒后自动关闭。
    用途：验证界面能否成功构建。`.pyw` 由 pyw.exe 启动时没有控制台，
    任何 tkinter 报错都会静默失败（用户双击后毫无反应），
    所以必须有这样一个可自动跑完的构建自检入口。
    """
    import tkinter as tk
    from tkinter import ttk, messagebox, simpledialog

    root = tk.Tk()
    root.title(APP_TITLE)
    root.geometry('980x600')
    root.minsize(820, 480)

    # 顶部：当前状态
    top = ttk.Frame(root, padding=(10, 8))
    top.pack(fill='x')
    cur_var = tk.StringVar()
    watch_var = tk.StringVar()
    ttk.Label(top, textvariable=cur_var, font=('Consolas', 10)).pack(anchor='w')
    ttk.Label(top, textvariable=watch_var, font=('Consolas', 9),
              foreground='#666').pack(anchor='w', pady=(2, 0))

    # 中部：快照列表
    mid = ttk.Frame(root, padding=(10, 0))
    mid.pack(fill='both', expand=True)
    cols = ('time', 'size', 'integrity', 'reason', 'name')
    tv = ttk.Treeview(mid, columns=cols, show='headings', height=18)
    for c, t, w in (('time', '时间', 180), ('size', '大小', 85), ('integrity', '完整', 60),
                    ('reason', '触发', 100), ('name', '文件名', 470)):
        tv.heading(c, text=t)
        tv.column(c, width=w, anchor='w' if c in ('name', 'time') else 'center')
    vs = ttk.Scrollbar(mid, orient='vertical', command=tv.yview)
    tv.configure(yscrollcommand=vs.set)
    tv.pack(side='left', fill='both', expand=True)
    vs.pack(side='right', fill='y')
    tv.tag_configure('newest', background='#2d4d2d', foreground='#ffffff')
    tv.tag_configure('bad', background='#5a2020', foreground='#ffffff')

    # 底部：操作
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
                      values=(ts, human_size(sz), '正常' if ok_i else '损坏', reason, name),
                      tags=tags)
        cur = current_save_info()
        if cur:
            cur_var.set('当前存档：%d 字节  %s    SHA256 %s' % (
                cur['size'],
                datetime.fromtimestamp(cur['mtime']).strftime('%Y-%m-%d %H:%M:%S'),
                cur['sha256'][:16]))
        else:
            cur_var.set('当前存档：**不存在**（已被删除，需要回档）')
        ok, s = task_status()
        watch_var.set('自动备份监视器：%s' % (('运行中 / %s' % s) if ok else '未注册')
                      + '　｜　游戏进程：' + ('运行中' if process_running() else '未运行')
                      + '　｜　快照 %d 份' % len(state['snaps']))
        if state['snaps']:
            tv.selection_set(state['snaps'][0][5])
            if n_bad:
                hint_var.set('提示：有 %d 份快照结构校验未通过（红色行），回档时会被拦截，请选其他份。'
                             % n_bad)
            else:
                hint_var.set('提示：列表第一项是"死亡前最后一份"，全部快照结构校验均通过。'
                             '双击任意一项即可回档。')

    def selected():
        sel = tv.selection()
        if not sel:
            messagebox.showinfo('未选择', '请先在列表中选择一份快照。')
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
                    '游戏正在运行',
                    '检测到 NEOScavenger.exe 正在运行。\n\n'
                    'Flash 把存档保存在内存中，游戏退出时会覆盖 nsSGv1.sol，\n'
                    '此时回档会被抹掉。\n\n'
                    '是否结束游戏进程后继续回档？'):
                return
            kill = True
        if not messagebox.askyesno(
                '确认回档',
                '把下面这份快照设为当前存档？\n\n'
                '时间：%s\n大小：%s\n触发：%s\n文件：%s\n\n'
                '（回档前的当前存档会自动另存一份）' % (
                    info[1], human_size(info[2]), info[3], info[5])):
            return
        ok, msg = restore_to_save(p, allow_kill=kill)
        (messagebox.showinfo if ok else messagebox.showerror)(
            '回档成功' if ok else '回档失败', msg)
        refresh()

    def do_slot(slot):
        p = selected()
        if not p:
            return
        info = [s for s in state['snaps'] if s[4] == p][0]
        label = None
        if slot != 0:
            label = simpledialog.askstring(
                '槽位描述', '给槽 %d 输入一个描述（留空则按时间自动生成）：' % slot,
                initialvalue='存档 %s' % info[1][5:16], parent=root)
            if label is None:
                return
            label = label.strip() or None
        ok, msg = restore_to_slot(p, slot, label)
        (messagebox.showinfo if ok else messagebox.showerror)(
            '已写入槽位' if ok else '写入失败', msg)
        refresh()

    def open_folder():
        try:
            os.startfile(BACKUP_ROOT)
        except Exception as e:
            messagebox.showerror('打开失败', str(e))

    def restart_watch():
        restart_watcher()
        time.sleep(1)
        refresh()
        messagebox.showinfo('已重启', '已重启自动备份监视器。')

    ttk.Button(btns, text='⏪ 回档为当前存档', command=do_restore).pack(side='left')
    ttk.Button(btns, text='存入槽1', command=lambda: do_slot(1)).pack(side='left', padx=4)
    ttk.Button(btns, text='存入槽2', command=lambda: do_slot(2)).pack(side='left')
    ttk.Button(btns, text='存入槽3', command=lambda: do_slot(3)).pack(side='left', padx=4)
    ttk.Button(btns, text='存入Quicksave槽', command=lambda: do_slot(0)).pack(side='left')
    ttk.Button(btns, text='刷新', command=refresh).pack(side='right')
    ttk.Button(btns, text='打开备份文件夹', command=open_folder).pack(side='right', padx=4)
    ttk.Button(btns, text='重启监视器', command=restart_watch).pack(side='right')

    tv.bind('<Double-1>', lambda e: do_restore())
    refresh()

    if selftest_ms is not None:
        # 自检：把整棵控件树建出来、走一遍 refresh()，然后自动关闭
        root.update_idletasks()
        root.update()
        print('[selftest] 界面构建成功')
        print('[selftest] 快照行数 = %d' % len(tv.get_children()))
        print('[selftest] 窗口尺寸 = %dx%d' % (root.winfo_width(), root.winfo_height()))
        root.after(int(selftest_ms), root.destroy)

    root.mainloop()
    return 0


def main():
    rc = cli_main(sys.argv[1:])
    if rc is None:
        if not try_acquire_single_instance():
            if focus_existing_window():
                print('已有回档器在运行，已切到该窗口。')
            else:
                print('已有回档器在运行（但未找到窗口，可能正在启动中）。')
            return 0
        ms = os.environ.get('NSM_GUI_SELFTEST')
        return run_gui(int(ms) if ms else None)
    return rc


if __name__ == '__main__':
    sys.exit(main())

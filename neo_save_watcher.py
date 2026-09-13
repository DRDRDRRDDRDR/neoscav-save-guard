#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""NEO Scavenger 存档自动备份监视器

由 Windows 计划任务在登录时以隐藏窗口常驻运行（pythonw.exe）。

职责：
  1. 检测到 NEOScavenger.exe 进程启动 → 立刻快照当前存档（reason=start）
  2. 检测到 nsSGv1.sol 内容变化     → 立刻快照（reason=save，即游戏内 Quit and Save）
  3. 可选：把最新快照同步刷新到 NEO Save Manager 的 Quicksave 槽（nsSGv1_g0.sol）
  4. 滚动保留最近 KEEP_LAST 份快照，自动清理更旧的

设计要点：
  · 备份落在游戏目录之外（用户文档目录下），因此工具菜单里的 D)/W) 都删不到它
  · 所有读取都走 read_stable()：双次读取 + mtime/size/内容比对，
    避免在游戏正在写盘时读到半截文件
  · 不依赖任何第三方库（无 psutil），进程检测用 Win32 Toolhelp32 快照
  · 纯轮询，每 POLL_SEC 秒一次；睡眠期间几乎不占资源
  · 路径不硬编码：环境变量 → config.json → 自动探测（见 nsg_paths.py）

用法：
  pythonw.exe neo_save_watcher.py            # 常驻（计划任务用这个）
  python    neo_save_watcher.py --once       # 只跑一轮，便于测试
  python    neo_save_watcher.py --once --dry-run
  python    neo_save_watcher.py --check      # 打印路径解析与环境探测结果后退出
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

# ---------------------------------------------------------------- 路径解析
# 路径不再硬编码：由 nsg_paths 按 【环境变量 → config.json → 自动探测】 的顺序解析。
# 必须在模块级完成，因为 STATE_FILE / LOG_FILE 是下面直接推导出来的。
import nsg_paths

# 先手工扫一遍 --config（此刻 argparse 还没跑，见 nsg_paths.cli_config_arg 的说明）
_CONFIG_ARG = nsg_paths.cli_config_arg()
PATHS = nsg_paths.resolve(_CONFIG_ARG)
_CFG = nsg_paths.load_config(_CONFIG_ARG)

SAVE_DIR = PATHS['save_dir']          # 可能为 None，main() 会给出可读的报错
GAME_DIR = PATHS['game_dir']          # 可能为 None（只影响 NSM 槽位同步）
NSM_DIR = PATHS['nsm_dir']
BACKUP_ROOT = PATHS['backup_root']

SAVE_NAME = nsg_paths.SAVE_NAME
GAME_EXE = nsg_paths.GAME_EXE

# 运行时参数：config.json 里给了就用 config 的，否则用这里的默认值
POLL_SEC = int(_CFG.get('poll_sec', 10))                     # 轮询间隔（秒）
REFRESH_G0 = bool(_CFG.get('refresh_g0', True))              # 同步刷新 NSM 的 Quicksave 槽
SNAP_ON_GAME_START = bool(_CFG.get('snap_on_game_start', True))
LAUNCH_GUI_ON_GAME_START = bool(_CFG.get('launch_gui_on_game_start', True))
LOG_MAX_BYTES = 1 << 20  # 日志滚动的阈值（1 MiB）

HERE = os.path.dirname(os.path.abspath(__file__))
# 回档器 GUI 与本脚本同目录
GUI_SCRIPT = os.path.join(HERE, '存档回档器.pyw')

# --- 冷却：默认关闭（0）
# 曾经设为 30 秒，但实测发现这个设计有害：存档变化时若处在冷却期内会推迟快照，
# 而"死亡"会在推迟窗口内把存档文件直接删掉 —— 于是那份状态永久丢失。
# 实测证据：17:02:04 检测到 198,511 B，因距上次快照仅 21s < 30s 被推迟，
#           10 秒后文件消失，该状态无法找回。
# 因为保留策略已按时间分层、总量有硬上限，取消冷却不会有磁盘风险。
MIN_SNAPSHOT_GAP_SEC = 0

# --- 按时间分层的保留策略（比"只留最近 N 份"更符合直觉）
KEEP_ALL_SEC = 2 * 3600      # 最近 2 小时：逐份全留
HOURLY_UNTIL_SEC = 48 * 3600  # 2h ~ 48h：每小时留 1 份
DAILY_AFTER_SEC = 48 * 3600   # 超过 48h：每天留 1 份
MAX_TOTAL_FILES = 600         # 硬上限：总份数
MAX_TOTAL_MB = 300            # 硬上限：总体积（MB）

STATE_FILE = os.path.join(BACKUP_ROOT, 'watcher_state.json')
LOG_FILE = os.path.join(BACKUP_ROOT, 'watcher.log')


# ---------------------------------------------------------------- 工具
if sys.stdout is None:                       # pythonw.exe 下 stdout 为 None
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
    r"""稳定化读取：连续两次读取的 (mtime, size, 内容) 完全一致才返回。

    游戏运行中 Flash 可能正在写 nsSGv1.sol，直接读有拿到半截文件的风险。
    失败返回 None（宁可这轮不备份，也不写一份坏快照）。
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
    log('  !! read_stable 失败（文件持续变化），本轮跳过：%s' % path)
    return None


# ---------------------------------------------------------------- 进程检测
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
    """用 Toolhelp32 快照检测进程，避免反复 spawn tasklist"""
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


# ---------------------------------------------------------------- 带起 GUI
def _pythonw():
    r"""挑一个无控制台的解释器来跑 GUI。

    本脚本由计划任务用 pythonw.exe 承载，所以 sys.executable 通常已经是
    pythonw.exe；但若被人用 python.exe 手动跑，要换成同目录的 pythonw.exe，
    否则会顺带弹出一个黑框。注意这里必须用【宿主】的 Python（含 tkinter），
    沙箱内 WorkBuddy 的 managed Python 没有 tkinter。
    """
    exe = sys.executable or ''
    d, n = os.path.split(exe)
    if n.lower() == 'python.exe':
        cand = os.path.join(d, 'pythonw.exe')
        if os.path.isfile(cand):
            return cand
    return exe


def launch_gui():
    r"""带起回档器 GUI。单实例保护在 GUI 侧（命名 mutex），
    所以这里可以无条件 spawn —— 已在运行时会自动切到那个窗口。
    """
    if not os.path.isfile(GUI_SCRIPT):
        log('  !! 无法带起 GUI：脚本不存在 %s' % GUI_SCRIPT)
        return False
    exe = _pythonw()
    if not os.path.isfile(exe):
        log('  !! 无法带起 GUI：解释器不存在 %s' % exe)
        return False
    try:
        DETACHED_PROCESS = 0x00000008
        CREATE_NEW_PROCESS_GROUP = 0x00000200
        subprocess.Popen([exe, GUI_SCRIPT],
                         creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP,
                         close_fds=True)
        log('  ~~ 已带起回档器 GUI (%s)' % os.path.basename(exe))
        return True
    except Exception as e:
        log('  !! 带起 GUI 失败: %r' % e)
        return False


# ---------------------------------------------------------------- 状态
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
        log('  !! 状态写入失败: %s' % e)


# ---------------------------------------------------------------- 备份
def snapshot(data, reason, dry_run=False):
    """写入一份带时间戳的快照，返回快照路径"""
    os.makedirs(BACKUP_ROOT, exist_ok=True)
    ts = datetime.now().strftime('%Y%m%d-%H%M%S')
    dst = os.path.join(BACKUP_ROOT, 'nsSGv1_%s_%s.sol' % (ts, reason))
    if os.path.exists(dst):
        dst = os.path.join(BACKUP_ROOT, 'nsSGv1_%s_%s_%d.sol' % (ts, reason, os.getpid()))
    if dry_run:
        log('  [dry-run] 将写入快照 %s (%d B)' % (os.path.basename(dst), len(data)))
        return dst
    with open(dst, 'wb') as f:
        f.write(data)
    log('  >> 快照 %s  (%d B, %s)' % (os.path.basename(dst), len(data), reason))
    return dst


def prune(dry_run=False):
    r"""清理 auto\ 目录内的快照，绝不触碰其他目录。

    按时间分层保留：
      最近 KEEP_ALL_SEC            —— 逐份全留
      KEEP_ALL_SEC ~ HOURLY_UNTIL  —— 每小时留 1 份
      超过 DAILY_AFTER_SEC         —— 每天留 1 份
    再套一层硬上限（MAX_TOTAL_FILES / MAX_TOTAL_MB），超了就丢最旧的。
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

    # 硬上限：先按份数
    ordered = [n for _ts, n in entries]
    if len(keep) > MAX_TOTAL_FILES:
        newest_first = [n for n in ordered if n in keep]
        keep = set(newest_first[:MAX_TOTAL_FILES])
    # 再按总体积
    size_of = lambda n: os.path.getsize(os.path.join(BACKUP_ROOT, n))
    total = sum(size_of(n) for n in keep)
    limit = MAX_TOTAL_MB * 1024 * 1024
    if total > limit:
        for n in [x for x in ordered if x in keep][::-1]:   # 从最旧的开始丢
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
            log('  [dry-run] 将删除旧快照 %s' % n)
        else:
            try:
                os.remove(p)
                removed += 1
            except Exception as e:
                log('  !! 清理失败 %s: %s' % (n, e))
    if removed:
        log('  -- 清理 %d 份旧快照（保留 %d 份）' % (removed, len(keep)))


def newest_snapshot_hash():
    r"""读取 auto\ 中最新一份快照的哈希，用于跨进程重启去重"""
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
    """把最新快照同步到 NEO Save Manager 的 Quicksave 槽（g0）"""
    if not REFRESH_G0:
        return
    if not NSM_DIR:
        return                     # 没探测到游戏目录，跳过（非致命）
    nsm = NSM_DIR
    if not os.path.isdir(nsm):
        return
    dst = os.path.join(nsm, 'nsSGv1_g0.sol')
    if dry_run:
        log('  [dry-run] 将刷新 Quicksave 槽 -> %s' % dst)
        return
    try:
        with open(snapshot_path, 'rb') as s, open(dst, 'wb') as d:
            d.write(s.read())
        log('  ~~ 已刷新 Quicksave 槽 (g0)')
    except Exception as e:
        log('  !! 刷新 Quicksave 槽失败: %s' % e)


# ---------------------------------------------------------------- 主循环
def cycle(st, dry_run=False):
    """一个轮询周期"""
    src = os.path.join(SAVE_DIR, SAVE_NAME)
    new_snapshot = None

    running = process_running()
    if running is None:
        log('  !! 进程检测失败，跳过进程逻辑')

    # 跨重启去重：拿 auto\ 里最新一份快照的哈希兜底
    baseline = st.get('last_hash') or newest_snapshot_hash()

    # --- 1) 游戏刚启动 → 带起 GUI + 快照
    # 注意用「状态翻转」而非「当前正在运行」来判断：st['game_was_running'] 在每轮
    # 末尾才更新，所以一次启动只会命中一个周期，不会反复 spawn。
    if running and st.get('game_was_running') is False:
        if LAUNCH_GUI_ON_GAME_START and not dry_run:
            launch_gui()
        if SNAP_ON_GAME_START:
            data = read_stable(src)
            if data:
                h = sha256_bytes(data)
                if h != baseline:
                    log('检测到游戏启动 -> 快照当前存档')
                    new_snapshot = snapshot(data, 'start', dry_run)
                    st['last_hash'] = h
                    st['last_snapshot_ts'] = time.time()
                else:
                    log('检测到游戏启动，但存档与上次快照一致，跳过')

    # --- 2) 存档内容变化 → 快照
    # 冷却默认关闭：推迟快照会在"死亡删档"场景下永久丢失该状态，见常量处注释。
    data = read_stable(src)
    if data is None:
        # 只在状态翻转时记一次，否则存档缺失会每 10 秒刷屏
        if not st.get('save_missing'):
            log('!! 存档不可读或不存在（可能已被游戏删除）')
            st['save_missing'] = True
    else:
        if st.get('save_missing'):
            log('存档已重新出现')
            st['save_missing'] = False
        h = sha256_bytes(data)
        baseline = st.get('last_hash') or newest_snapshot_hash()
        if h != baseline:
            since = time.time() - st.get('last_snapshot_ts', 0)
            if MIN_SNAPSHOT_GAP_SEC and since < MIN_SNAPSHOT_GAP_SEC:
                # 不更新 last_hash，下轮重试；只推迟，不丢事件
                log('存档已变化 (%d B)，距上次快照仅 %.0fs < 冷却 %ds，本轮推迟'
                    % (len(data), since, MIN_SNAPSHOT_GAP_SEC))
            else:
                log('检测到存档变化 (%d B) -> 快照' % len(data))
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
    ap.add_argument('--once', action='store_true', help='只跑一轮后退出')
    ap.add_argument('--dry-run', action='store_true', help='不写任何文件')
    ap.add_argument('--check', action='store_true', help='打印环境探测结果后退出')
    ap.add_argument('--config', metavar='FILE', help='指定 config.json 路径')
    ap.add_argument('--interval', type=int, default=POLL_SEC)
    a = ap.parse_args()

    if a.check:
        print(nsg_paths.describe(PATHS))
        print('=== 目录与进程 ===')
        print('存档目录存在      :', bool(SAVE_DIR) and os.path.isdir(SAVE_DIR))
        print('存档文件存在      :', bool(SAVE_DIR) and os.path.isfile(
            os.path.join(SAVE_DIR, SAVE_NAME)) if SAVE_DIR else False)
        print('游戏目录存在      :', bool(GAME_DIR) and os.path.isdir(GAME_DIR))
        print('NSM 目录存在      :', bool(NSM_DIR) and os.path.isdir(NSM_DIR))
        print('备份根目录        :', BACKUP_ROOT, '->',
              '存在' if os.path.isdir(BACKUP_ROOT) else '待建')
        print('游戏进程运行中    :', process_running())
        if SAVE_DIR:
            p = os.path.join(SAVE_DIR, SAVE_NAME)
            if os.path.isfile(p):
                s = os.stat(p)
                print('当前存档          : %d B  %s' % (
                    s.st_size,
                    datetime.fromtimestamp(s.st_mtime).strftime('%Y-%m-%d %H:%M:%S')))
        return

    if not SAVE_DIR:
        print('!! 未能定位存档目录，无法继续。')
        print(nsg_paths.describe(PATHS))
        print('\n请编辑 config.json 显式填写 save_dir，例如：')
        print('  {')
        print(r'    "save_dir": "C:\\Users\\<你>\\AppData\\Roaming\\Macromedia\\'
              r'Flash Player\\#SharedObjects\\XXXXXXXX\\localhost\\'
              r'Program Files (x86)\\Steam\\steamapps\\common\\'
              r'NEO Scavenger\\NEOScavenger.exe"')
        print('  }')
        sys.exit(2)

    st = load_state()
    if a.once:
        cycle(st, a.dry_run)
        if not a.dry_run:
            save_state(st)
        return

    log('=== 监视器启动 (interval=%ds, cooldown=%ds, 保留: 2h内全留/48h内每小时/之后每天, g0=%s, 带起GUI=%s) ==='
        % (a.interval, MIN_SNAPSHOT_GAP_SEC, REFRESH_G0, LAUNCH_GUI_ON_GAME_START))
    log('    存档目录: %s [%s]' % (SAVE_DIR, PATHS['source'].get('save_dir')))
    log('    备份目录: %s [%s]' % (BACKUP_ROOT, PATHS['source'].get('backup_root')))
    st = cycle(st, False)          # 启动即刻做一次
    save_state(st)
    while True:
        try:
            time.sleep(a.interval)
            st = cycle(st, False)
            save_state(st)
        except KeyboardInterrupt:
            log('收到中断，退出')
            return
        except Exception as e:
            log('!! 周期异常: %r' % e)
            time.sleep(a.interval)


if __name__ == '__main__':
    main()

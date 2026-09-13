# -*- coding: utf-8 -*-
r"""NEO Scavenger - 把当前存档播种进 NEO Save Manager 的指定槽位。

用途：工具首次安装时三个槽都是 "Empty Slot"，本脚本可提前把当前存档
写入某个槽，使存档立刻处于受保护状态。

复现批处理行为：
    copy "%savedir%nsSGv1.sol" "%gamedir%\NSM\nsSGv1_g<slot>.sol"
    echo <label>> "%gamedir%\NSM\nsSGv1_g<slot>.ini"

用法：
    python seed_slot.py                       # 自动探测路径，播种槽1，标签自动生成
    python seed_slot.py --slot 2
    python seed_slot.py --label "第三章 医院前"
    python seed_slot.py --game "D:\Games\NEO Scavenger" --saves "<存档目录>"
    python seed_slot.py --dry-run             # 只检查不写入

槽位约定：
    0 = Quicksave 槽（nsSGv1_g0.sol，对应菜单 R/S）
    1..3 = 三个命名槽（nsSGv1_g1..g3.sol，对应菜单 1/2/3 与 A/B/C）

说明：本脚本自带一套路径探测（find_savedir / find_gamedir），
与 nsg_paths.py 的能力重叠 —— 保留独立实现是为了让它可以单文件拿走使用。
若两处探测行为不一致，**以 nsg_paths.py 为准**。
"""
import argparse
import hashlib
import os
import subprocess
import sys
import time

ANCHORS = ('nsTest.sol', 'nsSGv1.sol')          # 用于反查存档目录的锚点文件
SAVE_NAME = 'nsSGv1.sol'
# cmd 特殊字符：标签会被 echo ... Game1 (%game1name%) 包裹，含 ) 会破坏语句
FORBIDDEN = set('&|<>^%()')


def appdata_dir():
    r"""解析 %APPDATA%。

    注意：沙箱内 Bash 启动的 Python **可能没有 APPDATA 环境变量**，
    因此必须多级回退（实测本机 Bash 通道下 APPDATA 为 None）。
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
    """定位 Flash SharedObject 存档目录（以锚点文件匹配，不猜随机串目录名）"""
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
    # 优先取含真实存档的那个
    for h in hits:
        if os.path.isfile(os.path.join(h, SAVE_NAME)):
            return h
    return hits[0]


def find_gamedir(savedir):
    r"""从存档目录反推游戏目录。

    Flash 的 SharedObject 路径形如：
        %APPDATA%\...\#SharedObjects\<随机串>\localhost\<游戏路径去掉盘符>\<exe名>
    注意「去掉盘符」——`C:` 被剥掉了，因此不能直接拼接，必须跨盘符探测。
    例：...\localhost\Program Files (x86)\Steam\steamapps\common\NEO Scavenger\NEOScavenger.exe
    """
    parts = savedir.split(os.sep)
    try:
        i = next(i for i, p in enumerate(parts) if p.startswith('#SharedObjects'))
    except StopIteration:
        return None
    rest = parts[i + 3:]                      # 跳过 #SharedObjects\<随机串>\localhost
    if len(rest) < 2:
        return None
    exe_name = rest[-1]                       # 末段是 exe 文件名
    rel_dir = os.sep.join(rest[:-1])          # 其余是 exe 所在目录（不含盘符）
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
        raise SystemExit('标签含 cmd 特殊字符 %s，会导致批处理 echo 语句出错，请改掉' % ''.join(bad))
    try:
        label.encode('gbk')
    except UnicodeEncodeError:
        raise SystemExit('标签含 GBK 无法表示的字符，控制台会乱码')


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
    ap.add_argument('--label', default=None, help='槽位描述，默认 "存档 YYYY-MM-DD HH:MM"')
    ap.add_argument('--game', default=None, help='游戏目录（覆盖自动探测）')
    ap.add_argument('--saves', default=None, help='存档目录（覆盖自动探测）')
    ap.add_argument('--dry-run', action='store_true')
    a = ap.parse_args()

    print('=== 1. 定位目录 ===')
    savedir = find_savedir(a.saves)
    if not savedir:
        raise SystemExit('未能定位存档目录，请用 --saves 指定')
    gamedir = a.game or find_gamedir(savedir)
    if not gamedir or not os.path.isdir(gamedir):
        raise SystemExit('未能定位游戏目录（推得 %r），请用 --game 指定' % gamedir)
    print('  存档目录 :', savedir)
    print('  游戏目录 :', gamedir)

    src = os.path.join(savedir, SAVE_NAME)
    if not os.path.isfile(src):
        raise SystemExit('当前存档不存在：%s（游戏是否已 "Quit and Save"？）' % src)
    nsm = os.path.join(gamedir, 'NSM')
    if not os.path.isdir(nsm):
        raise SystemExit('NSM 目录不存在：%s（NEO Save Manager 装了吗？）' % nsm)

    print()
    print('=== 2. 前置条件 ===')
    st = os.stat(src)
    print('  源存档      : %d B  mtime %s' % (
        st.st_size, time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(st.st_mtime))))
    running = game_running()
    print('  游戏进程    : %s' % ('运行中 ⚠' if running else '未运行 ✓' if running is False else '检测失败'))
    if running:
        print('    !! 建议先完全退出游戏，否则可能拷贝到半写入的存档')

    dst = os.path.join(nsm, 'nsSGv1_g%d.sol' % a.slot)
    lbl = os.path.join(nsm, 'nsSGv1_g%d.ini' % a.slot)
    print('  目标槽位    :', os.path.basename(dst))
    for p in (dst, lbl):
        print('    %-22s %s' % (os.path.basename(p),
              ('已存在 %d B（将被覆盖）' % os.path.getsize(p)) if os.path.exists(p) else '不存在'))

    label = a.label or time.strftime('存档 %Y-%m-%d %H:%M', time.localtime(st.st_mtime))
    validate_label(label)
    print('  标签        : %r  -> GBK %d 字节' % (label, len(label.encode('gbk'))))

    if a.dry_run:
        print()
        print('--dry-run：未做任何写入')
        return

    print()
    print('=== 3. 播种 ===')
    src_h = sha256(src)
    with open(src, 'rb') as f:
        data = f.read()
    with open(dst, 'wb') as f:
        f.write(data)
    dst_h = sha256(dst)
    print('  源 SHA256 :', src_h)
    print('  槽 SHA256 :', dst_h)
    print('  哈希一致  :', src_h == dst_h)
    if src_h != dst_h:
        raise SystemExit('哈希不一致，复制失败')

    with open(lbl, 'wb') as f:
        f.write((label + '\r\n').encode('gbk'))
    raw = open(lbl, 'rb').read()
    print('  %s = %r  (BOM=%s)' % (os.path.basename(lbl), raw, raw[:3] == b'\xef\xbb\xbf'))

    print()
    print('=== 4. NSM 目录最终内容 ===')
    for fn in sorted(os.listdir(nsm)):
        print('  %8d B  %s' % (os.path.getsize(os.path.join(nsm, fn)), fn))
    print()
    print('完成。启动 NEOSaveManager.bat，按 %d 即可从该槽读档。' % a.slot)


if __name__ == '__main__':
    main()

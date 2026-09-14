# -*- coding: utf-8 -*-
r"""Minimal bilingual support (standard library only).

Source strings (msgids) are **English**. ``tr()`` returns the Chinese text
when the active language is Chinese, and the original English otherwise.

Language resolution (highest priority first):

  1. NSG_LANG environment variable (en / zh)
  2. "lang" key in config.json (next to these scripts, or --config <file>)
  3. DEFAULT_LANG below (en)

This module is deliberately self-contained: it does NOT import nsg_paths,
because nsg_paths imports this module for its own messages (import cycle).
"""

import json
import os
import sys

DEFAULT_LANG = 'en'
SUPPORTED = ('en', 'zh')
HERE = os.path.dirname(os.path.abspath(__file__))


def cli_config_arg(argv=None):
    r"""Scan argv for --config before argparse runs (module-level use)."""
    argv = list(sys.argv[1:] if argv is None else argv)
    for i, a in enumerate(argv):
        if a == '--config' and i + 1 < len(argv):
            return argv[i + 1]
        if a.startswith('--config='):
            return a.split('=', 1)[1]
    return None


def _config_file(cli_arg=None):
    if cli_arg:
        return cli_arg
    return os.path.join(HERE, 'config.json')


def _config(cli_arg=None):
    p = _config_file(cli_arg)
    try:
        with open(p, encoding='utf-8-sig') as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def active_lang(cli_arg=None):
    v = (os.environ.get('NSG_LANG') or '').strip().lower()
    if v in SUPPORTED:
        return v
    v = str(_config(cli_arg).get('lang') or '').strip().lower()
    if v in SUPPORTED:
        return v
    return DEFAULT_LANG


LANG = active_lang(cli_config_arg())


def tr(s):
    r"""Translate one msgid. Non-strings and unknown msgids pass through."""
    if not isinstance(s, str) or LANG != 'zh':
        return s
    return _ZH.get(s, s)


def untranslated(msgids):
    r"""Return the msgids with no Chinese entry (audit helper for lang=zh)."""
    return [m for m in msgids if isinstance(m, str) and m not in _ZH]


# English msgid -> Chinese text. Kept sorted for easy diffing.
_ZH = {
    '\n=== NEO Save Manager slots ===': '\n=== NEO Save Manager 槽位 ===',
    '\n=== Registering scheduled task ===': '\n=== 注册计划任务 ===',
    '\n=== Snapshots ===': '\n=== 备份快照 ===',
    '\n=== Starting now (no need to log off and on) ===': '\n=== 立即启动（无需等待重新登录） ===',
    '\n=== Task status ===': '\n=== 任务状态 ===',
    '\n=== Watcher processes ===': '\n=== 监视器进程 ===',
    '\nBackup files were not deleted; they are still in: %s': '\n备份文件未被删除，仍在: %s',
    '\nEdit config.json and set save_dir explicitly, for example:': '\n请编辑 config.json 显式填写 save_dir，例如：',
    '\nLabel: %s': '\n标签：%s',
    '\nRequired conditions not met, aborting.': '\n必需条件不满足，终止。',
    '     !! Fully quit the game first, otherwise you may copy a half-written save': '    !! 建议先完全退出游戏，否则可能拷贝到半写入的存档',
    '     -> Set it explicitly in config.json, or pass --config <file>': '     → 请在 config.json 里显式填写，或用 --config 指定配置文件',
    '     ...   %d total': '    ...   共 %d 份',
    '     No python/pythonw process found (the watcher may not be running)': '    未发现 python/pythonw 进程（监视器可能未运行）',
    '     backup dir: %s [%s]': '    备份目录: %s [%s]',
    '     newest %9d B  %s': '    最新  %9d B  %s',
    '     save dir  : %s [%s]': '    存档目录: %s [%s]',
    '    "save_dir": "C:\\\\Users\\\\<you>\\\\AppData\\\\Roaming\\\\Macromedia\\\\Flash Player\\\\#SharedObjects\\\\XXXXXXXX\\\\localhost\\\\Program Files (x86)\\\\Steam\\\\steamapps\\\\common\\\\NEO Scavenger\\\\NEOScavenger.exe"': '    "save_dir": "C:\\\\Users\\\\<你>\\\\AppData\\\\Roaming\\\\Macromedia\\\\Flash Player\\\\#SharedObjects\\\\XXXXXXXX\\\\localhost\\\\Program Files (x86)\\\\Steam\\\\steamapps\\\\common\\\\NEO Scavenger\\\\NEOScavenger.exe"',
    '   !! Cannot determine the current user (USERNAME / COMPUTERNAME are empty), aborting.': '  !! 无法确定当前用户名（USERNAME / COMPUTERNAME 均为空），终止。',
    '   !! Cannot launch the GUI: interpreter not found %s': '  !! 无法带起 GUI：解释器不存在 %s',
    '   !! Cannot launch the GUI: script not found %s': '  !! 无法带起 GUI：脚本不存在 %s',
    '   !! Failed to launch the GUI: %r': '  !! 带起 GUI 失败: %r',
    '   !! Failed to refresh the Quicksave slot: %s': '  !! 刷新 Quicksave 槽失败: %s',
    '   !! Failed to write state: %s': '  !! 状态写入失败: %s',
    '   !! Process detection failed, skipping process logic': '  !! 进程检测失败，跳过进程逻辑',
    '   !! Prune failed %s: %s': '  !! 清理失败 %s: %s',
    '   !! Unresolved items: %s': '  !! 未能确定的项: %s',
    '   !! read_stable failed (file keeps changing), skipping this cycle: %s': '  !! read_stable 失败（文件持续变化），本轮跳过：%s',
    '   %-12s %-4s %s   (optional; only affects NSM slot sync)': '  %-12s %-4s %s   (可选，缺失只影响 NSM 槽位同步)',
    '   -- pruned %d old snapshot(s), %d kept': '  -- 清理 %d 份旧快照（保留 %d 份）',
    '   /Run return code:': '  /Run 返回码:',
    '   >> snapshot %s  (%d B, %s)': '  >> 快照 %s  (%d B, %s)',
    '   All task names failed to register': '  全部任务名均注册失败',
    '   Registering task name %r failed, trying the next one...': '  用任务名 %r 注册失败，尝试下一个…',
    '   [dry-run] would delete old snapshot %s': '  [dry-run] 将删除旧快照 %s',
    '   [dry-run] would refresh the Quicksave slot -> %s': '  [dry-run] 将刷新 Quicksave 槽 -> %s',
    '   [dry-run] would write snapshot %s (%d B)': '  [dry-run] 将写入快照 %s (%d B)',
    '   backup dir  : %s': '  备份目录 : %s',
    '   config file : %s %s': '  配置文件 : %s %s',
    '   dir         : %s': '  目录    : %s',
    '   directory does not exist yet:': '  目录尚不存在:',
    '   game dir    :': '  游戏目录 :',
    '   game dir    : %s': '  游戏目录 : %s',
    '   game process: %s': '  游戏进程    : %s',
    '   hash match  :': '  哈希一致  :',
    '   label       : %r  -> GBK %d bytes': '  标签        : %r  -> GBK %d 字节',
    '   not registered (use install)': '  未注册（用 install 注册）',
    '   save dir    :': '  存档目录 :',
    '   save dir    : %s': '  存档目录 : %s',
    '   slot SHA256 :': '  槽 SHA256 :',
    '   snapshots   : %d   %.2f MB total': '  快照份数: %d   合计 %.2f MB',
    '   source SHA256 :': '  源 SHA256 :',
    '   source save : %d B  mtime %s': '  源存档      : %d B  mtime %s',
    '   target slot :': '  目标槽位    :',
    '   task name: %s': '  任务名: %s',
    '   task: %s': '  任务: %s',
    '   wrote label: %s -> %s': '  写入标签: %s -> %s',
    '   wrote slot %d: %s (%d B)': '  写入槽%d: %s (%d B)',
    '   ~~ Quicksave slot refreshed (g0)': '  ~~ 已刷新 Quicksave 槽 (g0)',
    '   ~~ Rollback GUI launched (%s)': '  ~~ 已带起回档器 GUI (%s)',
    '  |  %d snapshot(s)': '\u3000｜\u3000快照 %d 份',
    '  |  game process: ': '\u3000｜\u3000游戏进程：',
    ' !! Could not locate the save directory, cannot continue.': '!! 未能定位存档目录，无法继续。',
    ' !! Cycle error: %r': '!! 周期异常: %r',
    ' !! Save is unreadable or missing (the game may have deleted it)': '!! 存档不可读或不存在（可能已被游戏删除）',
    ' (exists)': '(存在)',
    ' (missing)': '(不存在)',
    ' (not found)': '(未找到)',
    ' **MISSING**': '**不存在**',
    ' --dry-run: nothing was written': '--dry-run：未做任何写入',
    ' === 1. Locate directories ===': '=== 1. 定位目录 ===',
    ' === 2. Prerequisites ===': '=== 2. 前置条件 ===',
    ' === 3. Seeding ===': '=== 3. 播种 ===',
    ' === 4. Final contents of the NSM directory ===': '=== 4. NSM 目录最终内容 ===',
    ' === Deleting task: %s ===': '=== 删除任务: %s ===',
    ' === Directories and processes ===': '=== 目录与进程 ===',
    ' === Path resolution ===': '=== 路径解析 ===',
    ' === Preflight checks ===': '=== 前置校验 ===',
    ' === Scheduled task ===': '=== 计划任务 ===',
    ' === Watcher started (interval=%ds, cooldown=%ds, retention: all within 2h / hourly up to 48h / daily after, g0=%s, launchGUI=%s) ===': '=== 监视器启动 (interval=%ds, cooldown=%ds, 保留: 2h内全留/48h内每小时/之后每天, g0=%s, 带起GUI=%s) ===',
    '%d snapshot(s):': '快照共 %d 份：',
    '%s will be restored as the current save.': '将把 %s 回档为当前存档。',
    'A rollback GUI is already running (its window was not found yet; it may still be starting).': '已有回档器在运行（但未找到窗口，可能正在启动中）。',
    'A rollback GUI is already running; focused its window.': '已有回档器在运行，已切到该窗口。',
    'Auto-backup watcher restarted.': '已重启自动备份监视器。',
    'Auto-backup watcher: %s': '自动备份监视器：%s',
    'Backed up the pre-rollback save -> %s': '已备份回档前的当前存档 → %s',
    'CORRUPT': '损坏',
    'Cancelled': '已取消',
    'Confirm rollback': '确认回档',
    'Copy to Quicksave slot': '存入Quicksave槽',
    'Copy to slot 1': '存入槽1',
    'Copy to slot 2': '存入槽2',
    'Copy to slot 3': '存入槽3',
    'Could not locate the game directory (derived %r); specify it with --game': '未能定位游戏目录（推得 %r），请用 --game 指定',
    'Could not locate the save directory; specify it with --saves': '未能定位存档目录，请用 --saves 指定',
    'Current save not found: %s (did the game "Quit and Save"?)': '当前存档不存在：%s（游戏是否已 "Quit and Save"？）',
    'Current save: %d bytes  %s    SHA256 %s': '当前存档：%d 字节  %s    SHA256 %s',
    'Current save: **MISSING** (deleted; a rollback is needed)': '当前存档：**不存在**（已被删除，需要回档）',
    'Done. Launch NEOSaveManager.bat and press %d to load from this slot.': '完成。启动 NEOSaveManager.bat，按 %d 即可从该槽读档。',
    'Ending the game process...': '正在结束游戏进程…',
    'Enter a label for slot %d (leave empty to generate one from the time):': '给槽 %d 输入一个描述（留空则按时间自动生成）：',
    'Failed to back up the current save; aborted for safety: %s': '备份当前存档失败，为安全起见已中止：%s',
    'Failed to end the game process; close it manually and try again.': '结束游戏进程失败，请手动关闭后重试。',
    'Failed to parse config.json (ignoring it): %s -> %r': 'config.json 解析失败（将忽略）: %s -> %r',
    'Failed to read the snapshot': '读取快照失败',
    'Failed to read the snapshot (the file keeps changing or is unreadable)': '读取快照失败（文件持续变化或不可读）',
    'Failed to write the save: %s': '写入存档失败：%s',
    'File': '文件名',
    'Game dir': '游戏目录',
    'Game start detected -> snapshot the current save': '检测到游戏启动 -> 快照当前存档',
    'Game start detected, but the save matches the last snapshot; skipping': '检测到游戏启动，但存档与上次快照一致，跳过',
    'Integrity': '完整',
    'Interrupted, exiting': '收到中断，退出',
    'NEO Scavenger Save Rollback': 'NEO Scavenger 存档回档器',
    'NEOScavenger.exe is running.\n\nFlash keeps the save in memory and overwrites nsSGv1.sol when the game exits,\nso a rollback now would be wiped.\n\nEnd the game process and continue with the rollback?': '检测到 NEOScavenger.exe 正在运行。\n\nFlash 把存档保存在内存中，游戏退出时会覆盖 nsSGv1.sol，\n此时回档会被抹掉。\n\n是否结束游戏进程后继续回档？',
    'NSM dir': 'NSM 目录',
    'NSM dir exists    :': 'NSM 目录存在      :',
    'NSM directory does not exist: %s': 'NSM 目录不存在：%s',
    'NSM directory does not exist: %s (is NEO Save Manager installed?)': 'NSM 目录不存在：%s（NEO Save Manager 装了吗？）',
    'No snapshot found: %s': '找不到快照: %s',
    'No snapshots in auto\\, run the watcher once first': 'auto\\ 中没有快照，先跑一轮监视器',
    'Note: %d snapshot(s) failed the structure check (red rows). They are refused on rollback, so pick another one.': '提示：有 %d 份快照结构校验未通过（红色行），回档时会被拦截，请选其他份。',
    'OK': '正常',
    'Open backup folder': '打开备份文件夹',
    'Post-write verification failed! source %s / target %s': '写入后校验失败！源 %s / 目标 %s',
    'Press Enter to continue, type anything else to cancel.': '回车继续，输入其他内容取消。',
    'Reason': '触发',
    'Refresh': '刷新',
    'Restart watcher': '重启监视器',
    'Restore the snapshot below as the current save?\n\nTime  : %s\nSize  : %s\nReason: %s\nFile  : %s\n\n(The current save is backed up automatically before the rollback.)': '把下面这份快照设为当前存档？\n\n时间：%s\n大小：%s\n触发：%s\n文件：%s\n\n（回档前的当前存档会自动另存一份）',
    'Rollback complete': '回档成功',
    'Rollback complete.\nTarget : %s\nSize   : %d bytes\nSHA-256: %s\n\nStart the game and choose Continue on the main menu to resume this progress.': '回档成功。\n目标：%s\n大小：%d 字节\nSHA-256：%s\n\n现在启动游戏，在主菜单选 Continue 即可续上这份进度。',
    'Rollback failed': '回档失败',
    'Run `python neo_save_watcher.py --check` first to see the path resolution details.': '可先运行 `python neo_save_watcher.py --check` 查看路径解析明细。',
    'Save': '存档',
    'Save %Y-%m-%d %H:%M': '存档 %Y-%m-%d %H:%M',
    'Save %s': '存档 %s',
    'Save %s-%s-%s %s:%s': '存档 %s-%s-%s %s:%s',
    'Save changed (%d B) -> snapshot': '检测到存档变化 (%d B) -> 快照',
    'Save changed (%d B) only %.0fs after the last snapshot < cooldown %ds, deferring this cycle': '存档已变化 (%d B)，距上次快照仅 %.0fs < 冷却 %ds，本轮推迟',
    'Save dir': '存档目录',
    'Save directory does not exist: %s': '存档目录不存在：%s',
    'Save reappeared': '存档已重新出现',
    'Scheduled task not found; run install first': '未找到计划任务，请先 install',
    'Select a snapshot in the list first.': '请先在列表中选择一份快照。',
    'Size': '大小',
    'Slot label': '槽位描述',
    'Slot number': '槽号',
    'Slot written': '已写入槽位',
    'Snapshot': '快照',
    'Snapshot file does not exist: %s': '快照文件不存在：%s',
    'Structure': '结构完整',
    'Task restarted: %s (return code %s)': '已重启任务: %s (返回码 %s)',
    'The game is running': '游戏正在运行',
    'The game is running, so the rollback was refused.\nFlash keeps the save in memory and overwrites nsSGv1.sol when the game exits,\nso a rollback now would be wiped. Fully quit the game first (or choose to end the process in the UI).': '游戏正在运行，已拒绝回档。\nFlash 会把存档保存在内存里，游戏退出时会覆盖 nsSGv1.sol，\n此时回档会被抹掉。请先完全退出游戏（或在界面中选择结束进程）。',
    'The label contains characters that GBK cannot represent; the console will show mojibake': '标签含 GBK 无法表示的字符，控制台会乱码',
    'The label contains cmd special character(s) %s, which breaks the batch echo statement; please change it': '标签含 cmd 特殊字符 %s，会导致批处理 echo 语句出错，请改掉',
    'This snapshot failed the structure check, so the rollback was refused:\n%s\n\nRestoring a corrupt snapshot would leave the game unable to read the save,\nso it is blocked. Please pick another snapshot.': '该快照结构校验未通过，已拒绝回档：\n%s\n\n回档一个损坏的快照会让游戏读不出存档，\n所以宁可先拦住。请换一份快照。',
    'Time': '时间',
    'Tip: the first row is the last snapshot before death, and every snapshot passed the structure check. Double-click any row to roll back.': '提示：列表第一项是"死亡前最后一份"，全部快照结构校验均通过。双击任意一项即可回档。',
    'Wrote slot %d: %s (%d bytes)': '已写入槽 %d：%s（%d 字节）',
    '[selftest] UI built successfully': '[selftest] 界面构建成功',
    '[selftest] snapshot rows = %d': '[selftest] 快照行数 = %d',
    '[selftest] window size = %dx%d': '[selftest] 窗口尺寸 = %dx%d',
    'backup root       :': '备份根目录        :',
    'bad magic (expected 00 bf, got %s)': '魔数异常（期望 00 bf，实际 %s）',
    'comma separated, default 1,0': '逗号分隔，默认 1,0',
    'config.json must contain an object at the top level (ignoring it): %s': 'config.json 顶层必须是对象（将忽略）: %s',
    'current save      : %d B  %s': '当前存档          : %d B  %s',
    'current save:': '当前存档:',
    'detection failed': '检测失败',
    'exists': '存在',
    'exists, %d B (will be overwritten)': '已存在 %d B（将被覆盖）',
    'exists, status %s': '存在，状态 %s',
    'failed to open': '打开失败',
    'file too small (<6 bytes)': '文件过小（<6 字节）',
    'game dir exists   :': '游戏目录存在      :',
    'game directory (overrides auto-detection)': '游戏目录（覆盖自动探测）',
    'game process:': '游戏进程:',
    'game running      :': '游戏进程运行中    :',
    'game_dir (optional; only affects NSM slot sync)': 'game_dir (可选：只影响 NSM 槽位同步)',
    'hash mismatch, copy failed': '哈希不一致，复制失败',
    'label : %s': '标签  : %s',
    'length field %d != filesize-6 = %d (likely truncated)': '长度字段 %d ≠ 文件大小-6 = %d（疑被截断）',
    'missing': '不存在',
    'newest': '最新',
    'no snapshot found': '找不到快照',
    'non-interactive environment; add --yes': '非交互环境，需加 --yes',
    'not registered': '未注册',
    'not running': '未运行',
    'not running ✓': '未运行 ✓',
    'nothing selected': '未选择',
    'path to config.json': '指定 config.json 路径',
    'print the environment probe results and exit': '打印环境探测结果后退出',
    'python not found': '未找到 python',
    'query failed: %s': '查询失败: %s',
    'read failed: %s': '读取失败：%s',
    'registered': '已注册',
    'restarted': '已重启',
    'run one cycle then exit': '只跑一轮后退出',
    'running': '运行中',
    'running / %s': '运行中 / %s',
    'running ⚠': '运行中 ⚠',
    'save dir exists   :': '存档目录存在      :',
    'save directory (overrides auto-detection)': '存档目录（覆盖自动探测）',
    'save file exists  :': '存档文件存在      :',
    'scheduled task:': '计划任务:',
    'slot label; defaults to "Save YYYY-MM-DD HH:MM"': '槽位描述，默认 "存档 YYYY-MM-DD HH:MM"',
    'snapshots:': '快照份数:',
    'source snapshot: %s (%d B)': '源快照: %s (%d B)',
    'to be created': '待建',
    'watcher script': '监视器脚本',
    'write failed': '写入失败',
    'write failed: %s': '写入失败：%s',
    'write nothing': '不写任何文件',
    '⏪ Restore as current save': '⏪ 回档为当前存档',
}

# -*- coding: utf-8 -*-
r"""nsg_paths —— NEO Scavenger 路径解析（存档目录 / 游戏目录 / 备份目录）

被 neo_save_watcher.py、autobackup_setup.py、save_rollback_gui.pyw 共用。

解析优先级（由高到低）：

  1. 环境变量     NSG_SAVE_DIR / NSG_GAME_DIR / NSG_BACKUP_ROOT
  2. 配置文件     config.json（与本模块同目录，或用 --config 指定）
  3. 自动探测     见 detect_save_dir()

自动探测的原理
--------------
Flash 把本地 SWF 的 SharedObject 放在：

    %APPDATA%\Macromedia\Flash Player\#SharedObjects\<随机串>\localhost\<exe 全路径>\

其中 `<exe 全路径>` 是游戏主程序的路径去掉盘符与冒号（例如
`Program Files (x86)\Steam\steamapps\common\NEO Scavenger\NEOScavenger.exe`）。

因此：

* 递归找到含 `nsSGv1.sol` 的目录 → 就是存档目录；
* 由该路径**反推**游戏目录：去掉末段（exe），再逐个盘符试拼直到目录存在。

这样换机器 / 换 Steam 库 / 换用户名都不需要改代码。
"""

import json
import os
import re
import sys

SAVE_NAME = 'nsSGv1.sol'
GAME_EXE = 'NEOScavenger.exe'
GAME_FOLDER = 'NEO Scavenger'

CONFIG_NAME = 'config.json'
HERE = os.path.dirname(os.path.abspath(__file__))

#: 解析过程中收集到的告警（例如 config.json 存在但格式错误）。
#: 调用方可自行决定是否展示；--check 会打印出来。
WARNINGS = []


def warn(msg):
    WARNINGS.append(msg)


# ------------------------------------------------------------------ 配置
def cli_config_arg(argv=None):
    r"""在 argparse 之前先扫一遍 --config <FILE> / --config=FILE。

    为什么需要：STATE_FILE / LOG_FILE 这类常量是模块级推导的，
    那时 argparse 还没跑。各脚本在导入期调用本函数即可拿到配置文件路径。
    """
    argv = list(sys.argv[1:] if argv is None else argv)
    for i, a in enumerate(argv):
        if a == '--config' and i + 1 < len(argv):
            return argv[i + 1]
        if a.startswith('--config='):
            return a.split('=', 1)[1]
    return None


def config_path(cli_arg=None):
    """配置文件路径：显式指定 > 脚本同目录 > 用户目录。"""
    if cli_arg:
        return cli_arg
    local = os.path.join(HERE, CONFIG_NAME)
    if os.path.isfile(local):
        return local
    alt = os.path.join(os.path.expanduser('~'), '.' + CONFIG_NAME.replace('.json', '') + '.json')
    if os.path.isfile(alt):
        return alt
    return local          # 不存在也返回默认位置，便于报错时提示


def load_config(cli_arg=None):
    """读 config.json；不存在返回 {}。格式错误会记入 WARNINGS。"""
    p = config_path(cli_arg)
    if not os.path.isfile(p):
        return {}
    try:
        with open(p, encoding='utf-8-sig') as f:
            data = json.load(f)
    except Exception as e:
        warn('config.json 解析失败（将忽略）: %s -> %r' % (p, e))
        return {}
    if not isinstance(data, dict):
        warn('config.json 顶层必须是对象（将忽略）: %s' % p)
        return {}
    return data


# ------------------------------------------------------------------ 探测
def _flash_roots():
    r"""Flash SharedObject 的候选根目录（新旧两套命名都试）。"""
    appdata = os.environ.get('APPDATA') or os.path.join(
        os.path.expanduser('~'), 'AppData', 'Roaming')
    return [
        os.path.join(appdata, 'Macromedia', 'Flash Player', '#SharedObjects'),
        os.path.join(appdata, 'Adobe', 'Flash Player', '#SharedObjects'),
    ]


def detect_save_dir():
    r"""递归查找含 nsSGv1.sol 的目录。

    有多个候选时优先选路径里含 "NEO Scavenger" 的（避免同名 .sol 误判）。
    找不到返回 None。
    """
    hits = []
    for root in _flash_roots():
        if not os.path.isdir(root):
            continue
        for dirpath, _dirnames, filenames in os.walk(root):
            if SAVE_NAME in filenames:
                hits.append(dirpath)
    if not hits:
        return None
    hits.sort(key=lambda p: (GAME_FOLDER.lower() not in p.lower(), len(p)))
    return hits[0]


def _steam_roots():
    """常见 Steam 安装根目录 + libraryfolders.vdf 里登记的额外库。"""
    roots = []
    for base in (r'C:\Program Files (x86)\Steam', r'C:\Program Files\Steam',
                 r'C:\Steam', r'D:\Steam', r'D:\SteamLibrary', r'E:\SteamLibrary'):
        if os.path.isdir(base):
            roots.append(base)
    for r in list(roots):
        vdf = os.path.join(r, 'steamapps', 'libraryfolders.vdf')
        if not os.path.isfile(vdf):
            continue
        try:
            with open(vdf, encoding='utf-8', errors='replace') as f:
                text = f.read()
        except Exception:
            continue
        for m in re.finditer(r'"path"\s*"([^"]+)"', text):
            p = m.group(1).replace('\\\\', '\\')
            if os.path.isdir(p) and p not in roots:
                roots.append(p)
    return roots


def detect_game_dir(save_dir=None):
    r"""探测游戏目录。

    先由存档目录反推（去掉末段再试各盘符），再退化为扫常见 Steam 库。
    找不到返回 None。
    """
    if save_dir:
        parts = [p for p in save_dir.replace('/', '\\').split('\\') if p]
        if parts and parts[-1].lower().endswith('.exe'):
            tail = parts[:-1]
        else:
            tail = parts
        for drive in 'CDEFGHIJKLMNOPQRSTUVWXYZ':
            cand = drive + ':\\' + '\\'.join(tail)
            if os.path.isdir(cand) and os.path.isfile(os.path.join(cand, GAME_EXE)):
                return cand
        # 反推失败也无妨：末段可能就是游戏目录名
        for drive in 'CDEFGHIJKLMNOPQRSTUVWXYZ':
            cand = drive + ':\\' + '\\'.join(parts)
            if os.path.isdir(cand) and os.path.isfile(os.path.join(cand, GAME_EXE)):
                return cand
    for root in _steam_roots():
        cand = os.path.join(root, 'steamapps', 'common', GAME_FOLDER)
        if os.path.isdir(cand):
            return cand
    return None


def default_backup_root():
    """默认备份目录：<用户文档>\\NEO Scavenger Save Guard\\auto"""
    docs = os.path.join(os.path.expanduser('~'), 'Documents')
    if not os.path.isdir(docs):
        docs = os.path.expanduser('~')
    return os.path.join(docs, 'NEO Scavenger Save Guard', 'auto')


# ------------------------------------------------------------------ 汇总
def resolve(cli_config=None):
    r"""解析出最终路径。

    返回 dict：
        save_dir / game_dir / backup_root      —— 已确认（可能为 None）
        nsm_dir                                 —— 游戏目录下的 NSM（可能为 None）
        source                                  —— 各项来源：config/env/detect/default
        missing                                 —— 需要人工补的键名列表
    """
    cfg = load_config(cli_config)
    src = {}

    def pick(key, env_key, detector=None, default=None):
        v = os.environ.get(env_key)
        if v:
            src[key] = 'env(%s)' % env_key
            return v
        v = cfg.get(key)
        if v:
            src[key] = 'config'
            return v
        if detector is not None:
            v = detector()
            if v:
                src[key] = 'detect'
                return v
        src[key] = 'default' if default else 'unresolved'
        return default

    save_dir = pick('save_dir', 'NSG_SAVE_DIR', detect_save_dir)
    backup_root = pick('backup_root', 'NSG_BACKUP_ROOT', None, default_backup_root())
    game_dir = pick('game_dir', 'NSG_GAME_DIR', lambda: detect_game_dir(save_dir))

    missing = []
    if not save_dir:
        missing.append('save_dir')
    if not game_dir:
        missing.append('game_dir (可选：只影响 NSM 槽位同步)')

    nsm_dir = os.path.join(game_dir, 'NSM') if game_dir else None

    return {
        'save_dir': save_dir,
        'game_dir': game_dir,
        'nsm_dir': nsm_dir,
        'backup_root': backup_root,
        'source': src,
        'missing': missing,
        'config_file': config_path(cli_config),
        'warnings': list(WARNINGS),
    }


def describe(res):
    """把 resolve() 的结果格式化成可直接打印的多行文本。"""
    lines = ['=== 路径解析 ===']
    lines.append('  存档目录 : %s' % (res['save_dir'] or '(未找到)'))
    lines.append('             [%s]' % res['source'].get('save_dir'))
    lines.append('  游戏目录 : %s' % (res['game_dir'] or '(未找到)'))
    lines.append('             [%s]' % res['source'].get('game_dir'))
    lines.append('  备份目录 : %s' % (res['backup_root'] or '(未找到)'))
    lines.append('             [%s]' % res['source'].get('backup_root'))
    lines.append('  配置文件 : %s %s' % (
        res['config_file'], '(存在)' if os.path.isfile(res['config_file']) else '(不存在)'))
    for w in res['warnings']:
        lines.append('  !! %s' % w)
    if res['missing']:
        lines.append('  !! 未能确定的项: %s' % ', '.join(res['missing']))
        lines.append('     → 请在 config.json 里显式填写，或用 --config 指定配置文件')
    return '\n'.join(lines)


if __name__ == '__main__':
    print(describe(resolve()))

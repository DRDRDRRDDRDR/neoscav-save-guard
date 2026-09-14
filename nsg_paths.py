# -*- coding: utf-8 -*-
r"""nsg_paths — NEO Scavenger path resolution (save dir / game dir / backup dir).

Shared by neo_save_watcher.py, autobackup_setup.py and save_rollback_gui.pyw.

Resolution priority (highest first):

  1. Environment  NSG_SAVE_DIR / NSG_GAME_DIR / NSG_BACKUP_ROOT
  2. Config file  config.json (next to this module, or via --config)
  3. Detection    see detect_save_dir()

How detection works
-------------------
Flash stores local SWF SharedObjects under:

    %APPDATA%\Macromedia\Flash Player\#SharedObjects\<random>\localhost\<full exe path>\

where ``<full exe path>`` is the game executable's path with the drive letter and
colon stripped, for example
``Program Files (x86)\Steam\steamapps\common\NEO Scavenger\NEOScavenger.exe``.

Therefore:

* recursively find the directory containing ``nsSGv1.sol`` — that is the save dir;
* **reverse-derive** the game dir from it: drop the last segment (the exe), then try
  each drive letter until a directory exists.

This way a different machine, Steam library or username needs no code changes.
"""

import json
import os
import re
import sys
from nsg_i18n import tr

SAVE_NAME = 'nsSGv1.sol'
GAME_EXE = 'NEOScavenger.exe'
GAME_FOLDER = 'NEO Scavenger'

CONFIG_NAME = 'config.json'
HERE = os.path.dirname(os.path.abspath(__file__))

#: Warnings collected during resolution (e.g. config.json exists but is malformed).
#: Callers decide whether to surface them; --check prints them.
WARNINGS = []


def warn(msg):
    WARNINGS.append(msg)


# ------------------------------------------------------------------ config
def cli_config_arg(argv=None):
    r"""Scan for --config <FILE> / --config=FILE before argparse runs.

    Why this is needed: constants such as STATE_FILE / LOG_FILE are derived at
    module level, which happens before argparse has run. Each script calls this
    function at import time to learn the config file path.
    """
    argv = list(sys.argv[1:] if argv is None else argv)
    for i, a in enumerate(argv):
        if a == '--config' and i + 1 < len(argv):
            return argv[i + 1]
        if a.startswith('--config='):
            return a.split('=', 1)[1]
    return None


def config_path(cli_arg=None):
    """Config file path: explicit > next to the script > user home dir."""
    if cli_arg:
        return cli_arg
    local = os.path.join(HERE, CONFIG_NAME)
    if os.path.isfile(local):
        return local
    alt = os.path.join(os.path.expanduser('~'), '.' + CONFIG_NAME.replace('.json', '') + '.json')
    if os.path.isfile(alt):
        return alt
    return local          # return the default location even if absent, for error messages


def load_config(cli_arg=None):
    """Read config.json; return {} when absent. Malformed input goes to WARNINGS."""
    p = config_path(cli_arg)
    if not os.path.isfile(p):
        return {}
    try:
        with open(p, encoding='utf-8-sig') as f:
            data = json.load(f)
    except Exception as e:
        warn(tr('Failed to parse config.json (ignoring it): %s -> %r') % (p, e))
        return {}
    if not isinstance(data, dict):
        warn(tr('config.json must contain an object at the top level (ignoring it): %s') % p)
        return {}
    return data


# ------------------------------------------------------------------ detection
def _flash_roots():
    r"""Candidate Flash SharedObject roots (both the old and the new naming)."""
    appdata = os.environ.get('APPDATA') or os.path.join(
        os.path.expanduser('~'), 'AppData', 'Roaming')
    return [
        os.path.join(appdata, 'Macromedia', 'Flash Player', '#SharedObjects'),
        os.path.join(appdata, 'Adobe', 'Flash Player', '#SharedObjects'),
    ]


def detect_save_dir():
    r"""Recursively find the directory containing nsSGv1.sol.

    When several candidates exist, prefer the one whose path contains
    "NEO Scavenger" (avoids mistaking a same-named .sol for ours).
    Returns None when nothing is found.
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
    """Common Steam install roots plus extra libraries from libraryfolders.vdf."""
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
    r"""Detect the game directory.

    First reverse-derive it from the save dir (drop the last segment, try each
    drive letter), then fall back to scanning the common Steam libraries.
    Returns None when nothing is found.
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
        # Failing to reverse-derive is fine: the last segment may be the game dir name
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
    """Default backup dir: <user Documents>\\NEO Scavenger Save Guard\\auto"""
    docs = os.path.join(os.path.expanduser('~'), 'Documents')
    if not os.path.isdir(docs):
        docs = os.path.expanduser('~')
    return os.path.join(docs, 'NEO Scavenger Save Guard', 'auto')


# ------------------------------------------------------------------ aggregate
def resolve(cli_config=None):
    r"""Resolve the final paths.

    Returns a dict:
        save_dir / game_dir / backup_root      — resolved (may be None)
        nsm_dir                                — NSM under the game dir (may be None)
        source                                 — where each value came from: config/env/detect/default
        missing                                — keys that need to be supplied manually
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
        missing.append(tr('game_dir (optional; only affects NSM slot sync)'))

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
    """Format the result of resolve() as printable multi-line text."""
    lines = [tr(' === Path resolution ===')]
    lines.append(tr('   save dir    : %s') % (res['save_dir'] or tr(' (not found)')))
    lines.append('             [%s]' % res['source'].get('save_dir'))
    lines.append(tr('   game dir    : %s') % (res['game_dir'] or tr(' (not found)')))
    lines.append('             [%s]' % res['source'].get('game_dir'))
    lines.append(tr('   backup dir  : %s') % (res['backup_root'] or tr(' (not found)')))
    lines.append('             [%s]' % res['source'].get('backup_root'))
    lines.append(tr('   config file : %s %s') % (
        res['config_file'], tr(' (exists)') if os.path.isfile(res['config_file']) else tr(' (missing)')))
    for w in res['warnings']:
        lines.append('  !! %s' % w)
    if res['missing']:
        lines.append(tr('   !! Unresolved items: %s') % ', '.join(res['missing']))
        lines.append(tr('     -> Set it explicitly in config.json, or pass --config <file>'))
    return '\n'.join(lines)


if __name__ == '__main__':
    print(describe(resolve()))

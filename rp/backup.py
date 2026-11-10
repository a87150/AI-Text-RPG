# -*- coding: utf-8 -*-
"""把 data/ 打包成 zip 放 backups/，供手动或定时备份。

settings.json 里的 API Key 一律抹掉再写进包，备份文件因此可以放心放进
云盘或仓库以外的地方。
"""
import json
import os
import shutil
import threading
import time
import zipfile

from . import config
from .config import BASE_DIR, DATA_DIR, app

# 允许指到别处：跑检查时产物别混进用户真实的备份列表
BACKUP_DIR = os.environ.get("LT_BACKUP_DIR") or os.path.join(BASE_DIR, "backups")
KEEP_MAX = 30          # ponytail: 只按份数裁剪，磁盘不够时再加大小上限
_EXTRA = (".json",)
_log_lock = threading.Lock()


def ensure_dir():
    os.makedirs(BACKUP_DIR, exist_ok=True)
    return BACKUP_DIR


def mask_settings(raw):
    """把 providers 里的 api_key 清成空串，其余设置照旧带上。"""
    if not isinstance(raw, dict):
        return raw
    out = json.loads(json.dumps(raw))
    providers = out.get("providers")
    if isinstance(providers, dict):
        for item in providers.values():
            if isinstance(item, dict) and "api_key" in item:
                item["api_key"] = ""
    return out


def _iter_files(root):
    for here, _dirs, files in os.walk(root):
        for fn in files:
            if not fn.endswith(_EXTRA):
                continue
            full = os.path.join(here, fn)
            yield full, os.path.relpath(full, root).replace("\\", "/")


def create(reason="manual"):
    """打一个包；返回 {name, size, at, files, skipped}。"""
    ensure_dir()
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime())
    tag = "".join(ch for ch in str(reason) if ch.isalnum() or ch in "-_") or "manual"
    name = "lt-data-%s-%s.zip" % (stamp, tag)
    target = os.path.join(BACKUP_DIR, name)
    files = 0
    skipped = 0
    try:
        with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as zf:
            for full, rel in _iter_files(DATA_DIR):
                if rel == "settings.json":
                    try:
                        with open(full, encoding="utf-8") as f:
                            data = json.load(f)
                    except (OSError, json.JSONDecodeError):
                        skipped += 1
                        continue
                    zf.writestr("data/settings.json",
                                json.dumps(mask_settings(data), ensure_ascii=False, indent=2))
                    files += 1
                    continue
                try:
                    zf.write(full, "data/" + rel)
                    files += 1
                except OSError:
                    skipped += 1
    except (OSError, zipfile.BadZipFile) as exc:
        app.logger.warning("备份失败：%s", exc)
        if os.path.isfile(target):
            try:
                os.remove(target)
            except OSError:
                pass
        raise
    prune()
    stat = os.stat(target)
    return {"name": name, "size": stat.st_size, "at": int(stat.st_mtime),
            "files": files, "skipped": skipped}


def listing():
    ensure_dir()
    items = []
    for fn in os.listdir(BACKUP_DIR):
        if not fn.endswith(".zip"):
            continue
        full = os.path.join(BACKUP_DIR, fn)
        if not os.path.isfile(full):     # 归档子目录里的不算
            continue
        try:
            stat = os.stat(full)
        except OSError:
            continue
        items.append({"name": fn, "size": stat.st_size, "at": int(stat.st_mtime)})
    items.sort(key=lambda x: x["at"], reverse=True)
    return items


def path_of(name):
    """只认 backups/ 里真实存在的文件名，挡住 ../ 之类的路径。"""
    if not name or "/" in name or "\\" in name or not name.endswith(".zip"):
        return None
    full = os.path.join(ensure_dir(), name)
    return full if os.path.isfile(full) else None


def delete(name):
    full = path_of(name)
    if not full:
        return False
    try:
        os.remove(full)
        return True
    except OSError:
        return False


def prune(keep=None):
    # 用户可能把份数调过，没显式传就读设置里的值
    if keep is None:
        keep = config.backup_conf().get("keep") or KEEP_MAX
    for item in listing()[int(keep):]:
        delete(item["name"])


def _read_keys():
    """当前 settings.json 里存着的 api_key，按提供方分。"""
    path = os.path.join(DATA_DIR, "settings.json")
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}
    return {pid: (item or {}).get("api_key") or ""
            for pid, item in (data.get("providers") or {}).items()
            if isinstance(item, dict)}


def restore(name):
    """把一份备份解回 data/。

    两个不能省的步骤：
    1. 先给当前数据打一个 before-restore 的包，还原错了还能退回来；
    2. 备份里的 api_key 是被抹掉的，写回前用还原前的 Key 补上，
       否则用户一点还原就把云端 Key 弄没了。
    """
    full = path_of(name)
    if not full:
        return None
    try:
        zf = zipfile.ZipFile(full)
    except (OSError, zipfile.BadZipFile):
        return None

    keys = _read_keys()
    guard = create("before-restore")
    root = os.path.normpath(DATA_DIR)
    sep = os.sep
    written = 0
    settings_body = None

    with zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            rel = info.filename.replace(chr(92), "/")
            if not rel.startswith("data/") or not rel.endswith(".json"):
                continue
            inner = rel[len("data/"):]
            if inner == "settings.json":
                settings_body = zf.read(info)
                continue
            target = os.path.normpath(os.path.join(DATA_DIR, inner))
            # 挡住 ../ 这类跑出 data/ 的路径
            if not target.startswith(root + sep):
                continue
            folder = os.path.dirname(target)
            if folder:
                os.makedirs(folder, exist_ok=True)
            with open(target, "wb") as dst:
                dst.write(zf.read(info))
            written += 1

    filled = 0
    if settings_body is not None:
        written += 1
        try:
            data = json.loads(settings_body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            data = None
        if isinstance(data, dict):
            for pid, item in (data.get("providers") or {}).items():
                if not isinstance(item, dict) or item.get("api_key"):
                    continue
                if keys.get(pid):
                    item["api_key"] = keys[pid]
                    filled += 1
            try:
                with open(os.path.join(DATA_DIR, "settings.json"), "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
            except OSError:
                pass
    return {"files": written, "guard": guard["name"], "keys": filled}


def due(conf, latest_at, now=None):
    """现在该不该自动备份：开了、且距上一份超过间隔。"""
    if not isinstance(conf, dict) or not conf.get("enabled"):
        return False
    minutes = max(1, int(conf.get("every_minutes") or 1440))
    now = time.time() if now is None else now
    return (now - float(latest_at or 0)) >= minutes * 60


def auto_loop(stop_event, conf_reader):
    """定时备份：每次醒来重新读配置，改设置不用重启。"""
    while not stop_event.wait(60):
        try:
            conf = conf_reader() or {}
            latest = (listing() or [{}])[0].get("at") or 0
            if not due(conf, latest):
                continue
            result = create("auto")
            with _log_lock:
                app.logger.info("自动备份：%s（%d 个文件）", result["name"], result["files"])
        except Exception as exc:      # 定时任务绝不能把线程弄死
            app.logger.warning("自动备份出错：%s", exc)


def start_auto(conf_reader):
    stop = threading.Event()
    thread = threading.Thread(target=auto_loop, args=(stop, conf_reader), daemon=True)
    thread.start()
    return stop

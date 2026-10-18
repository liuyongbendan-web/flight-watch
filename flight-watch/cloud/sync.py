"""
跟 GitHub 私有仓库 ai-learning-tracker 的 flight-watch/ 目录双向同步，让手机上的 Artifact 能改清单、看结果。

    flight-watch/watchlist.json  航线清单：手机和本机都能改，按 updated 时间戳「后写的赢」
    flight-watch/status.json     本机写回的最新价格、占位记录、登录/服务状态，手机只读

仓库本机克隆用部署密钥推送（ssh 别名 github-ai-learning），不经浏览器。
"""

import json
import os
import subprocess
from datetime import datetime, timedelta

import watchlist

REPO = ""
SUB = "flight-watch"
REMOTE_WL = os.path.join(REPO, SUB, "watchlist.json")
REMOTE_ST = os.path.join(REPO, SUB, "status.json")
STATUS_MIN_INTERVAL = timedelta(hours=3)   # 结果没变时，状态文件最多 3 小时提交一次（只刷新心跳）


class SyncError(RuntimeError):
    pass


def _git(*args, timeout=90):
    r = subprocess.run(["git", "-C", REPO, *args], capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        raise SyncError(f"git {' '.join(args[:2])} 失败: {(r.stderr or r.stdout).strip()[:300]}")
    return r.stdout


def _pull():
    _git("pull", "--rebase", "--autostash", "-q")


def _push(paths, msg):
    _git("add", *paths)
    if not _git("diff", "--cached", "--name-only").strip():
        return False
    _git("commit", "-q", "-m", msg)
    try:
        _git("push", "-q")
    except SyncError:
        _pull()              # 云端 AI 学习任务或手机刚提交过，先变基再推一次
        _git("push", "-q")
    return True


def _read(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _write(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, path)


def sync_watchlist():
    """拉仓库，比较两边清单的 updated，新的覆盖旧的。返回 'pulled' / 'pushed' / 'same'。"""
    _pull()
    remote = _read(REMOTE_WL)
    local = _read(watchlist.PATH) or {"entries": []}
    r_upd, l_upd = (remote or {}).get("updated", ""), local.get("updated", "")

    if remote and r_upd > l_upd:
        # 手机上改的：先按本机规则校验，非法就不采用（不能让坏数据把盯价弄挂）
        try:
            entries = [watchlist.normalize(e) for e in remote.get("entries", [])]
        except (ValueError, KeyError, TypeError) as e:
            raise SyncError(f"手机上改的清单不合法，没有采用：{e}")
        settings = {**watchlist.SETTINGS_DEFAULTS, **(remote.get("settings") or {})}
        _write(watchlist.PATH, {"updated": r_upd, "entries": entries, "settings": settings})
        return "pulled"

    if not remote or l_upd > r_upd:
        _write(REMOTE_WL, local)
        _push([os.path.join(SUB, "watchlist.json")], "机票盯价：本机同步航线清单")
        return "pushed"
    return "same"


def status_body(state, extra):
    """手机页面读的 status.json 内容（不含 generated_at/source）。Mac 和云端共用。"""
    entries = {}
    for eid, r in (state.get("entries") or {}).items():
        entries[eid] = {
            "checked_at": r.get("checked_at"),
            "status": r.get("status"),
            "error": r.get("error"),
            "best": r.get("best"),
            "by_type": r.get("by_type"),
            "calendar": r.get("calendar"),
            "per_day": {d: (b and {"price": b["price"], "airlines": b["airlines"], "depart": b["depart"]})
                        for d, b in (r.get("per_day") or {}).items()},
        }
    body = {
        "entries": entries,
        "last_cycle": state.get("last_cycle"),
        "last_error": state.get("last_error"),
        **extra,
    }
    # 只拿「价格和占位结果」做指纹；查价时间每轮都变，不算变化
    fp = json.dumps({
        "prices": {eid: [(e["best"] or {}).get("price"), (e["best"] or {}).get("date"),
                         {k: v.get("price") for k, v in (e["by_type"] or {}).items()}, e.get("error")]
                   for eid, e in entries.items()},
        "err": body["last_error"], "extra": extra,
    }, sort_keys=True, ensure_ascii=False)
    body["fp"] = fp
    return body


def push_status(state, extra):
    """把本机最新结果写回仓库。结果没变就最多 3 小时提交一次，免得刷屏提交历史。"""
    body = status_body(state, extra)
    fp = body["fp"]
    old = _read(REMOTE_ST) or {}
    changed = old.get("fp") != fp
    try:
        stale = datetime.now() - datetime.fromisoformat(old.get("generated_at", "2000-01-01T00:00:00")) \
            > STATUS_MIN_INTERVAL
    except ValueError:
        stale = True
    if not (changed or stale):
        return False
    _pull()
    _write(REMOTE_ST, {"generated_at": datetime.now().isoformat(timespec="seconds"), "source": "mac", **body})
    return _push([os.path.join(SUB, "status.json")], "机票盯价：更新最新价格")

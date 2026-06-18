"""Local Flask server that serves manual.html and exposes tracking endpoints.

Multi-user version: every API call must include ?user=<name> (or X-User header).
Each user gets their own isolated workspace under users/<name>/:
    users/<name>/events/session_*/   # trajectories
    users/<name>/progress.json        # current index + completed task ids

Endpoints:
    GET  /                                  -> manual.html
    GET  /<file>                            -> static files (registered last)
    GET  /tasks                             -> all tasks
    GET  /current-task?user=NAME            -> current task for that user
    POST /set-index?user=NAME    {index}    -> jump to a specific task index
    POST /next-task?user=NAME               -> pick a random next task
    POST /start?user=NAME                   -> start tracking the current task
    POST /stop?user=NAME         {answer}   -> stop tracking
    GET  /status?user=NAME                  -> live tracker state for that user
    GET  /sessions?user=NAME                -> saved sessions for that user
    GET  /session/<name>/trajectory?user=NAME
    GET  /session/<name>/screenshot/<file>?user=NAME
    POST /session/<name>/save-thoughts?user=NAME {thoughts:[...]}
"""

import asyncio
import json
import os
import random
import re as _re
import shutil
import threading
import webbrowser
from datetime import datetime

from flask import Flask, jsonify, request, send_from_directory

from tracker import run_tracker
from tasks_data import TASKS   # embedded task list (no external json)


HERE = os.environ.get("WEBTRACKER_STATE_DIR") or os.path.dirname(os.path.abspath(__file__))
ASSETS = os.environ.get("WEBTRACKER_ASSETS_DIR") or HERE
USERS_ROOT = os.path.join(HERE, "users")
PORT = 5000

app = Flask(__name__, static_folder=None)
print(f"Loaded {len(TASKS)} tasks from tasks_data.py")


# Allow the manual page (served from a different origin / opened as a file) to
# call these endpoints. Flask auto-answers the preflight OPTIONS for each route;
# this just stamps the CORS headers onto every response, including that.
@app.after_request
def _add_cors_headers(resp):
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    return resp


# ---------- per-user state ----------

_NAME_RE = _re.compile(r"^[A-Za-z0-9_\-]{1,40}$")


def safe_user_name(name):
    name = (name or "").strip()
    if not name or not _NAME_RE.match(name):
        return None
    return name


def user_dir(name):       return os.path.join(USERS_ROOT, name)
def events_root(name):    return os.path.join(user_dir(name), "events")
def progress_file(name):  return os.path.join(user_dir(name), "progress.json")


_users = {}
_users_lock = threading.Lock()


def _default_progress():
    return {
        "current_index": random.randrange(len(TASKS)) if TASKS else 0,
        "completed_ids": [],
        "practice_done": False,
    }


PRACTICE_TASK = {
    "id": 0,
    "task": "Purchase Dunkin' Zero Sugar Refreshers SINGLES TO GO in Peach Passionfruit Lemonade Flavor.",
    "site_url": "https://www.google.com/",
    "final_url": "https://www.google.com/",
}


def _default_state():
    return {
        "status": "idle",
        "step_count": 0,
        "session_dir": None,
        "task_id": None,
        "task": None,
        "error": None,
        "last_session_dir": None,
        "practice": False,
    }


def _migrate_legacy_into(name):
    """Absorb any session_* folders left in the top-level legacy events/ dir
    into this user's events folder. Runs repeatedly (cheap no-op when nothing
    is there). Also inherits a top-level progress.json once."""
    user_events = events_root(name)
    legacy_events = os.path.join(HERE, "events")
    if os.path.isdir(legacy_events):
        moved = 0
        for entry in os.listdir(legacy_events):
            src = os.path.join(legacy_events, entry)
            if not os.path.isdir(src) or not entry.startswith("session_"):
                continue
            dst = os.path.join(user_events, entry)
            if os.path.exists(dst):
                continue
            try:
                shutil.move(src, dst)
                moved += 1
            except Exception as e:
                print(f"[warn] could not migrate {entry}: {e}")
        if moved:
            print(f"[migrate] moved {moved} legacy session(s) into users/{name}/events/")
        try:
            if not os.listdir(legacy_events):
                os.rmdir(legacy_events)
        except Exception:
            pass

    user_pf = progress_file(name)
    legacy_pf = os.path.join(HERE, "progress.json")
    if os.path.isfile(legacy_pf) and not os.path.isfile(user_pf):
        try:
            shutil.move(legacy_pf, user_pf)
            print(f"[migrate] inherited legacy progress.json -> users/{name}/")
        except Exception as e:
            print(f"[warn] could not migrate progress.json: {e}")


def _make_user(name):
    os.makedirs(events_root(name), exist_ok=True)
    _migrate_legacy_into(name)
    p = None
    pf = progress_file(name)
    if os.path.isfile(pf):
        try:
            with open(pf, "r", encoding="utf-8") as f:
                raw = json.load(f)
            p = {
                "current_index": int(raw.get("current_index", 0)),
                "completed_ids": list(raw.get("completed_ids", [])),
                "practice_done": bool(raw.get("practice_done", False)),
            }
        except Exception:
            p = None
    if p is None:
        p = _default_progress()
    return {
        "progress": p,
        "state": _default_state(),
        "lock": threading.Lock(),
        "stop_flag": None,
        "thread": None,
        "pending_answer": None,
    }


def get_user(name):
    with _users_lock:
        if name not in _users:
            _users[name] = _make_user(name)
        return _users[name]


def save_progress(name, info=None):
    info = info or _users.get(name)
    if not info:
        return
    try:
        os.makedirs(user_dir(name), exist_ok=True)
        with open(progress_file(name), "w", encoding="utf-8") as f:
            json.dump({
                "current_index": info["progress"]["current_index"],
                "completed_ids": info["progress"]["completed_ids"],
                "practice_done": info["progress"].get("practice_done", False),
            }, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[warn] could not save progress.json for {name}: {e}")


def _require_user():
    name = request.args.get("user") or request.headers.get("X-User") or ""
    name = safe_user_name(name)
    if not name:
        return None, None
    return name, get_user(name)


# ---------- helpers ----------

def _safe_index(info):
    if not TASKS:
        return 0
    return max(0, min(info["progress"]["current_index"], len(TASKS) - 1))


def _current_task(info):
    if not TASKS:
        return None
    return TASKS[_safe_index(info)]


# ---------- static routes ----------

@app.route("/")
def index():
    return send_from_directory(ASSETS, "manual.html")


# ---------- task management ----------

@app.route("/tasks")
def tasks_list():
    return jsonify({"tasks": TASKS, "total": len(TASKS)})


@app.route("/current-task")
def current_task():
    name, info = _require_user()
    if not info:
        return jsonify({"error": "user required"}), 400
    practice = request.args.get("practice") in ("1", "true")
    t = PRACTICE_TASK if practice else _current_task(info)
    return jsonify({
        "user": name,
        "index": -1 if practice else _safe_index(info),
        "total": len(TASKS),
        "task": t,
        "practice": practice,
        "practice_done": bool(info["progress"].get("practice_done", False)),
        "completed_ids": info["progress"]["completed_ids"],
    })


@app.route("/set-index", methods=["POST"])
def set_index():
    name, info = _require_user()
    if not info:
        return jsonify({"error": "user required"}), 400
    data = request.get_json(silent=True) or {}
    idx = data.get("index")
    if idx is None or not isinstance(idx, int):
        return jsonify({"error": "index (int) required"}), 400
    if not TASKS:
        return jsonify({"error": "no tasks loaded"}), 400
    if idx < 0 or idx >= len(TASKS):
        return jsonify({"error": f"index out of range (0..{len(TASKS)-1})"}), 400
    info["progress"]["current_index"] = idx
    save_progress(name, info)
    return jsonify({"index": idx, "task": TASKS[idx]})


@app.route("/next-task", methods=["POST"])
def next_task():
    name, info = _require_user()
    if not info:
        return jsonify({"error": "user required"}), 400
    if not TASKS:
        return jsonify({"error": "no tasks loaded"}), 400
    completed = set(info["progress"]["completed_ids"])
    current_id = TASKS[_safe_index(info)]["id"]
    pool = [i for i, t in enumerate(TASKS)
            if t["id"] not in completed and t["id"] != current_id]
    if not pool:
        pool = [i for i, t in enumerate(TASKS) if t["id"] != current_id] \
            or list(range(len(TASKS)))
    idx = random.choice(pool)
    info["progress"]["current_index"] = idx
    save_progress(name, info)
    # Picking a fresh task implies starting a new attempt — reset the run state
    # so the UI no longer shows the lingering "done" pill from the previous run.
    with info["lock"]:
        if info["state"].get("status") in ("done", "error"):
            info["state"].update({
                "status": "idle",
                "step_count": 0,
                "session_dir": None,
                "task_id": None,
                "task": None,
                "error": None,
                "last_session_dir": None,
                "practice": False,
            })
    return jsonify({"index": idx, "task": TASKS[idx]})


# ---------- tracking control ----------

@app.route("/status")
def status():
    name, info = _require_user()
    if not info:
        return jsonify({"error": "user required"}), 400
    with info["lock"]:
        s = dict(info["state"])
    s["user"] = name
    s["current_index"] = _safe_index(info)
    s["total_tasks"] = len(TASKS)
    s["save_path"] = events_root(name)
    return jsonify(s)


@app.route("/start", methods=["POST"])
def start():
    name, info = _require_user()
    if not info:
        return jsonify({"error": "user required"}), 400

    with info["lock"]:
        if info["state"]["status"] == "tracking":
            return jsonify({"error": "already tracking"}), 400

    data = request.get_json(silent=True) or {}
    practice = bool(data.get("practice"))
    if not practice and not info["progress"].get("practice_done", False):
        return jsonify({"error": "연습 task 를 먼저 완료해야 실제 task 를 시작할 수 있습니다."}), 403

    if practice:
        t = PRACTICE_TASK
    else:
        # Use the task ID the client believes it is starting — guarantees the
        # task shown on the page matches the task recorded into the session,
        # even if /next-task fired between render and click.
        client_task_id = data.get("task_id")
        t = None
        if isinstance(client_task_id, int):
            for task in TASKS:
                if task["id"] == client_task_id:
                    t = task
                    break
        if t is None:
            t = _current_task(info)
    if not t:
        return jsonify({"error": "no task available"}), 400

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    prefix = "practice_" if practice else f"session_"
    session_dir = os.path.join(events_root(name), f"{prefix}{ts}_task{t['id']:03d}")

    # Auto-discard any existing trajectory folders for this same task — when
    # the user restarts a task we treat the previous attempt as scrap.
    try:
        suffix = f"_task{t['id']:03d}"
        root = events_root(name)
        if os.path.isdir(root):
            for entry in os.listdir(root):
                if not entry.endswith(suffix):
                    continue
                if not entry.startswith(prefix):
                    continue
                old = os.path.join(root, entry)
                if os.path.isdir(old):
                    try:
                        shutil.rmtree(old)
                        print(f"  [auto-clean] removed prior attempt: {entry}")
                    except Exception as e:
                        print(f"  [warn] could not remove {entry}: {e}")
    except Exception as e:
        print(f"  [warn] auto-clean scan failed: {e}")

    with info["lock"]:
        info["state"].update({
            "status": "tracking",
            "step_count": 0,
            "session_dir": session_dir,
            "task_id": t["id"],
            "task": t["task"],
            "error": None,
            "practice": practice,
        })

    info["stop_flag"] = threading.Event()
    info["thread"] = threading.Thread(
        target=_run_tracker_thread,
        args=(name, info, t, session_dir, info["stop_flag"]),
        daemon=True,
    )
    info["thread"].start()

    return jsonify({"status": "tracking", "session_dir": session_dir, "task": t, "practice": practice})


@app.route("/explore", methods=["POST"])
def explore():
    """Open a plain Chromium window for exploration (no recording)."""
    name, info = _require_user()
    if not info:
        return jsonify({"error": "user required"}), 400
    with info["lock"]:
        if info["state"]["status"] == "tracking":
            return jsonify({"error": "기록 중에는 탐색할 수 없습니다."}), 400
        if info["state"].get("exploring"):
            return jsonify({"error": "이미 탐색 창이 열려 있습니다."}), 400
        info["state"]["exploring"] = True
    info["explore_stop"] = threading.Event()
    threading.Thread(target=_run_explore_thread, args=(info,), daemon=True).start()
    return jsonify({"status": "exploring"})


def _run_explore_thread(info):
    try:
        from tracker import run_explorer
        asyncio.run(run_explorer("https://www.google.com/",
                                 stop_flag=info.get("explore_stop")))
    except Exception as e:
        print(f"[explore] error: {e}")
    finally:
        with info["lock"]:
            info["state"]["exploring"] = False


@app.route("/finalize", methods=["POST"])
def finalize():
    """Set the outcome (성공/실패/다시하기) and its reason on the session that
    just finished (the worker closed the tracker window). Writes the outcome as
    the terminate action's status and the reason as its thought."""
    name, info = _require_user()
    if not info:
        return jsonify({"error": "user required"}), 400
    data = request.get_json(silent=True) or {}
    status = (data.get("status") or "").lower()
    if status not in ("success", "fail", "retry"):
        return jsonify({"error": "성공/실패/다시하기 중 하나를 선택하세요."}), 400
    reason = (data.get("reason") or "").strip()
    if not reason:
        return jsonify({"error": "완료 이유를 작성해 주세요."}), 400

    with info["lock"]:
        sdir = info["state"].get("last_session_dir")
    if not sdir or not os.path.isdir(sdir):
        return jsonify({"error": "마무리할 세션이 없습니다."}), 400

    # 연습(practice) 세션은 데이터로 저장하지 않는다 — 드릴이 끝나면 폐기.
    if os.path.basename(sdir).startswith("practice_"):
        try:
            shutil.rmtree(sdir)
        except Exception:
            pass
        with info["lock"]:
            if info["state"].get("last_session_dir") == sdir:
                info["state"]["last_session_dir"] = None
            info["state"]["status"] = "finalized"
        return jsonify({"status": "finalized", "practice": True, "discarded": True})

    jsonl = os.path.join(sdir, "trajectory.jsonl")
    try:
        with open(jsonl, "r", encoding="utf-8") as f:
            lines = f.readlines()
        term_idx = None
        for i in range(len(lines) - 1, -1, -1):
            try:
                o = json.loads(lines[i])
            except Exception:
                continue
            a = o.get("action")
            if isinstance(a, dict) and a.get("action") == "terminate":
                term_idx = i
                break
        if term_idx is not None:
            o = json.loads(lines[term_idx])
            o["action"]["status"] = status
            o["thought"] = reason
            lines[term_idx] = json.dumps(o, ensure_ascii=False) + "\n"
            with open(jsonl, "w", encoding="utf-8") as f:
                f.writelines(lines)

        meta_path = os.path.join(sdir, "meta.json")
        meta = {}
        if os.path.isfile(meta_path):
            with open(meta_path, "r", encoding="utf-8") as f:
                meta = json.load(f)
        meta["outcome"] = status
        meta["outcome_reason"] = reason
        with open(meta_path, "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)
    except Exception as e:
        return jsonify({"error": f"저장 실패: {e}"}), 500

    with info["lock"]:
        info["state"]["status"] = "finalized"
    return jsonify({"status": "finalized", "outcome": status})


@app.route("/stop", methods=["POST"])
def stop():
    name, info = _require_user()
    if not info:
        return jsonify({"error": "user required"}), 400

    with info["lock"]:
        if info["state"]["status"] != "tracking":
            return jsonify({"error": "not tracking"}), 400

    data = request.get_json(silent=True) or {}
    answer = (data.get("answer") or "").strip()
    raw_status = (data.get("terminate_status") or "success").strip().lower()
    if raw_status not in ("success", "fail"):
        raw_status = "success"

    info["pending_answer"] = answer if answer else None
    info["pending_terminate_status"] = raw_status

    with info["lock"]:
        info["state"]["status"] = "saving"

    if info["stop_flag"] is not None:
        info["stop_flag"].set()

    return jsonify({"status": "saving"})


# ---------- thought authoring ----------

@app.route("/sessions")
def sessions_list():
    name, info = _require_user()
    if not info:
        return jsonify({"error": "user required"}), 400
    # Auto-absorb any legacy session_* folders that may have been written
    # to the top-level events/ folder (e.g. by a stale server process before
    # the user restarted). Safe no-op when nothing legacy remains.
    _migrate_legacy_into(name)
    items = []
    root = events_root(name)
    if os.path.isdir(root):
        for entry in sorted(os.listdir(root), reverse=True):
            d = os.path.join(root, entry)
            if not os.path.isdir(d):
                continue
            jsonl = os.path.join(d, "trajectory.jsonl")
            meta = os.path.join(d, "meta.json")
            if not os.path.isfile(jsonl):
                continue
            steps = 0
            try:
                with open(jsonl, "r", encoding="utf-8") as f:
                    steps = sum(1 for _ in f)
            except Exception:
                pass
            task_text, task_id, has_thoughts = "", None, False
            outcome, outcome_reason = None, ""
            try:
                if os.path.isfile(meta):
                    with open(meta, "r", encoding="utf-8") as f:
                        m = json.load(f)
                    task_text = m.get("task_description") or m.get("task") or ""
                    task_id = m.get("task_id")
                    has_thoughts = bool(m.get("thoughts_written"))
                    outcome = m.get("outcome")
                    outcome_reason = m.get("outcome_reason", "")
            except Exception:
                pass
            items.append({
                "name": entry,
                "steps": steps,
                "task_id": task_id,
                "task": task_text,
                "has_thoughts": has_thoughts,
                "outcome": outcome,
                "outcome_reason": outcome_reason,
                "practice": entry.startswith("practice_"),
            })
    return jsonify({"sessions": items})


def _session_dir_for(user, name):
    safe = os.path.basename(name)
    return os.path.join(events_root(user), safe)


@app.route("/session/<sname>/trajectory")
def session_trajectory(sname):
    name, info = _require_user()
    if not info:
        return jsonify({"error": "user required"}), 400
    d = _session_dir_for(name, sname)
    jsonl = os.path.join(d, "trajectory.jsonl")
    meta_path = os.path.join(d, "meta.json")
    if not os.path.isfile(jsonl):
        return jsonify({"error": "session not found"}), 404
    steps = []
    with open(jsonl, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    steps.append(json.loads(line))
                except Exception:
                    pass
    meta = {}
    if os.path.isfile(meta_path):
        try:
            with open(meta_path, "r", encoding="utf-8") as f:
                meta = json.load(f)
        except Exception:
            pass
    return jsonify({"name": sname, "steps": steps, "meta": meta})


@app.route("/session/<sname>/screenshot/<path:filename>")
def session_screenshot(sname, filename):
    name, info = _require_user()
    if not info:
        return ("user required", 400)
    d = _session_dir_for(name, sname)
    sd = os.path.join(d, "screenshot")
    if not os.path.isdir(sd):
        return ("Not found", 404)
    safe = os.path.basename(filename)
    full = os.path.join(sd, safe)
    if not os.path.isfile(full):
        return ("Not found", 404)
    return send_from_directory(sd, safe)


@app.route("/session/<sname>/discard", methods=["POST"])
def session_discard(sname):
    name, info = _require_user()
    if not info:
        return jsonify({"error": "user required"}), 400
    d = _session_dir_for(name, sname)
    if not os.path.isdir(d):
        return jsonify({"error": "session not found"}), 404
    try:
        shutil.rmtree(d)
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    with info["lock"]:
        if info["state"].get("last_session_dir") == d:
            info["state"]["last_session_dir"] = None
    return jsonify({"status": "discarded", "session_dir": d})


@app.route("/session/<sname>/save-thoughts", methods=["POST"])
def session_save_thoughts(sname):
    name, info = _require_user()
    if not info:
        return jsonify({"error": "user required"}), 400
    d = _session_dir_for(name, sname)
    jsonl = os.path.join(d, "trajectory.jsonl")
    meta_path = os.path.join(d, "meta.json")
    if not os.path.isfile(jsonl):
        return jsonify({"error": "session not found"}), 404

    data = request.get_json(silent=True) or {}
    thoughts = data.get("thoughts")
    if not isinstance(thoughts, list):
        return jsonify({"error": "thoughts (list) required"}), 400
    # Targets is a sparse mapping {step_idx: "button label"} — optional refinement
    # of click/type action targets, written into the action dict on save.
    raw_targets = data.get("targets") or {}
    targets = {}
    if isinstance(raw_targets, dict):
        for k, v in raw_targets.items():
            try:
                targets[int(k)] = str(v).strip()
            except Exception:
                pass

    raw_status = (data.get("terminate_status") or "").strip().lower()
    if raw_status not in ("success", "fail"):
        raw_status = None  # leave unchanged
    answer = (data.get("answer") or "").strip()

    steps = []
    with open(jsonl, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    steps.append(json.loads(line))
                except Exception:
                    pass

    for i, step in enumerate(steps):
        step["thought"] = (thoughts[i] if i < len(thoughts) else "").strip()
        if i in targets and targets[i]:
            act = step.get("action") or {}
            if isinstance(act, dict):
                act["target"] = targets[i]
                step["action"] = act

    # Update the final terminate entry's status to the user's success/fail choice.
    if raw_status and steps:
        last = steps[-1]
        last_act = last.get("action") or {}
        if isinstance(last_act, dict) and last_act.get("action") == "terminate":
            last_act["status"] = raw_status
            last["action"] = last_act

    tmp = jsonl + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        for s in steps:
            f.write(json.dumps(s, ensure_ascii=False) + "\n")
    os.replace(tmp, jsonl)

    try:
        meta = {}
        if os.path.isfile(meta_path):
            with open(meta_path, "r", encoding="utf-8") as f:
                meta = json.load(f)
        meta["thoughts_written"] = True
        meta["thoughts_written_at"] = datetime.now().strftime("%Y-%m-%d_%H:%M:%S")
        if raw_status:
            meta["terminate_status"] = raw_status
        if answer:
            meta["answer"] = answer
        with open(meta_path, "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[warn] could not update meta.json: {e}")

    # Step-2 refinement: drop noise actions, mark click points, etc.
    try:
        from refinement import refine_session
        result = refine_session(d)
        if result:
            n_before, n_after = result
            print(f"[refine] {os.path.basename(d)}: {n_before} -> {n_after} actions")
    except Exception as e:
        print(f"[warn] refinement failed: {e}")

    return jsonify({"status": "saved", "steps": len(steps)})


# ---------- tracker thread ----------

def _run_tracker_thread(name, info, task, session_dir, stop_flag):
    try:
        progress = {"step_count": 0}
        relay_stop = threading.Event()

        def _relay():
            while not relay_stop.is_set():
                with info["lock"]:
                    info["state"]["step_count"] = progress.get("step_count", 0)
                relay_stop.wait(0.4)

        relay_thread = threading.Thread(target=_relay, daemon=True)
        relay_thread.start()

        recorder = asyncio.run(
            run_tracker(task["task"], session_dir, "https://www.google.com/",
                        stop_flag=stop_flag, progress=progress,
                        terminate_status=lambda: info.get("pending_terminate_status") or "success")
        )

        relay_stop.set()

        # Augment meta.json with task + user metadata
        try:
            meta_path = os.path.join(session_dir, "meta.json")
            meta = {}
            if os.path.isfile(meta_path):
                with open(meta_path, "r", encoding="utf-8") as f:
                    meta = json.load(f)
            meta["user"] = name
            meta["task_id"] = task["id"]
            meta["site_url"] = task.get("site_url")
            if info.get("pending_answer") is not None:
                meta["answer"] = info["pending_answer"]
            with open(meta_path, "w", encoding="utf-8") as f:
                json.dump(meta, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"[warn] could not augment meta.json: {e}")

        practice = bool(info["state"].get("practice"))

        # Run step-2 refinement BEFORE flipping status to done so the inline
        # thought UI can display marked screenshots from the very first load.
        if recorder.step > 0:
            try:
                from refinement import refine_session
                r = refine_session(session_dir)
                if r:
                    print(f"[refine] {os.path.basename(session_dir)}: {r[0]} -> {r[1]} actions")
            except Exception as e:
                print(f"[warn] auto-refinement failed: {e}")

        with info["lock"]:
            info["state"]["step_count"] = recorder.step
            if recorder.step == 0:
                # Nothing was recorded — wipe the empty folder
                if os.path.isdir(session_dir):
                    try:
                        shutil.rmtree(session_dir)
                    except Exception:
                        pass
                info["state"]["status"] = "idle"
                info["state"]["session_dir"] = None
                info["state"]["last_session_dir"] = None
                info["state"]["practice"] = False
            else:
                info["state"]["status"] = "done"
                info["state"]["last_session_dir"] = session_dir
                # Practice sessions are saved (so the user can write thoughts
                # during the full-flow drill) but do NOT count as a completed task.
                if practice:
                    # First successful practice unlocks the real task flow.
                    info["progress"]["practice_done"] = True
                    save_progress(name, info)
                else:
                    if task["id"] not in info["progress"]["completed_ids"]:
                        info["progress"]["completed_ids"].append(task["id"])
                    save_progress(name, info)
                info["state"]["practice"] = False

        info["pending_answer"] = None

    except Exception as e:
        with info["lock"]:
            info["state"]["status"] = "error"
            info["state"]["error"] = f"{type(e).__name__}: {e}"


# ---------- catch-all static route (registered last) ----------

@app.route("/<path:filename>")
def static_files(filename):
    full = os.path.join(ASSETS, filename)
    if not os.path.isfile(full):
        return ("Not found", 404)
    return send_from_directory(ASSETS, filename)


# ---------- launch ----------

def _open_browser():
    webbrowser.open(f"http://localhost:{PORT}/")


if __name__ == "__main__":
    os.makedirs(USERS_ROOT, exist_ok=True)
    print(f"Serving on http://localhost:{PORT}/  (Ctrl+C to quit)")
    threading.Timer(1.0, _open_browser).start()
    app.run(host="127.0.0.1", port=PORT, debug=False, use_reloader=False)
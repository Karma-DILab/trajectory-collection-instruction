"""Step-2 refinement for a single saved session.

Mirrors PC-Agent/web_postprocess/refinement.py but trimmed for in-process use:
  1. drop leading consecutive `wait` actions
  2. drop `wait` whose screenshot is identical to the next entry's
  3. mark click point on each click screenshot -> *_marked.png
  4. write a `marked_screenshot` field on every entry

NOTE: we intentionally do NOT drop consecutive same-coordinate `left_click`s.
Repeated clicks on one spot are usually intentional (quantity +/- steppers,
pagination "next", double-clicks, "load more"), and dropping them silently
deletes real steps from the trajectory.
"""

import os

from refine_utils import (
    load_jsonl,
    save_jsonl,
    get_action_type,
    get_click_coord,
    mark_click_point,
)


def drop_all_waits(entries):
    """Mid-trajectory idle is rarely useful for training — remove every wait."""
    return [e for e in entries if get_action_type(e) != "wait"]


def mark_clicks(entries, session_dir):
    for e in entries:
        coord = get_click_coord(e)
        if coord is None:
            e["marked_screenshot"] = e.get("screenshot")
            continue
        shot_rel = e.get("screenshot", "")
        shot_abs = os.path.join(session_dir, shot_rel)
        if not os.path.isfile(shot_abs):
            e["marked_screenshot"] = shot_rel
            continue
        base, ext = os.path.splitext(shot_rel)
        marked_rel = f"{base}_marked{ext}"
        marked_abs = os.path.join(session_dir, marked_rel)
        if not os.path.isfile(marked_abs):
            try:
                mark_click_point(shot_abs, coord[0], coord[1], save_path=marked_abs)
            except Exception as ex:
                print(f"  [refine] mark failed for {shot_rel}: {ex}")
                e["marked_screenshot"] = shot_rel
                continue
        e["marked_screenshot"] = marked_rel
    return entries


def refine_session(session_dir):
    """Run the refinement pipeline in-place. Returns (n_before, n_after) or None."""
    jsonl_path = os.path.join(session_dir, "trajectory.jsonl")
    if not os.path.isfile(jsonl_path):
        return None
    try:
        entries = load_jsonl(jsonl_path)
    except Exception:
        return None
    if not entries:
        return (0, 0)

    n_before = len(entries)
    entries = drop_all_waits(entries)
    entries = mark_clicks(entries, session_dir)

    try:
        save_jsonl(jsonl_path, entries)
    except Exception as e:
        print(f"  [refine] save failed: {e}")
        return None

    return (n_before, len(entries))

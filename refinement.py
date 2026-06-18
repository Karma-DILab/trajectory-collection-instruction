"""Step-2 refinement for a single saved session.

Mirrors PC-Agent/web_postprocess/refinement.py but trimmed for in-process use:
  1. drop leading consecutive `wait` actions
  2. drop `wait` whose screenshot is identical to the next entry's
  3. drop consecutive duplicate `left_click` at the same coord
  4. mark click point on each click screenshot -> *_marked.png
  5. write a `marked_screenshot` field on every entry
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


def drop_duplicate_clicks(entries):
    kept = []
    for e in entries:
        if kept:
            prev = kept[-1]
            if (get_action_type(prev) == "left_click"
                    and get_action_type(e) == "left_click"
                    and prev["action"].get("coordinate") == e["action"].get("coordinate")):
                continue
        kept.append(e)
    return kept


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
    entries = drop_duplicate_clicks(entries)
    entries = mark_clicks(entries, session_dir)

    try:
        save_jsonl(jsonl_path, entries)
    except Exception as e:
        print(f"  [refine] save failed: {e}")
        return None

    return (n_before, len(entries))

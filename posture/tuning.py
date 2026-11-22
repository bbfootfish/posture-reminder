"""Threshold tuning advice derived from human-labelled reminders.

This module is deliberately pure and free of I/O: it takes the per-issue
verdict aggregates from :meth:`ReminderStore.verdict_stats` plus the live
baseline and policy, and returns *suggestions* only.  Nothing here writes to
the database or changes live judgement.  A human must review the suggestions
and explicitly apply them through the API, which persists a
``thresholds_override.json`` read on next service start.

The direction of each suggestion:
  * A false positive means the reminder fired but the child was fine.  To
    reduce such firings we make the trigger stricter:
      - lower ``head_up_ratio`` (the head must drop further below baseline),
      - increase ``head_fwd_drop`` (the angle must collapse further),
      - lower ``occupancy_threshold`` is NOT used (it would fire more often),
        so occupancy is left alone.
  * A true positive confirms a real problem; it sets the *ceiling* for how
    far we may relax.  We never relax past the most extreme true positive,
    so a confirmed case keeps firing.

Adjustments are small and clamped, and require a minimum sample count, so a
handful of labels cannot swing the policy.  Every suggestion carries the
sample counts and a plain-language confidence note for the human to judge.
"""

from __future__ import annotations

from typing import Any

from .config import THRESHOLD_OVERRIDE_BOUNDS, THRESHOLD_OVERRIDE_KEYS
from .judge import thresholds_for
from .models import HEAD_DOWN, ISSUES, LEAN, TOO_CLOSE, Baseline

MIN_SAMPLES = 5  # per (issue, verdict) bucket before we trust its statistics
MAX_HEAD_UP_STEP = 0.05
MAX_FWD_DROP_STEP = 3.0


def _finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or number in (float("inf"), float("-inf")):
        return None
    return number


def _clamp(key: str, value: float) -> float:
    low, high = THRESHOLD_OVERRIDE_BOUNDS[key]
    return max(low, min(high, value))


def _round(value: float, digits: int = 4) -> float:
    return round(value, digits)


def suggest(
    stats: list[dict[str, object]],
    baseline: Baseline,
    policy: dict[str, float],
    *,
    missed_stats: list[dict[str, object]] | None = None,
) -> dict[str, Any]:
    """Return tuning advice plus the current policy for comparison.

    ``policy`` contains the live values of the keys in
    :data:`THRESHOLD_OVERRIDE_KEYS`.  The returned ``values`` dict is a sparse
    set of suggested replacements; empty means "keep current values".

    ``missed_stats`` are human-marked missed moments aggregated by issue.
    They push thresholds in the opposite direction to false positives:
    a miss means the trigger was too strict and should be relaxed.  When a
    false positive and a miss both ask for the same knob in opposite
    directions, we leave it unchanged and report a conflict for the human to
    resolve, rather than guessing.
    """
    missed_stats = missed_stats or []
    by_bucket: dict[tuple[str, str], dict[str, object]] = {}
    for row in stats:
        issue, verdict = row.get("issue"), row.get("verdict")
        if isinstance(issue, str) and isinstance(verdict, str):
            by_bucket[(issue, verdict)] = row

    current = {key: _finite(policy.get(key)) for key in THRESHOLD_OVERRIDE_KEYS}
    current = {key: value for key, value in current.items() if value is not None}
    suggested: dict[str, float] = dict(current)
    reasons: list[str] = []
    conflicts: list[str] = []
    fp_pushed: set[str] = set()  # knobs a false positive asked to tighten
    total_labelled = sum(int(r.get("count") or 0) for r in stats)
    total_usable = sum(
        int(r.get("count") or 0)
        for (issue, verdict), r in by_bucket.items()
        if verdict in ("true_positive", "false_positive")
    )

    # One conservative adjustment per trigger class, driven by false positives.
    # head_down is governed by head_up_ratio; forward/lean classes by
    # head_fwd_drop.  Both ceilings come from the true-positive distribution.
    head_up_ratio = current.get("head_up_ratio")
    head_fwd_drop = current.get("head_fwd_drop")
    head_down_fp = by_bucket.get((HEAD_DOWN, "false_positive"))
    head_down_tp = by_bucket.get((HEAD_DOWN, "true_positive"))
    fwd_fp_buckets = [
        by_bucket[(issue, "false_positive")]
        for issue in (LEAN, TOO_CLOSE)
        if (issue, "false_positive") in by_bucket
    ]
    fwd_tp_buckets = [
        by_bucket[(issue, "true_positive")]
        for issue in (LEAN, TOO_CLOSE)
        if (issue, "true_positive") in by_bucket
    ]

    if head_up_ratio is not None:
        fp_n = int(head_down_fp.get("count") or 0) if head_down_fp else 0
        if fp_n >= MIN_SAMPLES:
            fp_up = _finite((head_down_fp or {}).get("head_up", {}).get("avg"))
            base_up = _finite(baseline.head_up)
            if fp_up is not None and base_up and base_up > 0:
                # Head-up ratio that would (just) exclude the average false
                # positive.  Stay conservative with a half step.
                needed = fp_up / base_up - 0.01
                candidate = _clamp("head_up_ratio", min(head_up_ratio, needed))
                candidate = max(candidate, head_up_ratio - MAX_HEAD_UP_STEP)
                # Do not relax beyond the strictest true positive, if any.
                tp_up = _finite((head_down_tp or {}).get("head_up", {}).get("max"))
                if tp_up is not None and base_up > 0 and int((head_down_tp or {}).get("count") or 0) >= MIN_SAMPLES:
                    ceiling = tp_up / base_up - 0.01
                    candidate = min(candidate, _clamp("head_up_ratio", ceiling))
                if candidate != head_up_ratio:
                    suggested["head_up_ratio"] = _round(candidate)
                    fp_pushed.add("head_up_ratio")
                    reasons.append(
                        f"低头误报 {fp_n} 条：head_up_ratio 建议 {head_up_ratio:.3f}→{candidate:.3f}，"
                        "让低头阈值更严格。"
                    )

    if head_fwd_drop is not None and fwd_fp_buckets:
        fp_n = sum(int(r.get("count") or 0) for r in fwd_fp_buckets)
        if fp_n >= MIN_SAMPLES:
            # Average fwd of the false positives; a larger drop pushes the
            # threshold down so those angles no longer trigger.
            fps = [_finite(r.get("head_fwd", {}).get("avg")) for r in fwd_fp_buckets]
            fps = [v for v in fps if v is not None]
            if fps and baseline.head_fwd > 0:
                avg_fp = sum(fps) / len(fps)
                needed = baseline.head_fwd - avg_fp + 1.0
                candidate = _clamp("head_fwd_drop", max(head_fwd_drop, needed))
                candidate = min(candidate, head_fwd_drop + MAX_FWD_DROP_STEP)
                tp_fwd = [
                    _finite(r.get("head_fwd", {}).get("min"))
                    for r in fwd_tp_buckets
                    if int(r.get("count") or 0) >= MIN_SAMPLES
                ]
                tp_fwd = [v for v in tp_fwd if v is not None]
                if tp_fwd and baseline.head_fwd > 0:
                    ceiling = baseline.head_fwd - min(tp_fwd) + 1.0
                    candidate = min(candidate, _clamp("head_fwd_drop", ceiling))
                if candidate != head_fwd_drop:
                    suggested["head_fwd_drop"] = _round(candidate, 2)
                    fp_pushed.add("head_fwd_drop")
                    reasons.append(
                        f"前倾/侧歪误报 {fp_n} 条：head_fwd_drop 建议 "
                        f"{head_fwd_drop:.2f}→{candidate:.2f}，让夹角阈值更严格。"
                    )

    # Missed events push the opposite way: the trigger was too strict and
    # let a real problem pass.  Index missed stats by issue.
    missed_by_issue = {
        row["issue"]: row
        for row in missed_stats
        if isinstance(row.get("issue"), str)
    }
    missed_total = sum(int(r.get("count") or 0) for r in missed_stats)
    missed_head_down = missed_by_issue.get(HEAD_DOWN)
    missed_fwd = [
        missed_by_issue[issue]
        for issue in (LEAN, TOO_CLOSE)
        if issue in missed_by_issue
    ]

    if head_up_ratio is not None and missed_head_down:
        miss_n = int(missed_head_down.get("count") or 0)
        if miss_n >= MIN_SAMPLES:
            miss_up = _finite(missed_head_down.get("head_up", {}).get("avg"))
            base_up = _finite(baseline.head_up)
            if miss_up is not None and base_up and base_up > 0:
                # The missed head-up (e.g. 0.25) sits above the current
                # threshold (0.19); the ratio must rise so the threshold moves
                # up to catch it.  Stay under the false-positive-implied cap.
                needed = miss_up / base_up + 0.01
                candidate = _clamp("head_up_ratio", max(head_up_ratio, needed))
                candidate = min(candidate, head_up_ratio + MAX_HEAD_UP_STEP)
                if "head_up_ratio" in fp_pushed:
                    conflicts.append(
                        f"低头：{miss_n} 条漏报要求放宽、同时有误报要求收紧 "
                        "head_up_ratio，两者冲突，保持现值不动。"
                    )
                elif candidate != head_up_ratio:
                    suggested["head_up_ratio"] = _round(candidate)
                    reasons.append(
                        f"低头漏报 {miss_n} 条：head_up_ratio 建议 "
                        f"{head_up_ratio:.3f}→{candidate:.3f}，让低头阈值更宽松。"
                    )

    if head_fwd_drop is not None and missed_fwd:
        miss_n = sum(int(r.get("count") or 0) for r in missed_fwd)
        if miss_n >= MIN_SAMPLES:
            miss_fwds = [_finite(r.get("head_fwd", {}).get("avg")) for r in missed_fwd]
            miss_fwds = [v for v in miss_fwds if v is not None]
            if miss_fwds and baseline.head_fwd > 0:
                avg_miss = sum(miss_fwds) / len(miss_fwds)
                # A missed fwd (e.g. 155) is above the current threshold
                # (149); a smaller drop raises the threshold to catch it.
                needed = baseline.head_fwd - avg_miss - 1.0
                candidate = _clamp("head_fwd_drop", min(head_fwd_drop, needed))
                candidate = max(candidate, head_fwd_drop - MAX_FWD_DROP_STEP)
                if "head_fwd_drop" in fp_pushed:
                    conflicts.append(
                        f"前倾/侧歪：{miss_n} 条漏报要求放宽、同时有误报要求收紧 "
                        "head_fwd_drop，两者冲突，保持现值不动。"
                    )
                elif candidate != head_fwd_drop:
                    suggested["head_fwd_drop"] = _round(candidate, 2)
                    reasons.append(
                        f"前倾/侧歪漏报 {miss_n} 条：head_fwd_drop 建议 "
                        f"{head_fwd_drop:.2f}→{candidate:.2f}，让夹角阈值更宽松。"
                    )

    # A conflict wins over any relaxation on that knob.
    for knob in ("head_up_ratio", "head_fwd_drop"):
        if any(knob in c for c in conflicts):
            suggested[knob] = current.get(knob, suggested[knob])

    changed = {k: v for k, v in suggested.items() if v != current.get(k)}
    combined = total_usable + missed_total
    if combined < MIN_SAMPLES:
        confidence = "low"
        note = (
            f"标注样本不足（播报判定 {total_usable} 条、漏报 {missed_total} 条，"
            f"合计 {combined} 条，至少 {MIN_SAMPLES} 条才给建议），暂不建议调整。"
        )
    elif combined < 20:
        confidence = "medium"
        note = f"标注样本偏少（播报判定 {total_usable} 条、漏报 {missed_total} 条），建议仅供参考，先小步调整再观察。"
    else:
        confidence = "high"
        note = (
            f"标注样本较充分（播报判定 {total_usable} 条、漏报 {missed_total} 条），"
            "建议可供人工审阅后应用。"
        )

    if not reasons and combined >= MIN_SAMPLES:
        reasons.append("当前标注下未发现需要调整的方向；保持现有阈值。")
    reasons.extend(conflicts)

    current_thresholds = thresholds_for(
        baseline,
        head_up_ratio=current.get("head_up_ratio", 0.5),
        head_fwd_drop=current.get("head_fwd_drop", 15.0),
        too_close_ratio=current.get("too_close_ratio", 1.15),
    )
    suggested_thresholds = thresholds_for(
        baseline,
        head_up_ratio=suggested.get("head_up_ratio", 0.5),
        head_fwd_drop=suggested.get("head_fwd_drop", 15.0),
        too_close_ratio=suggested.get("too_close_ratio", 1.15),
    )

    return {
        "baseline": baseline.as_dict(),
        "current_policy": current,
        "suggested_policy": suggested,
        "changes": changed,
        "current_thresholds": current_thresholds,
        "suggested_thresholds": suggested_thresholds,
        "labelled_total": total_labelled,
        "labelled_usable": total_usable,
        "missed_total": missed_total,
        "missed_by_issue": {
            issue: int(missed_by_issue[issue].get("count") or 0)
            for issue in ISSUES
            if issue in missed_by_issue
        },
        "conflicts": conflicts,
        "by_bucket": [
            {
                "issue": r.get("issue"),
                "verdict": r.get("verdict"),
                "count": r.get("count"),
            }
            for r in stats
        ],
        "per_issue": {issue: _issue_counts(by_bucket, issue) for issue in ISSUES},
        "confidence": confidence,
        "note": note,
        "reasons": reasons,
    }


def _issue_counts(
    by_bucket: dict[tuple[str, str], dict[str, object]], issue: str
) -> dict[str, int]:
    return {
        "true_positive": int(
            by_bucket.get((issue, "true_positive"), {}).get("count") or 0
        ),
        "false_positive": int(
            by_bucket.get((issue, "false_positive"), {}).get("count") or 0
        ),
    }

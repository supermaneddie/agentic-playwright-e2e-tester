import hashlib
import json
import re


def running_step(run):
    """Return the one-based current step, retaining paused attempts on retry."""
    if not run or run.get("status") not in {"running", "waiting_for_input"}:
        return None
    completed = [result for result in run.get("results", []) if not result.get("superseded")]
    if any(result.get("fatal") or (result.get("dependency") and result["outcome"] != "passed")
           for result in completed):
        return None
    index = max((result["step"] for result in completed), default=0) + 1
    return index if index <= len(run.get("checklist", [])) else None


def test_groups(run):
    """Group dependency evidence without treating partial or superseded results as a pass."""
    if not run:
        return []
    groups, by_key, by_step = [], {}, {}

    def group_for(dependency):
        key = dependency["case_id"] if dependency else "main"
        if key not in by_key:
            group = {"key": key, "dependency": dependency, "steps": [], "results": []}
            by_key[key] = group
            groups.append(group)
        return by_key[key]

    for index, step in enumerate(run.get("checklist", []), 1):
        group = group_for(step.get("dependency"))
        group["steps"].append({"index": index, "step": step})
        by_step[index] = group
    for result in run.get("results", []):
        group = by_step.get(result.get("step"))
        if group is None:
            group = group_for(result.get("dependency"))
        group["results"].append(result)
    active = running_step(run)
    for group in groups:
        dependency = group["dependency"]
        expected = {step["index"] for step in group["steps"]}
        if dependency:
            expected.update(range(dependency["start"], dependency["end"] + 1))
        final = {result.get("step"): result for result in group["results"] if not result.get("superseded")}
        passed = sum(final.get(index, {}).get("outcome") == "passed" for index in expected)
        outcomes = {result["outcome"] for result in final.values()}
        if "error" in outcomes:
            outcome = "error"
        elif "failed" in outcomes:
            outcome = "failed"
        elif expected and passed == len(expected):
            outcome = "passed"
        elif active in expected:
            outcome = run["status"]
        elif run.get("status") == "stopped":
            outcome = "stopped" if group["results"] else "not_run"
        elif run.get("status") in {"passed", "failed", "error"}:
            outcome = "incomplete" if group["results"] else "not_run"
        else:
            outcome = "pending"
        group.update(outcome=outcome, passed=passed, total=len(expected))
    return groups


def description_segments(description, checklist):
    """Highlight only literal target phrases; keep all user content subject to HTML escaping."""
    targets = {action.get("target", "").strip()
               for step in checklist or [] if step.get("kind") == "browser"
               for action in step.get("actions", []) if action.get("target")}
    targets.discard("")
    if not targets:
        return [{"text": description, "matched": False}]
    pattern = re.compile(r"(?<!\w)(?:" + "|".join(re.escape(t) for t in sorted(targets, key=lambda t: (-len(t), t)))
                         + r")(?!\w)", re.IGNORECASE)
    segments, start = [], 0
    for match in pattern.finditer(description):
        segments.append({"text": description[start:match.start()], "matched": False})
        segments.append({"text": match.group(), "matched": True})
        start = match.end()
    segments.append({"text": description[start:], "matched": False})
    return segments


def section_updates(templates, sections, known_versions):
    """Render only changed Jinja sections; clients patch their existing DOM nodes."""
    versions, changes = {}, {}
    for key, (data, template_name, context) in sections.items():
        version = hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()
        versions[key] = version
        if known_versions.get(key) != version:
            changes[key] = templates.env.get_template(template_name).render(**context)
    return {"versions": versions, "sections": changes}

"""Shared guidance for interpreting natural-language UI targets."""

import re


QUOTED_TARGET = re.compile(r'''(?<!\w)(?:"([^"\n]+)"|“([^”\n]+)”|'([^'\n]+)'|‘([^’\n]+)’)(?!\w)''')


def exact_target(target: str) -> str | None:
    """The first quoted phrase names the target; subsequent phrases may describe context."""
    match = QUOTED_TARGET.search(target)
    return " ".join(next(value for value in match.groups() if value is not None).split()) if match else None


def preserve_target_quotes(plan, description: str):
    """Restore source quotes if a generated target retained the phrase but dropped its delimiters."""
    quoted = list(QUOTED_TARGET.finditer(description))
    for step in plan.steps:
        if step.kind != "browser":
            continue
        for item in [*step.actions, *step.assertions]:
            if not item.target or exact_target(item.target) is not None:
                continue
            for match in quoted:
                label = next(value for value in match.groups() if value is not None)
                if not label.strip():
                    continue
                phrase = re.compile(r"(?<!\w)" + r"\s+".join(re.escape(word) for word in label.split())
                                    + r"(?!\w)", re.IGNORECASE)
                if phrase.search(item.target):
                    item.target = phrase.sub(lambda _: match.group(), item.target, count=1)
                    break
    return plan


TARGET_MATCHING_GUIDANCE = """
Quoted target phrases require a case-insensitive, exact full-label match, ignoring capitalization
and whitespace layout differences. Preserve all words: "Student Role" can match Student Role or
student role, but not Student or Student Role Login. Straight and curly quotation marks both count.
The quotes are delimiters, not part of the visible label. Never relax quoted matching during fallback.
For a description containing a quoted target and surrounding context, match that target exactly
and use the surrounding context only to locate it.
UNQUOTED targets are natural-language descriptions, not literal text assertions. Interpret their
meaning using visible labels and context. For example, unquoted Student role can match a role
option labeled Student; unquoted Sign in button can match Sign in; unquoted Email field can match Email.
Words such as role, button, field, link, and tab may describe the control's purpose or type;
they do not have to appear in its visible label. Do not blindly remove these words when they
are part of a real name (for example "Role Management"). Preserve meaningful distinctions:
"Student" is not interchangeable with "Teacher", and a Student role option is not necessarily
a "Student Login" navigation link. Include alternatives only when context makes them plausible.
User-supplied literal expected text remains literal; semantic target matching must not rewrite it.
"""

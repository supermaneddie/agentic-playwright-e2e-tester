import re
from typing import Any

SENSITIVE = re.compile(r"password|passwd|secret|token|api[_-]?key|authorization|cookie|credential", re.I)


def sensitive_question(question: dict) -> bool:
    key = question.get("key", "")
    return bool(question.get("secret") or key in {"headers", "auth_headers"}
                or (not key.endswith("_selector") and SENSITIVE.search(key)))


def secret_values(value: Any) -> list[str]:
    found = []
    if isinstance(value, dict):
        for key, item in value.items():
            if SENSITIVE.search(key) and isinstance(item, str) and item:
                found.append(item)
                if item.lower().startswith("bearer "):
                    found.append(item[7:])
            found.extend(secret_values(item))
    elif isinstance(value, list):
        for item in value:
            found.extend(secret_values(item))
    return found


def redact(value: Any, secrets: list[str] | None = None) -> Any:
    secrets = secrets or []
    if isinstance(value, dict):
        return {k: "[redacted]" if SENSITIVE.search(k) and not isinstance(v, bool) else redact(v, secrets) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(item, secrets) for item in value]
    if isinstance(value, str):
        for secret in sorted(set(secrets), key=len, reverse=True):
            if secret:
                value = value.replace(secret, "[redacted]")
    return value

from contextlib import asynccontextmanager

import json
import re
from time import monotonic
from urllib.parse import urlsplit

import httpx

from app.config import TestingConfig
from app.models import Step

PLACEHOLDER = re.compile(r"\{\{\s*([A-Za-z_][\w.]*)\s*\}\}")


def lookup(context, path):
    current = context
    for key in path.split("."):
        current = current[int(key)] if isinstance(current, list) else current[key]
    return current


def resolve(value, context):
    if isinstance(value, str):
        full = PLACEHOLDER.fullmatch(value)
        if full:
            return lookup(context, full[1])
        return PLACEHOLDER.sub(lambda match: str(lookup(context, match[1])), value)
    if isinstance(value, dict):
        return {key: resolve(item, context) for key, item in value.items()}
    if isinstance(value, list):
        return [resolve(item, context) for item in value]
    return value


def missing_variables(value, context):
    missing = set()
    for match in PLACEHOLDER.finditer(json.dumps(value)):
        try:
            found = lookup(context, match[1])
            if found is None or found == "":
                missing.add(match[1].split(".")[0])
        except (KeyError, IndexError, TypeError, ValueError):
            missing.add(match[1].split(".")[0])
    return sorted(missing)


def target_url(base_url: str, path: str) -> str:
    base = urlsplit(base_url)
    if base.scheme not in {"http", "https"} or not base.hostname or base.username or base.password:
        raise ValueError("base_url must be HTTP(S), without embedded credentials")
    if base.query or base.fragment:
        raise ValueError("base_url must not contain query parameters or a fragment")
    if not path.startswith("/") or path.startswith("//") or "\\" in path:
        raise ValueError("Step path must be relative, starting with one /; external URLs are not allowed")
    url = base_url.rstrip("/") + path
    parsed = urlsplit(url)
    if (parsed.scheme, parsed.netloc) != (base.scheme, base.netloc):
        raise ValueError("Step cannot change the target origin")
    return url


class HTTPRunner:
    def __init__(self, settings: TestingConfig, transport=None):
        self.settings = settings
        self.transport = transport
        self.sessions = {}

    @asynccontextmanager
    async def client(self, run_id=None):
        if run_id is not None and run_id in self.sessions:
            yield self.sessions[run_id]
            return
        client = httpx.AsyncClient(timeout=self.settings.request_timeout, follow_redirects=False,
                                   trust_env=False, transport=self.transport)
        if run_id is not None:
            self.sessions[run_id] = client
        try:
            yield client
        finally:
            if run_id is None:
                await client.aclose()

    async def close_run(self, run_id):
        client = self.sessions.pop(run_id, None)
        if client:
            await client.aclose()

    async def close(self):
        for run_id in list(self.sessions):
            await self.close_run(run_id)

    async def execute(self, step: Step, context: dict, run_id=None):
        resolved = Step.model_validate(resolve(step.model_dump(), context))
        url = target_url(str(context["base_url"]), resolved.path)
        headers = httpx.Headers(context.get("headers", {}))
        headers.update(resolved.headers)
        headers.update(context.get("auth_headers", {}))
        started = monotonic()
        try:
            async with self.client(run_id) as client:
                async with client.stream(resolved.method, url, headers=headers, json=resolved.body) as response:
                    content = bytearray()
                    async for chunk in response.aiter_bytes():
                        content.extend(chunk)
                        if len(content) > self.settings.max_response_bytes:
                            return {"title": step.title, "outcome": "error", "status_code": response.status_code,
                                    "message": "Response exceeded configured size limit; assertions were not evaluated.",
                                    "assertions": []}
                    body = bytes(content).decode(response.encoding or "utf-8", errors="replace")
                    checks = []
                    for assertion in resolved.assertions:
                        try:
                            if assertion.type == "status":
                                actual = response.status_code
                                passed = actual == assertion.expected
                            elif assertion.type == "body_contains":
                                passed = assertion.expected in body
                            elif assertion.type == "header_contains":
                                passed = assertion.expected in response.headers.get(assertion.path, "")
                            else:
                                actual = json.loads(body)
                                if assertion.path:
                                    actual = lookup(actual, assertion.path)
                                passed = actual == assertion.expected
                            checks.append({"type": assertion.type, "path": assertion.path, "passed": passed})
                        except (ValueError, KeyError, IndexError, TypeError):
                            checks.append({"type": assertion.type, "path": assertion.path, "passed": False,
                                           "error": "Response is not valid JSON or the requested path is missing"})
                    return {
                        "title": step.title, "method": resolved.method,
                        "status_code": response.status_code,
                        "duration_ms": round((monotonic() - started) * 1000),
                        "outcome": "passed" if all(check["passed"] for check in checks) else "failed",
                        "assertions": checks,
                    }
        except httpx.HTTPError as exc:
            # Exception text and response bodies may contain credentials; keep them out of logs.
            return {"title": step.title, "outcome": "error", "message": f"HTTP request failed ({type(exc).__name__})",
                    "assertions": []}

import base64
import binascii
import re
from typing import Any, Literal

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, model_validator

from app.privacy import sensitive_question


class CaseImage(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    name: str = Field(min_length=1, max_length=255)
    data_url: str = Field(max_length=7_000_000, repr=False)

    @model_validator(mode="after")
    def validate_image(self):
        match = re.fullmatch(r"data:image/(png|jpeg|webp);base64,([A-Za-z0-9+/=]+)", self.data_url)
        if not match:
            raise ValueError("Images must be base64 PNG, JPEG or WebP data URLs")
        try:
            data = base64.b64decode(match[2], validate=True)
        except (ValueError, binascii.Error):
            raise ValueError("Invalid image encoding") from None
        if not 0 < len(data) <= 5 * 1024 * 1024:
            raise ValueError("Each image must be at most 5 MiB")
        signatures = {
            "png": data.startswith(b"\x89PNG\r\n\x1a\n"),
            "jpeg": data.startswith(b"\xff\xd8\xff"),
            "webp": data.startswith(b"RIFF") and data[8:12] == b"WEBP",
        }
        if not signatures[match[1]]:
            raise ValueError("Image content does not match its format")
        return self


class CaseInputs(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    name: str = Field(default="", min_length=1, max_length=200)
    description: str = Field(min_length=1, max_length=20000, validation_alias=AliasChoices("description", "descrition"))
    test_data: dict[str, Any] = Field(default_factory=dict)
    prompt: str | None = Field(default=None, max_length=20000)
    test_type: Literal["http", "browser"] = "http"
    depends_on: list[str] = Field(default_factory=list, max_length=100)
    images: list[CaseImage] = Field(default_factory=list, max_length=10)

    @model_validator(mode="after")
    def image_total(self):
        if sum(len(image.data_url.split(",", 1)[1]) * 3 // 4 - image.data_url.count("=")
               for image in self.images) > 20 * 1024 * 1024:
            raise ValueError("Images must total at most 20 MiB")
        return self


class Submission(CaseInputs):
    project_id: str = Field(min_length=1, max_length=200)


class CaseUpdate(CaseInputs):
    confirm_clear_results: Literal[True]


class ProjectSubmission(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    name: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=20000)
    test_data: dict[str, Any] = Field(default_factory=dict)
    prompt: str | None = Field(default=None, max_length=20000)


class Answer(BaseModel):
    answers: dict[str, Any] = Field(min_length=1)


class SavedDataUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: str
    answers: dict[str, Any] = Field(default_factory=dict)
    remove_answers: list[str] = Field(default_factory=list)
    remove_inspections: list[str] = Field(default_factory=list)


class InspectionQuery(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    run_id: str
    key: str
    mode: Literal["prompt", "css"]
    query: str = Field(default="", max_length=2000)
    action: Literal["locate", "preview", "scroll"] = "locate"
    full_page: bool = True
    direction: Literal["up", "down", "left", "right"] = "down"
    x: float = Field(default=0.5, ge=0, le=1, allow_inf_nan=False)
    y: float = Field(default=0.5, ge=0, le=1, allow_inf_nan=False)

    @model_validator(mode="after")
    def require_locate_query(self):
        if self.action == "locate" and not self.query:
            raise ValueError("Provide an element description or CSS selector")
        return self


class PortalPoll(BaseModel):
    versions: dict[str, str] = Field(default_factory=dict, max_length=2000)
    run_id: str | None = None
    after_log_id: int = Field(default=0, ge=0)


class Requirement(BaseModel):
    key: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    question: str = Field(min_length=1, max_length=1000)
    secret: bool = False

    @model_validator(mode="after")
    def infer_sensitive_input(self):
        self.secret = sensitive_question(self.model_dump())
        return self


class Assertion(BaseModel):
    type: Literal["status", "body_contains", "json_equals", "header_contains"]
    path: str = ""
    expected: Any

    @model_validator(mode="after")
    def validate_expected(self):
        placeholder = isinstance(self.expected, str) and re.fullmatch(r"\{\{\s*[A-Za-z_][\w.]*\s*\}\}", self.expected)
        if self.type == "status" and not placeholder and (type(self.expected) is not int or not 100 <= self.expected <= 599):
            raise ValueError("status assertion expected must be an integer HTTP status")
        if self.type in {"body_contains", "header_contains"} and not isinstance(self.expected, str):
            raise ValueError("contains assertions require a string")
        if self.type == "header_contains" and not self.path:
            raise ValueError("header_contains requires a header name in path")
        return self


class Step(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["http"] = "http"
    title: str = Field(min_length=1, max_length=500)
    method: Literal["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"] = "GET"
    path: str = Field(min_length=1)
    headers: dict[str, str] = Field(default_factory=dict)
    body: Any = None
    assertions: list[Assertion] = Field(min_length=1, max_length=30)


class BrowserAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["goto", "click", "fill", "fill_from_image", "select_option", "press", "check", "uncheck", "wait_for"]
    selector: str | None = None
    target: str | None = Field(default=None, max_length=500)
    path: str | None = None
    value: str | None = None
    if_present: bool = False
    image_selector: str | None = None
    image_target: str | None = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def required_fields(self):
        if self.action == "goto" and self.if_present:
            raise ValueError("goto cannot be conditional on element presence")
        if self.action == "goto":
            if not self.path:
                raise ValueError("goto requires a path or same-origin URL")
        elif not self.selector and not self.target:
            raise ValueError("This action requires a selector or a description of the target")
        if self.action in {"fill", "select_option", "press"} and self.value is None:
            raise ValueError("This action requires a value")
        if self.action == "fill_from_image":
            if not self.image_selector and not self.image_target:
                raise ValueError("fill_from_image requires image_selector or image_target")
            if self.value is not None:
                raise ValueError("fill_from_image reads its value from the image")
        elif self.image_selector is not None or self.image_target is not None:
            raise ValueError("Image fields require fill_from_image")
        return self


class BrowserAssertion(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["visible", "hidden", "text_contains", "text_equals", "url_contains", "title_contains", "value_equals", "checked"]
    selector: str | None = None
    target: str | None = Field(default=None, max_length=500)
    expected: str | None = None
    expected_from_action: int | None = Field(default=None, ge=1, le=50, strict=True,
        description="1-based index of a fill_from_image action in this step whose transcribed text is expected.")
    if_present: bool = False

    @model_validator(mode="after")
    def required_fields(self):
        if self.if_present and self.type in {"url_contains", "title_contains"}:
            raise ValueError("Presence conditions require an element assertion")
        if self.type not in {"url_contains", "title_contains"} and not self.selector and not self.target:
            raise ValueError("Element assertions require a selector or a target")
        if self.expected_from_action is not None and (self.type != "value_equals" or self.expected is not None):
            raise ValueError("expected_from_action requires value_equals without literal expected text")
        if (self.type in {"text_contains", "text_equals", "url_contains", "title_contains", "value_equals"}
                and self.expected is None and self.expected_from_action is None):
            raise ValueError("This assertion requires expected text")
        return self


class BrowserStep(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["browser"] = "browser"
    title: str = Field(min_length=1, max_length=500)
    max_retries: int = Field(default=0, ge=0, le=5, strict=True)
    actions: list[BrowserAction] = Field(min_length=1, max_length=50)
    assertions: list[BrowserAssertion] = Field(min_length=1, max_length=30)

    @model_validator(mode="after")
    def validate_action_expectations(self):
        for assertion in self.assertions:
            index = assertion.expected_from_action
            if index is not None and (index > len(self.actions) or self.actions[index - 1].action != "fill_from_image"):
                raise ValueError("expected_from_action must reference a fill_from_image action in this step")
        return self


class Plan(BaseModel):
    summary: str = Field(min_length=1, max_length=4000)
    requirements: list[Requirement] = Field(default_factory=list, max_length=30)
    steps: list[Step | BrowserStep] = Field(min_length=1, max_length=100)


class HTTPPlan(Plan):
    steps: list[Step] = Field(min_length=1, max_length=100)


class BrowserPlan(Plan):
    steps: list[BrowserStep] = Field(min_length=1, max_length=100)

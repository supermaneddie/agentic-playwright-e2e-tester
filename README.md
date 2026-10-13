# Agentic Playwright E2E Tester

A Python service and Jinja2 web portal for HTTP/API and browser UI testing. LangGraph builds a checklist, executes HTTP requests or Playwright browser actions, pauses for missing information, and resumes from a saved checkpoint. Test records, attempt history, and checkpoints are persisted in SQLite.

## Run locally

Requires Python 3.10+ and an OpenAI-compatible chat completions endpoint.

For initial setup, copy the example configuration:

```bash
cp example_config.yml config.yml
```

Edit `config.yml` to set `llm.url` and `llm.model` for your provider, and configure `llm.api_key` if required. The example contains a local network endpoint; replace it with one you can access. If you already have a configured `config.yml`, keep it and use `example_config.yml` as a reference.

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
# Required for browser tests (HTTP tests work without downloading Chromium):
python -m playwright install chromium
python -m app
```

Open **http://localhost:8091**. Interactive API documentation is at **/docs**.
`requirements.txt` lists the runtime dependencies directly. Package builds also read this file through `pyproject.toml`, keeping one dependency list. For editable development with test/lint tools, use `python -m pip install -e '.[dev]'` instead.
Use `python -m pip` so installation targets the same interpreter that runs `python -m app`.
On Debian/Ubuntu, install the matching `python3-venv` package if virtual environment creation fails.
If Chromium reports missing system libraries, use `python -m playwright install --with-deps chromium`. Installing system libraries may require administrator access. See the official [Playwright browser setup](https://playwright.dev/python/docs/browsers).

For example, your `config.yml` could contain:

```yaml
llm:
  url: http://localhost:11434/v1
  model: qwen2.5:14b
  # api_key: ${LLM_API_KEY}
server:
  host: 0.0.0.0
  port: 8091
storage:
  directory: ./data
testing:
  request_timeout: 30
  max_response_bytes: 1048576
  max_steps: 30
browser:
  headless: true
  action_timeout: 15
  step_timeout: 120
  screenshots: true
  max_sessions: 4
  auto_selectors: true
  vision: true
```

`llm.url` is the provider's base URL; the application appends `/chat/completions`. The example model must already be available on your provider. No Authorization header is sent when `api_key` is omitted. `${VARIABLE}` references are expanded from the environment; missing variables cause a startup error. Set `CONFIG_FILE=/path/to/config.yml` to load a different file. Storage paths are relative to the working directory.

## Portal

- **Index:** saved projects with their test case counts. Open a project to manage shared inputs and its test cases. Counts refresh every 10 seconds.
- **Details:** start/stop/retry/delete, checklist, assertion results, browser screenshots, LangGraph node log, progress log, and previous attempt results. AJAX polling every 3 seconds patches changed text and attributes in place and appends new log entries. Forms, typed answers, collapsed groups and scroll containers stay attached to the page.
- **Projects:** create and edit a name, description, JSON test data and additional instructions. Save the project before creating test cases. Project descriptions and instructions are prepended to case inputs; JSON objects merge recursively, with case values overriding shared values (including arrays). Changes apply to new runs and retries; active runs retain their original context.
- **Delete:** available on project pages and case details, with confirmation. Deleting a project also deletes all its test cases. Stops any active run and permanently removes the case, all attempts, logs, screenshots and LangGraph checkpoints. Other cases are preserved.
- **Saved answers and inspections:** open **Saved answers & inspections → View/Edit** from a test case details page. Edit or remove individual answers and inspect or remove cached element locations. Secrets stay hidden; leave their replacement fields blank to keep them. Save while the test is inactive; changes apply to the next retry. Reload if another run or editor changed the saved data.
- **Missing information:** answer the questions in the details page. Header values use a JSON object; other answers accept plain text or JSON values. Supply passwords/tokens through the requested fields rather than free-text descriptions.

## API

| Method | Path | Purpose |
| --- | --- | --- |
| POST | `/api/projects` | Save a project; returns `project_id` (201) |
| GET | `/api/projects` | List projects with `testcase_count` |
| GET | `/api/projects/{id}` | Project inputs and related test cases |
| PUT | `/api/projects/{id}` | Replace project inputs |
| DELETE | `/api/projects/{id}` | Delete project and all related test cases (204) |
| POST | `/api/testcases` | Submit a case under a saved `project_id`; returns a generated `testcase_id` (201) |
| GET | `/api/testcases` | List cases with latest status |
| GET | `/api/testcases/{id}` | Case, attempts, current questions, results and logs |
| DELETE | `/api/testcases/{id}` | Stop and permanently delete a case and its associated data (204) |
| POST | `/api/testcases/{id}/plan` | Prepare a checklist without executing tests (202) |
| POST | `/api/testcases/{id}/start` | Start a submitted or planned case asynchronously; reuse its prepared checklist (202) |
| POST | `/api/testcases/{id}/stop` | Cancel an active or waiting run |
| POST | `/api/testcases/{id}/retry` | Create a fresh attempt after a terminal run (202) |
| POST | `/api/testcases/{id}/inspection` | Locate matches for a pending inspection question using a prompt or CSS selector and a screenshot |
| GET | `/api/testcases/{id}/saved-data` | View saved answers (secrets omitted), inspection metadata and revision |
| PATCH | `/api/testcases/{id}/saved-data` | Update existing `answers`, `remove_answers` and `remove_inspections` using the returned `revision`; inactive tests only |
| POST | `/api/testcases/{id}/clear-answers` | Forget saved answers and element inspections while the test is inactive |
| POST | `/api/testcases/{id}/answers` | Supply requested information and resume (202) |
| GET | `/health` | Application liveness |

Submit a natural-language case:

```bash
curl -X POST http://localhost:8091/api/projects \
  -H 'Content-Type: application/json' \
  -d '{"name":"Orders API","description":"Shared API context","test_data":{"base_url":"https://api.example.com"},"prompt":"Use test accounts only"}'

# Use the returned project_id as PROJECT_ID below.
curl -X POST http://localhost:8091/api/testcases \
  -H 'Content-Type: application/json' \
  -d '{
    "project_id": "PROJECT_ID",
    "description": "GET /health should return HTTP 200 and JSON with status equal to ok.",
    "test_data": {"base_url": "https://your-api.example"},
    "prompt": "Only run the requested health check."
  }'
```

`project_id` (an existing saved project) and `description` are required. `test_data` is a JSON object (defaults to `{}`); `prompt` is optional. `test_type` is `http` (the default) or `browser`. Existing submissions remain HTTP tests. `descrition` is accepted as an alias for `description`.

```bash
curl -X POST http://localhost:8091/api/testcases/TESTCASE_ID/start

curl http://localhost:8091/api/testcases/TESTCASE_ID

# When the current questions request base_url:
curl -X POST http://localhost:8091/api/testcases/TESTCASE_ID/answers \
  -H 'Content-Type: application/json' \
  -d '{"answers": {"base_url": "https://your-api.example"}}'

# When an unexpected 401/403 prompts for auth_headers:
curl -X POST http://localhost:8091/api/testcases/TESTCASE_ID/answers \
  -H 'Content-Type: application/json' \
  -d '{"answers": {"auth_headers": {"Authorization": "Bearer YOUR_TOKEN"}}}'
```

An answers request must provide exactly the keys in the current `run.questions`. An invalid state transition returns 409; unknown IDs return 404; invalid input returns 422.

To delete a case through the API:

```bash
curl -X DELETE http://localhost:8091/api/testcases/TESTCASE_ID
```

Successful deletion returns 204 with no body; deleting an unknown ID returns 404. Active execution is cancelled before data is removed. Deleting a test does not undo changes already made to the target application.

## HTTP execution model

```text
summarize_checklist → prepare_test → execute_http_test → next step / end
                           ↓                 ↓
                    ask_for_information ← unexpected 401/403
                           ↓
                    prepare_test (after resume)
```

The planner converts descriptions into a validated JSON plan. The HTTP runner executes the plan with `httpx` and determines pass/fail from actual assertions, without relying on an LLM verdict. Available assertions:

| Type | `expected` | `path` |
| --- | --- | --- |
| `status` | Integer HTTP status | Unused |
| `body_contains` | Substring | Unused |
| `json_equals` | JSON value | Dot-separated keys/indexes, e.g. `users.0.id`; empty for whole body |
| `header_contains` | Substring | Header name, case-insensitive |

For deterministic regression tests, supply `test_data.steps` to bypass the model:

```json
{
  "project_id": "PROJECT_ID",
  "description": "Check a protected order endpoint",
  "test_data": {
    "base_url": "https://your-api.example/v1",
    "order_id": 42,
    "requirements": [
      {"key": "token", "question": "What bearer token should this test use?", "secret": true}
    ],
    "steps": [{
      "title": "Order is accessible",
      "method": "GET",
      "path": "/orders/{{order_id}}",
      "headers": {"Authorization": "Bearer {{token}}"},
      "assertions": [
        {"type": "status", "expected": 200},
        {"type": "json_equals", "path": "id", "expected": "{{order_id}}"}
      ]
    }]
  }
}
```

`{{variable}}` and `{{object.field}}` placeholders are resolved from test data and answers. A value consisting entirely of a placeholder preserves its JSON type. Request JSON can be supplied in `body`. Global `test_data.headers` are combined with per-step headers; authentication corrections in `auth_headers` take precedence. Paths append to `base_url`, including its path prefix. Absolute URLs and changes to the target origin are rejected. Redirects are reported as responses and are not followed.

The runner asks for missing variables before each step. Unexpected HTTP 401/403 responses prompt for corrected headers and retry the same step at most twice. Expected 401/403 assertions do not prompt. Earlier authentication responses stay in the evidence as superseded attempts. Other failed assertions are recorded and execution continues to the next step.

Use **Plan** before **Start** to review the checklist before execution. Once planning completes, the case is `planned`; the button stays available as **Re-plan** to generate a fresh checklist in a new attempt while preserving previous attempts. **Start** executes the latest prepared checklist without planning again. Planning is also available after a finished or stopped attempt; stop an active run before re-planning. Starting a submitted case still plans automatically. Editing a case clears its prepared checklist.

Statuses: `submitted`, `planned`, `running`, `waiting_for_input`, `passed`, `failed`, `error`, `stopped`. `failed` means one or more request/assertion results failed; `error` means the workflow could not proceed, such as an invalid model plan or an unavailable LLM endpoint.

## Browser / UI tests

Select **Browser / UI (Chromium)** when creating a case, or submit `"test_type": "browser"` through the same API. Natural-language descriptions use the configured LLM to produce browser steps. You can describe targets such as **Email field**, **Password field**, and **Sign in button** without providing CSS selectors.

With `browser.auto_selectors: true` (the default), the runner navigates to the page and inspects it immediately before each action that needs a target. It first sends a masked full-page screenshot to the configured image-capable LLM to locate what the user sees. Screenshot coordinates identify the element; the click uses the center of its current bounding box after scrolling and browser hit testing, including text inside delegated click handlers and tabs. If visual matching finds no target or image processing fails, it falls back to a bounded list of visible DOM elements, including:

- Placeholders, input names, types and autocomplete hints.
- Explicit `<label for="…">` associations and labels wrapping an input.
- `aria-label`, `aria-labelledby`, and supporting `aria-describedby` text.
- Nearby visible label text, with its direction and pixel distance from the field.
- Form/group headings and names, to distinguish similar fields in different sections.

The model considers these together: a placeholder alone is insufficient when it appears on multiple fields, and nearby text does not override a conflicting explicit label. The model selects an observed element ID; the runner constructs and validates the locator itself. It never executes model-generated JavaScript or selectors. Explicit selectors you supply continue to work without an inspection call.

Unquoted targets describe meaning, not necessarily literal labels: `Student role` can match a role option labeled Student, and `Sign in button` can match Sign in. Both screenshot and DOM matching use the requested control's purpose to distinguish alternatives such as a Student role option and a Student Login link. Once a plan is available, its target phrases are bolded where they occur in the test description and in the action checklist. The description updates as planning finishes. Explicit text assertions still require their literal expected text; the planner is instructed not to copy descriptive words such as “role” into invented text expectations. Retry an existing case to regenerate its plan with this guidance.

Quotation marks in the actual test description request exact, case-insensitive full-label matching. For example, `Click "Student Role"` can match `Student Role` or `student role`, but not `Student` or `Student Role Login`; `Click Student role` remains semantic. Straight and curly quotation marks are supported. Capitalization and layout whitespace are ignored. The planner preserves quoted targets, and both screenshot hit testing and DOM fallback enforce the full label without relaxing it.

If a quoted label is missing, the model can propose a translated equivalent observed on the page. For example, when `"Student"` is absent but `學生` is present, the test asks whether to use `學生`. Nothing is clicked until you explicitly accept. The approved label is then matched exactly against the current page; rejecting keeps the original label and asks for clarification. Approval is scoped to the target within that test step and also applies to assertions locating the same target, but it never changes an assertion's literal expected text.

Screenshot-first matching is enabled by default (`browser.vision: true`) and needs an image-capable model. Set `browser.vision: false` for DOM-only analysis with a **text-only LLM**. Form fields are masked and known credential values are redacted from text, but page labels and screenshots can contain other application data; these are sent to your configured LLM endpoint. When a click has several visual matches, the runner tries each center in order, observes the UI after each, and stops on the first change. If none changes the UI, it performs DOM inspection, excluding the elements already clicked, and clicks the resulting target. An unavailable observation stops further attempts. Explicit assertions still determine success.

Multiple automatic visual click matches (such as Student and Student Login) are tried in order until the UI changes. Ambiguous DOM matches, ambiguous non-click targets, and failed automatic lookups open an inspection question. Choose **By prompt** or **By CSS selector**, enter a description or native CSS selector, and press **Locate with LLM**. Use **Show current view** and the **Scroll up/down/left/right** buttons to reveal off-screen content. For a scrollable panel, click inside it on the screenshot to choose where scrolling applies (this does not click the website). Scrolling refreshes the masked viewport screenshot without an LLM call and clears previous selections. Then press **Locate with LLM** to search the current view without resetting its scroll position. Each lookup independently sends the current masked screenshot and observed element rectangles to the LLM without resuming the test. Both modes require an image-capable model, even when automatic vision is disabled. Matches are numbered and highlighted on the screenshot; click a highlight or its result button to confirm one, then **Submit & resume**. Editing the query clears the selection. The picker offers rectangles fully inside the current viewport. Scroll again and repeat the lookup if the target is still outside it.

The confirmed rectangle is stored with the workflow answer. Click actions use its center exactly once, even if the UI does not visibly change; other operations use the confirmed element. The page, element identity, viewport, scroll position, geometry, and hit target are validated before reuse. A moved or stale target requires a fresh lookup. Successful runs also save these answers for retry. The current action position and any pending DOM fallback are saved, so answering resumes without repeating earlier navigation, visual attempts or submissions. Missing credentials, URLs and business expectations still require your input. Legacy `{{email_selector}}` placeholders are inspected automatically instead of being requested before navigation.

The runner launches Chromium on the **server**, headless by default. It does not control the browser displaying the portal. One isolated browser session is shared by all steps in an attempt, including while waiting for answers. Cookies and page state remain available until completion, stop, or server shutdown. Each retry launches a fresh session.

Supported actions:

| Action | Fields |
| --- | --- |
| `goto` | `path` relative to `base_url`, such as `/login`, or a full URL on the same origin (including `{{base_url}}`) |
| `click`, `check`, `uncheck`, `wait_for` | `target` description or explicit `selector` |
| `fill`, `select_option`, `press` | `target` description or explicit `selector`, plus `value` |
| `fill_from_image` | Destination `target` or `selector`, plus `image_target` or `image_selector`; no `value` |

To read a generated text image (such as a CAPTCHA), describe the image and answer field in your browser test,
or include an explicit action like:

```json
{"action": "fill_from_image", "image_target": "CAPTCHA image", "target": "CAPTCHA answer field"}
```

The runner captures the selected image element at execution time and sends its PNG plus a masked
viewport screenshot (when available) to the configured LLM at high detail. The crop provides the
characters, while the screen provides context to distinguish the CAPTCHA from labels and buttons.
It preserves the destination field reference while inspecting the source, then fills the returned text.
Errors distinguish source capture/transcription failures from destination field failures.
This requires `browser.vision: true` and an
image-capable model, even with explicit selectors. Image transcription is independent of saved result
screenshots (`browser.screenshots`). It supports text transcription, not interactive image puzzles.
Unreadable images or provider errors stop the step with an error; remaining actions are not executed.
The transcription is not stored in results or progress logs. Add a separate `click` action to submit
the form and an assertion to check the outcome. Image content is sent to your configured LLM endpoint.

Explicit selectors use Playwright CSS or text selector syntax. `select_option` selects by option value. `press` accepts a key such as `Enter`. `wait_for` waits for a visible element. Browser assertions are `visible`, `hidden`, `checked`, `text_contains`, `text_equals`, `value_equals`, `url_contains`, and `title_contains`. Element assertions accept `target` or `selector`; text/value/URL/title assertions need `expected`. For `hidden` assertions, prefer an explicit selector because an already absent element cannot be identified from the visible page snapshot.

Generated browser plans reference supplied JSON input fields with placeholders. For example, an email fill and its value assertion both use `{{email}}` (or `{{account.email}}` for nested data), resolved from test data at execution time. If the model copies a supplied string literally, the planner replaces it with its JSON reference when exactly one field matches. Explicit plans retain the values and expectations you specify; use placeholders there when you want data-driven checks.

Use `test_data.steps` for an explicit, repeatable browser plan. Every step has `kind: browser`, `actions`, and at least one assertion. The first case in a run must start with `goto`; cases following dependencies can continue from the existing page:

```json
{
  "project_id": "PROJECT_ID",
  "test_type": "browser",
  "description": "Sign in and verify that the dashboard appears",
  "test_data": {
    "base_url": "https://your-ui.example",
    "username": "test-user",
    "steps": [{
      "kind": "browser",
      "title": "Sign in",
      "actions": [
        {"action": "goto", "path": "/login"},
        {"action": "fill", "target": "Username or email field", "value": "{{username}}"},
        {"action": "fill", "target": "Password field", "value": "{{password}}"},
        {"action": "click", "target": "Sign in button"}
      ],
      "assertions": [
        {"type": "url_contains", "expected": "/dashboard"},
        {"type": "visible", "target": "Dashboard heading"}
      ]
    }]
  }
}
```

Replace the URL and target descriptions with those of your app. This example pauses for `password` before execution, then identifies the form controls from the page. Target-based steps use the LLM for element selection even when `test_data.steps` bypasses initial planning; steps with explicit selectors need no LLM call unless they use `fill_from_image`. The same placeholder mechanism can request missing values before later steps without repeating earlier actions. For arbitrary secret names, add a requirement with `secret: true`.

Actions auto-wait for their target elements, and Playwright assertions retry until `browser.action_timeout` seconds. Each step has a total limit of `browser.step_timeout` seconds. UI execution stops after a failed step once its configured retries are exhausted. Screenshots are attached to step results on success and failure. Saved result images temporarily expand rendered scroll containers (including clipped app wrappers) for capture, then restore the live layout. These are expanded-layout images, so they can differ from the browser viewport. Virtualized or not-yet-loaded content is not included. This expansion is only for saved images, not targeting or inspection; disable them with `browser.screenshots: false`. Inputs, textareas, and editable fields are masked, but other displayed application data can still be visible in screenshots.

Browser actions and element assertions accept `if_present: true`. An absent target is skipped and
recorded in results. Present but disabled/unusable controls still fail; ambiguous targets or failed
inspection still request clarification. Explicit selectors test DOM presence; target descriptions
use the existing visible main-document inspection (and its coverage limits). Presence is checked
at execution time, so use a preceding wait when a control is expected to appear asynchronously.
Use `if_present` on the optional element's assertion too; URL/title assertions cannot be optional.

A browser step accepts `max_retries` from 0 (default) to 5: the number of **additional** attempts
after an action error or failed assertion. Each attempt repeats all actions and assertions in that
step on the same page/session, stopping on success. Each attempt has its own `browser.step_timeout`.
Clarification pauses preserve the attempt number and completed actions. Invalid navigation and
unavailable/lost browser sessions stop immediately. Results retain earlier attempts and screenshots.
Only put actions you want repeated in that step; keep credentials and role selection in an earlier
setup step. This supports bounded retry-on-failure, not arbitrary loops or expressions.

For example, after opening the login page, selecting Student, and filling credentials, use:

```json
[
{
  "kind": "browser",
  "title": "Accept terms if present",
  "actions": [{"action": "check", "target": "\"我已閱讀並同意\" checkbox", "if_present": true}],
  "assertions": [{"type": "checked", "target": "\"我已閱讀並同意\" checkbox", "if_present": true}]
},
{
  "kind": "browser",
  "title": "Submit login and verify Student Home",
  "max_retries": 2,
  "actions": [
    {"action": "fill_from_image", "target": "Verification code field",
     "image_target": "Generated CAPTCHA image on the left side"},
    {"action": "click", "target": "\"Login Now\" button"}
  ],
  "assertions": [{"type": "url_contains", "expected": "/student/activity/home"}]
}
]
```

These are two entries in `test_data.steps`. The login group runs at most three times and reads
the current CAPTCHA on each attempt. Natural-language descriptions can request the same behavior.
If the application requires explicitly refreshing the CAPTCHA, include that action in the description.

Failed browser assertions show the expected value, actual value observed after failure, selector, number of matching elements, matched element metadata, and configured assertion timeout. This distinguishes value mismatches from missing, ambiguous, or unsuitable elements. The target label is displayed separately from the expected value. Diagnostics are a snapshot after failure, so the page may have changed since the assertion ran. Known secrets and values from password or credential fields are redacted; other checked application values can appear in results. Raw Playwright error messages and DOM dumps are not stored. Existing results do not gain diagnostics retroactively; rerun the test to collect them.

Browser limitations: one page per attempt, no popup/tab switching, cross-origin login/SSO, uploads/downloads, arbitrary JavaScript execution, or mixed HTTP/browser steps in one case. Document navigation stays on the base URL's origin; external static resources are allowed. An unexpected login form is not automatically solved: the plan must include the login actions and credential placeholders.
Automatic inspection covers up to 150 visible elements in the main document; iframe contents, shadow DOM, and canvas-only controls need additional support. Inspection uses a fixed application-owned DOM extraction script. Page content is treated as untrusted data, not instructions, and DOM fallback selections are restricted to observed element IDs. Visual targets must pass full-page image bounds and browser hit testing after scrolling into view. Full-page capture covers the rendered document; independently scrolling panels and content loaded only after scrolling may still require explicit scrolling. Very long pages produce larger model inputs.

## Persistence and operational scope

- Run **one application worker / one replica** per data directory. Background tasks and control locks are process-local.
- A waiting **HTTP** run can resume after a restart using its SQLite LangGraph checkpoint. Browser runs waiting for input are marked stopped after a restart because the live DOM and JavaScript state cannot be restored from that checkpoint; use Retry. Any run interrupted while executing is marked stopped on startup; actions are not silently replayed.
- Retry starts a new plan and browser session, reusing answers from the most recent passed run of that test case. Answers (including credentials) are stored locally in SQLite and survive server restarts; their values are not included in case responses or pages. Failed or stopped attempts do not replace saved answers. Use **Clear saved answers & inspections** while the test is inactive to have the next retry ask again. Element inspections (DOM paths and visual coordinates) are also saved locally per case, including from failed or stopped attempts. Retries validate the stored element identity and, for visual targets, geometry before reuse, skipping the inspection LLM call on a match. Changed or missing targets trigger fresh inspection; image text is still read afresh. The clear button removes both answers and inspections. Editing a case also clears both. Clearing saved answers leaves historical results and checkpoints intact. Each attempt retains its results.
- Stop cancels local execution. Requests already received by the target may still have side effects. Retrying can repeat mutations; use disposable test data or API idempotency keys when appropriate.
- HTTP tests support JSON bodies and deterministic assertions. Browser tests support the actions above. Neither runner executes shell commands or model-generated code. HTTP response-to-request variable extraction is not implemented; provide dependent inputs when prompted.
- This is an internal testing tool without built-in user authentication. Restrict access with your network or an authenticated reverse proxy. It intentionally permits private API targets accessible to the server.
- Common credential keys and known secret values are redacted from portal output/logs and model input. HTTP response bodies are not retained in the UI. Test data, screenshots, and raw workflow checkpoints can contain sensitive data **at rest**; secure the data directory and its backups. Use credential placeholders rather than literals in browser actions. Do not embed secrets in descriptions, prompts, or URLs.

## Docker

Prepare `config.yml` from `example_config.yml` and configure your provider as described above before building or running with Docker or Docker Compose.

```bash
docker build -t agentic-playwright-e2e-tester .
docker run --rm --init --shm-size=1g -p 8091:8091 \
  -v "$(pwd)/config.yml:/app/config.yml:ro" \
  -v testing-assistant-data:/app/data \
  agentic-playwright-e2e-tester
```

Or run `docker compose up --build -d`. The image includes Chromium and its system libraries, runs as a non-root user, and stores data in `/app/data`. Use the named volume to preserve test cases, checkpoints and screenshots. Rebuild the image after upgrading Playwright so its browser binary matches the installed package.

The Dockerfile runs `python -m playwright install --with-deps chromium` during the image build. Chromium is stored in `/opt/playwright`, accessible to the non-root application user. To replace an older image, run `docker compose up --build -d`; no manual browser installation inside the running container is needed.

Inside a container, `localhost` refers to the container. Set `llm.url` and API target URLs to names reachable from the container. For a host service, use `host.docker.internal` (on Linux add `--add-host=host.docker.internal:host-gateway`) or a service name on a shared Docker network. If using `${LLM_API_KEY}`, pass it with `docker run -e LLM_API_KEY` or add it to Compose's environment. If changing the configured server port, update the published port mapping too.

## Tests

```bash
python -m pip install -e '.[dev]'
python -m unittest discover -s tests -v
node --test tests/test_polling.cjs
```

Tests use mock HTTP transports and deterministic plans, with no external model or target required. Core tests cover actual LangGraph interrupts/resume, mid-test questions, authentication retry limits, request assertions, cancellation, URL validation, redaction and Jinja escaping. API tests cover persistent SQLite checkpoints, restart/resume, control conflicts, attempt history, and rendered pages. The API test module is skipped if SQLite dependencies are not installed.
Browser unit tests use test doubles to verify action dispatch, assertions, screenshots, session reuse and cancellation. API tests verify browser pause/resume, cleanup, and restart behavior. `test_browser_live.py` exercises a real Chromium login flow against an in-memory page when Playwright and Chromium are installed; it is skipped otherwise.
Portal tests cover incremental JSON responses, log cursors, changed project sections, escaped HTML, and node-preserving DOM updates using a small DOM test double. The portal's internal `/api/portal/projects/updates`, `/api/portal/projects/{id}/updates` and `/api/portal/testcases/{id}/updates` endpoints accept section versions and a log cursor, returning only changed Jinja-rendered sections and new logs. The client patches existing nodes; it does not replace the list or detail component.

The workflow follows LangGraph's documented [interrupt and resume pattern](https://docs.langchain.com/oss/python/langgraph/interrupts).

Existing databases are migrated automatically on startup: each distinct project label becomes one saved project, preserving test case IDs, inputs, attempts and logs. Existing API clients must create a project first and submit its `project_id` instead of a project name.

### Test case dependencies

Use **Depends on** when creating or editing a test case to select multiple existing cases from the same project and test type. For example, make **Export file** depend on **Login**. The API accepts an ordered `depends_on` array of test case IDs on create and update.

Each attempt executes dependencies first (including their own dependencies), then the selected case. Shared dependencies execute once per attempt, in dependency order. Each case uses its own inherited inputs. All steps share one HTTP client or browser session, preserving cookies and browser state, including while waiting for answers. Retries start a fresh session and rerun dependencies. A failed dependency prevents later steps from executing. Dependency results appear in the requesting case's run; prerequisite cases' run histories are unchanged.

Circular, missing, cross-project, and mixed-type dependencies are rejected. Remove references before deleting a prerequisite. If the server restarts after execution has begun, a waiting session must be retried because its in-memory client state cannot be restored.

Dependencies reuse their saved steps and element inspections, like **Retry steps only**. If no saved plan exists, the dependency is planned once and cached for subsequent runs. Editing a case invalidates its saved plan and inspections. Cached element locations are checked against the current page; stale or missing matches require inspection again. Dependency groups in the checklist and results are collapsed by default; expand a group to see its steps. A group is marked passed only after all of its steps have passed.

When planning a case after dependencies, the planner receives the prerequisite steps and treats their results as preconditions. It generates only the remaining case actions, so a Dashboard case depending on Login can start with clicking Dashboard without logging in again. To replace a previously generated checklist that duplicated setup steps, stop any active attempt and use **Retry everything** once; **Retry steps only** preserves the saved checklist.

### Reference images for test planning

New and edited test cases can include multiple reference images, with previews, editable image names and individual removal. Drag images onto the Test description, paste images with Ctrl+V (Cmd+V on Mac), or use the file picker; refer to image names in the description to guide planning. Attach PNG, JPEG or WebP files (up to 10 images, 5 MiB per image, 20 MiB total). Images are saved with the case and sent to the configured vision-capable planning model when generating steps, including dependency planning and retries that regenerate the plan. Explicit `test_data.steps` and reused checklists bypass generation.

The create/update JSON APIs accept `images: [{"name": "screen.png", "data_url": "data:image/png;base64,..."}]`. On updates, omitting `images` preserves existing attachments; an empty list removes all attachments. Images are stored in the case database and removed when the case is deleted.

# FastAPI Tools Engine MVP

A containerized FastAPI service that visits up to five public websites with Playwright, extracts small pieces of page data, and saves full-page PNG snapshots.

## Author

Aniruddha Salve — salveaniruddha180@gmail.com

## AI browser agent

The API also accepts a natural-language instruction and lets an AWS Bedrock tool-calling agent operate the browser dynamically. The agent can inspect visible text and controls, click selectors or coordinates, type, press keys, select options, scroll, drag on canvas applications, and capture screenshots. Form submission proceeds automatically by default when it is part of the requested workflow; set `require_confirmation` to `true` when a caller must review the final submission first.

Configure AWS credentials with permission to call Amazon Bedrock before starting the service:

```bash
cp .env.example .env
# Edit .env and set AWS_REGION, BEDROCK_MODEL_ID, and AWS credentials if you are not using an IAM role
```

Create an agent task:

```bash
curl -X POST http://localhost:8000/v1/agent/tasks \
  -H 'Content-Type: application/json' \
  -d '{"url":"https://example.com","instruction":"Inspect the page and take a screenshot."}'
```

For a confirmation-gated workflow, include `"require_confirmation": true`. Without that field, it defaults to `false`, so registration and other straightforward requested form submissions do not pause at `waiting_confirmation`.

Poll `GET /v1/agent/tasks/{task_id}`. If `require_confirmation` is enabled and the agent reaches a form submission, the status becomes `waiting_confirmation`; review the returned `confirmation` object and call `POST /v1/agent/tasks/{task_id}/confirm` only when the final action is approved. Use `/cancel` to stop a task.

For a canvas game, a request can look like: `Open the game, inspect the controls, and play one round using clicks, drags, and keyboard input. Stop if the page asks for a login or payment.` The agent can interact with DOM-based and canvas-based games, but game-specific success depends on what the page exposes and whether it is reachable.

## Run locally with Docker

```bash
cp .env.example .env
# Edit .env and configure AWS Bedrock credentials
docker compose up --build
```

Check the service:

```bash
curl http://localhost:8000/health
```

Start the five-site demo:

```bash
curl -X POST http://localhost:8000/v1/runs \
  -H 'Content-Type: application/json' \
  --data @demo-run.json
```

The response contains a `job_id`. Poll it until `status` is `completed`:

```bash
curl http://localhost:8000/v1/jobs/JOB_ID
```

Each successful screenshot is available from the returned path, or through:

```text
http://localhost:8000/v1/jobs/JOB_ID/artifacts/example.png
```

OpenAPI documentation is available at `http://localhost:8000/docs`.

## Setup helpers

On Linux or macOS, run:

```bash
chmod +x setup.sh runner.sh
./setup.sh
# Edit .env with AWS Bedrock configuration
./runner.sh
```

On Windows with Docker Desktop, double-click `runner.bat`, or run it from Command Prompt:

```bat
runner.bat
```

Both runners create `.env` from `.env.example` when it is missing, create the local `artifacts` directory, and start the service with Docker Compose. The runner accepts additional Docker Compose arguments, for example `runner.bat --detach` or `./runner.sh --detach`.

The agent defaults to 20 steps and 60 seconds per task. The maximum bounds can be configured through `MAX_AGENT_STEPS` and `MAX_AGENT_TIMEOUT_SECONDS`; the defaults are 60 steps and 180 seconds. Independent multi-site targets run with bounded concurrency.

## Persistent login sessions and private clients

The Docker configuration mounts `browser-profile/` as a persistent Chromium profile. This allows cookies and local session state to survive container restarts. The agent does not bypass login, MFA, CAPTCHA, or bot checks; complete those steps through an authorized browser session before running the workflow. `GET /v1/browser/session` reports whether the profile is enabled and lists its active pages.

The profile is enabled by default in Docker with `BROWSER_PROFILE_DIR=/browser-profile`. For a local desktop login flow, set `BROWSER_HEADLESS=false` and run the service in an environment with a display. In headless servers, pre-populate the profile using an approved login bootstrap process rather than putting passwords in task instructions.

Private Redis clients, internal Postman deployments, and other private applications remain blocked unless explicitly allowlisted:

```env
ALLOW_PRIVATE_URLS=true
PRIVATE_URL_ALLOWLIST=redis-admin.internal,postman.internal,127.0.0.1
```

Use the file endpoint to provide an upload before an agent task:

```bash
curl -X POST http://localhost:8000/v1/agent/files \
  -F 'file=@./collection.json'
```

The agent can then use the `upload_file` tool with `collection.json`. Downloaded files and screenshots are returned through the task artifact endpoint. The `diagnostics` tool exposes recent browser console and page errors to help debug dynamic applications.

By default, `DEFAULT_REQUIRE_CONFIRMATION=true`. Set `require_confirmation` explicitly to control whether form submissions, posts, messages, commits, and other externally visible actions pause for review.

## Project structure

```text
tools-engine/
├── app/
│   ├── browser.py       # Playwright lifecycle and supported actions
│   ├── main.py          # FastAPI routes and in-memory job manager
│   ├── models.py        # Pydantic request/response models
│   └── security.py      # Public URL validation
├── artifacts/           # Mounted screenshot output directory
├── demo-run.json
├── Dockerfile
├── docker-compose.yml
├── setup.sh           # Linux/macOS prerequisite check and image build
├── runner.sh          # Linux/macOS service runner
├── runner.bat         # Windows Docker Desktop service runner
└── requirements.txt
```

## Supported actions

- `wait_for_load`
- `wait_for_selector` with `selector`
- `extract_title`
- `extract_text` with `selector`
- `click` with `selector`
- `fill` with `selector` and `text`
- `screenshot` with optional `full_page`

Jobs are intentionally held in memory for the MVP. A later production version should move job state to Redis or a database and artifacts to object storage.

## Safety limits

The service accepts only HTTP(S) URLs, rejects local/private network destinations, allows at most five sites per job, limits browser timeouts, and does not expose arbitrary shell execution.

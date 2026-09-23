# FastAPI Tools Engine MVP

A containerized FastAPI service that visits up to five public websites with Playwright, extracts small pieces of page data, and saves full-page PNG snapshots.

## Author

Aniruddha Salve — salveaniruddha180@gmail.com

## Run locally with Docker

```bash
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

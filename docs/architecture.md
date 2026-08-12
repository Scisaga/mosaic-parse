# Architecture

Docling GLM separates document orchestration from model serving. The FastAPI
parser remains a CPU service; GLM-OCR and Ollama are optional HTTP backends.

```text
Browser / HTTP client / MCP client
                 |
                 v
FastAPI: auth, validation, jobs, SSE, UI, MCP
                 |
                 v
Parser service: routing, timeout, normalization, quality warnings
        |                                  |
        v                                  v
Docling Standard (CPU)          optional Ollama VLM (remote)
        |
        v
optional docling-glm-ocr adapter (OpenAI-compatible HTTP)
        |
        v
GLM-OCR vLLM service (GPU, separate container or host)
```

## Responsibilities

- `app/api`: versioned HTTP routes, health/readiness, administration.
- `app/models`: public and internal Pydantic contracts; Docling objects never
  escape through these contracts.
- `app/parsers`: adapters for Docling Standard, remote GLM OCR, and optional VLM.
- `app/services`: source acquisition, parsing, jobs, quality checks, export, and
  cleanup.
- `app/repositories`: durable SQLite job state.
- `app/security`: authentication, upload validation, and SSRF protection.
- `app/mcp`: MCP 2.x Streamable HTTP surface mounted below `/mcp`.
- `frontend`: independent React application built by Vite and served as static
  files by FastAPI in production.

## Runtime lifecycle

At startup the service validates settings, opens the SQLite repository, marks
jobs left in `running` state as `failed/server_restarted`, creates the bounded
job queue, and initializes parser workers. A worker owns and reuses its
`DocumentConverter`; requests do not construct converters.

Docling conversion is synchronous CPU work and runs outside the FastAPI event
loop. The default is one parser worker because upstream converter objects are
not assumed to be thread-safe. External backend calls have independent timeout,
retry, and concurrency limits.

## Persistent state

```text
/data
├── jobs.db
└── jobs/<job_id>
    ├── input/original.<ext>
    ├── output/result.md
    ├── output/result.txt
    ├── output/metadata.json
    └── logs/warnings.json
```

SQLite is the durable source of truth for job state. SSE is an experience layer:
clients must reconnect and query `GET /v1/documents/jobs/{job_id}` when events
are missed.

## Parsing routes

- `standard`: Docling Standard on CPU; native text is preserved and OCR is used
  only when configured and required.
- `ocr`: Docling with full-page remote GLM-OCR intent for scans and images.
- `auto`: standard first, then conservative fallback rules. Automatic VLM
  fallback is off unless explicitly enabled.
- `vlm`: optional manually selected remote Ollama/OpenAI-compatible VLM route.

Unobservable page/region counts and confidence values remain `null` or absent.
The service never invents confidence or backend provenance.

## Trust boundaries

Uploaded bytes and source URLs are untrusted. Validation checks magic bytes,
size, page count, safe filenames, protocols, resolved IPs, and every redirect.
Private/link-local/metadata destinations are denied by default. The UI sanitizes
rendered Markdown. External model URLs are server configuration and cannot be
supplied per request.

## v0.1 scope

The service ends at Markdown or plain text. Financial metric extraction,
entities/relations, summarization, RAG, embeddings, vector databases, document
question answering, and long-term document management belong downstream.


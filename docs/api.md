# HTTP API

默认 Base URL 为 `http://localhost:12303`。交互式契约见 `/docs` 和
`/redoc`，机器可读契约见 `/openapi.json`。

## Authentication

当 `API_KEY` 非空时，受保护的公共 API 推荐发送：

```http
Authorization: Bearer <API_KEY>
```

也接受 `X-API-Key: <API_KEY>`。管理接口不使用 API Key，而要求：

```http
X-Admin-Token: <ADMIN_TOKEN>
```

Health/readiness 的具体公开范围以部署配置和 OpenAPI 为准。密钥不能放入
查询字符串。

## Parse options

同步和异步创建接口使用 `multipart/form-data`，`file` 与 `source_url` 必须
且只能提供一个。

| Field | Values / type | Default |
|---|---|---|
| `file` | PDF, PNG, JPEG, WEBP, TIFF | — |
| `source_url` | allowed HTTP/HTTPS URL | — |
| `mode` | `auto`, `standard`, `ocr`, `vlm` | `auto` |
| `profile` | `fast`, `balanced`, `accurate` | `balanced` |
| `output_format` | `markdown`, `text` | `markdown` |
| `page_range` | e.g. `1-5,8,10-12` | all pages |
| `language` | comma-separated, e.g. `zh,en` | `zh,en` |
| `enable_vlm_fallback` | boolean | `false` |
| `preserve_page_breaks` | boolean | `true` |
| `include_pages` | boolean | `false` |
| `include_diagnostics` | boolean | `true` |
| `timeout_seconds` | positive integer | configured timeout |

Page numbers are one-based. Duplicate/overlapping ranges are normalized; a
range outside the document is rejected rather than silently changed.

## Synchronous parse

```http
POST /v1/documents/parse
Content-Type: multipart/form-data
```

```bash
curl --fail-with-body http://localhost:12303/v1/documents/parse \
  -H "Authorization: Bearer $API_KEY" \
  -F file=@document.pdf \
  -F mode=auto \
  -F profile=balanced \
  -F output_format=markdown \
  -F page_range=1-5 \
  -F language=zh,en
```

The JSON envelope includes document metadata, status, selected pipeline,
content, warnings, and measured usage. Page/backend diagnostics are included
only when observable. A request beyond `SYNC_MAX_BYTES` or `SYNC_MAX_PAGES`
returns HTTP 409 `sync_limit_exceeded`; use the jobs endpoint instead.

## Asynchronous jobs

Create a job with the same multipart options:

```http
POST /v1/documents/jobs
```

```bash
curl --fail-with-body http://localhost:12303/v1/documents/jobs \
  -H "Authorization: Bearer $API_KEY" \
  -F file=@large-document.pdf \
  -F mode=auto \
  -F profile=balanced
```

The response contains a `job_*` ID and status/events/result URLs. Durable states
are:

```text
queued -> running -> completed
                  -> partial
                  -> failed
queued/running    -> cancelled
```

Query the durable source of truth:

```http
GET /v1/documents/jobs/{job_id}
```

Subscribe to progress:

```http
GET /v1/documents/jobs/{job_id}/events
Accept: text/event-stream
```

Event names include `job.started`, `page.started`, `page.completed`,
`page.warning`, `page.failed`, `job.progress`, `job.completed`, `job.failed`, and
`heartbeat`. SSE delivery is not the source of truth; after reconnecting, query
the job endpoint before continuing.

Get or download a completed result:

```http
GET /v1/documents/jobs/{job_id}/result?format=markdown&download=true
```

Content types are `text/markdown; charset=utf-8` and
`text/plain; charset=utf-8`. Download filenames are sanitized.

Retry or cancel/delete:

```http
POST   /v1/documents/jobs/{job_id}/retry
DELETE /v1/documents/jobs/{job_id}
```

A retry creates a new execution attempt without rewriting the historical state
of an unrelated job. A running delete first requests cancellation; persisted
input/result removal follows the documented job state and retention policy.

## Service status

- `GET /health`: process liveness, version, uptime, queue depth, non-secret
  limits, and a backend summary.
- `GET /ready`: storage/converter readiness and requirements of the active
  default route.
- `GET /v1/backends`: individual Docling, GLM, and VLM availability/capability.

GLM or VLM being disabled/unavailable does not crash the API. A route that
requires the missing backend fails with an explicit warning/error. Native
standard conversion remains available when GLM is disabled.

## Administration

```bash
curl --fail-with-body -X POST http://localhost:12303/admin/reload \
  -H "X-Admin-Token: $ADMIN_TOKEN"

curl --fail-with-body -X POST http://localhost:12303/admin/cleanup \
  -H "X-Admin-Token: $ADMIN_TOKEN"
```

Reload rebuilds cached parser/backend state in a controlled lifecycle. Cleanup
removes jobs older than `JOB_RETENTION_HOURS` when their state permits it.

## Error envelope

Application errors use one JSON shape:

```json
{
  "error": {
    "code": "backend_unavailable",
    "message": "GLM-OCR backend is unavailable",
    "request_id": "req_01J...",
    "details": {"backend": "glm-ocr-remote"}
  }
}
```

Common status codes:

| HTTP | Meaning |
|---:|---|
| 400 | conflicting source/options, invalid URL or page range |
| 401 | invalid API/admin credential |
| 404 | job not found |
| 409 | sync limit or invalid state transition |
| 413 | upload/download exceeds configured bytes |
| 415 | unsupported or mismatched file type |
| 422 | field validation failure |
| 429 | queue/concurrency limit reached |
| 502 | GLM/Ollama upstream failure |
| 504 | document/model timeout |

## URL safety

`source_url` allows HTTP/HTTPS only. Unless explicitly enabled, DNS results for
localhost, private, loopback, link-local, multicast, reserved, and cloud
metadata addresses are denied. Every redirect is resolved and checked again;
redirect count, response bytes, and time are bounded.

## MCP

`GET/POST /mcp` exposes MCP 2.x Streamable HTTP tools for parsing and job/result
queries. MCP large payloads use URLs or HTTP result downloads rather than huge
inline responses. Allowed Host and Origin values are configured separately via
`MCP_ALLOWED_HOSTS` and `MCP_ALLOWED_ORIGINS`; external domains must be listed
explicitly.


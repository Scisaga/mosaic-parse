from __future__ import annotations

import time
from pathlib import Path

from fastapi.testclient import TestClient

from app.main import create_app
from tests.conftest import fake_runtime, make_test_settings


def upload(path: Path, *, filename: str | None = None):
    return {"file": (filename or path.name, path.read_bytes(), "application/pdf")}


def wait_for_status(
    client: TestClient,
    job_id: str,
    expected: set[str],
    *,
    headers: dict[str, str] | None = None,
) -> dict[str, object]:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        response = client.get(f"/v1/documents/jobs/{job_id}", headers=headers)
        assert response.status_code == 200, response.text
        payload = response.json()
        if payload["status"] in expected:
            return payload
        time.sleep(0.01)
    raise AssertionError(f"job {job_id} did not reach {expected}")


def test_sync_parse_health_backends_and_ready(tmp_path: Path, native_pdf: Path) -> None:
    settings = make_test_settings(tmp_path)
    app = create_app(settings, runtime_factory=fake_runtime)
    with TestClient(app) as client:
        health = client.get("/health")
        assert health.status_code == 200
        assert health.json()["status"] == "ok"
        assert health.json()["config"]["glm_ocr_enabled"] is False

        ready = client.get("/ready")
        assert ready.status_code == 200
        assert ready.json()["checks"] == {
            "writable_data_dir": True,
            "writable_database": True,
            "docling": True,
        }

        backends = client.get("/v1/backends").json()
        assert [item["state"] for item in backends["backends"]] == [
            "ready",
            "disabled",
            "disabled",
        ]

        response = client.post(
            "/v1/documents/parse",
            files=upload(native_pdf),
            data={"include_pages": "true", "output_format": "markdown"},
        )
        assert response.status_code == 200, response.text
        parsed = response.json()
        assert parsed["status"] == "completed"
        assert parsed["content"] == "# Page 1\n\nValue: 12,345.67"
        assert parsed["pages"][0]["backend"] == "docling-standard"
        assert parsed["route_summary"]["ocr_regions"] is None
        assert response.headers["x-request-id"].startswith("req_")


def test_async_job_sse_results_and_delete(tmp_path: Path, native_pdf: Path) -> None:
    app = create_app(make_test_settings(tmp_path), runtime_factory=fake_runtime)
    with TestClient(app) as client:
        created = client.post(
            "/v1/documents/jobs",
            files=upload(native_pdf),
            data={"include_pages": "true", "include_diagnostics": "true"},
        )
        assert created.status_code == 202, created.text
        job = created.json()
        assert job["status"] == "queued"
        assert job["events_url"].endswith("/events")

        completed = wait_for_status(client, job["id"], {"completed"})
        assert completed["progress"]["current"] == 1
        assert completed["pages"][0]["page_number"] == 1
        assert completed["pages"][0]["backend"] == "docling-standard"
        assert completed["pipeline"]["primary"] == "docling-standard"
        assert completed["usage"]["duration_ms"] == 2

        markdown = client.get(f"{job['result_url']}?format=markdown&download=false")
        assert markdown.status_code == 200
        assert markdown.headers["content-type"].startswith("text/markdown")
        assert "12,345.67" in markdown.text

        downloaded = client.get(f"{job['result_url']}?format=text&download=true")
        assert downloaded.headers["content-disposition"].startswith("attachment;")
        assert downloaded.headers["content-type"].startswith("text/plain")

        events = client.get(job["events_url"])
        assert events.status_code == 200
        assert "event: job.started" in events.text
        assert "event: job.completed" in events.text
        assert '"type":"job.progress"' in events.text

        deleted = client.delete(f"/v1/documents/jobs/{job['id']}")
        assert deleted.json() == {"id": job["id"], "status": "deleted"}
        missing = client.get(f"/v1/documents/jobs/{job['id']}")
        assert missing.status_code == 404
        assert missing.json()["error"]["code"] == "job_not_found"


def test_failure_retry_and_cancel(tmp_path: Path, native_pdf: Path) -> None:
    app = create_app(make_test_settings(tmp_path), runtime_factory=fake_runtime)
    with TestClient(app) as client:
        failed_job = client.post(
            "/v1/documents/jobs",
            files=upload(native_pdf, filename="fail.pdf"),
        ).json()
        failed = wait_for_status(client, failed_job["id"], {"failed"})
        assert failed["error"]["code"] == "mock_parse_failed"

        retried = client.post(
            f"/v1/documents/jobs/{failed_job['id']}/retry",
            data={"page_range": "1"},
        )
        assert retried.status_code == 200, retried.text
        assert retried.json()["parent_job_id"] == failed_job["id"]
        assert retried.json()["attempt"] == 2

        slow = client.post(
            "/v1/documents/jobs",
            files=upload(native_pdf, filename="slow.pdf"),
        ).json()
        cancelled = client.delete(f"/v1/documents/jobs/{slow['id']}")
        assert cancelled.status_code == 200
        assert cancelled.json()["status"] == "cancelled"
        state = client.get(f"/v1/documents/jobs/{slow['id']}").json()
        assert state["status"] == "cancelled"


def test_auth_admin_limits_and_unified_errors(tmp_path: Path, native_pdf: Path) -> None:
    settings = make_test_settings(
        tmp_path,
        api_key="api-secret",
        admin_token="admin-secret",
        sync_max_bytes=100,
    )
    app = create_app(settings, runtime_factory=fake_runtime)
    api_headers = {"X-API-Key": "api-secret"}
    with TestClient(app) as client:
        unauthorized = client.get("/v1/backends")
        assert unauthorized.status_code == 401
        assert unauthorized.json()["error"]["code"] == "invalid_api_key"
        assert unauthorized.json()["error"]["request_id"].startswith("req_")

        limited = client.post(
            "/v1/documents/parse",
            headers=api_headers,
            files=upload(native_pdf),
        )
        assert limited.status_code == 409
        assert limited.json()["error"]["code"] == "sync_limit_exceeded"

        invalid = client.post(
            "/v1/documents/parse",
            headers=api_headers,
            data={"source_url": "https://8.8.8.8/report.pdf"},
            files=upload(native_pdf),
        )
        assert invalid.status_code == 400
        assert invalid.json()["error"]["code"] == "invalid_source"

        blocked = client.post(
            "/v1/documents/parse",
            headers=api_headers,
            data={"source_url": "http://127.0.0.1/private.pdf"},
        )
        assert blocked.status_code == 400
        assert blocked.json()["error"]["code"] == "source_url_blocked"

        denied_admin = client.post("/admin/cleanup")
        assert denied_admin.status_code == 401
        cleaned = client.post("/admin/cleanup", headers={"X-Admin-Token": "admin-secret"})
        assert cleaned.status_code == 200
        assert cleaned.json()["deleted_jobs"] == 0


def test_prefer_async_returns_job_contract(tmp_path: Path, native_pdf: Path) -> None:
    app = create_app(make_test_settings(tmp_path), runtime_factory=fake_runtime)
    with TestClient(app) as client:
        response = client.post(
            "/v1/documents/parse",
            files=upload(native_pdf),
            data={"prefer_async": "true"},
        )
        assert response.status_code == 202
        assert response.json()["object"] == "document.parse.job"

from __future__ import annotations

import hashlib
import io
import json
import time
import zipfile
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
        response = client.get(f"/v1/content/jobs/{job_id}", headers=headers)
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
            "/v1/content/parse",
            files=upload(native_pdf),
        )
        assert response.status_code == 200, response.text
        parsed = response.json()
        assert parsed["object"] == "content.evidence"
        assert parsed["schema_version"] == "content-evidence/1.0"
        assert parsed["status"] == "completed"
        assert parsed["renderings"]["markdown"] == "# Page 1\n\nValue: 12,345.67"
        assert parsed["units"][0]["blocks"][0]["text"].startswith("# Page 1")
        assert parsed["runtime"]["primary_backend"] == "docling-standard"
        assert response.headers["x-request-id"].startswith("req_")


def test_profile_is_the_only_public_visual_routing_control(
    tmp_path: Path, native_pdf: Path
) -> None:
    app = create_app(make_test_settings(tmp_path), runtime_factory=fake_runtime)
    with TestClient(app) as client:
        response = client.post(
            "/v1/content/jobs",
            files=upload(native_pdf),
            data={"profile": "accurate"},
        )
        assert response.status_code == 202, response.text
        assert response.json()["options"] == {
            "profile": "accurate",
            "unit_range": None,
            "language": ["zh", "en"],
            "include_renderings": True,
            "description_language": "zh-CN",
            "timeout_seconds": None,
        }

        removed = client.post(
            "/v1/content/jobs",
            files=upload(native_pdf),
            data={"vlm_policy": "auto_visual", "mode": "auto"},
        )
        assert removed.status_code == 422
        assert removed.json()["error"]["code"] == "removed_options"
        assert removed.json()["error"]["details"]["fields"] == ["mode", "vlm_policy"]


def test_include_renderings_false_keeps_evidence_and_hides_only_derived_views(
    tmp_path: Path,
    native_pdf: Path,
) -> None:
    app = create_app(make_test_settings(tmp_path), runtime_factory=fake_runtime)
    with TestClient(app) as client:
        response = client.post(
            "/v1/content/parse",
            files=upload(native_pdf),
            data={"include_renderings": "false"},
        )
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["units"][0]["blocks"]
        assert payload["units"][0]["diagnostics"]["quality_verdict"] == "trusted"
        assert payload["renderings"] == {"markdown": "", "plain_text": ""}
        assert payload["units"][0]["renderings"] == {"markdown": "", "plain_text": ""}


def test_async_job_sse_results_and_delete(tmp_path: Path, native_pdf: Path) -> None:
    app = create_app(make_test_settings(tmp_path), runtime_factory=fake_runtime)
    with TestClient(app) as client:
        created = client.post(
            "/v1/content/jobs",
            files=upload(native_pdf),
        )
        assert created.status_code == 202, created.text
        job = created.json()
        assert job["status"] == "queued"
        assert job["events_url"].endswith("/events")

        completed = wait_for_status(client, job["id"], {"completed"})
        assert completed["progress"]["current"] == 1
        assert "pages" not in completed
        assert "pipeline" not in completed

        evidence = client.get(job["result_url"])
        assert evidence.status_code == 200
        assert evidence.json()["object"] == "content.evidence"
        assert "12,345.67" in evidence.json()["renderings"]["markdown"]

        downloaded = client.get(f"/v1/content/jobs/{job['id']}/rendering/text?download=true")
        assert downloaded.headers["content-disposition"].startswith("attachment;")
        assert downloaded.headers["content-type"].startswith("text/plain")

        events = client.get(job["events_url"])
        assert events.status_code == 200
        assert "event: job.started" in events.text
        assert "event: job.completed" in events.text
        assert '"type":"job.progress"' in events.text

        deleted = client.delete(f"/v1/content/jobs/{job['id']}")
        assert deleted.json() == {"id": job["id"], "status": "deleted"}
        missing = client.get(f"/v1/content/jobs/{job['id']}")
        assert missing.status_code == 404
        assert missing.json()["error"]["code"] == "job_not_found"


def test_failure_retry_and_cancel(tmp_path: Path, native_pdf: Path) -> None:
    app = create_app(make_test_settings(tmp_path), runtime_factory=fake_runtime)
    with TestClient(app) as client:
        failed_job = client.post(
            "/v1/content/jobs",
            files=upload(native_pdf, filename="fail.pdf"),
        ).json()
        failed = wait_for_status(client, failed_job["id"], {"failed"})
        assert failed["error"]["code"] == "mock_parse_failed"

        retried = client.post(
            f"/v1/content/jobs/{failed_job['id']}/retry",
            data={"unit_range": "1"},
        )
        assert retried.status_code == 200, retried.text
        assert retried.json()["parent_job_id"] == failed_job["id"]
        assert retried.json()["attempt"] == 2

        slow = client.post(
            "/v1/content/jobs",
            files=upload(native_pdf, filename="slow.pdf"),
        ).json()
        cancelled = client.delete(f"/v1/content/jobs/{slow['id']}")
        assert cancelled.status_code == 200
        assert cancelled.json()["status"] == "cancelled"
        state = client.get(f"/v1/content/jobs/{slow['id']}").json()
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
            "/v1/content/parse",
            headers=api_headers,
            files=upload(native_pdf),
        )
        assert limited.status_code == 202
        assert limited.json()["object"] == "content.parse.job"

        invalid = client.post(
            "/v1/content/parse",
            headers=api_headers,
            data={"source_url": "https://8.8.8.8/report.pdf"},
            files=upload(native_pdf),
        )
        assert invalid.status_code == 400
        assert invalid.json()["error"]["code"] == "invalid_source"

        blocked = client.post(
            "/v1/content/parse",
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
            "/v1/content/parse",
            files=upload(native_pdf),
            data={"prefer_async": "true"},
        )
        assert response.status_code == 202
        assert response.json()["object"] == "content.parse.job"


def test_video_is_always_async_and_rejects_unit_range(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(
        "app.security.file_validation._inspect_video", lambda *args, **kwargs: None
    )
    video = Path("tests/fixtures/scene-switch.mp4")
    app = create_app(make_test_settings(tmp_path), runtime_factory=fake_runtime)
    with TestClient(app) as client:
        response = client.post(
            "/v1/content/parse",
            files={"file": (video.name, video.read_bytes(), "application/octet-stream")},
        )
        assert response.status_code == 202, response.text
        assert response.json()["object"] == "content.parse.job"
        assert response.json()["mime_type"] == "video/mp4"
        assert response.json()["progress"]["unit"] == "frame"

        invalid = client.post(
            "/v1/content/parse",
            files={"file": (video.name, video.read_bytes(), "video/mp4")},
            data={"unit_range": "1"},
        )
        assert invalid.status_code == 400
        assert invalid.json()["error"]["code"] == "invalid_unit_range"


def test_legacy_document_routes_are_not_registered(tmp_path: Path) -> None:
    app = create_app(make_test_settings(tmp_path), runtime_factory=fake_runtime)
    with TestClient(app) as client:
        assert client.post("/v1/documents/parse").status_code == 404
        assert client.get("/v1/documents/jobs/legacy").status_code == 404


def test_authenticated_asset_range_bundle_checksum_and_cleanup(
    tmp_path: Path, native_pdf: Path
) -> None:
    settings = make_test_settings(tmp_path, api_key="asset-secret")
    app = create_app(settings, runtime_factory=fake_runtime)
    headers = {"X-API-Key": "asset-secret"}
    with TestClient(app) as client:
        parsed = client.post(
            "/v1/content/parse", headers=headers, files=upload(native_pdf)
        )
        assert parsed.status_code == 200, parsed.text
        job_id = parsed.json()["source"]["content_id"]

        media = Path("tests/fixtures/scene-switch.mp4").read_bytes()
        digest = hashlib.sha256(media).hexdigest()
        asset_id = f"asset_{digest[:26]}"
        asset_dir = tmp_path / "jobs" / job_id / "assets" / "original"
        asset_path = asset_dir / f"{asset_id}__scene-switch.mp4"
        asset_path.write_bytes(media)

        result_path = tmp_path / "jobs" / job_id / "output" / "result.json"
        evidence = json.loads(result_path.read_text())
        evidence["assets"].append(
            {
                "asset_id": asset_id,
                "kind": "video",
                "role": "source",
                "mime_type": "video/mp4",
                "sha256": digest,
                "size_bytes": len(media),
                "filename": "scene-switch.mp4",
                "width": 640,
                "height": 360,
                "duration_ms": 6000,
                "parent_asset_id": None,
                "locations": [{"unit_id": evidence["units"][0]["unit_id"]}],
                "visual_analysis": None,
                "status": "ready",
                "warning_codes": [],
                "download_url": f"/v1/content/jobs/{job_id}/assets/{asset_id}",
            }
        )
        result_path.write_text(json.dumps(evidence), encoding="utf-8")

        assert client.get(f"/v1/content/jobs/{job_id}/assets").status_code == 401
        listed = client.get(f"/v1/content/jobs/{job_id}/assets", headers=headers)
        assert listed.status_code == 200
        assert listed.json()[0]["sha256"] == digest

        ranged = client.get(
            f"/v1/content/jobs/{job_id}/assets/{asset_id}",
            headers={**headers, "Range": "bytes=2-9"},
        )
        assert ranged.status_code == 206
        assert ranged.content == media[2:10]
        assert ranged.headers["content-range"] == f"bytes 2-9/{len(media)}"
        assert ranged.headers["etag"] == f'"{digest}"'

        invalid_range = client.get(
            f"/v1/content/jobs/{job_id}/assets/{asset_id}",
            headers={**headers, "Range": f"bytes={len(media)}-"},
        )
        assert invalid_range.status_code == 416
        assert invalid_range.json()["error"]["code"] == "range_not_satisfiable"

        bundle = client.get(f"/v1/content/jobs/{job_id}/bundle", headers=headers)
        assert bundle.status_code == 200
        with zipfile.ZipFile(io.BytesIO(bundle.content)) as archive:
            manifest = json.loads(archive.read("manifest.json"))
            archived = archive.read(f"assets/{asset_id}/scene-switch.mp4")
        assert manifest["schema_version"] == "content-evidence/1.0"
        assert manifest["assets"][0]["sha256"] == digest
        assert hashlib.sha256(archived).hexdigest() == digest

        deleted = client.delete(f"/v1/content/jobs/{job_id}", headers=headers)
        assert deleted.json()["status"] == "deleted"
        assert not (tmp_path / "jobs" / job_id).exists()

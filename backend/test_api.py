"""FastAPI TestClient integration tests — no mkvmerge binary required."""

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path, monkeypatch):
    src = tmp_path / "source"
    dst = tmp_path / "destination"
    src.mkdir()
    dst.mkdir()

    monkeypatch.setenv("SOURCE_DIR", str(src))
    monkeypatch.setenv("DEST_DIR", str(dst))

    # Reimport app so env vars are picked up via os.environ.get at request time
    import importlib
    import main as main_mod
    importlib.reload(main_mod)

    return TestClient(main_mod.app), src, dst


def test_list_source_files_only_mkv(client):
    tc, src, dst = client
    (src / "movie.mkv").write_bytes(b"fake")
    (src / "notes.txt").write_text("ignore me")
    (src / "sub" ).mkdir()
    (src / "sub" / "nested.mkv").write_bytes(b"fake2")

    resp = tc.get("/api/files?dir=source")
    assert resp.status_code == 200
    names = {f["name"] for f in resp.json()}
    assert names == {"movie.mkv", "nested.mkv"}
    assert "notes.txt" not in names


def test_list_destination_files_only_mkv(client):
    tc, src, dst = client
    (dst / "final.mkv").write_bytes(b"fake")
    (dst / "readme.md").write_text("ignore")

    resp = tc.get("/api/files?dir=destination")
    assert resp.status_code == 200
    names = {f["name"] for f in resp.json()}
    assert names == {"final.mkv"}


def test_probe_nonexistent_returns_404(client):
    tc, src, dst = client
    resp = tc.get("/api/probe?dir=source&path=doesnotexist.mkv")
    assert resp.status_code == 404


def test_remux_missing_source_returns_404(client):
    tc, src, dst = client
    # dest file exists, source does not
    (dst / "out.mkv").write_bytes(b"fake")

    resp = tc.post("/api/remux", json={
        "source_path": "ghost.mkv",
        "dest_path": "out.mkv",
        "track_ids": [],
        "dest_track_ids": [0],
        "chapters": True,
        "attachments": True,
        "tags": True,
    })
    assert resp.status_code == 404


def test_jobs_unknown_id_returns_404(client):
    tc, *_ = client
    resp = tc.get("/api/jobs/nonexistent-id-12345")
    assert resp.status_code == 404

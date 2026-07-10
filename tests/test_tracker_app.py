"""End-to-end test: boot the real FastAPI app, connect over WebSocket, and
verify it serves a live snapshot built from the real Polymarket API.
"""

from fastapi.testclient import TestClient

from polytracker.tracker.app import app


def test_index_serves_dashboard_html():
    with TestClient(app) as client:
        resp = client.get("/")
    assert resp.status_code == 200
    assert "BTC 15-Min Up/Down Tracker" in resp.text


def test_websocket_streams_live_snapshot(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYTRACKER_DB_PATH", str(tmp_path / "tracker_test.db"))

    with TestClient(app) as client:
        with client.websocket_connect("/ws") as ws:
            live_snapshot = None
            for _ in range(15):
                msg = ws.receive_json()
                if msg.get("status") == "live":
                    live_snapshot = msg
                    break

    assert live_snapshot is not None, "never received a live snapshot from /ws"
    assert live_snapshot["market_slug"].startswith("btc-updown-15m-")
    assert live_snapshot["seconds_remaining"] > 0
    assert live_snapshot["up"]["token_id"]
    assert live_snapshot["down"]["token_id"]


def test_api_state_matches_websocket_shape(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYTRACKER_DB_PATH", str(tmp_path / "tracker_test2.db"))

    with TestClient(app) as client:
        with client.websocket_connect("/ws"):
            pass
        resp = client.get("/api/state")

    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] in ("initializing", "live", "closed")

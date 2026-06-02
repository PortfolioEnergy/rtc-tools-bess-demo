"""Tests for the HTTP API layer (routes + model registry integration).

These tests use FastAPI's TestClient so no real server is started.
Solver-level tests are skipped here — they require RTC-Tools compilation.
We mock the solver to test the HTTP contract in isolation.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

from fastapi.testclient import TestClient


class TestHealthEndpoints:
    """Tests for /health and /ready."""

    def test_health(self, client: TestClient) -> None:
        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}

    def test_ready(self, client: TestClient) -> None:
        resp = client.get("/ready")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] in ("ready", "not_ready")


class TestSubmitSync:
    """Tests for POST /v1/models/{model_name}/submit_sync."""

    def test_unknown_model_returns_404(self, client: TestClient) -> None:
        resp = client.post(
            "/v1/models/unknown_model/submit_sync",
            json={"model_input_data": {}},
        )
        assert resp.status_code == 404

    def test_fcr_returns_404(self, client: TestClient) -> None:
        resp = client.post(
            "/v1/models/bess_fcr/submit_sync",
            json={"model_input_data": {}},
        )
        assert resp.status_code == 404

    def test_setpoints_returns_200(
        self, client: TestClient, setpoints_input: dict[str, Any]
    ) -> None:
        resp = client.post(
            "/v1/models/bess_setpoints/submit_sync",
            json={"model_input_data": setpoints_input},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert "result" in body
        assert "members" in body["result"]
        assert "_info" in body["result"]

    def test_setpoints_values_match(
        self, client: TestClient, setpoints_input: dict[str, Any]
    ) -> None:
        resp = client.post(
            "/v1/models/bess_setpoints/submit_sync",
            json={"model_input_data": setpoints_input},
        )
        values = resp.json()["result"]["members"]["default"]["setpoints"]["values"]
        assert values == [5.0, -3.0, 0.0, 7.5]

    def test_scheduling_model_dispatches_correctly(
        self, client: TestClient, scheduling_input: dict[str, Any]
    ) -> None:
        """Verify that a scheduling model name reaches run_solver with 'scheduling'."""
        mock_body = {
            "result": {"members": {"default": {}}, "_info": ["solver: mock"]},
        }
        with patch("service.routes.run_solver", return_value=mock_body) as mock:
            resp = client.post(
                "/v1/models/bess_day_ahead/submit_sync",
                json={"model_input_data": scheduling_input},
            )
            assert resp.status_code == 200
            mock.assert_called_once_with(
                "scheduling", scheduling_input, include_diagnostics=False
            )

    def test_intraday_model_dispatches_correctly(
        self, client: TestClient, intraday_input: dict[str, Any]
    ) -> None:
        mock_body = {
            "result": {"members": {"default": {}}, "_info": ["solver: mock"]},
        }
        with patch("service.routes.run_solver", return_value=mock_body) as mock:
            resp = client.post(
                "/v1/models/bess_rolling/submit_sync",
                json={"model_input_data": intraday_input},
            )
            assert resp.status_code == 200
            mock.assert_called_once_with(
                "intraday", intraday_input, include_diagnostics=False
            )

    def test_include_diagnostics_flag_forwarded(
        self, client: TestClient, scheduling_input: dict[str, Any]
    ) -> None:
        """include_diagnostics=True must be forwarded to run_solver."""
        mock_body = {
            "result": {"members": {"default": {}}, "_info": ["solver: mock"]},
        }
        with patch("service.routes.run_solver", return_value=mock_body) as mock:
            resp = client.post(
                "/v1/models/bess_day_ahead/submit_sync",
                json={
                    "model_input_data": scheduling_input,
                    "include_diagnostics": True,
                },
            )
            assert resp.status_code == 200
            mock.assert_called_once_with(
                "scheduling", scheduling_input, include_diagnostics=True
            )

    def test_images_present_when_diagnostics_returned(
        self, client: TestClient, setpoints_input: dict[str, Any]
    ) -> None:
        """When diagnostics are enabled, chart data URIs appear inside ``_info``
        as ``"image:<name>: data:image/png;base64,..."`` entries.

        The response shape (``result`` with ``members`` + ``_info``) is
        unchanged — no top-level ``images`` key is added.
        """
        mock_body = {
            "result": {
                "members": {"default": {"setpoints": {"values": [1.0]}}},
                "_info": [
                    "solver: mock",
                    "image:revenue_decomposition: data:image/png;base64,xyz",
                ],
            },
        }
        with patch("service.routes.run_solver", return_value=mock_body):
            resp = client.post(
                "/v1/models/bess_setpoints/submit_sync",
                json={
                    "model_input_data": setpoints_input,
                    "include_diagnostics": True,
                },
            )
            assert resp.status_code == 200
            body = resp.json()
            info = body["result"]["_info"]
            image_entries = [e for e in info if e.startswith("image:")]
            assert len(image_entries) == 1
            assert "revenue_decomposition" in image_entries[0]
            assert "data:image/png;base64," in image_entries[0]
            # no top-level images key and no images key inside result
            assert "images" not in body
            assert "images" not in body["result"]

    def test_reasoning_markdown_passed_through(
        self, client: TestClient, scheduling_input: dict[str, Any]
    ) -> None:
        """A top-level ``reasoning_markdown`` key from run_solver is returned verbatim.

        poc-backtesting reads ``reasoning_markdown`` from the top level of the
        optimiser response (next to ``result``), so the route must not strip it.
        """
        mock_body = {
            "result": {"members": {"default": {}}, "_info": ["solver: mock"]},
            "reasoning_markdown": "# Day-Ahead Scheduling — Optimiser Reasoning\n",
        }
        with patch("service.routes.run_solver", return_value=mock_body):
            resp = client.post(
                "/v1/models/bess_day_ahead/submit_sync",
                json={
                    "model_input_data": scheduling_input,
                    "include_diagnostics": True,
                },
            )
            assert resp.status_code == 200
            body = resp.json()
            assert "reasoning_markdown" in body
            assert body["reasoning_markdown"].startswith("# Day-Ahead Scheduling")
            # result shape unchanged
            assert "members" in body["result"]
            assert "_info" in body["result"]

    def test_solver_error_returns_500(
        self, client: TestClient, scheduling_input: dict[str, Any]
    ) -> None:
        with patch(
            "service.routes.run_solver",
            side_effect=RuntimeError("solver exploded"),
        ):
            resp = client.post(
                "/v1/models/bess_day_ahead/submit_sync",
                json={"model_input_data": scheduling_input},
            )
            assert resp.status_code == 500
            body = resp.json()
            assert "solver exploded" in body["detail"]["message"]

    def test_missing_model_input_data_returns_422(self, client: TestClient) -> None:
        resp = client.post(
            "/v1/models/bess_day_ahead/submit_sync",
            json={},
        )
        assert resp.status_code == 422

    def test_response_shape_matches_pe_api(
        self, client: TestClient, setpoints_input: dict[str, Any]
    ) -> None:
        """The PE client unwraps res["result"], so the shape must match."""
        resp = client.post(
            "/v1/models/bess_setpoints/submit_sync",
            json={"model_input_data": setpoints_input},
        )
        body = resp.json()
        # Top level has "result"
        assert "result" in body
        result = body["result"]
        # Inside result: "members" with "default" key, and "_info"
        assert "members" in result
        assert "default" in result["members"]
        assert "_info" in result
        assert isinstance(result["_info"], list)

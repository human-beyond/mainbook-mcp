"""Local-only REST stub used by the D8 live MCP transport smoke."""

from __future__ import annotations

import uuid
from typing import Any

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

STUB_API_KEY = "mb_live_local_stub_only"


class StubState:
    def __init__(self, origin: str) -> None:
        self.origin = origin
        self.jobs: dict[str, dict[str, Any]] = {}

    def public_job(self, job_id: str) -> dict[str, Any]:
        record = self.jobs[job_id]
        state = record["state"]
        return {
            "job_id": job_id,
            "state": state,
            "filename": record["filename"],
            "file_format": "pdf",
            "pages": record["pages"],
            "credits_reserved": None if state == "succeeded" else record["pages"],
            "source": "api",
            "created_at": "2026-08-10T12:00:00Z",
            "updated_at": "2026-08-10T12:00:01Z",
            "validation": (
                {"reconcilable": True, "passed": True, "mismatched_rows": 0}
                if state == "succeeded"
                else None
            ),
            "error": None,
        }

    def export(self, job_id: str) -> dict[str, Any]:
        record = self.jobs[job_id]
        return {
            "document": {
                "id": job_id,
                "display_name": record["filename"],
                "bank_name": "Local Stub Bank",
                "account_holder": "Smoke Test",
                "account_address": "",
                "account_number_masked": "****0001",
                "account_type": "checking",
                "kind": "bank_statement",
                "currency": "USD",
                "period_start": "2026-07-01",
                "period_end": "2026-07-31",
                "billing_cycle_length_days": 31,
                "starting_balance_cents": 10000,
                "ending_balance_cents": 11000,
                "transactions_count": 1,
                "net_credits_cents": 1000,
                "net_debits_cents": 0,
                "credit_limit_cents": None,
                "available_credit_cents": None,
                "previous_balance_cents": None,
                "new_balance_cents": None,
                "payment_due_amount_cents": None,
                "payment_due_date": None,
                "pages": record["pages"],
            },
            "transactions": [
                {
                    "row": 1,
                    "source_file": record["filename"],
                    "page": 1,
                    "line_index": 1,
                    "date": "2026-07-05",
                    "description": "Local smoke deposit",
                    "amount_cents": 1000,
                    "transaction_type": "credit",
                    "balance_after_cents": 11000,
                    "currency": "USD",
                    "validation_status": "valid",
                    "warning_flags": [],
                    "cardholder": "",
                    "cardholder_card_masked": "",
                }
            ],
            "has_warnings": False,
        }


def build_stub_app(origin: str) -> Starlette:
    state = StubState(origin)

    def authorized(request: Request) -> bool:
        return request.headers.get("Authorization") == f"Bearer {STUB_API_KEY}"

    async def balance(request: Request) -> Response:
        if not authorized(request):
            return JSONResponse({"detail": "invalid_key"}, status_code=401)
        return JSONResponse({"balance": 100, "reserved": 0, "available": 100})

    async def jobs(request: Request) -> Response:
        if not authorized(request):
            return JSONResponse({"detail": "invalid_key"}, status_code=401)
        if request.method == "GET":
            results = [state.public_job(job_id) for job_id in reversed(list(state.jobs))]
            return JSONResponse({"results": results, "next": None, "previous": None})
        body = await request.json()
        job_id = str(uuid.uuid4())
        state.jobs[job_id] = {
            "filename": body["filename"],
            "pages": body["page_count"],
            "state": "awaiting_upload",
            "uploaded": False,
        }
        return JSONResponse(
            {
                "job_id": job_id,
                "upload": {
                    "url": f"{origin}/upload/{job_id}",
                    "method": "PUT",
                    "headers": {
                        "Content-Type": "application/pdf",
                        "X-Stub-Signed": "exact-value",
                    },
                    "expires_at": "2026-08-10T12:15:00Z",
                },
                "credits_reserved": body["page_count"],
            },
            status_code=201,
        )

    async def upload(request: Request) -> Response:
        if "authorization" in request.headers:
            return JSONResponse({"detail": "bearer_key_leaked_to_storage"}, status_code=400)
        if request.headers.get("Content-Type") != "application/pdf":
            return JSONResponse({"detail": "signed_content_type_changed"}, status_code=400)
        if request.headers.get("X-Stub-Signed") != "exact-value":
            return JSONResponse({"detail": "signed_header_changed"}, status_code=400)
        job_id = request.path_params["job_id"]
        body = await request.body()
        state.jobs[job_id]["uploaded"] = body.startswith(b"%PDF")
        return Response(status_code=200)

    async def start(request: Request) -> Response:
        if not authorized(request):
            return JSONResponse({"detail": "invalid_key"}, status_code=401)
        job_id = request.path_params["job_id"]
        if not state.jobs[job_id]["uploaded"]:
            return JSONResponse({"reason": "invalid_request"}, status_code=409)
        state.jobs[job_id]["state"] = "succeeded"
        return JSONResponse(state.public_job(job_id))

    async def detail(request: Request) -> Response:
        if not authorized(request):
            return JSONResponse({"detail": "invalid_key"}, status_code=401)
        job_id = request.path_params["job_id"]
        if job_id not in state.jobs:
            return JSONResponse({"reason": "not_found"}, status_code=404)
        return JSONResponse(state.public_job(job_id))

    async def result(request: Request) -> Response:
        if not authorized(request):
            return JSONResponse({"detail": "invalid_key"}, status_code=401)
        job_id = request.path_params["job_id"]
        if request.query_params.get("type") != "json":
            return Response(b"binary-stub", media_type="application/octet-stream")
        return JSONResponse(state.export(job_id))

    return Starlette(
        routes=[
            Route("/api/v1/developer/balance", balance, methods=["GET"]),
            Route("/api/v1/developer/jobs", jobs, methods=["GET", "POST"]),
            Route("/api/v1/developer/jobs/{job_id}/start", start, methods=["POST"]),
            Route("/api/v1/developer/jobs/{job_id}/result", result, methods=["GET"]),
            Route("/api/v1/developer/jobs/{job_id}", detail, methods=["GET"]),
            Route("/upload/{job_id}", upload, methods=["PUT"]),
        ]
    )

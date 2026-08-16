"""Schema and validation tests derived from D8 task spec sections 2 and 3."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from mainbook_mcp.models import (
    DEFAULT_POLL_SECONDS,
    ConversionData,
    ConvertBankStatementInput,
    GetConversionInput,
    ListConversionsInput,
)


def test_convert_input_requires_exactly_one_source() -> None:
    """D8 task spec §3.1: exactly one of file_path/file_url is required."""
    with pytest.raises(ValidationError, match="exactly one"):
        ConvertBankStatementInput()

    with pytest.raises(ValidationError, match="exactly one"):
        ConvertBankStatementInput(file_path="a.pdf", file_url="https://example.com/a.pdf")


def test_convert_input_defaults_and_bounds() -> None:
    """D8 task spec §3.1: result_type defaults to JSON and timeout is 30..900 seconds."""
    request = ConvertBankStatementInput(file_path="statement.pdf")
    assert request.result_type == "json"
    # Deliberately under the 60 s request timeout Claude Desktop enforces: a client that gives
    # up first discards the job_id and a paid, still-running conversion looks lost to the user.
    assert request.timeout_seconds == DEFAULT_POLL_SECONDS
    assert DEFAULT_POLL_SECONDS < 60

    with pytest.raises(ValidationError):
        ConvertBankStatementInput(file_path="statement.pdf", timeout_seconds=29)
    with pytest.raises(ValidationError):
        ConvertBankStatementInput(file_path="statement.pdf", timeout_seconds=901)


def test_file_path_schema_explains_allowed_folder_defaults() -> None:
    description = ConvertBankStatementInput.model_json_schema()["properties"]["file_path"][
        "description"
    ].lower()

    assert "allowed folders" in description
    assert all(name in description for name in ("downloads", "desktop", "documents"))


def test_idempotency_key_matches_public_rest_limit() -> None:
    """Design spec §5.5: Idempotency-Key is optional and at most 255 characters."""
    assert ConvertBankStatementInput(file_path="statement.pdf", idempotency_key="x" * 255)
    with pytest.raises(ValidationError):
        ConvertBankStatementInput(file_path="statement.pdf", idempotency_key="x" * 256)


def test_list_input_has_bounded_page_size() -> None:
    """Design spec §5.1 and D8 task spec §3.3: list limit maps to API max page size 100."""
    request = ListConversionsInput()
    assert request.limit == 25
    assert request.cursor is None

    with pytest.raises(ValidationError):
        ListConversionsInput(limit=0)
    with pytest.raises(ValidationError):
        ListConversionsInput(limit=101)


def test_get_conversion_result_type_is_explicit() -> None:
    """D8 task spec §3.4: get_conversion can retrieve JSON or explain binary retrieval."""
    request = GetConversionInput(job_id="00000000-0000-0000-0000-000000000001")
    assert request.result_type == "json"


def test_conversion_data_schema_describes_export_shape() -> None:
    """D8 task spec §3.1: outputSchema must describe _json_doc_payload, not an opaque dict."""
    schema = ConversionData.model_json_schema()
    assert set(schema["properties"]) == {"document", "transactions", "has_warnings"}

    document_ref = schema["properties"]["document"]["$ref"].rsplit("/", 1)[-1]
    transaction_ref = schema["properties"]["transactions"]["items"]["$ref"].rsplit("/", 1)[-1]
    document_properties = schema["$defs"][document_ref]["properties"]
    transaction_properties = schema["$defs"][transaction_ref]["properties"]

    assert {
        "id",
        "display_name",
        "bank_name",
        "account_number_masked",
        "kind",
        "currency",
        "period_start",
        "period_end",
        "starting_balance_cents",
        "ending_balance_cents",
        "transactions_count",
        "pages",
    } <= set(document_properties)
    assert {
        "row",
        "page",
        "line_index",
        "date",
        "description",
        "amount_cents",
        "transaction_type",
        "balance_after_cents",
        "validation_status",
        "warning_flags",
    } <= set(transaction_properties)


class TestServerPayloadTolerance:
    """A field MainBook adds must never take an installed bundle offline."""

    def test_a_new_transaction_field_is_accepted(self) -> None:
        from mainbook_mcp.models import ConversionTransaction

        row = ConversionTransaction.model_validate(
            {
                "row": 1,
                "source_file": "march-statement.pdf",
                "page": 3,
                "line_index": 0,
                "date": "2026-04-01",
                "description": "COFFEE",
                "amount_cents": -1000,
                "transaction_type": "debit",
                "balance_after_cents": None,
                "currency": "USD",
                "validation_status": "valid",
                "warning_flags": [],
                "cardholder": "JORDAN RIVERA",
                "cardholder_card_masked": "****0004",
                "something_mainbook_adds_later": 42,
            }
        )

        assert row.cardholder == "JORDAN RIVERA"
        assert row.cardholder_card_masked == "****0004"

    def test_a_new_document_field_is_accepted(self) -> None:
        from mainbook_mcp.models import ConversionDocument

        document = ConversionDocument.model_validate(
            {
                "id": "abc",
                "display_name": "march-statement.pdf",
                "bank_name": "Northgate Bank",
                "account_holder": "JORDAN RIVERA",
                "account_address": "",
                "account_number_masked": "****0009",
                "account_type": "credit card",
                "kind": "credit_card",
                "currency": "USD",
                "period_start": "2026-03-18",
                "period_end": "2026-04-16",
                "billing_cycle_length_days": 30,
                "starting_balance_cents": 0,
                "ending_balance_cents": 100,
                "transactions_count": 1,
                "net_credits_cents": 0,
                "net_debits_cents": 100,
                "credit_limit_cents": None,
                "available_credit_cents": None,
                "previous_balance_cents": None,
                "new_balance_cents": None,
                "payment_due_amount_cents": None,
                "payment_due_date": None,
                "pages": 6,
                "something_mainbook_adds_later": "x",
            }
        )

        assert document.kind == "credit_card"

    def test_a_typo_in_tool_arguments_is_still_rejected(self) -> None:
        import pytest
        from pydantic import ValidationError

        from mainbook_mcp.models import GetConversionInput

        with pytest.raises(ValidationError):
            GetConversionInput.model_validate({"job_i": "abc"})

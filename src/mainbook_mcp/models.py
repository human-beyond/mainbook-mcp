"""Public input/output models for the MainBook MCP tools."""

from __future__ import annotations

from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

ResultType = Literal["json", "xlsx", "csv"]

# Claude Desktop 1.26832.0 aborts an MCP request after 60 s (`timeout ?? 6e4` in its client) and
# only resets that clock on progress when the caller opts in. A 151-second conversion observed on
# 2026-08-11 was killed there while the job kept running, and the job_id never reached the user.
# Returning our own timeout first keeps the job_id in the answer.
DEFAULT_POLL_SECONDS = 50
JobState = Literal[
    "awaiting_upload",
    "queued",
    "processing",
    "succeeded",
    "succeeded_with_warnings",
    "insufficient_credits",
    "failed",
    "expired",
]


class StrictModel(BaseModel):
    """Reject silent input drift in what a caller hands this server.

    Used for tool arguments, where an unexpected key is a caller's mistake and
    saying so is a kindness. NOT for what MainBook sends back — see
    `ServerPayload`.
    """

    model_config = ConfigDict(extra="forbid")


class ServerPayload(BaseModel):
    """Accept what MainBook sends, including fields added after this release.

    An installed bundle is a frozen copy of this code: the day the API grows a
    field, a strict model here would start rejecting every response and the
    tool would go dark until the user reinstalled. Ignoring unknown keys keeps
    an old bundle working against a newer server, which is the only sane
    default for a client we do not control the release cycle of.
    """

    model_config = ConfigDict(extra="ignore")


class ConvertBankStatementInput(StrictModel):
    """Arguments for the complete create/upload/start/poll/result workflow."""

    file_path: str | None = Field(
        default=None,
        min_length=1,
        description=(
            "Path to a PDF on the machine running the MCP server. This is available only over "
            "stdio and is rejected in HTTP mode. The path must be inside the allowed folders, "
            "which default to Downloads, Desktop, and Documents. Exactly one of file_path and "
            "file_url is required."
        ),
    )
    file_url: str | None = Field(
        default=None,
        min_length=1,
        description=(
            "Public HTTPS URL of a PDF. Redirects and private, loopback, link-local, or metadata "
            "addresses are rejected. Exactly one of file_path and file_url is required."
        ),
    )
    result_type: ResultType = Field(
        default="json",
        description=(
            "Result representation. JSON is returned inline. Over stdio, XLSX and CSV are saved "
            "to an allowed local folder and their path is returned without embedding binary "
            "bytes in model context. HTTP mode returns safe retrieval instructions."
        ),
    )
    timeout_seconds: int = Field(
        default=DEFAULT_POLL_SECONDS,
        ge=30,
        le=900,
        description=(
            "Maximum time to poll inside this call, from 30 to 900 seconds. A timeout returns the "
            "job_id for later use with get_conversion; it does not cancel the job. The default "
            "stays under the 60-second request timeout most MCP clients enforce, because a client "
            "that gives up first discards the job_id and the conversion looks lost. Raise it only "
            "where the client is known to wait longer."
        ),
    )
    idempotency_key: str | None = Field(
        default=None,
        min_length=1,
        max_length=255,
        description=(
            "Optional idempotency key forwarded verbatim as Idempotency-Key when creating the job."
        ),
    )
    output_path: str | None = Field(
        default=None,
        min_length=1,
        description=(
            "Optional absolute file or existing folder path for the result on the MCP server "
            "machine. This is available only over stdio. The destination must be inside the "
            "same allowed folders as local PDF reads. The result extension is always corrected "
            "to match result_type. For JSON, a file is written only when output_path is explicit."
        ),
    )

    @model_validator(mode="after")
    def require_exactly_one_source(self) -> Self:
        if (self.file_path is None) == (self.file_url is None):
            raise ValueError("exactly one of file_path or file_url must be provided")
        return self


class ListConversionsInput(StrictModel):
    """Arguments for one cursor page of account jobs."""

    limit: int = Field(
        default=25,
        ge=1,
        le=100,
        description="Number of jobs to return on this page, from 1 to the REST maximum of 100.",
    )
    cursor: str | None = Field(
        default=None,
        min_length=1,
        description="Opaque cursor returned as next_cursor by an earlier list_conversions call.",
    )


class GetConversionInput(StrictModel):
    """Arguments for checking one job and retrieving its finished representation."""

    job_id: str = Field(
        min_length=1,
        description="MainBook conversion job UUID returned by convert_bank_statement.",
    )
    result_type: ResultType = Field(
        default="json",
        description=(
            "Representation to retrieve after success. JSON is inline. Over stdio, XLSX and CSV "
            "are saved locally; HTTP mode returns the REST retrieval endpoint."
        ),
    )
    output_path: str | None = Field(
        default=None,
        min_length=1,
        description=(
            "Optional absolute file or existing folder path for the result on the MCP server "
            "machine. This is available only over stdio and must be inside an allowed folder."
        ),
    )


class JobValidation(ServerPayload):
    reconcilable: bool | None = Field(
        description=(
            "Whether statement math can be reconciled; null means the document kind has no "
            "running-balance reconciliation, such as many credit-card statements."
        )
    )
    passed: bool = Field(description="Whether MainBook's applicable statement-math checks passed.")
    mismatched_rows: int = Field(
        ge=0,
        description="Number of transaction rows that remain mathematically mismatched.",
    )


class JobError(ServerPayload):
    category: str | None
    reason: str | None


class Job(ServerPayload):
    job_id: str
    state: JobState
    filename: str
    file_format: Literal["pdf"]
    pages: int = Field(ge=1)
    credits_reserved: int | None = Field(default=None, ge=1)
    source: Literal["web", "api"]
    created_at: str
    updated_at: str
    validation: JobValidation | None
    error: JobError | None


class ConversionDocument(ServerPayload):
    """Document summary emitted by MainBook's JSON exporter."""

    id: str
    display_name: str
    bank_name: str
    account_holder: str
    account_address: str
    account_number_masked: str
    account_type: str
    kind: str
    currency: str
    period_start: str
    period_end: str
    billing_cycle_length_days: int | None
    starting_balance_cents: int
    ending_balance_cents: int
    transactions_count: int = Field(ge=0)
    net_credits_cents: int
    net_debits_cents: int
    credit_limit_cents: int | None
    available_credit_cents: int | None
    previous_balance_cents: int | None
    new_balance_cents: int | None
    payment_due_amount_cents: int | None
    payment_due_date: str | None
    pages: int | None = Field(default=None, ge=1)


class ConversionTransaction(ServerPayload):
    """One exported transaction row, with integer-cent monetary fields."""

    row: int = Field(ge=1)
    source_file: str
    page: int = Field(ge=1)
    line_index: int = Field(ge=0)
    date: str
    description: str
    amount_cents: int
    transaction_type: Literal["credit", "debit"]
    balance_after_cents: int | None
    currency: str | None
    validation_status: Literal[
        "valid",
        "system_mismatch",
        "user_edited_mismatch",
        "user_reviewed",
        "user_accepted",
    ]
    warning_flags: list[str]
    # Which card the row came from, on business card accounts that print a
    # section per card. Empty on every other statement. Defaulted so a newer
    # bundle still reads a result produced before MainBook shipped them.
    cardholder: str = ""
    cardholder_card_masked: str = ""


class ConversionData(ServerPayload):
    """Exact top-level JSON shape produced by the MainBook exporter."""

    document: ConversionDocument
    transactions: list[ConversionTransaction]
    has_warnings: bool


class DownloadInstruction(StrictModel):
    job_id: str
    result_type: Literal["xlsx", "csv"]
    rest_endpoint: str
    instruction: str


class SavedFile(StrictModel):
    path: str
    reason: str


class ConversionOutput(StrictModel):
    """Structured result shared by convert_bank_statement and get_conversion."""

    job_id: str
    state: JobState | None
    pages: int | None
    validation: JobValidation | None
    result_type: ResultType
    data: ConversionData | None = None
    download: DownloadInstruction | None = None
    saved_file: SavedFile | None = None
    timed_out: bool = False
    message: str


class BalanceOutput(StrictModel):
    balance: int
    reserved: int = Field(ge=0)
    available: int
    units: Literal["pages"] = "pages"
    explanation: str


class ListConversionsOutput(StrictModel):
    conversions: list[Job]
    next_cursor: str | None
    count: int = Field(ge=0)
    units: Literal["pages"] = "pages"


class OutputFolderOutput(StrictModel):
    output_folder: str
    allowed_folders: list[str]
    message: str

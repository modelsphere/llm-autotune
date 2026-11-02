"""The admin Settings page: external platforms' addresses and LLM endpoints."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class SettingsOut(BaseModel):
    # What is set here ("" when nothing is), and what the install's env says,
    # which applies while nothing is set.
    victorialogs_url: str = ""
    victorialogs_install: str = ""
    catalog_url: str = ""
    catalog_index_install: str = ""
    # The index the catalog export reads: <catalog_url>/index-v2.json, or the
    # install's catalogIndexUrl.
    catalog_index_url: str = ""
    catalog_has_token: bool = False
    # Whether "Publish to catalog" can work: a catalog server and its key.
    catalog_writable: bool = False
    reports_llm_endpoint_id: int | None = None


class SettingsIn(BaseModel):
    """Only what is sent changes. A URL of "" goes back to the install's
    value; a token of "" clears it, and an absent one keeps it."""

    victorialogs_url: str | None = None
    catalog_url: str | None = None
    catalog_token: str | None = None
    reports_llm_endpoint_id: int | None = None
    # Set reports_llm_endpoint_id to null (no endpoint) rather than leave it.
    clear_reports_llm_endpoint: bool = False


class CheckIn(BaseModel):
    # victorialogs | catalog. The URL to check; empty checks the saved one.
    target: str = Field(pattern="^(victorialogs|catalog)$")
    url: str = ""
    # For the catalog: a key to try instead of the saved one.
    token: str = ""


class CheckOut(BaseModel):
    ok: bool
    url: str
    # One line a person can act on.
    message: str
    # The catalog: whether its server takes writes, and whether the key did.
    writable: bool | None = None
    token_ok: bool | None = None
    models: int | None = None


class LlmEndpointIn(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    description: str = ""
    # OpenAI-compatible: requests go to <base_url>/chat/completions.
    base_url: str = Field(min_length=1, max_length=512)
    model: str = Field(min_length=1, max_length=255)
    # Write-only. None (or absent) keeps the stored key; "" clears it.
    api_key: str | None = None


class LlmEndpointOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    name: str
    description: str = ""
    base_url: str
    model: str
    has_api_key: bool = False
    # What uses it: "reports", and the policies' names.
    used_by: list[str] = Field(default_factory=list)
    created_at: datetime | None = None
    updated_at: datetime | None = None


class LlmTestOut(BaseModel):
    ok: bool
    message: str
    seconds: float | None = None
    # The model's reply, cut short; for "it answered", not for content.
    reply: str = ""

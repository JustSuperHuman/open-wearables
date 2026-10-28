import logging
import re
from typing import TYPE_CHECKING, Any

import sentry_sdk
from sentry_sdk.integrations.celery import CeleryIntegration
from sentry_sdk.integrations.logging import LoggingIntegration
from sentry_sdk.scrubber import DEFAULT_DENYLIST, DEFAULT_PII_DENYLIST, EventScrubber

from app import __version__
from app.config import settings
from app.utils.structured_logging import log_structured
from app.utils.url_secrets import redact_path_secrets

if TYPE_CHECKING:
    from sentry_sdk._types import Event, Hint

logger = logging.getLogger(__name__)

FILTERED = "[Filtered]"

# Keys whose values are masked anywhere in request data, extra, contexts, breadcrumb
# data, span data and user (on top of the SDK's credential/IP defaults).
PII_DENYLIST = [
    "email",
    "username",
    "provider_username",
    "first_name",
    "last_name",
    "full_name",
    "birth_date",
    "date_of_birth",
    "phone",
    "phone_number",
    "address",
    "client_secret",
    "code_verifier",
    "id_token",
]

# Pydantic renders the offending raw value into the error string, e.g.
# "[type=int_parsing, input_value='72 bpm', input_type=str]". Greedy up to the last
# ", input_type=" on the line so a repr containing that text cannot leak a tail.
_PYDANTIC_INPUT_VALUE = re.compile(r"input_value=.*, input_type=")
# Emails are the one kind of PII we can reliably spot in free text (exception messages,
# f-string log errors). Health values in prose are not detectable - keep them out of messages.
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
# SQLAlchemy and Postgres errors spell out the failing row: bound parameters, key values,
# "Failing row contains (...)". Those are health values, so everything after the label goes.
_SQL_PARAMETERS = re.compile(r"\[parameters: .*$", re.MULTILINE)
_PG_DETAIL = re.compile(r"DETAIL:.*$", re.MULTILINE)
_QUERY_VALUE = re.compile(r"=[^&]*")

_scrubber = EventScrubber(
    denylist=DEFAULT_DENYLIST + PII_DENYLIST,
    pii_denylist=DEFAULT_PII_DENYLIST,
    recursive=True,
)
# With SENTRY_SEND_SENSITIVE_DATA, credentials (tokens, secrets, cookies) are still masked.
# "headers" covers raw ASGI scopes in frame locals (a Starlette Request is a mapping over its
# scope), whose byte-string headers carry Authorization / API keys. Request headers themselves
# are scrubbed per header by the SDK, so they stay visible.
_credentials_scrubber = EventScrubber(denylist=[*DEFAULT_DENYLIST, "headers"], recursive=True)


def _scrub_text(value: Any) -> Any:
    if isinstance(value, str):
        value = _PYDANTIC_INPUT_VALUE.sub(f"input_value={FILTERED}, input_type=", value)
        value = _SQL_PARAMETERS.sub(f"[parameters: {FILTERED}]", value)
        value = _PG_DETAIL.sub(f"DETAIL: {FILTERED}", value)
        return _EMAIL.sub(FILTERED, value)
    if isinstance(value, dict):
        return {k: _scrub_text(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_scrub_text(v) for v in value]
    return value


# Shorter values could be ordinary words; a configured URL secret is far longer.
_MIN_SECRET_LENGTH = 8


def _url_secrets() -> list[str]:
    """Shared secrets providers send back in our webhook URLs (path or query)."""
    configured = (settings.garmin_webhook_token, settings.withings_webhook_token)
    values = [secret.get_secret_value() for secret in configured if secret is not None]
    return [value for value in values if len(value) >= _MIN_SECRET_LENGTH]


def _redact_url_secrets(value: Any, secrets: list[str]) -> Any:
    if isinstance(value, str):
        value = redact_path_secrets(value, FILTERED)
        for secret in secrets:
            value = value.replace(secret, FILTERED)
        return value
    if isinstance(value, dict):
        return {k: _redact_url_secrets(v, secrets) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact_url_secrets(v, secrets) for v in value]
    return value


def mask_url_secrets(event: "Event", hint: "Hint") -> "Event":
    """Mask webhook tokens carried in the request URL (Garmin's /webhooks/t/{token}).

    Runs in every mode, SENTRY_SEND_SENSITIVE_DATA included: like other credentials, the token is
    never shipped. Besides the request URL, the token shows up in frame locals in sensitive mode
    (the ASGI scope's path, reprs of FastAPI's resolved parameters), so every string in the event
    is checked for the path form and for the configured secret values themselves. Transaction
    names use the route template.
    """
    return _redact_url_secrets(event, _url_secrets())


def before_send(event: "Event", hint: "Hint") -> "Event":
    """Filter free text that the key-based scrubber can't see."""
    event = mask_url_secrets(event, hint)
    # `contexts` is where log_and_capture_error puts `extra` - EventScrubber skips it.
    for key in ("message", "logentry", "contexts", "extra"):
        if key in event:
            event[key] = _scrub_text(event[key])
    _scrubber.scrub_dict(event.get("contexts"))

    for exc in (event.get("exception") or {}).get("values") or []:
        exc["value"] = _scrub_text(exc.get("value"))

    # Keep parameter names for debugging; values can be emails or search terms (?email=, ?search=).
    request = event.get("request") or {}
    if isinstance(query_string := request.get("query_string"), str):
        request["query_string"] = _scrub_text(_QUERY_VALUE.sub(f"={FILTERED}", query_string))

    return event


def init_sentry() -> None:
    if not settings.SENTRY_ENABLED:
        return

    release = f"{__version__}+{settings.GIT_SHA[:12]}" if settings.GIT_SHA else __version__
    sensitive = settings.SENTRY_SEND_SENSITIVE_DATA
    integrations: list[Any] = [CeleryIntegration(monitor_beat_tasks=True, propagate_traces=True)]
    if sensitive:
        log_structured(
            logger,
            "warning",
            "SENTRY_SEND_SENSITIVE_DATA is on - Sentry receives request bodies, locals and log breadcrumbs, "
            "which carry health data and PII",
            action="sentry_sensitive_data_enabled",
        )
    else:
        # Log records are free-form text the scrubber cannot inspect, so don't turn
        # INFO/WARNING logs into breadcrumbs; ERROR logs still become events.
        integrations.append(LoggingIntegration(level=None))

    sentry_sdk.init(
        dsn=settings.SENTRY_DSN,
        environment=settings.SENTRY_ENV,
        server_name=settings.SENTRY_SERVER_NAME,
        release=release,
        traces_sample_rate=settings.SENTRY_SAMPLES_RATE,
        # We process health data: by default never ship request bodies (webhook/SDK payloads),
        # stack-frame locals (parsed records) or default PII (IPs, cookies, task args).
        send_default_pii=False,
        include_local_variables=sensitive,
        max_request_body_size="medium" if sensitive else "never",
        event_scrubber=_credentials_scrubber if sensitive else _scrubber,
        before_send=mask_url_secrets if sensitive else before_send,
        # Sampled transactions carry the request (URL, query string) too but skip before_send.
        before_send_transaction=mask_url_secrets if sensitive else before_send,
        integrations=integrations,
    )

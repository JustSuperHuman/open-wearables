"""Mask the shared secrets some provider webhook URLs carry in their path.

Garmin's portal drops query strings from registered push URLs, so its shared token travels as the
last path segment instead: ``/providers/garmin/webhooks/t/{token}`` (and the legacy
``/garmin/webhooks/push/t/{token}``, ``/garmin/webhooks/ping/t/{token}``). Anything that logs or
reports a request path (the access log, webhook handler logs, Sentry events) passes it through
``redact_path_secrets`` first. Query-string secrets are masked separately by key (see
``app.middlewares``).
"""

import re

REDACTED = "REDACTED"

# The segment after "/t/" on a webhook route. "?" and "#" end it in a URL; quotes and whitespace
# end it where a path is embedded in other text (e.g. a repr of the ASGI scope).
_PATH_TOKEN = re.compile(r"""(/webhooks(?:/push|/ping)?/t/)[^/?#'"\s]+""")


def redact_path_secrets(path: str, replacement: str = REDACTED) -> str:
    """``path`` with any webhook path token replaced by ``replacement``."""
    return _PATH_TOKEN.sub(lambda match: match.group(1) + replacement, path)

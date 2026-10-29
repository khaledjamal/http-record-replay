"""Which values are secrets, and how they are hidden, for the log and the recordings
alike.

- Headers: authorization and API key headers of the common providers (OpenAI, Anthropic,
  Google, Azure, AWS), cookies. An `Authorization` value keeps its scheme
  (`Bearer <redacted>`).
- Query and form parameters: keys, signatures and tokens, including the Azure Functions
  `?code=` key and the signature parts of AWS presigned URLs.
- JSON fields: tokens, passwords, keys, connection strings and credentials.
- More names: HTTP_REDACT_HEADERS, HTTP_REDACT_PARAMS and HTTP_REDACT_FIELDS
  (comma-separated, any case) add to the defaults; read by `load_extra_names()`, which
  enable() calls.
"""

from urllib.parse import unquote_plus

from . import settings

REDACTED = "<redacted>"
# Names compared in lower case.
SECRET_HEADERS = frozenset(
    {
        "authorization",
        "proxy-authorization",
        "cookie",
        "set-cookie",
        "x-api-key",  # Anthropic and many other APIs
        "x-goog-api-key",  # Google
        "api-key",  # Azure OpenAI and Azure AI Search
        "ocp-apim-subscription-key",  # Azure API Management, Azure AI services
        "x-functions-key",  # Azure Functions
        "x-amz-security-token",  # AWS temporary credentials
    }
)
SECRET_PARAMS = frozenset(
    {
        "code",
        "sig",
        "key",
        "api-key",
        "api_key",
        "subscription-key",
        "token",
        "access_token",
        "refresh_token",
        "client_secret",
        "client_assertion",
        "assertion",
        "password",
        "x-amz-signature",
        "x-amz-credential",
        "x-amz-security-token",
    }
)
SECRET_FIELDS = frozenset(
    {
        "access_token",
        "refresh_token",
        "id_token",
        "client_secret",
        "client_assertion",
        "password",
        "api_key",
        "apikey",
        "primarykey",
        "secondarykey",
        "connectionstring",
        "connection_string",
        "credentials",
    }
)
# Fields kept as they are inside a redacted object: an SDK picks the credential class
# from them.
KEPT_IN_SECRETS = frozenset({"type", "kind"})

_extra = {"headers": frozenset(), "params": frozenset(), "fields": frozenset()}


def load_extra_names():
    """Read HTTP_REDACT_HEADERS, HTTP_REDACT_PARAMS and HTTP_REDACT_FIELDS."""
    for kind in _extra:
        _extra[kind] = frozenset(name.lower() for name in settings.names(f"HTTP_REDACT_{kind.upper()}"))


def is_secret_header(name):
    lower = name.lower()
    return lower in SECRET_HEADERS or lower in _extra["headers"]


def is_secret_param(name):
    lower = unquote_plus(name).lower()
    return lower in SECRET_PARAMS or lower in _extra["params"]


def is_secret_field(name):
    lower = name.lower()
    return lower in SECRET_FIELDS or lower in _extra["fields"]


def header(name, value):
    """The value as it may be shown or saved."""
    if not is_secret_header(name):
        return value
    scheme, _, rest = value.partition(" ")
    return f"{scheme} {REDACTED}" if rest and name.lower().endswith("authorization") else REDACTED


def params(query):
    """A query string or form body with the secret parameters' values redacted."""
    parts = []
    for part in query.split("&"):
        name, sep, _ = part.partition("=")
        parts.append(f"{name}={REDACTED}" if sep and is_secret_param(name) else part)
    return "&".join(parts)


def url(text):
    base, sep, query = text.partition("?")
    return base + sep + params(query) if sep else text


def json_keep_shape(value, inside_secret=False, key=None):
    """Replace secret values with REDACTED, keeping the shape of objects under a secret
    field name and their KEPT_IN_SECRETS fields, so code that reads `credentials.key`
    still finds the field."""
    if isinstance(value, dict):
        return {k: json_keep_shape(v, inside_secret or is_secret_field(k), k) for k, v in value.items()}
    if isinstance(value, list):
        return [json_keep_shape(v, inside_secret, key) for v in value]
    if inside_secret and isinstance(value, str) and (key or "").lower() not in KEPT_IN_SECRETS:
        return REDACTED
    return value

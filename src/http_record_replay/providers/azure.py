"""Azure add-on for replay: no sign-in, and fake secrets the Azure SDKs can parse.

- Sign-in: every public azure-identity credential (DefaultAzureCredential,
  AzureCliCredential, ManagedIdentityCredential, ..., sync and async) returns a
  placeholder token, so replay needs no Azure login. All of them matter: the CLI
  credentials run the `az`, `azd` or PowerShell program, which the network guard (a
  socket check inside Python) can't see, so one left unpatched could quietly get a real
  token on a signed-in machine.
- Fake secrets: a redacted secret that the client parses (not only sends back) gets a
  fake value in the same format, from FAKE_SECRETS. An Application Insights connection
  (`type: AppInsights`, as an Azure AI Foundry project returns it) gets a connection
  string with a zero instrumentation key and an `.invalid` endpoint, so
  configure_azure_monitor() starts; its trace exports then find no recorded answer and
  fail inside the exporter, while the program's own calls still replay.
"""

import importlib.util

from .. import redact

PLACEHOLDER_TOKEN = "http-replay-placeholder-token"
# Connection type -> fake credentials.key in the format the client parses.
FAKE_SECRETS = {
    "AppInsights": (
        "InstrumentationKey=00000000-0000-0000-0000-000000000000;IngestionEndpoint=https://http-replay.invalid/"
    ),
}
EXPIRES = 4102444800  # 2100-01-01
# Hosts of telemetry the Azure SDKs send on their own (Azure Monitor exporters, the
# managed identity probe): their replay misses are expected, not failures.
TELEMETRY_HOSTS = (
    "http-replay.invalid",  # the fake ingestion endpoint of FAKE_SECRETS
    "applicationinsights.azure.com",
    "livediagnostics.monitor.azure.com",
    "settings.sdk.monitor.azure.com",
    "QuickPulseService",
    "169.254.169.254",
)


def available():
    return importlib.util.find_spec("azure") is not None and importlib.util.find_spec("azure.core") is not None


def install_replay(patches):
    try:
        import azure.identity
        import azure.identity.aio
        from azure.core.credentials import AccessToken
    except ImportError:
        return
    try:
        from azure.core.credentials import AccessTokenInfo
    except ImportError:  # azure-core before 1.31
        AccessTokenInfo = None

    def get_token(self, *scopes, **kwargs):
        return AccessToken(PLACEHOLDER_TOKEN, EXPIRES)

    def get_token_info(self, *scopes, **kwargs):
        return AccessTokenInfo(PLACEHOLDER_TOKEN, EXPIRES)

    async def get_token_async(self, *scopes, **kwargs):
        return get_token(self)

    async def get_token_info_async(self, *scopes, **kwargs):
        return get_token_info(self)

    for module, token, info in (
        (azure.identity, get_token, get_token_info),
        (azure.identity.aio, get_token_async, get_token_info_async),
    ):
        for owner in method_owners(module, "get_token"):
            patches.set(owner, "get_token", token)
        if AccessTokenInfo is not None:
            for owner in method_owners(module, "get_token_info"):
                patches.set(owner, "get_token_info", info)


def method_owners(module, name):
    """The classes that define the method `name` for the public classes of module: the
    class itself, or the base class it inherits the method from (several credentials
    share one), so each method is patched once and restored where it was."""
    owners = []
    for public_name in module.__all__:
        public_class = getattr(module, public_name)
        if not isinstance(public_class, type) or not hasattr(public_class, name):
            continue
        owner = next(base for base in public_class.__mro__ if name in vars(base))
        if owner not in owners:
            owners.append(owner)
    return owners


def fill_replayed_json(value):
    """Put a FAKE_SECRETS value in place of each redacted `credentials.key` of a
    connection whose type the client parses; everything else stays as recorded."""
    if isinstance(value, list):
        return [fill_replayed_json(v) for v in value]
    if not isinstance(value, dict):
        return value
    filled = {k: fill_replayed_json(v) for k, v in value.items()}
    # A JSON Schema in an agent definition has `type` fields too, some of them lists
    # such as ["string", "null"].
    connection_type = filled.get("type")
    fake_key = FAKE_SECRETS.get(connection_type) if isinstance(connection_type, str) else None
    credentials = filled.get("credentials")
    if fake_key and isinstance(credentials, dict) and credentials.get("key") == redact.REDACTED:
        filled["credentials"] = {**credentials, "key": fake_key}
    return filled

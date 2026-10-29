"""Optional add-ons for one provider's SDK, used by replay. Each is active only when its
SDK is installed.

A provider module has:
- `available()`: True when its SDK is installed
- `install_replay(patches)`: patch the SDK for replay (a sign-in stand-in, for example),
  through patches, so it is undone with them
- `fill_replayed_json(value)`: change a replayed JSON body before the program sees it
  (a fake secret in the format the SDK parses, for example)
- `TELEMETRY_HOSTS`: hosts of telemetry the SDK sends on its own; their replay misses
  are expected (workflow.py counts them apart)
"""

from . import azure

ALL = (azure,)


def active():
    return [provider for provider in ALL if provider.available()]


def telemetry_hosts():
    """The TELEMETRY_HOSTS of every provider, whether its SDK is installed here or not
    (the replayed program may run in another environment)."""
    return [host for provider in ALL for host in provider.TELEMETRY_HOSTS]

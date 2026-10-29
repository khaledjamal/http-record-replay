"""The random seed that makes a replay send the same random values as its record run.

A record run picks a fresh seed and notes it in recording-info.txt (`random seed: <n>`);
replay reads it back. In both modes the seed:
- seeds Python's global `random` (random choices, sampling, shuffles), and
- drives `uuid.uuid4()` through its own generator (generated IDs),
so the program makes the same choices and IDs on replay and its requests match. Each
record run gets a new seed, so the IDs sent to the service stay unique. Not reached:
os.urandom and `secrets`, numpy, values that depend on the order of concurrent tasks.
"""

import random
import threading
import uuid

SEED_LABEL = "random seed:"  # line in recording-info.txt

_lock = threading.Lock()


def new_seed():
    return random.SystemRandom().randrange(2**32)


def apply(random_seed, patches):
    """Seed the global `random` and give uuid.uuid4() its own generator from the same
    seed (patched through patches, so it is undone with them)."""
    random.seed(random_seed)
    uuid_random = random.Random(random_seed)

    def seeded_uuid4():
        with _lock:
            bits = uuid_random.getrandbits(128)
        return uuid.UUID(int=bits, version=4)

    patches.set(uuid, "uuid4", seeded_uuid4)


def reset():
    random.seed()  # back to an unpredictable state


def read_seed(info_path):
    """The seed noted in a recording-info.txt, or None (no file, or a recording made
    before seeds were noted)."""
    if not info_path.exists():
        return None
    for line in info_path.read_text().splitlines():
        if line.startswith(SEED_LABEL):
            return int(line.removeprefix(SEED_LABEL))
    return None

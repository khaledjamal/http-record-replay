"""Replace attributes of other modules and classes, and put the originals back: the one
way this package changes the libraries it hooks."""


class Patches:
    """A group of replaced attributes that is restored all at once."""

    def __init__(self):
        self._originals = {}  # (owner, name) -> original attribute

    def set(self, owner, name, replacement):
        """Put replacement in place of owner.name; the first original is kept, so a
        second set() of the same attribute still restores the real one."""
        if (owner, name) not in self._originals:
            self._originals[(owner, name)] = getattr(owner, name)
        setattr(owner, name, replacement)

    def has(self, owner, name):
        return (owner, name) in self._originals

    def __bool__(self):
        return bool(self._originals)

    def restore(self):
        for (owner, name), original in self._originals.items():
            setattr(owner, name, original)
        self._originals.clear()

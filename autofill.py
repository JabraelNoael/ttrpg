"""Dynamic, prefix-unique autofill for commands and NBT keys.

Rule (the spec): given what's typed, look at every candidate that starts with
it. If EXACTLY ONE matches, autofill to it; if more than one still match, wait.
So with apple / apricot / nice:
    n   -> nice      (unique)
    a   -> (wait)    apple, apricot both match
    ap  -> (wait)    still both
    app -> apple     (unique)
    apr -> apricot   (unique)

Everything is data-driven: add a word and the branch points recompute. NBT
vocabularies are read live off the element classes (GENERIC_NBT + NBT), so
completion is CONTEXT-AWARE: 'pr' -> 'prefix' inside an Item but 'proc' inside a
Talent. Truly dynamic NBT keys (in no list) are typed free-hand, never autofilled.

Boot/config lives in the CONFIG section at the bottom: list your COMMANDS and the
classes whose NBT should autofill. After changing vocab, call refresh() (or use
register_command / register_class) to keep completion live.
"""

from containers import Item, Talent, Ability, Effect, Stat, Player, Mob


class Completer:
    """Prefix-unique completion over a flat list of candidates."""
    def __init__(self, candidates=(), case_sensitive=False):
        self.case_sensitive = case_sensitive
        self.candidates = list(candidates)

    def _norm(self, text):
        return text if self.case_sensitive else text.lower()

    def matches(self, prefix):
        p = self._norm(prefix)
        return [c for c in self.candidates if self._norm(c).startswith(p)]

    def complete(self, prefix):
        """The single candidate this prefix resolves to, or None if 0 / >1 match.
        An empty prefix never autofills (everything would match)."""
        if not prefix:
            return None
        hits = self.matches(prefix)
        return hits[0] if len(hits) == 1 else None

    def suggest(self, prefix):
        """Every candidate matching the prefix — for showing options when ambiguous."""
        return self.matches(prefix)


def nbt_vocabulary(cls):
    """A class's autofill keys: GENERIC_NBT + its non-generic NBT, deduped in order."""
    vocab = []
    for key in list(getattr(cls, "GENERIC_NBT", [])) + list(getattr(cls, "NBT", [])):
        if key not in vocab:
            vocab.append(key)
    return vocab


class Autofill:
    """Builds completers from the command list and per-type NBT vocabularies.

    Parsed on boot via refresh(); stays dynamic through register_command /
    register_class (each re-parses). Command completion is slash-aware:
    '/gi' -> '/give', and a bare 'gi' resolves too.
    """
    def __init__(self, commands=(), classes=(), case_sensitive=False):
        self.case_sensitive = case_sensitive
        self._commands = list(commands)
        self._classes = list(classes)
        self.refresh()

    def refresh(self):
        self.command_completer = Completer(self._commands, self.case_sensitive)
        self.nbt_completers = {}
        for cls in self._classes:
            type_id = getattr(cls, "type_id", cls.__name__.lower())
            self.nbt_completers[type_id] = Completer(nbt_vocabulary(cls), self.case_sensitive)

    def register_command(self, name):
        self._commands.append(name)
        self.refresh()

    def register_class(self, cls):
        self._classes.append(cls)
        self.refresh()

    def complete_command(self, typed):
        raw = typed[1:] if typed.startswith("/") else typed
        hit = self.command_completer.complete(raw)
        return "/" + hit if hit else None

    def suggest_command(self, typed):
        raw = typed[1:] if typed.startswith("/") else typed
        return ["/" + c for c in self.command_completer.suggest(raw)]

    def _nbt_completer(self, type_id):
        """The completer for a type, falling back to the base 'item' generic keys for
        unregistered item types (so iron_sword{ completes name/prefix/... without a subclass)."""
        return self.nbt_completers.get(type_id) or self.nbt_completers.get("item")

    def complete_nbt_key(self, prefix, type_id):
        completer = self._nbt_completer(type_id)
        return completer.complete(prefix) if completer else None

    def suggest_nbt_key(self, prefix, type_id):
        completer = self._nbt_completer(type_id)
        return completer.suggest(prefix) if completer else []


# --- CONFIG / BOOT -----------------------------------------------------------
# Command names (no leading slash). When the real command parser exists it can
# register names automatically instead of relying on this static list.
COMMANDS = ["help", "character", "stat", "attack", "formula", "item", "turn", "calendar", "event", "map", "proc", "data", "function", "session", "execute", "tellraw", "summon", "effect", "kill", "talent", "ability"]

# Classes whose NBT keys should autofill. Add specific subclasses here as you
# build them; each subclass's NBT is merged with the inherited GENERIC_NBT.
NBT_CLASSES = [Item, Talent, Ability, Effect, Stat, Player, Mob]

# Ready-to-use engine, built from the config above. Import this: `from autofill import autofill`
autofill = Autofill(commands=COMMANDS, classes=NBT_CLASSES)

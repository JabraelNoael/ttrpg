"""Interactive command shell with LIVE tab-completion.

Launch it:   python3 main.py   (or)   python3 repl.py

TAB completes at every position, like `cd <TAB>`:
    /char<TAB>                 -> /character
    /character <TAB>           -> new modify show list remove get   (subcommands)
    /character new <TAB>       -> existing character names
    /stat hero <TAB>           -> set add modify get list           (operations)
    /item give hero iron_sword{<TAB>   -> NBT keys for that item

Reading values (two ways):
    /character get hero inventory_slots
    hero.inventory_slots                 (bare path, no slash — just prints the value)
    hero.stat.health                    (reach into containers)
Setting a character field:
    /character modify hero.inventory_slots(3)   (or bare: hero.inventory_slots(3))

The world lives in WORLD for this session (saving to disk is a later step).
Add a command: write a handler + list it in HANDLERS. Add a completable arg type:
extend ARG_SCHEMA / SUBCOMMANDS / OPS.
"""

import re
import os
import sys
import csv
import copy
import json
import random
import shutil
import string
import difflib
import threading
try:
    import gnureadline as readline  # real GNU readline (enables ordered/tiered listing + 1-tab)
except ImportError:
    import readline  # macOS falls back to libedit (works, but lists in its own sorted columns)
from containers import (Player, Mob, Item, Talent, Ability, Effect, Quest, Hook, HOOK_CONTEXTS, Structure, Cooldown, Cost, Proc, Reaction,
                        UnitValue, Formula, STAT_NAMES, FORMULA_CONSTANTS,
                        COMBAT_TYPES, COMBAT_RULES,
                        parse_item_text, parse_warnings, add_parse_warning, _fill_generic_nbt,
                        _split_pairs, _nbt_string, _parse_nbt_value, OBJECTIVE_TYPES)
from autofill import autofill

WORLD = {}  # name -> Player, for this session
WORLD_HOOKS = {}  # name -> Hook, the session's reusable hook library (the unified proc/reaction/on_* system)
HISTORY = []  # session log: function-run lines, '---' between player turns, '===' between cycles
SESSION_DIR = "sessions"
# A session is keyed by a 4-digit numeric ID that doubles as the UI's localhost port (so the URL is
# grounded in the session). The file is sessions/<id>.json; an optional human name lives inside it.
SESSION_PORT_MIN, SESSION_PORT_MAX = 1024, 9999  # valid unprivileged 4-digit ports
SESSION_ID = None    # this session's id / UI port (allocated lazily at first need)
SESSION_NAME = None  # optional friendly name for the current session


def _saved_session_ids():
    """The numeric ids of saved sessions (filenames like 1042.json)."""
    if not os.path.isdir(SESSION_DIR):
        return set()
    return {int(f[:-5]) for f in os.listdir(SESSION_DIR) if f.endswith(".json") and f[:-5].isdigit()}


def _port_in_use(port):
    """True if something is already listening on localhost:port (so two live sessions don't clash)."""
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.05)
        return probe.connect_ex(("127.0.0.1", port)) == 0


def _allocate_session_id():
    """Lowest free 4-digit id from 1024 up: not a saved session, not a port currently in use."""
    taken = _saved_session_ids()
    for candidate in range(SESSION_PORT_MIN, SESSION_PORT_MAX + 1):
        if candidate not in taken and not _port_in_use(candidate):
            return candidate
    return SESSION_PORT_MIN  # everything taken (won't happen at this scale)


def _ensure_session_id():
    """This session's id, allocated once at first need (REPL boot / UI launch / first save)."""
    global SESSION_ID
    if SESSION_ID is None:
        SESSION_ID = _allocate_session_id()
    return SESSION_ID

# Containers a class-path (in cost/reaction) can address. Used to validate paths and to
# autofill inside reaction:[ / cost:[ — catches typos like 'stat.health' (missing 's').
PATH_CONTAINERS = ["stat", "inventory", "equipment", "talent", "ability", "effect", "quest", "nbt"]

# Containers are SINGULAR (stat.health, talent.x, …). Plural spellings do NOT resolve — pure singular.
# The lone alias is attribute(s) -> stat, kept from the stats+attributes merge (atr./attributes. still
# read the stat container, per FORMULA_HELP); that's a deliberate synonym, not plural back-compat.
_CONTAINER_ALIASES = {"attribute": "stat", "attributes": "stat"}


def _canon_container(name):
    """Map a typed container segment to the real Player attribute (only the attribute->stat synonym)."""
    return _CONTAINER_ALIASES.get(name, name)


def _known_keys(player, container):
    """The keys that already 'exist' for a class-path container on this player:
    schema names + live keys for stats, member names for collections, nbt keys."""
    container = _canon_container(container)
    if container == "stat":
        return set(STAT_NAMES) | {s.name for s in player.stat}
    if container == "nbt":
        return set(Player.GENERIC_NBT) | set(player.nbt)
    if container == "inventory":
        return {i.type_id for i in player.inventory.items} | {i.name for i in player.inventory.items if i.name}
    if container in ("talent", "ability", "effect", "quest"):
        return {m.name for m in getattr(player, container)}
    return set()


_MISSING_FNS = []  # functions a just-set reaction references but that don't exist yet (populated by
#                    _path_issues, read by _confirm_or_undo for its 'c = create now' option); cleared per dispatch.


def _path_issues(player, value):
    """All problems with a cost/reaction Cost on this player. Catches (a) an unreal
    container (did-you-mean from PATH_CONTAINERS) and (b) a key that doesn't exist in
    that container — flagging a likely wrong-container typo (e.g. stats.attack when
    attack is an attribute) or a near-miss. A brand-new key (no match anywhere) is left
    alone, since reactions/costs legitimately create new stats."""
    issues = []
    # normalize: Cost has .entries [(path,amount)]; Reaction has .actions [("delta",path,amount)|("func",name)]
    deltas, funcs = [], []
    if isinstance(value, Reaction):
        deltas = [(a[1], a[2]) for a in value.actions if a[0] == "delta"]
        funcs = [a[1] for a in value.actions if a[0] == "func"]
    else:
        deltas = list(getattr(value, "entries", []) or [])
    for path, _amount in deltas:
        container, _, key = path.partition(".")
        container = _canon_container(container)
        if container not in PATH_CONTAINERS:
            guess = difflib.get_close_matches(container, PATH_CONTAINERS, n=1)
            issues.append(f"unknown container '{container}'" + (f" — did you mean '{guess[0]}'?" if guess else ""))
            continue
        # Only 'stats' has a fixed 2-level schema worth a typo check. Member/nbt containers are
        # deeper (inventory.<item>.<field>) and their members come and go — a path to an item/talent
        # not present YET (e.g. ammo added later) is legitimate, not a typo.
        if container != "stat":
            continue
        if not key or "." in key or key in _known_keys(player, container):
            continue
        close = difflib.get_close_matches(key, list(_known_keys(player, container)), n=1)
        if close:
            issues.append(f"{container} has no '{key}' — did you mean {container}.{close[0]}?")
    for name in funcs:  # a reaction naming a function that doesn't exist yet
        if name not in FUNCTIONS:
            if name not in _MISSING_FNS:
                _MISSING_FNS.append(name)  # offer to create it at the undo prompt
            issues.append(f"reaction calls function '{name}' which doesn't exist yet")
    return issues


def _flag_path_issues(player, element):
    """Push cost/reaction/on_hit path problems AND missing-function references (both via
    _path_issues) into the parse-warning collector so the dispatch undo prompt picks them up
    (and can offer to create the function — see _MISSING_FNS / _confirm_or_undo)."""
    for attr in ("cost", "reaction", "on_hit", "reward"):
        for issue in _path_issues(player, getattr(element, attr, None)):
            add_parse_warning(issue)


def _load_proc_names(path="proc_tree.json"):
    """Flatten every key in proc_tree.json into a suggestion list for /proc autofill.
    The tree is a NAMESPACE of known proc names, not a strict whitelist — custom names
    (like 'end_of_turn') are still allowed."""
    try:
        with open(path) as handle:
            tree = json.load(handle)
    except (OSError, ValueError):
        return []
    names = set()

    def walk(node):
        if isinstance(node, dict):
            for key, value in node.items():
                names.add(key)
                walk(value)

    walk(tree.get("events", tree))
    return sorted(names)


PROC_NAMES = _load_proc_names()


# --- value parsing -----------------------------------------------------------

def parse_value(raw):
    """Turn text into a Python value: int, float, bool, None, or str."""
    raw = raw.strip()
    if len(raw) >= 2 and raw[0] in "\"'" and raw[-1] == raw[0]:
        return raw[1:-1]
    low = raw.lower()
    if low == "none":
        return None
    if low == "true":
        return True
    if low == "false":
        return False
    try:
        return int(raw)
    except ValueError:
        pass
    try:
        return float(raw)
    except ValueError:
        pass
    unit = re.match(r"^(-?\d+(?:\.\d+)?)([A-Za-z%]+)$", raw)  # 20ft -> UnitValue (number + unit)
    if unit:
        amount = float(unit.group(1)) if "." in unit.group(1) else int(unit.group(1))
        return UnitValue(amount, unit.group(2))
    return raw


def _parse_kv(raw):
    """Parse 'name=hp value=3' into {'name': 'hp', 'value': 3} for modify ops."""
    fields = {}
    for token in raw.split():
        if "=" in token:
            key, _, val = token.partition("=")
            fields[key] = parse_value(val)
    return fields


def _parse_brace_keys(text):
    """Parse '{race,class}' / '{race:x}' / '{}' into a list of keys (empty list = all)."""
    text = text.strip()
    if text.startswith("{") and text.endswith("}"):
        text = text[1:-1]
    return [p.partition(":")[0].strip() for p in text.split(",") if p.strip()]


def _add_value(obj, key, val):
    """add/append/concat one field by type: number+number, string+string, list+list, or
    append a scalar onto a list. None starts from the empty value of val's type."""
    current = obj.field_value(key)
    if current is None:
        base = "" if isinstance(val, str) else ([] if isinstance(val, list) else 0)
    else:
        base = current
    try:
        if isinstance(base, list) and not isinstance(val, list):
            result = base + [val]   # append a single element to a list
        else:
            result = base + val     # numeric add / string concat / list concat
    except TypeError:
        return print(f"  can't add {val!r} to {key}={current!r}")
    obj.modify(**{key: result})


def _apply_ops(obj, rest):
    """Apply a set|add|reset op to obj (anything with modify/field_value/reset — Player, Item).
      {nbt}                               -> shorthand for 'set {nbt}' (merge the given keys)
      set <field> <value>                 -> update ONE key, touch nothing else
      set {nbt}                           -> merge the given keys, leave the rest untouched
      set {nbt} true                      -> RESET everything to default, THEN set the given keys
      add <field> <value> | add {nbt}     -> add/append/concat (per key)
      reset {keys} | reset <key> | reset  -> handled keys to default; dynamic keys removed; bare = all
    The op word may be first OR right after one field token (like /stat: 'set x v' or 'x set v')."""
    rest = rest.strip()
    if rest.startswith("{"):  # bare braces = shorthand for 'set {nbt}'
        rest = "set " + rest
    op_first = re.match(r"^(set|add|reset)\b(.*)$", rest, re.DOTALL)
    field_first = re.match(r"^(\S+)\s+(set|add|reset)\b(.*)$", rest, re.DOTALL)
    if op_first:
        op, tail = op_first.group(1), op_first.group(2).strip()
    elif field_first:
        op, tail = field_first.group(2), (field_first.group(1) + " " + field_first.group(3)).strip()
    else:
        return print("  expected: set|add|reset ...")
    if op == "reset":
        keys = _parse_brace_keys(tail)
        if "uuid" in keys:
            return print("  'uuid' is managed automatically — change it with /uuid set, not reset")
        return obj.reset(*keys) if tail else obj.reset()
    if not tail:
        return print(f"  usage: {op} <field> <value>   or   {op} {{nbt}}")
    reset_first = False
    if tail.startswith("{"):
        _, payload, remainder = parse_item_text("_" + tail)
        reset_first = remainder.strip().lower() in ("true", "1", "yes")  # 'set {nbt} true'
    else:
        field, _, value_text = tail.partition(" ")
        payload = {field: parse_value(value_text.strip())}
    if "uuid" in payload:  # uuid is managed automatically; never set it via /modify
        return print("  'uuid' is managed automatically — change it with /uuid set")
    if op == "set":
        if reset_first:                  # 'set {nbt} true': wipe the nbt block (keep name/identity), refill defaults
            if hasattr(obj, "nbt"):
                kept_uuid = obj.nbt.get("uuid")  # the uuid survives a reset (it's identity, not data)
                obj.nbt.clear()
                if kept_uuid:
                    obj.nbt["uuid"] = kept_uuid
            _fill_generic_nbt(obj)
        obj.modify(**payload)
    else:
        for key, val in payload.items():
            _add_value(obj, key, val)


def _split_player(rest):
    """Peel the player name off the front. Matches the longest known name (names may
    contain spaces); else falls back to the first whitespace token."""
    rest = rest.strip()
    best = None
    for name in WORLD:
        if (rest == name or rest.startswith(name + " ")) and (best is None or len(name) > len(best)):
            best = name
    if best is None:
        first, _, remainder = rest.partition(" ")
        return first, remainder.strip()
    return best, rest[len(best):].strip()


# --- selectors (players / entities) ------------------------------------------
# A <selector> is a name, a group (@a players, @!a NPCs, @m mobs, @ all entities), a sticky
# ref (@p previous / @s self), OR a list [a,b,c] (runs once per entry). Any of the @-groups can
# carry an nbt FILTER: @m[species="wolf"], @[mob=true], @a[class="rogue"]. <obj> = an item.
SELECTOR_TOKENS = ["@", "@a", "@!a", "@m", "@p", "@!p", "@s", "@!s", "@o", "@!o"]
CURRENT = None  # @p — the sticky last-used selection; set whenever a concrete selector resolves
SELF = None     # @s — the SUBJECT: owner of the proc/function/reaction currently firing (the one acted ON)
ORIGIN = None   # @o — the ORIGIN/cause: who TRIGGERED the current effect (attacker on a hit, caster of an
                # ability, owner of a proc). @!o = everyone except them. Set alongside SELF at trigger time.
AT_POS = None   # the POSITION context (x,y) set by `/execute at`; the reference cell for @[distance=..]
_INHERIT = object()  # sentinel for _run_function/_apply_reaction: keep the current ORIGIN (don't reset it)


def _is_npc(player):
    """A character is an NPC if its nbt 'npc' flag is truthy. Drives @a (players) vs @!a (NPCs)."""
    return bool(player.field_value("npc"))


def _is_mob(player):
    """A mob is an entity with the 'mob' flag (summoned via /summon). Drives @m."""
    return bool(player.field_value("mob"))


def _matching_bracket(text, start):
    """Index of the ']' matching the '[' at `start` (quote-aware), or -1."""
    depth, in_quote = 0, False
    for i in range(start, len(text)):
        char = text[i]
        if char == '"':
            in_quote = not in_quote
        elif not in_quote and char == "[":
            depth += 1
        elif not in_quote and char == "]":
            depth -= 1
            if depth == 0:
                return i
    return -1


def _split_selector(rest):
    """Peel a selector off the front: a list [a,b,c]; an @-group with an optional [filter]
    (@m[species="wolf"]); or a (multi-word) name."""
    rest = rest.strip()
    if rest.startswith("["):  # standalone list [a,b,c]
        end = _matching_bracket(rest, 0)
        if end != -1:
            return rest[:end + 1], rest[end + 1:].strip()
    if rest.startswith("@"):  # @-group, maybe with a [filter]
        i = 0
        while i < len(rest) and rest[i] not in " [":
            i += 1
        if i < len(rest) and rest[i] == "[":
            end = _matching_bracket(rest, i)
            if end != -1:
                return rest[:end + 1], rest[end + 1:].strip()
        return rest[:i], rest[i:].strip()
    return _split_player(rest)


def _num(text):
    """Parse to int/float, or None if it isn't a number."""
    text = text.strip()
    try:
        return int(text)
    except ValueError:
        try:
            return float(text)
        except ValueError:
            return None


def _parse_range(text):
    """A Minecraft-style numeric range -> (lo, hi) with None = open-ended. 'a..b', '..b', 'a..',
    or a bare number 'n' (-> (n, n)). Returns None if it isn't a number/range."""
    text = text.strip()
    if ".." in text:
        lo_s, _, hi_s = text.partition("..")
        lo, hi = _num(lo_s), _num(hi_s)
        if (lo_s.strip() and lo is None) or (hi_s.strip() and hi is None):
            return None
        return (lo, hi)
    n = _num(text)
    return (n, n) if n is not None else None


def _in_range(value, rng):
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    lo, hi = rng
    return (lo is None or value >= lo) and (hi is None or value <= hi)


def _parse_cells(text):
    """'[2,2]' or '{[2,2],[2,1]}' -> a set of (x,y) cells."""
    return {(int(x), int(y)) for x, y in re.findall(r"\[\s*(-?\d+)\s*,\s*(-?\d+)\s*\]", text)}


def _ref_pos():
    """The reference cell for distance filters: the /execute `at` position, else the @s entity's cell."""
    if AT_POS is not None:
        return AT_POS
    return _entity_pos(SELF) if SELF is not None else None


def _match_stats(p, body):
    """stat=[health=..50, attack=10] — every inner stat condition must hold (ranges or exact)."""
    if body.startswith("[") and body.endswith("]"):
        body = body[1:-1]
    for pair in _split_pairs(body):
        if "=" not in pair:
            continue
        name, _, spec = pair.partition("=")
        name, spec = name.strip(), spec.strip()
        val = _effective_stat(p, name) if any(s.name == name for s in p.stat) else None
        if ".." in spec:
            if not _in_range(val, _parse_range(spec) or (None, None)):
                return False
        elif val != parse_value(spec):
            return False
    return True


def _match_flags(p, body):
    """flag=in_combat (must be on) | flag=[in_combat=true, scared=false] — checks proc_state."""
    body = body.strip()
    if body.startswith("[") and body.endswith("]"):
        for pair in _split_pairs(body[1:-1]):
            if "=" not in pair:
                continue
            k, _, v = pair.partition("=")
            want = v.strip().lower() not in ("false", "0", "no", "")
            if bool(p.proc_state.get(k.strip(), False)) != want:
                return False
        return True
    return bool(p.proc_state.get(body, False))


def _match_collection(p, attr, spec):
    """quest={villageelder{uuid:qs-1842}} | quest=villageelder | quest={uuid:qs-1842} — true if the
    entity HOLDS a member matching the name/uuid and/or the nbt fields. attr: quest/talent/ability/
    effect, or item (-> inventory)."""
    members = list(p.inventory.items if attr == "item" else (getattr(p, attr, []) or []))
    spec = spec.strip()
    if spec.startswith("{") and spec.endswith("}"):
        spec = spec[1:-1].strip()
    name, nbt = None, {}
    if "{" in spec:                                  # name{nbt}
        try:
            name, nbt, _ = parse_item_text(spec)
        except ValueError:
            name = spec
    elif ":" in spec:                                # nbt-only: key:val[,key:val] (e.g. uuid:qs-1842)
        try:
            _, nbt, _ = parse_item_text("_{" + spec + "}")
        except ValueError:
            pass
    else:                                            # bare name-or-uuid token
        name = spec
    for m in members:
        mname = getattr(m, "name", None) or getattr(m, "type_id", None)
        muuid = m.nbt.get("uuid") if hasattr(m, "nbt") else None
        if name is not None and mname != name and muuid != name:
            continue
        if nbt and not all((m.field_value(k) if hasattr(m, "field_value") else None) == v for k, v in nbt.items()):
            continue
        return True
    return False


_COLLECTION_FILTERS = {"quest", "talent", "ability", "effect", "item"}


def _filter_targets(targets, filt):
    """Keep targets matching every key=value clause (no silent field->stat fallback; namespaces are explicit):
       distance=<range>           Chebyshev from the `at` position (or @s) — distance=..1, 2.., 2..5
       pos=[x,y] | pos={[x,y],..} the target stands on one of these cells
       stat=[health=..50, ...]    stat conditions (ranges or exact)
       flag=in_combat | flag=[a=true,b=false]   proc_state signals
       quest|talent|ability|effect|item={name{nbt}}   holds a matching member (by name/uuid and/or nbt)
       <field>=<range>|<value>    an nbt/core FIELD (species, class, npc, level…) — range or exact equality."""
    clauses = [(k.strip(), v.strip()) for pair in _split_pairs(filt) if "=" in pair
               for k, _, v in [pair.partition("=")]]
    out = []
    for n, p in targets:
        keep = True
        for key, raw in clauses:
            if key == "distance":
                ref, rng, d = _ref_pos(), _parse_range(raw), None
                if ref is not None:
                    pp = _entity_pos(p)
                    d = max(abs(ref[0] - pp[0]), abs(ref[1] - pp[1])) if pp is not None else None
                if rng is None or d is None or not _in_range(d, rng):
                    keep = False; break
            elif key == "pos":
                if _entity_pos(p) not in _parse_cells(raw):
                    keep = False; break
            elif key == "stat":
                if not _match_stats(p, raw):
                    keep = False; break
            elif key == "flag":
                if not _match_flags(p, raw):
                    keep = False; break
            elif key in _COLLECTION_FILTERS:
                if not _match_collection(p, key, raw):
                    keep = False; break
            elif ".." in raw:                        # numeric range on an nbt/core field
                if not _in_range(p.field_value(key), _parse_range(raw) or (None, None)):
                    keep = False; break
            elif p.field_value(key) != parse_value(raw):   # exact equality on a field
                keep = False; break
        if keep:
            out.append((n, p))
    return out


def _resolve_base(token):
    """Resolve a selector WITHOUT a filter: a list, an @-group, or a name."""
    if token.startswith("[") and token.endswith("]"):  # list [a,b,c] — run once per entry
        result = []
        for part in token[1:-1].split(","):
            part = part.strip()
            if not part:
                continue
            resolved = _resolve(part)
            if resolved is None:
                return None
            result += resolved
        return result
    if token == "@":  # universal — every entity in the world
        return list(WORLD.items())
    if token == "@a":
        return [(n, p) for n, p in WORLD.items() if not _is_npc(p)]  # players (non-NPC)
    if token == "@!a":
        return [(n, p) for n, p in WORLD.items() if _is_npc(p)]  # NPCs (incl. mobs)
    if token == "@m":
        return [(n, p) for n, p in WORLD.items() if _is_mob(p)]  # mobs only
    if token == "@p":  # the sticky last-used selection
        if CURRENT is None:
            print("  no previous selector (@p) yet — name someone first")
            return None
        return [(n, p) for n, p in CURRENT if n in WORLD]
    if token == "@!p":
        prev_names = {n for n, _ in (CURRENT or [])}
        return [(n, p) for n, p in WORLD.items() if not _is_npc(p) and n not in prev_names]
    if token == "@s":  # the proc/function owner (set during firing); see SELF
        if SELF is None:
            print("  @s (self) is only valid inside a running reaction/function")
            return None
        return [(SELF.name, SELF)]
    if token == "@!s":
        self_name = SELF.name if SELF is not None else None
        return [(n, p) for n, p in WORLD.items() if not _is_npc(p) and n != self_name]
    if token == "@o":  # the origin/cause (attacker, caster, proc owner); set during firing
        if ORIGIN is None:
            print("  @o (origin) is only valid inside an effect that has a known cause")
            return None
        return [(ORIGIN.name, ORIGIN)]
    if token == "@!o":
        origin_name = ORIGIN.name if ORIGIN is not None else None
        return [(n, p) for n, p in WORLD.items() if not _is_npc(p) and n != origin_name]
    if token in WORLD:
        return [(token, WORLD[token])]
    print(f"  no player/selector '{token}'")
    return None


def _resolve(token):
    """Resolve a selector to a list of (name, Player), applying any [filter] (e.g.
    @m[species="wolf"]). Returns None (after an error) if the base is unknown."""
    token = token.strip()
    base, filt = token, None
    if not token.startswith("[") and "[" in token and token.endswith("]"):
        base, _, inside = token.partition("[")
        filt = inside[:-1]
    targets = _resolve_base(base.strip())
    if targets is None:
        return None
    return _filter_targets(targets, filt) if filt is not None else targets


def _is_zero(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value == 0


def _container_line(player, container_attr):
    """Render a value/collection container's contents as 'a, b, c'. For STATS, hide
    derived stats (those with a formula) that currently sit at 0 — they're just noise —
    and append a '(+N hidden at 0)' note so nothing is dropped silently."""
    members = list(getattr(player, container_attr))
    hidden = 0
    if container_attr == "stat":
        kept = []
        for member in members:
            if member.name in player.formula and _is_zero(member.value):
                hidden += 1
            else:
                kept.append(member)
        members = kept
    if not members:
        return "(none)" if not hidden else f"(none shown; {hidden} derived at 0 hidden)"
    line = ", ".join(str(m) for m in members)
    return line + (f"   (+{hidden} derived at 0 hidden)" if hidden else "")


def _resolved(sel):
    """_resolve, but also treat an EMPTY match (e.g. @!a, or a list that expands to
    nothing) as a reported failure — so every command gives feedback instead of silently
    doing nothing. Returns the targets, or None (after printing) on error/empty.
    Side effect: a CONCRETE selector (a name, @a, @!a, or a list — not the @p/@s reference
    tokens) becomes the new @p 'previous selection' (a group sets @p to the whole group)."""
    global CURRENT
    targets = _resolve(sel)
    if targets is None:
        return None
    if not targets:
        print("  (nothing matched)")
        return None
    if sel.strip() not in ("@p", "@!p", "@s", "@!s", "@o", "@!o"):
        CURRENT = list(targets)
    return targets


# --- /character --------------------------------------------------------------
def _split_name_nbt(arg):
    """Split '<name>[{nbt}] [is_npc]' into ('hero', {...}); names may have spaces. An optional
    trailing true/false (outside the braces) sets nbt['npc'] — /character new goblin{...} true."""
    arg = arg.strip()
    npc = None
    if "{" in arg:
        name = arg[:arg.index("{")].strip()
        close = arg.rindex("}")
        _, nbt, _ = parse_item_text("_" + arg[arg.index("{"):close + 1])
        trailing = arg[close + 1:].strip().lower()
        if trailing in ("true", "false"):
            npc = trailing == "true"
    else:
        nbt = {}
        head, _, tail = arg.rpartition(" ")
        if head and tail.lower() in ("true", "false"):
            name, npc = head.strip(), tail.lower() == "true"
        else:
            name = arg
    if npc is not None:
        nbt = dict(nbt)
        nbt["npc"] = npc
    return name, nbt


def _character_set(name, field, value):
    """Bare-path / dotted set: hero.race("elf"). Any field works (handled or dynamic nbt).
    Note: `name` is a data field — setting it does NOT change the WORLD handle."""
    player = WORLD.get(name)
    if player is None:
        print(f"  no character named '{name}'")
        return False
    player.modify(**{field: value})
    print(f"  {name}.{field} -> {value!r}")
    return True


def _add_renamed(cmd):
    """The create-sense 'add' was renamed to 'new' (so it no longer collides with the + operator)."""
    print(f"  '{cmd} add' was split: use '/{cmd} new' to CREATE. (The + operator is '/{cmd} modify ... add', where supported.)")
    return False


def cmd_character(rest):
    sub, _, arg = rest.partition(" ")
    sub, arg = sub.strip(), arg.strip()
    if sub == "add":
        return _add_renamed("character")
    if sub == "new":
        if not arg:
            print("  usage: /character new <name>[{nbt}] [is_npc]   (trailing true = NPC)")
            return False
        name, nbt = _split_name_nbt(arg)
        if name in WORLD:
            print(f"  '{name}' already exists")
            return False
        global CURRENT
        WORLD[name] = _fill_generic_nbt(Player(name).modify(**nbt))  # pre-fill all tier keys
        _seed_equip_slots(WORLD[name])  # per-race slot capacities (from equipment.json), unless given
        if not _is_npc(WORLD[name]):
            WORLD[name].stat.set("turn", 0)  # non-NPCs join the turn order with a personal turn count
        CURRENT = [(name, WORLD[name])]  # the just-created character becomes @p
        print(f"  created '{name}'" + (f" with {nbt}" if nbt else ""))
        return True
    elif sub == "modify":
        # legacy dotted form still works for a single character: hero.field(value)
        dotted = re.match(r"^(\w+)\.(\w+)\((.*)\)$", arg)
        if dotted:
            return _character_set(dotted.group(1), dotted.group(2), parse_value(dotted.group(3)))
        sel, ops = _split_selector(arg)
        targets = _resolved(sel)
        if targets is None:
            return False
        if not ops.strip():
            print("  usage: /character modify <selector> <set|add|reset> {nbt}  (or  <name>.<field>(<value>))")
            return False
        for name, player in targets:
            _apply_ops(player, ops.strip())  # `name` is a data field; the WORLD handle is unchanged
            print(f"  {player}")
        return True
    elif sub == "get":
        sel, field = _split_selector(arg)
        field = field.strip()
        targets = _resolved(sel)
        if targets is None:
            return False
        if not field:
            print("  usage: /character get <selector> <field>  (e.g. inventory_slots, race, or any nbt key)")
            return False
        values = []
        for name, player in targets:
            value = player.field_value(field)
            print(f"  {name}.{field} = {value!r}")
            values.append(value)
        return values[0] if len(values) == 1 else values
    elif sub == "remove":
        sel, _ = _split_selector(arg)
        targets = _resolved(sel)
        if targets is None:
            return False
        removed = False
        for name, player in list(targets):
            del WORLD[name]
            print(f"  removed '{name}'")
            removed = True
        return removed
    elif sub == "show":
        sel, _ = _split_selector(arg)
        targets = _resolved(sel)
        if targets is None:
            return False
        for name, player in targets:
            print(f"  {player}")
        return True
    elif sub == "list":
        print("  " + (", ".join(WORLD) if WORLD else "(no characters yet)"))
        return list(WORLD)
    else:
        print("  usage: /character <new|modify|get|remove|show> <selector>  |  /character new <name>  |  /character list")


# --- /stat and /attribute (subcommand form, consistent with /item) -----------
# Each stat/attribute is name=value. Names live UNDER the subcommands (you tab to a
# subcommand first), so they don't clutter the top level.
STAT_OPS = ["set", "add", "reset"]


def _value_command(rest, container_attr, cmd_name):
    """/<cmd> new    <selector> <name> [value]   (or {name:val,...} to bulk-set)
       /<cmd> modify <selector> <name> <set|add|reset> [value]   (op acts on the value)
       /<cmd> get <selector> <name>   |   list <selector>   |   remove <selector> <name>"""
    sub, _, arg = rest.partition(" ")
    sub, arg = sub.strip(), arg.strip()
    sel, remainder = _split_selector(arg)
    targets = _resolved(sel)
    if targets is None:
        return False

    if sub == "list":
        results = []
        for name, player in targets:
            members = list(getattr(player, container_attr))
            print(f"  {name}.{container_attr}: " + _container_line(player, container_attr))
            results.append([m.name for m in members])
        return results[0] if len(results) == 1 else results
    if sub == "get":
        key = remainder.split()[0] if remainder.split() else None
        if not key:
            print(f"  usage: /{cmd_name} get <selector> <name>")
            return False
        values = []
        for name, player in targets:
            value = getattr(player, container_attr).get(key)
            print(f"  {name}.{container_attr}.{key} = {value!r}")
            values.append(value)
        return values[0] if len(values) == 1 else values
    if sub == "remove":
        keys = remainder.split()
        if not keys:
            print(f"  usage: /{cmd_name} remove <selector> <name>")
            return False
        for name, player in targets:
            for key in keys:
                getattr(player, container_attr).remove(key)
            print(f"  {name}.{container_attr}: " + _container_line(player, container_attr))
        return True
    if sub == "add":
        return _add_renamed(cmd_name)
    if sub == "new":
        if not remainder:
            print(f"  usage: /{cmd_name} new <selector> <name> [value]   (or {{name:val,...}})")
            return False
        for name, player in targets:
            container = getattr(player, container_attr)
            if remainder.startswith("{"):
                _, nbt, _ = parse_item_text("_" + remainder)
                container.modify(**nbt)
            else:
                parts = remainder.split()
                container.set(parts[0], parse_value(parts[1]) if len(parts) > 1 else 0)
            player.recompute()  # a base-stat change refreshes derived stats
            print(f"  {name}.{container_attr}: " + _container_line(player, container_attr))
        return True
    if sub == "modify":
        if remainder.startswith("{"):  # bulk MERGE: /stat modify sean {health:20,mana:30}
            _, nbt, _ = parse_item_text("_" + remainder)
            for name, player in targets:
                container = getattr(player, container_attr)
                for key, value in nbt.items():
                    container.set(key, value)
                player.recompute()
                print(f"  {name}.{container_attr}: " + _container_line(player, container_attr))
            return True
        args = remainder.split()
        op_idx = next((i for i, t in enumerate(args) if t in STAT_OPS), None)
        if op_idx is None:
            print(f"  usage: /{cmd_name} modify <selector> <name> <set|add|reset> [value]  |  modify <selector> {{name:val,...}}")
            return False
        op = args[op_idx]
        others = args[:op_idx] + args[op_idx + 1:]
        if not others:
            print(f"  usage: /{cmd_name} modify <selector> <name> {op} [value]")
            return False
        key = others[0]
        value_text = " ".join(others[1:])              # the raw value (may be a '=' expression)
        is_expr = value_text.startswith("=")           # set health =MAX(1.5, stat.health-1.5)
        for name, player in targets:
            container = getattr(player, container_attr)
            if op == "reset":  # reset = remove the key (consistent with /character reset)
                container.remove(key)
                player.recompute()
                print(f"  {name}.{container_attr}: removed '{key}'")
                continue
            # expressions evaluate PER player (they read this player's atr/stat); else a literal
            value = _eval_expr(player, value_text[1:]) if is_expr else (parse_value(others[1]) if len(others) > 1 else 0)
            if op == "set":
                container.set(key, value)
            elif op == "add":
                if key not in container:
                    container.set(key, 0)
                container[key].add(value)
            player.recompute()  # a base-stat change refreshes derived stats
            print(f"  {name}.{container_attr}: {container[key]}")
        return True
    print(f"  usage: /{cmd_name} <new|get|list|modify|remove> <selector> ...")
    return False


def _eval_expr(player, expr):
    """Evaluate a formula expression against a player's stats (reuses the formula engine).
    Lets `/stat modify set =MAX(1.5, stat.health - 1.5)` compute a clamped value, etc."""
    lookup = lambda n: player.stat.get(n) or 0
    return Formula(expr).evaluate(lookup, lookup)


STAT_CHANNELS = ("base", "multiplier", "mult", "flat")  # 'mult' is a hidden alias of 'multiplier'


def _stat_channel_modify(targets, channel, parts):
    """The layered modify: adjust a stat's base value, its multiplier, or its flat layer with
    set|add|reset (reset clears that layer back to its default). Same value logic as /stat modify,
    including '=EXPR' (evaluated per player). effective = (base + gear) * multiplier + flat
    (multiplier defaults to 1, flat to 0; kept in nbt stat_mult / stat_flat)."""
    if len(parts) < 2 or parts[1] not in STAT_OPS:
        print(f"  usage: /stat modify <selector> {'base|multiplier|flat' if channel not in STAT_CHANNELS else channel} <stat> <set|add|reset> [value]")
        return False
    stat, op = parts[0], parts[1]
    value_text = " ".join(parts[2:])
    is_expr = value_text.startswith("=")
    is_mult = channel in ("multiplier", "mult")
    label = "base" if channel == "base" else ("multiplier" if is_mult else "flat")
    for name, player in targets:
        value = _eval_expr(player, value_text[1:]) if is_expr else (parse_value(parts[2]) if len(parts) > 2 else 0)
        if channel == "base":                   # the base value itself (same layer as /stat new)
            if op == "reset":
                player.stat.remove(stat)
            else:
                cur = player.stat.get(stat) or 0
                player.stat.set(stat, value if op == "set" else cur + value)
            shown = player.stat.get(stat)
        else:                                    # the multiplier / flat modifier layer (nbt)
            tkey, default = ("stat_mult", 1) if is_mult else ("stat_flat", 0)
            table = player.nbt.setdefault(tkey, {})
            if op == "reset":
                table.pop(stat, None)
            else:
                table[stat] = value if op == "set" else table.get(stat, default) + value
            shown = table.get(stat, default)
        player.recompute()
        detail = "reset" if op == "reset" else f"= {shown}"
        print(f"  {name}.{label}.{stat} {detail}  (-> {round(_effective_stat(player, stat), 3)})")
    return True


def cmd_stat(rest):
    """/stat new|get|list|remove ...  AND  /stat modify <selector> [base|multiplier|flat] <stat>
    <set|add|reset> [value]. With no channel keyword, modify acts on the base value (as before);
    base|multiplier|flat target that layer of  effective = (base + gear) * multiplier + flat."""
    sub, _, arg = rest.partition(" ")
    sub = sub.strip()
    if sub == "modify":                          # the ONLY layered form: /stat modify <sel> <channel> <stat> <op>
        sel, remainder = _split_selector(arg)
        parts = remainder.split()
        if parts and parts[0] in STAT_CHANNELS:
            targets = _resolved(sel)
            return _stat_channel_modify(targets, parts[0], parts[1:]) if targets is not None else False
    return _value_command(rest, "stat", "stat")


# --- /formula (per-player derived-stat formulas) -----------------------------

def cmd_formula(rest):
    """/formula new|modify <selector> <stat> = <expression>   set a derived-stat formula
       /formula remove <selector> <stat>   |   /formula list <selector>
       /formula recompute <selector>   re-evaluate all formulas into stats
    The stat target is bare (always stat.<name>); the '=' separator is optional.
    Expressions reference stat.X (atr.X works as an alias), with + - * / ^, IF(c,a,b), LET(name,val,body)."""
    sub, _, arg = rest.partition(" ")
    sub, arg = sub.strip(), arg.strip()
    sel, remainder = _split_selector(arg)
    targets = _resolved(sel)
    if targets is None:
        return False
    if sub == "list":
        for name, player in targets:
            print(f"  {name}.formula:")
            for stat_name in player.formula:
                print(f"    {stat_name} = {player.formula.get(stat_name)}")
            if not len(player.formula):
                print("    (none)")
        return True
    if sub == "recompute":
        for name, player in targets:
            player.recompute()
            print(f"  {name}: recomputed {len(player.formula)} formulas")
        return True
    if sub == "add":
        return _add_renamed("formula")
    if sub in ("new", "modify"):
        stat_name, _, expr = remainder.partition(" ")
        stat_name, expr = stat_name.strip(), expr.strip()
        if expr.startswith("="):  # optional '=' separator: /formula new noael rage = atr.attack
            expr = expr[1:].strip()
        if not stat_name or not expr:
            print("  usage: /formula new <selector> <stat> = <expression>")
            return False
        for name, player in targets:
            player.formula.add(stat_name, expr)
            player.recompute()
            print(f"  {name}.formula.{stat_name} = {player.formula.get(stat_name)}  ->  stat.{stat_name} = {player.stat.get(stat_name)}")
        return True
    if sub == "remove":
        stat_name = remainder.split()[0] if remainder.split() else None
        if not stat_name:
            print("  usage: /formula remove <selector> <stat>")
            return False
        for name, player in targets:
            player.formula.remove(stat_name)
            had_stat = stat_name in player.stat
            player.stat.remove(stat_name)  # drop the derived stat too — no frozen orphan value
            note = " (and its stat)" if had_stat else ""
            print(f"  {name}: removed formula '{stat_name}'{note}")
        return True
    print("  usage: /formula <new|modify|remove|list|recompute> <selector> ...")
    return False


# --- equipment: per-race slot capacities (equipment.json) --------------------
# equipment.json maps race -> {slot: capacity}. A character's equip_slots nbt is seeded from its
# race on creation (unless given explicitly); the Equipment container's per-slot capacity is read
# live from that nbt, so changing equip_slots changes what the character can wear.
def _load_equip_races(path="equipment.json"):
    try:
        with open(path) as handle:
            data = json.load(handle)
            return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except ValueError as err:
        print(f"  [warning] {path} failed to parse ({err}); no race slot-sets loaded")
        return {}


EQUIP_RACES = _load_equip_races()


def _seed_equip_slots(player):
    """Give a new character its race's slot capacities (equipment.json), unless equip_slots was
    set explicitly at creation. Falls back to the 'default' race entry."""
    if isinstance(player.nbt.get("equip_slots"), dict):
        return  # explicitly provided — respect it
    race = player.field_value("race")
    slots = EQUIP_RACES.get(race) or EQUIP_RACES.get("default")
    if slots:
        player.nbt["equip_slots"] = dict(slots)


def _equip_capacity(player, slot):
    """How many items this character can wear in `slot` (0 = can't, e.g. race lacks it)."""
    slots = player.nbt.get("equip_slots")
    return int(slots.get(slot, 0) or 0) if isinstance(slots, dict) else 0


def _parse_element_specs(text):
    """Parse a bracketed list of element specs 'name{nbt},name2{nbt}' into [(name, nbt), ...].
    (The list parser leaves bracket-values with braces as raw text, so we split them here.)"""
    text = str(text or "").strip()
    if text.startswith("[") and text.endswith("]"):
        text = text[1:-1]
    specs = []
    for chunk in _split_pairs(text):
        chunk = chunk.strip()
        if chunk:
            type_id, nbt, _ = parse_item_text(chunk)
            specs.append((type_id, nbt))
    return specs


def _grant_equip_effects(player, item):
    """One-shot EVENT side of equipping (not the continuous grants — those are derived by
    _regrant_items): fire the `on_equip` REACTION (deltas/functions, NOT auto-undone) and pulse
    'item_equipped' + any on_equip_proc. Continuous talents/abilities/effects/quests/flags come from
    the item's `grants`/`while_held` nbt and are (re)applied by _refresh_inventory."""
    _report_fired(player.name, _pulse_procs(player, ["item_equipped"] + _proc_tokens(item.nbt.get("on_equip_proc"))))
    _report_fired(player.name, _emit(player, "equip", origin=player, subject=item))   # on_equip is now an on:equip hook


def _revoke_equip_effects(player, item):
    """One-shot EVENT side of unequipping: fire on_unequip REACTION + pulse 'item_unequipped' + any
    on_unequip_proc. Continuous grants are removed by _refresh_inventory once the item leaves the slot."""
    _report_fired(player.name, _pulse_procs(player, ["item_unequipped"] + _proc_tokens(item.nbt.get("on_unequip_proc"))))
    _report_fired(player.name, _emit(player, "unequip", origin=player, subject=item))   # on_unequip is now an on:unequip hook


# --- derived item grants: talents/abilities/effects/quests/flags an item confers while HELD (in the
# inventory) and/or while EQUIPPED. Recomputed from scratch on every possession change (like the
# equipped-stat bonus), so they're auto-reversible, save/load-safe, and edit-safe. ----------------
_GRANT_KINDS = {"talent": "talent", "ability": "ability", "effect": "effect", "quest": "quest"}


def _grant_specs(value):
    """Parse a grant list into [(kind, body), ...]. kind in talent|ability|effect|quest|flag; body is
    an element spec ('fireproof', 'fireball{cooldown:3}') or a flag token ('blessed', 'cursed:false').
    A bare entry with no recognized kind: prefix defaults to a talent (back-compat with `talents`).
    Accepts either a real list (the nbt parser yields one when there are no nested braces) or a raw
    bracket string (it falls back to raw text when an element carries its own {nbt})."""
    if isinstance(value, (list, tuple)):
        chunks = [str(c) for c in value]
    else:
        text = str(value or "").strip()
        if text.startswith("[") and text.endswith("]"):
            text = text[1:-1]
        chunks = _split_pairs(text)
    out = []
    for chunk in chunks:
        chunk = chunk.strip()
        if not chunk:
            continue
        kind, sep, body = chunk.partition(":")
        kind = kind.strip().lower()
        if sep and kind in _GRANT_KINDS or kind == "flag":
            out.append((kind, body.strip()))
        else:
            out.append(("talent", chunk))   # bare 'fireproof' -> a talent grant
    return out


def _item_grant_source(item):
    return item.nbt.get("uuid") or item.type_id   # stable per-item id for tagging (uuid if minted)


def _is_item_granted(el):
    """True if an element was conferred by an item (so it's derived, not persisted)."""
    return bool(el.nbt.get("_granted_by") or el.nbt.get("_equipped_by"))


def _desired_grants(player):
    """The grants every possessed item SHOULD currently confer:
       {(source, kind, key): (kind, body, source)}  — key = element name or flag name (nbt-independent
       so re-grants are stable). while_held applies whenever possessed; grants/when_equipped/legacy
       `talents` apply only while the item sits in an equipment slot."""
    desired = {}
    possessed = [(it, False) for it in player.inventory.items]
    for items in player.equipment.equipped().values():
        possessed += [(it, True) for it in items]
    for item, equipped in possessed:
        source = _item_grant_source(item)
        specs = list(_grant_specs(item.nbt.get("while_held")))
        if equipped:
            specs += _grant_specs(item.nbt.get("grants"))
            specs += _grant_specs(item.nbt.get("talents"))                       # legacy equipped talents
            specs += [("flag", t) for t in _proc_tokens(item.nbt.get("when_equipped"))]
        for kind, body in specs:
            key = _signal_value(body)[0] if kind == "flag" else parse_item_text(body)[0]
            desired[(source, kind, key)] = (kind, body, source)
    return desired


def _regrant_items(player):
    """Make the player's granted talents/abilities/effects/quests/flags match _desired_grants: remove
    granted ones no longer conferred, add newly-conferred ones (preserving any whose source+kind+name
    still holds, so ability cooldowns / effect timers don't reset every recompute)."""
    desired = _desired_grants(player)
    sets = {"talent": player.talent, "ability": player.ability, "effect": player.effect, "quest": player.quest}
    builders = {"talent": Talent, "ability": Ability, "effect": Effect, "quest": Quest}
    present = set()
    for kind, setobj in sets.items():
        for el in list(setobj):
            src = el.nbt.get("_granted_by") or el.nbt.get("_equipped_by")   # _equipped_by = legacy tag
            if src is None:
                continue
            key = (src, kind, el.name)
            if key in desired:
                present.add(key)
            else:
                setobj.remove(el)
    for (source, kind, name), (k, body, src) in desired.items():
        if kind == "flag" or (source, kind, name) in present:
            continue
        type_id, nbt, _ = parse_item_text(body)
        el = builders[kind].from_nbt(type_id, dict(nbt))
        el.nbt["_granted_by"] = source
        el.nbt["_granted"] = True
        sets[kind].add(el)
    # flags: diff against the set we granted last time so we never clear a manually-set, non-granted flag
    want = {}
    for (source, kind, key), (k, body, src) in desired.items():
        if kind == "flag":
            name, val = _signal_value(body)
            want[name] = val
    prev = set(player.nbt.get("_granted_flags") or [])
    for f in prev - set(want):
        player.proc_state.pop(f, None)
    player.proc_state.update(want)
    player.nbt["_granted_flags"] = sorted(want)


# ---- INVENTORY SPACE (grid containers) -------------------------------------------------------
# A player's grid inventory is a list of CONTAINERS. The BASE set lives in the player's
# `inventory_space` nbt (a raw "[{...}]" string, like proc); equipped/held items that declare
# their own `inventory_space` APPEND more containers (a backpack, pockets, cybernetic module slots).
# Each container: {id, label, color, style, cells:[[r,c],...]}. `cells` is a free-form mask so a
# grid can be non-rectangular (rows/cols appended one at a time). Items carry a footprint
# `shape:[[r,c],...]` and a placement `pos:{c:<container id>,x,y,r}`. Zero-shape items take no grid
# space (they live in the loose bin). All of this is data the UI authors; the engine just stores it.
DEFAULT_CONTAINER = {"id": "pack", "label": "Pack", "color": "#6f8fae", "style": "round",
                     "cells": [[r, c] for r in range(4) for c in range(6)]}


def _parse_space(value):
    """Parse an inventory_space value (raw "[{...},{...}]" string, or an already-parsed list) into a
    list of container dicts. Tolerant: empty/garbage -> []."""
    if isinstance(value, list):
        return [dict(c) for c in value if isinstance(c, dict)]
    text = str(value or "").strip()
    if not (text.startswith("[") and text.endswith("]")):
        return []
    out = []
    for chunk in _split_pairs(text[1:-1]):
        chunk = chunk.strip()
        if not chunk.startswith("{"):
            continue
        try:
            _, nbt, _ = parse_item_text("_" + chunk)
        except ValueError:
            continue
        out.append(nbt)
    return out


def _space_to_text(containers):
    """Render a list of container dicts back to a raw "[{...}]" string for storage in nbt."""
    return "[" + ",".join(_nbt_string(c) for c in containers) + "]"


def _base_containers(player):
    """The player's OWN containers (source 'base'), parsed from nbt."""
    return _parse_space(player.nbt.get("inventory_space"))


def _base_or_default(player):
    """Base containers, or the implicit default (materialized) when none are set yet — so the
    default 'pack' the UI shows becomes a real, editable container the moment it's touched."""
    return _base_containers(player) or [dict(DEFAULT_CONTAINER)]


def _write_base_containers(player, containers):
    player.nbt["inventory_space"] = _space_to_text(containers)


def _reveal_active(player, reveal):
    """A container's `reveal` gate: the container only appears while this flag/proc is active in the
    player's proc_state. Equipping (via when_equipped), a use-toggle, an on_equip that sets a flag,
    etc. are all just ways to turn it on — same proc_state that `flag=` conditions read."""
    return bool(player.proc_state.get(str(reveal), False))


def _effective_inventory_space(player):
    """The player's LIVE container list: base containers + any contributed by possessed items
    (held OR equipped). A contributed container's id is namespaced by its source uuid so two
    backpacks never collide; `source` records provenance ('base' or the item's uuid). A container
    carrying `reveal:<flag>` only shows while that flag/proc is active (so a backpack reveals its
    space only once worn/used, not just carried)."""
    out = []
    base = _base_containers(player)
    for cont in base:
        cont = dict(cont)
        cont.setdefault("source", "base")
        if cont.get("reveal") and not _reveal_active(player, cont["reveal"]):
            continue
        out.append(cont)
    if not base:
        out.append(dict(DEFAULT_CONTAINER, source="base"))
    possessed = list(player.inventory.items)
    for items in player.equipment.equipped().values():
        possessed += items
    for item in possessed:
        provided = _parse_space(item.nbt.get("inventory_space"))
        if not provided:
            continue
        src = _item_grant_source(item)
        for cont in provided:
            cont = dict(cont)
            if cont.get("reveal") and not _reveal_active(player, cont["reveal"]):
                continue
            cont["id"] = f"{src}:{cont.get('id') or 'c'}"
            cont["source"] = src
            cont.setdefault("label", item.name or item.type_id)
            out.append(cont)
    return out


def _refresh_inventory(player):
    """Re-derive item grants, then recompute stats. Call after ANY possession change (equip, unequip,
    use/consume, add, remove, loot, load)."""
    _regrant_items(player)
    player.recompute()


def _equip_weapon(player, item, wield):
    """Equip a weapon into hand slot(s) with REPLACEMENT: 'both' takes BOTH hands; 'main'/'off'
    replace that hand; equipping into a hand a two-hander occupies frees the two-hander. Displaced
    weapons return to inventory (and their equip-effects are revoked)."""
    need = ["main_hand", "off_hand"] if wield == "both" else (["main_hand"] if wield == "main" else ["off_hand"])
    for hand in need:
        if _equip_capacity(player, hand) <= 0:
            print(f"  {player.name} has no '{hand}' slot — can't wield {item.type_id}")
            return
        player.equipment.define_slot(hand, _equip_capacity(player, hand))

    def clear(slot):
        freed = []
        for worn in list(player.equipment.equipped(slot)):
            player.equipment.unequip(slot, worn)
            player.inventory.add(worn)
            _revoke_equip_effects(player, worn)
            freed.append(worn.type_id)
        return freed
    main = player.equipment.equipped("main_hand")
    displaced = []
    if wield == "both":
        displaced += clear("main_hand") + clear("off_hand")
        target = "main_hand"
    elif wield == "main":
        displaced += clear("main_hand")
        target = "main_hand"
    else:  # off — a two-hander in main_hand uses both hands, so it must come off
        if main and main[0].nbt.get("wield") == "both":
            displaced += clear("main_hand")
        displaced += clear("off_hand")
        target = "off_hand"
    player.inventory.remove(item)
    player.equipment.equip(target, item)
    _grant_equip_effects(player, item)
    _refresh_inventory(player)
    where = "both hands" if wield == "both" else target
    print(f"  {player.name} wields {item.type_id} ({where})" + (f"; displaced {displaced}" if displaced else ""))


def _is_uuid_token(token):
    """True if `token` is a bare uuid like 'it-aB3x' (2-char type code, dash, 4 base62)."""
    return bool(re.fullmatch(r"[A-Za-z0-9]{2}-[A-Za-z0-9]{4}", token.strip()))


def _inv_item(player, token):
    """Find an inventory item by bare UUID (exact, unambiguous) OR by name/type_id (legacy)."""
    if _is_uuid_token(token):
        return next((it for it in player.inventory.items if it.nbt.get("uuid") == token.strip()), None)
    return player.inventory.get(token)


def _item_equip(arg):
    """/item equip <selector> <item-or-uuid> [slot]  — equip an inventory item.
    <item> may be a bare UUID (it-XXXX — exact, handles names with spaces / duplicate names), or a
    name/type_id. Weapons (nbt 'wield' main/off/both) go to hand slots with swap logic; other gear
    uses 'equippable' + the per-race capacity in equip_slots."""
    sel, remainder = _split_selector(arg)
    targets = _resolved(sel)
    if targets is None:
        return False
    parts = remainder.split()
    if not parts:
        print("  usage: /item equip <selector> <item> [slot]")
        return False
    item_name, slot = parts[0], (parts[1] if len(parts) > 1 else None)
    for name, player in targets:
        item = _inv_item(player, item_name)
        if item is None:
            print(f"  {name} has no '{item_name}' in inventory")
            continue
        wield = item.nbt.get("wield")
        hand_slot = slot in ("main_hand", "off_hand", "main", "off", "both")
        # weapons go to hand slots — UNLESS the user explicitly named a different (non-hand) slot
        # (an item can be both wieldable AND wearable, e.g. a ring-dagger; the named slot wins)
        if wield in ("main", "off", "both") and (slot is None or hand_slot):
            _equip_weapon(player, item, wield)
            continue
        equippable = item.nbt.get("equippable")
        options = equippable if isinstance(equippable, list) else ([equippable] if equippable else [])
        chosen = slot or (options[0] if len(options) == 1 else None)
        if chosen is None:
            print(f"  {item_name} fits {options or 'no slots'} — name one: /item equip {name} {item_name} <slot>")
            continue
        if options and chosen not in options:
            print(f"  {item_name} can't go in '{chosen}' (equippable: {options})")
            continue
        cap = _equip_capacity(player, chosen)
        if cap <= 0:
            print(f"  {name} has no '{chosen}' slot (race/equip_slots) — can't equip there")
            continue
        if len(player.equipment.equipped(chosen)) >= cap:
            print(f"  {name}'s '{chosen}' is full ({cap}/{cap})")
            continue
        player.equipment.define_slot(chosen, cap)
        player.inventory.remove(item)
        player.equipment.equip(chosen, item)
        _grant_equip_effects(player, item)
        _refresh_inventory(player)
        print(f"  {name} equipped {item.type_id} in {chosen} ({len(player.equipment.equipped(chosen))}/{cap})")
    return True


def _item_unequip(arg):
    """/item unequip <selector> <item-or-slot>  — return equipped item(s) to inventory and revoke
    any talents/effects they granted. 2nd token = an item name/type, or a slot (clears the slot)."""
    sel, remainder = _split_selector(arg)
    targets = _resolved(sel)
    if targets is None:
        return False
    key = remainder.split()[0] if remainder.split() else None
    if not key:
        print("  usage: /item unequip <selector> <item-or-slot>")
        return False
    for name, player in targets:
        worn = player.equipment.equipped()  # {slot: [items]}
        pairs = ([(key, it) for it in list(worn[key])] if key in worn
                 else [(slot, it) for slot, items in worn.items() for it in list(items)
                       if it.name == key or it.type_id == key or it.nbt.get("uuid") == key])
        removed = []
        for slot, item in pairs:
            player.equipment.unequip(slot, item)
            player.inventory.add(item)
            _revoke_equip_effects(player, item)
            removed.append(f"{item.type_id} from {slot}")
        if removed:
            _refresh_inventory(player)
            print(f"  {name} unequipped " + ", ".join(removed))
        else:
            print(f"  {name} has nothing equipped matching '{key}'")
    return True


# --- /item add|remove|loot|modify|list ---------------------------------------

def _item_add(arg):
    sel, remainder = _split_selector(arg)
    targets = _resolved(sel)
    if targets is None:
        return False
    if not remainder:
        print("  usage: /item new <selector> <item>[{nbt}] [count]")
        return False
    try:
        type_id, nbt, after = parse_item_text(remainder)
    except ValueError as error:
        print(f"  NBT parse error: {error}")
        return False
    qty = None
    if after:
        try:
            qty = int(after.split()[0])
        except ValueError:
            print(f"  count must be a number, got '{after.split()[0]}'")
            return False
    for name, player in targets:
        item = Item.from_nbt(type_id, nbt)
        if qty is not None:
            item.quantity = qty
        player.inventory.add(item)
        _refresh_inventory(player)   # a held item may confer while_held grants
        print(f"  gave {name}: {item}")
    return True


def _item_remove(arg):
    """/item remove <selector> <obj>[count]  — remove count copies (default: the whole stack)."""
    sel, remainder = _split_selector(arg)
    targets = _resolved(sel)
    if targets is None:
        return False
    parts = remainder.split()
    if not parts:
        print("  usage: /item remove <selector> <item> [count]")
        return False
    obj_sel = parts[0]
    explicit = None
    if len(parts) > 1:
        try:
            explicit = int(parts[1])
        except ValueError:
            print(f"  count must be a number, got '{parts[1]}'")
            return False
    ok = False
    for name, player in targets:
        inv = player.inventory
        item, sel_count, error = _select_item(inv, obj_sel)
        if item is None:
            print(f"  {name}: {error}")
            continue
        n = explicit if explicit is not None else item.quantity
        taken = _take_from_stack(inv, item, n)
        _refresh_inventory(player)   # losing a held item drops its while_held grants
        print(f"  {name}: removed {taken.quantity}x {taken.type_id}")
        ok = True
    return ok


def _item_list(arg):
    sel, _ = _split_selector(arg)
    targets = _resolved(sel)
    if targets is None:
        return False
    for name, player in targets:
        items = player.inventory.items
        cap = player.inventory.slots if player.inventory.slots is not None else "∞"
        if not items:
            print(f"  {name}'s inventory is empty (0/{cap})")
            continue
        print(f"  {name}'s inventory ({len(items)}/{cap}):")
        for i, item in enumerate(items):
            print(f"    [{i}] x{item.quantity}  {item}")
    return True


def _take_from_stack(inventory, item, n):
    """Split n copies off a stack. Returns a detached Item; mutates the stack in place."""
    n = min(n, item.quantity)
    if n >= item.quantity:
        inventory.items.remove(item)
        return item
    item.quantity -= n
    clone = item.copy()
    clone.quantity = n
    return clone


def _select_item(inv, selector):
    """Locate the item an <obj> selector points at. Forms:
       [N]           -> the inventory entry at index N (see /item list)
       obj           -> first stack whose type_id OR name is obj
       obj[N]        -> the Nth stack matching obj (0-based)
       obj{nbt}      -> also match the given nbt fields; count: = how many copies to affect
    Returns (item, count_to_affect, error_message)."""
    selector = selector.strip()
    bracket = re.match(r"^\[(\d+)\]$", selector)
    if bracket:
        idx = int(bracket.group(1))
        if 0 <= idx < len(inv.items):
            return inv.items[idx], inv.items[idx].quantity, None  # the WHOLE entry/stack at index N
        return None, 1, f"no item at index {idx} (inventory has {len(inv.items)})"
    # a trailing [N] on a type/name selects the Nth matching stack
    nth = None
    trailing = re.search(r"\[(\d+)\]$", selector)
    if trailing:
        nth = int(trailing.group(1))
        selector = selector[:trailing.start()]
    try:
        sel_type, sel_nbt, _ = parse_item_text(selector)
    except ValueError as error:
        return None, 1, str(error)
    count = sel_nbt.pop("count", 1)
    matches = [it for it in inv.items
               if (it.type_id == sel_type or it.name == sel_type) and all(it.field_value(k) == v for k, v in sel_nbt.items())]
    if not matches:
        return None, count, f"no item matching '{sel_type}'"
    if nth is not None:
        if 0 <= nth < len(matches):
            return matches[nth], count, None
        return None, count, f"'{sel_type}' has no match #{nth} ({len(matches)} found)"
    return matches[0], count, None


def _item_modify(arg):
    """/item modify <selector> <obj>[{count:N}|[index]] <set|add|reset> <field> <value>
    Affects N copies (default 1), splitting them off the stack."""
    sel, remainder = _split_selector(arg)
    targets = _resolved(sel)
    if targets is None:
        return False
    parts = remainder.split(None, 1)
    if len(parts) < 2:
        print("  usage: /item modify <selector> <item>[{count:N}|[index]] <set|add|reset> <field> <value>")
        return False
    obj_sel, rest = parts[0], parts[1].strip()
    ok = False
    for name, player in targets:
        inv = player.inventory
        item, count, error = _select_item(inv, obj_sel)
        if item is None:
            print(f"  {name}: {error}")
            continue
        idx = inv.items.index(item)
        whole = count >= item.quantity
        portion = _take_from_stack(inv, item, count)
        _apply_ops(portion, rest)            # shared set|add|reset logic
        # merge into an identical stack, else re-insert (whole-stack edits keep their slot)
        for existing in inv.items:
            if existing.stack_key() == portion.stack_key():
                existing.quantity += portion.quantity
                break
        else:
            inv.items.insert(idx if whole else len(inv.items), portion)
        print(f"  {name}: modified {portion.quantity}x -> {portion}")
        ok = True
    return ok


def _item_place(arg):
    """/item place <selector> <item-uuid> <container> <x> <y> [rot]
    Set an item's grid placement (the WHOLE stack — no split, so a stack stays one footprint).
    x=col, y=row (top-left of the footprint), rot in {0,90,180,270}. The container id is whatever
    /state reports for the player (base or an item-provided 'src:id')."""
    sel, remainder = _split_selector(arg)
    targets = _resolved(sel)
    if targets is None:
        return False
    parts = remainder.split()
    if len(parts) < 4:
        print("  usage: /item place <selector> <item-uuid> <container> <x> <y> [rot]")
        return False
    token, container, xs, ys = parts[0], parts[1], parts[2], parts[3]
    rot = parts[4] if len(parts) > 4 else None
    ok = False
    for name, player in targets:
        item = _inv_item(player, token)
        if item is None:
            print(f"  {name}: no item '{token}' in inventory")
            continue
        if container in ("none", "-", "__loose__"):   # unplace -> back to the loose bin
            item.nbt.pop("pos", None)
            print(f"  {name}: unplaced {item.type_id} (loose)")
            ok = True
            continue
        try:
            x, y = int(xs), int(ys)
        except ValueError:
            print("  x and y must be integers")
            continue
        cur_r = int((item.nbt.get("pos") or {}).get("r", 0))
        item.nbt["pos"] = {"c": container, "x": x, "y": y, "r": int(rot) if rot is not None else cur_r}
        print(f"  {name}: placed {item.type_id} -> {container} ({x},{y}) r{item.nbt['pos']['r']}")
        ok = True
    return ok


def _item_rotate(arg):
    """/item rotate <selector> <item-uuid> [deg]  — turn an item's footprint by deg (default 90)."""
    sel, remainder = _split_selector(arg)
    targets = _resolved(sel)
    if targets is None:
        return False
    parts = remainder.split()
    if not parts:
        print("  usage: /item rotate <selector> <item-uuid> [deg]")
        return False
    token = parts[0]
    deg = int(parts[1]) if len(parts) > 1 and parts[1].lstrip("-").isdigit() else 90
    ok = False
    for name, player in targets:
        item = _inv_item(player, token)
        if item is None:
            print(f"  {name}: no item '{token}' in inventory")
            continue
        pos = dict(item.nbt.get("pos") or {})
        pos["r"] = (int(pos.get("r", 0)) + deg) % 360
        item.nbt["pos"] = pos
        print(f"  {name}: rotated {item.type_id} -> r{pos['r']}")
        ok = True
    return ok


def _roll_loot(table):
    """DUMMY loot roll. Endgame: load loottables from CSV/JSON (items + chances) keyed
    by name like 'boss:cyclops', roll against them, return the drops. For now: one stub."""
    return Item(name="Mystery Loot", description=f"placeholder drop from '{table}'", type_id="mystery_loot", origin=table)


def _item_loot(arg):
    table, _, sel = arg.partition(" ")
    table, sel = table.strip(), sel.strip()
    if not table or not sel:
        print("  usage: /item loot <table> <selector>")
        return False
    targets = _resolved(sel)
    if targets is None:
        return False
    for name, player in targets:
        item = _roll_loot(table)
        player.inventory.add(item)
        _refresh_inventory(player)
        print(f"  [loot: DUMMY — real loottables come later] rolled '{table}' -> gave {name}: {item}")
    return True


ITEM_SUBS = ["new", "equip", "unequip", "use", "remove", "loot", "modify", "list", "place", "rotate"]  # 'add'/'give' = hidden aliases of 'new'


def _item_use(arg):
    """/item use <selector> <item>  — use an inventory item.
    Fires the item's `on_use` REACTION (functions + class-path deltas, @s = the user); pulses proc
    signals (base 'item_used' + any nbt `on_use_proc`); and if the item carries a `consume` count
    (the editor's 'destroy qty x N on use'), removes that many from the stack — a depleted stack
    leaves the inventory and additionally pulses 'item_consumed'."""
    sel, remainder = _split_selector(arg)
    targets = _resolved(sel)
    if targets is None:
        return False
    item_name = remainder.strip()
    if not item_name:
        print("  usage: /item use <selector> <item>")
        return False
    for name, player in targets:
        item = player.inventory.get(item_name)
        if item is None:
            print(f"  {name} has no '{item_name}' in inventory")
            continue
        used = _emit(player, "use", origin=player, subject=item)   # on_use is now an on:use hook
        signals = ["item_used"] + _proc_tokens(item.nbt.get("on_use_proc"))
        consume = item.nbt.get("consume")
        qty = int(consume) if isinstance(consume, (int, float)) else (int(consume) if isinstance(consume, str) and consume.isdigit() else (1 if consume else 0))
        consumed = 0
        if qty:
            remaining = (item.quantity or 1) - qty
            if remaining > 0:
                item.quantity = remaining
            else:
                player.inventory.remove(item)
            consumed = qty
            signals.append("item_consumed")
        _refresh_inventory(player)   # consuming a held item may drop its while_held grants
        print(f"  {name} uses {item.type_id}" + (f"; consumed {consumed}" if consumed else "")
              + f"; pulsed {', '.join(signals)}")
        _report_fired(name, used)
        _report_fired(name, _pulse_procs(player, signals))
    return True


def cmd_item(rest):
    sub, _, arg = rest.partition(" ")
    sub, arg = sub.strip(), arg.strip()
    # 'new' is canonical (consistent with /character new etc.); 'add'/'give' are hidden aliases.
    handler = {"new": _item_add, "add": _item_add, "give": _item_add, "remove": _item_remove,
               "equip": _item_equip, "unequip": _item_unequip, "use": _item_use,
               "loot": _item_loot, "modify": _item_modify, "list": _item_list,
               "place": _item_place, "rotate": _item_rotate}.get(sub)
    if handler:
        return handler(arg)
    print("  usage: /item new <selector> <item>  |  equip <selector> <item> [slot]  |  unequip <selector> <item-or-slot>  |  "
          "use <selector> <item>  |  remove <selector> <item> [count]  |  loot <table> <selector>  |  modify <selector> <item> <set|add|reset> {nbt}  |  list <selector>  |  place <selector> <uuid> <container> <x> <y> [rot]  |  rotate <selector> <uuid> [deg]")
    return False


# --- /container (grid inventory containers) ----------------------------------
CONTAINER_SUBS = ["new", "modify", "delete", "list"]


def _apply_container_op(cont, rest):
    """set|add|reset on a plain container dict. `add cells [[r,c],...]` appends cells; everything else
    replaces. Bare 'field value' (no op word) means set."""
    m = re.match(r"^(set|add|reset)\s+(.*)$", rest.strip(), re.DOTALL)
    if not m:
        field, _, val = rest.strip().partition(" ")
        if field:
            cont[field.strip()] = _parse_nbt_value(val.strip())
        return
    op, tail = m.group(1), m.group(2).strip()
    if op == "reset":
        for key in tail.split():
            cont.pop(key, None)
        return
    field, _, val = tail.partition(" ")
    field, value = field.strip(), _parse_nbt_value(val.strip())
    if op == "add" and field == "cells" and isinstance(cont.get("cells"), list) and isinstance(value, list):
        cont["cells"] = cont["cells"] + value
    else:
        cont[field] = value


def _container_new(arg):
    sel, remainder = _split_selector(arg)
    targets = _resolved(sel)
    if targets is None:
        return False
    head = remainder.strip()
    idm = re.match(r"([\w:-]+)", head)
    if not idm:
        print("  usage: /container new <selector> <id>[{label:..,color:..,style:..,cells:[[r,c],...]}]")
        return False
    cid, body = idm.group(1), head[idm.end():].strip()
    nbt = parse_item_text("_" + body)[1] if body.startswith("{") else {}
    ok = False
    for name, player in targets:
        conts = _base_or_default(player)   # keep the implicit default visible alongside a new one
        if any(str(c.get("id")) == cid for c in conts):
            print(f"  {name}: container '{cid}' already exists")
            continue
        conts.append({"id": cid, "label": nbt.get("label", cid), "color": nbt.get("color", "#6f8fae"),
                      "style": nbt.get("style", "round"),
                      "cells": nbt.get("cells") or [[r, c] for r in range(4) for c in range(6)]})
        _write_base_containers(player, conts)
        print(f"  {name}: added container '{cid}'")
        ok = True
    return ok


def _container_modify(arg):
    sel, remainder = _split_selector(arg)
    targets = _resolved(sel)
    if targets is None:
        return False
    parts = remainder.split(None, 1)
    if len(parts) < 2:
        print("  usage: /container modify <selector> <id> <set|add|reset> <field> <value>")
        return False
    cid, rest = parts[0], parts[1].strip()
    ok = False
    for name, player in targets:
        conts = _base_or_default(player)   # materialize the default so editing it persists
        cont = next((c for c in conts if str(c.get("id")) == cid), None)
        if cont is None:
            print(f"  {name}: no base container '{cid}'")
            continue
        _apply_container_op(cont, rest)
        _write_base_containers(player, conts)
        print(f"  {name}: container '{cid}' -> {_nbt_string(cont)}")
        ok = True
    return ok


def _container_delete(arg):
    sel, remainder = _split_selector(arg)
    targets = _resolved(sel)
    if targets is None:
        return False
    cid = remainder.strip()
    if not cid:
        print("  usage: /container delete <selector> <id>")
        return False
    ok = False
    for name, player in targets:
        conts = _base_containers(player)
        kept = [c for c in conts if str(c.get("id")) != cid]
        if len(kept) == len(conts):
            print(f"  {name}: no base container '{cid}'")
            continue
        _write_base_containers(player, kept)
        print(f"  {name}: deleted container '{cid}'")
        ok = True
    return ok


def _container_list(arg):
    targets = _resolved(arg.strip())
    if targets is None:
        return False
    for name, player in targets:
        conts = _effective_inventory_space(player)
        print(f"  {name} containers ({len(conts)}):")
        for c in conts:
            print(f"    {c.get('id')} [{c.get('source', 'base')}] '{c.get('label')}' "
                  f"{len(c.get('cells') or [])} cells {c.get('style')} {c.get('color')}")
    return True


def cmd_container(rest):
    """/container new|modify|delete|list — edit a player's BASE grid containers (the inventory_space
    nbt). Item-provided containers (backpacks/pockets) come from the item, not edited here."""
    sub, _, arg = rest.partition(" ")
    sub, arg = sub.strip(), arg.strip()
    handler = {"new": _container_new, "modify": _container_modify,
               "delete": _container_delete, "list": _container_list}.get(sub)
    if handler:
        return handler(arg)
    print("  usage: /container new <selector> <id>{...}  |  modify <selector> <id> set <field> <value>  |  delete <selector> <id>  |  list <selector>")
    return False


# --- /kill -------------------------------------------------------------------

def cmd_kill(rest):
    """/kill <selector> [true]
    Default (false): set the target's stats.health to 0 and pulse the 'died' proc.
    true: hard-remove the character entirely (the old behavior; will despawn NPCs later)."""
    sel, arg = _split_selector(rest)
    targets = _resolved(sel)
    if targets is None:
        return False
    if not targets:
        print("  (nothing matched)")
        return False
    hard = arg.strip().lower() == "true"
    for name, player in list(targets):
        if hard:
            del WORLD[name]
            print(f"  killed '{name}' (removed)")
        else:
            player.stat.set("health", 0)
            fired = _pulse_procs(player, ["died"])
            print(f"  {name}: health -> 0; pulsed 'died'" + ("" if fired else " (no talents reacted)"))
            _report_fired(name, fired)
    return True


# --- /attack -----------------------------------------------------------------
# Weapons carry their OWN stats (nbt 'stats' dict) so a weapon's profile never leaks across
# others: an attack builds an EFFECTIVE stat set = attacker's stats + ONLY the wielded weapon(s),
# recomputes derived stats on it, then runs the file-driven combat resolution (combat.csv +
# combat_rules.csv) per damage type against the TARGET's defenses. Wield: item nbt wield:main/off/both.
def _weapon_stats(item):
    """A weapon's own stat contributions (nbt 'stats' dict), {} if none."""
    stats = item.nbt.get("stats")
    return dict(stats) if isinstance(stats, dict) else {}


def _validate_wield(weapons):
    """Check a main/off combo. Returns an error string, or None if the combo is legal."""
    wields = [(w.nbt.get("wield") or "main") for w in weapons]
    if "both" in wields and len(weapons) > 1:
        return "a 'both'-hands weapon can't be paired with another"
    if wields.count("main") > 1:
        return "two 'main' weapons can't be wielded together (one must be 'off')"
    if wields.count("off") > 1:
        return "two 'off' weapons can't be wielded together"
    return None


def _effective_attacker(attacker, weapons, attack_override=None):
    """A deep copy of the attacker with each wielded weapon's stats ADDED in (stacked), then
    recomputed — so derived stats (pierce, slash, arcana, *_penetration) reflect char + this weapon."""
    eff = copy.deepcopy(attacker)
    for weapon in weapons:
        for key, value in _weapon_stats(weapon).items():
            eff.stat.set(key, (eff.stat.get(key) or 0) + value)
    if attack_override is not None:
        eff.stat.set("attack", attack_override)
    eff.recompute()
    return eff


def _stat_components(entity, name):
    """The (base, mult, flat) of a stat. base = stored/derived value + equipped-gear base bonus;
    mult (default 1) and flat (default 0) are the modifier layer kept in nbt stat_mult / stat_flat
    (set via /stat mult|flat, and later by gear)."""
    base = (entity.stat.get(name) or 0) + entity._equipped_stat_bonus().get(name, 0)
    mult = (entity.nbt.get("stat_mult") or {}).get(name, 1)
    flat = (entity.nbt.get("stat_flat") or {}).get(name, 0)
    return base, mult, flat


def _effective_stat(entity, name):
    """The stat's final value: (base + gear) * mult + flat. Defaults (mult 1, flat 0) leave it equal
    to base+gear, so this is backward-compatible until a multiplier/flat is set."""
    base, mult, flat = _stat_components(entity, name)
    return base * mult + flat


def _resolve_damage(eff, target, explicit=None):
    """File-driven combat resolution + armor_class (pre-resist) and defense (flat, post).
    `explicit` = {type:raw} feeds raw per type directly (the 'inverse'); else raw comes from the
    effective attacker's derived stats. Returns (total, [(type, raw, net), ...]).
    PIPELINE per type: raw  ->  armor_class gate (if AC > this raw, ignore armor_class_efficiency
    of it, default 0.6)  ->  resist/ward (combat_rules.csv).  Then sum,  then flat `defense`
    (a flat reduction that can NEVER bring the hit below 1)."""
    target.recompute()  # make sure derived defenses (resist/ward) reflect current gear/inputs
    armor_class = _effective_stat(target, "armor_class")
    ac_efficiency = _effective_stat(target, "armor_class_efficiency") or 0.6   # default 60% ignored
    defense = _effective_stat(target, "defense")
    total, breakdown = 0.0, []
    for spec in COMBAT_TYPES:
        typ = spec["type"]
        if explicit is not None:
            if typ not in explicit:
                continue
            raw = float(explicit[typ])
        else:
            raw = float(eff.stat.get(spec["raw"]) or 0)
        if not raw:
            continue
        if armor_class > raw:                 # armor_class only bites hits it out-classes...
            raw *= (1 - ac_efficiency)         # ...then it ignores armor_class_efficiency of them
        pen = float(eff.stat.get(spec["penetration"]) or 0) if spec["penetration"] else 0.0
        res = float(target.stat.get(spec["defense"]) or 0) if spec["defense"] else 0.0
        # the rule is PER-ATTACKER: their own nbt 'combat_rules' override, else the shared default.
        # It may reference the attacker's own stats (stat.crit_chance, …) — those resolve per-player,
        # so adding stat.crit_chance to a rule affects only attackers who actually have that stat.
        own = eff.nbt.get("combat_rules")
        rule = (own.get(spec["channel"]) if isinstance(own, dict) else None) or COMBAT_RULES.get(spec["channel"], "atk.raw")
        atk_vals, def_vals = {"raw": raw, "pen": pen}, {"res": res}
        atk_stat = lambda n: (eff.stat.get(n) or 0)
        net = Formula(rule).evaluate(atk_stat, atk_stat,
                                     namespaces={"atk": atk_vals.get, "def": def_vals.get})
        total += net
        breakdown.append((typ, round(raw, 2), round(net, 2)))
    if total > 1 and defense:                  # flat reduction, floored at 1 (defense never kills)
        total = max(1.0, total - defense)
    return total, breakdown


def _lethal(attacker_name, attacker, target_name, target):
    """A target at <=0 health: execution_check=false (or unset) insta-kills; =true gives the
    ATTACKER a choice (mobs auto-'final kill'). final kill = remove + pulse 'died'; spare = leave alive."""
    def finish():
        fired = _pulse_procs(target, ["died"])
        if target_name in WORLD:
            del WORLD[target_name]
        print(f"    {target_name} is slain (removed)" + ("" if not fired else f"; pulsed 'died' {fired}"))
    if not target.field_value("execution_check"):
        finish()
        return
    if _is_mob(attacker) or not sys.stdin.isatty():  # mobs always choose final kill; non-interactive too
        print(f"    [execution check] {attacker_name} (auto) -> final kill")
        finish()
        return
    try:
        choice = input(f"    [execution check] {attacker_name}: final kill / spare? [k/s]: ").strip().lower()
    except EOFError:
        choice = "k"
    if choice.startswith("s"):
        print(f"    {target_name} is spared (left at {round(target.stat.get('health') or 0, 2)} health)")
    else:
        finish()


def cmd_attack(rest):
    """/attack <attacker> <target>                            use the attacker's EQUIPPED hands (main+off)
       /attack <attacker> <target> with <weapon>[+<weapon2>]   override: wield inventory item(s) for this hit
       /attack <attacker> <target> {pierce:5,slash:3}          deal explicit per-type raw damage (the 'inverse')
       /attack <attacker> <target> <number>                    use <number> as the attacker's effective attack
       /attack <attacker> list                                 show equipped hands + wieldable inventory
    Damage runs through combat.csv + combat_rules.csv against the target's defenses, then is
    subtracted from health; a lethal hit triggers the execution_check (see _lethal)."""
    atk_sel, after = _split_selector(rest)
    attackers = _resolved(atk_sel)
    if not attackers:
        print("  (no attacker matched)")
        return False
    attacker_name, attacker = attackers[0]
    after = after.strip()
    if after == "list" or not after:
        hands = attacker.equipment.equipped("main_hand") + attacker.equipment.equipped("off_hand")
        print(f"  {attacker_name} hands: " + (", ".join(f"{it.type_id}(wield:{it.nbt.get('wield')})" for it in hands) or "empty"))
        wieldable = [it for it in attacker.inventory if it.nbt.get("wield")]
        for it in wieldable:
            print(f"  inventory: {it.type_id} (wield:{it.nbt.get('wield')}) stats={_weapon_stats(it) or '{}'}")
        if not after:
            print("  usage: /attack <attacker> <target> [with <weapon>[+<weapon2>] | {type:amount} | <number>]   (no spec = equipped hands)")
        return True
    tgt_sel, spec = _split_selector(after)
    targets = _resolved(tgt_sel)
    if targets is None:
        return False
    if not targets:
        print("  (no target matched)")
        return False
    spec = spec.strip()
    explicit, weapons, attack_override = None, [], None
    if spec.startswith("with "):
        names = [n.strip() for n in spec[len("with "):].split("+") if n.strip()]
        for wname in names:
            weapon = attacker.inventory.get(wname)
            if weapon is None:
                print(f"  {attacker_name} has no '{wname}' to wield")
                return False
            weapons.append(weapon)
        error = _validate_wield(weapons)
        if error:
            print(f"  bad wield combo: {error}")
            return False
    elif spec.startswith("{"):
        _, explicit, _ = parse_item_text("_" + spec)
    elif spec:
        try:
            attack_override = float(spec)
        except ValueError:
            print("  spec must be: with <weapon> | {type:amount,...} | <number>")
            return False
    else:  # no spec -> use the attacker's EQUIPPED hands (main_hand + off_hand)
        weapons = attacker.equipment.equipped("main_hand") + attacker.equipment.equipped("off_hand")
        if not weapons:
            print(f"  {attacker_name} has nothing equipped in hand — equip a weapon, or pass {{type:amount}} or a number")
            return False
    eff = _effective_attacker(attacker, weapons, attack_override)
    reach = float(eff.stat.get("reach") or 0)  # base + wielded weapon's reach
    wpn_label = ("+".join(w.type_id for w in weapons) if weapons else
                 ("{...}" if explicit is not None else f"attack {attack_override}"))
    for target_name, target in list(targets):
        gap = _distance(attacker, target)  # Chebyshev; None if either side is unpositioned
        if gap is not None and gap > reach:
            print(f"  {target_name} is out of reach ({gap} tiles away, reach {reach:g}) — move closer")
            continue
        total, breakdown = _resolve_damage(eff, target, explicit)
        target.stat.set("health", (target.stat.get("health") or 0) - total)
        target.recompute()
        print(f"  {attacker_name} attacks {target_name} with {wpn_label}: {round(total, 2)} damage "
              f"{breakdown} -> {target_name}.health = {round(target.stat.get('health') or 0, 2)}")
        if (target.stat.get("health") or 0) <= 0:
            _lethal(attacker_name, attacker, target_name, target)
    return True


# --- /move (grid movement, speed-gated, Chebyshev) ---------------------------

def cmd_move(rest):
    """/move <selector> <x> <y>   WALK to a cell one tile at a time (diagonal-first, so it reads as the
    shortest path), gated by the mover's `speed` (Chebyshev distance ≤ speed). The mover reveals/dims
    every tile along the way (per their reveal_radius/dim_radius). The FIRST placement (no current pos)
    just drops them on the tile. GM teleport with NO walk / NO speed check: /map pos <player> <x> <y>."""
    sel, remainder = _split_selector(rest)
    targets = _resolved(sel)
    if targets is None:
        return False
    nums = [int(t) for t in remainder.split() if t.lstrip("-").isdigit()]
    if len(nums) < 2:
        print("  usage: /move <selector> <x> <y>")
        return False
    x, y = nums[0], nums[1]
    for name, mover in targets:
        cur = _entity_pos(mover)
        is_player = not _is_mob(mover)
        reveal_r, dim_r = _vision_radii(mover)
        if cur is None:                                  # first placement: just drop onto the tile
            _set_entity_pos(mover, x, y)
            mover.recompute()
            if is_player:
                _mark_vision(name, x, y, reveal_r, dim_r)
            print(f"  {name} placed at ({x},{y})")
        else:
            step = max(abs(x - cur[0]), abs(y - cur[1]))  # Chebyshev tiles to cover
            speed = _effective_stat(mover, "speed")
            if step > speed:
                print(f"  {name} can't reach ({x},{y}) — {step} tiles away, speed {speed:g}")
                continue
            cx, cy, walked = cur[0], cur[1], 0            # walk one tile per step, diagonal-first
            while (cx, cy) != (x, y):
                cx += (x > cx) - (x < cx)                 # sign(dx): diagonal while both differ, then straight
                cy += (y > cy) - (y < cy)                 # sign(dy)
                _set_entity_pos(mover, cx, cy)
                if is_player:
                    _mark_vision(name, cx, cy, reveal_r, dim_r)
                walked += 1
            mover.recompute()
            print(f"  {name} walks to ({x},{y})  ({walked} tile{'s' if walked != 1 else ''})")
        grew = _auto_expand_near(x, y)
        if grew:
            print(f"    (map auto-expanded {', '.join(grew)} — staying ahead of the edge)")
        _save_map()
    return True


# --- /turn -------------------------------------------------------------------
# TURN is the WORLD turn (drives the calendar); it advances by 1 each time the player turn
# order completes a full cycle. TURN_ACTIVE is the name of the player whose turn is in progress.
# Player turn order = the non-NPC players in creation order (WORLD is insertion-ordered).
TURN = 0
TURN_ACTIVE = None


def _log(event):
    """Append an event to the session HISTORY, stamped with the world turn and whose turn is active
    at that moment, e.g. '[turn 3 · noael] /attack noael goblin'. (TURN/TURN_ACTIVE resolved live.)"""
    HISTORY.append(f"[turn {TURN} · {TURN_ACTIVE or 'no active turn'}] {event}")


def _apply_slice(spec, lst):
    """Apply one Python index/slice spec string to a list. '-1' -> [last], ':1' -> first,
    '::3' -> every 3rd, '2' -> [lst[2]]. Returns the selected sublist ([] on a bad spec)."""
    spec = str(spec).strip()
    try:
        if ":" in spec:
            nums = [int(p) if p.strip() else None for p in spec.split(":")]
            return lst[slice(nums[0], nums[1])] if len(nums) == 2 else lst[slice(nums[0], nums[1], nums[2])]
        return [lst[int(spec)]]
    except (ValueError, IndexError):
        return []


def _step_turns(steps, total):
    """The set of 1-based turn positions an effect fires on. No steps -> every turn."""
    turns = list(range(1, total + 1))
    if not steps:
        return set(turns)
    selected = set()
    for spec in steps:
        selected.update(_apply_slice(spec, turns))
    return selected


def _fire_effect(player, effect):
    """An effect fires this turn: PULSE its proc signals (so talents keyed on them react) and
    apply its reaction (@s = the afflicted player)."""
    changes = []
    procs = effect.field_value("proc") or []
    if procs:
        for tname, _tchanges in _pulse_procs(player, procs):
            changes.append(tname)
    reaction = effect.field_value("reaction")
    if isinstance(reaction, Reaction) and reaction.actions:
        changes += _apply_reaction(player, reaction)
    detail = f" ({', '.join(str(c) for c in changes)})" if changes else ""
    print(f"  [turn {TURN}] {player.name}: effect {effect.name} fired{detail}")


def _fire_element_hook(player, element, hook):
    """An element was gained/lost: fire its gain/lose HOOKS through the bus. (The legacy on_apply/
    on_clear Reaction fields were migrated onto hooks, so the bespoke firing here is gone.)"""
    event = {"on_apply": "gain", "on_clear": "lose"}.get(hook, hook)
    _report_fired(player.name, _emit(player, event, origin=player, subject=element))


def _clear_effect(player, effect):
    """The single effect-removal path: remove it, THEN fire on_clear (remove-first so an on_clear that
    re-pulses procs can't re-trigger this same effect's clear)."""
    if effect not in list(player.effect):
        return
    player.effect.remove(effect)
    _fire_element_hook(player, effect, "on_clear")


def _check_effect_clears(player):
    """Clear any effect whose `clear_when` Proc matches the player's current proc-state (then on_clear
    fires). Called wherever proc-state changes — so a `/proc` pulse/enable ends the effect at once."""
    for effect in list(player.effect):
        cw = effect.field_value("clear_when")
        if isinstance(cw, Proc) and cw.clauses and cw.matches(player.proc_state):
            _clear_effect(player, effect)


def _tick_effects(player):
    """One turn passes for each effect: if this is a firing turn (per its step schedule), fire
    it (pulse proc + reaction); then count duration down and remove at 0. Effects with neither
    a proc nor a reaction just tick silently."""
    for effect in list(player.effect):
        duration = effect.field_value("duration")
        if not isinstance(duration, (int, float)) or isinstance(duration, bool):
            continue  # no valid duration -> inert (doesn't tick or fire)
        if effect._total is None:
            effect._total = int(duration)
        reaction = effect.field_value("reaction")
        has_action = bool(effect.field_value("proc")) or (isinstance(reaction, Reaction) and bool(reaction.actions))
        position = effect._elapsed + 1  # 1-based: which turn of its life this is
        if has_action and position in _step_turns(effect.field_value("step"), effect._total):
            _fire_effect(player, effect)
        effect._elapsed += 1
        new_duration = int(duration) - 1
        if new_duration <= 0:
            _clear_effect(player, effect)   # fires on_clear, then removes
        else:
            effect.modify(duration=new_duration)


def _tick_cooldowns(player):
    """One turn passes: count each ability's cooldown down toward 0 (ready)."""
    for ability in player.ability:
        if isinstance(ability.cooldown, Cooldown):
            ability.cooldown.tick()


def _regen(player):
    """One turn passes: health/mana gain their regen, capped at the max_ ceiling.
    Skips a stat that has its own formula — that formula owns it (the default health/mana
    formulas in formulas.csv already fold regen + the max_ clamp in, so this is the fallback
    for characters whose health/mana formula has been removed via /formula)."""
    for live, regen, ceiling_key in (("health", "health_regen", "max_health"),
                                     ("mana", "mana_regen", "max_mana")):
        if live in player.formula:
            continue
        current = player.stat.get(live)
        if current is None:
            continue
        ceiling = player.stat.get(ceiling_key)
        new_value = current + (player.stat.get(regen) or 0)
        if ceiling is not None and new_value > ceiling:
            new_value = ceiling
        player.stat.set(live, new_value)


def _turn_order():
    """The player turn order: non-NPC characters in creation order (WORLD is insertion-ordered)."""
    return [name for name, player in WORLD.items() if not _is_npc(player)]


def _process_turn_start(name):
    """Begin a player's turn: tick their cooldowns/effects, bump their personal stats.turn,
    regen, recompute formulas, then fire 'turn_start' procs/talents."""
    player = WORLD.get(name)
    if player is None:
        return
    _tick_cooldowns(player)
    _tick_effects(player)
    _tick_quests(player)
    player.stat.set("turn", (player.stat.get("turn") or 0) + 1)  # this player's own turn count
    _regen(player)
    player.recompute()  # derived stats + DELTA-based per-turn formulas
    for talent_name, changes in _pulse_procs(player, ["turn_start"]):
        detail = f" ({', '.join(changes)})" if changes else ""
        print(f"    {name} turn_start: {talent_name} procced{detail}")


def _process_turn_end(name):
    """End a player's turn: fire 'turn_end' procs/talents."""
    player = WORLD.get(name)
    if player is None:
        return
    for talent_name, changes in _pulse_procs(player, ["turn_end"]):
        detail = f" ({', '.join(changes)})" if changes else ""
        print(f"    {name} turn_end: {talent_name} procced{detail}")


def _next_player_turn():
    """End the current player's turn and begin the next's. When the order wraps back to index 0,
    the WORLD turn (and calendar) advances. Returns the new active player's name, or None."""
    global TURN, TURN_ACTIVE
    order = _turn_order()
    if not order:
        print("  no players in the turn order (create a non-NPC character first)")
        return None
    if TURN_ACTIVE not in order:  # start of play (or the active player is gone): begin the first
        TURN_ACTIVE = order[0]
        _process_turn_start(TURN_ACTIVE)
        return TURN_ACTIVE
    idx = order.index(TURN_ACTIVE)
    _process_turn_end(TURN_ACTIVE)
    nxt = (idx + 1) % len(order)
    if nxt == 0:  # cycled through everyone -> world time advances
        TURN += 1
        _log(f"=== world turn {TURN} — {_format_date(TURN).strip()} ===")  # cycle boundary
        print(f"  === cycle complete — world turn {TURN}: {_format_date(TURN).strip()} ===")
        _fire_due_events(TURN)  # any queued world events whose turn arrived
    TURN_ACTIVE = order[nxt]
    _log(f"--- {TURN_ACTIVE}'s turn ---")  # player-turn boundary (now that TURN_ACTIVE is updated)
    _process_turn_start(TURN_ACTIVE)
    return TURN_ACTIVE


def cmd_turn(rest):
    """/turn                     whose turn it is + the world turn
       /turn next                end the current player's turn, begin the next (world turn ticks on wrap)
       /turn next <selector>     advance just that player's turn (their stats.turn), out of order
       /turn set <n> | add <n>   move the WORLD turn directly (for the calendar; no player ticking)"""
    global TURN, TURN_ACTIVE
    sub, _, arg = rest.partition(" ")
    sub, arg = sub.strip(), arg.strip()
    if sub in ("", "query"):
        order = _turn_order()
        active = TURN_ACTIVE if TURN_ACTIVE in order else "(none yet)"
        print(f"  world turn {TURN}  |  active: {active}  |  order: {', '.join(order) or '(no players)'}")
        return TURN
    if sub == "next":
        if not arg:
            name = _next_player_turn()
            if name:
                print(f"  now {name}'s turn  (their stats.turn = {WORLD[name].stat.get('turn')})")
            return TURN
        targets = _resolved(arg)  # /turn next <selector>: just advance those players' own turns
        if targets is None:
            return False
        for name, player in targets:
            _process_turn_start(name)
            print(f"  {name}: advanced their turn (stats.turn = {player.stat.get('turn')})")
        return TURN
    if sub in ("add", "set"):
        try:
            value = int(arg.split()[0])
        except (ValueError, IndexError):
            print(f"  usage: /turn {sub} <value>   (sets the WORLD turn directly; no player ticking)")
            return False
        TURN = max(0, TURN + value if sub == "add" else value)
        print(f"  world turn -> {TURN}: {_format_date(TURN).strip()}")
        _fire_due_events(TURN)  # fire any queued events now due
        return TURN
    print("  usage: /turn [query] | next [<selector>] | add <n> | set <n>")
    return False


# --- /calendar (date/time derived from the world turn) -----------------------
# Iteration 1: the date model + display. The clock derives from the global world TURN
# (TURNS_PER_DAY turns = 1 day). Calendar: 13 months of 26 days (the 13th is 'Sol'), then a
# 14th month 'Lanus' = New Year, 1 day (2 on a leap year). Leap = every 4th year.
# Coming later: a visual box grid, weather (lingering, from a CSV), and event scheduling.
TURNS_PER_DAY = 8          # how many turns make a day (provisional; user may retune)
DAYS_PER_MONTH = 26
REGULAR_MONTHS = 13        # months 1..13 each have DAYS_PER_MONTH days (13th = Sol)
MONTH_NAMES = {1: "January", 2: "February", 3: "March", 4: "April", 5: "May", 6: "June",
               7: "July", 8: "August", 9: "September", 10: "October", 11: "November", 12: "December",
               13: "Sol", 14: "Lanus"}  # 13th = Sol, 14th = Lanus (New Year, 1 day; 2 on a leap year)


def _is_leap(year):
    return year % 4 == 0


def _lanus_days(year):
    return 2 if _is_leap(year) else 1


def _year_length(year):
    return REGULAR_MONTHS * DAYS_PER_MONTH + _lanus_days(year)


def _date_from_turn(turn):
    """(year, month, day, time_of_day, day_of_year) from a world-turn count. All 1-based except
    time_of_day (0..TURNS_PER_DAY-1). Year/month/day start at 1 on turn 0."""
    total_days, time_of_day = divmod(max(0, turn), TURNS_PER_DAY)
    year = 1
    while total_days >= _year_length(year):
        total_days -= _year_length(year)
        year += 1
    day_of_year = total_days  # 0-based within the year
    doy = total_days
    for month in range(1, REGULAR_MONTHS + 1):
        if doy < DAYS_PER_MONTH:
            return year, month, doy + 1, time_of_day, day_of_year + 1
        doy -= DAYS_PER_MONTH
    return year, REGULAR_MONTHS + 1, doy + 1, time_of_day, day_of_year + 1  # Lanus


def _month_name(month):
    return MONTH_NAMES.get(month, f"Month {month}")


def _format_date(turn):
    year, month, day, tod, doy = _date_from_turn(turn)
    name = _month_name(month)
    tag = ""
    if month == REGULAR_MONTHS + 1:  # Lanus
        tag = "  — New Year!" + ("  (leap)" if _lanus_days(year) == 2 else "")
    return (f"  Year {year}, {name}, Day {day}   |   time {tod + 1}/{TURNS_PER_DAY} of the day"
            f"   |   day {doy}/{_year_length(year)}   [world turn {turn}]{tag}")


def _month_length(year, month):
    return _lanus_days(year) if month == REGULAR_MONTHS + 1 else DAYS_PER_MONTH


def _turn_from_date(year, month, day, tod=0):
    """Inverse of _date_from_turn: a (year, month, day, time) -> world turn count."""
    days = sum(_year_length(y) for y in range(1, max(1, year)))
    days += (month - 1) * DAYS_PER_MONTH if month <= REGULAR_MONTHS else REGULAR_MONTHS * DAYS_PER_MONTH
    days += day - 1
    return max(0, days * TURNS_PER_DAY + tod)


def _parse_date_to_turn(text):
    """A date 'Y/M/D' or 'M/D' (current year) -> absolute world turn (day start). None if not a date."""
    m = re.fullmatch(r"\s*(?:(\d+)/)?(\d+)/(\d+)\s*", str(text))
    if not m:
        return None
    year = int(m.group(1)) if m.group(1) else _date_from_turn(TURN)[0]
    return _turn_from_date(year, int(m.group(2)), int(m.group(3)))


def _resolve_expire_on(value):
    """A quest's expire_on -> an absolute world turn. Accepts an int turn, an int-string, or a date
    'Y/M/D'/'M/D'. Returns None if it isn't resolvable."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    s = str(value).strip()
    return int(s) if re.fullmatch(r"-?\d+", s) else _parse_date_to_turn(s)


# Scheduled calendar notes (quests, reminders). Each: {year, month, day, end, turn, text}.
# Terminal can't truly "hover", so the grid marks event-days with * and lists them underneath.
CAL_EVENTS = []


def _events_for(year, month):
    return [e for e in CAL_EVENTS if e["year"] == year and e["month"] == month]


CAL_CELL_W = 9    # inner width of a day box
CAL_CELL_H = 2    # content rows per day box (under the day-number border) — room for events


def _cal_day_box(d, day, events_by_day):
    """The box-drawn lines (CAL_CELL_H + 2 tall) for one calendar day: the day number sits in the
    top border (prefixed ◆ when it's the current/previewed day), then content rows for events."""
    label = ("◆" if d == day else "") + str(d)
    top = "╔" + label + "═" * (CAL_CELL_W - len(label)) + "╗"
    bottom = "╚" + "═" * CAL_CELL_W + "╝"
    evs = events_by_day.get(d, [])
    rows = []
    if evs:
        rows.append("*" + evs[0])           # first event (truncated to fit)
        if len(evs) > 1:
            rows.append(f"+{len(evs) - 1} more")
    lines = [top]
    for r in range(CAL_CELL_H):
        text = (rows[r] if r < len(rows) else "")[:CAL_CELL_W].ljust(CAL_CELL_W)
        lines.append("║" + text + "║")
    return lines + [bottom]


def _calendar_grid(turn, width=7):
    """A box grid of a month's days: each day is a ╔═╗ cell, the current day's number is marked ◆,
    event-days carry a snippet inside the box, and full event text is listed below."""
    year, month, day, tod, _doy = _date_from_turn(turn)
    length = _month_length(year, month)
    is_now = (turn == TURN)
    weather = (_weather_now()[0] if is_now else None)
    head = f"  {_month_name(month)} — Year {year}   (day {day}/{length}, time {tod + 1}/{TURNS_PER_DAY}"
    head += f", weather: {weather})" if weather else ")"
    events = _events_for(year, month)
    events_by_day = {}
    for e in sorted(events, key=lambda x: (x["day"], x["turn"] or 0)):
        for d in range(e["day"], (e["end"] or e["day"]) + 1):
            events_by_day.setdefault(d, []).append(e["text"])
    lines, week = [head], []
    for d in range(1, length + 1):
        week.append(_cal_day_box(d, day, events_by_day))
        if len(week) == width:
            for r in range(CAL_CELL_H + 2):
                lines.append("  " + "".join(box[r] for box in week))
            week = []
    if week:
        for r in range(CAL_CELL_H + 2):
            lines.append("  " + "".join(box[r] for box in week))
    for e in sorted(events, key=lambda x: (x["day"], x["turn"] or 0)):
        span = f"day {e['day']}" + (f"-{e['end']}" if e["end"] else "") + (f" turn {e['turn']}" if e["turn"] else "")
        lines.append(f"    * {span}: {e['text']}")
    return "\n".join(lines)


def _calendar_year(turn):
    """STRUCTURED data for the calendar UI (a pure renderer): the WHOLE current year — all 14 months
    (so the zoom-out shows everything and Lanus's leap length is visible) — with each day's manual
    notes + computed effect/quest EXPIRIES placed at their exact turn-of-day (0-based; UI shows N+1),
    plus where each player sits in the turn cycle (their stats.turn)."""
    _ensure_cal_ids()
    year, cur_month, today, tod, _ = _date_from_turn(turn)
    months = {}
    for m in range(1, REGULAR_MONTHS + 2):                   # 1..14 (14 = Lanus)
        length = _month_length(year, m)
        months[m] = {"month": m, "name": _month_name(m), "length": length,
                     "days": {d: {"day": d, "notes": [], "effects": [], "quests": []} for d in range(1, length + 1)}}
    for e in (ev for ev in CAL_EVENTS if ev["year"] == year):   # manual /calendar event notes, any month
        m = e["month"]
        if m in months:
            for d in range(e["day"], (e["end"] or e["day"]) + 1):
                if d in months[m]["days"]:
                    months[m]["days"][d]["notes"].append({"id": e["id"], "text": e["text"], "turn": e["turn"]})
    def _place(bucket, end_turn, text):
        ey, em, ed, etod, _ = _date_from_turn(end_turn)
        if ey == year and em in months and ed in months[em]["days"]:
            months[em]["days"][ed][bucket].append({"text": text, "turn": etod})
    for name, p in WORLD.items():
        for eff in getattr(p, "effect", []):                 # effects: remaining = (started total or duration) - elapsed
            total = eff._total if getattr(eff, "_total", None) is not None else getattr(eff, "duration", None)
            if isinstance(total, (int, float)) and total:
                _place("effects", turn + max(0, int(total - getattr(eff, "_elapsed", 0))), f"{eff.name} · {name}")
        for q in getattr(p, "quest", []):                    # quests: turns-countdown OR an absolute end date
            exp = getattr(q, "expiration", None)              # (proc-based expire_when has no date -> not placed)
            if isinstance(exp, (int, float)) and not isinstance(exp, bool):
                _place("quests", turn + max(0, int(exp)), f"{q.name} · {name}")
            else:
                target = _resolve_expire_on(getattr(q, "expire_on", None))
                if target is not None:
                    _place("quests", target, f"{q.name} · {name}")
    players = []
    for name, p in WORLD.items():
        try:
            t = int(p.stat.get("turn") or 0)
        except (TypeError, ValueError):
            t = 0
        # turn N of the day -> slot N-1 (so turns 1..8 fill slots 0..7; turn 9 wraps to slot 0 of the
        # NEXT day, not back to the same day). t==0 (hasn't acted yet) sits at the first slot.
        players.append({"name": name, "symbol": _entity_symbol(p, name[:1]),
                        "turn_of_day": (t - 1) % TURNS_PER_DAY if t > 0 else 0})
    return {"year": year, "turns_per_day": TURNS_PER_DAY, "current_month": cur_month, "today": today,
            "tod": tod, "leap": _is_leap(year),
            "months": [{"month": months[m]["month"], "name": months[m]["name"], "length": months[m]["length"],
                        "days": [months[m]["days"][d] for d in range(1, months[m]["length"] + 1)]}
                       for m in range(1, REGULAR_MONTHS + 2)],
            "players": players}


# --- weather (lingering, from weather.csv) -----------------------------------
# Weather has momentum: each day it tends to PERSIST (per the event's persistence), else it
# transitions to a weighted-random event — so it lingers and shifts rather than being an
# independent per-day dice roll. Forced/queued events (via /event) override the roll.
def _load_weather(path="weather.csv"):
    table = []
    try:
        with open(path) as handle:
            for row in csv.DictReader(handle):
                trans_text = (row.get("transitions") or "").strip()
                try:  # transitions = a nested {next_weather:weight,...} map; empty {} = use base weights
                    transitions = parse_item_text("_" + trans_text)[1] if trans_text.startswith("{") else {}
                except ValueError:
                    transitions = {}
                table.append({"weather": row["weather"].strip(),
                              "weight": float(row.get("weight") or 1),
                              "persistence": float(row.get("persistence") or 0),
                              "description": (row.get("description") or "").strip(),
                              "transitions": transitions})
    except (OSError, ValueError, KeyError):
        pass
    return table


WEATHER_TABLE = _load_weather()
WEATHER_TURN = -1           # the world TURN WEATHER_BY_UNIT reflects (-1 = uninitialized)
WEATHER_BY_UNIT = {}        # weather-unit key -> weather for WEATHER_TURN (regional; see _cell_unit)
WEATHER_QUEUE = {}          # (day, unit) -> forced weather (set via /event)
_WEATHER_SYNCED_CELLS = -1  # cell-count WEATHER_BY_UNIT was last seeded for (skip the re-seed when unchanged)


def _weather_entry(name):
    return next((w for w in WEATHER_TABLE if w["weather"] == name), None)


def _roll_next_weather(previous):
    """Pick the next day's weather: keep `previous` with its persistence, else weighted-random.
    If `previous` defines transition weights ({next:weight} in weather.csv) those bias the pick;
    otherwise the base `weight` column is used."""
    if not WEATHER_TABLE:
        return previous
    entry = _weather_entry(previous) if previous else None
    if entry and random.random() < entry["persistence"]:
        return previous  # lingers another day
    transitions = entry.get("transitions") if entry else None
    if transitions:  # per-weather transition weights {next_weather: weight}
        pool = [(name, float(wt)) for name, wt in transitions.items()]
    else:            # default: the base weight column
        pool = [(w["weather"], w["weight"]) for w in WEATHER_TABLE]
    total = sum(wt for _, wt in pool)
    if total <= 0:
        return previous or (WEATHER_TABLE[0]["weather"] if WEATHER_TABLE else None)
    roll, acc = random.uniform(0, total), 0
    for name, wt in pool:
        acc += wt
        if roll <= acc:
            return name
    return pool[-1][0]


# --- regional weather: each cell belongs to a UNIT (its named region, else itself). Region
# units roll as one block (a whole kingdom shares weather); standalone cells are neighbor-biased
# + lingering, so a front drifts across a big biome and you can out-walk it.
def _cell_unit(x, y):
    """The weather unit a cell belongs to: 'region:<name>' if the cell has a region (all its
    cells share weather), else 'cell:x,y' (per-cell)."""
    cell = MAP["cells"].get(f"{x},{y}")
    if cell and cell.get("region"):
        return f"region:{cell['region']}"
    return f"cell:{x},{y}"


def _all_units():
    return {_cell_unit(*(int(n) for n in k.split(","))) for k in MAP["cells"]}


def _roll_cell_weather(x, y, prev):
    """A standalone cell's next weather: heavily favors its own yesterday (linger) and its
    neighbors' yesterday (clump), else a fresh weighted roll."""
    own = prev.get(_cell_unit(x, y))
    pool = [own] * 4 if own else []
    for dx, dy in ((-1, 0), (1, 0), (0, -1), (0, 1)):
        neighbor = prev.get(_cell_unit(x + dx, y + dy))
        if neighbor:
            pool.append(neighbor)
    if not pool or random.random() < 0.2:
        return _roll_next_weather(own)
    return random.choice(pool)


def _step_weather(prev):
    """Next day's weather for every unit, given the previous day's map."""
    new = {}
    for unit in _all_units():
        if unit.startswith("region:"):
            new[unit] = _roll_next_weather(prev.get(unit))  # a region rolls as one block
        else:
            x, y = (int(n) for n in unit[len("cell:"):].split(","))
            new[unit] = _roll_cell_weather(x, y, prev)
    return new


def _sync_weather(turn):
    """Advance regional weather TURN-by-turn up to `turn` (linger + clump each step). Queued
    forces for (turn, unit) override; newly-added map units get seeded. Idempotent once caught up.
    (Per the user: weather rolls per world-turn, not per day — tune persistence/weights to linger.)"""
    global WEATHER_TURN, WEATHER_BY_UNIT, _WEATHER_SYNCED_CELLS
    ncells = len(MAP["cells"])
    # FAST PATH: already at this turn with no cells added/removed -> nothing to do. Without this the
    # _all_units() re-seed below ran on EVERY _weather_at call, making get_state O(cells^2) (a 60x60
    # map hung the server ~33s). Now _weather_at is O(1) once caught up.
    if WEATHER_TURN == turn and ncells == _WEATHER_SYNCED_CELLS:
        return
    if WEATHER_TURN < 0:
        WEATHER_BY_UNIT = {u: _roll_next_weather(None) for u in _all_units()}
        WEATHER_TURN = turn
        _apply_weather_queue(turn)
        _WEATHER_SYNCED_CELLS = ncells
        return
    while WEATHER_TURN < turn:
        WEATHER_TURN += 1
        WEATHER_BY_UNIT = _step_weather(WEATHER_BY_UNIT)
        _apply_weather_queue(WEATHER_TURN)
    for unit in _all_units():  # seed units added to the map since the last sync
        WEATHER_BY_UNIT.setdefault(unit, _roll_next_weather(None))
    _WEATHER_SYNCED_CELLS = ncells


def _apply_weather_queue(turn):
    for unit in _all_units():
        if (turn, unit) in WEATHER_QUEUE:
            WEATHER_BY_UNIT[unit] = WEATHER_QUEUE[(turn, unit)]


def _weather_at(x, y):
    """Weather (and its description) at a specific cell, for the current world turn."""
    _sync_weather(TURN)
    weather = WEATHER_BY_UNIT.get(_cell_unit(x, y))
    entry = _weather_entry(weather)
    return weather, (entry["description"] if entry else "")


def _weather_now():
    """Weather at the party's current map position (MAP['pos'])."""
    px, py = MAP.get("pos", [0, 0])
    return _weather_at(px, py)


CAL_UNITS = ("time", "day", "month", "year")


def _cal_adjust(unit, value, mode):
    """add/set a calendar component (time/day/month/year), recomputing the world turn.
    'add' rolls over naturally (months wrap into years); 'set' clamps to the valid range."""
    global TURN
    year, month, day, tod, _ = _date_from_turn(TURN)
    if mode == "add":
        if unit == "time":
            TURN = max(0, TURN + value)
        elif unit == "day":
            TURN = max(0, TURN + value * TURNS_PER_DAY)
        elif unit == "month":
            month += value
            while month > REGULAR_MONTHS + 1:
                month -= REGULAR_MONTHS + 1
                year += 1
            while month < 1:
                month += REGULAR_MONTHS + 1
                year -= 1
            year = max(1, year)
            TURN = _turn_from_date(year, month, min(day, _month_length(year, month)), tod)
        elif unit == "year":
            year = max(1, year + value)
            TURN = _turn_from_date(year, month, min(day, _month_length(year, month)), tod)
    else:  # set: clamp the component
        if unit == "time":
            tod = max(0, min(value, TURNS_PER_DAY - 1))
        elif unit == "day":
            day = max(1, min(value, _month_length(year, month)))
        elif unit == "month":
            month = max(1, min(value, REGULAR_MONTHS + 1))
            day = min(day, _month_length(year, month))
        elif unit == "year":
            year = max(1, value)
            day = min(day, _month_length(year, month))
        TURN = _turn_from_date(year, month, day, tod)
    print(f"  {_format_date(TURN).strip()}")
    return TURN


def _ensure_cal_ids():
    """Give every calendar event a stable id (lazy migration — legacy/loaded events may lack one)."""
    nxt = max((e.get("id", 0) for e in CAL_EVENTS), default=0) + 1
    for e in CAL_EVENTS:
        if not e.get("id"):
            e["id"] = nxt
            nxt += 1


def _cal_event(args):
    """/calendar event add <when> <text> | set <id> <text> | remove <id> | list | clear.
    `when` = 12 (day), 12.4 (day.turn), or 12-15 (day range). Defaults to the current year/month."""
    _ensure_cal_ids()
    if not args or args[0] in ("list", "show"):
        if not CAL_EVENTS:
            print("  (no calendar events)")
            return []
        for e in sorted(CAL_EVENTS, key=lambda x: (x["year"], x["month"], x["day"])):
            span = f"day {e['day']}" + (f"-{e['end']}" if e["end"] else "") + (f" turn {e['turn']}" if e["turn"] else "")
            print(f"  [{e['id']}] Year {e['year']} {_month_name(e['month'])} {span}: {e['text']}")
        return list(CAL_EVENTS)
    if args[0] == "clear":
        CAL_EVENTS.clear()
        print("  cleared all calendar events")
        return True
    if args[0] in ("remove", "delete", "rm"):
        if len(args) < 2 or not args[1].isdigit():
            print("  usage: /calendar event remove <id>   (see ids in /calendar event list)")
            return False
        eid = int(args[1])
        match = next((e for e in CAL_EVENTS if e.get("id") == eid), None)
        if match is None:
            print(f"  no calendar event #{eid}")
            return False
        CAL_EVENTS.remove(match)
        print(f"  removed calendar event #{eid}: {match['text']}")
        return True
    if args[0] in ("set", "edit"):
        if len(args) < 3 or not args[1].isdigit():
            print("  usage: /calendar event set <id> <text>")
            return False
        eid = int(args[1])
        match = next((e for e in CAL_EVENTS if e.get("id") == eid), None)
        if match is None:
            print(f"  no calendar event #{eid}")
            return False
        match["text"] = " ".join(args[2:])
        print(f"  calendar event #{eid} -> {match['text']}")
        return True
    if args[0] == "add":
        if len(args) < 3:
            print("  usage: /calendar event add <when> <text>   (schedules in the CURRENT month/year)")
            print("    <when>:  12      -> day 12")
            print("             12.4    -> day 12, turn 4 of that day")
            print("             12-15   -> spanning days 12 through 15")
            print("    e.g.  /calendar event add 12.4 quest deadline")
            return False
        when, text = args[1], " ".join(args[2:])
        year, month = _date_from_turn(TURN)[:2]
        if "/" in when:                            # optional "month/day..." prefix targets a month of the current year
            mstr, when = when.split("/", 1)
            if mstr.lstrip("-").isdigit():
                month = max(1, min(int(mstr), REGULAR_MONTHS + 1))
        next_id = max((e.get("id", 0) for e in CAL_EVENTS), default=0) + 1
        event = {"id": next_id, "year": year, "month": month, "day": 1, "end": None, "turn": None, "text": text}
        try:
            if "-" in when:
                start, end = when.split("-", 1)
                event["day"], event["end"] = int(start), int(end)
            elif "." in when:
                d, t = when.split(".", 1)
                event["day"], event["turn"] = int(d), int(t)
            else:
                event["day"] = int(when)
        except ValueError:
            print("  bad <when>; use 12 (day), 12.4 (day.turn), or 12-15 (range)")
            return False
        CAL_EVENTS.append(event)
        print(f"  scheduled in {_month_name(month)} Year {year}: {text}")
        return True
    print("  usage: /calendar event <add|set|remove|list|clear> ...")
    return False


def cmd_calendar(rest):
    """/calendar [show]              box grid of the current month (date, time, weather, events)
       /calendar show at <turn>      grid for a given world turn
       /calendar add|set <time|day|month|year> <value>   move the calendar
       /calendar event add <when> <text> | list | clear  schedule notes"""
    parts = rest.split()
    sub = parts[0] if parts else "show"
    if sub in ("show", "grid", "now"):
        if "at" in parts:
            try:
                turn = int(parts[parts.index("at") + 1])
            except (ValueError, IndexError):
                print("  usage: /calendar show at <world-turn>")
                return False
            print(_calendar_grid(turn))
            return True
        print(_calendar_grid(TURN))
        return True
    if sub in ("add", "set"):
        if len(parts) < 3 or parts[1] not in CAL_UNITS or not parts[2].lstrip("-").isdigit():
            print(f"  usage: /calendar {sub} <time|day|month|year> <value>")
            return False
        return _cal_adjust(parts[1], int(parts[2]), sub)
    if sub == "event":
        return _cal_event(parts[1:])
    if sub == "date":  # the old one-line text view, still handy
        print(_format_date(TURN))
        return _date_from_turn(TURN)[:3]
    print("  usage: /calendar [show] | show at <turn> | add|set <unit> <n> | event ... | date")
    return False


# --- /event (categorized world events: weather, festival, seasonal, quest) ---
# Offsets are in WORLD TURNS (0 = now/force, 1 = next turn, ...). Weather events set the regional
# weather (per cell/region, location optional → current pos). Other categories run an ACTION
# command when they fire — typically /tellraw (announce) or /function.
EVENT_CATEGORIES = ["weather", "festival", "seasonal", "quest"]
EVENT_QUEUE = []  # non-weather: list of {turn, category, action}; action is a command string


def _peel_at(parts):
    """Peel an optional 'at <x> <y>' from a token list → ((x,y) or None, remaining parts)."""
    if "at" in parts:
        i = parts.index("at")
        if i + 2 < len(parts) and parts[i + 1].lstrip("-").isdigit() and parts[i + 2].lstrip("-").isdigit():
            return (int(parts[i + 1]), int(parts[i + 2])), parts[:i] + parts[i + 3:]
        return False, parts  # malformed
    return None, parts


def _fire_due_events(turn):
    """Run (and remove) any queued non-weather events whose fire-turn has arrived."""
    due = [e for e in EVENT_QUEUE if e["turn"] <= turn]
    for event in due:
        EVENT_QUEUE.remove(event)
        print(f"  [{event['category']} event @turn {turn}]")
        dispatch(_as_command_line(event["action"]))


def cmd_event(rest):
    """/event list [category]                 show queued events (all, or one category)
       /event clear [category]                clear queued events (all, or one category)
       /event queue <category> <event...> <N> [at <x> <y>]   schedule N world-turns out (0 = now)
       /event force <category> <event...> [at <x> <y>]       fire now (same as queue ... 0)
    weather: <event> is a weather name (set regionally). Other categories: <event> is a command
    that runs when it fires — e.g. /event force festival "/tellraw &6Harvest Fair!\\nAll welcome"."""
    _sync_weather(TURN)
    parts = rest.split()
    sub = parts[0] if parts else "list"
    if sub == "list":
        category = parts[1] if len(parts) > 1 else None
        weathered = (not category or category == "weather")
        if weathered:
            for (qt, qu), qw in sorted(WEATHER_QUEUE.items()):
                print(f"  turn {qt}  weather {qu}: {qw}")
        for event in sorted(EVENT_QUEUE, key=lambda e: e["turn"]):
            if not category or event["category"] == category:
                print(f"  turn {event['turn']}  {event['category']}: {event['action']}")
        if not WEATHER_QUEUE and not EVENT_QUEUE:
            print("  (no queued events)")
        return True
    if sub == "clear":
        category = parts[1] if len(parts) > 1 else None
        if not category or category == "weather":
            WEATHER_QUEUE.clear()
        EVENT_QUEUE[:] = [e for e in EVENT_QUEUE if category and e["category"] != category]
        print(f"  cleared {category or 'all'} events")
        return True
    if sub in ("queue", "force"):
        loc, args = _peel_at(parts[1:])
        if loc is False:
            print("  usage: ... at <x> <y>")
            return False
        if sub == "queue":
            if len(args) < 3 or not args[-1].lstrip("-").isdigit():
                print("  usage: /event queue <category> <event...> <N>   (N = world-turns out, 0 = now)")
                return False
            offset, args = max(0, int(args[-1])), args[:-1]
        else:  # force = now
            offset = 0
        if len(args) < 2:
            print(f"  usage: /event {sub} <category> <event...>" + (" <N>" if sub == "queue" else ""))
            return False
        category, action = args[0], " ".join(args[1:])
        if len(action) >= 2 and action[0] in "\"'" and action[-1] == action[0]:
            action = action[1:-1]  # a quoted "/tellraw ..." action
        fire_turn = TURN + offset
        if category == "weather":
            unit = _cell_unit(*(loc or tuple(MAP.get("pos", [0, 0]))))
            if offset == 0:
                WEATHER_BY_UNIT[unit] = action
                print(f"  forced weather '{action}' now at {unit}")
            else:
                WEATHER_QUEUE[(fire_turn, unit)] = action
                print(f"  queued weather '{action}' for turn {fire_turn} at {unit}")
        else:
            if offset == 0:
                print(f"  [{category} event now]")
                dispatch(_as_command_line(action))
            else:
                EVENT_QUEUE.append({"turn": fire_turn, "category": category, "action": action})
                print(f"  queued {category} for turn {fire_turn}: {action}")
        return True
    print("  usage: /event <list|clear|queue|force> ...")
    return False


# --- /map (a grid of biome cells; cells tie to item/character {origin}) ------
# Stored standalone in map.json (JSON so a cell can grow nested info later). A cell is keyed
# "x,y" and holds at least a biome + name; the name is what {origin:""} references (a village,
# region, kingdom...). Biomes are CONTINUOUS, so /map recommend suggests by neighbors.
MAP_FILE = "map.json"


def _load_map(path=MAP_FILE):
    default = {"cells": {"0,0": {"biome": "plains", "name": ""}, "1,0": {"biome": "plains", "name": ""},
                         "0,1": {"biome": "forest", "name": ""}, "1,1": {"biome": "forest", "name": ""}}}
    try:
        with open(path) as handle:
            data = json.load(handle)
            if isinstance(data.get("cells"), dict):
                default = data
    except (OSError, ValueError):
        pass
    default.setdefault("pos", [0, 0])  # the party's current cell (marker + future weather anchor)
    return default


def _load_biomes(path="biomes.json"):
    """biome name -> metadata dict {label, color, glyph, weight, temperature, moisture, affinity}.
    Drives the atlas (color/glyph), generation (weight + affinity continuity), and weather bias
    (temperature/moisture). Top-level keys starting with '_' (e.g. notes) are skipped. User-extendable."""
    try:
        with open(path) as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: v for k, v in data.items() if not k.startswith("_") and isinstance(v, dict)}


MAP = _load_map()
BIOMES = _load_biomes()


def _save_map():
    _ensure_session_uuids()  # so freshly-created structures are saved WITH their uuid
    MAP["structures"] = [s.as_dict() for s in STRUCTURES]  # persist structures alongside cells
    with open(MAP_FILE, "w") as handle:
        json.dump(MAP, handle, indent=2)


def _load_structures():
    """Rebuild Structure objects from the map file's serialized 'structures' list."""
    out = []
    for entry in MAP.get("structures", []):
        if isinstance(entry, dict) and entry.get("name"):
            nbt = {k: v for k, v in entry.items() if k != "name"}
            out.append(Structure.from_nbt(entry["name"], nbt))
    return out


STRUCTURES = _load_structures()  # all map structures (global; each carries its own pos:[x,y])


# --- UUIDs (per-session unique ids: <2-letter type>-<4 base62>, e.g. it-aB39) ----------------
# Every initiated entity carries a uuid in its nbt under 'uuid'. Format: a 2-letter TYPE code, a
# dash, then 4 base62 chars (A-Z a-z 0-9) = 14,776,336 per type. Unique WITHIN a session (not across
# sessions) by generate-and-check. Stored in nbt so it round-trips through every existing save path;
# excluded from item stack_key (fungible copies still merge) and from manual /modify edits.
UUID_ALPHABET = string.ascii_letters + string.digits  # 62 chars
UUID_TYPE_CODES = {"item": "it", "quest": "qs", "structure": "st",
                   "talent": "tl", "ability": "ab", "effect": "ef"}


def _uuid_type_code(obj):
    """The 2-letter type code for an entity (pl/np/mb for characters by role; else by class).
    Item's type_id is the SPECIFIC type (potion/sword/...), so items are matched by class, not type_id."""
    if isinstance(obj, Mob):
        return "mb"
    if isinstance(obj, Player):
        return "np" if obj.field_value("npc") else "pl"
    if isinstance(obj, Item):
        return "it"
    return UUID_TYPE_CODES.get(getattr(obj, "type_id", ""), "xx")


def _all_entities():
    """Every uuid-bearing object live in the session: characters + their items/elements + structures."""
    for player in WORLD.values():
        yield player
        yield from player.inventory.items
        for items in player.equipment.equipped().values():
            yield from items
        yield from player.talent
        yield from player.ability
        yield from player.effect
        yield from player.quest
    yield from STRUCTURES


def _entity_uuid(obj):
    return obj.nbt.get("uuid") if hasattr(obj, "nbt") else None


def _uuids_in_use():
    return {u for u in (_entity_uuid(e) for e in _all_entities()) if u}


def _mint_uuid(obj, used):
    """A fresh '<code>-<4 base62>' not in `used` (which is updated in place)."""
    code = _uuid_type_code(obj)
    while True:
        uid = code + "-" + "".join(random.choice(UUID_ALPHABET) for _ in range(4))
        if uid not in used:
            used.add(uid)
            return uid


def _ensure_session_uuids():
    """Give a uuid to any live entity that lacks one (idempotent). Run after each command, so
    anything initiated by ANY path ends up with a session-unique id."""
    used = _uuids_in_use()
    for entity in _all_entities():
        if hasattr(entity, "nbt") and not entity.nbt.get("uuid"):
            entity.nbt["uuid"] = _mint_uuid(entity, used)


def _find_by_uuid(uid):
    """The live entity holding `uid`, or None."""
    return next((e for e in _all_entities() if _entity_uuid(e) == uid), None)


def _entity_owner(target):
    """The character that holds `target` (an item/element), or None for characters/structures."""
    for player in WORLD.values():
        contents = (list(player.inventory.items) + list(player.talent) + list(player.ability)
                    + list(player.effect) + list(player.quest))
        for items in player.equipment.equipped().values():
            contents += list(items)
        if target in contents:
            return player
    return None


def _describe_entity(entity):
    """A short human label for /uuid lookup: type, name, and where it lives."""
    code = _uuid_type_code(entity)
    name = getattr(entity, "name", "") or getattr(entity, "type_id", "?")  # type_id when unnamed (items)
    if isinstance(entity, Player):
        return f"{code} {name}"
    if entity in STRUCTURES:
        return f"{code} {name} @ {entity.pos or '(no cell)'}"
    owner = _entity_owner(entity)
    return f"{code} {name}" + (f" (on {owner.name})" if owner else "")


def _structure_matches(struct, ident, filt):
    """A structure matches if its name == ident and every key:value in the nbt filter holds."""
    if struct.name != ident:
        return False
    return all(struct.field_value(key) == value for key, value in filt.items())


def _choose_structure(matches):
    """One match -> return it. Several -> numbered menu; type the number + Enter (blank cancels)."""
    if len(matches) == 1:
        return matches[0]
    print(f"  {len(matches)} structures match — pick one:")
    for i, struct in enumerate(matches, 1):
        print(f"    {i}. {struct}  @ {struct.pos or '(no cell)'}")
    try:
        raw = input("  number (blank = cancel): ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return None
    if not raw.isdigit() or not (1 <= int(raw) <= len(matches)):
        print("  cancelled")
        return None
    return matches[int(raw) - 1]


def _cell_tokens(text):
    """Parse trailing 'x,y x,y ...' coordinate tokens into [[x,y],...]. Returns (cells, bad_token):
    bad_token is the first token that isn't an 'x,y' pair (None if all good)."""
    cells = []
    for tok in text.split():
        match = re.fullmatch(r"(-?\d+),(-?\d+)", tok)
        if match:
            cells.append([int(match.group(1)), int(match.group(2))])
        else:
            return cells, tok
    return cells, None


UUID_PATTERN = re.compile(r"[A-Za-z0-9]{2}-[A-Za-z0-9]{4}")


def cmd_uuid(rest):
    """/uuid get <selector>            show the uuid(s) of matched character(s)
       /uuid lookup <uuid>             what entity holds that uuid
       /uuid set <old-uuid> <new-uuid> change a uuid (if the new one's taken: swap / regen / cancel)
       /uuid list [type]               counts per type, or every uuid of a 2-letter type (it/pl/mb/...)
    Every initiated entity has a session-unique uuid like it-aB39 (2-letter type + 4 base62)."""
    sub, _, arg = rest.partition(" ")
    sub, arg = sub.strip(), arg.strip()
    if sub == "get":
        targets = _resolved(arg) if arg else None
        if not targets:
            print("  usage: /uuid get <selector>")
            return False
        for name, player in targets:
            print(f"  {name}: {_entity_uuid(player) or '(none yet)'}")
        return [_entity_uuid(p) for _n, p in targets]
    if sub == "lookup":
        entity = _find_by_uuid(arg)
        if entity is None:
            print(f"  no entity has uuid '{arg}'")
            return False
        print(f"  {arg} -> {_describe_entity(entity)}")
        return True
    if sub == "list":
        in_use = sorted(_uuids_in_use())
        if arg:  # a 2-letter type filter
            picks = [u for u in in_use if u[:2] == arg]
            print(f"  {arg}: " + (", ".join(picks) if picks else "(none)"))
            return picks
        counts = {}
        for u in in_use:
            counts[u[:2]] = counts.get(u[:2], 0) + 1
        print(f"  {len(in_use)} uuid(s): " + (", ".join(f"{k}={v}" for k, v in sorted(counts.items())) or "(none)"))
        return counts
    if sub == "set":
        old, _, new = arg.partition(" ")
        old, new = old.strip(), new.strip()
        if not old or not new:
            print("  usage: /uuid set <old-uuid> <new-uuid>")
            return False
        if not UUID_PATTERN.fullmatch(new):
            print(f"  '{new}' isn't a valid uuid — use <2 chars>-<4 chars>, e.g. it-aB39")
            return False
        entity = _find_by_uuid(old)
        if entity is None:
            print(f"  no entity has uuid '{old}'")
            return False
        if new == old:
            print("  (unchanged)")
            return True
        clash = _find_by_uuid(new)
        if clash is None:
            entity.nbt["uuid"] = new
            print(f"  {old} -> {new}  ({_describe_entity(entity)})")
            return True
        # new is taken: ask what to do
        print(f"  '{new}' is already used by {_describe_entity(clash)}.")
        choice = _read_choice("  [s = swap the two / r = give that one a new random id / c = cancel]: ")
        if choice == "s":
            entity.nbt["uuid"], clash.nbt["uuid"] = new, old
            print(f"  swapped: {_describe_entity(entity)}={new}, {_describe_entity(clash)}={old}")
            return True
        if choice == "r":
            entity.nbt["uuid"] = new                       # take the wanted id
            clash.nbt["uuid"] = _mint_uuid(clash, _uuids_in_use())  # other one gets a fresh random id
            print(f"  {_describe_entity(entity)}={new}, {_describe_entity(clash)}={clash.nbt['uuid']}")
            return True
        print("  cancelled — nothing changed.")
        return False
    print("  usage: /uuid <get|lookup|set|list> ...")
    return False


def cmd_structure(rest):
    """/structure new <structure>{nbt} [<x,y> <x,y> ...]   place a structure spanning those cells
       /structure delete <structure>{nbt}          remove (menu to pick if several match)
       /structure modify <structure>{nbt} <set|add|reset> <field> [{nbt}|value]
       /structure list
    The structure is always the first arg; any number of x,y cells follow (multi-cell kingdoms etc.).
    Cells can instead be given as pos in the nbt (pos:[[1,2],[2,2]]); positional cells override it.
    Structures are NOT unique — any number can share a name; the {nbt} narrows, a menu disambiguates."""
    sub, _, arg = rest.partition(" ")
    sub, arg = sub.strip(), arg.strip()
    if sub == "list":
        if not STRUCTURES:
            print("  (no structures)")
            return []
        for struct in STRUCTURES:
            print(f"  {struct}  @ {struct.pos or '(no cell)'}")
        return [s.name for s in STRUCTURES]
    if sub == "new":
        if not arg:
            print("  usage: /structure new <structure>{nbt} [<x,y> <x,y> ...]")
            return False
        try:
            ident, nbt, trailing = parse_item_text(arg)  # 'kingdom{nbt} 1,2 2,2' -> ident, nbt, '1,2 2,2'
        except ValueError as error:
            print(f"  parse error: {error}")
            return False
        cells, bad = _cell_tokens(trailing)
        if bad is not None:
            print(f"  bad cell '{bad}' — use x,y (e.g. 1,2).  /structure new <structure>{{nbt}} [<x,y> ...]")
            return False
        if cells:
            nbt["pos"] = cells  # positional cells override any pos: in the nbt
        struct = Structure.from_nbt(ident, nbt)
        STRUCTURES.append(struct)
        _save_map()
        print(f"  created structure {struct} @ {struct.pos or '(no cell)'}")
        return True
    if sub not in ("delete", "modify"):
        print("  usage: /structure <new|delete|modify|list> ...")
        return False
    if not arg:
        print(f"  usage: /structure {sub} <structure>{{nbt}} ...")
        return False
    try:
        ident, filt, ops = parse_item_text(arg)
    except ValueError as error:
        print(f"  parse error: {error}")
        return False
    matches = [s for s in STRUCTURES if _structure_matches(s, ident, filt)]
    if not matches:
        print(f"  no structure named '{ident}' matches")
        return False
    if sub == "delete":
        chosen = _choose_structure(matches)
        if chosen is None:
            return False
        STRUCTURES.remove(chosen)
        _save_map()
        print(f"  deleted structure {chosen} @ {chosen.pos}")
        return True
    if not ops:  # modify
        print("  usage: /structure modify <structure>{nbt} <set|add|reset> <field> [{nbt}|value]")
        return False
    chosen = _choose_structure(matches)
    if chosen is None:
        return False
    _apply_ops(chosen, ops)
    _save_map()
    print(f"  {chosen} @ {chosen.pos}")
    return True


def _map_bounds():
    keys = [tuple(int(n) for n in k.split(",")) for k in MAP["cells"]]
    xs, ys = [k[0] for k in keys], [k[1] for k in keys]
    return (min(xs), max(xs), min(ys), max(ys)) if keys else (0, 0, 0, 0)


# --- world generation: weighted-random biomes with strong neighbor COHESION (big organic clumps).
# A candidate's odds = base `weight`, boosted per neighboring cell it relates to: same biome (scaled by
# that biome's `cluster` strength — oceans/deserts sprawl, volcanoes stay tiny) >> on its affinity list
# (mountains↔hills, river↔lake, …) > same climate. A smoothing pass then grows the clumps, and rivers
# are carved as lines from high ground to water.
GEN_SAME_BONUS, GEN_AFFINITY_BONUS, GEN_CLIMATE_BONUS = 9.0, 4.0, 1.8
_EIGHT = ((-1, -1), (-1, 0), (-1, 1), (0, -1), (0, 1), (1, -1), (1, 0), (1, 1))
WATER_BIOMES = ("ocean", "lake", "sea", "river", "jungle_river", "reef", "coast")
HIGH_BIOMES = ("mountain", "alpine", "hills", "volcano", "glacier")


def _cluster(biome):
    """A biome's cohesion strength (how big its blobs grow). 1 if unset."""
    return float((BIOMES.get(biome) or {}).get("cluster", 1) or 1)


def _gen_neighbors(x, y):
    """The biomes of (x,y)'s 8 surrounding cells that already exist on the map."""
    out = []
    for dx, dy in _EIGHT:
        cell = MAP["cells"].get(f"{x + dx},{y + dy}")
        if cell and cell.get("biome") in BIOMES:
            out.append(cell["biome"])
    return out


def _gen_pick(x, y):
    """Pick a biome for (x,y): weighted-random by base weight × cohesion bonuses from the 8 neighbors.
    Same-biome bonus scales with that biome's `cluster` strength (oceans clump hard). Fresh each call."""
    if not BIOMES:
        return None
    neighbors = _gen_neighbors(x, y)
    pool = []
    for name, entry in BIOMES.items():
        weight = float(entry.get("weight", 1) or 1)
        for nb in neighbors:
            nbe = BIOMES.get(nb, {})
            if name == nb:
                weight *= GEN_SAME_BONUS * _cluster(name)
            elif name in (nbe.get("affinity") or []) or nb in (entry.get("affinity") or []):
                weight *= GEN_AFFINITY_BONUS
            elif entry.get("temperature") == nbe.get("temperature") and entry.get("moisture") == nbe.get("moisture"):
                weight *= GEN_CLIMATE_BONUS
        pool.append((name, weight))
    total = sum(w for _, w in pool)
    if total <= 0:
        return random.choice(list(BIOMES))
    roll, acc = random.uniform(0, total), 0.0
    for name, weight in pool:
        acc += weight
        if roll <= acc:
            return name
    return pool[-1][0]


def _smooth_biomes(passes=2):
    """De-speckle: flip ONLY near-isolated cells (at most 1 of their 8 neighbors share their biome) to
    the dominant neighbor biome. Cleans lone 1-tile specks and ragged edges into smoother clumps WITHOUT
    homogenizing — cells embedded in a region (2+ like neighbors) are never touched, so oceans, deserts,
    mountains etc. all survive as big regions. Only run on a fresh generate."""
    for _ in range(passes):
        updates = {}
        for key, cell in MAP["cells"].items():
            x, y = (int(n) for n in key.split(","))
            nbrs = _gen_neighbors(x, y)
            if not nbrs:
                continue
            cur = cell.get("biome")
            if sum(1 for b in nbrs if b == cur) > 1:    # well-embedded in its own region -> keep
                continue
            counts = {}
            for b in nbrs:
                counts[b] = counts.get(b, 0) + 1
            best = max(counts, key=counts.get)
            if best != cur and counts[best] >= 3:        # a clear local majority absorbs the speck
                updates[key] = best
        for key, biome in updates.items():
            MAP["cells"][key]["biome"] = biome


def _river_path(start_key, water_keys):
    """A roughly straight (h/v segment) path from start toward the nearest water cell."""
    sx, sy = (int(n) for n in start_key.split(","))
    tx, ty = min((tuple(int(n) for n in w.split(",")) for w in water_keys),
                 key=lambda p: abs(p[0] - sx) + abs(p[1] - sy))
    path, x, y, guard = [], sx, sy, 0
    while (x, y) != (tx, ty) and guard < 500:
        guard += 1
        if abs(tx - x) >= abs(ty - y) and x != tx:   # advance along the longer axis -> straight runs
            x += 1 if tx > x else -1
        elif y != ty:
            y += 1 if ty > y else -1
        else:
            x += 1 if tx > x else -1
        key = f"{x},{y}"
        if key in MAP["cells"]:
            path.append(key)
    return path


def _carve_rivers(n=None):
    """Carve a few rivers as lines from high ground (mountains/hills) to the nearest water, so rivers
    snake across the map and connect peaks to oceans/lakes. Won't overwrite water or peaks."""
    cells = MAP["cells"]
    water = [k for k, c in cells.items() if c.get("biome") in ("ocean", "lake", "sea")]
    highs = [k for k, c in cells.items() if c.get("biome") in ("mountain", "alpine", "glacier", "volcano")]
    if not water or not highs:
        return 0
    if n is None:
        n = max(1, len(cells) // 60)
    made = 0
    keep = set(WATER_BIOMES) | set(HIGH_BIOMES)
    for _ in range(n):
        for key in _river_path(random.choice(highs), set(water)):
            if cells[key].get("biome") not in keep:
                cells[key]["biome"] = "river"
                made += 1
    return made


def _map_recommend(x, y):
    """Suggest a biome for a single cell (used by /map new|recommend) — same weighted+continuity pick
    the generator uses, so a hand-added cell blends with its neighbors."""
    return _gen_pick(x, y)


def _weighted_biome():
    """A biome chosen by base `weight` alone (used to seed regions)."""
    pool = [(name, float(e.get("weight", 1) or 1)) for name, e in BIOMES.items()]
    total = sum(w for _, w in pool) or 1
    roll, acc = random.uniform(0, total), 0.0
    for name, w in pool:
        acc += w
        if roll <= acc:
            return name
    return pool[-1][0] if pool else None


def _gen_full(cols, rows):
    """Generate a fresh cols×rows world by SEED-AND-GROW: scatter a handful of weighted-random biome
    seeds, then flood outward — each empty cell adjacent to filled ones picks via _gen_pick (strong
    cohesion). This yields several big, VARIED regions (oceans, deserts, forests…) instead of one biome
    snowballing across the whole grid. Returns cells made."""
    coords = [(x, y) for y in range(rows) for x in range(cols)]
    seeds = max(3, len(coords) // 16)                      # ~1 region nucleus per 16 cells -> big clumps
    for x, y in random.sample(coords, min(seeds, len(coords))):
        MAP["cells"][f"{x},{y}"] = {"biome": _weighted_biome(), "name": ""}
    empties = [c for c in coords if f"{c[0]},{c[1]}" not in MAP["cells"]]
    guard = 0
    while empties and guard < 100000:
        guard += 1
        random.shuffle(empties)                            # random fill order -> organic, non-directional edges
        rest = []
        for x, y in empties:
            if _gen_neighbors(x, y):
                MAP["cells"][f"{x},{y}"] = {"biome": _gen_pick(x, y), "name": ""}
            else:
                rest.append((x, y))
        if len(rest) == len(empties):                      # an isolated pocket with no filled neighbor -> seed it
            x, y = rest.pop()
            MAP["cells"][f"{x},{y}"] = {"biome": _weighted_biome(), "name": ""}
        empties = rest
    return len(coords)


def _gen_cells(coords):
    """Generate + place a biome for each (x,y) in `coords`, IN ORDER, writing into MAP['cells'] as we
    go so later cells see earlier ones as neighbors (continuity). Used for EXPAND (grow from the edge).
    Skips coords that already exist. Returns how many were created."""
    made = 0
    for x, y in coords:
        key = f"{x},{y}"
        if key in MAP["cells"]:
            continue
        MAP["cells"][key] = {"biome": _gen_pick(x, y), "name": ""}
        made += 1
    return made


_DIRS = {"north": "n", "up": "n", "n": "n", "south": "s", "down": "s", "s": "s",
         "east": "e", "right": "e", "e": "e", "west": "w", "left": "w", "w": "w"}


def _expand(direction, count):
    """Append `count` rows/columns onto a side of the existing map, generating the new cells with
    continuity seeded from the current edge. Returns cells made, or -1 for a bad direction."""
    d = _DIRS.get(str(direction).lower())
    if d is None:
        return -1
    if not MAP["cells"] or count < 1:
        return 0
    minx, maxx, miny, maxy = _map_bounds()
    coords = []
    if d == "e":      # new columns to the east; nearest-to-existing first so they seed off the edge
        for x in range(maxx + 1, maxx + count + 1):
            coords += [(x, y) for y in range(miny, maxy + 1)]
    elif d == "w":
        for x in range(minx - 1, minx - count - 1, -1):
            coords += [(x, y) for y in range(miny, maxy + 1)]
    elif d == "s":
        for y in range(maxy + 1, maxy + count + 1):
            coords += [(x, y) for x in range(minx, maxx + 1)]
    else:             # north
        for y in range(miny - 1, miny - count - 1, -1):
            coords += [(x, y) for x in range(minx, maxx + 1)]
    return _gen_cells(coords)


AUTO_EXPAND_MARGIN, AUTO_EXPAND_RING = 1, 3  # grow when an entity comes within MARGIN of an edge


def _auto_expand_near(x, y):
    """If (x,y) sits within AUTO_EXPAND_MARGIN of the map's edge, grow that side by AUTO_EXPAND_RING
    so players stay ahead of the boundary. Returns the directions grown (for reporting)."""
    if not MAP["cells"]:
        return []
    minx, maxx, miny, maxy = _map_bounds()
    grew = []
    if x - minx <= AUTO_EXPAND_MARGIN and _expand("west", AUTO_EXPAND_RING) > 0:
        grew.append("west")
    if maxx - x <= AUTO_EXPAND_MARGIN and _expand("east", AUTO_EXPAND_RING) > 0:
        grew.append("east")
    if y - miny <= AUTO_EXPAND_MARGIN and _expand("north", AUTO_EXPAND_RING) > 0:
        grew.append("north")
    if maxy - y <= AUTO_EXPAND_MARGIN and _expand("south", AUTO_EXPAND_RING) > 0:
        grew.append("south")
    return grew


# Fog of war is stored PER CELL and PER PLAYER, persistently, as two lists:
#   cell['seen'] = players who've fully revealed it (BRIGHT)   cell['dim'] = players who've only glimpsed
# it (DIMMED, stays dim forever). Moving reveals the cells within the mover's reveal_radius and dims the
# cells within dim_radius (defaults: reveal 0 = just the tile you stand on, dim 1 = the surrounding ring;
# both are stats so a scout can reveal/sense farther). The DM sees everything and hovers for who's been where.
REVEAL_RADIUS_BASE, DIM_RADIUS_BASE = 0, 1


def _cell_list(cell, key):
    lst = cell.get(key)
    if not isinstance(lst, list):
        lst = []
        cell[key] = lst
    return lst


def _vision_radii(entity):
    """(reveal_radius, dim_radius) for an entity = the base defaults + their stats (dim >= reveal)."""
    reveal = max(0, int(round(REVEAL_RADIUS_BASE + _effective_stat(entity, "reveal_radius"))))
    dim = max(reveal, int(round(DIM_RADIUS_BASE + _effective_stat(entity, "dim_radius"))))
    return reveal, dim


def _reveal_cell(player_name, x, y):
    """Fully reveal one cell for a player (scout / step). Promotes it out of 'dim'. True if it exists."""
    cell = MAP["cells"].get(f"{x},{y}")
    if cell is None:
        return False
    seen = _cell_list(cell, "seen")
    if player_name not in seen:
        seen.append(player_name)
    dim = cell.get("dim")
    if isinstance(dim, list) and player_name in dim:
        dim.remove(player_name)
    return True


def _dim_cell(player_name, x, y):
    """Mark one cell DIMMED for a player (glimpsed). Won't override a fully-revealed cell. True if exists."""
    cell = MAP["cells"].get(f"{x},{y}")
    if cell is None:
        return False
    if player_name not in _cell_list(cell, "seen"):
        dim = _cell_list(cell, "dim")
        if player_name not in dim:
            dim.append(player_name)
    return True


def _unsee_cell(player_name, x, y):
    """Undiscover a cell for a player (back to black): drop them from both seen and dim."""
    cell = MAP["cells"].get(f"{x},{y}")
    if cell is None:
        return False
    for key in ("seen", "dim"):
        lst = cell.get(key)
        if isinstance(lst, list) and player_name in lst:
            lst.remove(player_name)
    return True


def _mark_vision(player_name, cx, cy, reveal_r, dim_r):
    """Reveal cells within reveal_r of (cx,cy) and dim cells out to dim_r — persistently."""
    for dx in range(-dim_r, dim_r + 1):
        for dy in range(-dim_r, dim_r + 1):
            cheb = max(abs(dx), abs(dy))
            if cheb <= reveal_r:
                _reveal_cell(player_name, cx + dx, cy + dy)
            elif cheb <= dim_r:
                _dim_cell(player_name, cx + dx, cy + dy)


def _entity_pos(entity):
    """An entity's map cell (x,y) or None. Players store it as nbt pos:{x,y}; mobs use flat x,y."""
    pos = entity.nbt.get("pos")
    if isinstance(pos, dict) and pos.get("x") is not None and pos.get("y") is not None:
        return (int(pos["x"]), int(pos["y"]))
    x, y = entity.nbt.get("x"), entity.nbt.get("y")
    if x is not None and y is not None:
        return (int(x), int(y))
    return None


def _set_entity_pos(entity, x, y):
    """Write an entity's position (players: nbt pos:{x,y}; mobs keep their flat x,y if they use it)."""
    if "x" in entity.nbt or "y" in entity.nbt:
        entity.nbt["x"], entity.nbt["y"] = x, y
    else:
        entity.nbt["pos"] = {"x": x, "y": y}


def _distance(a, b):
    """Chebyshev distance (8-way, diagonals = 1) between two entities' cells, or None if either
    has no position. distance = max(|dx|, |dy|)."""
    pa, pb = _entity_pos(a), _entity_pos(b)
    if pa is None or pb is None:
        return None
    return max(abs(pa[0] - pb[0]), abs(pa[1] - pb[1]))


def _map_occupants():
    """{(x,y): [names]} — every entity in WORLD that has a position (players are NOT a party;
    they scatter, so the map shows each one where they actually are)."""
    occupants = {}
    for name, entity in WORLD.items():
        cell = _entity_pos(entity)
        if cell:
            occupants.setdefault(cell, []).append(name)
    return occupants


def _entity_symbol(entity, default):
    """The map glyph for an entity: its nbt `symbol` if set, else `default`. ANY string works
    (incl. emoji), but a single narrow character keeps the box grid aligned — wide/emoji glyphs
    are 2 columns and will nudge that cell's row."""
    symbol = entity.nbt.get("symbol") if entity else None
    return str(symbol) if symbol else default


def _map_glyph(cell, focus_here=False, occupied=False):
    """The single symbol for a cell in the text view: occupant marker, focus marker, else biome glyph."""
    if occupied:
        return "@"
    if focus_here:
        return "◆"
    glyph = (BIOMES.get((cell or {}).get("biome"), {}) or {}).get("glyph")
    return glyph or "·"


def _map_table(focus=None, occupants=None):
    """A compact TEXT view of the map (we're atlas-only — the rich layered map is the UI atlas):
    a glyph grid (one biome glyph per cell) + a legend of the biomes present + a list of named cells,
    where entities stand, and the weather under each. `focus` (x,y) marks ◆."""
    cells = MAP["cells"]
    if not cells:
        return ["  (map is empty — generate one:  /map generate <N>x<K>   e.g.  /map generate 12x8)"]
    occupants = occupants if occupants is not None else _map_occupants()
    minx, maxx, miny, maxy = _map_bounds()
    w, h = maxx - minx + 1, maxy - miny + 1
    out = [f"  Map {w}×{h}   x {minx}..{maxx}  y {miny}..{maxy}   ({len(cells)} cells)"
           + (f"   ◆ ({focus[0]},{focus[1]})" if focus else "")]
    present = {}
    for y in range(miny, maxy + 1):
        row = []
        for x in range(minx, maxx + 1):
            cell = cells.get(f"{x},{y}")
            if cell:
                present[cell.get("biome") or "?"] = True
            glyph = _map_glyph(cell, focus == (x, y), (x, y) in occupants) if cell else "·"
            row.append((glyph + " ")[:2] if len(glyph) == 1 else glyph)  # pad narrow glyphs so rows ~align
        out.append("  " + "".join(row))
    legend = "  ".join(f"{(BIOMES.get(b, {}) or {}).get('glyph', '?')} {b}" for b in sorted(present))
    out.append("  legend: " + (legend or "—"))
    for (x, y), who in sorted(occupants.items()):
        weather, _ = _weather_at(x, y)
        out.append(f"    ({x},{y}) {', '.join(who)}" + (f"  ·  {weather}" if weather else ""))
    for key, cell in cells.items():
        if cell.get("name"):
            x, y = (int(n) for n in key.split(","))
            out.append(f"    ({x},{y}) ★ {cell['name']} — {cell.get('biome', '?')}")
    return out


def _map_view(focus=None, occupants=None):
    print("\n".join(_map_table(focus=focus, occupants=occupants)))


def _map_interactive(anchor=None, focus=None, occupants=None):
    """Atlas-only: the live arrow-key ASCII pan is retired — print the compact text view instead.
    (The full layered, filterable map is the UI atlas; `anchor` is accepted+ignored for callers.)"""
    _map_view(focus=focus, occupants=occupants)
    return True


def cmd_map(rest):
    """/map generate <N>x<K>      generate a fresh N×K world (weighted biomes + neighbor continuity);
                                   add  replace  to wipe an existing map first
       /map expand <dir> <count>   grow the map: append <count> rows/cols on north|south|east|west,
                                   biomes seeded from the edge (also auto-grows as players near an edge)
       /map look|dim|hide <player> <x> <y> […]    adjust a player's fog without moving: reveal (bright) /
                                   dim (glimpsed) / hide (back to black) the given tile(s)
       /map [view [<player>]]      compact text view (glyph grid + legend); <player> marks ◆ on them
       /map pos <player> <x> <y>    set a PLAYER's position (nbt pos:{x,y}) — players aren't a party
       /map pos <x> <y>             set the legacy global marker (MAP['pos'])
       /map get <x> <y>            a cell's full info
       /map set <x> <y> <biome>|{nbt}   create/update a cell
       /map new <x> <y> [biome]    create a cell (biome auto-recommended from neighbors if omitted)
       /map recommend <x> <y>      suggest a biome from neighboring cells
    (Atlas-only: the rich layered/filterable map is the UI atlas; the console view is text.)"""
    parts = rest.split()
    sub = parts[0] if parts else "view"
    if sub in ("view", "show"):
        anchor, focus, occupants = None, None, _map_occupants()
        nums = [int(t) for t in parts[1:] if t.lstrip("-").isdigit()]
        names = [t for t in parts[1:] if not t.lstrip("-").isdigit()]
        if len(nums) >= 2:
            anchor = (nums[0], nums[1])
        if names:  # focus on a named player
            entity = WORLD.get(names[0])
            if entity is None:
                print(f"  no character '{names[0]}'")
                return False
            focus = _entity_pos(entity)
            if focus is None:
                print(f"  {names[0]} has no position — set it with /map pos {names[0]} <x> <y>")
        _map_view(focus=focus, occupants=occupants)
        return True
    if sub == "generate":
        dims = re.findall(r"\d+", " ".join(parts[1:]))
        if len(dims) < 2:
            print("  usage: /map generate <N>x<K>   (columns × rows, e.g. /map generate 12x8)")
            return False
        n, k = int(dims[0]), int(dims[1])
        if n < 1 or k < 1:
            print("  size must be at least 1×1")
            return False
        if MAP["cells"] and "replace" not in parts:
            print(f"  the map already has {len(MAP['cells'])} cells — add  replace  to wipe + regenerate, "
                  "or grow it with  /map expand <dir> <count>")
            return False
        if "replace" in parts:
            MAP["cells"].clear()
            STRUCTURES.clear()
        made = _gen_full(n, k)                  # seed-and-grow: several big, varied regions
        _smooth_biomes(2)                       # de-speckle ragged edges / lone tiles
        rivers = _carve_rivers()                # snake a few rivers from peaks to water
        MAP["pos"] = [n // 2, k // 2]
        _save_map()
        print(f"  generated a {n}×{k} world ({made} cells" + (f", {rivers} river tiles" if rivers else "")
              + ") — open the atlas to view it")
        return True
    if sub == "expand":
        if not MAP["cells"]:
            print("  nothing to expand — generate first:  /map generate <N>x<K>")
            return False
        direction = parts[1] if len(parts) > 1 else ""
        count = next((int(t) for t in parts[2:] if t.isdigit()), 1)
        made = _expand(direction, count)
        if made < 0:
            print("  usage: /map expand <north|south|east|west> <count>")
            return False
        minx, maxx, miny, maxy = _map_bounds()
        _save_map()
        print(f"  expanded {direction} by {count} ({made} new cells) — map now x {minx}..{maxx} y {miny}..{maxy}")
        return True
    if sub in ("look", "scout", "reveal", "dim", "hide", "unsee"):   # adjust a player's fog without moving
        verb = {"look": _reveal_cell, "scout": _reveal_cell, "reveal": _reveal_cell,
                "dim": _dim_cell, "hide": _unsee_cell, "unsee": _unsee_cell}[sub]
        named = [t for t in parts[1:] if not t.lstrip("-").isdigit()]
        nums = [int(t) for t in parts[1:] if t.lstrip("-").isdigit()]
        if not named or len(nums) < 2:
            print(f"  usage: /map {sub} <player> <x> <y> [<x2> <y2> ...]   (reveal | dim | hide tile(s) for a player)")
            return False
        if named[0] not in WORLD:
            print(f"  no character '{named[0]}'")
            return False
        pairs = [(nums[i], nums[i + 1]) for i in range(0, len(nums) - 1, 2)]
        done = sum(1 for x, y in pairs if verb(named[0], x, y))
        _save_map()
        word = {"look": "reveals", "scout": "reveals", "reveal": "reveals", "dim": "dims", "hide": "hides", "unsee": "hides"}[sub]
        print(f"  {named[0]} {word} {done} tile{'s' if done != 1 else ''}: {', '.join(f'({x},{y})' for x, y in pairs)}")
        return True
    if sub == "pos":
        named = [t for t in parts[1:] if not t.lstrip("-").isdigit()]
        nums = [int(t) for t in parts[1:] if t.lstrip("-").isdigit()]
        if named and len(nums) >= 2:  # set a player's position
            entity = WORLD.get(named[0])
            if entity is None:
                print(f"  no character '{named[0]}'")
                return False
            entity.nbt["pos"] = {"x": nums[0], "y": nums[1]}
            if not _is_mob(entity):       # teleporting a player reveals/dims their vision around the landing cell
                _mark_vision(named[0], nums[0], nums[1], *_vision_radii(entity))
            _save_map()
            print(f"  {named[0]} is now at ({nums[0]},{nums[1]})")
            return True
        if len(nums) >= 2:  # legacy global marker
            MAP["pos"] = [nums[0], nums[1]]
            _save_map()
            print(f"  global marker set to ({nums[0]},{nums[1]})")
            return True
        print("  usage: /map pos <player> <x> <y>   (or  /map pos <x> <y>  for the global marker)")
        return False
    if sub == "add":
        return _add_renamed("map")
    if sub in ("get", "set", "new", "recommend") and len(parts) >= 3 and parts[1].lstrip("-").isdigit() and parts[2].lstrip("-").isdigit():
        x, y = int(parts[1]), int(parts[2])
        key = f"{x},{y}"
        if sub == "get":
            cell = MAP["cells"].get(key)
            print(f"  ({x},{y}): {cell}" if cell else f"  no cell at ({x},{y})")
            return cell
        if sub == "recommend":
            biome = _map_recommend(x, y)
            print(f"  ({x},{y}) recommended biome: {biome}" if biome else f"  ({x},{y}): no neighbors to suggest from")
            return biome
        if sub == "set":
            rest_val = rest.split(None, 3)[3] if len(rest.split(None, 3)) > 3 else ""
            cell = MAP["cells"].setdefault(key, {"biome": "", "name": ""})
            if rest_val.startswith("{"):
                _, nbt, _ = parse_item_text("_" + rest_val)
                cell.update(nbt)
            elif rest_val:
                cell["biome"] = rest_val.strip()
            else:
                print("  usage: /map set <x> <y> <biome>|{nbt}")
                return False
            _save_map()
            print(f"  ({x},{y}) = {cell}")
            return True
        if sub == "new":
            if key in MAP["cells"]:
                print(f"  cell ({x},{y}) already exists — use /map set to edit it")
                return False
            biome = parts[3] if len(parts) > 3 else (_map_recommend(x, y) or "unknown")
            MAP["cells"][key] = {"biome": biome, "name": ""}
            _save_map()
            print(f"  added ({x},{y}) biome '{biome}'" + ("  (recommended from neighbors)" if len(parts) <= 3 else ""))
            return True
    print("  usage: /map generate <N>x<K> | expand <dir> <count> | view [player] | pos <x> <y> | "
          "get <x> <y> | set <x> <y> <biome>|{nbt} | new <x> <y> [biome] | recommend <x> <y>")
    return False


# --- /summon (spawn mob NPCs; like /character but mob-flavored, built for hordes) ------------
def _load_bestiary(path="bestiary.json"):
    """Mob presets: species -> {description, stats, attributes, nbt, abilities[]}."""
    try:
        with open(path) as handle:
            data = json.load(handle)
            return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except ValueError as err:  # malformed JSON: warn loudly rather than silently dropping ALL presets
        print(f"  [warning] {path} failed to parse ({err}); no bestiary presets loaded until it's fixed")
        return {}


BESTIARY = _load_bestiary()


def _load_body_coverage(path="body_coverage.csv"):
    """Equip-slot -> % of the body it protects (the shared default; players can override per-slot).
    Slot names are normalized lowercase + de-pluralized so 'Pauldrons'/'Greaves' match the slot keys
    'pauldron'/'greave'. Uses the armor coverage %, falling back to the under-armor % (clothing)."""
    coverage = {}
    try:
        with open(path) as handle:
            for row in csv.DictReader(handle):
                name = (row.get("Article") or "").strip().lower().rstrip("s")
                value = (row.get("Armor Coverage (%)") or "").strip() or (row.get("Under-Armor Coverage (%)") or "").strip()
                if name and value:
                    try:
                        coverage[name] = float(value)
                    except ValueError:
                        pass
    except OSError:
        pass
    return coverage


BODY_COVERAGE = _load_body_coverage()


def _apply_template(mob, species):
    """Stamp a bestiary preset onto a freshly-made mob: default stats/attributes/nbt/abilities.
    (Inline /summon {nbt} is applied AFTER this, so it overrides.)"""
    template = BESTIARY.get(species)
    if not template:
        return False
    mob.modify(**template.get("nbt", {}))
    for stat, value in {**template.get("attributes", {}), **template.get("stats", {})}.items():
        mob.stat.set(stat, value)  # attributes folded into the one stat namespace
    for ability_text in template.get("abilities", []):
        try:
            mob.ability.add(Ability.from_nbt(*parse_item_text(ability_text)[:2]))
        except ValueError:
            pass
    return True


def cmd_summon(rest):
    """/summon <species> [count <n>] [at <x> <y>] [{nbt}]
    Spawns mob NPC(s): Player-grade entities with mob:true + npc:true and a 'species' (NOT the
    character tier fields). A matching bestiary.json preset stamps default stats/attributes/
    abilities/nbt; inline {nbt} overrides. Found by @!a and @m; handles are <species>#N:
      /summon wolf count 5 at 2 1      /summon cyclops{name:"One-Eye"}"""
    global CURRENT
    rest = rest.strip()
    nbt = {}
    if "{" in rest:
        brace = rest.index("{")
        try:
            _, nbt, _ = parse_item_text("_" + rest[brace:])
        except ValueError as error:
            print(f"  NBT parse error: {error}")
            return False
        rest = rest[:brace].strip()
    tokens = rest.split()
    if not tokens:
        print("  usage: /summon <species> [count <n>] [at <x> <y>] [{nbt}]")
        return False
    species = tokens[0]
    count, x, y, i = 1, None, None, 1
    while i < len(tokens):
        if tokens[i] == "count" and i + 1 < len(tokens) and tokens[i + 1].isdigit():
            count = int(tokens[i + 1])
            i += 2
        elif tokens[i] == "at" and i + 2 < len(tokens) and tokens[i + 1].lstrip("-").isdigit() and tokens[i + 2].lstrip("-").isdigit():
            x, y = int(tokens[i + 1]), int(tokens[i + 2])
            i += 3
        else:
            print(f"  unexpected '{tokens[i]}' in /summon (use: count <n>, at <x> <y>, {{nbt}})")
            return False
    extra = dict(nbt)
    if x is not None:
        extra["x"], extra["y"] = x, y
    created = []
    for _ in range(max(1, count)):
        n = 1
        while f"{species}#{n}" in WORLD:
            n += 1
        name = f"{species}#{n}"
        mob = Mob(name).modify(species=species)
        _apply_template(mob, species)     # bestiary preset (stats/attrs/abilities/nbt)
        mob.modify(**extra)               # inline {nbt} + position override the preset
        mob.recompute()                   # derive stats from the preset's attributes
        WORLD[name] = _fill_generic_nbt(mob)
        created.append(name)
    CURRENT = [(c, WORLD[c]) for c in created]  # the summoned group becomes @p
    where = f" at ({x},{y})" if x is not None else ""
    known = " [bestiary]" if species in BESTIARY else ""
    print(f"  summoned {len(created)}x {species}{where}{known}: {', '.join(created)}")
    return True


# --- /talent /effect /ability (collections of named NBT elements) ------------

def _collection_command(rest, container_attr, element_cls, cmd_name):
    """Shared new|list|modify|remove for talents/effects/abilities, using the same NBT logic.
       /<cmd> new    <selector> <name>{nbt}
       /<cmd> list   <selector>
       /<cmd> modify <selector> <name> {nbt}  |  <name> <set|add|reset> <field> <value>
       /<cmd> remove <selector> <name>"""
    sub, _, arg = rest.partition(" ")
    sub, arg = sub.strip(), arg.strip()
    sel, remainder = _split_selector(arg)
    targets = _resolved(sel)
    if targets is None:
        return False
    if sub == "list":
        results = []
        for name, player in targets:
            members = list(getattr(player, container_attr))
            print(f"  {name}.{container_attr}: " + _container_line(player, container_attr))
            results.append([m.name for m in members])
        return results[0] if len(results) == 1 else results
    if sub == "add":
        return _add_renamed(cmd_name)
    if sub == "new":
        if not remainder:
            print(f"  usage: /{cmd_name} new <selector> <name>{{nbt}}")
            return False
        try:
            ident, nbt, _ = parse_item_text(remainder)
        except ValueError as error:
            print(f"  parse error: {error}")
            return False
        for name, player in targets:
            element = element_cls.from_nbt(ident, nbt)
            getattr(player, container_attr).add(element)
            print(f"  {name}: added {cmd_name} {element}")
            if cmd_name in ("effect", "talent", "ability"):
                _fire_element_hook(player, element, "on_apply")   # passive grant when gained
            _flag_path_issues(player, element)
        return True
    if sub in ("modify", "remove"):
        # ident ends at the first space OR '{', so `name{nbt}` (attached) routes the braces to ops
        head = re.match(r"^([^\s{]+)\s*(.*)$", remainder, re.DOTALL)
        if not head or not head.group(1):
            print(f"  usage: /{cmd_name} {sub} <selector> <name> ...")
            return False
        ident, ops = head.group(1), head.group(2).strip()
        ok = False
        for name, player in targets:
            container = getattr(player, container_attr)
            element = container.get(ident)
            if element is None:
                print(f"  {name} has no {cmd_name} '{ident}'")
                continue
            if sub == "remove":
                if cmd_name in ("effect", "talent", "ability"):
                    _fire_element_hook(player, element, "on_clear")   # reverse the grant on removal
                container.remove(element)
                print(f"  {name}: removed {cmd_name} '{ident}'")
                ok = True
            elif not ops:
                print(f"  usage: /{cmd_name} modify <selector> <name> {{nbt}}  |  <name> <set|add|reset> <field> <value>")
            else:
                _apply_ops(element, ops)  # ops is '{nbt}' (merge) or 'set|add|reset ...'
                print(f"  {name}: {element}")
                _flag_path_issues(player, element)
                ok = True
        return ok
    print(f"  usage: /{cmd_name} <new|list|modify|remove> <selector> <name>{{nbt}}")
    return False


# --- /quest (character-held quests with rewards + expiry) --------------------

def cmd_quest(rest):
    """/quest new <selector> <quest>{nbt} [turns_to_expire]   give a quest (pulses quest_obtained)
       /quest complete <selector> <quest>      run its reward (@s=owner) + pulse quest_complete, drop it
       /quest modify <selector> <quest> <set|add|reset> <field> [{nbt}|value]
       /quest delete <selector> <quest> [true]    drop it; true also pulses quest_failed (no reward)
       /quest list <selector>
    `reward` is a Reaction (deltas and/or /function names). `expiration` counts down on the owner's
    turns; at 0 it auto-fails (pulses quest_failed)."""
    sub, _, arg = rest.partition(" ")
    sub, arg = sub.strip(), arg.strip()
    sel, remainder = _split_selector(arg)
    targets = _resolved(sel)
    if targets is None:
        return False
    if sub == "list":
        out = []
        for name, player in targets:
            quests = list(player.quest)
            shown = ", ".join(_quest_label(q) for q in quests) if quests else "(none)"
            print(f"  {name}.quest: {shown}")
            out.append([q.name for q in quests])
        return out[0] if len(out) == 1 else out
    if sub == "new":
        if not remainder:
            print("  usage: /quest new <selector> <quest>{nbt} [turns_to_expire]")
            return False
        try:
            ident, nbt, trailing = parse_item_text(remainder)
        except ValueError as error:
            print(f"  parse error: {error}")
            return False
        if trailing:
            if re.fullmatch(r"-?\d+", trailing):
                nbt["expiration"] = int(trailing)  # the positional [turns] overrides
            else:
                print(f"  (ignored trailing '{trailing}' — [turns_to_expire] must be a whole number)")
        for name, player in targets:
            quest = Quest.from_nbt(ident, dict(nbt))
            player.quest.add(quest)
            print(f"  {name}: obtained quest {quest}")
            _flag_path_issues(player, quest)  # flag missing reward functions
            _report_fired(name, _pulse_procs(player, ["quest_obtained"]))
        return True
    if sub == "objective":   # manage a quest's progress-elements (add/list/clear/<idx> set|goal|label|proc|remove)
        m = re.match(r"^(\S+)\s*(.*)$", remainder, re.DOTALL)
        if not m:
            print("  usage: /quest objective <selector> <quest> <list|add|clear|<idx> ...>")
            return False
        ident, ops = m.group(1), m.group(2).strip()
        ok = False
        for name, player in targets:
            quest = player.quest.get(ident)
            if quest is None:
                print(f"  {name} has no quest '{ident}'")
                continue
            if _quest_objective_op(name, player, quest, ops):
                ok = True
        return ok
    # complete / modify / delete address an EXISTING quest by name
    head = re.match(r"^([^\s{]+)\s*(.*)$", remainder, re.DOTALL)
    if not head or not head.group(1):
        print(f"  usage: /quest {sub or '<sub>'} <selector> <quest> ...")
        return False
    ident, ops = head.group(1), head.group(2).strip()
    ok = False
    for name, player in targets:
        quest = player.quest.get(ident)
        if quest is None:
            print(f"  {name} has no quest '{ident}'")
            continue
        if sub == "complete":
            _complete_quest(name, player, quest)
            ok = True
        elif sub == "delete":
            failed = ops.strip().lower() in ("true", "1", "yes")
            player.quest.remove(quest)
            print(f"  {name}: deleted quest '{ident}'" + (" -> quest_failed" if failed else " (silent)"))
            if failed:
                _report_fired(name, _pulse_procs(player, ["quest_failed"]))
            ok = True
        elif sub == "modify":
            if not ops:
                print("  usage: /quest modify <selector> <quest> <set|add|reset> <field> [{nbt}|value]")
                continue
            _apply_ops(quest, ops)
            print(f"  {name}: {quest}")
            _flag_path_issues(player, quest)
            ok = True
        else:
            print("  usage: /quest <new|complete|modify|delete|list|objective> <selector> <quest> ...")
            return False
    return ok


def _obj_done(o):
    """An objective is complete when current meets its target (percent>=100, segment>=goal, binary>=1)."""
    cur = o.get("current", 0) or 0
    t = o.get("type")
    if t == "percent":
        return cur >= 100
    if t == "segment":
        return cur >= (o.get("goal", 1) or 1)
    return cur >= 1


def _obj_progress_text(o):
    t = o.get("type")
    if t == "percent":
        return f"{o.get('current', 0)}%"
    if t == "segment":
        return f"{o.get('current', 0)}/{o.get('goal', 1)}"
    return "done" if (o.get("current", 0) or 0) else "incomplete"


def _complete_quest(owner_name, player, quest):
    """Fire the quest's on:complete hooks (the reward — @s = owner), drop the quest, and pulse
    'quest_complete'. Shared by /quest complete and the all-objectives-done auto-complete."""
    rewarded = _emit(player, "complete", origin=player, subject=quest)   # quest reward is now an on:complete hook
    player.quest.remove(quest)
    print(f"  {owner_name}: completed quest '{quest.name}'")
    _report_fired(owner_name, rewarded)
    _report_fired(owner_name, _pulse_procs(player, ["quest_complete"]))


def _find_objective(quest, key):
    """Locate an objective by 0-based index OR by id. Returns (index, obj) or (None, None)."""
    objs = quest.objectives
    if re.fullmatch(r"\d+", str(key)):
        i = int(key)
        if 0 <= i < len(objs):
            return i, objs[i]
    for i, o in enumerate(objs):
        if o.get("id") == key:
            return i, o
    return None, None


def _next_obj_id(quest):
    n, used = 1, {o.get("id") for o in quest.objectives}
    while f"ob{n}" in used:
        n += 1
    return f"ob{n}"


def _set_objective(owner_name, player, quest, obj, value):
    """Set one objective's progress (clamped to its type). Pulses the objective's own procs on the
    rising edge (incomplete -> complete), then AUTO-COMPLETES the quest if every objective is done.
    Returns True if the quest auto-completed (so the caller stops touching it)."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        print("  progress must be a number")
        return False
    t = obj.get("type")
    if t == "percent":
        v = max(0, min(100, v))
    elif t == "segment":
        v = max(0, min(obj.get("goal", 1) or 1, int(round(v))))
    else:  # binary
        v = 1 if v else 0
    obj["current"] = int(v) if v == int(v) else v
    was_done, now_done = bool(obj.get("done")), _obj_done(obj)
    obj["done"] = now_done
    label = obj.get("label") or obj.get("id")
    print(f"  {owner_name}: '{quest.name}' · {label} -> {_obj_progress_text(obj)}" + ("  ✓" if now_done else ""))
    if now_done and not was_done and obj.get("proc"):
        _report_fired(owner_name, _pulse_procs(player, list(obj["proc"])))
    if quest.objectives and all(_obj_done(o) for o in quest.objectives):
        print(f"    all objectives complete -> auto-completing '{quest.name}'")
        _complete_quest(owner_name, player, quest)
        return True
    return False


def _quest_objective_op(owner_name, player, quest, ops):
    """One /quest objective operation on a single owner's quest. Verbs: (none)/list, add <type> <label>,
    clear, and <idx|id> <set|goal|label|proc|remove> ... Returns True on success."""
    parts = ops.split()
    if not parts or parts[0] == "list":
        if not quest.objectives:
            print(f"  '{quest.name}': no objectives")
            return True
        for i, o in enumerate(quest.objectives):
            proc = ("  proc:" + ",".join(o["proc"])) if o.get("proc") else ""
            print(f"    [{i}] {o['id']} {o['type']} \"{o['label']}\"  {_obj_progress_text(o)}{proc}")
        return True
    verb = parts[0]
    if verb == "add":
        t = parts[1].lower() if len(parts) > 1 else ""
        if t not in OBJECTIVE_TYPES:
            print("  usage: /quest objective <sel> <quest> add binary|segment|percent <label>")
            return False
        label = ops.split(None, 2)[2] if len(parts) > 2 else ""
        obj = {"id": _next_obj_id(quest), "type": t, "label": label,
               "goal": 100 if t == "percent" else (1 if t == "binary" else 3),
               "current": 0, "proc": [], "done": False}
        quest.objectives.append(obj)
        print(f"  {owner_name}: '{quest.name}' + {t} objective [{len(quest.objectives) - 1}] {obj['id']} \"{label}\"")
        return True
    if verb == "clear":
        quest.objectives.clear()
        print(f"  {owner_name}: '{quest.name}' objectives cleared")
        return True
    idx, obj = _find_objective(quest, verb)
    if obj is None:
        print(f"  '{quest.name}' has no objective '{verb}' (use an index 0.. or its id)")
        return False
    action = parts[1] if len(parts) > 1 else ""
    rest_text = ops.split(None, 2)[2].strip() if len(parts) > 2 else ""
    if action == "remove":
        quest.objectives.pop(idx)
        print(f"  {owner_name}: '{quest.name}' removed objective [{idx}]")
        return True
    if action == "label":
        obj["label"] = rest_text
        print(f"  {owner_name}: '{quest.name}' [{idx}] label = \"{rest_text}\"")
        return True
    if action == "goal":
        try:
            obj["goal"] = max(1, int(rest_text))
        except ValueError:
            print("  goal must be a whole number")
            return False
        if (obj.get("current") or 0) > obj["goal"]:
            obj["current"] = obj["goal"]
        obj["done"] = _obj_done(obj)
        print(f"  {owner_name}: '{quest.name}' [{idx}] goal = {obj['goal']}")
        return True
    if action == "proc":
        if not rest_text or rest_text.lower() == "reset":
            obj["proc"] = []
            print(f"  {owner_name}: '{quest.name}' [{idx}] proc cleared")
            return True
        obj["proc"] = [s.strip() for s in re.split(r"[,\s]+", rest_text.strip("[]{}")) if s.strip()]
        print(f"  {owner_name}: '{quest.name}' [{idx}] proc = {','.join(obj['proc'])}")
        return True
    if action == "set":
        if not rest_text:
            print("  usage: /quest objective <sel> <quest> <idx> set <n>")
            return False
        _set_objective(owner_name, player, quest, obj, rest_text.split()[0])
        return True
    print(f"  unknown objective action '{action}' (set|goal|label|proc|remove)")
    return False


def _quest_label(quest):
    """'slay_dragon (3t)' / '(until turn 92)' / '(until {hex_lifted:true})' — show the remaining expiry."""
    exp = quest.expiration
    if isinstance(exp, int) and not isinstance(exp, bool):
        return f"{quest.name} ({exp}t)"
    target = _resolve_expire_on(getattr(quest, "expire_on", None))
    if target is not None:
        return f"{quest.name} (until turn {target})"
    cw = getattr(quest, "expire_when", None)
    if isinstance(cw, Proc) and cw.clauses:
        return f"{quest.name} (until {cw})"
    return quest.name


def _fail_quest(player, quest, reason):
    """Drop a quest as failed: remove it + pulse 'quest_failed' on the owner."""
    player.quest.remove(quest)
    print(f"    {player.name}: quest '{quest.name}' {reason} -> quest_failed")
    _report_fired(player.name, _pulse_procs(player, ["quest_failed"]))


def _tick_quests(player):
    """One of the owner's turns passes: count down a turns-based expiration (auto-fail at 0), and
    fail any quest whose expire_on date has been reached. Proc-based expire_when is handled the moment
    the proc fires (see _check_quest_clears). Quests with no expiry are untouched."""
    for quest in list(player.quest):
        exp = quest.expiration
        if isinstance(exp, int) and not isinstance(exp, bool):
            exp -= 1
            if exp <= 0:
                _fail_quest(player, quest, "expired")
                continue
            quest.expiration = exp
        target = _resolve_expire_on(getattr(quest, "expire_on", None))
        if target is not None and TURN >= target:
            _fail_quest(player, quest, "reached its end date")


def _check_quest_clears(player):
    """Fail any quest whose expire_when Proc matches the player's current proc-state (the quest analog
    of an effect's clear_when). Called wherever proc-state changes, so a pulse ends it at once."""
    for quest in list(player.quest):
        cw = getattr(quest, "expire_when", None)
        if isinstance(cw, Proc) and cw.clauses and cw.matches(player.proc_state):
            _fail_quest(player, quest, "condition met")


def cmd_talent(rest):
    return _collection_command(rest, "talent", Talent, "talent")


def cmd_effect(rest):
    return _collection_command(rest, "effect", Effect, "effect")


def _resolve_path(player, path):
    """Resolve a class-path to (get, set) callables so deltas can target ANYTHING editable:
       stats.<name> / attributes.<name>     -> the value
       nbt.<key>                            -> a player nbt key
       inventory.<item>.<field>             -> an item field (e.g. inventory.rope.count)
       talents/abilities/effects.<name>.<field>
    Returns None if it can't resolve."""
    parts = path.split(".")
    head = _canon_container(parts[0])  # stats/attributes -> stat, etc. (singular containers)
    container = getattr(player, head, None)
    if container is None:
        return None
    rest = parts[1:]
    if isinstance(container, dict):  # player.nbt
        if len(rest) == 1:
            key = rest[0]
            return (lambda: container.get(key), lambda v: container.__setitem__(key, v))
        return None
    if head == "stat" and len(rest) == 1:  # the value container
        key = rest[0]
        return (lambda: container.get(key), lambda v: container.set(key, v))
    if len(rest) == 2 and hasattr(container, "get"):  # member.field (items, talents, ...)
        member = container.get(rest[0])
        if member is None or not hasattr(member, "field_value"):
            return None
        field = rest[1]
        return (lambda: member.field_value(field), lambda v: member.modify(**{field: v}))
    return None


def _cost_plan(player, cost):
    """Inspect (don't mutate) an ability's Cost against the player. Returns
    (problems, planned) — collects ALL problems, not just the first. Paths can reach
    item fields (e.g. inventory.rope.count) so an ability can spend ammo."""
    problems, planned = [], []
    if cost is None:
        return problems, planned
    for path, amount in cost.entries:
        target = _resolve_path(player, path)
        if target is None:
            problems.append(f"can't resolve '{path}'")
            continue
        get, set_ = target
        current = get()
        if current is None:
            problems.append(f"no {path}")
        elif current < amount:
            problems.append(f"not enough {path} ({current} < {amount})")
        else:
            planned.append((set_, current - amount, path, amount))
    return problems, planned


def _ability_cast(arg):
    """/ability cast <caster> <ability> [<target>] — the caster pays the cost and triggers the
    cooldown; the ability's on_hit reaction is applied to the TARGET (@s = target inside it).
    Target defaults to the caster (self-cast). Reports EVERY blocking reason, not just the first."""
    sel, remainder = _split_selector(arg)
    casters = _resolved(sel)
    if casters is None:
        return False
    parts = remainder.split(None, 1)
    ident = parts[0].partition("{")[0].strip() if parts else ""
    target_sel = parts[1].strip() if len(parts) > 1 else None
    if not ident:
        print("  usage: /ability cast <caster> <ability> [<target>]")
        return False
    ok = False
    for caster_name, caster in casters:
        ability = caster.ability.get(ident)
        if ability is None:
            print(f"  {caster_name} has no ability '{ident}'")
            continue
        reasons = []
        if ability.cooldown is None:  # None = not implemented yet -> the ability isn't usable
            reasons.append("not usable yet (no cooldown set)")
        elif isinstance(ability.cooldown, Cooldown) and not ability.cooldown.ready:
            reasons.append(f"on cooldown ({ability.cooldown})")
        problems, planned = _cost_plan(caster, ability.cost)
        reasons += problems
        if reasons:
            if len(reasons) == 1:
                print(f"  {caster_name}: can't cast {ability.name} — {reasons[0]}")
            else:
                print(f"  {caster_name}: can't cast {ability.name}")
                for reason in reasons:
                    print(f"    - {reason}")
            continue
        for set_, new_value, _path, _amount in planned:
            set_(new_value)
        if isinstance(ability.cooldown, Cooldown):
            ability.cooldown.trigger()
        detail = []
        if planned:
            detail.append("paid " + ", ".join(f"{path}-{amount}" for _set, _new, path, amount in planned))
        if isinstance(ability.cooldown, Cooldown):
            detail.append(f"cooldown {ability.cooldown}")
        # who gets hit: the named target(s), or the caster if none given
        hit_targets = _resolved(target_sel) if target_sel else [(caster_name, caster)]
        if hit_targets is None:
            hit_targets = []
        print(f"  {caster_name}: cast {ability.name}" + (f" ({'; '.join(detail)})" if detail else "")
              + (f" -> {', '.join(n for n, _ in hit_targets)}" if target_sel else ""))
        for tname, target in hit_targets:
            applied = _apply_reaction(target, ability.on_hit, origin=caster)  # @s = target, @o = caster
            if applied:
                print(f"    on_hit {tname}: {', '.join(applied)}")
        ok = True
    return ok


def cmd_ability(rest):
    sub = rest.split(None, 1)[0] if rest.split() else ""
    if sub == "cast":
        return _ability_cast(rest.partition(" ")[2])
    return _collection_command(rest, "ability", Ability, "ability")


# --- /proc (signals that fire talents) ---------------------------------------

def _run_function(name, owner, origin=_INHERIT):
    """Run a /function's body lines with @s (SELF) bound to `owner`. `origin` sets @o (the cause):
    pass an entity to bind it, or leave it as _INHERIT to keep the caller's @o (so a plain sub-call
    carries the same cause through the chain). Returns True if it ran."""
    body = FUNCTIONS.get(name)
    if body is None:
        return False
    global SELF, ORIGIN
    prev_self, SELF = SELF, owner
    prev_origin = ORIGIN
    if origin is not _INHERIT:
        ORIGIN = origin
    _log(f"function '{name}' run (@s={owner.name if owner else '—'}, @o={ORIGIN.name if ORIGIN else '—'})")
    try:
        for line in body:
            dispatch(line)
    finally:
        SELF, ORIGIN = prev_self, prev_origin
    return True


def _apply_reaction(player, reaction, origin=_INHERIT):
    """Apply a Reaction to the owner. Each action is a class-path delta (negative = damage/spend;
    paths can reach item fields, e.g. inventory.rope.count(-1)) OR a /function call run with
    @s = the owner. `origin` sets @o (the cause) inside any function actions — e.g. the attacker on
    an on_hit, the caster of an ability. Returns a list of change descriptions."""
    if not isinstance(reaction, Reaction):
        return []
    applied = []
    for action in reaction.actions:
        if action[0] == "delta":
            _kind, path, amount = action
            target = _resolve_path(player, path)
            if target is None:
                applied.append(f"{path}?")  # unresolved
                continue
            get, set_ = target
            current = get()
            set_((current if current is not None else 0) + amount)
            applied.append(f"{path}{amount:+}")
        else:  # ("func", name)
            name = action[1]
            if _run_function(name, player, origin):
                applied.append(f"fn:{name}")
            else:
                applied.append(f"fn:{name}?(missing)")
    return applied


def _fire_talents(player):
    """Fire every flag-condition HOOK the player wears whose clause now matches their proc-state, then
    check effect/quest clears. (Formerly also iterated talent.proc/reaction — that's been migrated onto
    the hook bus.) Sets @s = player so reactions/functions resolve against the owner. Keeps its name:
    it's still the proc-pulse fan-out, just hook-driven now."""
    global SELF, ORIGIN
    prev_self, SELF = SELF, player
    prev_origin, ORIGIN = ORIGIN, player  # a self-procced reaction's cause is its owner (@o = @s here)
    try:
        fired = []
        # worn hooks whose `on` is a flag-condition (on:'' or 'proc') and whose flag clause now holds.
        # Flags stay the trigger — the hook just carries the reaction (was: talent.proc -> talent.reaction).
        for name, kind in _owner_hook_pairs(player):
            hook = WORLD_HOOKS.get(name)
            if hook is None or hook.on not in ("", "proc"):
                continue
            if not (isinstance(hook.condition, Proc) and hook.condition.clauses):
                continue   # a condition-less proc hook would fire on every pulse — require a flag clause
            if hook.condition.matches(player.proc_state):
                fired.append((name, _apply_reaction(player, hook.run, origin=player) if isinstance(hook.run, Reaction) else []))
        _check_effect_clears(player)   # a proc that just fired may also END an effect (clear_when)
        _check_quest_clears(player)    # ...or fail a quest whose expire_when proc now holds
        return fired
    finally:
        SELF, ORIGIN = prev_self, prev_origin


def _pulse_procs(player, names):
    """Momentary signal: turn names on, fire matching talents, then revert (reactions persist).
    Each name may be bare ('is_poisoned' -> True) or carry a bool ('is_poisoned:true',
    'scared:false') — the ':bool' sets the SIGNAL's value, not part of the key (so it matches a
    talent proc written {is_poisoned:true})."""
    prior = dict(player.proc_state)
    for token in names:
        key, sep, val = token.partition(":")
        player.proc_state[key.strip()] = (val.strip().lower() not in ("false", "0", "no", "")) if sep else True
    fired = _fire_talents(player)
    player.proc_state = prior
    return fired


def _proc_tokens(value):
    """Split an nbt proc field ('a,b' / 'a b' / list) into a list of signal-name tokens."""
    if isinstance(value, str):
        return [s.strip() for s in re.split(r"[,\s]+", value) if s.strip()]
    if isinstance(value, (list, tuple)):
        return [str(s) for s in value]
    return []


def _signal_value(token):
    """('is_poisoned' -> ('is_poisoned', True)); ('scared:false' -> ('scared', False))."""
    key, sep, val = token.partition(":")
    return key.strip(), ((val.strip().lower() not in ("false", "0", "no", "")) if sep else True)


def _enable_procs(player, names):
    """Sticky ON (like /proc enable): set the signals and fire matching talents. Returns fired list."""
    for token in names:
        key, value = _signal_value(token)
        player.proc_state[key] = value
    return _fire_talents(player)


def _disable_procs(player, names):
    """Sticky OFF (like /proc disable): clear the signals."""
    for token in names:
        player.proc_state.pop(_signal_value(token)[0], None)


def _report_fired(name, fired):
    for talent_name, changes in fired:
        detail = f" ({', '.join(changes)})" if changes else ""
        print(f"    -> {talent_name} procced{detail}")


# --- the unified HOOK event bus ----------------------------------------------
# A hook lives in WORLD_HOOKS (the session library). An element "wears" a hook by listing its name in
# that element's nbt `hooks:[...]`. _emit(owner, event) finds every hook worn by the owner's elements
# (context-gated to that element's kind) whose `on` matches the event and whose condition holds, and
# runs its reaction. This is the single path that will replace proc/reaction/on_*/reward/clear_when.
def _owner_hook_pairs(owner):
    """(hook_name, element_kind) for every hook the owner currently wears. Sources: the owner's own
    nbt.hooks (kind '') + each talent/ability/effect/quest (its kind) + each inventory/equipped item."""
    pairs = []
    def add(container, kind):
        names = container.get("hooks") if isinstance(container, dict) else getattr(container, "nbt", {}).get("hooks")
        for n in _proc_tokens(names):
            pairs.append((n, kind))
    add(owner.nbt, "")
    for t in getattr(owner, "talent", []):
        add(t, "talent")
    for a in getattr(owner, "ability", []):
        add(a, "ability")
    for e in getattr(owner, "effect", []):
        add(e, "effect")
    for q in getattr(owner, "quest", []):
        add(q, "quest")
    for it in list(getattr(owner, "inventory", [])) + [i for items in getattr(owner, "equipment", _NoEquip()).equipped().values() for i in items]:
        add(it, "item")
    return pairs


class _NoEquip:
    def equipped(self):
        return {}


def _element_kind(el):
    """The hook-context category of an element (item/ability/talent/effect/quest), '' if unknown."""
    for cls, kind in ((Item, "item"), (Ability, "ability"), (Talent, "talent"), (Effect, "effect"), (Quest, "quest")):
        if isinstance(el, cls):
            return kind
    return ""


def _subject_hook_pairs(el):
    return [(n, _element_kind(el)) for n in _proc_tokens(getattr(el, "nbt", {}).get("hooks"))]


def _emit(owner, event, origin=None, subject=None):
    """Fire hooks listening for `event` (context-gated, condition-checked). `subject` scopes to ONE
    element's hooks (so equipping item A fires only A's hooks); omit it for owner-wide events.
    Returns a list of (hook_name, applied_changes). @s = owner, @o = origin during each reaction."""
    global SELF, ORIGIN
    fired, seen = [], set()
    prev_self, prev_origin = SELF, ORIGIN
    SELF, ORIGIN = owner, origin if origin is not None else owner
    try:
        for name, kind in (_subject_hook_pairs(subject) if subject is not None else _owner_hook_pairs(owner)):
            hook = WORLD_HOOKS.get(name)
            if hook is None or hook.on != event:
                continue
            if hook.context and kind and hook.context != kind:
                continue  # a hook scoped to one element kind shouldn't fire from another
            if isinstance(hook.condition, Proc) and hook.condition.clauses and not hook.condition.matches(owner.proc_state):
                continue
            key = (name, kind)
            if key in seen:
                continue
            seen.add(key)
            applied = _apply_reaction(owner, hook.run, origin=ORIGIN) if isinstance(hook.run, Reaction) else []
            rc = hook.nbt.get("run_command")   # a Lab-built command (e.g. an /execute … run function …)
            if rc:
                rc = str(rc)
                dispatch(rc if rc.startswith("/") else "/" + rc)   # @s=owner bound; chain re-evaluates at fire time
                applied = applied + [f"ran: {rc}"]
            fired.append((name, applied))
    finally:
        SELF, ORIGIN = prev_self, prev_origin
    return fired


def _legacy_field(el, key):
    """Read a legacy field from its (possibly removed) structured attr, else from nbt where the parser
    leaves it as a string/list. Returns the raw value (str/list/Proc/Reaction) or None."""
    v = getattr(el, key, None)
    return el.nbt.get(key) if v is None else v


def _clear_legacy(el, key):
    if getattr(el, key, None) is not None:
        try:
            setattr(el, key, None)
        except (AttributeError, TypeError):
            pass
    el.nbt.pop(key, None)


def _hookify_player(player):
    """One-time migration: fold this player's legacy reaction-bearing fields into WORLD_HOOKS + each
    element's nbt.hooks, then clear them. Covers talent proc+reaction, any element's on_apply/on_clear,
    item on_equip/on_unequip/on_use, and quest reward. Idempotent (no-ops once the fields are empty)."""
    migrated = 0
    def attach(el, hn):
        worn = list(_proc_tokens(el.nbt.get("hooks")))
        if hn not in worn:
            worn.append(hn)
        el.nbt["hooks"] = worn
    def reaction(el, key):
        v = _legacy_field(el, key)
        if v in (None, "", "None"):
            return None
        r = Reaction.parse(v)
        return r if r.actions else None
    def make(el, ident, kind, suffix, on, condition=None, run=None):
        nonlocal migrated
        hn = f"{ident}__{suffix}"
        WORLD_HOOKS.setdefault(hn, Hook(name=hn, on=on, context=kind, condition=condition, run=run))
        attach(el, hn); migrated += 1
        return hn
    for t in player.talent:   # talent proc-condition + reaction -> on:proc
        pv = _legacy_field(t, "proc")
        proc = Proc.parse(pv) if pv not in (None, "", "None") else None
        run = reaction(t, "reaction")
        if proc and proc.clauses and run:
            make(t, t.name, "talent", "on", "proc", condition=repr(proc), run=repr(run))
            _clear_legacy(t, "proc"); _clear_legacy(t, "reaction")
    for kind, setobj in (("talent", player.talent), ("effect", player.effect), ("ability", player.ability)):
        for el in setobj:
            for field, event in (("on_apply", "gain"), ("on_clear", "lose")):
                r = reaction(el, field)
                if r:
                    make(el, el.name, kind, event, event, run=repr(r)); _clear_legacy(el, field)
    for it in list(player.inventory) + [i for v in player.equipment.equipped().values() for i in v]:
        for field, event in (("on_equip", "equip"), ("on_unequip", "unequip"), ("on_use", "use")):
            r = reaction(it, field)
            if r:
                make(it, it.type_id, "item", event, event, run=repr(r)); it.nbt.pop(field, None)
    for q in player.quest:   # quest reward -> on:complete
        r = reaction(q, "reward")
        if r:
            make(q, q.name, "quest", "complete", "complete", run=repr(r)); _clear_legacy(q, "reward")
    return migrated


def _hookify_all():
    return sum(_hookify_player(p) for p in WORLD.values())


def cmd_hook(rest):
    """/hook new <name>{on:..., context:..., condition:..., run:...}   define a reusable hook
       /hook list · /hook modify <name> <set|add|reset> <field> [value] · /hook delete <name>
       /hook attach <selector> <hook>      make targets wear the hook (adds to their nbt.hooks)
       /hook fire <selector> <event>       manually emit an event on targets (for testing)
    `on` = the event (equip/gain/cast/turn.end/… or a flag); `run` = a Reaction; `condition` = a Proc;
    `context` = item/ability/talent/effect/quest or blank (any)."""
    sub, _, arg = rest.partition(" ")
    sub, arg = sub.strip(), arg.strip()
    if sub == "migrate":
        n = _hookify_all()
        print(f"  migrated {n} legacy field(s) into hooks" if n else "  nothing to migrate (already on hooks)")
        return True
    if sub == "list":
        if not WORLD_HOOKS:
            print("  (no hooks defined)")
            return []
        for h in WORLD_HOOKS.values():
            print(f"  {h}")
        return sorted(WORLD_HOOKS)
    if sub == "new":
        try:
            ident, nbt, _ = parse_item_text(arg)
        except ValueError as error:
            print(f"  parse error: {error}")
            return False
        WORLD_HOOKS[ident] = Hook.from_nbt(ident, dict(nbt))
        print(f"  defined hook {WORLD_HOOKS[ident]}")
        return True
    if sub in ("modify", "delete"):
        name, _, ops = arg.partition(" ")
        hook = WORLD_HOOKS.get(name)
        if hook is None:
            print(f"  no hook '{name}'")
            return False
        if sub == "delete":
            del WORLD_HOOKS[name]
            print(f"  deleted hook '{name}'")
            return True
        if not ops.strip():
            print("  usage: /hook modify <name> <set|add|reset> <field> [value]")
            return False
        _apply_ops(hook, ops.strip())
        print(f"  {hook}")
        return True
    if sub == "attach":
        sel, rem = _split_selector(arg)
        targets = _resolved(sel)
        if targets is None:
            return False
        hookname = rem.strip().split()[0] if rem.strip() else ""
        if hookname not in WORLD_HOOKS:
            print(f"  no hook '{hookname}' (see /hook list)")
            return False
        for nm, player in targets:
            worn = list(_proc_tokens(player.nbt.get("hooks")))
            if hookname not in worn:
                worn.append(hookname)
            player.nbt["hooks"] = worn
            print(f"  {nm}: now wears hook '{hookname}'")
        return True
    if sub == "fire":
        sel, rem = _split_selector(arg)
        targets = _resolved(sel)
        if targets is None:
            return False
        event = rem.strip().split()[0] if rem.strip() else ""
        for nm, player in targets:
            _report_fired(nm, _emit(player, event))
        return True
    print("  usage: /hook <new|list|modify|delete|attach|fire> ...")
    return False


def cmd_proc(rest):
    """/proc <pulse|enable|disable> <selector> a,b,c
    enable: turn signals on (sticky) and fire matching talents.
    pulse:  turn on, fire, then revert (momentary event).
    disable: turn signals off."""
    mode, _, arg = rest.partition(" ")
    mode = mode.strip()
    if mode not in ("pulse", "enable", "disable", "query"):
        print("  usage: /proc <pulse|enable|disable|query> <selector> <proc,proc,...>")
        return False
    sel, names_text = _split_selector(arg)
    targets = _resolved(sel)
    if targets is None:
        return False
    if mode == "query":
        for name, player in targets:
            on = [k for k, v in player.proc_state.items() if v]
            print(f"  {name}.proc_state: " + (", ".join(on) if on else "(none active)"))
        return True
    names = [n.strip() for n in names_text.replace(" ", ",").split(",") if n.strip()]
    if not names:
        print(f"  usage: /proc {mode} <selector> <proc,proc,...>")
        return False
    # a signal token may carry a bool: 'is_poisoned:true' / 'scared:false' / bare 'is_poisoned' (=True).
    # The ':bool' sets the signal VALUE, not part of the key (so it matches a proc written {name:true}).
    def _signal(token):
        key, sep, val = token.partition(":")
        return key.strip(), ((val.strip().lower() not in ("false", "0", "no", "")) if sep else True)
    for name, player in targets:
        if mode == "disable":
            for proc_name in names:
                player.proc_state.pop(_signal(proc_name)[0], None)
            print(f"  {name}: disabled {', '.join(names)}")
            continue
        prior = dict(player.proc_state)
        for proc_name in names:
            key, value = _signal(proc_name)
            player.proc_state[key] = value
        fired = _fire_talents(player)
        if mode == "pulse":
            player.proc_state = prior  # momentary: revert the signals (reactions persist)
        verb = "pulsed" if mode == "pulse" else "enabled"
        print(f"  {name}: {verb} {', '.join(names)}" + ("" if fired else " (no talents reacted)"))
        _report_fired(name, fired)
    return True


# --- /var (variables + $(...) text replacement) ------------------------------
VARS = {}  # name -> stored value, text-substituted into later commands


def _roll(sides):
    """A single die roll, 1..sides inclusive (sides < 1 -> 0)."""
    return random.randint(1, sides) if sides >= 1 else 0


def cmd_random(rest):
    """/random range <min>..<max>   roll one integer in [min, max] inclusive (Minecraft-style).
    Unlike bare 'd20' (which returns a LIST of rolls), /random returns ONE number — so it captures
    cleanly:  /execute store result var $(x)->int run random range 1..20
              /data result $(x)->int run random range 1..20
    'range' is a subcommand so /random can grow other modes later. A bare '<n>' (no '..') = 1..n."""
    sub, _, arg = rest.partition(" ")
    sub, arg = sub.strip(), arg.strip()
    if sub != "range" or not arg:
        print("  usage: /random range <min>..<max>   (e.g. /random range 1..20)")
        return False
    lo_s, sep, hi_s = arg.partition("..")
    if not sep:                       # a lone number means 1..n
        lo_s, hi_s = "1", lo_s
    lo_s, hi_s = lo_s.strip(), hi_s.strip()
    if not (lo_s.lstrip("-").isdigit() and hi_s.lstrip("-").isdigit()):
        print("  usage: /random range <min>..<max>   (e.g. /random range 1..20)")
        return False
    lo, hi = int(lo_s), int(hi_s)
    if lo > hi:
        lo, hi = hi, lo  # tolerate reversed bounds
    value = random.randint(lo, hi)
    print(f"  rolled {value}  ({lo}..{hi})")
    return value


def _eval_token(inner):
    """Evaluate one $(...) token to text. `inner` may be:
       dN              -> a die roll (1..N)
       @s / @p / @a..  -> the matching entity NAME(s)  (e.g. $(@s) -> 'isleia')
       a dotted path   -> that value  ($(@s.stat.health))
       a var name      -> its stored value             ($(hp))
    Unknown var names are left as literal $(name) (so a not-yet-set var is obvious)."""
    inner = inner.strip()
    die = re.fullmatch(r"d(\d+)", inner)
    if die:
        return str(_roll(int(die.group(1))))
    if "." in inner:                                # a path -> its value ($(@s.stat.health), name.x)
        try:
            return str(_operand_value(inner))
        except Exception:
            return inner
    if inner.startswith("@"):                       # bare selector -> entity name(s)
        try:
            targets = _resolved(inner)
            if targets:
                return ", ".join(n for n, _ in targets)
        except Exception:
            pass
        return inner
    return str(VARS.get(inner, "$(" + inner + ")"))  # var; unknown stays literal


def _substitute(text):
    """Quote-aware text substitution. Inside double-quotes everything is LITERAL ("d20" -> d20,
    "$(@s)" -> $(@s)). Outside quotes, $(...) tokens are evaluated (see _eval_token) and bare dice
    dN roll. ($() is the substitution sigil rather than {braces} because {…} already means NBT.)"""
    out, plain, i, n = [], [], 0, len(text)

    def flush():
        seg = re.sub(r"\$\(([^)]*)\)", lambda m: _eval_token(m.group(1)), "".join(plain))
        seg = re.sub(r"\bd(\d+)\b", lambda m: str(_roll(int(m.group(1)))), seg)
        plain.clear()
        out.append(seg)

    while i < n:
        if text[i] == '"':                          # copy a quoted run verbatim (no substitution)
            flush()
            j = i + 1
            while j < n and text[j] != '"':
                j += 1
            out.append(text[i:min(j + 1, n)])
            i = min(j + 1, n)
        else:
            plain.append(text[i])
            i += 1
    flush()
    return "".join(out)


def _var_name(token):
    """Accept the variable name as $(name) or a bare name."""
    token = token.strip()
    match = re.match(r"^\$\((\w+)\)$", token)
    if match:
        return match.group(1)
    return token if re.match(r"^\w+$", token) else None


def _parse_var_target(token):
    """Parse the var target, optionally with a cast: $(x)->int -> ('x', 'int')."""
    token = token.strip()
    cast = None
    if "->" in token:
        token, _, cast = token.partition("->")
        cast = cast.strip() or None
    return _var_name(token), cast


DATATYPES = ["int", "float", "str", "list", "bool"]


def _cast(value, kind):
    """Cast a captured result to a datatype. int-of-a-list = its length (a count);
    float; str; list = wrap non-lists; bool = truthiness ('false'/'0'/''/'none' -> False).
    None/invalid numbers -> a sensible default."""
    if kind is None or value is None:
        return value
    if kind == "list":
        return value if isinstance(value, list) else [value]
    if kind == "str":
        if isinstance(value, list):
            return ", ".join(str(x) for x in value)  # 'albis, noael' (wrap as [$(x)] for a selector)
        return str(value)
    if kind == "bool":
        if isinstance(value, list):
            return len(value) > 0
        if isinstance(value, str):
            return value.strip().lower() not in ("", "false", "0", "none", "no")
        return bool(value)
    if kind in ("int", "float"):
        if isinstance(value, list):
            return len(value)
        try:
            number = float(value)
        except (TypeError, ValueError):
            return 0
        return int(number) if kind == "int" else number
    return value


def _format_var(value):
    """Store form: lists become [a,b,c] (reusable as a list selector); None -> ''.
    (False is kept — e.g. a ->bool cast — so it stores as 'False', not blank.)"""
    if isinstance(value, list):
        return "[" + ",".join(str(x) for x in value) + "]"
    if value is None:
        return ""
    return value


def _as_command_line(command):
    """A /var run-target may be a command written without '/' ('character list') or a bare
    path ('noael.name'). Prefix '/' only when the first token is a real command."""
    command = command.strip()
    if command.startswith("/"):
        return command
    first = command.split()[0] if command.split() else ""
    expanded = autofill.complete_command("/" + first)
    if expanded and expanded[1:] in HANDLERS:
        return "/" + command
    return command


def cmd_data(rest):
    """/data result  $(name) run <command>   store the command's return value
       /data success $(name) run <command>   store 1 if it worked, else 0
       /data set <name> <value>   |   /data list
    Then $(name) is text-replaced anywhere in later commands this session.
    (The capture forms also live on /execute store result|success var $(name) run <command>.)"""
    mode, _, tail = rest.partition(" ")
    mode = mode.strip()
    if mode == "list":
        if not VARS:
            return print("  (no variables set)")
        for key, value in VARS.items():
            print(f"  $({key}) = {value!r}")
        return
    if mode == "set":
        name_token, _, value = tail.partition(" ")
        name, cast = _parse_var_target(name_token)
        if not name:
            return print("  usage: /data set <name>[->int|float|str|list|bool] <value>")
        parsed = parse_value(_substitute(value).strip())
        VARS[name] = _format_var(_cast(parsed, cast))
        return print(f"  $({name}) = {VARS[name]!r}")
    if mode in ("result", "success"):
        name_token, _, after = tail.partition(" ")
        name, cast = _parse_var_target(name_token)
        runkw, _, command = after.partition(" ")
        if not name or runkw.strip() != "run" or not command.strip():
            return print(f"  usage: /data {mode} $(name)[->int|float|str|list|bool] run <command>")
        command = _substitute(command.strip())
        try:
            result = dispatch(_as_command_line(command))
        except Exception as error:
            print(f"  (command errored: {error})")
            result = None
        if mode == "success":
            # worked AND produced something (None = "didn't exist / no result"); cast applies too
            value = 0 if (result is None or result is False) else 1
        else:
            value = None if (result is False and not cast) else result  # bare failure -> blank
        VARS[name] = _format_var(_cast(value, cast))
        return print(f"  $({name}) = {VARS[name]!r}")
    print("  usage: /data <result|success|set|list> ...")


# --- /function (named, reusable sequences of commands) -----------------------
# A function is just a list of command-line strings. `/function execute <path> <sel>` runs
# them in order with @s (self) bound to each target — so a body like `/stat add @s health 2`
# acts on whoever you executed it on. Stored globally; @s/@p inside resolve at run time.
FUNCTIONS = {}  # path -> list of command lines


def _print_function(path, lines):
    print(f"  function '{path}':" + ("" if lines else " (empty)"))
    for i, line in enumerate(lines, 1):
        print(f"    {i:>2}  {line}")


EDITOR_META = ":list  :del N  :ins N <command>  :move A B  :swap A B  :done  :cancel  :help"  # one source of truth


def _function_editor(path):
    """In-REPL line editor for a function body. Normal command lines (with live TAB autofill)
    are appended; ':'-prefixed meta-commands edit the buffer. Doubles as create."""
    global PROMPT
    lines = list(FUNCTIONS.get(path, []))
    print(f"  editing function '{path}' — {len(lines)} line(s)" + (" (new)" if path not in FUNCTIONS else ""))
    print(f"  type command lines (TAB completes). meta: {EDITOR_META}")
    while True:
        try:
            PROMPT = f"  {path}:{len(lines) + 1}> "  # so the completion-listing redraw uses THIS prompt
            raw = input(PROMPT).strip()
        except (EOFError, KeyboardInterrupt):
            print("\n  (cancelled — not saved)")
            return False
        if not raw:
            continue
        if not raw.startswith(":"):
            lines.append(raw)
            continue
        tokens = raw[1:].split(None, 2)  # [meta, arg1, rest]
        meta = tokens[0].lower() if tokens else ""
        if meta in ("done", "save", "wq"):
            FUNCTIONS[path] = lines
            print(f"  saved '{path}' ({len(lines)} line(s)).")
            return True
        if meta in ("cancel", "quit", "q"):
            print("  (cancelled — not saved)")
            return False
        if meta in ("list", "l"):
            _print_function(path, lines)
        elif meta in ("help", "h", "?"):
            print(f"  {EDITOR_META}")
        elif meta in ("del", "d", "rm"):
            idx = _editor_index(tokens[1:], lines)
            if idx is not None:
                print(f"  deleted [{idx + 1}] {lines.pop(idx)}")
        elif meta in ("ins", "insert", "i"):
            if len(tokens) < 3 or not tokens[1].isdigit():
                print("  usage: :ins <line#> <command>")
            else:
                at = max(0, min(int(tokens[1]) - 1, len(lines)))
                lines.insert(at, tokens[2])
                print(f"  inserted at [{at + 1}] {tokens[2]}")
        elif meta in ("move", "mv", "m"):
            nums = raw[1:].split()[1:]
            if len(nums) != 2 or not all(n.lstrip("-").isdigit() for n in nums):
                print("  usage: :move <from#> <to#>")
            else:
                a = _editor_index([nums[0]], lines)
                if a is not None:
                    b = max(0, min(int(nums[1]) - 1, len(lines) - 1))
                    lines.insert(b, lines.pop(a))
                    print(f"  moved [{a + 1}] -> [{b + 1}]")
        elif meta in ("swap", "sw"):
            nums = raw[1:].split()[1:]
            if len(nums) != 2 or not all(n.lstrip("-").isdigit() for n in nums):
                print("  usage: :swap <line#> <line#>")
            else:
                a = _editor_index([nums[0]], lines)
                b = _editor_index([nums[1]], lines) if a is not None else None
                if a is not None and b is not None:
                    lines[a], lines[b] = lines[b], lines[a]
                    print(f"  swapped [{a + 1}] <-> [{b + 1}]")
        else:
            print(f"  unknown editor command ':{meta}'  (try :help)")


def _editor_index(args, lines):
    """Parse a 1-based line number into a valid 0-based index, or None (after an error)."""
    if not args or not args[0].lstrip("-").isdigit():
        print("  expected a line number")
        return None
    idx = int(args[0]) - 1
    if 0 <= idx < len(lines):
        return idx
    print(f"  no line {args[0]} (function has {len(lines)})")
    return None


def _function_execute(path, selector=None):
    """Run a function's lines. With a selector, @s (self) binds to each target. WITHOUT one, it
    INHERITS the caller's @s (SELF) — so a function calling another function carries @s through,
    Minecraft-style. At top level SELF is None, so a no-@s function (e.g. world setup) still runs."""
    body = FUNCTIONS.get(path)
    if body is None:
        print(f"  no function '{path}'")
        return False
    if not selector:
        _run_function(path, SELF)  # inherit caller's @s (None at top level)
        print(f"  ran '{path}'" + (f" (@s={SELF.name})" if SELF else ""))
        return True
    targets = _resolved(selector)
    if targets is None:
        return False
    for name, player in targets:
        _run_function(path, player)
    print(f"  ran '{path}' for {len(targets)} target(s)")
    return True


def cmd_function(rest):
    """/function edit <path>            create/edit a function (line editor)
       /function run <path> <selector>   run it (@s = each target)
       /function list | show <path> | remove <path>"""
    sub, _, arg = rest.partition(" ")
    sub, arg = sub.strip(), arg.strip()
    if sub in ("edit", "create"):
        if not arg:
            print("  usage: /function edit <path>   (add command lines after the path to set it directly)")
            return False
        # Inline body: path on the first line, the body on the lines after it. This sets the function
        # directly — works from the UI and the console alike. A BARE path (no body lines) opens the
        # interactive line editor in a real terminal; in the UI (no tty) it just shows the current body.
        first, newline, body = arg.partition("\n")
        path = first.split()[0]
        if newline:
            lines = [ln.strip() for ln in body.splitlines() if ln.strip()]
            FUNCTIONS[path] = lines
            print(f"  saved '{path}' ({len(lines)} line(s)).")
            return True
        if sys.stdin.isatty():
            return _function_editor(path)
        existing = FUNCTIONS.get(path)
        if existing is None:
            print(f"  '{path}' is new — send the body on lines after the path to set it.")
        else:
            _print_function(path, existing)
        return True
    if sub in ("run", "execute"):  # 'run' is the name; 'execute' kept as a quiet alias
        path, _, sel = arg.partition(" ")
        if not path.strip():
            print("  usage: /function run <path> [selector]   (selector optional — omit if it has no @s)")
            return False
        return _function_execute(path.strip(), sel.strip() or None)
    if sub == "list":
        if not FUNCTIONS:
            print("  (no functions yet)")
            return []
        for name, body in FUNCTIONS.items():
            print(f"  {name}  ({len(body)} line(s))")
        return list(FUNCTIONS)
    if sub == "show":
        if arg not in FUNCTIONS:
            print(f"  no function '{arg}'")
            return False
        _print_function(arg, FUNCTIONS[arg])
        return True
    if sub == "remove":
        if arg not in FUNCTIONS:
            print(f"  no function '{arg}'")
            return False
        del FUNCTIONS[arg]
        print(f"  removed function '{arg}'")
        return True
    print("  usage: /function <edit|run|list|show|remove> ...")
    return False


# --- /session (save / load / get / history) ----------------------------------
# Persist the whole world to JSON under sessions/<name>.json and restore it. Elements
# (items/talents/abilities/effects) round-trip through their parseable repr + from_nbt;
# stats/attributes/formulas/nbt/proc_state are plain dicts. UnitValues are stored as their
# text ("20ft") and recovered on load.

def _jsonable(value):
    """Make a stat/attr/nbt value JSON-safe: UnitValue -> its text; everything else as-is."""
    return str(value) if isinstance(value, UnitValue) else value


def _unjson(value):
    """Reverse of _jsonable: only a unit-looking string ('20ft') becomes a UnitValue again;
    all other strings stay strings (so 'true'/'none'/etc. aren't reinterpreted)."""
    if isinstance(value, str) and re.fullmatch(r"-?\d+(?:\.\d+)?[A-Za-z%]+", value):
        return UnitValue.parse(value)
    return value


def _ability_to_dict(a):
    d = {
        "name": a.name,
        "level": a.level,
        "description": a.description,
        "cooldown": repr(a.cooldown) if a.cooldown else None,
        "cost": repr(a.cost) if a.cost else None,
        "on_hit": repr(a.on_hit) if a.on_hit else None,
        "origin": a.origin or None,
    }
    d.update(a.nbt)
    return d


def _quest_to_dict(q):
    """A quest as JSON-able structured data (like _ability_to_dict) so its `objectives` list travels
    intact to the UI and the session file instead of being squeezed through a repr string."""
    d = {
        "name": q.name,
        "description": q.description,
        "reward": repr(q.reward) if q.reward else None,
        "expiration": q.expiration,
        "expire_on": q.expire_on,
        "expire_when": repr(q.expire_when) if getattr(q, "expire_when", None) else None,
        "objectives": [dict(o) for o in getattr(q, "objectives", [])],
    }
    d.update(q.nbt)
    return d


def _player_to_dict(p):
    return {
        "name": p.name,
        "inventory_slots": p.inventory.slots,
        "nbt": {k: _jsonable(v) for k, v in p.nbt.items()},
        "stats": {s.name: _jsonable(_effective_stat(p, s.name)) for s in p.stat},  # final = (base+gear)*mult+flat
        "stat_detail": {s.name: dict(zip(("base", "mult", "flat"), _stat_components(p, s.name))) for s in p.stat},
        "formulas": {n: str(p.formula.get(n)) for n in p.formula},
        "proc_state": dict(p.proc_state),
        "items": [repr(it) for it in p.inventory.items],
        "inventory_space": _effective_inventory_space(p),   # live grid containers (base + item-provided)
        "equipment": {slot: [repr(it) for it in items] for slot, items in p.equipment.equipped().items()},
        # NB: item-granted elements (tagged _granted_by) ARE included — the UI needs them (to show the
        # source badge). They round-trip harmlessly: _player_from_dict calls _refresh_inventory, and
        # _regrant_items reconciles by diff (re-derives from current items; no duplication, stale removed).
        "talents": [repr(t) for t in p.talent],
        "abilities": [_ability_to_dict(a) for a in p.ability],
        "effects": [{"repr": repr(e), "elapsed": e._elapsed, "total": e._total} for e in p.effect],
        "quests": [_quest_to_dict(q) for q in p.quest],
    }


def _player_from_dict(d):
    nbt = d.get("nbt", {})
    cls = Mob if nbt.get("mob") else Player  # restore mobs as Mob (preserves their type/generics)
    p = cls(d["name"], inventory_slots=d.get("inventory_slots"))
    p.nbt = {k: _unjson(v) for k, v in nbt.items()}
    for name, value in d.get("stats", {}).items():
        p.stat.set(name, _unjson(value))
    for name, value in d.get("attributes", {}).items():  # legacy sessions: fold attributes into stats
        p.stat.set(name, _unjson(value))
    for name in list(p.formula):           # drop the default formulas, restore the saved set
        p.formula.remove(name)
    for name, expr in d.get("formulas", {}).items():
        p.formula.add(name, expr)
    p.proc_state = dict(d.get("proc_state", {}))
    p.inventory.items = [Item.from_nbt(*parse_item_text(r)[:2]) for r in d.get("items", [])]
    for slot, reprs in d.get("equipment", {}).items():  # restore equipped gear (capacity from equip_slots nbt)
        p.equipment.define_slot(slot, _equip_capacity(p, slot) or len(reprs))
        for r in reprs:
            p.equipment.equip(slot, Item.from_nbt(*parse_item_text(r)[:2]))
    for r in d.get("talents", []):
        p.talent.add(Talent.from_nbt(*parse_item_text(r)[:2]))
    for r in d.get("abilities", []):
        if isinstance(r, dict):
            nbt = {k: v for k, v in r.items() if k != "name" and v is not None}
            p.ability.add(Ability.from_nbt(r.get("name", ""), nbt))
        else:
            p.ability.add(Ability.from_nbt(*parse_item_text(r)[:2]))
    for e in d.get("effects", []):
        effect = Effect.from_nbt(*parse_item_text(e["repr"])[:2])
        effect._elapsed, effect._total = e.get("elapsed", 0), e.get("total")
        p.effect.add(effect)
    for r in d.get("quests", []):
        if isinstance(r, dict):                              # structured form: objectives travel as a real list
            nbt = {k: v for k, v in r.items() if k != "name" and v is not None}
            p.quest.add(Quest.from_nbt(r.get("name", ""), nbt))
        else:                                                # legacy sessions stored quests as a repr string
            p.quest.add(Quest.from_nbt(*parse_item_text(r)[:2]))
    _refresh_inventory(p)   # re-derive item-granted talents/abilities/effects/quests/flags + recompute
    return p


def _session_to_dict():
    return {
        "id": SESSION_ID,
        "name": SESSION_NAME,
        "turn": TURN,
        "turn_active": TURN_ACTIVE,
        "weather": {"by_unit": dict(WEATHER_BY_UNIT), "turn": WEATHER_TURN,
                    "queue": [[t, u, w] for (t, u), w in WEATHER_QUEUE.items()]},
        "events": list(EVENT_QUEUE),
        "cal_events": list(CAL_EVENTS),
        "vars": {k: _jsonable(v) for k, v in VARS.items()},
        "functions": {k: list(v) for k, v in FUNCTIONS.items()},
        "hooks": {name: repr(h) for name, h in WORLD_HOOKS.items()},
        "history": list(HISTORY),
        "players": {name: _player_to_dict(p) for name, p in WORLD.items()},
    }


def _session_from_dict(data):
    global TURN, TURN_ACTIVE, WEATHER_TURN
    WORLD.clear()
    for name, pd in data.get("players", {}).items():
        WORLD[name] = _player_from_dict(pd)
    TURN = data.get("turn", 0)
    TURN_ACTIVE = data.get("turn_active")
    weather = data.get("weather", {})
    WEATHER_TURN = weather.get("turn", -1)
    WEATHER_BY_UNIT.clear()
    WEATHER_BY_UNIT.update(weather.get("by_unit", {}))
    WEATHER_QUEUE.clear()
    WEATHER_QUEUE.update({(t, u): w for t, u, w in weather.get("queue", [])})
    EVENT_QUEUE[:] = data.get("events", [])
    CAL_EVENTS.clear()
    CAL_EVENTS.extend(data.get("cal_events", []))
    VARS.clear()
    VARS.update({k: _unjson(v) for k, v in data.get("vars", {}).items()})
    FUNCTIONS.clear()
    FUNCTIONS.update({k: list(v) for k, v in data.get("functions", {}).items()})
    WORLD_HOOKS.clear()
    for name, rep in data.get("hooks", {}).items():
        WORLD_HOOKS[name] = Hook.from_nbt(*parse_item_text(rep)[:2])
    _hookify_all()   # fold any legacy proc/reaction/on_apply/on_clear in loaded elements into hooks
    HISTORY.clear()
    HISTORY.extend(data.get("history", []))


def _session_meta():
    """Map saved id -> friendly name (None if unnamed), read from each session file's header."""
    meta = {}
    for sid in _saved_session_ids():
        try:
            with open(_session_path(sid)) as handle:
                meta[sid] = json.load(handle).get("name")
        except (OSError, ValueError):
            meta[sid] = None
    return meta


def _resolve_session(arg):
    """An id (4-digit) or a friendly name -> the saved session id, or None."""
    arg = arg.strip()
    if arg.isdigit() and int(arg) in _saved_session_ids():
        return int(arg)
    for sid, name in _session_meta().items():  # match by friendly name
        if name and name == arg:
            return sid
    return None


def cmd_session(rest):
    """/session save [name]   save to sessions/<id>.json (id = this session's port); name is optional
       /session load <id|name>   load by 4-digit id or friendly name
       /session get | id | history | list
    The session id doubles as the UI port — open http://localhost:<id>."""
    global SESSION_ID, SESSION_NAME
    sub, _, arg = rest.partition(" ")
    sub, arg = sub.strip(), arg.strip()
    if sub == "save":
        sid = _ensure_session_id()
        if arg:
            SESSION_NAME = arg  # optional friendly name (stored inside the file)
        os.makedirs(SESSION_DIR, exist_ok=True)
        with open(_session_path(sid), "w") as handle:
            json.dump(_session_to_dict(), handle, indent=2)
        label = f" '{SESSION_NAME}'" if SESSION_NAME else ""
        print(f"  saved session {sid}{label} -> {_session_path(sid)}  ({len(WORLD)} characters)")
        print(f"  UI: http://localhost:{sid}")
        return sid
    if sub == "load":
        if not arg:
            print("  usage: /session load <id|name>")
            return False
        sid = _resolve_session(arg)
        if sid is None:
            print(f"  no saved session '{arg}' (try /session list)")
            return False
        with open(_session_path(sid)) as handle:
            loaded = json.load(handle)
        _session_from_dict(loaded)
        SESSION_ID, SESSION_NAME = sid, loaded.get("name")
        label = f" '{SESSION_NAME}'" if SESSION_NAME else ""
        print(f"  loaded session {sid}{label}  ({len(WORLD)} characters, world turn {TURN})")
        return sid
    if sub in ("get", "id"):
        sid = _ensure_session_id()
        label = f" '{SESSION_NAME}'" if SESSION_NAME else ""
        print(f"  session {sid}{label}  —  http://localhost:{sid}")
        return sid
    if sub == "history":
        if not HISTORY:
            print("  (history is empty)")
            return []
        for line in HISTORY:
            print(f"  {line}")
        return list(HISTORY)
    if sub == "list":
        meta = _session_meta()
        if not meta:
            print("  saved sessions: (none)")
            return []
        for sid in sorted(meta):
            print(f"  {sid}" + (f"  '{meta[sid]}'" if meta[sid] else ""))
        return sorted(meta)
    print("  usage: /session <save|load|get|id|history|list> [name]")
    return False


def _session_path(sid):
    return os.path.join(SESSION_DIR, f"{sid}.json")


# --- /execute (conditional command execution) --------------------------------
COMPARATORS = {
    ">": lambda a, b: a > b, "<": lambda a, b: a < b,
    ">=": lambda a, b: a >= b, "<=": lambda a, b: a <= b,
    "=": lambda a, b: a == b, "==": lambda a, b: a == b, "!=": lambda a, b: a != b,
}


def _operand_value(token):
    """Resolve an /execute operand: a dotted PATH (@s.stat.health, @p.x, name.attributes.y)
    -> that value; otherwise a literal (number/string). $(vars) are already substituted."""
    token = token.strip()
    if "." in token and not re.fullmatch(r"-?\d+\.\d+", token):  # a path, not a plain float
        head, _, path = token.partition(".")
        player = SELF if head == "@s" else (CURRENT[0][1] if head == "@p" and CURRENT else WORLD.get(head))
        if player is not None:
            target = _resolve_path(player, path)
            if target is not None:
                return target[0]()
            return player.field_value(path)
    return parse_value(token)


def _compare(left, op, right):
    fn = COMPARATORS.get(op)
    if fn is None:
        return None
    try:
        return fn(float(left), float(right))  # numeric when possible
    except (TypeError, ValueError):
        return fn(str(left), str(right)) if op in ("=", "==", "!=") else False


def _check_condition(negate, left, operator, right):
    """Evaluate one if/unless clause (operands resolved against the CURRENT @s)."""
    result = bool(_compare(_operand_value(left), operator, _operand_value(right)))
    return (not result) if negate else result


def _apply_store(store, result, owner):
    """Store a run-command's result/success into a var or an entity path (Minecraft execute store).
    store = (mode, 'var', name_token) | (mode, 'entity', selector, path)."""
    mode, target = store[0], store[1]
    if mode == "success":
        value = 0 if (result is None or result is False) else 1
    else:  # result: a bare failure (False) stores as blank/None
        value = None if result is False else result
    if target == "var":
        name, cast = _parse_var_target(store[2])
        if not name:
            print("  store var: bad $(name)")
            return
        VARS[name] = _format_var(_cast(value, cast))
        print(f"  stored $({name}) = {VARS[name]!r}")
    else:  # entity
        selector, path = store[2], store[3]
        targets = _resolved(selector)
        if targets is None:
            return
        for name, player in targets:
            resolved = _resolve_path(player, path)
            if resolved is None:
                print(f"  {name}: can't store into '{path}'")
                continue
            resolved[1](value)  # the setter
            print(f"  {name}.{path} = {value!r}")


def cmd_execute(rest):
    """/execute [as <sel>] [at <sel>|<x> <y>] [if|unless <l> <op> <r>] [store ...] run <command>
    Chains clauses left-to-right (Minecraft-style), transforming a set of (subject, position)
    contexts, then runs <command> once per surviving context:
      as <selector>            fork one context per matched entity, each with @s = it
      at <selector>            set the position context to that entity's cell (@s keeps its subject)
      at <x> <y>               set the position context to an explicit cell
                               (the position is the reference for @[distance=..]; e.g.
                                `/execute at @s run proc enable @a[distance=..2] rallied:true`)
      if <cond> / unless <cond>  keep only contexts where the condition is true / false
      store result|success <target>   capture the command's return value / 1|0 into:
          var $(name)[->cast]          a $() variable     (same as /data result|success)
          entity <selector> <path>     an entity's stats.X / attributes.X / nbt.X
    <left>/<right> are paths (@s.stat.health) or literals; <operator> is > < >= <= = != .
      /execute as noael store result var $(hp)->float run stat get @s health
      /execute store result entity mira stats.health run stat get noael health"""
    global SELF, AT_POS
    clauses, sep, command = rest.strip().partition(" run ")
    if not sep or not command.strip():
        print("  usage: /execute [as <sel>] [at <sel>|<x> <y>] [if|unless <l> <op> <r>] [store ...] run <command>")
        return False
    command = _substitute(command.strip())          # the run-command: substitute $() now
    tokens = clauses.split()
    # contexts = a list of (self, pos) the chain runs in; clauses transform it left-to-right (MC-style)
    contexts, store, i = [(SELF, AT_POS)], None, 0

    def in_ctx(ctx, fn):                            # run fn with SELF/AT_POS bound to this context
        global SELF, AT_POS
        save = (SELF, AT_POS)
        SELF, AT_POS = ctx
        try:
            return fn()
        finally:
            SELF, AT_POS = save

    while i < len(tokens):
        word = tokens[i]
        if word == "as" and i + 1 < len(tokens):    # fork one context per matched entity (sets @s)
            sel = _substitute(tokens[i + 1]); i += 2
            contexts = [(t, ctx[1]) for ctx in contexts for _n, t in (in_ctx(ctx, lambda: _resolve(sel)) or [])]
        elif word == "at" and i + 1 < len(tokens):  # set the position context (@[distance] measures from here)
            if i + 2 < len(tokens) and tokens[i + 1].lstrip("-").isdigit() and tokens[i + 2].lstrip("-").isdigit():
                pos = (int(tokens[i + 1]), int(tokens[i + 2])); i += 3
                contexts = [(ctx[0], pos) for ctx in contexts]
            else:
                sel = _substitute(tokens[i + 1]); i += 2
                contexts = [(ctx[0], _entity_pos(t)) for ctx in contexts for _n, t in (in_ctx(ctx, lambda: _resolve(sel)) or [])]
        elif word in ("if", "unless"):
            if len(tokens) - i < 4:
                print(f"  '{word}' needs: {word} <left> <operator> <right>"); return False
            neg, lt, op, rt = word == "unless", _substitute(tokens[i + 1]), tokens[i + 2], _substitute(tokens[i + 3]); i += 4
            contexts = [c for c in contexts if in_ctx(c, lambda: _check_condition(neg, lt, op, rt))]
        elif word == "store":
            if len(tokens) - i < 4 or tokens[i + 1] not in ("result", "success"):
                print("  store needs: store result|success var $(name) | entity <selector> <path>"); return False
            mode, ttype = tokens[i + 1], tokens[i + 2]
            if ttype == "var":
                store = (mode, "var", tokens[i + 3]); i += 4
            elif ttype == "entity":
                if len(tokens) - i < 5:
                    print("  store entity needs: store result|success entity <selector> <path>"); return False
                store = (mode, "entity", _substitute(tokens[i + 3]), tokens[i + 4]); i += 5
            else:
                print(f"  unknown store target '{ttype}' (use var or entity)"); return False
        else:
            print(f"  unexpected '{word}' in /execute (expected as / at / if / unless / store / run)")
            return False
    ran = False
    for ctx in contexts:
        result = in_ctx(ctx, lambda: dispatch(_as_command_line(command)))
        if store:
            _apply_store(store, result, ctx[0])
        ran = True
    if not ran:
        print("  condition(s)/selector left nothing to run")
    return ran


# --- /tellraw (formatted text output; Minecraft-inspired) --------------------
# Escapes: \n \t \r \\ . Style/color via '&' codes (Minecraft-style): &0-&9 &a-&f colors,
# &l bold &n underline &o italic &m strike &r reset. Usable directly inside a reaction/event.
TELLRAW_CODES = {
    "&0": "30", "&1": "34", "&2": "32", "&3": "36", "&4": "31", "&5": "35", "&6": "33", "&7": "37",
    "&8": "90", "&9": "94", "&a": "92", "&b": "96", "&c": "91", "&d": "95", "&e": "93", "&f": "97",
    "&l": "1", "&n": "4", "&o": "3", "&m": "9", "&r": "0",
}


def _tellraw_escapes(text):
    out, i = [], 0
    while i < len(text):
        if text[i] == "\\" and i + 1 < len(text):
            out.append({"n": "\n", "t": "\t", "r": "\r", "\\": "\\"}.get(text[i + 1], text[i + 1]))
            i += 2
        else:
            out.append(text[i])
            i += 1
    return "".join(out)


def _tellraw_format(text):
    used = False
    for code, ansi in TELLRAW_CODES.items():
        if code in text:
            text = text.replace(code, f"\033[{ansi}m")
            used = True
    return text + ("\033[0m" if used else "")


def cmd_tellraw(rest):
    """/tellraw <text> — print formatted text. \\n \\t escapes + &-codes (&c red, &l bold, &r reset).
    Use it directly in a reaction/event (e.g. /event force festival "/tellraw &6Harvest Fair!")."""
    text = rest.strip()
    if len(text) >= 2 and text[0] in "\"'" and text[-1] == text[0]:
        text = text[1:-1]
    rendered = _tellraw_format(_tellraw_escapes(text))
    print(rendered)
    return text


# --- /help (only 'formula' for now; other commands aren't finished) ----------
FORMULA_HELP = """  Formula expressions — evaluated and written into stats (see /formula).

  THE TARGET (left of '='):  a bare stat name — it's always written to stat.<name>.
    /formula new noael rage = stat.attack   creates/updates stat.rage   (the '=' is optional)

  REFERENCES (must be prefixed):
    stat.NAME   a stat   (also stats.NAME)   e.g. stat.health, stat.attack, stat.pierce_affinity
    (atr.NAME / attributes.NAME still work as ALIASES of stat. — stats are one merged namespace now)
    (bare names work only as LET variables; the tuning constants below are bare)

  OPERATORS:  + - * /    ^ or ** (power)    -x (negate)    n! (factorial, postfix)
              comparisons: > < >= <= ==   (each yields 1 or 0)

  FUNCTIONS:
    IF(cond, a, b)         -> a if cond else b
    LET(name, val, body)   -> bind name = val, then evaluate body (a local variable)
    DELTA(ref)             -> how much ref changed since the last recompute (counts once)
    MIN(a, b, ...)   MAX(a, b, ...)
    CLAMP(x, lo, hi)       -> keep x within [lo, hi]
    ABS(x)   FLOOR(x)   CEIL(x)   SQRT(x)
    EXP(x)                 -> e^x  (Euler's number ~2.71828 raised to a power)
    FACT(n)  or  n!        -> factorial:  FACT(5) = 5! = 120  (gamma for non-integers; n<0 -> 0)
    ROUND(x)               -> nearest whole number              ROUND(1.6) = 2
    ROUND(x, places)       -> round to that many decimal places:
                              ROUND(1.234, 0) = 1   ROUND(1.234, 1) = 1.2   ROUND(1.234, 2) = 1.23

  TUNING CONSTANTS (edit in containers.FORMULA_CONSTANTS):
    {constants}

  EXAMPLES:
    stat.attack * IF(stat.pierce_affinity > 1, stat.pierce_affinity^AFFINITY_TAX, stat.pierce_affinity)
    LET(p, 1 - ((1 - stat.physical_penetration) * (1 - stat.slash_penetration_base)),
        IF(p > stat.slash_penetration_cap, stat.slash_penetration_cap, p))
    MIN(stat.health + stat.health_regen * DELTA(stat.turn), stat.max_health)   # heal per turn, capped

  NOTES:
    - A formula referencing its own stat (stat.health inside the health formula) reads the
      current value, so it accumulates. DELTA(...) prevents per-turn runaway.
    - Any error (unknown ref, divide-by-zero, factorial of a negative) makes that stat 0.
    - Set per player:  /formula new <selector> <stat> = <expression>"""


def cmd_help(rest):
    topic = (rest.split() or [""])[0]
    if topic == "formula":
        constants = "   ".join(f"{k}={v}" for k, v in FORMULA_CONSTANTS.items())
        print(FORMULA_HELP.format(constants=constants))
        return True
    print("  /help is only available for 'formula' right now (other commands aren't finished yet).")
    return False


def cmd_storyline(rest):
    """/storyline add <selector> <turn> <text>     add a story entry at a world turn (markdown ok)
       /storyline edit <selector> <id> <text>      replace an entry's text (id from /storyline list)
       /storyline remove <selector> <id>           delete an entry
       /storyline list <selector>                  show entries + notes
       /storyline note <selector> <text>           set the DM notes (private)
       /storyline summary <selector> <text>        set the player's 'last turn summary'
    Per-player: entries live in nbt.storyline [{id,turn,text}]; notes in nbt.dm_notes / nbt.last_summary.
    Text is taken literally (no dice/$() substitution) so prose and markdown survive."""
    sub, _, arg = rest.partition(" ")
    sub = sub.strip()
    sel, remainder = _split_selector(arg)
    targets = _resolved(sel)
    if targets is None:
        return False
    if sub == "list":
        for name, p in targets:
            entries = sorted(p.nbt.get("storyline") or [], key=lambda e: e.get("turn", 0))
            print(f"  {name}.storyline: {len(entries)} entr{'y' if len(entries) == 1 else 'ies'}")
            for e in entries:
                print(f"    [{e.get('id')}] turn {e.get('turn')}: {str(e.get('text', '')).splitlines()[0][:60] if e.get('text') else ''}")
            if p.nbt.get("dm_notes"):
                print(f"    dm_notes: {str(p.nbt['dm_notes']).splitlines()[0][:60]}")
            if p.nbt.get("last_summary"):
                print(f"    last_summary: {str(p.nbt['last_summary']).splitlines()[0][:60]}")
        return True
    if sub == "add":
        turn_tok, _, text = remainder.partition(" ")
        if not turn_tok.lstrip("-").isdigit() or not text.strip():
            print("  usage: /storyline add <selector> <turn> <text>")
            return False
        for name, p in targets:
            entries = p.nbt.setdefault("storyline", [])
            entries.append({"id": max((e.get("id", 0) for e in entries), default=0) + 1,
                            "turn": int(turn_tok), "text": text})
            print(f"  {name}: story entry added at turn {turn_tok}")
        return True
    if sub in ("edit", "remove"):
        id_tok, _, text = remainder.partition(" ")
        if not id_tok.lstrip("-").isdigit():
            print(f"  usage: /storyline {sub} <selector> <id> {'<text>' if sub == 'edit' else ''}")
            return False
        sid, ok = int(id_tok), False
        for name, p in targets:
            entries = p.nbt.get("storyline") or []
            entry = next((e for e in entries if e.get("id") == sid), None)
            if entry is None:
                print(f"  {name}: no story entry [{sid}]")
                continue
            if sub == "remove":
                entries.remove(entry)
                print(f"  {name}: removed story entry [{sid}]")
                ok = True
            elif text.strip():
                entry["text"] = text
                print(f"  {name}: edited story entry [{sid}]")
                ok = True
            else:
                print("  usage: /storyline edit <selector> <id> <text>")
        return ok
    if sub in ("note", "summary"):
        key = "dm_notes" if sub == "note" else "last_summary"
        for name, p in targets:
            p.nbt[key] = remainder
            print(f"  {name}: {sub} set")
        return True
    print("  usage: /storyline <add|edit|remove|list|note|summary> <selector> ...")
    return False


ALLEGIANCE_KINDS = ("ally", "neutral", "hostile")


def cmd_allegiance(rest):
    """/allegiance <player> <target> <ally|neutral|hostile>   how <player> regards <target> — per-player,
                                       so a mob can be hostile to one PC and an ally to another. Drives the
                                       atlas dot color from that player's viewpoint. Unlisted = neutral.
       /allegiance <player> list             show this player's set allegiances
       /allegiance <player> clear <target>   remove an entry (back to neutral)"""
    parts = rest.split()
    if len(parts) < 2:
        print("  usage: /allegiance <player> <target> <ally|neutral|hostile> | list | clear <target>")
        return False
    pname, player = parts[0], WORLD.get(parts[0])
    if player is None:
        print(f"  no character '{pname}'")
        return False
    table = player.nbt.get("allegiance")
    if not isinstance(table, dict):
        table = {}
        player.nbt["allegiance"] = table
    if parts[1] == "list":
        if not table:
            print(f"  {pname}: no allegiances set (everyone defaults to neutral)")
        for target, disp in table.items():
            print(f"  {pname} -> {target}: {disp}")
        return True
    if parts[1] in ("clear", "remove") and len(parts) >= 3:
        table.pop(parts[2], None)
        print(f"  {pname}: {parts[2]} reset to neutral")
        return True
    if len(parts) < 3 or parts[2] not in ALLEGIANCE_KINDS:
        print("  usage: /allegiance <player> <target> <ally|neutral|hostile>")
        return False
    table[parts[1]] = parts[2]
    print(f"  {pname} now regards {parts[1]} as {parts[2]}")
    return True


def cmd_combat(rest):
    """/combat rule <selector> <channel> = <formula>   set a PER-PLAYER combat rule (overrides the
                                       shared combat_rules.csv for that player only). The formula uses
                                       atk.raw / atk.pen / def.res and MAY read the attacker's own stats
                                       (e.g. stat.crit_chance), which resolve per-player.
       /combat rule  <selector> <channel> reset    drop the override (back to the shared default)
       /combat rules <selector>                     list this player's rule overrides
    Channels are the combat.csv channels (physical, magic, …); unset channels use the default."""
    sub, _, arg = rest.partition(" ")
    sub = sub.strip()
    sel, remainder = _split_selector(arg.strip())
    targets = _resolved(sel)
    if targets is None:
        return False
    if sub in ("rules", "list"):
        for name, player in targets:
            own = player.nbt.get("combat_rules") if isinstance(player.nbt.get("combat_rules"), dict) else {}
            print(f"  {name}.combat_rules:")
            for ch in sorted(set(COMBAT_RULES) | set(own)):
                src = own.get(ch); print(f"    {ch} = {src}  (override)" if src else f"    {ch} = {COMBAT_RULES.get(ch)}  (default)")
        return True
    if sub == "rule":
        parts = remainder.split(None, 1)
        channel = parts[0] if parts else ""
        body = (parts[1].strip() if len(parts) > 1 else "")
        if not channel:
            print("  usage: /combat rule <selector> <channel> = <formula>  |  <channel> reset")
            return False
        for name, player in targets:
            table = player.nbt.setdefault("combat_rules", {})
            if body == "" or body.lower() == "reset":
                table.pop(channel, None)
                print(f"  {name}: combat rule '{channel}' reset to default")
            else:
                expr = body[1:].strip() if body.startswith("=") else body
                table[channel] = expr
                print(f"  {name}: combat rule '{channel}' = {expr}")
        return True
    print("  usage: /combat rule <selector> <channel> = <formula> | <channel> reset  ·  /combat rules <selector>")
    return False


def cmd_coverage(rest):
    """/coverage <player> <slot> <pct>   set a PER-PLAYER body-coverage % for an equip slot (overrides the
       shared body_coverage.csv default for that player). /coverage <player> <slot> reset · <player> list."""
    parts = rest.split()
    if not parts:
        print("  usage: /coverage <player> <slot> <pct|reset>  |  /coverage <player> list")
        return False
    player = WORLD.get(parts[0])
    if player is None:
        print(f"  no character '{parts[0]}'")
        return False
    table = player.nbt.get("coverage")
    if not isinstance(table, dict):
        table = {}
        player.nbt["coverage"] = table
    if len(parts) < 2 or parts[1] == "list":
        if not table:
            print(f"  {parts[0]}: no coverage overrides (using body_coverage.csv defaults)")
        for slot, val in table.items():
            print(f"  {parts[0]}.coverage.{slot} = {val}%  (override)")
        return True
    if len(parts) < 3:
        print("  usage: /coverage <player> <slot> <pct|reset>")
        return False
    slot = parts[1]
    if parts[2].lower() == "reset":
        table.pop(slot, None)
        print(f"  {parts[0]}: coverage '{slot}' reset to default")
        return True
    try:
        table[slot] = float(parts[2])
    except ValueError:
        print("  pct must be a number (or 'reset')")
        return False
    print(f"  {parts[0]}: coverage '{slot}' = {table[slot]}%")
    return True


# --- dispatch ----------------------------------------------------------------

HANDLERS = {
    "character": cmd_character,
    "stat": cmd_stat,
    "attack": cmd_attack,
    "move": cmd_move,
    "formula": cmd_formula,
    "item": cmd_item,
    "container": cmd_container,
    "kill": cmd_kill,
    "turn": cmd_turn,
    "talent": cmd_talent,
    "effect": cmd_effect,
    "ability": cmd_ability,
    "proc": cmd_proc,
    "hook": cmd_hook,
    "data": cmd_data,
    "function": cmd_function,
    "calendar": cmd_calendar,
    "event": cmd_event,
    "map": cmd_map,
    "summon": cmd_summon,
    "session": cmd_session,
    "execute": cmd_execute,
    "tellraw": cmd_tellraw,
    "allegiance": cmd_allegiance,
    "combat": cmd_combat,
    "coverage": cmd_coverage,
    "random": cmd_random,
    "quest": cmd_quest,
    "storyline": cmd_storyline,
    "structure": cmd_structure,
    "uuid": cmd_uuid,
    "help": cmd_help,
}


# Player members that are sub-CONTAINERS (drilled into), not plain fields.
# Singular container names (+ the attribute->stat synonym). These read as container paths in a bare
# dotted read like `hero.stat.health`; anything else at that position is a plain field read.
_PATH_SEGMENTS = {"stat", "attribute", "attributes", "equipment", "inventory",
                  "talent", "ability", "effect", "quest", "formula", "nbt", "proc_state"}


def _resolve_and_print(path):
    """Read a bare dotted path: hero, hero.name, hero.stat.health. Returns the value
    (so /var result can capture it); returns False if it can't resolve."""
    parts = path.split(".")
    player = WORLD.get(parts[0])
    if player is None:
        print(f"  no character named '{parts[0]}'")
        return False
    if len(parts) == 1:
        print(f"  {player}")
        return player.name
    # hero.<field>: any non-container second segment is a field read — missing -> None,
    # matching `/character get` (don't error just because the key isn't set).
    if len(parts) == 2 and parts[1] not in _PATH_SEGMENTS:
        value = player.field_value(parts[1])
        print(f"  {path} = {value!r}")
        return value
    obj = player
    for seg in parts[1:]:
        if obj is player:
            seg = _canon_container(seg)  # stats/attributes/... -> the real singular attribute
        if hasattr(obj, seg):
            obj = getattr(obj, seg)
        elif hasattr(obj, "__getitem__") and hasattr(obj, "__contains__") and seg in obj:
            obj = obj[seg]
        else:
            print(f"  can't resolve '{seg}' in '{path}'")
            return False
    # a Stat/Attribute resolves to its value; everything else prints as-is
    if hasattr(obj, "value") and hasattr(obj, "name"):
        obj = obj.value
    print(f"  {path} = {obj!r}")
    return obj


def handle_bare_path(expr):
    """No leading slash: a set 'name.field(value)' or a get 'name(.seg)*'."""
    expr = expr.strip()
    setter = re.match(r"^(\w+)\.(\w+)\((.*)\)$", expr)
    if setter:
        return _character_set(setter.group(1), setter.group(2), parse_value(setter.group(3)))
    if re.match(r"^\w+(\.\w+)*$", expr):
        return _resolve_and_print(expr)
    print("  commands start with '/'. To read a value try:  hero.inventory_slots")


def _read_choice(prompt):
    """Read a SINGLE keypress (no Enter) when interactive; fall back to input() when piped.
    Echoes the key so the transcript shows the choice."""
    print(prompt, end="", flush=True)
    if not sys.stdin.isatty():
        try:
            return (input() or "n").strip().lower()[:1]
        except EOFError:
            print(); return "n"
    import termios
    import tty
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        ch = sys.stdin.read(1)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
    print(ch)  # echo + newline
    return ch.lower()


def _confirm_or_undo(snapshot, warnings):
    """Input had parse problems. Show them, then offer (single keypress): y = revert & retry,
    n = keep it anyway, c = create the referenced-but-missing function(s) now (then keep)."""
    print("  heads up — some input wasn't understood:")
    for warning in warnings:
        print(f"    - {warning}")
    missing = list(_MISSING_FNS)
    _MISSING_FNS.clear()
    if not sys.stdin.isatty():  # UI / piped: no way to answer a prompt — keep + show the warnings (chosen behavior)
        if missing:
            print("  (kept — function(s) not defined yet: " + ", ".join(missing) + " — create with /function edit)")
        else:
            print("  (kept — fix the above when you're ready)")
        return True
    label = "[y = revert / n = keep" + (" / c = create the missing function(s) now" if missing else "") + "]"
    answer = _read_choice(f"  undo this change? {label}: ")
    if answer in ("y", "yes"):
        WORLD.clear()
        WORLD.update(snapshot)
        print("  reverted — nothing was changed.")
        return False
    if missing and answer == "c":
        print("  kept the change — now create the function(s):")
        for fn in missing:
            _function_editor(fn)  # the in-REPL editor, one per missing function
        return True
    print("  kept as-is.")
    return True


# Serializes every command so the UI (server thread) and a live terminal shell (main thread) can't
# mutate WORLD at the same time. Reentrant: a function/execute body re-enters dispatch on the SAME
# thread without deadlocking; only cross-thread calls actually wait.
_DISPATCH_LOCK = threading.RLock()


def dispatch(line):
    with _DISPATCH_LOCK:
        return _dispatch(line)


def _dispatch(line):
    if not line.startswith("/"):
        if re.fullmatch(r"\s*d\d+(\s+d\d+)*\s*", line):  # bare dice: 'd20' or 'd2 d3 d4' -> rolls
            rolls = [str(_roll(int(n))) for n in re.findall(r"d(\d+)", line)]
            print("  " + " ".join(rolls))
            return rolls
        return handle_bare_path(_substitute(line))
    word, _, rest = line[1:].partition(" ")
    full = autofill.complete_command("/" + word)  # expand abbreviations: /char -> /character
    name = full[1:] if full else word
    handler = HANDLERS.get(name)
    if name not in ("data", "execute", "function", "storyline"):
        rest = _substitute(rest)  # /data, /execute, /function do their own / no substitution
        #   (so a $(name) being DEFINED stays literal while operands/commands get substituted;
        #    /function bodies are stored LITERALLY and substituted at run time, not at edit time)
    if handler:
        parse_warnings()  # clear any stale warnings before running
        _MISSING_FNS.clear()
        snapshot = copy.deepcopy(WORLD)  # so we can undo on a parse problem or a crash
        try:
            result = handler(rest)
        except Exception as error:  # a handler blew up mid-command — revert so nothing is half-applied
            WORLD.clear()
            WORLD.update(snapshot)
            print(f"  command failed: {error}  (no changes made)")
            return False
        warnings = parse_warnings()
        if warnings:
            kept = _confirm_or_undo(snapshot, warnings)
            if not kept:
                return False
        _ensure_session_uuids()  # anything just initiated (by any path) gets a session-unique uuid
        _log(f"/{name} {rest}".rstrip())  # turn-stamped command log (rest is post-substitution: shows rolls/vars)
        return result
    if "/" + name in autofill.suggest_command("/" + word):
        print(f"  /{name} is a known command but isn't wired up yet")
    else:
        print(f"  unknown command '/{word}'. Known: {', '.join('/' + c for c in autofill._commands)}")
    return False


# --- LIVE TAB COMPLETION (readline) ------------------------------------------
COLLECTION_CMDS = {"talent": "talent", "effect": "effect", "ability": "ability"}
VALUE_CMDS = {"stat": "stat"}
SUBCOMMANDS = {
    "character": ["new", "modify", "get", "show", "list", "remove"],
    "item": ITEM_SUBS,  # 'new' is canonical now (add/give still work as hidden aliases)
    "container": CONTAINER_SUBS,
    "turn": ["query", "next", "add", "set"],  # turn 'add' = + operator, stays
    **{cmd: ["new", "list", "modify", "remove"] for cmd in COLLECTION_CMDS},
    "ability": ["new", "list", "modify", "remove", "cast"],
    "function": ["edit", "run", "list", "show", "remove"],
    "calendar": ["show", "add", "set", "event", "date"],  # calendar 'add' = move time (operator)
    "map": ["generate", "expand", "look", "dim", "hide", "view", "pos", "get", "set", "new", "recommend"],
    "allegiance": ["list", "clear", "ally", "neutral", "hostile"],
    "combat": ["rule", "rules"],
    "hook": ["new", "list", "modify", "delete", "attach", "fire"],
    "coverage": ["list"],
    "session": ["save", "load", "get", "history", "list"],
    "quest": ["new", "complete", "modify", "delete", "list", "objective"],
    "storyline": ["add", "edit", "remove", "list", "note", "summary"],
    "structure": ["new", "delete", "modify", "list"],
    "uuid": ["get", "lookup", "set", "list"],
    **{cmd: ["new", "get", "list", "modify", "remove"] for cmd in VALUE_CMDS},  # base|multiplier|flat live UNDER modify
}
OPS3 = ["set", "add", "reset"]


def _selectors():
    return list(WORLD) + SELECTOR_TOKENS


def _relevant_procs(selector_token):
    """Proc names that matter for this selector: those its talents react to + any it has
    currently active. These show first; the full proc_tree namespace is the fallback."""
    player = _selector_player(selector_token)
    if player is None:
        return []
    names = set(player.proc_state)
    for talent in player.talent:   # legacy talents (pre-migration) may still carry a proc clause
        if isinstance(getattr(talent, "proc", None), Proc):
            for clause in talent.proc.clauses:
                names.update(clause)
    for name, _kind in _owner_hook_pairs(player):   # flags now live in the hooks the player wears
        hook = WORLD_HOOKS.get(name)
        if hook and isinstance(hook.condition, Proc):
            for clause in hook.condition.clauses:
                names.update(clause)
    return sorted(names)


def _buffer_player(buffer):
    """The player named somewhere in the command line (for path-key autofill); @p/@s too."""
    for token in buffer.split():
        player = _selector_player(token)
        if player is not None:
            return player
    return None


def _path_members(player, head):
    """Member names of a container for path autofill: stat/attribute names, item type_ids/names,
    talent/effect/ability names, or player.nbt keys."""
    container = getattr(player, _canon_container(head), None) if player else None
    if container is None:
        return []
    if isinstance(container, dict):
        return list(container)
    members = getattr(container, "items", None)
    if members is None:
        try:
            members = list(container)
        except TypeError:
            return []
    out = []
    for member in members:
        if head == "inventory" and getattr(member, "type_id", ""):
            out.append(member.type_id)
        if getattr(member, "name", ""):
            out.append(member.name)
    return list(dict.fromkeys(out))


def _path_fields(player, head, member_name):
    """The settable fields of a member (e.g. an item's keys) for the 3rd path level."""
    container = getattr(player, _canon_container(head), None) if player else None
    if container is None or not hasattr(container, "get"):
        return []
    return _object_keys(container.get(member_name))


def _in_path_bracket(buffer):
    """If the cursor is inside an unclosed reaction:[ or cost:[ , return that key, else None."""
    open_at = buffer.rfind("[")
    if open_at == -1 or "]" in buffer[open_at:]:
        return None
    match = re.search(r"(\w+)\s*:\s*$", buffer[:open_at])
    return match.group(1) if match and match.group(1) in ("reaction", "cost") else None


def _path_completions(text, buffer):
    """Complete a class-path inside reaction:[ / cost:[ , drilling in with '.':
       container -> stats/attributes/inventory/...   (PATH_CONTAINERS)
       container.member -> stat names / item type_ids / ...
       container.member.field -> the member's fields (e.g. inventory.rope.count)"""
    if "[" in text:
        prefix, partial = text[:text.rindex("[") + 1], text[text.rindex("[") + 1:]
    else:
        prefix, partial = "", text
    if "(" in partial:  # already typing the amount; nothing to complete
        return []
    player = _buffer_player(buffer)
    segs = partial.split(".")
    if len(segs) == 1:
        comps = [c for c in PATH_CONTAINERS if c.startswith(segs[0])]
    elif len(segs) == 2:
        comps = [f"{segs[0]}.{m}" for m in _path_members(player, segs[0]) if m.startswith(segs[1])]
    elif len(segs) == 3:
        comps = [f"{segs[0]}.{segs[1]}.{f}" for f in _path_fields(player, segs[0], segs[1]) if f.startswith(segs[2])]
    else:
        comps = []
    return [prefix + c for c in comps]


def _selector_player(token):
    """For autofill: resolve a selector token to ONE representative player so TAB can list its
    members. Handles @s (self), @p (previous), and a plain name; groups -> None."""
    if token == "@s":
        return SELF
    if token == "@p":
        return CURRENT[0][1] if CURRENT else None
    return WORLD.get(token)


def _member_pool(selector_token, container_attr):
    """Names/type_ids/indices of a player's items, talents, stats, etc. — so TAB on an
    <obj>/<name> position lists what's actually there (like a quick list)."""
    player = _selector_player(selector_token)
    if player is None:
        return []
    container = getattr(player, container_attr)
    members = getattr(container, "items", None)
    if members is None:
        members = list(container)
    pool = []
    for i, member in enumerate(members):
        pool.append(f"[{i}]")
        if container_attr == "inventory" and getattr(member, "type_id", ""):
            pool.append(member.type_id)
        if getattr(member, "name", ""):
            pool.append(member.name)
    return list(dict.fromkeys(pool))


def _object_keys(obj):
    """The NBT/field keys you can set on an object — its type's known keys plus any
    dynamic keys it currently carries. Powers field-name autofill after set/add/reset."""
    if obj is None:
        return []
    keys = list(getattr(type(obj), "GENERIC_NBT", [])) + list(getattr(type(obj), "NBT", []))
    keys += list(getattr(obj, "nbt", {}).keys())
    return [k for k in dict.fromkeys(keys) if k != "uuid"]  # uuid is managed via /uuid, not editable


def _item_keys(selector_token, obj_sel):
    player = _selector_player(selector_token)
    if player is None:
        return []
    item, _count, _error = _select_item(player.inventory, obj_sel)
    return _object_keys(item)


def _member_keys(selector_token, container_attr, ident):
    player = _selector_player(selector_token)
    if player is None:
        return []
    return _object_keys(getattr(player, container_attr).get(ident))


def _completion_pool(cmd, args_before):
    """Candidate pool for the NEXT arg, given the command and the args already typed after it.
    Handles subcommands, selectors (incl. @a/@!a), <obj> members, op keywords, and field keys."""
    sel = _selectors()
    if cmd == "kill":
        if len(args_before) == 0:
            return sel
        if len(args_before) == 1:
            return ["true", "false"]  # true = remove entirely; default false = health 0 + pulse died
        return []
    if cmd == "attack":
        if len(args_before) == 0:
            return sel                         # attacker
        if len(args_before) == 1:
            return sel                         # target
        if len(args_before) == 2:
            return ["with"]                    # then a weapon, {type:amount}, or a number
        return []
    if cmd == "move":
        return sel if len(args_before) == 0 else []  # mover; then <x> <y>
    if cmd == "turn":
        if not args_before:
            return SUBCOMMANDS["turn"]
        if args_before[0] == "next" and len(args_before) == 1:
            return ["cycle"] + sel  # cycle, or a selector to advance just that player
        return []
    if cmd == "help":
        return ["formula"] if not args_before else []  # only formula documented for now
    if cmd == "data":
        return ["result", "success", "set", "list"] if not args_before else []
    if cmd == "random":
        return ["range"] if not args_before else []  # the '<min>..<max>' is typed (TAB inserts '..')
    if cmd == "quest":
        if not args_before:
            return SUBCOMMANDS["quest"]
        sub, after = args_before[0], args_before[1:]
        if len(after) == 0:
            return sel
        if sub in ("complete", "modify", "delete", "objective") and len(after) == 1:
            return _member_pool(after[0], "quest")
        if sub == "modify" and len(after) == 2:
            return OPS3
        if sub == "modify" and len(after) == 3:
            return _member_keys(after[0], "quest", after[1])
        if sub == "delete" and len(after) == 2:
            return ["true", "false"]
        if sub == "objective" and len(after) == 2:           # /quest objective <sel> <quest> <verb>
            return ["list", "add", "clear"]
        if sub == "objective" and len(after) == 3 and after[2] == "add":
            return list(OBJECTIVE_TYPES)                      # add binary|segment|percent
        return []
    if cmd == "uuid":
        if not args_before:
            return SUBCOMMANDS["uuid"]
        sub, after = args_before[0], args_before[1:]
        if sub == "get" and len(after) == 0:
            return sel
        if sub in ("lookup", "set") and len(after) == 0:
            return sorted(_uuids_in_use())  # an existing uuid
        if sub == "list" and len(after) == 0:
            return sorted({u[:2] for u in _uuids_in_use()})  # the 2-letter type codes in use
        return []
    if cmd == "structure":
        if not args_before:
            return SUBCOMMANDS["structure"]
        sub, after = args_before[0], args_before[1:]
        if sub in ("new", "delete", "modify") and len(after) == 0:
            return sorted({s.name for s in STRUCTURES})  # existing structure names (new: make another)
        if sub == "modify" and len(after) == 1:
            return OPS3
        return []  # trailing x,y cells (new) are free-form
    if cmd == "formula":
        if not args_before:
            return ["new", "modify", "remove", "list", "recompute"]
        sub, after = args_before[0], args_before[1:]
        if len(after) == 0:
            return sel
        if sub in ("new", "modify", "remove") and len(after) == 1:
            player = _selector_player(after[0])
            existing = list(player.formula) if player else []
            return list(dict.fromkeys(STAT_NAMES + existing))
        if sub in ("new", "modify") and len(after) == 2:
            return ["= "]  # the formula separator, with a trailing space to start typing
        return []
    if cmd == "proc":
        if not args_before:
            return ["pulse", "enable", "disable", "query"]
        if len(args_before) == 1:
            return sel
        return PROC_NAMES  # the proc names from proc_tree.json (suggestions; custom allowed)
    if cmd == "calendar":
        if not args_before:
            return ["show", "add", "set", "event", "date"]
        if args_before[0] in ("add", "set") and len(args_before) == 1:
            return list(CAL_UNITS)
        if args_before[0] == "event" and len(args_before) == 1:
            return ["add", "list", "clear"]
        if args_before[0] == "show" and len(args_before) == 1:
            return ["at"]
        return []
    if cmd == "map":
        if not args_before:
            return SUBCOMMANDS["map"]
        sub, after = args_before[0], args_before[1:]
        if sub == "expand" and len(after) == 0:
            return ["north", "south", "east", "west"]
        if sub in ("look", "scout", "reveal", "dim", "hide", "unsee", "pos", "view") and len(after) == 0:
            return sel       # a player/selector
        return []
    if cmd == "combat":
        if not args_before:
            return SUBCOMMANDS["combat"]
        sub, after = args_before[0], args_before[1:]
        if len(after) == 0:
            return sel
        if sub == "rule" and len(after) == 1:
            return sorted(COMBAT_RULES)   # channel names (physical, magic, …)
        return []
    if cmd == "coverage":
        if not args_before:
            return sel
        if len(args_before) == 1:
            return ["list"] + sorted(BODY_COVERAGE)   # a slot name (or 'list')
        return []
    if cmd == "summon":
        if not args_before:
            return list(BESTIARY)  # known species (free-form names also allowed)
        return [] if args_before[-1] in ("count", "at") else ["count", "at"]
    if cmd == "event":
        if not args_before:
            return ["list", "clear", "queue", "force"]
        sub = args_before[0]
        if sub in ("list", "clear") and len(args_before) == 1:
            return EVENT_CATEGORIES
        if sub in ("queue", "force") and len(args_before) == 1:
            return EVENT_CATEGORIES
        if sub in ("queue", "force") and len(args_before) == 2 and args_before[1] == "weather":
            return [w["weather"] for w in WEATHER_TABLE]  # weather names
        return []
    if cmd == "execute":
        a = args_before
        if not a:
            return ["as", "if", "unless", "store"]
        last = a[-1]
        if last in ("as", "if", "unless"):
            return sel  # a selector (for 'as') or the left operand
        if last == "store":
            return ["result", "success"]
        if last in ("result", "success") and len(a) >= 2 and a[-2] == "store":
            return ["var", "entity"]
        if last == "entity":
            return sel  # the entity to store into
        if len(a) >= 2 and a[-2] in ("if", "unless"):
            return [">", "<", ">=", "<=", "=", "!="]  # operator after a left operand
        return ["run", "if", "unless", "store"]  # after an operand/selector: chain more or run
    if cmd == "session":
        if not args_before:
            return ["save", "load", "get", "id", "history", "list"]
        if args_before[0] == "load" and len(args_before) == 1:  # ids + friendly names
            meta = _session_meta()
            return [str(s) for s in sorted(meta)] + [n for n in meta.values() if n]
        return []
    if cmd == "function":
        if not args_before:
            return ["edit", "run", "list", "show", "remove"]
        sub, after = args_before[0], args_before[1:]
        if sub in ("edit", "run", "show", "remove") and len(after) == 0:
            return list(FUNCTIONS)
        if sub == "run" and len(after) == 1:
            return sel  # the selector to run it on
        return []
    if cmd in ("character", "item", "container") or cmd in COLLECTION_CMDS or cmd in VALUE_CMDS:
        if not args_before:
            return SUBCOMMANDS.get(cmd, [])
        sub, after = args_before[0], args_before[1:]
        if cmd == "character":
            if sub in ("modify", "get", "show", "remove") and len(after) == 0:
                return sel
            if sub == "modify" and len(after) == 1:
                return OPS3
            if sub == "modify" and len(after) == 2:
                return _object_keys(_selector_player(after[0]))
            if sub == "get" and len(after) == 1:
                return _object_keys(_selector_player(after[0]))
        elif cmd == "item":
            # every item subcommand whose FIRST arg is a selector (incl. the add/give aliases of new)
            if sub in ("new", "add", "give", "equip", "unequip", "use", "remove", "list", "modify", "place", "rotate") and len(after) == 0:
                return sel
            if sub == "loot" and len(after) == 1:
                return sel
            if sub in ("remove", "modify", "equip", "use", "place", "rotate") and len(after) == 1:
                return _member_pool(after[0], "inventory")  # the item (equip/use pulls from inventory)
            if sub == "place" and len(after) == 2:
                return [str(c.get("id")) for c in _effective_inventory_space(_selector_player(after[0]))] if _selector_player(after[0]) else []
            if sub == "modify" and len(after) == 2:
                return OPS3
            if sub == "modify" and len(after) == 3:
                return _item_keys(after[0], after[1])
        elif cmd == "container":
            if sub in ("new", "modify", "delete", "list") and len(after) == 0:
                return sel
            if sub in ("modify", "delete") and len(after) == 1:
                p = _selector_player(after[0])
                return [str(c.get("id")) for c in _base_containers(p)] if p else []
            if sub == "modify" and len(after) == 2:
                return OPS3
            if sub == "modify" and len(after) == 3:
                return ["label", "color", "style", "cells", "id"]
        elif cmd in COLLECTION_CMDS:
            container_attr = COLLECTION_CMDS[cmd]
            if sub in ("new", "list", "modify", "remove", "cast") and len(after) == 0:
                return sel
            if sub in ("modify", "remove", "cast") and len(after) == 1:
                return _member_pool(after[0], container_attr)
            if cmd == "ability" and sub == "cast" and len(after) == 2:
                return sel  # the target to cast at (on_hit applies here)
            if sub == "modify" and len(after) == 2:
                return OPS3
            if sub == "modify" and len(after) == 3:
                return _member_keys(after[0], container_attr, after[1])
        elif cmd in VALUE_CMDS:
            container_attr = VALUE_CMDS[cmd]
            schema = STAT_NAMES
            CH = ["base", "multiplier", "flat"]    # layered-modify channels (mult is a hidden alias)
            if sub in ("new", "get", "list", "modify", "remove") and len(after) == 0:
                return sel
            if sub == "modify" and len(after) == 1:
                return CH        # pick the layer FIRST (base|multiplier|flat) — then the stat name
            if sub in ("new", "get", "remove") and len(after) == 1:
                return _value_name_pool(after[0], container_attr, schema)
            if sub == "modify" and len(after) == 2:              # channel -> stat name; else -> the op
                return _value_name_pool(after[0], container_attr, schema) if after[1] in STAT_CHANNELS else OPS3
            if sub == "modify" and len(after) == 3 and after[1] in STAT_CHANNELS:
                return OPS3                                      # /stat modify <sel> <channel> <stat> <op>
        return []
    return []


def _value_name_pool(selector_token, container_attr, schema):
    """Stat/attribute name suggestions: the generic schema (in order) plus any custom names
    the player already has that aren't in the schema."""
    player = _selector_player(selector_token)
    existing = [m.name for m in getattr(player, container_attr)] if player else []
    return list(dict.fromkeys(list(schema) + existing))


def _active_type(buffer):
    """The item type_id whose NBT braces the cursor is inside, or None."""
    if buffer.count("{") <= buffer.count("}"):
        return None
    before = buffer[:buffer.rfind("{")]
    match = re.search(r"([A-Za-z_]\w*)\s*$", before)
    return match.group(1) if match else None


def _var_name_pool(text):
    """Autofill the /var name slot $(name)[->type]:
       '' or '$'      -> '$('    (start the variable)
       '$(x'         -> '$(x)'    (close the paren)
       '$(x)' / '$(x)-' -> insert '->' and (via the display hook) list the bare datatypes"""
    if not text or text == "$":
        return ["$("]
    if text.startswith("$(") and ")" not in text:
        return [text + ")"]
    match = re.fullmatch(r"(\$\(\w+\))-?", text)  # right after ) or after a lone '-'
    if match:
        return [f"{match.group(1)}->{t}" for t in DATATYPES]  # common prefix '$(x)->' is inserted
    return []


# Classes whose NBT autofill is grouped into tiers (primary / secondary / ...).
# A trailing group catches any GENERIC_NBT key not placed in a named tier (e.g. inventory_slots).
TIER_MAP = {}
for _cls in (Player,):
    _tiers = getattr(_cls, "NBT_TIERS", None)
    if _tiers:
        _flat = {k for tier in _tiers for k in tier}
        _leftover = [k for k in _cls.GENERIC_NBT if k not in _flat]
        TIER_MAP[_cls.type_id] = [list(t) for t in _tiers] + ([_leftover] if _leftover else [])

_DISPLAY_GROUPS = None  # set by the completer: ordered groups (list of lists) for the listing
PROMPT = "ttrpg> "      # the prompt currently being shown; the display hook redraws with it
                        # (set by run() and by the function editor so the redraw isn't mis-prompted)


def _display_matches(substitution, matches, longest_match_length):
    """Custom completion listing. var-casts show bare datatypes; otherwise render the groups
    the completer chose (preserving order; tiers separated by '---')."""
    if matches and all("->" in m for m in matches):
        groups = [[m.split("->", 1)[1] for m in matches]]
    elif _DISPLAY_GROUPS:
        groups = [[m for m in group if m in matches] for group in _DISPLAY_GROUPS]
        groups = [g for g in groups if g]
    else:
        groups = [sorted(matches)]
    body = "\n---\n".join("  ".join(group) for group in groups)
    sys.stdout.write("\n  " + body.replace("\n", "\n  ") + "\n" + PROMPT + readline.get_line_buffer())
    sys.stdout.flush()


def _complete_line(buffer, text, bare=False):
    """Completions for one command line; also sets _DISPLAY_GROUPS for the listing.
    `bare`=True drops the leading '/' from command-name suggestions (used after a 'run').
    The buffer-level rewrites below (run-command, $()-vars, ranges) live HERE rather than only in
    the readline wrapper, so the web UI's /complete endpoint (which calls this directly) gets them too
    — e.g. after `/execute ... run ` you get real command autofill (/stat, /character), not if/as/run."""
    global _DISPLAY_GROUPS
    prior0 = buffer[:len(buffer) - len(text)].split()
    cmdr = (autofill.complete_command(prior0[0]) or prior0[0]).lstrip("/") if prior0 else ""
    ins = re.match(r"^:(?:ins|insert|i)\s+\d+\s", buffer)        # function editor ':ins N <command>'
    if ins:
        return _complete_line(buffer[ins.end():], text)
    if cmdr == "random" and len(prior0) >= 2 and "range".startswith(prior0[1].lower()) and re.fullmatch(r"\d+", text):
        _DISPLAY_GROUPS = [[text + ".."]]                       # '1' + TAB -> '1..' (Minecraft range)
        return [text + ".."]
    if (re.match(r"^/data\s+(result|success|set)\s+\$\(\w+\)->\w*$", buffer)
            or (buffer.lstrip().startswith("/execute") and re.search(r"\bstore\s+(result|success)\s+var\s+\$\(\w+\)->\w*$", buffer))):
        head = text[:text.find("->") + 2] if "->" in text else text
        options = [head + t for t in DATATYPES if (head + t).startswith(text)]
        _DISPLAY_GROUPS = [options]
        return options
    run = re.match(r"^/data\s+\S+\s+\S+\s+run\s", buffer)        # /data ... run <command> -> complete it
    if run:
        return _complete_line(buffer[run.end():], text, bare=True)
    erun = re.match(r"^/execute\s+.*?\srun\s", buffer)           # /execute ... run <command> -> complete it
    if erun:
        return _complete_line(buffer[erun.end():], text, bare=True)
    if cmdr == "data" and len(prior0) == 2 and prior0[1] in ("result", "success", "set"):
        return _var_name_pool(text)                             # $(name)[->type] slot
    if cmdr == "execute" and len(prior0) >= 3 and prior0[-3] == "store" and prior0[-2] in ("result", "success") and prior0[-1] == "var":
        return _var_name_pool(text)
    if cmdr == "data" and len(prior0) == 3 and prior0[1] in ("result", "success"):
        options = [c for c in ["run"] if c.startswith(text)]
        _DISPLAY_GROUPS = [options]
        return options
    if _in_path_bracket(buffer):
        options = _path_completions(text, buffer)
        _DISPLAY_GROUPS = [options]
        return options
    if _active_type(buffer):
        first = buffer.lstrip("/").split()[0] if buffer.split() else ""
        cmd = (autofill.complete_command("/" + first) or "").lstrip("/")
        # the NBT vocabulary is keyed by the COMMAND for character/talent/effect/ability
        # (the token before '{' is the element's NAME, not a type); /item uses the type token.
        by_command = {"character": "player", "talent": "talent", "effect": "effect",
                      "ability": "ability", "summon": "mob", "quest": "quest", "structure": "structure"}
        type_id = by_command.get(cmd) or _active_type(buffer)
        options = autofill.suggest_nbt_key(text, type_id)
        _DISPLAY_GROUPS = TIER_MAP.get(type_id) or [options]  # tiers for player; ordered otherwise
        return options
    prior = buffer[:len(buffer) - len(text)].split()
    if not prior:
        cmds = autofill.suggest_command(text)
        options = [c.lstrip("/") for c in cmds] if bare else cmds
        _DISPLAY_GROUPS = [options]
        return options
    cmd = (autofill.complete_command(prior[0]) or prior[0]).lstrip("/")
    if cmd == "proc" and len(prior) >= 3:
        # relevant procs (this player's) FIRST, but always keep the full ~518-name tree
        # reachable underneath — so you still get the 'show all' listing for the rest.
        relevant = [c for c in _relevant_procs(prior[2]) if c.startswith(text)]
        others = [c for c in PROC_NAMES if c.startswith(text) and c not in relevant]
        options = relevant + others
        _DISPLAY_GROUPS = [g for g in (relevant, others) if g] or [options]
        return options
    options = [c for c in _completion_pool(cmd, prior[1:]) if c.startswith(text)]
    _DISPLAY_GROUPS = [options]  # preserve the order the pool returned (not sorted)
    return options


def _gather(text):
    """Compute the completion candidates for the current readline line and set _DISPLAY_GROUPS.
    All the buffer-level handling now lives in _complete_line (so the UI gets it too); this is just
    the readline entry point."""
    global _DISPLAY_GROUPS
    _DISPLAY_GROUPS = None
    return _complete_line(readline.get_line_buffer(), text)


def _completer(text, state):
    """readline calls this repeatedly; compute once (state 0), then index the cache."""
    if state == 0:
        _completer.cache = _gather(text)
    cache = getattr(_completer, "cache", [])
    return cache[state] if state < len(cache) else None


def setup_readline():
    if "libedit" in (readline.__doc__ or ""):
        readline.parse_and_bind("bind ^I rl_complete")
    else:
        readline.parse_and_bind("tab: complete")
    readline.parse_and_bind("set show-all-if-ambiguous on")    # list options on the first TAB
    readline.parse_and_bind("set show-all-if-unmodified on")   # ...even when it can't extend — kills the 2nd TAB
    readline.parse_and_bind("set completion-query-items 200")  # don't prompt 'display all N?' for normal lists
    readline.parse_and_bind("set page-completions off")        # no --More-- paging spam; print once
    readline.set_completer_delims(" \t\n{,")  # NOT '>' — it appears inside $(x)->type candidates
    readline.set_completer(_completer)
    # NOTE: we deliberately do NOT install a custom completion-display hook. It looked nicer
    # (tiered/ordered groups) but it fought readline's own redisplay: when readline auto-inserts a
    # common prefix (e.g. '$(x)' -> '$(x)->') and THEN lists, it redraws the line incrementally
    # (just the inserted '->') right after the hook's manual prompt+line redraw — painting a phantom
    # second '->' ('$(x)->->') and corrupting the editor prompt. The line BUFFER was always correct,
    # but the screen wasn't, which also broke backspacing/further autofill. readline's native listing
    # redraws the prompt+line correctly on its own, so we rely on it. (_display_matches/_DISPLAY_GROUPS
    # are left in place, unused, in case a backend-safe grouped display is revisited later.)


# --- run loop ----------------------------------------------------------------

def run():
    global PROMPT
    setup_readline()
    sid = _ensure_session_id()
    print("TTRPG shell. Ctrl-C / Ctrl-D to quit.")
    print(f"session {sid}  (saves to {_session_path(sid)}; UI would host on http://localhost:{sid})")
    print("Type `/` then hit TAB to see your options.\n")
    while True:
        try:
            PROMPT = "ttrpg> "  # the main prompt (the editor overrides PROMPT while it's open)
            line = input(PROMPT).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not line:
            continue
        try:
            dispatch(line)
        except Exception as error:
            print("  error:", error)


if __name__ == "__main__":
    run()

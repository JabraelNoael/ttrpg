"""Interactive command shell with LIVE tab-completion.

Launch it:   python3 main.py   (or)   python3 repl.py

TAB completes at every position, like `cd <TAB>`:
    /char<TAB>                 -> /character
    /character <TAB>           -> add modify show list remove get   (subcommands)
    /character add <TAB>       -> existing character names
    /stat hero <TAB>           -> set add modify get list           (operations)
    /item give hero iron_sword{<TAB>   -> NBT keys for that item

Reading values (two ways):
    /character get hero inventory_slots
    hero.inventory_slots                 (bare path, no slash — just prints the value)
    hero.stats.health                    (reach into containers)
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
import difflib
try:
    import gnureadline as readline  # real GNU readline (enables ordered/tiered listing + 1-tab)
except ImportError:
    import readline  # macOS falls back to libedit (works, but lists in its own sorted columns)
from containers import (Player, Mob, Item, Talent, Ability, Effect, Cooldown, Cost, Proc, Reaction,
                        UnitValue, Formula, STAT_NAMES, FORMULA_CONSTANTS,
                        COMBAT_TYPES, COMBAT_RULES,
                        parse_item_text, parse_warnings, add_parse_warning, _fill_generic_nbt,
                        _split_pairs)
from autofill import autofill

WORLD = {}  # name -> Player, for this session
HISTORY = []  # session log: function-run lines, '---' between player turns, '===' between cycles
SESSION = None  # name of the currently loaded/saved session (None until save/load)
SESSION_DIR = "sessions"

# Containers a class-path (in cost/reaction) can address. Used to validate paths and to
# autofill inside reaction:[ / cost:[ — catches typos like 'stat.health' (missing 's').
PATH_CONTAINERS = ["stats", "inventory", "equipment", "talents", "abilities", "effects", "nbt"]


def _known_keys(player, container):
    """The keys that already 'exist' for a class-path container on this player:
    schema names + live keys for stats, member names for collections, nbt keys."""
    if container == "stats":
        return set(STAT_NAMES) | {s.name for s in player.stats}
    if container == "nbt":
        return set(Player.GENERIC_NBT) | set(player.nbt)
    if container == "inventory":
        return {i.type_id for i in player.inventory.items} | {i.name for i in player.inventory.items if i.name}
    if container in ("talents", "abilities", "effects"):
        return {m.name for m in getattr(player, container)}
    return set()


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
        if container not in PATH_CONTAINERS:
            guess = difflib.get_close_matches(container, PATH_CONTAINERS, n=1)
            issues.append(f"unknown container '{container}'" + (f" — did you mean '{guess[0]}'?" if guess else ""))
            continue
        # Only 'stats' has a fixed 2-level schema worth a typo check. Member/nbt containers are
        # deeper (inventory.<item>.<field>) and their members come and go — a path to an item/talent
        # not present YET (e.g. ammo added later) is legitimate, not a typo.
        if container != "stats":
            continue
        if not key or "." in key or key in _known_keys(player, container):
            continue
        close = difflib.get_close_matches(key, list(_known_keys(player, container)), n=1)
        if close:
            issues.append(f"{container} has no '{key}' — did you mean {container}.{close[0]}?")
    for name in funcs:  # a reaction naming a function that doesn't exist yet
        if name not in FUNCTIONS:
            issues.append(f"reaction calls function '{name}' which doesn't exist (create it with /function edit {name})")
    return issues


def _flag_path_issues(player, element):
    """Push any cost/reaction path problems into the parse-warning collector so the
    dispatch undo prompt picks them up (uniform with NBT parse warnings)."""
    for attr in ("cost", "reaction"):
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
        return obj.reset(*_parse_brace_keys(tail)) if tail else obj.reset()
    if not tail:
        return print(f"  usage: {op} <field> <value>   or   {op} {{nbt}}")
    reset_first = False
    if tail.startswith("{"):
        _, payload, remainder = parse_item_text("_" + tail)
        reset_first = remainder.strip().lower() in ("true", "1", "yes")  # 'set {nbt} true'
    else:
        field, _, value_text = tail.partition(" ")
        payload = {field: parse_value(value_text.strip())}
    if op == "set":
        if reset_first:                  # 'set {nbt} true': wipe the nbt block (keep name/identity), refill defaults
            if hasattr(obj, "nbt"):
                obj.nbt.clear()
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
SELECTOR_TOKENS = ["@", "@a", "@!a", "@m", "@p", "@!p", "@s", "@!s"]
CURRENT = None  # @p — the sticky last-used selection; set whenever a concrete selector resolves
SELF = None     # @s — the owner of the proc/function currently executing (set during firing)


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


def _filter_targets(targets, filt):
    """Keep targets matching every key=value in the filter body (values parsed; field_value compared)."""
    conditions = []
    for pair in _split_pairs(filt):
        if "=" in pair:
            key, _, value = pair.partition("=")
            conditions.append((key.strip(), parse_value(value.strip())))
    return [(n, p) for n, p in targets if all(p.field_value(k) == v for k, v in conditions)]


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
    if container_attr == "stats":
        kept = []
        for member in members:
            if member.name in player.formulas and _is_zero(member.value):
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
    if sel.strip() not in ("@p", "@!p", "@s", "@!s"):
        CURRENT = list(targets)
    return targets


# --- /character --------------------------------------------------------------
def _split_name_nbt(arg):
    """Split '<name>[{nbt}] [is_npc]' into ('hero', {...}); names may have spaces. An optional
    trailing true/false (outside the braces) sets nbt['npc'] — /character add goblin{...} true."""
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
        if not _is_npc(WORLD[name]):
            WORLD[name].stats.set("turn", 0)  # non-NPCs join the turn order with a personal turn count
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
    lookup = lambda n: player.stats.get(n) or 0
    return Formula(expr).evaluate(lookup, lookup)


def cmd_stat(rest):
    return _value_command(rest, "stats", "stat")


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
            print(f"  {name}.formulas:")
            for stat_name in player.formulas:
                print(f"    {stat_name} = {player.formulas.get(stat_name)}")
            if not len(player.formulas):
                print("    (none)")
        return True
    if sub == "recompute":
        for name, player in targets:
            player.recompute()
            print(f"  {name}: recomputed {len(player.formulas)} formulas")
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
            player.formulas.add(stat_name, expr)
            player.recompute()
            print(f"  {name}.formulas.{stat_name} = {player.formulas.get(stat_name)}  ->  stat.{stat_name} = {player.stats.get(stat_name)}")
        return True
    if sub == "remove":
        stat_name = remainder.split()[0] if remainder.split() else None
        if not stat_name:
            print("  usage: /formula remove <selector> <stat>")
            return False
        for name, player in targets:
            player.formulas.remove(stat_name)
            had_stat = stat_name in player.stats
            player.stats.remove(stat_name)  # drop the derived stat too — no frozen orphan value
            note = " (and its stat)" if had_stat else ""
            print(f"  {name}: removed formula '{stat_name}'{note}")
        return True
    print("  usage: /formula <new|modify|remove|list|recompute> <selector> ...")
    return False


# --- /item add|remove|loot|modify|list ---------------------------------------

def _item_add(arg):
    sel, remainder = _split_selector(arg)
    targets = _resolved(sel)
    if targets is None:
        return False
    if not remainder:
        print("  usage: /item add <selector> <item>[{nbt}] [count]")
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
            return inv.items[idx], 1, None
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
        print(f"  [loot: DUMMY — real loottables come later] rolled '{table}' -> gave {name}: {item}")
    return True


ITEM_SUBS = ["add", "remove", "loot", "modify", "list"]


def cmd_item(rest):
    sub, _, arg = rest.partition(" ")
    sub, arg = sub.strip(), arg.strip()
    handler = {"add": _item_add, "give": _item_add, "remove": _item_remove,
               "loot": _item_loot, "modify": _item_modify, "list": _item_list}.get(sub)
    if handler:
        return handler(arg)
    print("  usage: /item add <selector> <item>  |  remove <selector> <item> [count]  |  "
          "loot <table> <selector>  |  modify <selector> <item> <set|add|reset> {nbt}  |  list <selector>")
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
            player.stats.set("health", 0)
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
            eff.stats.set(key, (eff.stats.get(key) or 0) + value)
    if attack_override is not None:
        eff.stats.set("attack", attack_override)
    eff.recompute()
    return eff


def _resolve_damage(eff, target, explicit=None):
    """Run the file-driven combat resolution. `explicit` = {type:raw} feeds raw damage directly
    (the 'inverse'); else raw per type comes from the effective attacker's derived stats.
    Returns (total, [(type, raw, net), ...])."""
    total, breakdown = 0.0, []
    for spec in COMBAT_TYPES:
        typ = spec["type"]
        if explicit is not None:
            if typ not in explicit:
                continue
            raw = float(explicit[typ])
        else:
            raw = float(eff.stats.get(spec["raw"]) or 0)
        if not raw:
            continue
        pen = float(eff.stats.get(spec["penetration"]) or 0) if spec["penetration"] else 0.0
        res = float(target.stats.get(spec["defense"]) or 0) if spec["defense"] else 0.0
        rule = COMBAT_RULES.get(spec["channel"], "atk.raw")
        atk_vals, def_vals = {"raw": raw, "pen": pen}, {"res": res}
        net = Formula(rule).evaluate(lambda n: 0, lambda n: 0,
                                     namespaces={"atk": atk_vals.get, "def": def_vals.get})
        total += net
        breakdown.append((typ, round(raw, 2), round(net, 2)))
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
        print(f"    {target_name} is spared (left at {round(target.stats.get('health') or 0, 2)} health)")
    else:
        finish()


def cmd_attack(rest):
    """/attack <attacker> <target> with <weapon>[+<weapon2>]   resolve a weapon attack
       /attack <attacker> <target> {pierce:5,slash:3}          deal explicit per-type raw damage
       /attack <attacker> <target> <number>                    use <number> as the attacker's effective attack
       /attack <attacker> list                                 list the attacker's wieldable items (have a 'wield' nbt)
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
        wieldable = [it for it in attacker.inventory if it.nbt.get("wield")]
        if not wieldable:
            print(f"  {attacker_name} has no wieldable items (set an item's nbt 'wield' to main/off/both)")
        for it in wieldable:
            print(f"  {it.type_id} (wield:{it.nbt.get('wield')}) stats={_weapon_stats(it) or '{}'}")
        if not after:
            print("  usage: /attack <attacker> <target> with <weapon>[+<weapon2>] | {type:amount} | <number>")
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
    eff = _effective_attacker(attacker, weapons, attack_override)
    wpn_label = ("+".join(w.type_id for w in weapons) if weapons else
                 ("{...}" if explicit is not None else f"attack {attack_override}"))
    for target_name, target in list(targets):
        total, breakdown = _resolve_damage(eff, target, explicit)
        target.stats.set("health", (target.stats.get("health") or 0) - total)
        target.recompute()
        print(f"  {attacker_name} attacks {target_name} with {wpn_label}: {round(total, 2)} damage "
              f"{breakdown} -> {target_name}.health = {round(target.stats.get('health') or 0, 2)}")
        if (target.stats.get("health") or 0) <= 0:
            _lethal(attacker_name, attacker, target_name, target)
    return True


# --- /turn -------------------------------------------------------------------
# TURN is the WORLD turn (drives the calendar); it advances by 1 each time the player turn
# order completes a full cycle. TURN_ACTIVE is the name of the player whose turn is in progress.
# Player turn order = the non-NPC players in creation order (WORLD is insertion-ordered).
TURN = 0
TURN_ACTIVE = None


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


def _tick_effects(player):
    """One turn passes for each effect: if this is a firing turn (per its step schedule), fire
    it (pulse proc + reaction); then count duration down and remove at 0. Effects with neither
    a proc nor a reaction just tick silently."""
    for effect in list(player.effects):
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
            player.effects.remove(effect)
        else:
            effect.modify(duration=new_duration)


def _tick_cooldowns(player):
    """One turn passes: count each ability's cooldown down toward 0 (ready)."""
    for ability in player.abilities:
        if isinstance(ability.cooldown, Cooldown):
            ability.cooldown.tick()


def _regen(player):
    """One turn passes: health/mana gain their regen, capped at the max_ ceiling.
    Skips a stat that has its own formula — that formula owns it (the default health/mana
    formulas in formulas.csv already fold regen + the max_ clamp in, so this is the fallback
    for characters whose health/mana formula has been removed via /formula)."""
    for live, regen, ceiling_key in (("health", "health_regen", "max_health"),
                                     ("mana", "mana_regen", "max_mana")):
        if live in player.formulas:
            continue
        current = player.stats.get(live)
        if current is None:
            continue
        ceiling = player.stats.get(ceiling_key)
        new_value = current + (player.stats.get(regen) or 0)
        if ceiling is not None and new_value > ceiling:
            new_value = ceiling
        player.stats.set(live, new_value)


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
    player.stats.set("turn", (player.stats.get("turn") or 0) + 1)  # this player's own turn count
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
        HISTORY.append("===")  # turn-cycle boundary
        print(f"  === cycle complete — world turn {TURN}: {_format_date(TURN).strip()} ===")
        _fire_due_events(TURN)  # any queued world events whose turn arrived
    else:
        HISTORY.append("---")  # player-turn boundary
    TURN_ACTIVE = order[nxt]
    _process_turn_start(TURN_ACTIVE)
    return TURN_ACTIVE


def cmd_turn(rest):
    """/turn                     whose turn it is + the world turn
       /turn next                end the current player's turn, begin the next (world turn ticks on wrap)
       /turn next cycle          run a full cycle (one turn for every player)
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
                print(f"  now {name}'s turn  (their stats.turn = {WORLD[name].stats.get('turn')})")
            return TURN
        if arg == "cycle":
            order = _turn_order()
            if not order:
                print("  no players in the turn order")
                return False
            for _ in range(len(order)):
                _next_player_turn()
            print(f"  ran a full cycle  |  world turn {TURN}  |  active: {TURN_ACTIVE}")
            return TURN
        targets = _resolved(arg)  # /turn next <selector>: just advance those players' own turns
        if targets is None:
            return False
        for name, player in targets:
            _process_turn_start(name)
            print(f"  {name}: advanced their turn (stats.turn = {player.stats.get('turn')})")
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
    print("  usage: /turn [query] | next [cycle|<selector>] | add <n> | set <n>")
    return False


# --- /calendar (date/time derived from the world turn) -----------------------
# Iteration 1: the date model + display. The clock derives from the global world TURN
# (TURNS_PER_DAY turns = 1 day). Calendar: 13 months of 26 days (the 13th is 'Sol'), then a
# 14th month 'Lanus' = New Year, 1 day (2 on a leap year). Leap = every 4th year.
# Coming later: a visual box grid, weather (lingering, from a CSV), and event scheduling.
TURNS_PER_DAY = 8          # how many turns make a day (provisional; user may retune)
DAYS_PER_MONTH = 26
REGULAR_MONTHS = 13        # months 1..13 each have DAYS_PER_MONTH days (13th = Sol)
MONTH_NAMES = {13: "Sol", 14: "Lanus"}  # months 1..12 are unnamed for now ("Month N")


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
    global WEATHER_TURN, WEATHER_BY_UNIT
    if WEATHER_TURN < 0:
        WEATHER_BY_UNIT = {u: _roll_next_weather(None) for u in _all_units()}
        WEATHER_TURN = turn
        _apply_weather_queue(turn)
        return
    while WEATHER_TURN < turn:
        WEATHER_TURN += 1
        WEATHER_BY_UNIT = _step_weather(WEATHER_BY_UNIT)
        _apply_weather_queue(WEATHER_TURN)
    for unit in _all_units():  # seed units added to the map since the last sync
        WEATHER_BY_UNIT.setdefault(unit, _roll_next_weather(None))


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


def _cal_event(args):
    """/calendar event add <when> <text> | list | clear. `when` = 12 (day), 12.4 (day.turn),
    or 12-15 (day range). Defaults to the current year/month."""
    if not args or args[0] in ("list", "show"):
        if not CAL_EVENTS:
            print("  (no calendar events)")
            return []
        for e in sorted(CAL_EVENTS, key=lambda x: (x["year"], x["month"], x["day"])):
            span = f"day {e['day']}" + (f"-{e['end']}" if e["end"] else "") + (f" turn {e['turn']}" if e["turn"] else "")
            print(f"  Year {e['year']} {_month_name(e['month'])} {span}: {e['text']}")
        return list(CAL_EVENTS)
    if args[0] == "clear":
        CAL_EVENTS.clear()
        print("  cleared all calendar events")
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
        event = {"year": year, "month": month, "day": 1, "end": None, "turn": None, "text": text}
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
    print("  usage: /calendar event <add|list|clear> ...")
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
    """biome -> list of ASCII-art lines, shown inside each map cell. User-extendable."""
    try:
        with open(path) as handle:
            data = json.load(handle)
            return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


MAP = _load_map()
BIOME_ART = _load_biomes()
CELL_W = 11   # inner cell width (matches biomes.json art width)
CELL_H = 6    # content rows per cell: row 0 = biome label, rows 1.. = biome art (5 art rows)


def _save_map():
    with open(MAP_FILE, "w") as handle:
        json.dump(MAP, handle, indent=2)


def _map_bounds():
    keys = [tuple(int(n) for n in k.split(",")) for k in MAP["cells"]]
    xs, ys = [k[0] for k in keys], [k[1] for k in keys]
    return (min(xs), max(xs), min(ys), max(ys)) if keys else (0, 0, 0, 0)


def _map_recommend(x, y):
    """Suggest a biome for a cell from its 4 orthogonal neighbors (most common) — biomes are
    continuous, so a new cell usually matches its surroundings rather than rolling fresh."""
    neighbors = []
    for dx, dy in ((-1, 0), (1, 0), (0, -1), (0, 1)):
        cell = MAP["cells"].get(f"{x + dx},{y + dy}")
        if cell and cell.get("biome"):
            neighbors.append(cell["biome"])
    if not neighbors:
        return None
    return max(set(neighbors), key=neighbors.count)


def _cell_box(x, y):
    """The text lines (CELL_H + 2 tall) for one map cell as a box-drawn frame: coord in the top
    border, biome label, then the biome's ASCII art. Empty coords render as a blank frame."""
    coord = f"({x},{y})"
    top = "╔" + coord + "═" * (CELL_W - len(coord)) + "╗"
    bottom = "╚" + "═" * CELL_W + "╝"
    cell = MAP["cells"].get(f"{x},{y}")
    if not cell:
        return [top] + ["║" + " " * CELL_W + "║"] * CELL_H + [bottom]
    biome = cell.get("biome") or "?"
    marker = "◆" if [x, y] == MAP.get("pos") else ("*" if cell.get("name") else " ")
    label = (marker + biome)[:CELL_W].ljust(CELL_W)
    art = BIOME_ART.get(biome) or BIOME_ART.get("default") or []
    lines = [top, "║" + label + "║"]
    for r in range(CELL_H - 1):
        art_line = (art[r] if r < len(art) else "")[:CELL_W].center(CELL_W)
        lines.append("║" + art_line + "║")
    return lines + [bottom]


def _map_render(anchor=None, rows_hint=None):
    """Build the viewport lines (list of strings). `anchor` (x,y) = the top-left cell shown
    (how you pan). Defaults to the map's min corner. Viewport size follows the terminal."""
    cells = MAP["cells"]
    if not cells:
        return ["  (map is empty — /map add <x> <y> [biome])"]
    minx, maxx, miny, maxy = _map_bounds()
    cols, rows = shutil.get_terminal_size((80, 24))
    per_row = max(1, cols // (CELL_W + 2))
    per_col = max(1, ((rows_hint or rows) - 4) // (CELL_H + 2))
    ax, ay = anchor if anchor else (minx, miny)
    xs = list(range(ax, min(ax + per_row, maxx + 1)))
    ys = list(range(ay, min(ay + per_col, maxy + 1)))
    if not xs or not ys:
        return [f"  (nothing at ({ax},{ay}); map spans x {minx}..{maxx}, y {miny}..{maxy})"]
    px, py = MAP.get("pos", [0, 0])
    out = [f"  viewport ({xs[0]},{ys[0]})..({xs[-1]},{ys[-1]})  |  ◆ ({px},{py})  |  map x {minx}..{maxx} y {miny}..{maxy}"]
    named = []
    for y in ys:
        boxes = [_cell_box(x, y) for x in xs]
        for r in range(CELL_H + 2):
            out.append("  " + "".join(box[r] for box in boxes))
        for x in xs:
            cell = cells.get(f"{x},{y}")
            if cell and cell.get("name"):
                named.append(f"    ({x},{y}) {cell['name']} — {cell.get('biome', '?')}")
    return out + named


def _map_view(anchor=None):
    print("\n".join(_map_render(anchor)))


def _map_interactive(anchor=None):
    """Live viewer: arrow keys pan cell-by-cell, q or :done exits. Needs a real terminal —
    falls back to a one-shot static render when stdin isn't a TTY (piped/scripted)."""
    if not sys.stdin.isatty():
        _map_view(anchor)
        return True
    import termios
    import tty
    import select
    minx, maxx, miny, maxy = _map_bounds()
    ax, ay = anchor if anchor else (minx, miny)
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        while True:
            ax = max(minx, min(ax, maxx))
            ay = max(miny, min(ay, maxy))
            frame = _map_render((ax, ay)) + ["", "  arrows pan · q or :done to exit"]
            sys.stdout.write("\033[2J\033[H" + "\r\n".join(frame) + "\r\n")
            sys.stdout.flush()
            ch = sys.stdin.read(1)
            if ch in ("q", "\x03", "\x04"):  # q / Ctrl-C / Ctrl-D
                break
            if ch == ":":  # read the rest of a :word (cooked-ish) and exit on :done/:q
                word = ""
                while True:
                    c = sys.stdin.read(1)
                    if c in ("\r", "\n"):
                        break
                    word += c
                if word.strip() in ("done", "q", "quit", "exit"):
                    break
            elif ch == "\x1b":  # escape — an arrow seq (ESC [ A/B/C/D) or a bare ESC
                if select.select([fd], [], [], 0.05)[0]:
                    seq = sys.stdin.read(2)
                    ay -= seq == "[A"
                    ay += seq == "[B"
                    ax += seq == "[C"
                    ax -= seq == "[D"
                else:
                    break  # bare ESC = exit
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
        sys.stdout.write("\033[2J\033[H")
        sys.stdout.flush()
    _map_view((ax, ay))  # leave the final frame in the scrollback
    return True


def cmd_map(rest):
    """/map [view [x y]]           show the biome grid (box cells); x y = pan the viewport's corner
       /map pos <x> <y>            set the party's current cell (the ◆ marker)
       /map get <x> <y>            a cell's full info
       /map set <x> <y> <biome>|{nbt}   create/update a cell
       /map new <x> <y> [biome]    create a cell (biome auto-recommended from neighbors if omitted)
       /map recommend <x> <y>      suggest a biome from neighboring cells"""
    parts = rest.split()
    sub = parts[0] if parts else "view"
    if sub in ("view", "show"):
        anchor = None
        if len(parts) >= 3 and parts[1].lstrip("-").isdigit() and parts[2].lstrip("-").isdigit():
            anchor = (int(parts[1]), int(parts[2]))
        _map_interactive(anchor)  # arrow-key pan in a real terminal; static print when piped
        return True
    if sub == "pos":
        if len(parts) >= 3 and parts[1].lstrip("-").isdigit() and parts[2].lstrip("-").isdigit():
            MAP["pos"] = [int(parts[1]), int(parts[2])]
            _save_map()
            print(f"  position set to ({parts[1]},{parts[2]})")
            return True
        print(f"  position is ({MAP.get('pos', [0, 0])[0]},{MAP.get('pos', [0, 0])[1]})  —  /map pos <x> <y> to move")
        return MAP.get("pos")
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
    print("  usage: /map [view [x y]] | pos <x> <y> | get <x> <y> | set <x> <y> <biome>|{nbt} | new <x> <y> [biome] | recommend <x> <y>")
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


def _apply_template(mob, species):
    """Stamp a bestiary preset onto a freshly-made mob: default stats/attributes/nbt/abilities.
    (Inline /summon {nbt} is applied AFTER this, so it overrides.)"""
    template = BESTIARY.get(species)
    if not template:
        return False
    mob.modify(**template.get("nbt", {}))
    for stat, value in {**template.get("attributes", {}), **template.get("stats", {})}.items():
        mob.stats.set(stat, value)  # attributes folded into the one stat namespace
    for ability_text in template.get("abilities", []):
        try:
            mob.abilities.add(Ability.from_nbt(*parse_item_text(ability_text)[:2]))
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


def cmd_talent(rest):
    return _collection_command(rest, "talents", Talent, "talent")


def cmd_effect(rest):
    return _collection_command(rest, "effects", Effect, "effect")


def _resolve_path(player, path):
    """Resolve a class-path to (get, set) callables so deltas can target ANYTHING editable:
       stats.<name> / attributes.<name>     -> the value
       nbt.<key>                            -> a player nbt key
       inventory.<item>.<field>             -> an item field (e.g. inventory.rope.count)
       talents/abilities/effects.<name>.<field>
    Returns None if it can't resolve."""
    parts = path.split(".")
    head = "stats" if parts[0] == "attributes" else parts[0]  # attributes folded into stats (alias)
    container = getattr(player, head, None)
    if container is None:
        return None
    rest = parts[1:]
    if isinstance(container, dict):  # player.nbt
        if len(rest) == 1:
            key = rest[0]
            return (lambda: container.get(key), lambda v: container.__setitem__(key, v))
        return None
    if head == "stats" and len(rest) == 1:  # the value container
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
        ability = caster.abilities.get(ident)
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
            applied = _apply_reaction(target, ability.on_hit)  # @s = target inside on_hit
            if applied:
                print(f"    on_hit {tname}: {', '.join(applied)}")
        ok = True
    return ok


def cmd_ability(rest):
    sub = rest.split(None, 1)[0] if rest.split() else ""
    if sub == "cast":
        return _ability_cast(rest.partition(" ")[2])
    return _collection_command(rest, "abilities", Ability, "ability")


# --- /proc (signals that fire talents) ---------------------------------------

def _run_function(name, owner):
    """Run a /function's body lines with @s (SELF) bound to `owner`. Returns True if it ran."""
    body = FUNCTIONS.get(name)
    if body is None:
        return False
    global SELF
    prev, SELF = SELF, owner
    HISTORY.append(f"{owner.name}: {name}")  # session history: what function ran, for whom
    try:
        for line in body:
            dispatch(line)
    finally:
        SELF = prev
    return True


def _apply_reaction(player, reaction):
    """Apply a Reaction to the owner. Each action is a class-path delta (negative = damage/spend;
    paths can reach item fields, e.g. inventory.rope.count(-1)) OR a /function call run with
    @s = the owner. Returns a list of change descriptions."""
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
            if _run_function(name, player):
                applied.append(f"fn:{name}")
            else:
                applied.append(f"fn:{name}?(missing)")
    return applied


def _fire_talents(player):
    """Fire every talent whose proc conditions match the player's current proc-state.
    Returns a list of (talent_name, applied_changes). Sets SELF=player for the duration so
    @s (self) resolves to the proc owner inside reactions/functions."""
    global SELF
    prev_self, SELF = SELF, player
    try:
        fired = []
        for talent in player.talents:
            if isinstance(talent.proc, Proc) and talent.proc.matches(player.proc_state):
                fired.append((talent.name, _apply_reaction(player, talent.reaction)))
        return fired
    finally:
        SELF = prev_self


def _pulse_procs(player, names):
    """Momentary signal: turn names on, fire matching talents, then revert (reactions persist)."""
    prior = dict(player.proc_state)
    for proc_name in names:
        player.proc_state[proc_name] = True
    fired = _fire_talents(player)
    player.proc_state = prior
    return fired


def _report_fired(name, fired):
    for talent_name, changes in fired:
        detail = f" ({', '.join(changes)})" if changes else ""
        print(f"    -> {talent_name} procced{detail}")


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
    for name, player in targets:
        if mode == "disable":
            for proc_name in names:
                player.proc_state.pop(proc_name, None)
            print(f"  {name}: disabled {', '.join(names)}")
            continue
        prior = dict(player.proc_state)
        for proc_name in names:
            player.proc_state[proc_name] = True
        fired = _fire_talents(player)
        if mode == "pulse":
            player.proc_state = prior  # momentary: revert the signals (reactions persist)
        verb = "pulsed" if mode == "pulse" else "enabled"
        print(f"  {name}: {verb} {', '.join(names)}" + ("" if fired else " (no talents reacted)"))
        _report_fired(name, fired)
    return True


# --- /var (variables + $(...) text replacement) ------------------------------
VARS = {}  # name -> stored value, text-substituted into later commands


def _substitute(text):
    """Replace $(name) with the stored value; unknown $(name) is left untouched."""
    return re.sub(r"\$\((\w+)\)", lambda m: str(VARS.get(m.group(1), m.group(0))), text)


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


EDITOR_META = ":list  :del N  :ins N <command>  :move A B  :done  :cancel  :help"  # one source of truth


def _function_editor(path):
    """In-REPL line editor for a function body. Normal command lines (with live TAB autofill)
    are appended; ':'-prefixed meta-commands edit the buffer. Doubles as create."""
    lines = list(FUNCTIONS.get(path, []))
    print(f"  editing function '{path}' — {len(lines)} line(s)" + (" (new)" if path not in FUNCTIONS else ""))
    print(f"  type command lines (TAB completes). meta: {EDITOR_META}")
    while True:
        try:
            raw = input(f"  {path}:{len(lines) + 1}> ").strip()
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


def _function_execute(path, selector):
    """Run a function's lines for each target, with @s (self) bound to that target so the
    body's @s references resolve to the entity it's being run on."""
    body = FUNCTIONS.get(path)
    if body is None:
        print(f"  no function '{path}'")
        return False
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
            print("  usage: /function edit <path>")
            return False
        return _function_editor(arg.split()[0])
    if sub in ("run", "execute"):  # 'run' is the name; 'execute' kept as a quiet alias
        path, _, sel = arg.partition(" ")
        if not path.strip() or not sel.strip():
            print("  usage: /function run <path> <selector>")
            return False
        return _function_execute(path.strip(), sel.strip())
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


def _player_to_dict(p):
    return {
        "name": p.name,
        "inventory_slots": p.inventory.slots,
        "nbt": {k: _jsonable(v) for k, v in p.nbt.items()},
        "stats": {s.name: _jsonable(s.value) for s in p.stats},
        "formulas": {n: str(p.formulas.get(n)) for n in p.formulas},
        "proc_state": dict(p.proc_state),
        "items": [repr(it) for it in p.inventory.items],
        "talents": [repr(t) for t in p.talents],
        "abilities": [repr(a) for a in p.abilities],
        "effects": [{"repr": repr(e), "elapsed": e._elapsed, "total": e._total} for e in p.effects],
    }


def _player_from_dict(d):
    nbt = d.get("nbt", {})
    cls = Mob if nbt.get("mob") else Player  # restore mobs as Mob (preserves their type/generics)
    p = cls(d["name"], inventory_slots=d.get("inventory_slots"))
    p.nbt = {k: _unjson(v) for k, v in nbt.items()}
    for name, value in d.get("stats", {}).items():
        p.stats.set(name, _unjson(value))
    for name, value in d.get("attributes", {}).items():  # legacy sessions: fold attributes into stats
        p.stats.set(name, _unjson(value))
    for name in list(p.formulas):           # drop the default formulas, restore the saved set
        p.formulas.remove(name)
    for name, expr in d.get("formulas", {}).items():
        p.formulas.add(name, expr)
    p.proc_state = dict(d.get("proc_state", {}))
    p.inventory.items = [Item.from_nbt(*parse_item_text(r)[:2]) for r in d.get("items", [])]
    for r in d.get("talents", []):
        p.talents.add(Talent.from_nbt(*parse_item_text(r)[:2]))
    for r in d.get("abilities", []):
        p.abilities.add(Ability.from_nbt(*parse_item_text(r)[:2]))
    for e in d.get("effects", []):
        effect = Effect.from_nbt(*parse_item_text(e["repr"])[:2])
        effect._elapsed, effect._total = e.get("elapsed", 0), e.get("total")
        p.effects.add(effect)
    return p


def _session_to_dict():
    return {
        "turn": TURN,
        "turn_active": TURN_ACTIVE,
        "weather": {"by_unit": dict(WEATHER_BY_UNIT), "turn": WEATHER_TURN,
                    "queue": [[t, u, w] for (t, u), w in WEATHER_QUEUE.items()]},
        "events": list(EVENT_QUEUE),
        "cal_events": list(CAL_EVENTS),
        "vars": {k: _jsonable(v) for k, v in VARS.items()},
        "functions": {k: list(v) for k, v in FUNCTIONS.items()},
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
    HISTORY.clear()
    HISTORY.extend(data.get("history", []))


def cmd_session(rest):
    """/session save <name> | load <name> | get | history | list"""
    global SESSION
    sub, _, arg = rest.partition(" ")
    sub, arg = sub.strip(), arg.strip()
    if sub == "save":
        if not arg:
            print("  usage: /session save <name>")
            return False
        os.makedirs(SESSION_DIR, exist_ok=True)
        with open(_session_path(arg), "w") as handle:
            json.dump(_session_to_dict(), handle, indent=2)
        SESSION = arg
        print(f"  saved session '{arg}' -> {_session_path(arg)}  ({len(WORLD)} characters)")
        return True
    if sub == "load":
        if not arg:
            print("  usage: /session load <name>")
            return False
        path = _session_path(arg)
        if not os.path.exists(path):
            print(f"  no saved session '{arg}' (looked in {path})")
            return False
        with open(path) as handle:
            _session_from_dict(json.load(handle))
        SESSION = arg
        print(f"  loaded session '{arg}'  ({len(WORLD)} characters, world turn {TURN})")
        return True
    if sub == "get":
        print(f"  current session: {SESSION}" if SESSION else "  no session loaded")
        return SESSION
    if sub == "history":
        if not HISTORY:
            print("  (history is empty)")
            return []
        for line in HISTORY:
            print(f"  {line}")
        return list(HISTORY)
    if sub == "list":
        saved = sorted(f[:-5] for f in os.listdir(SESSION_DIR) if f.endswith(".json")) if os.path.isdir(SESSION_DIR) else []
        print("  saved sessions: " + (", ".join(saved) or "(none)"))
        return saved
    print("  usage: /session <save|load|get|history|list> [name]")
    return False


def _session_path(name):
    return os.path.join(SESSION_DIR, name + ".json")


# --- /execute (conditional command execution) --------------------------------
COMPARATORS = {
    ">": lambda a, b: a > b, "<": lambda a, b: a < b,
    ">=": lambda a, b: a >= b, "<=": lambda a, b: a <= b,
    "=": lambda a, b: a == b, "==": lambda a, b: a == b, "!=": lambda a, b: a != b,
}


def _operand_value(token):
    """Resolve an /execute operand: a dotted PATH (@s.stats.health, @p.x, name.attributes.y)
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
    """/execute [as <selector>] [if|unless <left> <operator> <right>] [store result|success <target>] run <command>
    Chains clauses (Minecraft-style), then runs <command> if every if/unless clause passes:
      as <selector>            run AS that entity, so @s = it (repeats per target for a group)
      if <cond> / unless <cond>  require the condition true / false
      store result|success <target>   capture the command's return value / 1|0 into:
          var $(name)[->cast]          a $() variable     (same as /data result|success)
          entity <selector> <path>     an entity's stats.X / attributes.X / nbt.X
    <left>/<right> are paths (@s.stats.health) or literals; <operator> is > < >= <= = != .
      /execute as noael store result var $(hp)->float run stat get @s health
      /execute store result entity mira stats.health run stat get noael health"""
    global SELF
    clauses, sep, command = rest.strip().partition(" run ")
    if not sep or not command.strip():
        print("  usage: /execute [as <sel>] [if|unless <l> <op> <r>] [store result|success <target>] run <command>")
        return False
    command = _substitute(command.strip())          # the run-command: substitute $() now
    tokens = clauses.split()
    as_selector, conditions, store, i = None, [], None, 0
    while i < len(tokens):
        word = tokens[i]
        if word == "as" and i + 1 < len(tokens):
            as_selector = _substitute(tokens[i + 1])
            i += 2
        elif word in ("if", "unless"):
            if len(tokens) - i < 4:
                print(f"  '{word}' needs: {word} <left> <operator> <right>")
                return False
            conditions.append((word == "unless", _substitute(tokens[i + 1]), tokens[i + 2], _substitute(tokens[i + 3])))
            i += 4
        elif word == "store":
            if len(tokens) - i < 4 or tokens[i + 1] not in ("result", "success"):
                print("  store needs: store result|success var $(name) | entity <selector> <path>")
                return False
            mode, ttype = tokens[i + 1], tokens[i + 2]
            if ttype == "var":  # KEEP $(name) literal — it's the target being defined
                store = (mode, "var", tokens[i + 3])
                i += 4
            elif ttype == "entity":
                if len(tokens) - i < 5:
                    print("  store entity needs: store result|success entity <selector> <path>")
                    return False
                store = (mode, "entity", _substitute(tokens[i + 3]), tokens[i + 4])
                i += 5
            else:
                print(f"  unknown store target '{ttype}' (use var or entity)")
                return False
        else:
            print(f"  unexpected '{word}' in /execute (expected as / if / unless / store / run)")
            return False
    runners = [None]  # run once with the current @s
    if as_selector is not None:
        targets = _resolved(as_selector)
        if targets is None:
            return False
        runners = [player for _name, player in targets]
    ran = False
    for player in runners:
        previous = SELF
        if player is not None:
            SELF = player
        try:
            if all(_check_condition(*cond) for cond in conditions):
                result = dispatch(_as_command_line(command))
                if store:
                    _apply_store(store, result, player)
                ran = True
        finally:
            SELF = previous
    if not ran:
        print("  condition(s) not met — nothing run" if conditions else "  (nothing to run)")
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


# --- dispatch ----------------------------------------------------------------

HANDLERS = {
    "character": cmd_character,
    "stat": cmd_stat,
    "attack": cmd_attack,
    "formula": cmd_formula,
    "item": cmd_item,
    "kill": cmd_kill,
    "turn": cmd_turn,
    "talent": cmd_talent,
    "effect": cmd_effect,
    "ability": cmd_ability,
    "proc": cmd_proc,
    "data": cmd_data,
    "function": cmd_function,
    "calendar": cmd_calendar,
    "event": cmd_event,
    "map": cmd_map,
    "summon": cmd_summon,
    "session": cmd_session,
    "execute": cmd_execute,
    "tellraw": cmd_tellraw,
    "help": cmd_help,
}


# Player members that are sub-CONTAINERS (drilled into), not plain fields.
_PATH_SEGMENTS = {"stats", "equipment", "inventory",
                  "talents", "abilities", "effects", "formulas", "nbt", "proc_state"}


def _resolve_and_print(path):
    """Read a bare dotted path: hero, hero.name, hero.stats.health. Returns the value
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


def _confirm_or_undo(snapshot, warnings):
    """Input had parse problems. Show them, then offer to undo (revert to snapshot).
    y = undo & try again; n = keep it anyway. Returns the (possibly changed) outcome flag."""
    print("  heads up — some input wasn't understood:")
    for warning in warnings:
        print(f"    - {warning}")
    try:
        answer = input("  undo this change? [y = revert and retry / n = keep it anyway]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        answer = "n"
        print()
    if answer in ("y", "yes"):
        WORLD.clear()
        WORLD.update(snapshot)
        print("  reverted — nothing was changed.")
        return False
    print("  kept as-is.")
    return True


def dispatch(line):
    if not line.startswith("/"):
        return handle_bare_path(_substitute(line))
    word, _, rest = line[1:].partition(" ")
    full = autofill.complete_command("/" + word)  # expand abbreviations: /char -> /character
    name = full[1:] if full else word
    handler = HANDLERS.get(name)
    if name not in ("data", "execute"):
        rest = _substitute(rest)  # /data and /execute do their own selective substitution
        #   (so a $(name) being DEFINED stays literal while operands/commands get substituted)
    if handler:
        parse_warnings()  # clear any stale warnings before running
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
        return result
    if "/" + name in autofill.suggest_command("/" + word):
        print(f"  /{name} is a known command but isn't wired up yet")
    else:
        print(f"  unknown command '/{word}'. Known: {', '.join('/' + c for c in autofill._commands)}")
    return False


# --- LIVE TAB COMPLETION (readline) ------------------------------------------
COLLECTION_CMDS = {"talent": "talents", "effect": "effects", "ability": "abilities"}
VALUE_CMDS = {"stat": "stats"}
SUBCOMMANDS = {
    "character": ["new", "modify", "get", "show", "list", "remove"],
    "item": ITEM_SUBS,  # /item keeps 'add' (add to inventory), per user
    "turn": ["query", "next", "add", "set"],  # turn 'add' = + operator, stays
    **{cmd: ["new", "list", "modify", "remove"] for cmd in COLLECTION_CMDS},
    "ability": ["new", "list", "modify", "remove", "cast"],
    "function": ["edit", "run", "list", "show", "remove"],
    "calendar": ["show", "add", "set", "event", "date"],  # calendar 'add' = move time (operator)
    "map": ["view", "pos", "get", "set", "new", "recommend"],
    "session": ["save", "load", "get", "history", "list"],
    **{cmd: ["new", "get", "list", "modify", "remove"] for cmd in VALUE_CMDS},
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
    for talent in player.talents:
        if isinstance(talent.proc, Proc):
            for clause in talent.proc.clauses:
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
    container = getattr(player, head, None) if player else None
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
    container = getattr(player, head, None) if player else None
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
    keys = list(getattr(type(obj), "GENERIC_NBT", []))
    keys += list(getattr(obj, "nbt", {}).keys())
    return list(dict.fromkeys(keys))


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
    if cmd == "formula":
        if not args_before:
            return ["new", "modify", "remove", "list", "recompute"]
        sub, after = args_before[0], args_before[1:]
        if len(after) == 0:
            return sel
        if sub in ("new", "modify", "remove") and len(after) == 1:
            player = _selector_player(after[0])
            existing = list(player.formulas) if player else []
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
        return ["view", "pos", "get", "set", "new", "recommend"] if not args_before else []
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
            return ["save", "load", "get", "history", "list"]
        if args_before[0] == "load" and len(args_before) == 1:  # complete saved session names
            return sorted(f[:-5] for f in os.listdir(SESSION_DIR) if f.endswith(".json")) if os.path.isdir(SESSION_DIR) else []
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
    if cmd in ("character", "item") or cmd in COLLECTION_CMDS or cmd in VALUE_CMDS:
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
            if sub in ("add", "remove", "list", "modify") and len(after) == 0:
                return sel
            if sub == "loot" and len(after) == 1:
                return sel
            if sub in ("remove", "modify") and len(after) == 1:
                return _member_pool(after[0], "inventory")
            if sub == "modify" and len(after) == 2:
                return OPS3
            if sub == "modify" and len(after) == 3:
                return _item_keys(after[0], after[1])
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
            if sub in ("new", "get", "list", "modify", "remove") and len(after) == 0:
                return sel
            if sub in ("new", "get", "modify", "remove") and len(after) == 1:
                return _value_name_pool(after[0], container_attr, schema)
            if sub == "modify" and len(after) == 2:
                return OPS3
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
    sys.stdout.write("\n  " + body.replace("\n", "\n  ") + "\n" + "ttrpg> " + readline.get_line_buffer())
    sys.stdout.flush()


def _complete_line(buffer, text, bare=False):
    """Completions for one command line; also sets _DISPLAY_GROUPS for the listing.
    `bare`=True drops the leading '/' from command-name suggestions (used after /var's 'run')."""
    global _DISPLAY_GROUPS
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
                      "ability": "ability", "summon": "mob"}
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
    """Compute the completion candidates for the current line and set _DISPLAY_GROUPS."""
    global _DISPLAY_GROUPS
    _DISPLAY_GROUPS = None
    buffer = readline.get_line_buffer()
    # /data <mode> $(name)->TYPE  OR  /execute ... store result|success var $(name)->TYPE :
    # complete the datatype as a bare word (int, float, ...).
    if (re.match(r"^/data\s+(result|success|set)\s+\$\(\w+\)->\w*$", buffer)
            or (buffer.lstrip().startswith("/execute") and re.search(r"\bstore\s+(result|success)\s+var\s+\$\(\w+\)->\w*$", buffer))):
        options = [t for t in DATATYPES if t.startswith(text)]
        _DISPLAY_GROUPS = [options]
        return options
    run = re.match(r"^/data\s+\S+\s+\S+\s+run\s", buffer)  # autofill the run-command part
    if run:
        return _complete_line(buffer[run.end():], text, bare=True)
    erun = re.match(r"^/execute\s+.*?\srun\s", buffer)  # /execute ... run <command> -> complete it
    if erun:
        return _complete_line(buffer[erun.end():], text, bare=True)
    prior = buffer[:len(buffer) - len(text)].split()
    cmd0 = (autofill.complete_command(prior[0]) or prior[0]).lstrip("/") if prior else ""
    if cmd0 == "data" and len(prior) == 2 and prior[1] in ("result", "success", "set"):
        return _var_name_pool(text)  # $(name)[->type] slot (display hook shows bare types)
    if cmd0 == "execute" and len(prior) >= 3 and prior[-3] == "store" and prior[-2] in ("result", "success") and prior[-1] == "var":
        return _var_name_pool(text)  # /execute store result var $(name) — same $()-> slot
    if cmd0 == "data" and len(prior) == 3 and prior[1] in ("result", "success"):
        options = [c for c in ["run"] if c.startswith(text)]
        _DISPLAY_GROUPS = [options]
        return options
    return _complete_line(buffer, text)


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
    readline.parse_and_bind("set show-all-if-ambiguous on")  # list options on the first TAB
    readline.set_completer_delims(" \t\n{,>")  # '>' breaks so $(x)->TYPE completes TYPE alone
    readline.set_completer(_completer)
    try:
        readline.set_completion_display_matches_hook(_display_matches)
    except (AttributeError, NotImplementedError):
        pass  # not supported on this backend; falls back to default listing


# --- run loop ----------------------------------------------------------------

def run():
    setup_readline()
    print("TTRPG shell. TAB to complete, Ctrl-C / Ctrl-D to quit.")
    print("Try:  /character add hero   then   /stat hero health set 20   then   hero.stats.health\n")
    while True:
        try:
            line = input("ttrpg> ").strip()
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

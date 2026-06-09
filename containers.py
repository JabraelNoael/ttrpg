"""TTRPG world model — core entities and containers.

Minecraft-command-style management of a TTRPG world. This module defines the
DATA MODEL only: the objects and the containers that hold them. The commands
(/give, /summon, /effect, /execute, /talent, /ability, /attribute, /kill), NBT
(de)serialization, the proc event tree, the reference-CSV loaders, and the /ai
agent are all built ON TOP of this and are intentionally NOT implemented here.

Design decisions locked in with the user:
  - Item = core fields (name, description, quantity) + a free-form `nbt` dict.
  - Containers are independent classes (no shared base) but share method names
    (add / remove / get / __iter__ / __repr__) so they feel uniform.
  - Equipment is a dedicated, slotted container with equip()/unequip(); a slot's
    capacity may be a fixed int OR a callable resolved against the owner's Stats
    (e.g. ring-slot capacity = number of fingers).
  - Plain .py module for now; a notebook will import it later.

Hooks left open for future prompts (no mechanics assumed here):
  - Item.type_id subclasses + DEFAULT NBT states + NBT naming conventions.
  - NBT string parse/serialize round-trip (quoting rules are the user's call).
  - Talent.proc wired into a real event tree (loaded from CSV/other later).
  - Ability cost/cooldown unit semantics + a valid-units list (loaded later).
  - Equip-time granting of temporary talents/abilities/attributes.
  - Persistence (to_dict/from_dict) and the command parser + /ai agent.
"""

import re
import math
import csv
import os


# --- Parse-warning collector -------------------------------------------------
# Parsers append non-fatal problems here (e.g. a dropped/ignored token) instead
# of failing silently. The shell reads them after a command via parse_warnings()
# and can offer to undo the change. Always cleared on read.
_PARSE_WARNINGS = []


def _warn_parse(message):
    _PARSE_WARNINGS.append(message)


# Public alias so the command layer can feed runtime warnings (e.g. a class-path
# whose key doesn't exist on the player) into the same undo prompt.
add_parse_warning = _warn_parse


def parse_warnings():
    """Return accumulated parse warnings and CLEAR the buffer."""
    global _PARSE_WARNINGS
    collected, _PARSE_WARNINGS = _PARSE_WARNINGS, []
    return collected


# --- Generic schema (the standard stat names) --------------------------------
# Stats merged into ONE namespace (attributes folded in). A stat is either a BASE input
# (no formula — set directly or by gear) or a DERIVED output (owns a formula in formulas.csv).
# The name list lives in stats.csv so it's editable outside the code; every new character is
# seeded with all of these at 0 on creation (see Player.__init__).
_DATA_DIR = os.path.dirname(os.path.abspath(__file__))


def _load_stat_schema(path="stats.csv"):
    """The generic stat names, in order, from stats.csv (first row is a header, skipped)."""
    names = []
    try:
        with open(os.path.join(_DATA_DIR, path)) as handle:
            for i, row in enumerate(csv.reader(handle)):
                if i == 0 or not row or not row[0].strip():
                    continue  # header / blank
                names.append(row[0].strip())
    except OSError:
        pass
    return names


STAT_NAMES = _load_stat_schema()


# --- NBT string rendering (provisional; full parser comes later) -------------

def _nbt_value(value):
    """Render one NBT value. Quoting convention is provisional and user-adjustable."""
    if value is None:
        return "None"
    if isinstance(value, bool):
        return "True" if value else "False"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, (UnitValue, Cooldown, Cost, Proc, Reaction)):
        return str(value)  # e.g. 2t, 0/7, [stats.mana(20)], [{in_combat:true}] — unquoted
    if isinstance(value, list):
        return "[" + ",".join(str(v) for v in value) + "]"  # e.g. [poison], [:1,-1,::3]
    if isinstance(value, dict):  # nested nbt, e.g. a weapon's stats:{pierce_affinity:1,reach:20}
        return "{" + ",".join(f"{k}:{_nbt_value(v)}" for k, v in value.items()) + "}"
    text = str(value).replace('"', '\\"')
    return f'"{text}"'


def _nbt_string(data):
    """Render a dict as `{key:value,...}`. Apostrophes inside strings are fine."""
    parts = [f"{key}:{_nbt_value(value)}" for key, value in data.items()]
    return "{" + ",".join(parts) + "}"


# --- NBT string PARSING (reverse of _nbt_string; backslash-escaped quotes) ----

def _parse_brace(s):
    """Given text starting with '{', return (body_without_braces, remainder_after_close).
    Quote- and escape-aware so '}' and ',' inside strings don't end things early."""
    i, depth, in_quote, escape, body = 1, 1, False, False, []
    while i < len(s):
        c = s[i]
        if escape:
            body.append(c)
            escape = False
        elif c == "\\":
            body.append(c)
            escape = True
        elif in_quote:
            if c == '"':
                in_quote = False
            body.append(c)
        elif c == '"':
            in_quote = True
            body.append(c)
        elif c == "{":
            depth += 1
            body.append(c)
        elif c == "}":
            depth -= 1
            if depth == 0:
                return "".join(body), s[i + 1:]
            body.append(c)
        else:
            body.append(c)
        i += 1
    raise ValueError("unbalanced '{' in NBT")


def _split_pairs(body):
    """Split a brace body on top-level commas, ignoring commas inside quotes/braces."""
    pairs, cur, depth, in_quote, escape = [], [], 0, False, False
    for c in body:
        if escape:
            cur.append(c)
            escape = False
        elif c == "\\":
            cur.append(c)
            escape = True
        elif in_quote:
            if c == '"':
                in_quote = False
            cur.append(c)
        elif c == '"':
            in_quote = True
            cur.append(c)
        elif c in "{[":
            depth += 1
            cur.append(c)
        elif c in "}]":
            depth -= 1
            cur.append(c)
        elif c == "," and depth == 0:
            pairs.append("".join(cur))
            cur = []
        else:
            cur.append(c)
    if cur:
        pairs.append("".join(cur))
    return pairs


def _parse_nbt_value(text):
    """Turn one NBT value token into a Python value (str/int/float/bool/None)."""
    text = text.strip()
    if len(text) >= 2 and text[0] == '"' and text[-1] == '"':
        return text[1:-1].replace('\\"', '"').replace("\\\\", "\\")
    if len(text) >= 2 and text[0] == "[" and text[-1] == "]":  # [a,b,c] -> a real list (recursive)
        inner = text[1:-1].strip()
        # Leave STRUCTURED bracket-values as raw text for their own parsers: proc clauses use {…}
        # and cost/reaction use path(amount). Only plain scalar lists (oath:[wind,steel]) convert.
        if "{" in inner or "(" in inner:
            return text
        return [_parse_nbt_value(part) for part in _split_pairs(inner)] if inner else []
    if len(text) >= 2 and text[0] == "{" and text[-1] == "}":  # {k:v,...} -> a nested dict (recursive)
        result = {}
        for pair in _split_pairs(text[1:-1].strip()):
            key, sep, val = pair.partition(":")
            if sep:
                result[key.strip()] = _parse_nbt_value(val)
        return result
    low = text.lower()
    if low == "none":
        return None
    if low == "true":
        return True
    if low == "false":
        return False
    try:
        return int(text)
    except ValueError:
        pass
    try:
        return float(text)
    except ValueError:
        pass
    unit = re.match(r"^(-?\d+(?:\.\d+)?)([A-Za-z%]+)$", text)  # 20ft, 3.5kg, 200mana -> UnitValue
    if unit:
        amount = float(unit.group(1)) if "." in unit.group(1) else int(unit.group(1))
        return UnitValue(amount, unit.group(2))
    return text


def parse_item_text(text):
    """Parse 'type_id{key:value,...}' into (type_id, nbt_dict, remainder_after_braces).
    The remainder is whatever follows the closing brace (e.g. a trailing count)."""
    text = text.strip()
    match = re.match(r"([A-Za-z_]\w*)", text)
    if not match:
        raise ValueError("expected an item type id, e.g. iron_sword")
    type_id = match.group(1)
    rest = text[match.end():]
    nbt = {}
    if rest.startswith("{"):
        body, rest = _parse_brace(rest)
        for pair in _split_pairs(body):
            if not pair.strip():
                continue
            key, sep, val = pair.partition(":")
            if not sep:
                raise ValueError(f"NBT entry '{pair.strip()}' is missing ':'")
            nbt[key.strip()] = _parse_nbt_value(val)
    return type_id, nbt, rest.strip()


class UnitValue:
    """A magnitude plus a trailing unit string, e.g. '20ft', '200mana', '2t'.

    Behaves like its number for arithmetic and comparison (the unit rides along), so a value
    like 20ft can be stored in count/stats and still be added, subtracted, compared, and
    used with min()/etc. Displays as '20ft'."""
    def __init__(self, amount, unit=""):
        self.amount = amount
        self.unit = unit

    @classmethod
    def parse(cls, value):
        if isinstance(value, UnitValue):
            return value
        if isinstance(value, (int, float)):
            return cls(value, "")
        text = str(value).strip()
        i = 0
        while i < len(text) and (text[i].isdigit() or text[i] in ".+-"):
            i += 1
        number, unit = text[:i], text[i:].strip()
        if not number:
            amount = None
        elif "." in number:
            amount = float(number)
        else:
            amount = int(number)
        return cls(amount, unit)

    def _amt(self, other):
        return other.amount if isinstance(other, UnitValue) else other

    def __add__(self, other):
        return UnitValue(self.amount + self._amt(other), self.unit)

    def __radd__(self, other):
        return UnitValue(self._amt(other) + self.amount, self.unit)

    def __sub__(self, other):
        return UnitValue(self.amount - self._amt(other), self.unit)

    def __lt__(self, other):
        return self.amount < self._amt(other)

    def __le__(self, other):
        return self.amount <= self._amt(other)

    def __gt__(self, other):
        return self.amount > self._amt(other)

    def __ge__(self, other):
        return self.amount >= self._amt(other)

    def __eq__(self, other):
        if isinstance(other, UnitValue):
            return (self.amount, self.unit) == (other.amount, other.unit)
        return self.amount == other

    def __hash__(self):
        return hash((self.amount, self.unit))

    def __int__(self):
        return int(self.amount)

    def __index__(self):
        return int(self.amount)

    def __float__(self):
        return float(self.amount)

    def __repr__(self):
        return f"{self.amount}{self.unit}"


def _to_number(text):
    """Coerce text to int/float, leaving it as-is if neither."""
    text = str(text).strip()
    try:
        return int(text)
    except ValueError:
        pass
    try:
        return float(text)
    except ValueError:
        return text


class Cooldown:
    """An ability cooldown as current/total turns. `0/7` = ready, `7/7` = just cast.
    Parsed from 'current/total', or a single number (the total; starts ready at 0)."""
    def __init__(self, total=0, current=0):
        self.total = total
        self.current = current

    @classmethod
    def parse(cls, value):
        if isinstance(value, Cooldown):
            return value
        if isinstance(value, (int, float)):
            return cls(total=int(value), current=0)
        text = str(value).strip()
        if "/" in text:
            cur, _, tot = text.partition("/")
            return cls(total=int(tot.strip() or 0), current=int(cur.strip() or 0))
        return cls(total=int(text or 0), current=0)

    @property
    def ready(self):
        return self.current <= 0

    def trigger(self):
        """Casting: put the cooldown on full (current = total)."""
        self.current = self.total
        return self

    def tick(self):
        """One turn passes: count current down toward 0 (ready)."""
        if self.current > 0:
            self.current -= 1
        return self

    def __repr__(self):
        return f"{self.current}/{self.total}"


class Cost:
    """A list of resource costs, each a class-path + amount: e.g.
    [stats.mana(20), stats.health(1)]. The path says WHICH stat/attribute to spend;
    resolving/spending happens at cast time (the command layer walks the path)."""
    def __init__(self, entries=None):
        self.entries = list(entries or [])  # list of (path_str, amount)

    @classmethod
    def parse(cls, value):
        if isinstance(value, Cost):
            return value
        if value is None or value == "":
            return cls([])
        text = str(value).strip()
        if text.startswith("[") and text.endswith("]"):
            text = text[1:-1]
        entries = []
        for chunk in _split_pairs(text):
            chunk = chunk.strip()
            if not chunk:
                continue
            match = re.fullmatch(r"([\w.]+)\s*\(([^)]*)\)", chunk)
            if match:
                entries.append((match.group(1), _to_number(match.group(2))))
            else:
                _warn_parse(f"cost/reaction: ignored '{chunk}' — write it as path(amount), e.g. stats.health(2)")
        return cls(entries)

    def __add__(self, other):
        """Append: Cost + 'stats.health(1)' (or another Cost) -> a combined Cost."""
        other = other if isinstance(other, Cost) else Cost.parse(other)
        return Cost(self.entries + other.entries)

    def __repr__(self):
        return "[" + ",".join(f"{path}({amount})" for path, amount in self.entries) + "]"


class Reaction:
    """What a talent/effect DOES when it fires: an ordered list of actions, each either
    a class-path delta (path, amount) like stats.health(-2), OR a function call (a /function
    name) run with @s = the owner. Parsed from [stats.health(-2), poison_dmg] or [/function x].
    'Accept either' per the user — deltas for quick edits, functions for richer logic."""
    def __init__(self, actions=None):
        self.actions = list(actions or [])  # list of ("delta", path, amount) | ("func", name)

    @classmethod
    def parse(cls, value):
        if isinstance(value, Reaction):
            return value
        if isinstance(value, Cost):  # migrate an old class-path Cost into deltas
            return cls([("delta", p, a) for p, a in value.entries])
        if value is None or value == "":
            return cls([])
        text = str(value).strip()
        if text.startswith("[") and text.endswith("]"):
            text = text[1:-1]
        actions = []
        for chunk in _split_pairs(text):
            chunk = chunk.strip()
            if not chunk:
                continue
            delta = re.fullmatch(r"([\w.]+)\s*\(([^)]*)\)", chunk)
            if delta:
                actions.append(("delta", delta.group(1), _to_number(delta.group(2))))
                continue
            name = chunk
            if name.startswith("/function"):
                name = name[len("/function"):].strip()
            elif name.startswith("/"):
                name = name[1:].strip()
            if re.fullmatch(r"[\w:.\-]+", name):
                actions.append(("func", name))
            else:
                _warn_parse(f"reaction: ignored '{chunk}' — use path(amount) or a function name")
        return cls(actions)

    def __repr__(self):
        out = []
        for action in self.actions:
            out.append(f"{action[1]}({action[2]})" if action[0] == "delta" else action[1])
        return "[" + ",".join(out) + "]"


def _parse_token_list(value):
    """Parse a bracketed token list into a list of strings: '[poison,bleed]' -> ['poison','bleed'],
    '[:1,-1,::3]' -> [':1','-1','::3']. Used for an effect's proc-names and step specs."""
    if value is None or value == "":
        return []
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    text = str(value).strip()
    if text.startswith("[") and text.endswith("]"):
        text = text[1:-1]
    return [chunk.strip() for chunk in _split_pairs(text) if chunk.strip()]


def _parse_bool(text):
    return str(text).strip().lower() not in ("false", "0", "no", "")


class Proc:
    """A talent's trigger conditions: a list of clauses, each a dict of {proc_name: bool}.
    Matching is AND across everything (per the design): every key in every clause must equal
    the player's current proc-state for the talent to fire. Parsed from text like
    [{in_combat:true,end_of_turn:true}] or [{in_combat:true},{scared:false}]."""
    def __init__(self, clauses=None):
        self.clauses = [dict(clause) for clause in (clauses or [])]

    @classmethod
    def parse(cls, value):
        if isinstance(value, Proc):
            return value
        if value is None or value == "":
            return cls([])
        text = str(value).strip()
        if text.startswith("[") and text.endswith("]"):
            text = text[1:-1]
        clauses = []
        for chunk in _split_pairs(text):
            chunk = chunk.strip()
            if chunk.startswith("{") and chunk.endswith("}"):
                chunk = chunk[1:-1]
            condition = {}
            for pair in _split_pairs(chunk):
                key, sep, val = pair.partition(":")
                if sep:
                    condition[key.strip()] = _parse_bool(val)
                elif pair.strip():  # a bare name with no ':true' — don't drop it silently
                    _warn_parse(f"proc: ignored '{pair.strip()}' — write it as '{pair.strip()}:true' (or :false)")
            if condition:
                clauses.append(condition)
        return cls(clauses)

    def matches(self, state):
        """True if ANY clause fully holds (clauses are OR'd; keys within a clause are AND'd).
        So [{a:true,b:true}] needs both a AND b; [{a:true},{b:true}] fires on a OR b.
        Absent signals read as False."""
        if not self.clauses:
            return False
        for clause in self.clauses:
            if all(bool(state.get(key, False)) == bool(required) for key, required in clause.items()):
                return True
        return False

    def __repr__(self):
        clauses = ["{" + ",".join(f"{k}:{str(v).lower()}" for k, v in c.items()) + "}" for c in self.clauses]
        return "[" + ",".join(clauses) + "]"


# --- Formula engine (derived stats: atr.X / stat.X, arithmetic, IF, LET) ------
# Tuning knobs the formulas reference by name (adjust here, not in formula text).
FORMULA_CONSTANTS = {
    "AFFINITY_TAX": 0.8,   # exponent on weapon affinities > 1 (softer/harder specialization tax)
    "MAGIA_EXP": 0.75,     # diminishing returns on spreading magic across schools
    "WARD_EXP": 0.7,       # ward synergy exponent
}

def _factorial(n):
    """n! — exact for non-negative integers (5 or 5.0 -> 120), gamma(n+1) for fractional n.
    Negative n raises (the formula engine turns any error into 0.0)."""
    if n < 0:
        raise ValueError("factorial of a negative number")
    if float(n).is_integer():
        return float(math.factorial(int(n)))
    return math.gamma(n + 1)


# Multi-arg functions callable in formulas (IF/LET/DELTA are parsed specially, not here).
_FORMULA_FUNCS = {
    "MIN": min, "MAX": max, "ABS": abs,
    "FLOOR": math.floor, "CEIL": math.ceil, "SQRT": math.sqrt,
    "ROUND": lambda x, n=0: round(x, int(n)),  # n = decimal places (float-safe; engine passes floats)
    "CLAMP": lambda value, low, high: max(low, min(value, high)),
    "EXP": math.exp,    # e**x  (Euler's number ~2.71828 raised to a power)
    "FACT": _factorial,  # n! factorial (also writable postfix: n!)
}


def _tokenize_formula(text):
    tokens = []
    for match in re.finditer(r"\d+\.?\d*|\.\d+|[A-Za-z_][\w.]*|\*\*|<=|>=|==|[-+*/^()<>,!]", text):
        tok = match.group(0)
        if tok == "**":
            tok = "^"  # accept Python-style power as an alias for ^
        kind = "num" if tok[0].isdigit() or tok[0] == "." else ("id" if tok[0].isalpha() or tok[0] == "_" else "op")
        tokens.append((kind, tok))
    return tokens


class _FormulaParser:
    """Recursive-descent evaluator for the formula DSL (no eval). Grammar by precedence:
    comparison < add/sub < mul/div < power(^, right-assoc) < unary(-) < postfix(!) < primary.
    primary = number | (expr) | IF(c,a,b) | LET(name,val,body) | MIN/MAX/EXP/FACT(...) | stat.X / let-var."""
    def __init__(self, tokens, atr, stat, delta=None, namespaces=None):
        self.toks, self.pos, self.atr, self.stat = tokens, 0, atr, stat
        self._delta = delta or (lambda ref, current: 0.0)
        self.namespaces = namespaces or {}  # extra prefixes, e.g. {'atk': fn, 'def': fn} for /attack

    def _peek(self):
        return self.toks[self.pos] if self.pos < len(self.toks) else (None, None)

    def _next(self):
        tok = self.toks[self.pos]
        self.pos += 1
        return tok

    def _expect(self, value):
        if self._next()[1] != value:
            raise ValueError(f"expected '{value}' in formula")

    def expr(self, env):
        left = self.additive(env)
        op = self._peek()[1]
        if op in (">", "<", ">=", "<=", "=="):
            self._next()
            right = self.additive(env)
            return float({">": left > right, "<": left < right, ">=": left >= right,
                          "<=": left <= right, "==": left == right}[op])
        return left

    def additive(self, env):
        left = self.multiplicative(env)
        while self._peek()[1] in ("+", "-"):
            op = self._next()[1]
            right = self.multiplicative(env)
            left = left + right if op == "+" else left - right
        return left

    def multiplicative(self, env):
        left = self.power(env)
        while self._peek()[1] in ("*", "/"):
            op = self._next()[1]
            right = self.power(env)
            left = left * right if op == "*" else (left / right if right else 0.0)
        return left

    def power(self, env):
        base = self.unary(env)
        if self._peek()[1] == "^":
            self._next()
            return base ** self.power(env)  # right-associative
        return base

    def unary(self, env):
        if self._peek()[1] == "-":
            self._next()
            return -self.unary(env)
        return self.postfix(env)

    def postfix(self, env):
        value = self.primary(env)
        while self._peek()[1] == "!":  # factorial binds tightest: 3!^2 = (3!)^2, -3! = -(3!)
            self._next()
            value = _factorial(value)
        return value

    def primary(self, env):
        kind, value = self._next()
        if kind == "num":
            return float(value)
        if value == "(":
            inner = self.expr(env)
            self._expect(")")
            return inner
        if kind == "id":
            if self._peek()[1] == "(":
                return self.call(value, env)
            return self.resolve(value, env)
        raise ValueError(f"unexpected '{value}' in formula")

    def call(self, name, env):
        self._expect("(")
        upper = name.upper()
        if upper == "IF":
            cond = self.expr(env); self._expect(",")
            then = self.expr(env); self._expect(",")
            other = self.expr(env); self._expect(")")
            return then if cond else other
        if upper == "DELTA":
            ref = self._next()[1]  # a single reference, e.g. stat.turn (not an expression)
            self._expect(")")
            return self._delta(ref, self.resolve(ref, env))  # change in ref since last recompute
        if upper == "LET":
            var = self._next()[1]; self._expect(",")
            bound = self.expr(env); self._expect(",")
            body = self.expr({**env, var: bound}); self._expect(")")
            return body
        args = [self.expr(env)]
        while self._peek()[1] == ",":
            self._next()
            args.append(self.expr(env))
        self._expect(")")
        return _FORMULA_FUNCS[upper](*args)

    def resolve(self, ident, env):
        if ident in env:
            return env[ident]
        if ident in FORMULA_CONSTANTS:
            return FORMULA_CONSTANTS[ident]
        head, _, key = ident.partition(".")
        if head in self.namespaces:           # combat refs like atk.raw / def.res
            return self.namespaces[head](key)
        # one merged namespace now: atr./attributes. are accepted as aliases of stat./stats.
        if head in ("stat", "stats", "atr", "attributes"):
            return self.stat(key)
        raise ValueError(f"unknown reference '{ident}' in formula")


class Formula:
    """A derived-stat expression stored as text and evaluated against a player's atr/stat.
    atr/stat are callables name -> number. Returns 0.0 on any error (missing ref, /0, ...)."""
    def __init__(self, text):
        self.text = str(text).strip()

    def evaluate(self, atr, stat, delta=None, namespaces=None):
        try:
            return _FormulaParser(_tokenize_formula(self.text), atr, stat, delta, namespaces).expr({})
        except Exception:
            return 0.0

    def __repr__(self):
        return self.text


class Formulas:
    """A player's derived-stat formulas: stat name -> Formula."""
    def __init__(self):
        self.formulas = {}

    def __len__(self):
        return len(self.formulas)

    def __iter__(self):
        return iter(self.formulas)

    def __contains__(self, name):
        return name in self.formulas

    def __repr__(self):
        return f"Formulas({list(self.formulas)})"

    def add(self, name, expr):
        self.formulas[name] = expr if isinstance(expr, Formula) else Formula(expr)
        return self.formulas[name]

    def get(self, name):
        return self.formulas.get(name)

    def remove(self, name):
        return self.formulas.pop(name, None)


# The generic derived-stat formulas every character starts with — loaded from formulas.csv so
# they're editable outside the code (the file is the permanent source; /formula edits a single
# character at runtime). Columns: stat,formula (the formula column is quoted, it contains commas).
def _load_formulas(path="formulas.csv"):
    """stat name -> formula text, from formulas.csv (header row skipped)."""
    formulas = {}
    try:
        with open(os.path.join(_DATA_DIR, path)) as handle:
            for row in csv.DictReader(handle):
                name = (row.get("stat") or "").strip()
                expr = (row.get("formula") or "").strip()
                if name and expr:
                    formulas[name] = expr
    except OSError:
        pass
    return formulas


DEFAULT_FORMULAS = _load_formulas()


# --- Combat resolution (file-driven; used by /attack) ------------------------
# combat.csv  : one row per damage type -> which stats supply its raw damage, penetration, and the
#               target's defense, plus a channel (physical/magic). combat_rules.csv: per channel, the
#               net-damage formula using combat refs atk.raw / atk.pen / def.res (edit the math there).
def _load_combat(types_path="combat.csv", rules_path="combat_rules.csv"):
    types, rules = [], {}
    try:
        with open(os.path.join(_DATA_DIR, types_path)) as handle:
            for row in csv.DictReader(handle):
                name = (row.get("type") or "").strip()
                if name:
                    types.append({"type": name, "raw": (row.get("raw") or name).strip(),
                                  "penetration": (row.get("penetration") or "").strip(),
                                  "defense": (row.get("defense") or "").strip(),
                                  "channel": (row.get("channel") or "").strip()})
    except OSError:
        pass
    try:
        with open(os.path.join(_DATA_DIR, rules_path)) as handle:
            for row in csv.DictReader(handle):
                channel, expr = (row.get("channel") or "").strip(), (row.get("formula") or "").strip()
                if channel and expr:
                    rules[channel] = expr
    except OSError:
        pass
    return types, rules


COMBAT_TYPES, COMBAT_RULES = _load_combat()


# --- Elements (one class per kind of thing; specific types subclass later) ---

class Item:
    """A single item stack. Core fields are explicit; everything else is NBT.

    `type_id` is the command-level identity ('iron_sword'); specific items will
    eventually subclass Item and set their own type_id + DEFAULT nbt.
    """
    type_id = "item"
    # GENERIC_NBT = predictable keys common to all items (the autofill vocabulary,
    # inherited by every specific item). NBT = non-generic keys for a subclass.
    # Both are editable; truly dynamic keys typed at runtime aren't listed here.
    GENERIC_NBT = ["name", "description", "count", "prefix", "suffix", "origin"]
    NBT = []

    def __init__(self, name="", description="", quantity=1, type_id=None, **nbt):
        self.name = name
        self.description = description
        self.quantity = quantity
        if type_id is not None:
            self.type_id = type_id
        self.nbt = dict(nbt)

    _STRUCTURED = {"name", "description", "count"}

    @classmethod
    def from_nbt(cls, type_id, nbt):
        """Build an Item from a type_id + parsed nbt dict; name/description/count map to core fields."""
        data = dict(nbt)
        name = data.pop("name", "")
        description = data.pop("description", "")
        quantity = data.pop("count", 1)
        obj = cls(name=name, description=description, quantity=quantity, type_id=type_id, **data)
        return _fill_generic_nbt(obj)

    @classmethod
    def from_text(cls, text):
        """Build an Item from NBT text, e.g. Item.from_text('iron_sword{prefix:sharp}')."""
        type_id, nbt, _ = parse_item_text(text)
        return cls.from_nbt(type_id, nbt)

    def _all_nbt(self):
        """Core fields + extra NBT, merged for display. Quantity renders as `count`."""
        data = {"name": self.name, "description": self.description, "count": self.quantity}
        data.update(self.nbt)
        return data

    def modify(self, **fields):
        """Edit any field by name. `count` maps to quantity; unknown keys go to nbt.
        e.g. sword.modify(name="New Name", count=7, prefix="vicious")."""
        for key, val in fields.items():
            if key == "count":
                self.quantity = val
            elif key in ("name", "description", "quantity", "type_id"):
                setattr(self, key, val)
            else:
                self.nbt[key] = val
        return self

    def field_value(self, key):
        """Read any field: count -> quantity, core fields, else nbt (None if unset)."""
        if key == "count":
            return self.quantity
        if key in ("name", "description", "type_id"):
            return getattr(self, key)
        return self.nbt.get(key)

    def reset(self, *keys):
        """Reset core fields to default; remove dynamic nbt keys. No keys = reset all."""
        defaults = {"name": "", "description": "", "count": 1}
        if not keys:
            self.modify(name="", description="", count=1)
            self.nbt.clear()
            return self
        for key in keys:
            if key in defaults:
                self.modify(**{key: defaults[key]})
            else:
                self.nbt.pop(key, None)
        return self

    def copy(self):
        """A detached duplicate (same fields + nbt). Used to split items off a stack."""
        return Item(name=self.name, description=self.description, quantity=self.quantity, type_id=self.type_id, **dict(self.nbt))

    def stack_key(self):
        """Identity for stacking: everything EXCEPT quantity. Two items with the same
        stack_key are 'identical' and merge into one stack."""
        return (self.type_id, self.name, self.description, tuple(sorted(self.nbt.items())))

    def __repr__(self):
        return f"{self.type_id}{_nbt_string(self._all_nbt())}"


class Talent:
    """A passive buff from expertise. `proc` is a Proc (trigger conditions); `reaction` is a
    Cost-style list of class-path deltas applied to the owner when it fires (negative = damage).

    e.g. proc=[{in_combat:true,end_of_turn:true}], reaction=[stats.health(-5)] -> when the
    owner is in combat at end of turn, they take 5 damage. (Real AoE/targeting comes later;
    for now the reaction modifies the owner's own stats.)
    """
    type_id = "talent"
    GENERIC_NBT = ["name", "level", "description", "origin", "proc", "reaction"]
    NBT = []
    CORE = ("name", "level", "description", "origin")
    DEFAULTS = {"level": 1, "description": "", "origin": "", "proc": None, "reaction": None}
    _STRUCTURED = {"name", "level", "description", "origin", "proc", "reaction"}

    def __init__(self, name="", level=1, description="", origin="", proc=None, **nbt):
        self.name = name
        self.level = level
        self.description = description
        self.origin = origin
        self.proc = Proc.parse(proc) if proc is not None else None
        self.reaction = None
        self.nbt = dict(nbt)

    @classmethod
    def from_nbt(cls, ident, nbt):
        """Build from a leading name token + parsed nbt: Talent.from_nbt('chainsaw', {...})."""
        return _fill_generic_nbt(cls(name=ident).modify(**nbt))

    def modify(self, **fields):
        """Edit any field by name. proc -> Proc, reaction -> Cost; unknown keys go to nbt."""
        for key, val in fields.items():
            if key == "proc":
                self.proc = Proc.parse(val) if val is not None else None
            elif key == "reaction":
                self.reaction = Reaction.parse(val) if val is not None else None
            elif key in self.CORE:
                setattr(self, key, val)
            else:
                self.nbt[key] = val
        return self

    def field_value(self, key):
        if key in ("proc", "reaction") or key in self.CORE:
            return getattr(self, key)
        return self.nbt.get(key)

    def reset(self, *keys):
        return _reset_fields(self, self.DEFAULTS, keys)

    def __repr__(self):
        fields = {"level": self.level, "description": self.description, "origin": self.origin,
                  "proc": self.proc, "reaction": self.reaction}
        fields.update(self.nbt)
        return f"{self.name}{_nbt_string(fields)}"


class Ability:
    """A castable with a cost and cooldown, both unit-aware via UnitValue."""
    type_id = "ability"
    GENERIC_NBT = ["name", "description", "damage", "affinity", "cost", "cooldown", "cast_type", "on_hit"]
    NBT = []

    def __init__(self, name="", level=1, description="", cooldown=None, cost=None, origin="", on_hit=None, **nbt):
        self.name = name
        self.level = level
        self.description = description
        self.cooldown = Cooldown.parse(cooldown) if cooldown is not None else None
        self.cost = Cost.parse(cost) if cost is not None else None
        self.on_hit = Reaction.parse(on_hit) if on_hit is not None else None  # applied to the TARGET
        self.origin = origin
        self.nbt = dict(nbt)

    CORE = ("name", "level", "description", "cooldown", "cost", "on_hit", "origin")
    DEFAULTS = {"level": 1, "description": "", "cooldown": None, "cost": None, "on_hit": None, "origin": ""}
    _STRUCTURED = {"name", "description", "cost", "cooldown", "on_hit"}

    @classmethod
    def from_nbt(cls, ident, nbt):
        """Build from a leading name token + parsed nbt: Ability.from_nbt('firebolt', {...})."""
        return _fill_generic_nbt(cls(name=ident).modify(**nbt))

    def modify(self, **fields):
        """Edit any field by name: ability.modify(cooldown='4/7', cost='[stats.mana(20)]', level=2).
        cooldown -> Cooldown, cost -> Cost; unknown keys go to nbt."""
        for key, val in fields.items():
            if key == "cooldown":
                self.cooldown = Cooldown.parse(val) if val is not None else None
            elif key in ("cooldown[0]", "cooldown[1]"):
                # set just the current (index 0) or total (index 1) of the cooldown
                if not isinstance(self.cooldown, Cooldown):
                    self.cooldown = Cooldown()
                if key.endswith("[0]"):
                    self.cooldown.current = int(val)
                else:
                    self.cooldown.total = int(val)
            elif key == "cost":
                self.cost = Cost.parse(val) if val is not None else None
            elif key == "on_hit":
                self.on_hit = Reaction.parse(val) if val is not None else None
            elif key in ("name", "level", "description", "origin"):
                setattr(self, key, val)
            else:
                self.nbt[key] = val
        return self

    def field_value(self, key):
        return getattr(self, key) if key in self.CORE else self.nbt.get(key)

    def reset(self, *keys):
        return _reset_fields(self, self.DEFAULTS, keys)

    def __repr__(self):
        fields = {"level": self.level, "description": self.description, "cooldown": self.cooldown,
                  "cost": self.cost, "on_hit": self.on_hit, "origin": self.origin}
        fields.update(self.nbt)
        return f"{self.name}{_nbt_string(fields)}"


class Effect:
    """A status effect (buff/debuff) that DOES something over its lifetime:
      duration  - how many turns it lasts (int)
      step      - WHICH of those turns it fires on, as Python slice/index specs over the
                  turn list [1..duration]:  :1 first, -1 last, ::3 every 3rd, : (or none) every turn
      proc      - proc signals to PULSE on a firing turn (so a talent keyed on e.g. 'poison' fires)
      reaction  - a Reaction (class-path deltas and/or a /function) applied on a firing turn
    The command layer (repl) walks `step` each turn and fires proc+reaction (see _tick_effects)."""
    type_id = "effect"
    GENERIC_NBT = ["name", "duration", "step", "proc", "reaction", "description"]
    NBT = []
    CORE = ("name",)
    DEFAULTS = {"duration": None, "step": None, "proc": None, "reaction": None, "description": ""}
    _STRUCTURED = {"name", "duration", "step", "proc", "reaction", "description"}

    def __init__(self, name="", duration=None, step=None, proc=None, reaction=None, description="", **nbt):
        self.name = name
        self.duration = duration
        self.step = _parse_token_list(step)
        self.proc = _parse_token_list(proc)
        self.reaction = Reaction.parse(reaction) if reaction is not None else None
        self.description = description
        self.nbt = dict(nbt)
        self._elapsed = 0     # turns this effect has been active (set by the turn ticker)
        self._total = None    # its starting duration (captured on first tick)

    @classmethod
    def from_nbt(cls, ident, nbt):
        return _fill_generic_nbt(cls(name=ident, **nbt))

    def field_value(self, key):
        if key in ("name", "duration", "step", "proc", "reaction", "description"):
            return getattr(self, key)
        return self.nbt.get(key)

    def modify(self, **fields):
        for key, val in fields.items():
            if key == "step":
                self.step = _parse_token_list(val)
            elif key == "proc":
                self.proc = _parse_token_list(val)
            elif key == "reaction":
                self.reaction = Reaction.parse(val) if val is not None else None
            elif key in ("name", "duration", "description"):
                setattr(self, key, val)
            else:
                self.nbt[key] = val
        return self

    def reset(self, *keys):
        return _reset_fields(self, self.DEFAULTS, keys)

    def __repr__(self):
        fields = {"duration": self.duration, "step": self.step, "proc": self.proc,
                  "reaction": self.reaction, "description": self.description}
        fields.update(self.nbt)
        return f"{self.name}{_nbt_string(fields)}"


def _modify_fields(obj, core, fields):
    """Apply field=value edits: known `core` names set attributes, the rest go to nbt."""
    for key, val in fields.items():
        if key in core:
            setattr(obj, key, val)
        else:
            obj.nbt[key] = val
    return obj


def _fill_generic_nbt(obj):
    """Make a freshly-created element show ALL its completable generic keys: any GENERIC_NBT
    key that isn't a structured field and isn't already set defaults to None (or its entry in
    the class's _NBT_DEFAULTS). Keeps 'what you see on creation' identical to 'what TAB offers
    under modify' (per the user's consistency ask)."""
    structured = getattr(obj, "_STRUCTURED", set())
    defaults = getattr(obj, "_NBT_DEFAULTS", {})
    for key in getattr(type(obj), "GENERIC_NBT", []):
        if key != "name" and key not in structured and key not in obj.nbt:
            obj.nbt[key] = defaults.get(key)
    return obj


def _reset_fields(obj, defaults, keys):
    """Reset: core keys (in `defaults`) go to their default; other keys are removed from nbt.
    No keys = reset all core defaults and clear nbt."""
    if not keys:
        for key, default in defaults.items():
            setattr(obj, key, default)
        obj.nbt.clear()
        return obj
    for key in keys:
        if key in defaults:
            setattr(obj, key, defaults[key])
        else:
            obj.nbt.pop(key, None)
    return obj


class Stat:
    """A single stat name -> value. Stats are now ONE merged namespace (attributes folded in):
    a BASE stat has no formula (set directly / by gear); a DERIVED stat owns a formula."""
    type_id = "stat"
    GENERIC_NBT = ["name", "value"]
    NBT = []

    def __init__(self, name, value, **nbt):
        self._container = None
        self._name = name
        self.value = value
        self.nbt = dict(nbt)

    @property
    def name(self):
        return self._name

    @name.setter
    def name(self, new_name):
        old = getattr(self, "_name", None)
        self._name = new_name
        if getattr(self, "_container", None) is not None and old != new_name:
            self._container._rekey(old, new_name, self)

    def set(self, value):
        """Set the value outright: health.set(3)."""
        self.value = value
        return self

    def add(self, amount):
        """Shift the value by amount: health.add(3) or health.add(-3)."""
        self.value += amount
        return self

    def modify(self, **fields):
        """Edit any field by name: health.modify(value=3, name='hp')."""
        return _modify_fields(self, ("name", "value"), fields)

    def __repr__(self):
        return f"{self.name}={self.value}"


# --- Containers (independent classes, consistent method names) ---------------

class Inventory:
    """Holds loose Items. `slots` caps the number of stacks (None = unlimited).

    Stack-merging is a mechanic decision left for later; each add() is one entry.
    """
    def __init__(self, slots=None):
        self.slots = slots
        self.items = []

    def __len__(self):
        return len(self.items)

    def __iter__(self):
        return iter(self.items)

    def __repr__(self):
        cap = self.slots if self.slots is not None else "∞"
        return f"Inventory({len(self.items)}/{cap}): {self.items}"

    def is_full(self):
        return self.slots is not None and len(self.items) >= self.slots

    def add(self, item, merge=True):
        """Add an item. By default merges into an existing IDENTICAL stack (same
        stack_key) by summing quantity; pass merge=False to always add a new entry."""
        if merge:
            for existing in self.items:
                if existing.stack_key() == item.stack_key():
                    existing.quantity += item.quantity
                    return existing
        if self.is_full():
            raise ValueError(f"Inventory full ({self.slots} slots)")
        self.items.append(item)
        return item

    def remove(self, item):
        self.items.remove(item)
        return item

    def get(self, name):
        return next((i for i in self.items if i.name == name or i.type_id == name), None)

    def find(self, name):
        return [i for i in self.items if i.name == name or i.type_id == name]


class Equipment:
    """Slotted worn gear: slot name -> list of Items.

    A slot capacity may be a fixed int or a callable(owner) -> int, so capacities
    can depend on Stats (ring-slot capacity = owner.stats.get('fingers')). `owner`
    is the Player, set by Player so capacity callables can read live stats.
    """
    def __init__(self, owner=None):
        self.owner = owner
        self.slots = {}
        self.capacities = {}

    def define_slot(self, name, capacity=1):
        self.slots.setdefault(name, [])
        self.capacities[name] = capacity
        return self

    def capacity_of(self, name):
        cap = self.capacities.get(name, 1)
        return cap(self.owner) if callable(cap) else cap

    def equip(self, slot, item):
        if slot not in self.slots:
            raise KeyError(f"Slot '{slot}' is not defined; call define_slot() first")
        if len(self.slots[slot]) >= self.capacity_of(slot):
            raise ValueError(f"Slot '{slot}' is full (capacity {self.capacity_of(slot)})")
        self.slots[slot].append(item)
        return item

    def unequip(self, slot, item=None):
        worn = self.slots.get(slot, [])
        if not worn:
            raise ValueError(f"Nothing equipped in slot '{slot}'")
        return worn.pop() if item is None else worn.pop(worn.index(item))

    def equipped(self, slot=None):
        if slot is not None:
            return list(self.slots.get(slot, []))
        return {s: list(v) for s, v in self.slots.items() if v}

    def __iter__(self):
        for items in self.slots.values():
            yield from items

    def __repr__(self):
        return f"Equipment({ {s: v for s, v in self.slots.items() if v} })"


class Talents:
    """Holds a player's Talents; supports lookup by the proc that triggers them."""
    def __init__(self):
        self.talents = []

    def __len__(self):
        return len(self.talents)

    def __iter__(self):
        return iter(self.talents)

    def __repr__(self):
        return f"Talents({self.talents})"

    def add(self, talent):
        self.talents.append(talent)
        return talent

    def remove(self, talent):
        self.talents.remove(talent)
        return talent

    def get(self, name):
        return next((t for t in self.talents if t.name == name), None)

    def by_proc(self, proc):
        return [t for t in self.talents if t.proc == proc]


class Abilities:
    """Holds a player's castable Abilities."""
    def __init__(self):
        self.abilities = []

    def __len__(self):
        return len(self.abilities)

    def __iter__(self):
        return iter(self.abilities)

    def __repr__(self):
        return f"Abilities({self.abilities})"

    def add(self, ability):
        self.abilities.append(ability)
        return ability

    def remove(self, ability):
        self.abilities.remove(ability)
        return ability

    def get(self, name):
        return next((a for a in self.abilities if a.name == name), None)


class Effects:
    """Holds a player's status Effects (buffs/debuffs)."""
    def __init__(self):
        self.effects = []

    def __len__(self):
        return len(self.effects)

    def __iter__(self):
        return iter(self.effects)

    def __repr__(self):
        return f"Effects({self.effects})"

    def add(self, effect):
        self.effects.append(effect)
        return effect

    def remove(self, effect):
        self.effects.remove(effect)
        return effect

    def get(self, name):
        return next((e for e in self.effects if e.name == name), None)


class Stats:
    """Maps stat name -> Stat (the one merged namespace; see stats.csv for the generic set)."""
    def __init__(self):
        self.stats = {}

    def __len__(self):
        return len(self.stats)

    def __iter__(self):
        return iter(self.stats.values())

    def __getitem__(self, name):
        return self.stats[name]

    def __contains__(self, name):
        return name in self.stats

    def __repr__(self):
        return f"Stats({list(self.stats.values())})"

    def add(self, stat):
        stat._container = self
        self.stats[stat.name] = stat
        return stat

    def set(self, name, value):
        if name in self.stats:
            self.stats[name].value = value
        else:
            self.add(Stat(name, value))
        return self.stats[name]

    def get(self, name, default=None):
        stat = self.stats.get(name)
        return stat.value if stat is not None else default

    def modify(self, **fields):
        """Set stats by name (create or overwrite): modify(health=3, mana=5)."""
        for name, value in fields.items():
            self.set(name, value)
        return self

    def field_value(self, name):
        return self.get(name)

    def reset(self, *names):
        """Remove the named stats; no names = remove all."""
        if not names:
            self.stats.clear()
        else:
            for name in names:
                self.remove(name)
        return self

    def _rekey(self, old, new, obj):
        """Move an entry when its object is renamed, so the key tracks .name."""
        if old in self.stats and self.stats[old] is obj:
            del self.stats[old]
        self.stats[new] = obj

    def remove(self, name):
        return self.stats.pop(name, None)


# --- Player (ties every container together) ----------------------------------

class Player:
    """A player/character: identity, containers, and free-form NBT.

    Like an Item, a Player has HANDLED keys (name, inventory_slots — they route to real
    structure) plus a dynamic `nbt` dict for anything else (race, class, subclass, ...).
    GENERIC_NBT is the autofill suggestion list; type_id keys it in the completer."""
    type_id = "player"
    # NBT_TIERS drives autofill order + grouping (primary shown first, then ---, etc.).
    NBT_TIERS = [
        ["name", "class", "race", "clan", "level", "alignment", "sex"],
        ["background", "backstory", "oath", "deity"],
        ["eyes", "structure", "dominant_hand", "height", "hair", "character_details"],
    ]
    GENERIC_NBT = [key for tier in NBT_TIERS for key in tier] + ["inventory_slots", "npc"]
    NBT = []
    _STRUCTURED = {"name", "inventory_slots"}  # routed to real structure, not the nbt dict
    _NBT_DEFAULTS = {"npc": False}  # a flag reads better as False than None

    def __init__(self, name="Unknown", inventory_slots=None):
        self.name = name
        self.inventory = Inventory(slots=inventory_slots)
        self.equipment = Equipment(owner=self)
        self.talents = Talents()
        self.abilities = Abilities()
        self.effects = Effects()
        self.stats = Stats()
        for stat_name in STAT_NAMES:        # seed every generic stat (from stats.csv) at 0 on creation
            self.stats.set(stat_name, 0)
        self.formulas = Formulas()  # derived-stat definitions (start from the generic set)
        for stat_name, expr in DEFAULT_FORMULAS.items():
            self.formulas.add(stat_name, expr)
        self.nbt = {}
        self.proc_state = {}  # active proc signals (name -> bool), set by /proc
        self._last_seen = {}  # ref -> value at last recompute, for DELTA(...) in formulas

    def recompute(self):
        """Evaluate every formula and write the result into stats. Formulas read other stats
        (stat.X — atr.X is accepted as an alias); cross-stat refs resolve on demand (cycle-guarded),
        so magia_pool is computed before the school stats that use it. A formula that references
        ITSELF (e.g. health = MIN(health, max_health)) reads its current value, so it accumulates
        / clamps. DELTA(ref) returns ref's change since the last recompute (recorded once per pass)."""
        cache, pending = {}, {}

        def get_stat(name):
            if name in cache:
                return cache[name]
            formula = self.formulas.get(name)
            if formula is None:
                value = self.stats.get(name)
                return value if value is not None else 0
            current = self.stats.get(name)  # self-reference resolves to the current value
            cache[name] = current if current is not None else 0
            cache[name] = formula.evaluate(get_stat, get_stat, delta)
            return cache[name]

        def delta(ref, current):
            change = current - self._last_seen.get(ref, current)  # 0 the first time a ref is seen
            pending[ref] = current
            return change

        for stat_name in list(self.formulas):
            self.stats.set(stat_name, get_stat(stat_name))
        self._last_seen.update(pending)  # advance the snapshot once, after the whole pass
        return self

    def _handled(self):
        """Keys with special routing -> (getter, setter, default). Everything else is nbt."""
        return {
            "name": (lambda: self.name, lambda v: setattr(self, "name", v), "Unknown"),
            "inventory_slots": (lambda: self.inventory.slots, lambda v: setattr(self.inventory, "slots", v), None),
        }

    def field_value(self, key):
        """Read any field: a handled key or a dynamic nbt key (None if unset)."""
        handled = self._handled()
        return handled[key][0]() if key in handled else self.nbt.get(key)

    def modify(self, **fields):
        """Set fields: handled keys route to structure, the rest go to nbt."""
        handled = self._handled()
        for key, val in fields.items():
            if key in handled:
                handled[key][1](val)
            else:
                self.nbt[key] = val
        return self

    def reset(self, *keys):
        """Reset handled keys to their default; remove dynamic nbt keys. No keys = reset all."""
        handled = self._handled()
        if not keys:
            for getter, setter, default in handled.values():
                setter(default)
            self.nbt.clear()
            return self
        for key in keys:
            if key in handled:
                handled[key][1](handled[key][2])
            else:
                self.nbt.pop(key, None)
        return self

    def __repr__(self):
        base = (f"Player({self.name!r}: {len(self.inventory)} items, {len(self.talents)} talents, "
                f"{len(self.abilities)} abilities, {len(self.effects)} effects, "
                f"{len(self.stats)} stats")
        slots = self.inventory.slots
        extra = ({"inventory_slots": slots} if slots is not None else {}) | self.nbt
        return base + (f"; {extra})" if extra else ")")


class Mob(Player):
    """A summoned creature: mechanically a Player (stats/attributes/abilities/can be killed &
    targeted, found by @!a/@m), but created via /summon with its OWN generics — mob:true,
    npc:true, species — instead of the character tier fields (race/class/eyes/...). Built for
    hordes (a pack of wolves), not one nuanced character."""
    type_id = "mob"
    GENERIC_NBT = ["name", "species", "mob", "npc", "x", "y"]
    NBT = []
    _STRUCTURED = {"name", "inventory_slots"}      # routed to structure, like Player
    _NBT_DEFAULTS = {"mob": True, "npc": True}      # what makes it a mob NPC by default

    def __repr__(self):
        return f"Mob({self.name!r}: {len(self.stats)} stats, {len(self.abilities)} abilities; {self.nbt})"

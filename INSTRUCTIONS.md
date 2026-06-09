# TTRPG World Model — How To Use It

## Files

### Code

| File | What it is | Creates anything on its own? |
|---|---|---|
| `containers.py` | The library: all the classes (Player, Item, Stat, …). | **No.** Just definitions. |
| `autofill.py` | Completion engine + a ready `autofill` object. | No game entities. |
| `repl.py` | The interactive command shell + live TAB completion. | Only when you run it. |
| `main.py` | Entry point — launches the shell. | Launches `repl.run()`. |

### Data

| File | What it is |
|---|---|
| `materials.csv` | Contains information about different materials within the world, their strengths and generic stat modifiers |
| `body_coverage.csv` | Contains proportions on how to handle how much coverage you get from a helmet or chestplate or etc. |
| `stats.csv` | Contains all of the player's generic stats, on new characer creation initialize all of these to None |
| `prefixes.csv` | Contains recognized prefixes for gear, prefixes modify gear's stats and can be rerolled |
| `suffixes.csv` | Contains recognized suffixes for gear, suffixes modify gear's capabilities and need to be renechanted or resocketed |
| `weather.csv` | Contains recognized weather types and weights as well as persistence which help determine how likely the weather event is to occur and how long it should linger to reflect how long rain or sun etc. last |
| `biomes.json` | Contains all information about specific biomes that can appear including weather modifiers and ASCII visuals |
| `bestiary.json` | Contains mobs that players can encounter |

---

## A) The command shell

```bash
cd /Users/jnoael/ttrpg
python3 main.p
```

You get a `ttrpg>` prompt. **Press TAB to complete at any position** — like `cd <TAB>`:

- `/char` + TAB → `/character` · `/` + TAB → every command
- `/character ` + TAB → `add modify get show list remove` (subcommands)
- `/character add ` + TAB → existing character names
- `/stat hero ` + TAB → `set add modify get list` (operations)
- inside item NBT, e.g. `iron_sword{pr` + TAB → `prefix`

Commands so far (the rest are recognized but not wired yet):

**Terminology:** `<selector>` = who (a name, or `@a` = all players, `@!a` = all non-player
entities — none yet). `<obj>` = which item (a type_id, a name, an `[index]`, `type[N]`, or
`type{nbt}`). `<set|add|reset>` = the operation. **TAB lists what's available at each spot.**

```
/character add <name>[{nbt}]                     create a character, optionally with NBT
/character modify <selector> <set|add|reset> {nbt}   edit dynamic NBT (see below)
/character modify <name>.<field>(<value>)        legacy single-field set (still works)
/character get <selector> <field>                read a field (handled or dynamic)
/character show <selector>   |   /character list   |   /character remove <selector>

/stat add <selector> <name> [value]               create/set a stat (or {name:val,...} bulk)
/stat modify <selector> <name> <set|add|reset> <value>   set/add to the value; reset -> 0
/stat get <selector> <name>  |  list <selector>  |  remove <selector> <name>
/attribute ...                                   identical, on attributes

/item add <selector> <obj>[{nbt}] [count]         create an item -> inventory (merges if identical)
/item remove <selector> <obj> [count]             remove count copies (default: whole stack)
/item list <selector>                             show the inventory (indices + counts)
/item modify <selector> <obj> <set|add|reset> <field> <value>   edit (or set|add|reset {nbt})
/item loot <table> <selector>                     DUMMY roll (real loottables come later)

/kill <selector>                                  remove player(s) (shorthand; will cover NPCs)
/turn query | next | add <value> | set <value> [silent]    the turn counter (see below)
/talent  <add|list|modify|remove> <selector> <name>{nbt}   manage talents
/effect  <add|list|modify|remove> <selector> <name>{nbt}   manage status effects
/ability <add|list|modify|remove> <selector> <name>{nbt}   manage abilities
/ability cast <selector> <obj>                             pay cost + trigger cooldown
/proc <pulse|enable|disable|query> <selector> <a,b,c>      fire talents via signals
```

**Procs & talents** — a talent can react to signals:

```
proc:[{in_combat:true,end_of_turn:true}]   conditions that must ALL hold (AND)
reaction:[stats.health(-5)]                what happens when it fires (class-path deltas)
```

`/proc` sets signals on the selector and fires any talent whose `proc` conditions match
the player's current signals (absent signal = false, so `scared:false` holds until you
enable `scared`):

```
/proc enable noael in_combat        # sticky: stays on; fires matching talents now
/proc enable noael end_of_turn      # now in_combat AND end_of_turn -> deathblast fires
/proc pulse  noael in_combat,end_of_turn   # momentary: on -> fire -> revert
/proc disable noael in_combat       # turn a signal off
/proc query  noael                  # show active signals
```

Proc names autocomplete from `proc_tree.json` (a big namespace of suggestions —
`in_combat`, `on_turn_end`, `damage_dealt`, …) but you can use any name you like. The
reaction modifies the talent owner's own stats for now (real targeting/AoE comes later).

**Cooldowns** are `current/total` turns: `0/7` = ready, `7/7` = just cast. Add an ability
with `cooldown:7` (total; starts ready at `0/7`) or `cooldown:4/7` (4 turns left). Casting
sets `current = total`; each turn (`/turn next|add`) counts it down to `0`. Set either side
on its own with `set cooldown[0] <n>` (current) or `set cooldown[1] <n>` (total), or both
at once with `set cooldown <cur>/<tot>`.

`add` appends to list fields too: `... add cost stats.health(1)` tacks another cost onto
the existing list (alongside numeric-add and string-concat).

**Costs** are a list of class-path deductions spent on cast:
`cost:[stats.mana(20),stats.health(1)]` — the path picks WHICH stat/attribute to spend, so
costs can come from anything (`attributes.max_mana(1)` works too). `/ability cast` checks
you can afford all of them (else it casts nothing) and that the cooldown is ready, and lists
**every** blocking reason at once (on cooldown + each unmet cost), not just the first.

```
/ability add noael firebolt{cooldown:3,cost:[stats.mana(20),stats.health(1)]}
/ability cast noael firebolt        # mana-20, health-1, cooldown -> 3/3
/turn add 3                         # cooldown ticks 3/3 -> 0/3 (ready again)
```

**Picking a specific item** (`<obj>`) when you have duplicates — TAB after the selector
lists them:

```
/item modify noael [0] set name "x"               # by inventory index (from /item list)
/item modify noael healing_potion[1] set name "x" # the 2nd healing_potion stack
/item modify noael healing_potion{name:"x"} ...   # the stack matching those nbt fields
/item modify noael healing_potion ...             # the first matching stack
```

**`/turn`** — a global turn counter (foundation for per-turn procs later):

```
/turn query          show the current turn
/turn next           +1   (same as /turn add 1)
/turn add <n>        advance by n
/turn set <n>        jump to n, walking each turn; prints the signed difference
/turn set <n> true   jump to n silently (just corrects the counter, nothing ticks)
```

**Advancing a turn ticks the clock:** each turn that passes (`next`, `add`, or a forward
`set`) decrements every ability's `cooldown` (clearing it at 0 = ready) and every effect's
`duration` (removing the effect at 0). `set <n> true` is the escape hatch that fixes the
number without ticking. `_advance_turn` in repl.py is also where future per-turn procs
(heal-per-turn, etc.) will fire.

Element displays use **full field names** in the `name{...}` form, e.g.
`firebolt{level:1,description:"",cooldown:2t,cost:200mana,origin:""}` — what you see is what
you type (`set cooldown 4t`, not `cd`).

`@a` applies a command to every player at once (`/stat @a set health 20`, `/item give @a potion`).

### Dynamic NBT: `set` / `add` / `reset`

Characters and items both have **free-form NBT** — handled keys route to real structure
(a character's `inventory_slots` → inventory capacity; an item's `count` → quantity),
and any other key (`race`, `class`, `subclass`, `sharpness`, `rage`…) is just stored.
Nothing needs to be predefined — invent a key and use it. The three ops, shared by
`/character modify` and `/item modify`:

```
set {nbt}      overlay the given keys      set {race:"elf",inventory_slots:7}
set <f> <v>    one field                   set name "murmur"
add {nbt}      numeric add / string concat add {inventory_slots:5}   add {name:" world"}
reset {keys}   handled keys -> default; dynamic keys removed     reset {subclass}
reset {}       reset everything
```

The op word can sit on either side (`set name X` or `name set X`), like `/stat`. You can
also create with NBT directly: `/character add hero{race:"tiefling",inventory_slots:7}`.

`/item modify` affects **one** copy by default, splitting it off the stack; use
`<obj>{count:N}` to affect N (`add` concatenates strings, e.g. → "murmur the blade").
The `<obj>` selector picks **which** item when you have duplicates:

```
/item modify hero [1] set name "murmur"            # by inventory index (see /item list)
/item modify hero iron_sword{name:"poop"} add count 1   # by matching NBT fields
/item modify hero iron_sword{count:2} add count 5  # affect 2 copies
/item modify hero murmur add name " the blade"     # by name; string concat
/item modify hero murmur reset {prefix}            # remove a key
```

Reading values without a command (bare path, no slash):

```
hero.inventory_slots          ->  27
hero.race                     ->  'tiefling'   (dynamic nbt key)
hero.stats.health             ->  6
hero.inventory_slots(3)       ->  sets it (same as /character modify)
hero.subclass("ranger")       ->  sets a dynamic key
```

`/stat`'s operation word is found wherever it sits, so **both of these work**:
`/stat hero health set 3` and `/stat hero set health 3`. set/add/modify on a missing
stat create it at 0 first, so `/stat hero health add 3` on a fresh character gives 3.

NBT text round-trips: what an item prints as is exactly what `/item give` parses back.
Strings go in double quotes; an interior `"` is written `\"`; apostrophes need nothing.
A trailing count overrides any `count:` in the NBT.

```
/item give hero potion 5
/item give hero iron_sword{prefix:sharp,origin:"Dungeon Chest"}
/item give hero iron_sword{name:"Ira' kurri",description:"says \"hi\", loudly"} 9
```

> **Stacking:** identical items auto-merge. "Identical" = same `type_id` AND same
> name/description/NBT (count is summed, not compared). So `/item give hero iron_sword`
> twice = one stack of x2. Modifying part of a stack splits it back out (see `/item modify`).
> Known limit: an item whose **name has spaces** can't be selected by name yet (the selector
> reads a single token) — rename back or use its `type_id`.

Abbreviations work everywhere: `/char a` + TAB → `add`; `/char add hero` == `/character add hero`.

**Adding more:** a settable `/character` field = one line in `CHARACTER_FIELDS` (now a
`(getter, setter)` pair) in `repl.py`. A new command = a handler + a line in `HANDLERS`;
make its args completable via `ARG_SCHEMA` / `SUBCOMMANDS`. Still to wire: `/effect`,
`/talent`, `/ability`, `/kill`, `/summon`, `/execute`.

---

## B) The Python API

```python
from containers import Player, Item, Talent, Ability, Attribute, Stat
```

### Create a character

```python
hero = Player("Alfred Winkleberry")                       # unlimited inventory
hero = Player("Alfred Winkleberry", inventory_slots=27)   # capped at 27 stacks
```

Every player starts with empty containers: `hero.inventory`, `hero.equipment`,
`hero.stats`, `hero.attributes`, `hero.talents`, `hero.abilities`.

### Stats and Attributes — `set` / `add` / `modify`

`set`/`add`/`modify` are methods **on the value object itself** (this is what you asked for).
`Stats`/`Attributes` work identically.

```python
# create a stat — returns the Stat object so you can chain
health = hero.stats.set("health", 20)     # health == "health=20"

# then operate on the object:
health.set(3)        # set value outright   -> health=3
health.add(5)        # shift by +5          -> health=8
health.add(-2)       # shift by -2          -> health=6
health.modify(value=99)            # edit any field by name
health.modify(name="hp", value=10) # rename + set in one call

# you don't have to keep the handle around — grab it by name any time:
hero.stats["health"].add(-3)       # the Stat object
hero.stats.get("health")           # just the number (or a default): 16
hero.stats.modify("health", value=50)   # edit by name via the container
hero.stats.remove("health")        # delete it
```

Attributes are the same: `hero.attributes.set("max_health", 20)`, then `.add`, `.modify`, etc.

> **Renaming auto-reindexes:** `health.modify(name="hp")` (or `health.name = "hp"`) moves the
> entry so `stats["hp"]` works immediately and `stats["health"]` is gone. Characters renamed via
> `/character modify hero.name(...)` re-key in the shell's `WORLD` the same way.

### Items

Three core fields + a free-form NBT bag for everything else.

```python
sword = Item("Ira' kurri", "A humming blade.", quantity=1,
             type_id="iron_sword", prefix="sharp", origin="Dungeon Chest")

sword.modify(name="Ira' Kurri", count=7, prefix="vicious")   # edit any field; count -> quantity
print(sword)
# iron_sword{name:"Ira' Kurri",description:"A humming blade.",count:7,prefix:"vicious",origin:"Dungeon Chest"}
```

Apostrophes/quotes in names are fine — internally it's a plain string; escaping only happens
when it's printed as NBT text.

```python
hero.inventory.add(sword)
hero.inventory.get("Ira' kurri")   # first match (or None)
hero.inventory.find("Potion")      # all matches
hero.inventory.remove(sword)
list(hero.inventory)               # everything
```

### Equipment

Define slots first; capacity can be a number **or** depend on a stat.

```python
hero.equipment.define_slot("helmet", capacity=1)
hero.equipment.define_slot("ring", capacity=lambda owner: owner.stats.get("fingers", 0))
hero.stats.set("fingers", 10)
hero.equipment.capacity_of("ring")        # -> 10

hero.equipment.equip("ring", Item("Band of Sparks", type_id="copper_ring"))
hero.equipment.unequip("ring")            # last one, or pass the item
hero.equipment.equipped()                 # {'ring': [...]}  (non-empty slots only)
```

### Talents and Abilities

```python
hero.talents.add(Talent("chainsaw", level=1, description="fell trees instantly", proc="chopped_tree"))
hero.talents.get("chainsaw")
hero.talents.by_proc("chopped_tree")      # talents triggered by an event

fb = hero.abilities.add(Ability("firebolt", level=1, cooldown="2t", cost="200mana"))
hero.abilities.get("firebolt")
fb.cost.amount, fb.cost.unit              # -> (200, "mana")
fb.modify(cooldown="3t", cost="1hp")      # re-parses units
```

All elements (Item/Talent/Ability/Attribute/Stat) have `.modify(**fields)` for editing
any field; Stat/Attribute additionally have `.set(value)` / `.add(amount)`.

---

## C) Autofill (the engine behind TAB)

The vocabulary lives **on the classes**: each has `GENERIC_NBT` (common keys) + `NBT`
(type-specific). When you create a specific item type, register it and TAB knows it:

```python
from autofill import autofill

class IronSword(Item):
    type_id = "iron_sword"
    NBT = ["sharpness", "reach"]          # merged with inherited GENERIC_NBT

autofill.register_class(IronSword)
autofill.register_command("teleport")     # add a command name

# direct calls (TAB uses these under the hood):
autofill.complete_command("/gi")          # -> "/give"
autofill.suggest_command("/a")            # -> ["/ability", "/attribute"]
autofill.complete_nbt_key("sh", "iron_sword")  # -> "sharpness"
```

---

## Not built yet (on purpose — your call later)

- The `/give`, `/summon`, `/effect`, `/execute`, `/kill`, `/talent`, `/ability`,
  `/attribute` handlers (names are reserved; only `/character` is wired).
- NBT **default values** + naming conventions per item type.
- Parsing NBT **text back into objects** (objects → text works today).
- The **proc event tree**.
- Loading the **reference CSVs** into autofill / validation.
- Equip-time **temporary buffs**.
- **Saving/loading** to disk (the shell's world is in-memory per session).
- The `/ai` assistant.

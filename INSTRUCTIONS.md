# TTRPG World Model — Command Reference

Run it:

```bash
cd /Users/jnoael/ttrpg
python3 main.py
```

You get a `ttrpg>` prompt. **Press TAB anywhere** to complete commands, subcommands, selectors, item names, NBT keys, and operators. Abbreviations resolve (`/char`→`/character`).

---

## Functions, reactions & hooks (read this first)

A **function** is a saved list of command lines. **Reactions** and **hooks** (`on_hit`, `on_equip`, `proc reaction`, …) can run deltas AND/OR call functions. The #1 syntax trap is how you call a function from inside a reaction.

### Calling a function from a reaction/hook

A function call is **just the bare function name**. These all work:

```
[poison]                 # calls function "poison"
[/function poison]       # same
[/poison]                # same
```

These DO NOT work:

```
[/function run poison]   # IGNORED — the "run" keyword + space breaks it
[function:poison]        # treated as a function literally named "function:poison" (missing)
```

A reaction is a list; mix deltas and function calls freely (run in order):

```
reaction:[stat.health(-5), poison, stat.mana(-2)]
```

- **delta** = `path(amount)` → e.g. `stat.health(-5)`, `inventory.rope.count(-1)`. Negative = subtract. (Containers are singular: `stat`, `talent`, `effect`, `ability`, `quest`, `nbt` — `attribute`/`atr` still read the stat namespace from the merge.)
- **function** = a bare name → runs that `/function`'s lines.

### Where reactions/hooks live

| Element | Field | Fires when | `@s` (subject) | `@o` (origin/cause) |
|---|---|---|---|---|
| Talent | `proc` + `reaction` | its proc signals match | the talent owner | the owner |
| Ability | `on_hit` | `/ability cast` lands | the target | the caster |
| Item | `on_equip` / `on_unequip` | equipped / removed | the wearer | (inherited) |
| Effect | `proc` + `reaction` | on its firing turns | the afflicted | (inherited) |

Inside a called function, `@s` = who the effect acts on, `@o` = who caused it. Both persist through nested function calls.

### `/function`

```
/function edit <name>          open the line editor (create or edit)
/function run <name> [sel]     run it; @s = each target. No selector → inherits caller's @s/@o
/function list | show <name> | remove <name>
```

**Editor** (`name:N>` prompt): type command lines (TAB completes them). Meta-commands:

```
:list   :del N   :ins N <command>   :move A B   :swap A B   :done   :cancel   :help
```

**Returns:** `True` if it ran.

---

## Selectors

`<selector>` = who a command targets.

| Token | Means |
|---|---|
| `name` | that character |
| `@a` / `@!a` | all players / all NPCs |
| `@m` | all mobs |
| `@` | every entity |
| `@p` / `@!p` | previous selection / everyone but it |
| `@s` / `@!s` | the subject of the running effect / everyone but it |
| `@o` / `@!o` | the origin/cause of the running effect / everyone but it |
| `[a,b,c]` | a list — runs once per entry |

Group selectors take an NBT filter: `@m[species="wolf"]`, `@a[class="rogue"]`.

---

## Substitution: `$()` and dice

Outside quotes, `$(...)` is replaced before the command runs; inside `"quotes"` everything is literal.

```
$(@s)               → the subject's name        $(@o) → the cause's name
$(@s.stat.health)   → a value at that path
$(hp)               → a stored variable (see /data)
dN                  → a die roll (bare, e.g. d20) # returns random number 1 through 20 inclusive
```

---

## /random

```
/random range <min>..<max>     one integer in [min,max] inclusive (min first)
```

Typing a number then TAB inserts `..`. Bare `<n>` = `1..n`; reversed bounds tolerated.
**Returns:** one int — captures cleanly (unlike `dN`).

```
/execute store result var $(x)->int run random range 1..20
/data result $(x)->int run random range 1..20
```

---

## /uuid

Every initiated entity (character, item, mob, NPC, quest, structure, talent/ability/effect) automatically gets a **session-unique id**: a 2-letter type code + `-` + 4 base62 chars — e.g. `it-aB39`. Type codes: `pl` player · `np` NPC · `mb` mob · `it` item · `qs` quest · `st` structure · `tl` talent · `ab` ability · `ef` effect. Ids are unique within a session (not across sessions) and persist through save/load — so a UI can reference an entity by uuid instead of by nbt.

```
/uuid get <selector>             show the uuid(s) of matched character(s)
/uuid lookup <uuid>              what entity holds that uuid
/uuid set <old-uuid> <new-uuid>  change a uuid; if the new one is taken: swap / regen the other / cancel
/uuid list [type]                counts per type, or every uuid of a 2-letter type (e.g. /uuid list it)
```

`uuid` is managed automatically — it can't be set via `/modify` and survives a reset; use `/uuid set` to change one.

---

## /character

```
/character new <name>{nbt} [true]      create (trailing true = NPC)
/character modify <selector> <set|add|reset> {nbt}     edit NBT
/character get <selector> <field>             read a field
/character show <selector> | list | remove <selector>
```

NBT is free-form: handled keys (`inventory_slots`) route to structure; any other key is just stored.

---

## /stat

A stat is BASE (a number) or DERIVED (owns a `/formula`). New characters seed every stat in `stats.csv` at 0.

```
/stat new <selector> <name> [value]              create/set (or {a:1,b:2} bulk)
/stat modify <selector> <name> <set|add|reset> [value]    set / add (behaves as operator/append/concat) / reset→0
/stat get <selector> <name> | list <selector> | remove <selector> <name>
```

The op word sits on either side: `/stat modify hero health set 3` == `... set health 3`.
**`/stat get` returns** the number (capturable).

---

## /formula

```
/formula new|modify <selector> <stat> = <expr>    set a derived-stat formula ('=' optional)
/formula remove <selector> <stat> | list <selector> | recompute <selector>
```

Expr refs `stat.X` (`atr.X` alias). Ops `+ - * / ^`, postfix `!`. Functions: `IF(c,a,b)`, `LET(n,v,body)`, `DELTA(ref)`, `MIN MAX ABS FLOOR CEIL ROUND SQRT CLAMP EXP FACT`.

---

## /item

```
/item new <selector> <item>{nbt} [count]     create → inventory (identical stacks merge)
/item remove <selector> <item> [count]
/item list <selector>
/item modify <selector> <item> <set|add|reset> <field> <value>     (or set {nbt})
/item equip <selector> <item> [slot]          weapons go to main/off hand
/item unequip <selector> <item-or-slot>
/item loot <table> <selector>                 dummy for now
```

Item NBT includes `equippable`, `wield:main|off|both`, `stats:{...}` (per-item stats), `on_equip`, `on_unequip`, `talents`. Pick a duplicate by `[index]`, `type[N]`, or `type{nbt}`.

---

## /attack

```
/attack <atk> <tgt>                      use equipped hands (main+off)
/attack <atk> <tgt> with <wpn>[+<wpn2>]  wield inventory item(s) for this hit
/attack <atk> <tgt> {pierce:5,slash:3}   explicit per-type raw damage
/attack <atk> <tgt> <number>             use <number> as effective attack
/attack <atk> list                       show wieldables
```

Damage runs through `combat.csv` + `combat_rules.csv` vs the target's defenses, subtracts from health; a lethal hit triggers `execution_check`.

---

## /move

```
/move <selector> <x> <y>     step to a cell, gated by the mover's `speed` (Chebyshev; diagonals = 1)
```

First placement is free. GM teleport with no speed check: `/map pos <player> <x> <y>`.

---

## /proc

```
/proc enable  <selector> a,b,c     turn signals ON (sticky), fire matching talents
/proc pulse   <selector> a,b,c     ON → fire → revert (momentary)
/proc disable <selector> a,b,c     turn signals OFF
/proc query   <selector>           show active signals
```

A signal may carry a bool: `is_poisoned:true`, `scared:false`, or bare (`=true`).

### proc conditions (on a talent/effect)

```
proc:[{in_combat:true,end_of_turn:true}]   AND within braces  → both must hold
proc:[{burning:true},{frozen:true}]        OR across braces   → either fires
```

Each condition must be `name:true` / `name:false` (a bare name is rejected).

---

## /talent · /effect · /ability

```
/talent  new <selector> <name>{proc:[...],reaction:[...]}
/effect  new <selector> <name>{duration:N,step:[...],proc:[...],reaction:[...]}
/ability new <selector> <name>{cooldown:N,cost:[...],on_hit:[...]}
<cmd> list <selector> | modify <selector> <name> ... | remove <selector> <name>
/ability cast <caster> <ability> [<target>]     pay cost + cooldown; on_hit hits target (default: self)
```

- **cost** = a list of deltas spent on cast: `cost:[stat.mana(20),stat.health(1)]`. Cast lists every blocking reason at once.
- **cooldown** = `current/total` turns. `cooldown:7` starts ready (`0/7`); casting sets `7/7`; `/turn next` ticks it down.
- **reaction / on_hit** = the reaction list (see top section).

---

## /quest

Character-held quests. `reward` is a Reaction (deltas and/or function names), run with `@s` = the owner.

```
/quest new <selector> <quest>{nbt} [turns_to_expire]   give it; pulses quest_obtained
/quest complete <selector> <quest>     run reward + pulse quest_complete, then drop it
/quest modify <selector> <quest> <set|add|reset> <field> [{nbt}|value]
/quest delete <selector> <quest> [true]   drop it; true also pulses quest_failed (no reward)
/quest list <selector>
```

- **`{reward:[...]}`** — e.g. `reward:[stat.gold(100), grant_sword]`. Runs on `/quest complete`.
- **`[turns_to_expire]`** — counts down on the owner's turns; at 0 the quest auto-fails (pulses `quest_failed`). Omit for no expiry. Also settable as `expiration:` in the nbt.
- Procs fired: `quest_obtained` (new), `quest_complete` (complete), `quest_failed` (expiry or `delete ... true`) — talents can react to these.

---

## /structure

Things that occupy one or more map cells (kingdoms, taverns, dungeons). Not character-owned — they live in the map (saved to disk). **Not unique:** many can share a name; the `{nbt}` narrows, and a numbered menu disambiguates when several match.

```
/structure new <structure>{nbt} [<x,y> <x,y> ...]   place it spanning those cells
/structure delete <structure>{nbt}          remove (menu if several match)
/structure modify <structure>{nbt} <set|add|reset> <field> [{nbt}|value]
/structure list
```

- **Multi-cell:** the structure comes first, then any number of `x,y` cells — `/structure new kingdom{ruler:"Mara"} 1,2 2,2 3,2`. `pos` is stored as a list of cells (`pos:[[1,2],[2,2],[3,2]]`).
- Cells can instead live in the nbt: `/structure new fort{pos:[[7,7],[7,8]]}` (leave the trailing cells off). Positional cells override an nbt `pos`.
- Grow/shrink later: `/structure modify kingdom add pos 4,2` (annex a cell) or `set {pos:[[1,2],[2,2]]}` (replace the whole list).
- Target by name + nbt **globally** (no x,y needed): `/structure delete kingdom{ruler:"Bob"}`. An explicit `name:` in the nbt overrides the `<structure>` token as the name.

---

## /turn

```
/turn                  whose turn + the world turn
/turn next             end turn → next player (world turn ticks on wrap)
/turn next cycle       one turn for every player
/turn next <selector>       advance just that player
/turn set <n> | add <n>     move the WORLD turn (no player ticking)
```

Each passing turn ticks every ability cooldown and effect duration.

---

## /data · /execute (capture & control)

```
/data result  $(name) run <command>     store the command's return value
/data success $(name) run <command>     store 1 if it worked else 0
/data set <name> <value> | /data list
```

```
/execute [as <selector>] [if|unless <l> <operator> <r>] [store result|success <target>] run <command>
  as <selector>                  run AS it, so @s = it
  if/unless <l> <operator> <r>    <operator> is > < >= <= = != ; operands are paths or literals
  store ... var $(name)->[int|float|str|list|bool]    capture into a variable no `->` assumes data type
  store ... entity <selector> <path>                        capture into an entity field
```

`$(name)->TYPE` casts the captured value. Examples:

```
/execute as noael store result var $(hp)->float run stat get @s health
/data result $(x)->int run random range 1..20
```

---

## /tellraw

```
/tellraw <text>     print formatted text. \n \t escapes; &-codes (&c red, &l bold, &r reset)
```

Use it inside a reaction/event/function: `/tellraw $(@s) was hit by $(@o)`.

---

## /map · /calendar · /event · /summon · /session · /kill

```
/map view [<player>] [x y] | pos <player> <x> <y> | get <x> <y> | set <x> <y> <biome>|{nbt}
        | new <x> <y> [biome] | recommend <x> <y>
/calendar [show] | show at <turn> | add|set <time|day|month|year> <v> | event add <when> <text>|list|clear
/event list|clear [category] | queue <cat> <event...> <N> [at <x> <y>] | force <cat> <event...> [at <x> <y>]
/summon <species> [count <n>] [at <x> <y>] {nbt}      mobs; handles are <species>#N
/session save [name] | load <id|name> | get | id | history | list   (id = 4-digit, also the UI port)
/kill <selector> [true]      true = remove entirely; default = health 0 + pulse 'died'
```

---

## Python API (containers.py)

`containers.py` defines classes and creates nothing on import. The command shell is the intended interface; the API mirrors it (`Player`, `Item`, `Stat`, `Talent`, `Ability`, `Effect`, `Reaction`, `Proc`). Stat/value objects carry `.set()` / `.add()` / `.modify()`; containers carry `.add()` / `.get()` / `.remove()`.

# Formatting Reference

Quick, copy-pasteable syntax for the things that have their own little languages. Edit `reference.md` and reload this page to change it.

# Selectors — who a command targets

- `@s` — **self**: the owner of the proc/reaction/function running right now
- `@o` — **origin**: the cause (attacker, caster)
- `@p` — **previous**: the last entity you named
- `@a` — all players · `@!a` — all NPCs · `@m` — all mobs · `@` — everything
- a name — just `noael`
- a list — `[noael, mira, goblin#1]`

## Selector filters `@a[ … ]`

- `@a[distance=..3]` — within 3 tiles (Chebyshev) of the `at` position (or @s)
- `@a[distance=2..5]` · `@a[distance=4..]` — range / open-ended
- `@a[pos=[2,2]]` · `@a[pos={[2,2],[2,1]}]` — standing on those cells
- `@a[stat=[health=..50, attack=10]]` — stat conditions (ranges or exact)
- `@a[flag=in_combat]` · `@a[flag=[scared=false]]` — proc-state flags
- `@a[quest={dragon{uuid:qs-1842}}]` — holds a matching member (also `talent`/`ability`/`effect`/`item`)
- `@m[species="wolf"]` — an nbt/core field, exact

# Ranges

Minecraft-style, used by `distance=` and any numeric filter.

- `5` — exactly 5
- `..5` — 5 or less
- `2..` — 2 or more
- `2..5` — between 2 and 5

# Stats — `(base + gear) × multiplier + flat`

```
/stat new <sel> <stat> <value>                            create / set the base value
/stat modify <sel> <stat> <set|add|reset> [value]          the base layer (no channel = base)
/stat modify <sel> base       <stat> <set|add|reset> [n]   the base value
/stat modify <sel> multiplier <stat> <set|add|reset> [n]   the multiplier layer (default 1)
/stat modify <sel> flat       <stat> <set|add|reset> [n]   the flat layer (default 0)
```

`reset` clears that layer back to its default. `set` accepts `=EXPR` (e.g. `=MAX(1, stat.health-2)`).

# Formulas — derived stats

`/formula new <sel> <stat> = <expression>`

- math: `+  -  *  /  ^`  and parentheses
- reads stats: `stat.health`, `stat.max_health`
- functions: `MIN MAX ABS FLOOR CEIL ROUND SQRT CLAMP EXP FACT`
- `IF(cond, a, b)` · `LET(name, value, body)`

```
/formula new noael power = stat.attack * 2 + FLOOR(stat.luck / 10)
/formula new noael guard = CLAMP(stat.defense, 0, 100)
```

No randomness in formulas — roll with dice at command time instead.

# Procs & flags — trigger conditions

A **clause** is `{flag:true, flag2:false}` (every key must match = **AND**).
A list of clauses is **OR**: `[{a:true},{b:true}]`.

- talent `proc:[{in_combat:true,end_of_turn:true}]` — fires when **both** hold
- `/proc pulse <sel> <flag>` — turn on, fire, revert (momentary)
- `/proc enable|disable <sel> <flag>` — sticky on / off

In the editor's proc box the outer `[ ]` is added for you — just type `{in_combat:true}`.

# Reactions — what happens when something fires

A list of actions: stat **deltas** and/or **function** calls.

- `[stat.health(-5)]` — lose 5 health (negative = damage)
- `[stat.mana(10), /function bless]` — gain 10 mana **and** run a function

Used by talent `reaction`, ability `on_hit`, effect `reaction`, and the `on_apply` / `on_clear` hooks.

# Functions — reusable command lists

```
/function edit <path>        open the line editor (terminal)
/function run  <path> [sel]   run it (@s = each target)
```

In the UI's function editor, write one command per line; `@s` is the runner.
Paths nest with `:` and `/` — e.g. `name:talents/death`.

# Effects — buffs/debuffs over time

- `duration` — turns it lasts (clears at 0)
- `clear_when` — a proc condition that ends it early — `{hex_removed:true}`
- `step` — which turns it fires on: `:1` first, `-1` last, `::2` every other, `:` every turn; combine `[1,::2]`
- `proc` — **signals to pulse** on a step turn (a list: `[poison_tick]`)
- `reaction` — applied on a step turn
- `on_apply` — once when gained · `on_clear` — once when it ends

# Talents — passives

- `proc → reaction` — when the proc matches, the reaction fires
- `on_apply` / `on_clear` — grant something on gain, reverse it on removal (e.g. `on_apply:[stat.bludgeon_resistance(3)]`)

# Abilities — castables

- `cost` — spent on cast: `[stat.mana(20)]` or `[inventory.firewood.count(1)]`
- `cooldown` — turns before recast (an ability with **no** cooldown can't be cast)
- `on_hit` — reaction applied to the target on cast
- `on_apply` / `on_clear` — grant on gain (e.g. a new `rage` stat via `[/function grant_rage]`)

`/ability cast <caster> <ability> [target]`

# /execute — run a command in a context

```
/execute as <sel> at <sel|x y> if|unless <l> <op> <r> run <command>
```

- `as` — run as each match (@s = it) · `at` — set the position for `distance=`
- `if` / `unless` — `<l> <op> <r>` with `> < >= <= = !=`

```
/execute at @s run proc enable @a[distance=..2] rallied:true
```

# Dice

- `d20` on its own — rolls
- inside a command — `/stat modify @s base health add d6`
- `$(d8)` — inline anywhere

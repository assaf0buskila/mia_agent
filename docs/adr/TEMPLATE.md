# ADR template

Copy this into `docs/adr/ADR-NNN-<slug>.md` for a new ADR. Do not skip fields.
Take the next free number from the index in `docs/DECISIONS.md`. Never reuse or
renumber an existing one.

```markdown
# ADR-NNN Title

- **Status:** proposed | accepted | superseded | rejected
- **Date:** YYYY-MM-DD
- **Assaf:** KEEP / ADOPT / TEST BOTH / DEFER / unset

**Context**
What forces the choice.

**Decision**
What we will do.

**Consequences**
What becomes easier, harder, or off-limits.

**Alternatives considered**
What we did not pick, and why.
```

A superseded record keeps its body and gains one line:

```markdown
- **Superseded by:** ADR-NNN (one clause saying what changed)
```

Then add the row to the index table in `docs/DECISIONS.md`.

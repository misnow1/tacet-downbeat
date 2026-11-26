# Domain Docs

How the engineering skills should consume this repo's domain documentation when exploring the codebase.

## This repo: `GLOSSARY.md` for words, `docs/design.md` for reasons

**`GLOSSARY.md`** at the repo root owns the vocabulary. Where design.md or CLAUDE.md still use a word the glossary lists under _Avoid_ ("band PA", "conductor", "trim"), the glossary wins; the older docs have not all been swept yet.

**`docs/design.md`** owns the reasoning, and CLAUDE.md requires reading it before proposing anything. It holds the reasoning behind every hard constraint in CLAUDE.md; without it they look arbitrary, and several contradict what would otherwise be sensible defaults. There is no `docs/adr/` yet.

- Treat design.md sections and CLAUDE.md's **Hard constraints** the way the rules below treat ADRs. If your output contradicts one, say so and stop; never work around it.
- Issues labelled `hard-constraint` touch one of these; read design.md before touching them.

The "proceed silently" rule below covers the missing ADR files only. It never excuses skipping design.md.

## Before exploring, read these

- **`GLOSSARY.md`** at the repo root, or
- **`GLOSSARY-MAP.md`** at the repo root if it exists: it points at one `GLOSSARY.md` per context. Read each one relevant to the topic.
- **`docs/adr/`**: read ADRs that touch the area you're about to work in. In multi-context repos, also check `src/<context>/docs/adr/` for context-scoped decisions.

If any of these files don't exist, **proceed silently**. Don't flag their absence; don't suggest creating them upfront. The `/domain-modeling` skill (reached via `/grill-with-docs` and `/improve-codebase-architecture`) creates them lazily when terms or decisions actually get resolved.

## File structure

Single-context repo (most repos):

```
/
├── GLOSSARY.md
├── docs/adr/
│   ├── 0001-event-sourced-orders.md
│   └── 0002-postgres-for-write-model.md
└── src/
```

Multi-context repo (presence of `GLOSSARY-MAP.md` at the root):

```
/
├── GLOSSARY-MAP.md
├── docs/adr/                          ← system-wide decisions
└── src/
    ├── ordering/
    │   ├── GLOSSARY.md
    │   └── docs/adr/                  ← context-specific decisions
    └── billing/
        ├── GLOSSARY.md
        └── docs/adr/
```

## Use the glossary's vocabulary

When your output names a domain concept (in an issue title, a refactor proposal, a hypothesis, a test name), use the term as defined in `GLOSSARY.md`. Don't drift to synonyms the glossary explicitly avoids.

If the concept you need isn't in the glossary yet, that's a signal: either you're inventing language the project doesn't use (reconsider) or there's a real gap (note it for `/domain-modeling`).

## Flag ADR conflicts

If your output contradicts an existing ADR, surface it explicitly rather than silently overriding:

> _Contradicts ADR-0007 (event-sourced orders), but worth reopening because…_

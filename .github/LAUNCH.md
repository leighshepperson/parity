# Parity launch copy

Use the repository link as the primary destination. Lead with common Python dependency upgrades,
refactors, worktrees and backend migrations. Use the cross-language proofs to establish breadth
without presenting them as the primary workflow or claiming universal equivalence.

## Launch thread

### Post 1

Migrations fail in the cases nobody thought to write down.

Parity runs old and new implementations on the same generated inputs, finds behavioural
differences, shrinks them to minimal examples, and saves them for replay.

Dependency upgrades, refactors, backends, worktrees—or languages.

https://github.com/leighshepperson/parity

### Post 2

The common case is Python on both sides.

In the Pydantic 1 → 2 proof, the same callable runs in isolated environments with conflicting
dependencies. Parity generated inputs and minimized four historical behaviour changes, including
coercion, optional defaults and equality.

https://github.com/leighshepperson/parity/tree/main/case_studies/pydantic_version

### Post 3

For a larger Python backend migration, Parity audited five PyTimeTK APIs across pandas and Polars.

The stock candidate failed every covered unit. After targeted repairs, all 15 campaigns passed in
both the released and current dependency lanes.

https://github.com/leighshepperson/parity/tree/main/case_studies/pytimetk_migration

### Post 4

The same engine also verified JavaScript → Python rules, a stateful C++ → Python order book and a
Fortran → Python numerical rewrite. Those are proofs of the boundary, not special migration modes.

If two systems can receive equivalent inputs and expose observable results, Parity can compare
them.

### Post 5

    pip install parity-check

The standard install manages isolated Python environments for dependency upgrades and local
checkouts. It also covers direct refactors, alternate backends and adapted external processes.

### Post 6

Have a migration you do not fully trust? Send the old target, new target and callable boundary.
I’ll help turn the first three suitable open-source migrations into reproducible Parity cases.

https://github.com/leighshepperson/parity/issues/new?template=migration.yml

## Follow-up proof posts

Publish these separately rather than on launch day:

1. Pydantic 1 → 2: four minimized historical differences across incompatible Python environments.
2. pandas 2.3 → 3.0 and Polars 0.20 → 1.x: unchanged callables exposing intentional version drift.
3. PyTimeTK pandas → Polars: a five-API migration across released and current dependency lanes.
4. Worktree comparison: install two local checkouts independently and bind their source identities
   into the evidence.
5. Cross-language boundary: use the JavaScript, C++ and Fortran proofs together to establish that
   the engine is not coupled to Python internals.
6. Public-project validation: link a bounded study from `case_studies/ADOPTION_LOG.md` and state its
   exact versions and domain.

## Show HN

Title:

> Show HN: Parity – Find and shrink behavioural differences in software migrations

Body:

> I built Parity to answer a practical migration question: where do the existing and replacement
> implementations observably disagree?
>
> It drives both sides with the same complete calls, compares returns, exceptions and mutation,
> searches generated domains, shrinks differences and retains replayable evidence. The targets can
> use different dependency versions, APIs, environments or languages.
>
> The repository includes runnable Pydantic 1→2, pandas cross-version and PyTimeTK pandas→Polars
> studies, plus local-checkout workflows. JavaScript→Python, C++→Python and Fortran→Python proofs
> demonstrate that the comparison boundary is not coupled to Python internals. The standard
> install manages isolated reference and candidate Python environments.
>
> `pip install parity-check`
>
> I would particularly value feedback on real migration boundaries that are difficult to express
> cleanly, and on the first-run workflow.

Direct link: https://github.com/leighshepperson/parity

## Short descriptions

GitHub description:

> Find and shrink behavioural differences across dependency upgrades, refactors and rewrites.

Directory description:

> Behavioural compatibility verification for software migrations. Run old and new implementations
> on the same generated calls, minimize differences and retain replayable evidence.

Recommended topics:

`differential-testing`, `property-based-testing`, `migration-testing`, `regression-testing`,
`software-migration`, `testing-tools`, `python-cli`

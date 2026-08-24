# Parity launch copy

Use the repository link as the primary destination. The PyPI link supports the install command;
the executable proof links establish breadth without claiming universal equivalence.

## Launch thread

### Post 1

Rewrites fail in the cases nobody thought to write down.

Parity runs old and new implementations on the same generated inputs, finds behavioural
differences, shrinks them to minimal examples, and saves them for replay.

Versions, refactors, runtimes or languages.

https://github.com/leighshepperson/parity

### Post 2

To test that claim, I used a JavaScript rules engine as the reference and a Python rewrite as the
candidate.

Parity generated recursive JSON programs and minimized three independent defects: eager
evaluation, first-match behaviour and a threshold boundary.

https://github.com/leighshepperson/parity/tree/main/case_studies/javascript_python_rules

### Post 3

Then I tried a stateful C++ → Python order-book rewrite.

Event streams exercised price-time priority, partial fills and failures. Parity found and shrank
five distinct behavioural defects.

https://github.com/leighshepperson/parity/tree/main/case_studies/cpp_python_orderbook

### Post 4

The boundary is simple: if two systems can receive equivalent inputs and expose observable
results, Parity can compare them.

    pip install parity-check

Dependency upgrades, refactors, worktrees, alternate backends, services and cross-language
rewrites.

### Post 5

Have a migration you do not fully trust? Send the old target, new target and callable boundary.
I’ll help turn the first three suitable open-source migrations into reproducible Parity cases.

https://github.com/leighshepperson/parity/issues/new?template=migration.yml

## Follow-up proof posts

Publish these separately rather than on launch day:

1. Fortran → Python: a plausible numerical rewrite loses a term during catastrophic cancellation;
   Parity reduces it to three rows and replays it.
2. Dependency upgrade: isolated reference and candidate environments prevent their dependency
   graphs from contaminating one another.
3. Worktree comparison: install two local checkouts independently and bind their source identities
   into the evidence.
4. Public-project validation: link a bounded study from `case_studies/ADOPTION_LOG.md` and state its
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
> The repository includes runnable JavaScript→Python, C++→Python and Fortran→Python proofs, plus
> dependency-version, worktree and public-project studies. The standard install manages isolated
> reference and candidate Python environments.
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

# Product direction: semantic trust for changed computation

## North star

AI will make rewriting computation cheap. Establishing that the rewrite preserved meaning will
become the expensive part.

Parity should become the independent gate between “an agent produced a faster/newer implementation”
and “this is safe to merge.” Its durable object is **semantic evidence**: an explicit contract,
the search performed, minimized counterexamples, environment provenance and a reproducible result.

This is a direction document, not a feature or date commitment. The local verifier remains useful
at every stage even if none of the longer-range platform work ships.

## Where it can hit hardest

Parity is most valuable when all four conditions hold:

1. A working implementation already exists and can serve as an executable contract.
2. There is strong pressure to rewrite it—speed, cost, cloud/engine migration, dependency risk or AI
   automation.
3. Ordinary example tests miss edge semantics.
4. A wrong answer can pass silently and cost far more than a crash.

That concentrates the initial market in dataframe/SQL migrations, financial and scientific
calculation refactors, feature pipelines, billing/allocation code and dependency/runtime upgrades.
The product does not need to compete for greenfield application development.

## Two years: own data-computation migrations

By roughly 2028, the target position is:

> Before a team replaces pandas, Polars, DuckDB, Ibis, Spark or a SQL execution path, it runs Parity.

### Product

- A hard, public semantic fault corpus with hundreds of versioned cases across joins, groupings,
  temporal operations, null logic, categoricals, decimal/numeric behaviour, windows and ordering.
- Stable pandas, Polars, Arrow, DuckDB/Ibis and SQL adapters; Spark as an isolated optional adapter
  only when reliability justifies its operational weight.
- Cross-environment campaigns that compare Python, engine and dependency upgrades in their real
  locked environments.
- Trace-assisted domain capture that records schema, bounds and invariants without copying production
  rows into a hosted service.
- Contract packs maintained for common migrations—reviewable starting policies, never opaque magic.
- First-class GitHub, GitLab, pytest and pre-merge integrations with SARIF/check annotations, JUnit
  and content-addressed replay artifacts.
- A machine protocol for coding agents: propose rewrite, invoke Parity, inspect counterexample, amend
  code and resubmit. The writer does not control the verifier's policy.
- Hardened OCI worker profiles for running newly generated code with no ambient credentials/network.

### Commercial wedge

The open-source local engine remains complete. Revenue comes from work organizations do not want to
assemble themselves:

- paid migration-verification engagements that seed real contracts and prove value;
- a self-hosted team evidence service for protected policies, history, artifact retention, approval
  and audit; and
- maintained enterprise adapters/contract packs and support.

The sale is not “more tests.” It is measurable migration risk reduction plus evidence that the
faster/cheaper engine did not alter answers. A successful initial engagement should find at least one
defect existing fixtures missed and leave permanent CI gates behind.

### Defensible assets

- The semantic fault corpus and version-to-version engine knowledge.
- High-quality minimization across heterogeneous runtimes.
- Longitudinal evidence linking a contract to every migrated/AI-written implementation.
- Trusted integrations that keep private computation local.

The CLI alone is reproducible; those accumulated semantics and trust relationships are the moat.

## Three years: the independent reviewer for AI-written computation

By roughly 2029, agents will routinely port and optimise whole data/numerical subsystems. The core
workflow becomes a two-agent separation of duties:

```text
writer agent → candidate change → Parity verifier → evidence/counterexample → merge gate
```

The verifier must be independently pinned, use organization-owned policies and remain capable of
saying “insufficient evidence.” It must not share the writer's incentive to declare completion.

### Product expansion

- **Proof-carrying pull requests:** signed attestations bind source/environment digests, input-domain
  contract, verifier version, search budget and result.
- **Semantic change intelligence:** show which contract surface changed, which historic
  counterexamples were replayed and what remains untested—without turning the product into a generic
  code-review dashboard.
- **Search portfolio:** property-based generation plus coverage guidance, metamorphic relations,
  constraint solving and domain-specific boundary packs. Formal solvers are used where tractable;
  passing never masquerades as universal proof.
- **Numerical and stochastic contracts:** distributions, confidence bounds, monotonicity,
  conservation laws and error envelopes for scientific/ML pipelines.
- **Stateful sequence verification:** compare incremental/streaming computations over minimized event
  sequences, not just one frame.
- **Organizational policy:** protected contract ownership, risk-tiered budgets, required independent
  approval and retention controls.
- **Private fleet learning:** teams can contribute anonymized fault signatures and generator patterns
  without contributing source rows or implementations.

### Buyer and budget

The economic buyer moves from the migration lead to the platform/AI-governance owner. Parity becomes
part of the organization's permission to deploy agent-written calculation code. Pricing can follow
protected repositories, verification workers and assurance tier rather than developer seats.

## Ten years: a compatibility and assurance layer for computation

By roughly 2036, code may be continuously regenerated for new hardware, cost targets, jurisdictions
and runtime constraints. Humans will not review every implementation. They will own the contract and
the acceptable evidence.

The blue-sky position is:

> Parity evidence becomes to computational change what tests plus signed provenance are to software
> release: a standard artifact that travels with the implementation.

### What that could mean

- An open **Semantic Evidence Protocol** for contracts, search methods, counterexamples, provenance
  and assurance levels—portable across CI vendors and languages.
- Cross-language and cross-hardware verification spanning Python/SQL, JVM, Rust/C++, WASM, GPU
  kernels, query optimizers and ML inference runtimes.
- Agents negotiate an executable contract before rewriting, then attach evidence for every target
  implementation. Production selects implementations by cost/performance only inside the verified
  envelope.
- Continuous compatibility maps reveal when a dependency, compiler, model, engine or hardware update
  changes observable meaning before an organization rolls it out.
- Regulators, auditors, customers and insurers can verify signed claims without receiving proprietary
  code or data. Zero-knowledge or confidential-compute techniques may prove selected evidence across
  organizational boundaries.
- A public semantic fault commons lets engine maintainers test their releases against real classes of
  migration failures while private organizations retain data and code locally.
- Dynamic differential evidence composes with formal proof, static analysis and runtime monitoring;
  Parity becomes an orchestrator of assurance methods, not a claim that fuzzing alone proves programs.

The business at that point is trust infrastructure: independent certification profiles, enterprise
policy/evidence networks and verified compatibility programs. The open format and local engine are
strategic—organizations will not accept a proprietary black box as the sole judge of meaning.

## Product principles that survive every horizon

1. **Local first.** Source and values need not leave the owner's boundary.
2. **Writer/verifier independence.** The system producing code cannot quietly weaken its contract.
3. **Evidence over confidence scores.** Preserve the concrete witness and exact policy.
4. **Errors fail closed.** Timeout, unsupported type and uncertain comparison are not passes.
5. **Explicit semantics.** Tolerance, ordering and missing-value rules are reviewable configuration.
6. **Engine neutrality.** No execution engine gets to define universal correctness.
7. **Replay before explanation.** AI may help explain later; deterministic reproduction comes first.
8. **No general application framework.** Parity verifies computations. It does not become a dashboard,
   widget system or user-interface runtime.

## What would invalidate the strategy

The direction should be reconsidered if real migrations rarely contain defects beyond existing unit
fixtures, teams will not run an independent gate inside CI, or cross-engine minimization cannot be
made reliable enough to avoid verifier fatigue. The strongest near-term validation is not downloads:
it is repeated discovery of costly, previously unknown semantic defects on customer-owned migrations.

Conversely, if AI coding accelerates rewrites while failure investigation remains expensive, the
verification burden compounds. That is the asymmetry Parity is built to own.

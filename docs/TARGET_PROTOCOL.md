# External target protocol

Protocol version 2 lets any local executable participate as a reference or candidate without
installing Parity or using Python. The executable is a thin contract adapter: it receives canonical
Arrow/JSON invocations, invokes the implementation under test, and reports a canonical return, a
target-raised exception, or an adapter/infrastructure error.

Use a command endpoint when the target is not an importable Python callable or when a process
boundary is the natural integration point:

```toml
[cases.reference]
command = ["./bin/reference-adapter"]

[cases.candidate]
command = ["./bin/candidate-adapter", "--compat"]
```

`command` is an argument vector, never a shell string. Python-only fields such as `target`,
`python`, `adapter`, `pandas_input` and `canonicalizer` do not apply. The command owns adaptation
and output canonicalisation.

## Python adapter SDK

Most Python command adapters should use `parity.target_adapter` instead of implementing this wire
format directly:

```bash
parity adapter init adapters/reference.py
```

The generated module exports a `CommandAdapter` named `adapter`. Configure the SDK runner as the
endpoint; Parity appends its private session-root argument when it starts the command:

```toml
[cases.reference]
command = ["parity", "adapter", "serve", "adapters/reference.py"]
```

The SDK handles the persistent session, strict path and request validation, Arrow/JSON transport,
Return/Raise/Error responses and atomic publication. Project code supplies the runtime identity,
optional target preflight and canonical-input-to-target-to-canonical-output function. See the
[Python command-adapter SDK guide](TARGET_ADAPTER_SDK.md).

The rest of this document is the normative language-neutral protocol. Implement it directly when
the adapter itself cannot use Python or install `parity-check`; external target programs called by
an SDK adapter still need neither.

## Session lifecycle

Parity starts one persistent process per side and appends one final argument: the absolute path to
a private session directory. The configured arguments precede that path. Standard output and error
are not protocol channels.

For every request Parity:

1. creates a private `call-...` directory below the session directory;
2. writes `request.json` and the Arrow input files there;
3. writes the opaque call-directory name followed by a newline to the command's standard input;
4. waits for `<call-directory>/response.json`; and
5. validates the response and any declared output before continuing.

The process reads one ASCII token per line until standard input closes. It must not infer or invent
paths from untrusted input: join the received token to the session root, require the resolved path
to remain an immediate child, and use only the paths declared in that request. Write
`response.json` atomically (temporary file plus rename) so the controller never reads a partial
response.

Published protocol files must be single-linked regular files at their exact immediate-child paths;
symlinks and hard links are rejected. The controller also rejects replacement or mutation while a
file is read. `response.json` is limited to 1 MiB, JSON output to 16 MiB and Arrow output to 256 MiB.
Exceeding a limit is a protocol `ERROR`, so adapters should reject or summarize larger application
results before publication.

The session persists across generated examples, confirmation and performance repeats. Process
globals therefore persist too. Every input comes from a newly deserialized Arrow file. A timeout,
crash, invalid response or broken protocol fails the session closed; Parity does not silently
restart it with different state.

## Operations

Every request has `protocol_version = 2` and one of three operations:

- `runtime` validates the transport and returns a bounded runtime identity. It must not import or
  invoke the application target.
- `inspect` validates the adapter, target and target-side dependencies without invoking the
  behavioural operation.
- `execute` reads the inputs, invokes the behavioural operation once and returns its observation.

Parity preflights in two phases: it completes `runtime` for both sides before sending `inspect` to
either. This distinguishes a missing transport/runtime requirement from a target import or adapter
failure and prevents one endpoint import from running when the peer transport is unusable. The
deferred healthy side is explicitly `not_checked`; it is not presented as ready or failed. A failed
preflight is `ERROR`; neither implementation is treated as behavioural evidence.

## Request

The controller writes a JSON object shaped as follows. Paths are controller-created files inside
the current private call directory.

```json
{
  "protocol_version": 2,
  "operation": "execute",
  "endpoint": {
    "kind": "command",
    "record_distributions": []
  },
  "invocation": {
    "args": [
      {"kind": "json", "value": "sum"},
      {"kind": "arrow", "path": "/private/session/call-.../input-00000000.arrow"}
    ],
    "kwargs": {
      "batches": {
        "kind": "frames",
        "container": "list",
        "items": [
          {"kind": "arrow", "path": "/private/session/call-.../input-00000001.arrow"}
        ]
      },
      "descending": {"kind": "json", "value": false}
    }
  },
  "output": {
    "arrow": "/private/session/call-.../output.arrow",
    "json": "/private/session/call-.../output.json"
  }
}
```

`invocation.args` is ordered and `invocation.kwargs` preserves the configured keyword names. Each
value is exactly one recursive node:

- `{"kind":"arrow","path":"..."}` binds one Arrow IPC file;
- `{"kind":"json","value":...}` carries one portable JSON-like value; or
- `{"kind":"frames","container":"list"|"tuple","items":[...]}` carries one frame sequence.
  Its items are Arrow nodes.

The adapter reconstructs those nodes and invokes its application boundary with exactly
`execute(*args, **kwargs)`. Zero arguments, many arguments, list/tuple-valued frame arguments and
ordinary `*frames` calls are therefore unambiguous; expanded varargs already appear as separate
top-level `args` nodes.

There are at most 256 positional slots, 256 keyword slots and 256 items in one frame sequence.
Keyword names are non-keyword Python identifiers of at most 128 characters. Each JSON value is at
most 256 KiB and all JSON arguments total at most 512 KiB. `request.json` is limited to 1 MiB.
Arrow files do not count toward that JSON limit and remain the canonical frame authority. The
command must not modify them.

Requests for `runtime` and `inspect` also contain an invocation and output paths, but those
operations must not read inputs, invoke behaviour or write behavioural output.

## Response

Each operation writes exactly these top-level fields:

```json
{
  "protocol_version": 2,
  "outcome": "returned",
  "duration_seconds": 0.00125,
  "exception": null,
  "mutated_inputs": [],
  "return_type": "example.Result",
  "runtime": {
    "executor": "command",
    "runtime_name": "example-runtime",
    "runtime_version": "1.0",
    "python_implementation": null,
    "python_version": null,
    "platform_system": "Linux",
    "platform_machine": "x86_64",
    "parity_version": null,
    "distributions": [],
    "identities": []
  },
  "output": {"kind": "arrow"}
}
```

Fields are strict: unknown or malformed fields are a protocol `ERROR`.

### Return

`outcome = "returned"` is `Return(canonical_value)`. For `execute`, write exactly one output and
set `output.kind`:

- `arrow`: write an Arrow IPC file to the request's `output.arrow` path; or
- `json`: write one JSON-compatible value to `output.json`.

For successful `runtime` and `inspect`, set `output` and `return_type` to `null`. The controller
uses the command-reported duration for target timing and independently samples process peak RSS.

### Raise

`outcome = "raised"` means the application target deliberately raised or rejected the canonical
input. Set `output` to `null` and supply:

```json
{
  "module": "legacy.domain",
  "type": "InvalidTrade",
  "message": "trade 123 was rejected",
  "details": {"error_codes": ["expired"], "location_shapes": ["field/field"]}
}
```

Parity normalizes and fingerprints type, message and allow-listed structured details. Volatile
paths, addresses, timestamps, identifiers, versions and witness literals do not make new findings;
stable API subjects and reason codes can. A `Raise` is semantic evidence: return-versus-raise or
different raises produce `FAILED` when the sides disagree.

Do not put secrets in messages or details. The response directory is private, but adapters should
still report only the minimum semantic information.

### Error

`outcome = "error"` means the adapter could not perform a meaningful comparison: invalid input
transport, dependency/import failure, canonicalisation failure or another adapter/infrastructure
problem. Set `output` to `null` and provide bounded, data-safe exception metadata. Parity reports
the case as `ERROR`, not as a behavioural incompatibility.

Do not map a normal application exception to `error`; use `raised`. Conversely, do not map adapter
failure to a domain exception merely to obtain `FAILED`.

`mutated_inputs` is an ordered subset of top-level call labels: `args/0`, `args/1`, … followed by
`kwargs/name`. A frame sequence is tracked as its containing call argument. The field reports
application-visible mutation of deserialized objects, not changes to transport files. Use an empty
list if the adapter cannot expose mutation as part of its contract. `return_type` is a bounded
descriptive label for a successful raw application result, or `null` otherwise.

## Runtime and source identity

Every response carries the same path-free runtime object for the lifetime of a session:

- `executor` must be `command`;
- `runtime_name` and `runtime_version` identify the language/runtime or executable contract;
- Python fields are optional for non-Python commands;
- `platform_system` and `platform_machine` are bounded labels;
- `parity_version` is the installed SDK version when a command uses `parity.target_adapter`; a raw
  adapter that does not install Parity reports `null`, and the controller records its own version
  separately;
- `distributions` is a sorted, bounded list of explicitly relevant package identities; and
- `identities` may contain sorted `git-worktree-v1` source claims with a revision, dirty flag and
  content digest.

A distribution entry is `{name, status, version}` where status is `installed`, `missing` or
`unavailable`; only `installed` has a version. Commands that cannot report Python distributions may
leave the list empty unless the case declares `required_distributions`. A declared requirement is
fail-closed, so the command must report enough provenance to verify it.

Replay records and rechecks runtime and source identities. Report only stable identity—not paths,
hostnames, environment values, command lines or a full dependency inventory.

## Security and reproducibility rules

- Treat the command and both implementations as trusted project code. Process isolation and a
  private directory are not a hostile-code sandbox.
- Parity removes the controller's inherited `PYTHONPATH`; declare target imports and environment
  values explicitly.
- Never place credentials in `parity.toml`, arguments, responses or artifacts. Inject unavoidable
  secrets through a protected runner and prefer offline adapters.
- Resolve every protocol path beneath the supplied session/call root. Reject symlinks or path
  traversal according to the adapter's platform policy.
- Bound JSON size, nesting, labels and application diagnostics. Do not echo raw private input into
  exception metadata.
- Write output before atomically publishing the response. Flush and close Arrow writers first.
- Keep adapter behaviour deterministic. If process state affects outputs, Parity's stability check
  should surface an `ERROR`, but avoiding hidden state produces much clearer findings.
- Pin or otherwise identify the command binary and its dependencies in release gates. The protocol
  records identity evidence; it is not a package resolver, container digest or cryptographic
  attestation system.

## Minimal implementation checklist

An adapter is ready when it can:

1. accept the appended session-root argument and read call tokens from standard input;
2. respond successfully to `runtime` without importing the application;
3. respond to `inspect` after checking target-side imports/configuration but without invocation;
4. reconstruct every invocation node and apply the canonical-to-target mapping;
5. distinguish `returned`, application `raised`, and adapter `error` outcomes;
6. write canonical Arrow/JSON output and an atomic strict response;
7. report stable runtime/source provenance; and
8. survive repeated requests in one session without data or state leaking between cases.

Keep this adapter small. It should translate one shared behavioural contract, not reimplement
Parity's generation, comparison, shrinking, finding or replay logic.

The Python SDK owns checklist items 1, 5's response encoding, 6 and the protocol portions of 7.
Its user still owns target inspection, the canonical mapping, correct semantic classification,
stable provenance and application-state cleanup.

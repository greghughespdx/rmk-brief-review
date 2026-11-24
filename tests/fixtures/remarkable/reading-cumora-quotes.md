# Reading fixture: block quotes and a table too wide to fit

Passages from a research note about a public open-source project, whose block
quote once printed as a bare vertical bar. The last table is not from that note: it is a deliberately over-wide table, kept
here because the real research documents all fit and the labelled-block fallback
still has to be exercised.

## A comparison table that fits the page

| Fact | 2026-08-19 | 2026-09-03 |
|---|---|---|
| Stars | 2,716 | 3,435 |
| Forks | 314 | 430 |
| Open issues | 4 | 11 |
| Last push | 2026-08-20 | 2026-09-03 (today) |
| Releases | not noted | 10 |
| BYOA engines | 3 | 9 (2 sandboxed by default) |
| Contributors | one primary author | 12, top two at 72 and 48 commits |
| Licence | MIT | MIT (unchanged) |

## A block quote

`server/src/agents/computer/engine.ts` line 352 declares nine engine ids; line 361
declares that only `claude` and `codex` are in `SANDBOXED_ENGINE_IDS`. The comment
above it says exactly why, and it is the right distinction:

> Engines for which Cumora can impose a fail-closed filesystem + tool-network boundary
> non-interactively. The remaining adapters still work for operators who explicitly accept
> host-level execution, but are never selected merely because their binary happens to be
> on PATH.

## A table too wide for any column layout

| SandboxedEngineIdentifier | FilesystemBoundaryDescription | ToolNetworkBoundaryDescription | HostExecutionFallbackDescription | OperatorAcceptanceRequirement | VerificationCommandInvocation | ObservedRegressionWindow |
|---|---|---|---|---|---|---|
| ClaudeSandboxedAdapterIdentifier | FailClosedFilesystemBoundaryEnforced | FailClosedToolNetworkBoundaryEnforced | HostExecutionFallbackNeverSelected | ExplicitOperatorAcceptanceRequired | VerificationCommandInvocationRecorded | ObservedRegressionWindowUnknown |
| CodexSandboxedAdapterIdentifier | FailClosedFilesystemBoundaryEnforced | FailClosedToolNetworkBoundaryEnforced | HostExecutionFallbackNeverSelected | ExplicitOperatorAcceptanceRequired | VerificationCommandInvocationRecorded | ObservedRegressionWindowUnknown |

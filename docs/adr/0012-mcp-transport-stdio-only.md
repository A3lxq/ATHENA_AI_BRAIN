# ADR-0012: MCP Transport is Stdio-Only

- **ID:** ADR-0012
- **Title:** MCP Transport is Stdio-Only
- **Status:** Accepted
- **Date proposed:** 2026-10-03
- **Date accepted:** 2026-10-03
- **Depends on:** ADR-0007 (MCP tool contract)

## Context

`docs/SECURITY_MODEL.md` TB-1 (Spoofing) names a standing gap: ATHENA
AI-BRAIN's MCP server has no application-level authentication of its own.
If it ran over an HTTP transport, a malicious or compromised client could
issue tool calls indistinguishable from a trusted one, with no protocol-
level session concept beyond whatever handle the server itself mints. TB-1
explicitly recommends: *"a follow-up ADR should explicitly state stdio-only
for Phase 1; any future handle-based state (job IDs, elicitation
confirmations) must be non-deterministic, bound to caller, never treated as
authentication on its own."*

That follow-up ADR was never actually written — confirmed directly during
a 2026-10-03 security audit (`grep -rl "stdio" docs/adr/` found no match
across all eleven existing ADRs). The *code* has, in practice, always been
stdio-only: `src/athena/mcp_server/server.py` calls `mcp.run(transport="stdio")`,
and a full-package grep of `src/athena/mcp_server/` for `uvicorn`,
`starlette`, `FastAPI`, or any `0.0.0.0`/network-bind pattern turns up
nothing — there has never been an HTTP code path to secure in the first
place. This ADR exists to close the pure governance/documentation gap
(CLAUDE.md rule 7: "every significant technical decision gets an ADR"),
ratifying already-shipped behavior rather than changing anything.

## Decision

**Accepted:** ATHENA AI-BRAIN's MCP server transport is stdio-only. The
server is launched as a subprocess by its MCP host (Claude Code, Claude
Desktop, or equivalent) via `deployment/bubblewrap/athena-mcp-launch.sh`,
communicating over stdin/stdout. The OS process boundary — who can spawn or
attach to that subprocess — is the server's actual authentication
mechanism, by design, not an oversight to be patched later. No HTTP, SSE,
or other network-reachable transport is exposed, now or without a new ADR
explicitly superseding this one.

## Alternatives Considered

| Option | Verdict |
|---|---|
| HTTP transport with no additional auth | Rejected — would expose tool calls to anything that can reach the port, with zero application-level identity check; unacceptable for a server that includes destructive (if MRTR-gated) and data-exfiltrating (`vault_search`, `note_read`) capability. |
| HTTP transport with bearer-token or OAuth-style auth | Rejected for now — the master specification and every phase to date target a single local user on a single machine; a real auth layer is meaningful added complexity and attack surface with no corresponding benefit at this scale. Revisit if a genuinely multi-host or remote-access use case ever emerges — that would need its own threat-modeled design, not a quick bolt-on. |
| Stdio-only (status quo, now formalized) | **Accepted** — matches every other locally-scoped MCP server this project's own research surveyed (the official filesystem reference server, `obsidian-mcp-server`), requires no new code, and the OS process boundary is a real, well-understood security boundary for this exact deployment shape. |

## Rationale

1. **This is what the code has always done.** `server.py`'s `mcp.run(transport="stdio")` was never ambiguous in practice; this ADR removes the documentation gap, not a code gap.
2. **The OS process boundary is a real control, not a hand-wave.** `deployment/bubblewrap/athena-mcp-launch.sh` already sandboxes the server process (Phase 1, P0 #2); whoever can invoke that script is, by construction, the only party who can open a session with the server. This is the same trust model the master specification's single-user, local-first framing already assumes everywhere else.
3. **Closing TB-1 as "mitigated by design constraint" is honest, not a downgrade.** TB-1 remains correctly named in `SECURITY_MODEL.md` as a residual consideration for anyone who *does* later expose this server over a network transport — this ADR's job is to make explicit that doing so is an architecture change requiring its own ADR and threat-model pass, not a configuration flag to flip casually.

## Consequences

- Any future work that exposes ATHENA AI-BRAIN's MCP server over HTTP, SSE, or any other network-reachable transport requires a new ADR explicitly superseding this one, including a real authentication/authorization design — not a quiet addition.
- `docs/SECURITY_MODEL.md` TB-1 should be annotated to point at this ADR as the resolution of its "no follow-up ADR exists yet" gap (done alongside this ADR's acceptance).
- No code change accompanies this ADR — it ratifies existing, already-verified behavior.

## References

- `docs/SECURITY_MODEL.md` TB-1 (Spoofing).
- ADR-0007 (MCP Tool Contract), which already assumes a single trusted caller per session without stating the transport explicitly.
- `src/athena/mcp_server/server.py` (the `mcp.run(transport="stdio")` call this ADR ratifies).
- `deployment/bubblewrap/athena-mcp-launch.sh`, `docs/design/os-level-process-sandboxing.md` (the OS-level sandboxing this ADR's trust model depends on).

## Open Questions

None — this ADR closes a pure documentation gap with no open implementation questions. A future genuinely multi-host/remote-access use case would reopen this as a new ADR, not an amendment to this one.

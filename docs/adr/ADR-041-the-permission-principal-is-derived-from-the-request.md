# ADR-041 The permission principal is derived from the request

- **Status:** accepted
- **Date:** 2026-08-26
- **Assaf:** ADOPT (chat: take the rebuild to 9-10)

**Context**
`app/capabilities/policy.py` enforced the per-graph capability allowlist correctly, but the value it checked was a `graph=GraphName.OWNER` literal typed at each of the 8 call sites. The guarded code chose its own trust level. Owner/client isolation therefore held only because of module topology -- the owner tool registry happened to be reachable only from the Telegram route, behind the numeric allowlist. Any new code path calling an owner helper from a web-triggered path would have inherited owner trust silently, and no test could have detected it.

**Decision**
Trust is established once, at the channel entry point, and passed down as a frozen `Principal` (graph + source + actor_id). `Principal.owner()` is minted in `app/api/owner.py` only after the numeric owner allowlist has matched; `Principal.client()` is minted in `app/api/website.py` and `app/api/inbound.py`. `authorize()` and `execute_capability()` take `principal=`. No module outside `capabilities/types.py` (the constructors) and `capabilities/registry.py` (the allowlists) may name a graph. That invariant is enforced by `tests/unit/test_vnext_principal.py::test_no_module_names_its_own_trust_level`, which walks `app/` with `ast` and fails on any module that names its own trust.

**Consequences**
Adding a capability call means threading the principal you were given, not choosing one. A new web-reachable path cannot silently acquire owner rights: it would have to name a graph, and the guard test fails on that. If a genuine client-to-owner crossing ever appears (client activity needing an owner capability, not merely an owner notification), it must be introduced as a named, reasoned escalation rather than a literal -- one was written for the hot-handoff path and removed again once that path proved not to use a capability at all.

**Alternatives considered**
Keep `graph=` literals and rely on code review -- rejected; that is the status quo that produced a boundary no test could verify. A full auth framework with roles and scopes -- rejected as far more machinery than two trust levels need. Deriving trust inside the policy layer by inspecting a call stack or context var -- rejected; implicit ambient authority is harder to read and to test than an argument that must be passed.

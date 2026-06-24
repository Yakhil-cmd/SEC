Audit Report

## Title
Ingress Pool Admission DoS via Per-Message vs. Cumulative Cycles Check Mismatch — (`rs/execution_environment/src/execution_environment.rs`, `rs/ingress_manager/src/ingress_selector.rs`)

## Summary
The ingress admission gate checks only the single-message induction cost against a canister's current balance, while the proposal-time gate checks the cumulative cost of all messages from that canister in the block. Messages that pass admission but fail the cumulative check at proposal time are only removed from a local in-memory queue — not from the validated pool — and remain there until TTL expiry. An attacker with a canister holding minimal cycles can flood the validated pool with messages that will never be included in a block, saturating the pool and causing the HTTP endpoint to return 503 to all legitimate users.

## Finding Description
**Admission-time check** (`should_accept_ingress_message`, `rs/execution_environment/src/execution_environment.rs` L3340–3373): The code explicitly labels this a "first-pass" check and calls `can_withdraw_cycles_with_threshold` with `cost` — the cost of the single message being submitted. The comment acknowledges "A more rigorous check happens later in the ingress selector." Because the check reads the same canister state for every submission, N concurrent submissions from a canister with cycles sufficient for exactly one message all pass independently.

**Pool validation** (`validate_ingress_pool_object`, `rs/ingress_manager/src/ingress_handler.rs` L167–206): When messages move from unvalidated to validated, only size, already-known status, and signature are checked. No cycles check is performed. All N messages enter the validated pool.

**Proposal-time check** (`validate_ingress`, `rs/ingress_manager/src/ingress_selector.rs` L566–584): At block-building time, the check accumulates cost across all messages from the same canister: `*cumulative_ingress_cost + ingress_cost`. Message 1 passes; messages 2–N fail with `InsufficientCycles`.

**No eviction of failing messages** (`rs/ingress_manager/src/ingress_selector.rs` L204–207): The failure branch executes only `queue.msgs.pop()` — a pop from the local in-memory queue for that round. No `RemoveFromValidated` change action is emitted. The messages remain in the validated pool.

**Pool saturation → HTTP 503** (`rs/http_endpoints/public/src/call.rs` L229–236): The HTTP endpoint calls `ingress_throttler.read().unwrap().exceeds_threshold()` before accepting any new submission. `IngressPoolThrottler::exceeds_threshold` checks the total (global) entry count. Once the pool is saturated with zombie messages, all new HTTP ingress submissions are rejected with `SERVICE_UNAVAILABLE`.

The validated pool cleanup path (`rs/ingress_manager/src/ingress_handler.rs` L112–137) only removes messages whose `IngressHistoryReader` status is non-Unknown, or messages explicitly queued for purge via finalization. Messages that fail cycles validation at proposal time never acquire a non-Unknown status and are never queued for purge — they remain until `PurgeBelowExpiry` fires at TTL expiry.

## Impact Explanation
This is a **High** severity finding matching the allowed impact: "Application/platform-level DoS, crash, consensus blocking, certified-state disruption, or subnet availability impact not based on raw volumetric DDoS." The attack exploits a structural logic flaw — not raw traffic volume — to fill the validated ingress pool with messages that are permanently ineligible for block inclusion. Once the pool is full, `exceeds_threshold()` returns true and the HTTP endpoint returns 503 to all users on the affected node(s), blocking all ingress submission for up to the full TTL window per attack wave.

## Likelihood Explanation
The attack requires only a canister with a minimal cycles balance (enough for one induction cost — a negligible amount proportional to message size). No privileged access, governance majority, or threshold corruption is required. The attacker submits messages via the standard HTTPS `/api/v2/canister/{id}/call` endpoint. The attack is repeatable every TTL window. Using multiple canisters (each funded for one message) and multiple boundary nodes, the attacker can saturate the pool across all subnet nodes. The structural mismatch is present in production code and is not gated by any feature flag.

## Recommendation
1. **Evict messages that fail cycles validation**: When `validate_ingress` returns `InsufficientCycles` during `get_ingress_payload`, emit a `RemoveFromValidated` change action so the message is purged from the validated pool rather than re-selected every round.
2. **Tighten admission-time gate**: Track per-canister cumulative ingress cost at admission time, or check whether the canister's balance exceeds the freeze threshold by more than a configurable multiple of a single message cost before admitting additional messages from the same canister within a TTL window.
3. **Per-canister admission rate limiting**: Limit the number of messages from a single canister that can be admitted to the pool within a TTL window, proportional to the canister's available cycles above the freeze threshold.

## Proof of Concept
1. Create canister `C` with cycles balance = `freeze_threshold + ingress_induction_cost(msg)` (sufficient for exactly one message).
2. Submit N messages targeting `C` via `POST /api/v2/canister/{C}/call` in rapid succession. Each invocation of `should_accept_ingress_message` reads the same canister state and checks `can_withdraw_cycles_with_threshold(cost_of_one_message)` — all N pass.
3. All N messages enter the unvalidated pool and are moved to validated by `validate_ingress_pool_object` (no cycles check at this stage).
4. On the next `get_ingress_payload` call, `validate_ingress` checks cumulative cost: message 1 passes (`cumulative = cost`), message 2 fails (`cumulative = 2×cost > balance − threshold`), messages 3–N fail similarly. Each failure executes `queue.msgs.pop()` only — no pool eviction.
5. Messages 2–N remain in the validated pool. Repeat with additional canisters until `exceeds_threshold()` returns true.
6. Legitimate users now receive `503 Service Unavailable` for all ingress submissions until the attacker's messages expire after `MAX_INGRESS_TTL`.
7. A deterministic integration test can verify this by: (a) creating a canister with minimal cycles, (b) submitting N > 1 messages, (c) calling `get_ingress_payload` and asserting only 1 message is included, (d) asserting the validated pool still contains N − 1 messages, and (e) asserting `exceeds_threshold()` returns true after pool saturation.
Audit Report

## Title
Global CMC Rate Limiter Exhaustible by Single Unprivileged Actor, Blocking All Users from Minting Cycles - (File: rs/nns/cmc/src/main.rs)

## Summary
The Cycles Minting Canister enforces a single global hourly rate limit (`base_limiter`) shared across all callers. Because the check in `limiter::check_and_add_cycles` uses strict greater-than (`count + cycles_to_mint > limit`), a single actor can mint exactly `DEFAULT_CYCLES_LIMIT` (150 petacycles) in one call, consuming the entire hourly budget. All subsequent minting calls from any user are then rejected for up to one hour. Since the attacker receives cycles in exchange for ICP, the net cost is only the ICP ledger transaction fee, and the attack is repeatable every hour.

## Finding Description
`DEFAULT_CYCLES_LIMIT` is set to `150e15 as u128` (150 petacycles per hour) in `rs/nns/cmc/src/main.rs` at line 83. The global `base_limiter` of type `limiter::Limiter` is stored in the shared `State` struct (lines 238–239) and accumulates all cycles minted across all callers within a rolling one-hour window.

In `notify_top_up` (lines 1148–1155), the limiter selector is `CyclesMintingLimiterSelector::BaseLimit` for every caller except the Subnet Rental Canister topping up itself. All ordinary users share this single bucket.

In `limiter::check_and_add_cycles` (lines 34–56 of `rs/nns/cmc/src/limiter.rs`), the guard is:

```rust
if count + cycles_to_mint > limit {
    return Err(...);
}
```

The condition is **strictly greater than**, not `>=`. When `count = 0` and `cycles_to_mint = 150P` and `limit = 150P`, the expression evaluates to `false`, so the full limit is consumed in a single call. After that, any call minting even 1 cycle evaluates `150P + 1 > 150P` → `true` → rejected.

`ensure_balance` (lines 2306–2324) calls `limiter_to_use.check_and_add_cycles` before minting, so this guard is the sole enforcement point for `notify_top_up`, `notify_create_canister`, and `notify_mint_cycles`.

The integration test `cmc_notify_top_up_rate_limited` (lines 1644–1687 of `rs/nns/integration_tests/src/cycles_minting_canister.rs`) directly confirms that a single large mint exhausts the global budget and causes all subsequent callers to receive `"try again later"`.

## Impact Explanation
This is an **application/platform-level DoS** on a core NNS canister. All users are blocked from minting cycles via the CMC — including canister creation (`notify_create_canister`), topping up canisters (`notify_top_up`), and minting to the cycles ledger (`notify_mint_cycles`) — for up to one hour per attack cycle. The attack is repeatable every hour, making the CMC effectively permanently unavailable to all other users. This matches the allowed High impact: *"Application/platform-level DoS, crash, consensus blocking, certified-state disruption, or subnet availability impact not based on raw volumetric DDoS."*

## Likelihood Explanation
Any unprivileged ingress sender or canister caller holding sufficient ICP (~1,500 ICP at 100 XDR/ICP) can trigger this. The ICP is not lost — it is converted to cycles — so the net financial cost is only the ICP ledger transaction fee (~0.0001 ICP). No privileged role is required; `notify_top_up` is an open endpoint. The attack is repeatable every hour indefinitely. A competitor or ecosystem disruptor has clear motivation and near-zero cost.

## Recommendation
1. **Per-caller sub-limits**: Track cycles minted per `PrincipalId` within the rolling window and reject requests where the caller's individual share would exceed a per-caller cap (e.g., a fraction of the global limit).
2. **Single-call cap**: Reject any single call that would mint more than a configurable fraction (e.g., 10%) of the global hourly limit, regardless of ICP amount.
3. **Graduated limits**: Reserve a portion of the global budget for small mints so that small users cannot be blocked by a single large actor exhausting the budget.

## Proof of Concept
1. Attacker transfers 1,500 ICP to the CMC subaccount for their canister (yields exactly 150P cycles at 100 XDR/ICP, 1T cycles/XDR).
2. Attacker calls `notify_top_up` with the resulting block index and their canister ID.
3. CMC calls `ensure_balance` → `check_and_add_cycles(now, 150P, 150P)`. The check `0 + 150P > 150P` is `false`; the full limit is consumed. Attacker's canister receives 150P cycles.
4. Any subsequent call from any user minting even 1 cycle evaluates `150P + N > 150P` → `true` → returns `Err("More than 150000000000000000 cycles have been minted in the last 3600 seconds, please try again later.")`.
5. All users are blocked for up to 3,600 seconds.
6. Attacker repeats steps 1–5 every hour at near-zero net cost.

This is directly reproducible using the existing `cmc_notify_top_up_rate_limited` integration test pattern in `rs/nns/integration_tests/src/cycles_minting_canister.rs` by substituting a single 150P mint for the two 90P mints shown in the test.
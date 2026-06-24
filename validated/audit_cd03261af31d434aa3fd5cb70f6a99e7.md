Audit Report

## Title
Global Cycles-Minting Rate Limit in CMC Can Be Exhausted by a Single Unprivileged User, Denying Service to All Other Users - (File: `rs/nns/cmc/src/main.rs`)

## Summary
The Cycles Minting Canister (CMC) enforces a single global rate limit of 150 Peta-cycles per hour (`DEFAULT_CYCLES_LIMIT`) shared across all callers via a single `Limiter` instance (`base_limiter`). A single unprivileged user who converts approximately 1,500 ICP to cycles in one call atomically consumes the entire hourly budget, causing every subsequent `notify_top_up` and `notify_mint_cycles` call from any other user to be rejected with a refund for up to one hour. The attack is economically near-free because the attacker receives cycles of equivalent market value in exchange for their ICP.

## Finding Description
`rs/nns/cmc/src/main.rs` defines a hard-coded global limit at line 83:
```rust
const DEFAULT_CYCLES_LIMIT: u128 = 150e15 as u128;
```
This is stored in `State::base_cycles_limit` and enforced by a single `base_limiter: limiter::Limiter` instance (line 382), which maintains a single `total_count: Cycles` field accumulating across **all** callers with no per-principal isolation.

The `Limiter::check_and_add_cycles` function in `rs/nns/cmc/src/limiter.rs` (lines 34–56) performs the check:
```rust
if count + cycles_to_mint > limit { ... return Err(...); }
self.add(now, cycles_to_mint);
```
Both public minting paths feed into this shared bucket:
- `notify_top_up` → `process_top_up` → `deposit_cycles` → `ensure_balance` (main.rs lines 1985–2117)
- `notify_mint_cycles` → `process_mint_cycles` → `do_mint_cycles` → `ensure_balance` (main.rs line 2151)

The check uses `>` (strict), so a call that brings `total_count` exactly to `limit` succeeds, leaving zero budget for any subsequent caller. There is no per-principal accounting, no per-call cap, and no mechanism to prevent a single transaction from consuming the entire window.

## Impact Explanation
This is a **High** severity application/platform-level DoS on the CMC, a critical NNS canister. For up to 3,600 seconds following a single exhaustion event, every user's `notify_top_up` and `notify_mint_cycles` call is rejected. Canisters that depend on timely cycle top-ups — including production services, SNS canisters, and subnet-rental operations — are denied service. The impact is concrete, repeatable, and not based on raw volumetric DDoS; it exploits a design flaw in a single transaction.

## Likelihood Explanation
The attack requires only a standard ICP ledger transfer followed by a `notify_top_up` call — both are fully permissionless. No privileged role, key, or governance majority is needed. The 150 Peta-cycle threshold corresponds to roughly 1,500 ICP at current prices. The attacker receives cycles of equivalent market value, so the net cost is only the ICP ledger transfer fee. The attack is repeatable every hour. The integration test at `rs/nns/integration_tests/src/cycles_minting_canister.rs` lines 1677–1686 explicitly confirms that the rate limit is global and that subsequent callers are rejected after the budget is consumed.

## Recommendation
Replace the single global `base_limiter` with a per-`PrincipalId` rate limiter so that one user exhausting their own quota does not affect others. Alternatively, cap the maximum cycles mintable in a single call to a fraction of the global limit (e.g., 10%) to prevent any single transaction from consuming the entire budget. The limit value and window should remain adjustable via NNS governance, but accounting must be isolated per principal.

## Proof of Concept
1. Alice holds 2,000 ICP. She transfers 1,500 ICP to the CMC subaccount for canister `X` and calls `notify_top_up { block_index: B, canister_id: X }`.
2. CMC converts 1,500 ICP → ~150 Peta-cycles. `check_and_add_cycles(now, 150P, 150P)` evaluates `0 + 150P > 150P` → **false** → call succeeds; `total_count` becomes 150P. Alice receives 150P cycles.
3. Bob immediately calls `notify_top_up` for 1 ICP (→ ~100T cycles). `check_and_add_cycles(now, 100T, 150P)` evaluates `150P + 100T > 150P` → **true** → Bob's call is rejected with `"try again later"` and his ICP is refunded minus the transfer fee.
4. Every other user's `notify_top_up` or `notify_mint_cycles` call is similarly rejected for up to 3,600 seconds.
5. Alice repeats the attack each hour at near-zero net cost (ICP converted to cycles at market rate; only the ledger transfer fee is lost).

This is directly reproducible using the existing integration test framework in `rs/nns/integration_tests/src/cycles_minting_canister.rs`, which already demonstrates the global rate-limit behavior at lines 1677–1686.
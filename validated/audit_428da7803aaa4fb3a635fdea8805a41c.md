The code matches all cited claims. Let me verify the one remaining question — whether `validate_possibility_of_direct_participation` blocks the attacker's setup contribution.

Audit Report

## Title
SNS Swap DoS via Front-Running When `min_direct_participation == max_direct_participation` - (File: `rs/sns/swap/src/swap.rs`)

## Summary
The SNS swap initialization validation permits `min_direct_participation_icp_e8s == max_direct_participation_icp_e8s` due to a non-strict `<` comparison. An unprivileged attacker can exploit this by contributing exactly `available_capacity - 1 e8`, leaving 1 e8 of remaining capacity. Any subsequent participant's effective balance is capped to 1 e8, which falls below `min_participant_icp_e8s`, causing their contribution to be rejected. If all other participants have reached `max_participant_icp_e8s`, only the attacker can fill the final 1 e8 — and by refusing, they permanently prevent the swap from reaching `min_direct_participation`, forcing an abort and failing the SNS launch.

## Finding Description

**Root Cause 1 — Non-strict validation allows `min == max`:**

In `rs/sns/init/src/lib.rs` at L1590, the check uses strict `<`:

```rust
if max_direct_participation_icp_e8s < min_direct_participation_icp_e8s {
    return Err(...)
}
```

This permits `min == max`, creating a configuration where the swap has exactly one valid total ICP amount at which it can succeed.

**Root Cause 2 — Post-cap balance checked against `min_participant_icp_e8s`:**

In `refresh_buyer_token_e8s`, `max_increment_e8s` is set to `available_direct_participation_e8s()` (L1177). The requested increment is capped to this value (L1224), and the resulting `new_balance_e8s` is then capped to `max_participant_icp_e8s` (L1237). Only after both caps is `new_balance_e8s` checked against `min_participant_icp_e8s` (L1241–1246). When `available = 1 e8`, any new participant's `new_balance_e8s` becomes 1 e8, which is below any realistic `min_participant_icp_e8s`.

**Why `validate_possibility_of_direct_participation` does not block the attack:**

This guard (L336–346) only rejects participation when `icp_target.is_reached_or_exceeded()`. In the attack state, `current = max - 1 e8`, which maps to `IcpTargetProgress::NotReached`, so the guard passes and the attacker's setup contribution is accepted.

**Concrete exploit (min = max = 10 ICP, min_participant = 1 ICP, max_participant = 4 ICP):**

1. Alice contributes 4 ICP → accepted, hits `max_participant`.
2. Bob contributes 4 ICP → accepted, hits `max_participant`.
3. Attacker contributes `2 ICP - 1 e8 = 199,999,999 e8s`:
   - `available = 200,000,000 e8s`; `actual_increment = min(200,000,000, 199,999,999) = 199,999,999`
   - `new_balance = 199,999,999 ≥ min_participant (100,000,000)` → **accepted**
   - `current_direct_participation = 999,999,999 e8s = 10 ICP - 1 e8`
4. New participant submits 1 ICP:
   - Passes initial check at L1202 (`100,000,000 ≥ min_participant`)
   - `max_increment_e8s = available = 1`; `actual_increment = 1`; `new_balance = 1`
   - `1 < min_participant (100,000,000)` → **REJECTED** at L1241
5. Alice and Bob cannot increase (already at `max_participant`; any additional ICP is capped back to their existing balance, producing zero net increment).
6. Attacker holds `old_amount = 199,999,999 e8s < max_participant = 400,000,000 e8s` and could fill the last 1 e8, but refuses.
7. `min_direct_participation_icp_e8s_reached()` returns `false` (L2833: `999,999,999 < 1,000,000,000`).
8. `can_commit()` returns `false` (L2891) until deadline; `try_abort` succeeds at deadline.

The existing test suite at `rs/sns/swap/tests/swap.rs` L5845–5853 already demonstrates the exact rejection behavior (`"Rejecting participation of effective amount {}; minimum required to participate: {}"`) when `available_direct_participation_e8s()` is less than `min_participant_icp_e8s`.

## Impact Explanation

The SNS decentralization swap is permanently blocked from committing. All participants receive their ICP back on abort, but the SNS launch fails entirely. Since `min_direct_participation_icp_e8s`, `max_direct_participation_icp_e8s`, and `min_participant_icp_e8s` are immutable after swap creation, there is no recovery path during the swap duration. This matches the High impact category: **"Significant SNS security impact with concrete user or protocol harm"** — a targeted SNS launch can be reliably sabotaged by a single unprivileged actor at no permanent financial cost.

## Likelihood Explanation

The attack requires: (1) a swap configured with `min == max` direct participation — explicitly permitted by current validation and a natural "exact target" configuration; (2) an attacker willing to lock ICP for the swap duration, recovered in full on abort; (3) timing the front-run after enough participants have hit `max_participant_icp_e8s`. Condition 1 is a valid documented configuration. Conditions 2 and 3 are achievable by any motivated actor monitoring the swap canister state. The attacker bears zero permanent financial loss and gains a competitive advantage by preventing a rival SNS from launching.

## Recommendation

Change the validation in `validate_participation_constraints` in `rs/sns/init/src/lib.rs` from strict less-than to less-than-or-equal:

```rust
// Before (allows min == max):
if max_direct_participation_icp_e8s < min_direct_participation_icp_e8s {

// After (prevents min == max):
if max_direct_participation_icp_e8s <= min_direct_participation_icp_e8s {
```

This ensures `max > min`, so even if `current_direct_participation = max - 1 e8`, the swap can still abort gracefully (since `current ≥ min`) rather than being stuck in a state where it can neither commit nor accept new contributions.

## Proof of Concept

Using the existing `SwapBuilder` test infrastructure in `rs/sns/swap/tests/swap.rs`:

```rust
// Setup: min_direct == max_direct == 10 ICP, min_participant = 1 ICP, max_participant = 4 ICP
// 1. Alice: refresh_buyer_token_e8s(400_000_000) -> Ok (hits max_participant)
// 2. Bob:   refresh_buyer_token_e8s(400_000_000) -> Ok (hits max_participant)
// 3. Attacker: refresh_buyer_token_e8s(199_999_999) -> Ok
//    current_direct_participation = 999_999_999 e8s
// 4. New participant: refresh_buyer_token_e8s(100_000_000)
//    max_increment_e8s = 1
//    new_balance_e8s = 1 < min_participant (100_000_000)
//    -> Err("Rejecting participation of effective amount 1; minimum required to participate: 100000000")
// 5. assert!(!swap.can_commit(now));
// 6. swap.try_abort(deadline) == true -> Lifecycle::Aborted
```

The rejection path is confirmed by the existing test at `rs/sns/swap/tests/swap.rs` L5845–5853, which asserts the identical error string when `available_direct_participation_e8s() < min_participant_icp_e8s`.
The code behavior is confirmed. Let me verify the key facts before rendering a verdict:

1. `Cycles::sub` is saturating (confirmed at `rs/types/cycles/src/cycles.rs` lines 133–139)
2. `ensure_balance` uses `canister_balance128()` — the total live balance — as the baseline (line 2312–2313)
3. `check_and_add_cycles` in the limiter adds `cycles_to_mint` to the window count; if `cycles_to_mint == 0`, the limiter is not charged (limiter.rs lines 43–54)
4. `total_cycles_minted` is incremented by `cycles_to_mint` (line 2317)
5. `deposit_cycles` sends the full `cycles` amount regardless (line 2126)
6. Any canister can call `deposit_cycles` on the management canister targeting the CMC — no privilege required

All claims check out against the actual code.

---

Audit Report

## Title
Donated Cycles to CMC Bypass Rate Limiter and Understate `total_cycles_minted` — (File: rs/nns/cmc/src/main.rs)

## Summary
`ensure_balance` computes `cycles_to_mint = cycles - canister_balance128()` using saturating subtraction. Because `canister_balance128()` includes all cycles held by the CMC — including those donated by any unprivileged caller via `management_canister.deposit_cycles` — a pre-funded CMC balance causes `cycles_to_mint` to saturate to zero. This silently charges the rate limiter zero, leaves `total_cycles_minted` unchanged, and still distributes the full `cycles` amount to the target canister while burning the corresponding ICP.

## Finding Description
In `rs/nns/cmc/src/main.rs` at line 2312–2313, `ensure_balance` reads the CMC's total live balance and subtracts it from the requested `cycles`:

```rust
let current_balance = Cycles::from(ic_cdk::api::canister_balance128());
let cycles_to_mint = cycles - current_balance;  // saturating_sub → 0 when balance ≥ cycles
```

`Cycles::sub` is implemented as `saturating_sub` (`rs/types/cycles/src/cycles.rs` lines 133–139), so when `current_balance ≥ cycles`, `cycles_to_mint` is zero. This zero value is then passed to:

- `limiter_to_use.check_and_add_cycles(state, now, cycles_to_mint)` (line 2316): the limiter's `check_and_add_cycles` in `rs/nns/cmc/src/limiter.rs` (lines 43–54) checks `count + 0 > limit`, which always passes, and records zero in the window — the per-period cap is not consumed.
- `state.total_cycles_minted += cycles_to_mint` (line 2317): the audit counter is incremented by zero.
- `ic0_mint_cycles128(cycles_to_mint)` (line 2322): zero new cycles are minted.

Despite this, `deposit_cycles` at line 2126 sends the full `cycles` amount to the target canister from the CMC's existing (donated) balance, and `burn_and_log` burns the corresponding ICP. The CMC's balance is replenished by the donated cycles, not by minting.

Any canister on any subnet can call `management_canister.deposit_cycles` targeting the CMC's canister ID with an arbitrary cycle attachment — this is a standard, unprivileged inter-canister call with no access control. Cycles for this purpose can be sourced from chain-fusion bridges (ckETH, ckBTC) without having previously been subject to the CMC rate limiter.

## Impact Explanation
**Rate-limiter bypass (High):** The rate limiter is the sole on-chain mechanism preventing a burst of ICP-to-cycles conversions within a governance-set period. An attacker who pre-funds the CMC with donated cycles can allow an arbitrary volume of ICP to be burned and cycles distributed in a single period without the limiter ever firing. This undermines a critical NNS governance security control. This matches the allowed impact: *"Significant NNS security impact with concrete user or protocol harm."*

**`total_cycles_minted` understatement:** The public query `total_cycles_minted` is used by governance and monitoring tooling to audit the ICP-to-cycles conversion rate. When the CMC's balance is inflated by donations, this counter silently diverges from the true amount of cycles distributed, breaking the conservation invariant `total_cycles_minted ≈ Σ(ICP burned × rate)`.

## Likelihood Explanation
Any canister on any subnet can execute the attack with no privileged access, no governance majority, and no threshold-crypto compromise. Cycles can be sourced from ckETH/ckBTC chain-fusion bridges, meaning the attacker is not constrained by the CMC rate limiter itself. The cost to the attacker is the donated cycles, but for a large ICP holder seeking to convert quickly, the benefit of bypassing the rate limiter outweighs this cost. The attack is repeatable as long as the attacker can supply donated cycles.

## Recommendation
Track the CMC's "minted-from-ICP" balance separately from its total live balance. Introduce a `cycles_held_from_minting: Cycles` field in `State` that is incremented by `cycles_to_mint` after each successful `ic0_mint_cycles128` call and decremented when cycles are sent out. Use this field — not `canister_balance128()` — to compute `cycles_to_mint`:

```rust
let cycles_to_mint = cycles.saturating_sub(state.cycles_held_from_minting);
```

This ensures that donated cycles do not reduce the amount charged to the rate limiter or credited to `total_cycles_minted`.

## Proof of Concept
1. Attacker's canister calls `management_canister.deposit_cycles(CMC_CANISTER_ID)` attaching `LARGE_CYCLES` — donating them to the CMC.
2. CMC's `canister_balance128()` is now `LARGE_CYCLES + prior_balance`.
3. Victim calls `notify_top_up` for `N` ICP (≡ `X` cycles, where `X ≤ LARGE_CYCLES`).
4. `ensure_balance(X)` runs: `cycles_to_mint = X - (LARGE_CYCLES + prior_balance) = 0` (saturating).
5. `check_and_add_cycles(state, now, 0)` — rate limiter not charged.
6. `state.total_cycles_minted += 0` — counter not updated.
7. CMC sends `X` cycles to victim's canister from its existing balance.
8. `burn_and_log` burns `N` ICP.

Repeat steps 3–8 for any number of users until `LARGE_CYCLES` is exhausted; the rate limiter is never triggered regardless of total ICP burned. A deterministic integration test using PocketIC can confirm this by: (a) pre-funding the CMC with donated cycles, (b) calling `notify_top_up` in a loop exceeding the configured `base_cycles_limit`, and (c) asserting that no `NotifyError` is returned and `total_cycles_minted` remains zero throughout.
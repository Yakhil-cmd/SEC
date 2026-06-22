### Title
Cycles Minting Rate Limit Bypass via Pre-Accumulated Cycles from Failed Deposits - (`File: rs/nns/cmc/src/main.rs`)

### Summary

The `ensure_balance` function in the Cycles Minting Canister (CMC) computes the cycles to mint as `cycles_to_mint = cycles - current_balance` using saturating subtraction. When a top-up deposit fails (e.g., target canister does not exist), the already-minted cycles remain in the CMC's balance. After the sliding rate-limit window expires, a subsequent deposit request for the same or smaller amount computes `cycles_to_mint = 0`, adds nothing to the limiter, and deposits the pre-accumulated cycles to the target canister — effectively bypassing the rate limit for that request.

### Finding Description

The `ensure_balance` function is the sole enforcement point for the CMC's cycles-minting rate limit:

```rust
fn ensure_balance(
    cycles: Cycles,
    limiter_to_use: CyclesMintingLimiterSelector,
) -> Result<(), String> {
    let now = now_system_time();
    let current_balance = Cycles::from(ic_cdk::api::canister_balance128());
    let cycles_to_mint = cycles - current_balance;   // saturating sub → 0 when balance ≥ cycles

    with_state_mut(|state| {
        limiter_to_use.check_and_add_cycles(state, now, cycles_to_mint)?;
        state.total_cycles_minted += cycles_to_mint;
        Ok::<_, String>(())
    })?;

    let _minted_cycles = ic0_mint_cycles128(cycles_to_mint);
    ...
}
```

The `Cycles` type implements `Sub` as saturating subtraction:

```rust
impl Sub for Cycles {
    type Output = Self;
    fn sub(self, rhs: Self) -> Self {
        Self(self.0.saturating_sub(rhs.0))
    }
}
```

So when `current_balance >= cycles`, `cycles_to_mint` saturates to `Cycles(0)`, and `check_and_add_cycles(state, now, Cycles(0))` adds nothing to the limiter.

The `process_top_up` flow is:

1. `deposit_cycles(canister_id, cycles, true, limiter)` → calls `ensure_balance` (mints cycles, increments limiter) → calls `call_with_payment128` to deposit to target canister.
2. If the deposit call fails (e.g., canister does not exist), `process_top_up` calls `refund_icp` to return ICP to the sender.
3. The minted cycles are **not** returned to the system — they remain in the CMC's balance.

The rate-limit `Limiter` uses a sliding time window (`max_age = 1 hour` for the base limiter). After the window expires, the cycles minted during the failed deposit are purged from the limiter's `total_count`.

### Impact Explanation

An unprivileged ingress sender can exceed the per-hour cycles-minting cap (`base_cycles_limit`, currently 150 Peta-cycles/hour) by the following sequence:

1. **T = 0**: Send ICP to the CMC subaccount for a non-existing canister. Call `notify_top_up`. CMC mints X cycles (counted in limiter), deposit fails, ICP is refunded. CMC balance = X.
2. **T = 1 hour**: The limiter's sliding window purges the X cycles from `total_count`. Limiter is now at 0.
3. **T = 1 hour**: Call `notify_top_up` for a valid canister with the same ICP amount. `ensure_balance` computes `cycles_to_mint = X − X = 0`. Limiter is **not** incremented. CMC deposits X cycles to the target canister from its existing balance.
4. **T = 1 hour**: Also call `notify_top_up` for another valid canister up to the full rate limit (150 P cycles). This is accepted normally.

Net result: at T = 1 hour, the attacker deposits X + 150 P cycles, while the rate limit is 150 P cycles/hour. The bypass scales linearly with X (bounded only by the attacker's ICP budget and the fee cost of failed deposits).

The impact is a **cycles/resource accounting bug**: the rate limit protecting the IC's ICP-to-cycles conversion is circumventable by any unprivileged ingress sender willing to pay the `TOP_UP_CANISTER_REFUND_FEE` on failed deposits.

### Likelihood Explanation

- The attack requires no privileged access, no governance majority, and no threshold corruption.
- The attacker only needs ICP tokens and the ability to call `notify_top_up` — a public endpoint.
- The `TOP_UP_CANISTER_REFUND_FEE` is small relative to the cycles gained.
- The attack is repeatable every rate-limit window (hourly for the base limiter).
- The existing test `cmc_notify_top_up_not_rate_limited_by_invalid_top_up` confirms the mechanism (failed deposits leave cycles in the CMC balance) but does not test the cross-window bypass scenario.

### Recommendation

Track and subtract cycles that remain in the CMC's balance due to failed deposits from the limiter, or alternatively, do not mint cycles until the deposit to the target canister is confirmed. One concrete fix: move `ensure_balance` (the limiter increment and minting) to **after** the `call_with_payment128` succeeds, so that only cycles actually delivered to a target canister are counted. Alternatively, on deposit failure, explicitly call `ic0_mint_cycles128` with a negative adjustment or burn the stranded cycles so they do not persist in the CMC balance across rate-limit windows.

### Proof of Concept

```
// Setup: base_cycles_limit = 150 Peta-cycles/hour
// ICP/XDR rate: 100 XDR/ICP, cycles_per_xdr = 1T

// Step 1 (T=0): Attacker sends 1500 ICP to CMC subaccount for non-existing canister X
//   notify_top_up({block_index: B1, canister_id: X})
//   → ensure_balance(150P): current_balance=0, cycles_to_mint=150P, limiter += 150P
//   → deposit_cycles to X fails (X does not exist)
//   → refund_icp: attacker gets ~1500 ICP back (minus fee)
//   → CMC balance = 150P cycles

// Step 2 (T=1 hour): Limiter window expires, total_count reset to 0

// Step 3 (T=1 hour): Attacker sends 1500 ICP to CMC subaccount for valid canister Y
//   notify_top_up({block_index: B2, canister_id: Y})
//   → ensure_balance(150P): current_balance=150P, cycles_to_mint=0, limiter += 0
//   → deposit_cycles to Y succeeds (150P deposited, no rate limit consumed)
//   → burn_and_log: 1500 ICP burned

// Step 4 (T=1 hour): Attacker also sends 1500 ICP for valid canister Z
//   notify_top_up({block_index: B3, canister_id: Z})
//
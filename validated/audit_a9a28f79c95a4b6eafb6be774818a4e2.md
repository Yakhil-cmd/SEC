Audit Report

## Title
`ensure_balance` Charges Rate Limiter Only on Delta vs. Live Balance, Enabling ICP-to-Cycles Conversion Without Rate-Limit Accounting - (File: `rs/nns/cmc/src/main.rs`)

## Summary
The `ensure_balance` function computes cycles to mint as `cycles - canister_balance128()` using saturating subtraction. Because `create_canister` accepts the caller's cycles into the CMC's own balance after a successful call, any subsequent ICP-based conversion whose cycle equivalent is ≤ the residual CMC balance will mint zero cycles, charge zero against the rate limiter, and add zero to `total_cycles_minted`, while still forwarding the full cycle amount to the target canister from the pre-existing balance. ICP is burned without new cycles being minted, and the rate limiter is not charged for the ICP-originated conversion.

## Finding Description

**Root cause — saturating subtraction in `ensure_balance`:**

`Cycles::sub` is implemented as `saturating_sub` over `u128`:

```rust
impl Sub for Cycles {
    fn sub(self, rhs: Self) -> Self {
        Self(self.0.saturating_sub(rhs.0))   // silently clamps to 0
    }
}
``` [1](#0-0) 

`ensure_balance` reads the live balance and computes the delta:

```rust
let current_balance = Cycles::from(ic_cdk::api::canister_balance128());
let cycles_to_mint = cycles - current_balance;   // saturates to 0 when balance >= cycles
with_state_mut(|state| {
    limiter_to_use.check_and_add_cycles(state, now, cycles_to_mint)?;
    state.total_cycles_minted += cycles_to_mint;
    ...
})?;
let _minted_cycles = ic0_mint_cycles128(cycles_to_mint);
``` [2](#0-1) 

The rate limiter's `check_and_add_cycles` only records `cycles_to_mint`, not the full `cycles` argument: [3](#0-2) 

**How the CMC accumulates a residual balance:**

The cycles-based `create_canister` endpoint calls `do_create_canister` (which calls `ensure_balance` and mints cycles), then on success calls `msg_cycles_accept(cycles)`, depositing the caller's cycles into the CMC's own balance: [4](#0-3) 

The integration test explicitly asserts this residue is stable: [5](#0-4) 

**ICP-based paths that call `ensure_balance` through `deposit_cycles`:**

`process_top_up` → `deposit_cycles(mint_cycles=true)` → `ensure_balance`: [6](#0-5) 

`do_mint_cycles` also calls `ensure_balance` directly: [7](#0-6) 

**Exploit flow:**

1. Attacker calls `create_canister` with X cycles. `ensure_balance(X)` mints X cycles (rate limiter charged X), sends them to the management canister to create a new canister, then `msg_cycles_accept(X)` deposits X cycles into the CMC's balance. CMC balance = X.
2. Attacker (or any user) sends ICP worth X cycles to the CMC subaccount and calls `notify_top_up`.
3. `process_top_up` → `deposit_cycles` → `ensure_balance(X)`: `current_balance = X`, `cycles_to_mint = X − X = 0`. Rate limiter charged: **0**. `total_cycles_minted += 0`. CMC sends X cycles from its existing balance to the target canister. `burn_and_log` burns the ICP.
4. CMC balance returns to 0. X ICP burned. X cycles delivered. Rate limiter not charged for the ICP conversion.
5. Attacker uses cycles from the newly created canister (step 1) to fund the next `create_canister` call and repeats.

## Impact Explanation

**Rate-limiter bypass for ICP-based conversions:** The `check_and_add_cycles` guard is charged 0 for the ICP-originated minting event. An attacker who pre-funds the CMC via `create_canister` can convert ICP to cycles without consuming any rate-limit budget for those conversions, allowing ICP burning at a rate exceeding the intended per-period minting cap.

**`total_cycles_minted` undercount:** The governance-visible counter incremented by `state.total_cycles_minted += cycles_to_mint` records 0 for each bypassed conversion, making on-chain accounting of ICP-originated cycles creation incorrect. [8](#0-7) 

**ICP conservation violation:** `burn_and_log` destroys the full ICP amount while zero new cycles are minted. The ICP supply decreases without a corresponding increase in the cycles supply, distorting the ICP/cycles exchange rate — a concrete NNS/financial-integrity impact.

This matches the **High** impact tier: *Significant NNS/ledger/infrastructure security impact with concrete user or protocol harm.*

## Likelihood Explanation

`create_canister` is a public `#[update]` endpoint callable by any canister on the IC with sufficient cycles. No privileged access, governance vote, or subnet-majority is required. The residual balance is a deterministic, test-confirmed property of the current implementation. The attack is repeatable: each iteration requires only one `create_canister` call and one ICP-based notify call, and the cycles from the newly created canister can fund the next iteration, making the loop self-sustaining after the initial cycle outlay.

## Recommendation

Replace the delta-based approach with one that always mints the full requested amount and charges the full amount to the rate limiter, regardless of the CMC's current balance:

```rust
fn ensure_balance(cycles: Cycles, limiter_to_use: CyclesMintingLimiterSelector) -> Result<(), String> {
    let now = now_system_time();
    with_state_mut(|state| {
        limiter_to_use.check_and_add_cycles(state, now, cycles)?;
        state.total_cycles_minted += cycles;
        Ok::<_, String>(())
    })?;
    let _minted_cycles = ic0_mint_cycles128(cycles);
    Ok(())
}
```

Alternatively, separate the cycles-based `create_canister` path from ICP-based paths so the CMC never accumulates caller-provided cycles in its own balance, and the rate limiter exclusively tracks ICP-originated minting.

## Proof of Concept

Deterministic integration test plan (extending the existing test in `rs/nns/integration_tests/src/cycles_minting_canister.rs`):

1. Record the rate limiter's current count via `get_icp_xdr_conversion_rate` / state inspection.
2. Call `create_canister` with 10 T cycles from a universal canister. Assert `cmc_cycles_balance() == 10_000_000_000_000`.
3. Transfer 1 ICP (≈ 10 T cycles at test rate) to the CMC subaccount for a target canister. Call `notify_top_up`.
4. Assert the target canister received 10 T cycles.
5. Assert `total_cycles_minted()` did **not** increase by 10 T (it increased by 0).
6. Assert the rate limiter count did **not** increase for the `notify_top_up` call.
7. Assert `cmc_cycles_balance() == 0` (pre-existing balance consumed, no new minting).
8. Assert 1 ICP was burned from the CMC subaccount.

Steps 2–8 can be repeated in a loop to demonstrate the self-sustaining nature of the bypass.

### Citations

**File:** rs/types/cycles/src/cycles.rs (L133-139)
```rust
impl Sub for Cycles {
    type Output = Self;

    fn sub(self, rhs: Self) -> Self {
        Self(self.0.saturating_sub(rhs.0))
    }
}
```

**File:** rs/nns/cmc/src/main.rs (L1502-1505)
```rust
    match do_create_canister(caller(), cycles.into(), subnet_selection, settings).await {
        Ok(canister_id) => {
            ic_cdk::api::call::msg_cycles_accept(cycles);
            Ok(canister_id)
```

**File:** rs/nns/cmc/src/main.rs (L2116-2118)
```rust
    if mint_cycles {
        ensure_balance(cycles, limiter_to_use)?;
    }
```

**File:** rs/nns/cmc/src/main.rs (L2151-2151)
```rust
    ensure_balance(cycles, CyclesMintingLimiterSelector::BaseLimit)?;
```

**File:** rs/nns/cmc/src/main.rs (L2312-2322)
```rust
    let current_balance = Cycles::from(ic_cdk::api::canister_balance128());
    let cycles_to_mint = cycles - current_balance;

    with_state_mut(|state| {
        limiter_to_use.check_and_add_cycles(state, now, cycles_to_mint)?;
        state.total_cycles_minted += cycles_to_mint;
        Ok::<_, String>(())
    })?;

    // unused because of check above
    let _minted_cycles = ic0_mint_cycles128(cycles_to_mint);
```

**File:** rs/nns/cmc/src/limiter.rs (L34-55)
```rust
    pub fn check_and_add_cycles(
        &mut self,
        now: SystemTime,
        cycles_to_mint: Cycles,
        limit: Cycles,
    ) -> Result<(), String> {
        self.purge_old(now);
        let count = self.get_count();

        if count + cycles_to_mint > limit {
            LIMITER_REJECT_COUNT.with(|count| {
                count.set(count.get().saturating_add(1));
            });

            return Err(format!(
                "More than {} cycles have been minted in the last {} seconds, please try again later.",
                limit,
                self.get_max_age().as_secs(),
            ));
        }
        self.add(now, cycles_to_mint);
        Ok(())
```

**File:** rs/nns/integration_tests/src/cycles_minting_canister.rs (L475-476)
```rust
    // We minted, then used, then accepted some cycles.
    assert_eq!(cmc_cycles_balance(), Nat::from(10_000_000_000_000_u128));
```

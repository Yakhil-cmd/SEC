Audit Report

## Title
CMC `ensure_balance` Rate-Limiter Bypass via External Cycle Pre-Deposit - (File: rs/nns/cmc/src/main.rs)

## Summary
The `ensure_balance` function charges the rate limiter and `total_cycles_minted` counter only for the delta between the requested `cycles` amount and the CMC's current live balance, not for the full `cycles` amount dispensed. Because any canister can deposit cycles directly to the CMC via the management canister's `deposit_cycles` call, an attacker can pre-fund the CMC's balance to reduce the recorded delta, bypassing the hourly minting rate limit and permanently understating `total_cycles_minted`. The claimed "catastrophic mint / underflow" sub-scenario is **not valid**: `Cycles::sub` uses `saturating_sub` (not wrapping arithmetic), so `cycles - current_balance` saturates to zero when `current_balance >= cycles`, causing `ic0_mint_cycles128(0)` to be called rather than a near-`u128::MAX` mint.

## Finding Description

`ensure_balance` at lines 2306–2325 of `rs/nns/cmc/src/main.rs` reads the live balance and computes the delta:

```rust
let current_balance = Cycles::from(ic_cdk::api::canister_balance128());
let cycles_to_mint = cycles - current_balance;   // saturating_sub

with_state_mut(|state| {
    limiter_to_use.check_and_add_cycles(state, now, cycles_to_mint)?;
    state.total_cycles_minted += cycles_to_mint;
    ...
})?;
let _minted_cycles = ic0_mint_cycles128(cycles_to_mint);
``` [1](#0-0) 

Both the limiter (`check_and_add_cycles`) and `total_cycles_minted` receive `cycles_to_mint` — the newly minted portion — not the full `cycles` amount that is ultimately forwarded to the caller. The `Cycles::sub` operator is `saturating_sub`: [2](#0-1) 

So if `current_balance >= cycles`, `cycles_to_mint` saturates to zero: the limiter records 0, `total_cycles_minted` increases by 0, and `ic0_mint_cycles128(0)` is called. The CMC then forwards the full `cycles` amount from its pre-funded balance via `call_with_payment128(..., u128::from(cycles))`. [3](#0-2) 

This is called from all three minting paths: `deposit_cycles` (line 2117), `do_mint_cycles` (line 2151), and `do_create_canister` (line 2249). [4](#0-3) [5](#0-4) [6](#0-5) 

The limiter's `check_and_add_cycles` enforces `count + cycles_to_mint > limit`, where `cycles_to_mint` is the reduced delta, not the full dispensed amount: [7](#0-6) 

The `base_cycles_limit` and `subnet_rental_cycles_limit` fields in state are the enforced caps: [8](#0-7) 

## Impact Explanation

This is a **High** severity finding. The `base_cycles_limit` (150T cycles/hour) and `subnet_rental_cycles_limit` (500T cycles/month) are financial controls on the NNS intended to prevent rapid large-scale ICP-to-cycles conversion. An attacker who pre-deposits cycles to the CMC can cause the limiter to record a fraction of the true dispensed amount, allowing ICP-to-cycles conversion at a rate exceeding the intended cap. Additionally, `total_cycles_minted` — a publicly queryable metric — permanently understates the true number of cycles dispensed, breaking auditability of ICP-to-cycles conversion. This matches the allowed impact: "Significant NNS security impact with concrete user or protocol harm."

The catastrophic mint sub-claim (underflow to near-`u128::MAX`) is **invalid**: `Cycles::sub` uses `saturating_sub`, so the result is 0, not a huge value.

## Likelihood Explanation

Any canister on any subnet can call the management canister's `deposit_cycles` to send cycles to the CMC. No privileged role is required. The attacker needs only a canister and cycles for the pre-deposit. The attack is fully permissionless and repeatable. The economic cost is real (the attacker spends cycles in the pre-deposit), but the bypass is achievable by any party wishing to convert ICP to cycles faster than the hourly limit permits.

## Recommendation

Charge the rate limiter for the full `cycles` amount requested, not just the minted delta. The minted delta is still the correct argument to `ic0_mint_cycles128`, but the accounting should reflect the full dispensed amount:

```rust
fn ensure_balance(cycles: Cycles, limiter_to_use: CyclesMintingLimiterSelector) -> Result<(), String> {
    let now = now_system_time();
    let current_balance = Cycles::from(ic_cdk::api::canister_balance128());
    let cycles_to_mint = cycles - current_balance; // saturating, so 0 if already funded

    with_state_mut(|state| {
        // Charge limiter for the full dispensed amount, not just the minted delta
        limiter_to_use.check_and_add_cycles(state, now, cycles)?;
        state.total_cycles_minted += cycles_to_mint;
        Ok::<_, String>(())
    })?;

    let _minted_cycles = ic0_mint_cycles128(cycles_to_mint);
    assert!(ic_cdk::api::canister_balance128() >= cycles.get());
    Ok(())
}
```

## Proof of Concept

1. Deploy canister `Attacker` on any subnet with sufficient cycles.
2. `Attacker` calls `management.deposit_cycles({ canister_id: CMC_ID })` attaching 100T cycles. CMC balance is now 100T.
3. `Attacker` transfers ICP worth 150T cycles to the CMC top-up subaccount and calls `notify_top_up`.
4. Inside `ensure_balance(150T, BaseLimit)`: `cycles_to_mint = 150T - 100T = 50T` (saturating). Limiter records 50T against the 150T/hour cap.
5. CMC mints 50T (balance: 150T), then forwards 150T to the target canister. Limiter has only consumed 50T of the 150T/hour cap.
6. Repeat: each iteration charges only 50T to the limiter while dispensing 150T, allowing 3× the intended throughput per hour relative to the cap.
7. A PocketIC or local replica integration test can verify this by asserting `limiter.get_count() == cycles_to_mint` (50T) rather than `cycles` (150T) after a single top-up with a pre-funded CMC balance.

### Citations

**File:** rs/nns/cmc/src/main.rs (L232-245)
```rust
    /// How many cycles are allowed to be minted in an hour.
    pub base_cycles_limit: Cycles,

    /// How many cycles are allowed to be minted by the Subnet Rental Canister in a month.
    pub subnet_rental_cycles_limit: Cycles,

    /// Maintain a count of how many cycles have been minted in the last hour.
    pub base_limiter: limiter::Limiter,

    /// Maintain a count of how many cycles have been minted by the Subnet Rental Canister
    /// in the last month.
    pub subnet_rental_canister_limiter: limiter::Limiter,

    pub total_cycles_minted: Cycles,
```

**File:** rs/nns/cmc/src/main.rs (L2116-2118)
```rust
    if mint_cycles {
        ensure_balance(cycles, limiter_to_use)?;
    }
```

**File:** rs/nns/cmc/src/main.rs (L2120-2128)
```rust
    let res: CallResult<()> = ic_cdk::api::call::call_with_payment128(
        candid::Principal::management_canister(),
        METHOD_DEPOSIT_CYCLES,
        (CanisterIdRecord {
            canister_id: canister_id.get().0,
        },),
        u128::from(cycles),
    )
    .await;
```

**File:** rs/nns/cmc/src/main.rs (L2151-2151)
```rust
    ensure_balance(cycles, CyclesMintingLimiterSelector::BaseLimit)?;
```

**File:** rs/nns/cmc/src/main.rs (L2249-2249)
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

**File:** rs/types/cycles/src/cycles.rs (L133-139)
```rust
impl Sub for Cycles {
    type Output = Self;

    fn sub(self, rhs: Self) -> Self {
        Self(self.0.saturating_sub(rhs.0))
    }
}
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

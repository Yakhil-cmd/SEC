### Title
Inconsistent Cycles Accounting in `ensure_balance()` Bypasses Rate Limiter When CMC Has Pre-existing Balance - (File: rs/nns/cmc/src/main.rs)

### Summary
The `ensure_balance()` function in the Cycles Minting Canister (CMC) computes `cycles_to_mint = cycles - current_balance` using saturating arithmetic. When the CMC holds a pre-existing cycles balance greater than or equal to the requested `cycles`, `cycles_to_mint` saturates to zero. The rate limiter and `total_cycles_minted` counter are then updated with zero, while the CMC still distributes the full `cycles` amount from its pre-existing balance — bypassing the minting rate limit entirely for that distribution.

### Finding Description
In `ensure_balance()`:

```rust
fn ensure_balance(cycles: Cycles, limiter_to_use: CyclesMintingLimiterSelector) -> Result<(), String> {
    let current_balance = Cycles::from(ic_cdk::api::canister_balance128());
    let cycles_to_mint = cycles - current_balance;   // saturating subtraction

    with_state_mut(|state| {
        limiter_to_use.check_and_add_cycles(state, now, cycles_to_mint)?;  // charged 0
        state.total_cycles_minted += cycles_to_mint;                        // incremented by 0
        Ok::<_, String>(())
    })?;

    let _minted_cycles = ic0_mint_cycles128(cycles_to_mint);  // mints 0
    assert!(ic_cdk::api::canister_balance128() >= cycles.get());
    Ok(())
}
``` [1](#0-0) 

When `current_balance >= cycles`, the saturating subtraction at line 2313 yields `cycles_to_mint = 0`. All three accounting operations — the rate limiter check, the `total_cycles_minted` increment, and the actual `ic0_mint_cycles128` call — receive zero. Yet the caller (`deposit_cycles` or `do_create_canister`) proceeds to attach the full `cycles` amount via `call_with_payment128`, drawing from the CMC's pre-existing balance: [2](#0-1) [3](#0-2) 

This is structurally identical to the reported Solidity bug: the "start balance" (`current_balance`) is not correctly factored into the accounting, causing the comparison and counter update to use an incorrect (zero) value while the actual transfer uses the full amount.

The `Cycles` type uses saturating arithmetic, confirmed by the comment in `cycles_account_manager.rs`: [4](#0-3) 

The rate limiter ceiling is 150 petacycles/month (`CYCLES_MINTING_LIMIT = 150e15`): [5](#0-4) 

### Impact Explanation
Any cycles distributed from the CMC's pre-existing balance are:
1. **Not counted against the rate limiter** — `check_and_add_cycles` is called with 0, so the monthly minting cap is not consumed.
2. **Not reflected in `total_cycles_minted`** — the on-chain accounting metric undercounts actual cycles distributed.

An unprivileged attacker can exploit this by sending cycles directly to the CMC (any canister can send cycles to any other canister), inflating the CMC's `current_balance`. On a subsequent `notify_top_up` or `notify_create_canister` call, if `current_balance >= cycles`, the rate limiter is bypassed. The attacker recovers their pre-loaded cycles via the top-up/creation payment, effectively laundering the rate-limit bypass at near-zero net cost. This undermines the inflation-control invariant the rate limiter is designed to enforce.

### Likelihood Explanation
The CMC's balance is normally near zero between calls (it mints exactly what it needs and immediately sends it). However, the CMC is a publicly addressable canister — any canister can call `deposit_cycles` targeting the CMC's principal, pre-loading it. The attack requires the attacker to hold cycles (obtainable through normal ICP conversion within the rate limit over time), making it a realistic multi-step attack by a motivated actor. Likelihood is **Medium** given the economic cost to set up but the clear, reachable code path.

### Recommendation
Replace the saturating subtraction with an explicit guard:

```rust
fn ensure_balance(cycles: Cycles, limiter_to_use: CyclesMintingLimiterSelector) -> Result<(), String> {
    let current_balance = Cycles::from(ic_cdk::api::canister_balance128());
    // Always charge the rate limiter for the full cycles being distributed,
    // regardless of how much is already in the CMC's balance.
    let cycles_to_charge = cycles;
    let cycles_to_mint = if current_balance >= cycles {
        Cycles::zero()
    } else {
        cycles - current_balance
    };

    with_state_mut(|state| {
        limiter_to_use.check_and_add_cycles(state, now, cycles_to_charge)?;
        state.total_cycles_minted += cycles_to_mint;
        Ok::<_, String>(())
    })?;

    if cycles_to_mint > Cycles::zero() {
        let _minted = ic0_mint_cycles128(cycles_to_mint);
    }
    assert!(ic_cdk::api::canister_balance128() >= cycles.get());
    Ok(())
}
```

Alternatively, document and enforce that the CMC must have a zero balance before each `ensure_balance` call, and add an assertion to that effect.

### Proof of Concept
1. Attacker legitimately converts ICP to cycles (within rate limit) and accumulates `Z` cycles in canister A.
2. Attacker calls `deposit_cycles` on the management canister targeting the CMC's principal ID, transferring `Z` cycles to the CMC. CMC's `current_balance = Z`.
3. Attacker sends `Y` ICP to the CMC's top-up subaccount for canister A and calls `notify_top_up`.
4. `process_top_up` → `deposit_cycles` → `ensure_balance(Z, BaseLimit)`.
5. `cycles_to_mint = Z - Z = 0` (saturating). Rate limiter charged 0. `total_cycles_minted += 0`.
6. `ic0_mint_cycles128(0)` — nothing minted.
7. CMC sends `Z` cycles to canister A from its pre-existing balance. Assertion passes.
8. Canister A receives `Z` cycles. Attacker has converted `Y` ICP → `Z` cycles with zero rate-limiter consumption.
9. Repeating steps 2–8 allows unlimited ICP-to-cycles conversion, bypassing the 150 petacycle/month cap. [6](#0-5) [7](#0-6) [8](#0-7)

### Citations

**File:** rs/nns/cmc/src/main.rs (L2110-2138)
```rust
async fn deposit_cycles(
    canister_id: CanisterId,
    cycles: Cycles,
    mint_cycles: bool,
    limiter_to_use: CyclesMintingLimiterSelector,
) -> Result<(), String> {
    if mint_cycles {
        ensure_balance(cycles, limiter_to_use)?;
    }

    let res: CallResult<()> = ic_cdk::api::call::call_with_payment128(
        candid::Principal::management_canister(),
        METHOD_DEPOSIT_CYCLES,
        (CanisterIdRecord {
            canister_id: canister_id.get().0,
        },),
        u128::from(cycles),
    )
    .await;

    res.map_err(|(code, msg)| {
        format!(
            "Depositing cycles failed with code {}: {:?}",
            code as i32, msg
        )
    })?;

    Ok(())
}
```

**File:** rs/nns/cmc/src/main.rs (L2140-2172)
```rust
async fn do_mint_cycles(
    account: Account,
    cycles: Cycles,
    deposit_memo: Option<Vec<u8>>,
) -> Result<CyclesLedgerDepositResult, String> {
    let Some(cycles_ledger_canister_id) = with_state(|state| state.cycles_ledger_canister_id)
    else {
        return Err("No cycles ledger canister id configured.".to_string());
    };
    // Always use base cycles limit for minting cycles, since the Subnet Rental Canister
    // doesn't call endpoints using this function.
    ensure_balance(cycles, CyclesMintingLimiterSelector::BaseLimit)?;

    let arg = CyclesLedgerDepositArgs {
        to: account,
        memo: deposit_memo,
    };

    let result: CallResult<(CyclesLedgerDepositResult,)> = ic_cdk::api::call::call_with_payment128(
        cycles_ledger_canister_id.get().0,
        "deposit",
        (arg,),
        u128::from(cycles),
    )
    .await;

    result.map(|r| r.0).map_err(|(code, msg)| {
        format!(
            "Cycles ledger rejected deposit call with code {}: {:?}",
            code as i32, msg
        )
    })
}
```

**File:** rs/nns/cmc/src/main.rs (L2306-2325)
```rust
fn ensure_balance(
    cycles: Cycles,
    limiter_to_use: CyclesMintingLimiterSelector,
) -> Result<(), String> {
    let now = now_system_time();

    let current_balance = Cycles::from(ic_cdk::api::canister_balance128());
    let cycles_to_mint = cycles - current_balance;

    with_state_mut(|state| {
        limiter_to_use.check_and_add_cycles(state, now, cycles_to_mint)?;
        state.total_cycles_minted += cycles_to_mint;
        Ok::<_, String>(())
    })?;

    // unused because of check above
    let _minted_cycles = ic0_mint_cycles128(cycles_to_mint);
    assert!(ic_cdk::api::canister_balance128() >= cycles.get());
    Ok(())
}
```

**File:** rs/cycles_account_manager/src/cycles_account_manager.rs (L1067-1069)
```rust
        // The subtraction '*cycles_balance - threshold' is saturating
        // and hence returned value will never be negative.
        let burning = min(amount_to_burn, *cycles_balance - threshold);
```

**File:** rs/nns/integration_tests/src/cycles_minting_canister.rs (L59-63)
```rust
const CYCLES_LEDGER_FEE: u128 = 100_000_000;
const CYCLES_MINTING_LIMIT: u128 = 150e15 as u128;

// per month
const SUBNET_RENTAL_CYCLES_MINTING_LIMIT: u128 = 500e15 as u128;
```

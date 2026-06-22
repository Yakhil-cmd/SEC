### Title
Minting Rate Limiter Bypass via CMC Balance Inflation in `ensure_balance` - (File: `rs/nns/cmc/src/main.rs`)

### Summary
The `ensure_balance` function in the Cycles Minting Canister (CMC) computes the amount of cycles to freshly mint using `canister_balance128()` (the live canister balance), and passes only that delta to the rate limiter. Because `Cycles::Sub` is saturating, if the CMC's balance already meets or exceeds the requested amount, `cycles_to_mint` collapses to zero and the rate limiter is checked against zero — always passing. Any unprivileged canister can inflate the CMC's balance by sending cycles to it via `deposit_cycles`, causing the rate limiter to be silently bypassed for subsequent minting operations.

### Finding Description

In `rs/nns/cmc/src/main.rs`, `ensure_balance` is the single gate that enforces the minting rate limit for all three minting paths (`notify_top_up`, `notify_create_canister`, `notify_mint_cycles`):

```rust
fn ensure_balance(
    cycles: Cycles,
    limiter_to_use: CyclesMintingLimiterSelector,
) -> Result<(), String> {
    let now = now_system_time();

    let current_balance = Cycles::from(ic_cdk::api::canister_balance128());
    let cycles_to_mint = cycles - current_balance;   // ← saturating sub

    with_state_mut(|state| {
        limiter_to_use.check_and_add_cycles(state, now, cycles_to_mint)?;  // ← checked against delta, not `cycles`
        state.total_cycles_minted += cycles_to_mint;
        Ok::<_, String>(())
    })?;

    let _minted_cycles = ic0_mint_cycles128(cycles_to_mint);
    assert!(ic_cdk::api::canister_balance128() >= cycles.get());
    Ok(())
}
``` [1](#0-0) 

The `Cycles` type implements `Sub` as saturating subtraction:

```rust
impl Sub for Cycles {
    type Output = Self;
    fn sub(self, rhs: Self) -> Self {
        Self(self.0.saturating_sub(rhs.0))
    }
}
``` [2](#0-1) 

When `current_balance >= cycles`, `cycles_to_mint` becomes `Cycles(0)`. The rate limiter `check_and_add_cycles` is called with `0`, which trivially passes regardless of how large `cycles` is. The CMC then sends the full `cycles` amount from its existing balance, and `total_cycles_minted` is not incremented.

The CMC's balance can be inflated by any canister via `deposit_cycles` on the management canister — this is a standard, unprivileged IC operation available to any canister on any subnet via XNet messaging. The CMC is a regular canister that accepts incoming cycles.

This is structurally identical to the Loop bug: just as `address(this).balance` in `PrelaunchPoints.sol` includes donated ETH and inflates `claimedAmount`, `canister_balance128()` in `ensure_balance` includes donated cycles and deflates `cycles_to_mint`, causing the rate limiter to see a falsely small (or zero) value. [3](#0-2) 

### Impact Explanation

The minting rate limiter is the primary on-chain mechanism preventing runaway ICP-to-cycles conversion that could destabilize the ICP/cycles exchange rate. By bypassing it:

1. An attacker can facilitate arbitrarily large ICP-to-cycles conversions in a single time window without triggering the limiter, for themselves or colluding parties.
2. `total_cycles_minted` is understated by the amount dispensed from the pre-loaded balance, corrupting the governance metric used to audit total cycle issuance.
3. The invariant that "cycles dispensed per period ≤ rate limit" is silently violated with no on-chain signal. [4](#0-3) 

### Likelihood Explanation

The attacker must spend cycles to pre-load the CMC — a real but bounded cost. Any canister (including one controlled by the attacker) can call `deposit_cycles` on the management canister targeting the CMC's principal. The CMC is on the NNS subnet but XNet messaging makes it reachable from any subnet. The attacker does not need privileged access, governance approval, or any special role. The attack is repeatable: once the pre-loaded balance is consumed, the attacker can top it up again.

### Recommendation

The rate limiter must be checked against `cycles` (the total amount being dispensed to the caller), not `cycles_to_mint` (the freshly minted delta). The corrected logic should be:

```rust
limiter_to_use.check_and_add_cycles(state, now, cycles)?;  // check full dispensed amount
state.total_cycles_minted += cycles_to_mint;               // only count newly minted
```

This ensures the rate limiter correctly bounds total cycle outflow per period regardless of the CMC's pre-existing balance.

### Proof of Concept

1. Attacker controls canister `A` with a large cycles balance.
2. Attacker calls `deposit_cycles` on the management canister, sending `X` cycles to the CMC (where `X` is large enough to cover anticipated user requests).
3. User calls `notify_top_up(block_index, target_canister)` having previously sent `N` ICP to the CMC's subaccount.
4. CMC calls `process_top_up` → `deposit_cycles(target, cycles, true, limiter)` → `ensure_balance(cycles, limiter)`.
5. Inside `ensure_balance`: `current_balance = canister_balance128() = X >= cycles`, so `cycles_to_mint = 0`.
6. `check_and_add_cycles(state, now, Cycles(0))` trivially passes — rate limiter is not triggered.
7. `ic0_mint_cycles128(0)` is called — no new cycles are minted from ICP.
8. CMC sends `cycles` to `target_canister` from the attacker's pre-loaded balance.
9. `total_cycles_minted` is unchanged.
10. Repeat for additional users; the rate limiter remains bypassed until the pre-loaded balance is exhausted. [5](#0-4) [6](#0-5)

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

**File:** rs/types/cycles/src/cycles.rs (L133-139)
```rust
impl Sub for Cycles {
    type Output = Self;

    fn sub(self, rhs: Self) -> Self {
        Self(self.0.saturating_sub(rhs.0))
    }
}
```

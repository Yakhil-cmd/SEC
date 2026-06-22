### Title
CMC `ensure_balance` Uses Live Canister Balance to Gate Minting Limiter, Allowing Cycle Deposit Inflation to Bypass Rate Limit and Under-Count `total_cycles_minted` - (File: rs/nns/cmc/src/main.rs)

### Summary
The `ensure_balance` function in the Cycles Minting Canister reads its own live cycle balance to compute how many cycles to mint. Because any canister can deposit cycles to the CMC via the management canister's `deposit_cycles` call, an unprivileged actor can inflate the CMC's balance before a legitimate `notify_top_up`, `notify_create_canister`, or `notify_mint_cycles` call. This causes the minting-rate limiter to be checked against a reduced (or zero) `cycles_to_mint` value, partially or fully bypassing the per-period minting cap, and causes `state.total_cycles_minted` to be under-counted by the inflated amount.

### Finding Description
In `rs/nns/cmc/src/main.rs`, `ensure_balance` is the sole gate that enforces the minting rate limit before the CMC sends cycles to a target canister or the cycles ledger:

```rust
fn ensure_balance(cycles: Cycles, limiter_to_use: CyclesMintingLimiterSelector) -> Result<(), String> {
    let now = now_system_time();
    let current_balance = Cycles::from(ic_cdk::api::canister_balance128()); // live balance read
    let cycles_to_mint = cycles - current_balance;                          // saturating sub

    with_state_mut(|state| {
        limiter_to_use.check_and_add_cycles(state, now, cycles_to_mint)?;  // limiter sees reduced amount
        state.total_cycles_minted += cycles_to_mint;                        // accounting under-counted
        Ok::<_, String>(())
    })?;

    let _minted_cycles = ic0_mint_cycles128(cycles_to_mint);
    assert!(ic_cdk::api::canister_balance128() >= cycles.get());
    Ok(())
}
```

`cycles_to_mint` is derived from `cycles - current_balance` using saturating arithmetic. If `current_balance >= cycles` (because an attacker pre-deposited that many cycles), `cycles_to_mint` saturates to zero. The limiter call `check_and_add_cycles(state, now, 0)` always succeeds, `total_cycles_minted` is incremented by zero, and `ic0_mint_cycles128(0)` mints nothing. The CMC then sends the full `cycles` amount to the target using the pre-deposited balance, with no minting-limit consumption recorded.

This is called from three paths:

- `deposit_cycles` (top-up flow) — `ensure_balance(cycles, limiter_to_use)?`
- `do_mint_cycles` (cycles-ledger mint flow) — `ensure_balance(cycles, CyclesMintingLimiterSelector::BaseLimit)?`
- `create_canister_in_subnet` — `ensure_balance(cycles, CyclesMintingLimiterSelector::BaseLimit)?` [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) 

### Impact Explanation
**Minting-rate limiter bypass**: The per-period minting cap (`CYCLES_MINTING_LIMIT` / `SUBNET_RENTAL_CYCLES_MINTING_LIMIT`) is enforced exclusively through `check_and_add_cycles(state, now, cycles_to_mint)`. When `cycles_to_mint = 0`, the limiter is never consumed. An attacker who pre-deposits enough cycles to the CMC can allow an arbitrary number of ICP-to-cycles conversions to proceed within a single rate-limit window without the limiter registering any of them.

**`total_cycles_minted` accounting corruption**: `state.total_cycles_minted` is the canonical on-chain record of how many cycles have been minted from ICP. It is exposed via the `total_cycles_minted` query endpoint and is used for governance observability. Pre-depositing cycles causes this counter to be under-counted by the deposited amount, silently corrupting the ledger of minted cycles.

**Non-ICP cycles enter the conversion accounting**: Cycles deposited directly to the CMC (not derived from ICP burning) are used to satisfy user conversion requests. The ICP is still burned, but the cycles delivered to the target were not minted from that ICP — they came from an external deposit. This is the direct IC analog of the reported issue: externally sourced tokens entering the accounting system as if they were legitimately sourced.

### Likelihood Explanation
Any canister on any subnet can call `management_canister::deposit_cycles` targeting

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

**File:** rs/nns/cmc/src/main.rs (L2245-2250)
```rust
    // We have subnets available, so we can now mint the cycles and create the canister.

    // Always use base cycles limit for minting cycles, since the Subnet Rental Canister
    // doesn't call endpoints using this function.
    ensure_balance(cycles, CyclesMintingLimiterSelector::BaseLimit)?;

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

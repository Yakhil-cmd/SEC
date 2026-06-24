### Title
CMC `ensure_balance` Rate-Limiter Bypass via External Cycle Deposits - (File: rs/nns/cmc/src/main.rs)

### Summary
The `ensure_balance` function in the Cycles Minting Canister (CMC) computes `cycles_to_mint = cycles - canister_balance128()` and charges only that delta against the rate limiter and `total_cycles_minted` counter. Because any canister can deposit cycles directly to the CMC via the management canister's `deposit_cycles` call, an attacker can pre-fund the CMC's balance, causing subsequent legitimate minting operations to record a smaller `cycles_to_mint` than the cycles actually dispensed. This lets an attacker bypass the hourly minting rate limit and causes `total_cycles_minted` to be permanently understated.

### Finding Description

`ensure_balance` is called by every minting path (`deposit_cycles`, `do_create_canister`, `do_mint_cycles`):

```rust
fn ensure_balance(
    cycles: Cycles,
    limiter_to_use: CyclesMintingLimiterSelector,
) -> Result<(), String> {
    let now = now_system_time();

    let current_balance = Cycles::from(ic_cdk::api::canister_balance128()); // reads live balance
    let cycles_to_mint = cycles - current_balance;                           // delta only

    with_state_mut(|state| {
        limiter_to_use.check_and_add_cycles(state, now, cycles_to_mint)?;   // limiter sees delta
        state.total_cycles_minted += cycles_to_mint;                        // counter sees delta
        Ok::<_, String>(())
    })?;

    let _minted_cycles = ic0_mint_cycles128(cycles_to_mint);
    assert!(ic_cdk::api::canister_balance128() >= cycles.get());
    Ok(())
}
```

The rate limiter (`base_limiter` / `subnet_rental_canister_limiter`) and the `total_cycles_minted` counter both receive `cycles_to_mint` — the *newly minted* portion — not the full `cycles` amount that is ultimately dispensed to the caller. The CMC's live balance (`canister_balance128()`) is observable and writable by any canister on any subnet via the management canister's `deposit_cycles` endpoint.

**Attack scenario:**
1. Attacker converts ICP to cycles through a normal channel, obtaining, say, 100T cycles.
2. Attacker deposits those 100T cycles directly to the CMC canister via `deposit_cycles`.
3. Attacker then sends ICP worth 150T cycles to the CMC and calls `notify_top_up`.
4. Inside `ensure_balance`: `current_balance = 100T`, `cycles_to_mint = 150T − 100T = 50T`.
5. The limiter records only 50T against the hourly cap (default 150T), not 150T.
6. The attacker receives 150T cycles but only 50T is counted against the rate limit.
7. The attacker can repeat, effectively doubling throughput relative to the limit.

Additionally, if the externally deposited balance ever equals or exceeds `cycles`, the unsigned subtraction `cycles - current_balance` underflows (wraps to a huge u128 in release builds), causing `ic0_mint_cycles128` to be called with an astronomically large argument — a potential catastrophic mint.

### Impact Explanation

- **Rate limit bypass**: The `base_cycles_limit` (150e15 cycles/hour) and `subnet_rental_cycles_limit` (500e15 cycles/month) are enforced only on the minted delta, not on the full amount dispensed. An attacker who pre-deposits cycles can extract more cycles per time window than the limit intends.
- **`total_cycles_minted` undercount**: The publicly queryable metric and the internal counter permanently understate the true number of cycles that have been dispensed, breaking auditability of ICP-to-cycles conversion.
- **Potential underflow / catastrophic mint**: If `current_balance >= cycles`, the subtraction wraps, and `ic0_mint_cycles128` is called with a near-`u128::MAX` value. On the NNS subnet the CMC is the only canister permitted to call `ic0_mint_cycles128`, so this could create cycles out of thin air at a massive scale.

### Likelihood Explanation

Any canister on any subnet can call the management canister's `deposit_cycles` to send cycles to an arbitrary canister, including the CMC. No privileged role is required. The attacker only needs ICP (to convert to cycles for the initial deposit) and a canister. The attack is fully permissionless and repeatable.

### Recommendation

Replace the delta-based accounting with full-amount accounting:

```rust
fn ensure_balance(cycles: Cycles, limiter_to_use: CyclesMintingLimiterSelector) -> Result<(), String> {
    let now = now_system_time();
    let current_balance = Cycles::from(ic_cdk::api::canister_balance128());

    // Guard against underflow
    if current_balance >= cycles {
        return Ok(()); // already funded, nothing to mint
    }
    let cycles_to_mint = cycles - current_balance;

    with_state_mut(|state| {
        // Charge the limiter and counter for the FULL cycles amount, not just the delta
        limiter_to_use.check_and_add_cycles(state, now, cycles)?;
        state.total_cycles_minted += cycles_to_mint;
        Ok::<_, String>(())
    })?;

    let _minted_cycles = ic0_mint_cycles128(cycles_to_mint);
    assert!(ic_cdk::api::canister_balance128() >= cycles.get());
    Ok(())
}
```

Alternatively, track a `pre_existing_balance` at canister initialization and subtract it from the limiter baseline, or reject any externally deposited cycles by checking that the balance has not grown unexpectedly.

### Proof of Concept

1. Deploy a canister `Attacker` on any subnet with 100T cycles.
2. `Attacker` calls `management.deposit_cycles({ canister_id: CMC_ID })` with 100T cycles attached.
3. CMC's `canister_balance128()` is now 100T.
4. `Attacker` transfers 1.5 ICP to `CMC_SUBACCOUNT(governance_canister_id)` with memo `MEMO_TOP_UP_CANISTER`.
5. `Attacker` calls `notify_top_up({ block_index, canister_id: governance_canister_id })`.
6. Inside `ensure_balance(150T, BaseLimit)`: `cycles_to_mint = 150T − 100T = 50T`. Limiter records 50T.
7. Governance canister receives 150T cycles. Only 50T is counted against the 150T/hour cap.
8. Repeat steps 2–7: each iteration deposits 100T and extracts 150T while charging only 50T to the limiter, achieving an effective rate of 150T cycles per iteration vs. the intended 150T/hour cap. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4)

### Citations

**File:** rs/nns/cmc/src/main.rs (L232-246)
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

**File:** rs/nns/cmc/src/main.rs (L2244-2249)
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

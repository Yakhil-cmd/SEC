### Title
`ensure_balance` Uses Live `canister_balance128()` Instead of Minting Parameter, Enabling Rate-Limiter Bypass via `create_canister` Cycle Residue - (File: `rs/nns/cmc/src/main.rs`)

### Summary
The `ensure_balance` function in the Cycles Minting Canister (CMC) reads the canister's live cycle balance (`canister_balance128()`) to compute how many cycles to mint, rather than always minting the full requested amount. Because the CMC's `create_canister` endpoint leaves the caller's accepted cycles in the CMC's balance after a successful call, any subsequent ICP-based conversion (`notify_top_up`, `notify_create_canister`, `notify_mint_cycles`) that calls `ensure_balance` with an amount ≤ the residual balance will mint zero cycles, record zero in `total_cycles_minted`, and charge zero against the rate limiter — while still depositing the full amount to the target canister from the pre-existing balance.

### Finding Description

`ensure_balance` is the single function that both mints cycles and enforces the minting rate limit:

```rust
fn ensure_balance(cycles: Cycles, limiter_to_use: CyclesMintingLimiterSelector) -> Result<(), String> {
    let current_balance = Cycles::from(ic_cdk::api::canister_balance128()); // live balance
    let cycles_to_mint = cycles - current_balance;                          // may be 0 or underflow

    with_state_mut(|state| {
        limiter_to_use.check_and_add_cycles(state, now, cycles_to_mint)?;  // rate-limit on delta only
        state.total_cycles_minted += cycles_to_mint;                        // accounting on delta only
        Ok::<_, String>(())
    })?;

    let _minted_cycles = ic0_mint_cycles128(cycles_to_mint);
    ...
}
``` [1](#0-0) 

The CMC's direct `create_canister` endpoint (cycles-based, not ICP-based) works as follows:

1. Reads `cycles = msg_cycles_available()` from the caller.
2. Calls `do_create_canister(caller, cycles, ...)`, which calls `ensure_balance(cycles, BaseLimit)` — minting cycles from thin air and sending them to the management canister.
3. On success, calls `msg_cycles_accept(cycles)` — accepting the caller's cycles into the CMC's own balance. [2](#0-1) 

The integration test explicitly confirms this residue:

```
// We minted, then used, then accepted some cycles.
assert_eq!(cmc_cycles_balance(), Nat::from(10_000_000_000_000_u128));
``` [3](#0-2) 

After a successful `create_canister` call for X cycles, the CMC holds X cycles in its balance. When any subsequent ICP-based call (`notify_top_up`, `notify_create_canister`, `notify_mint_cycles`) converts an ICP amount whose cycle equivalent is ≤ X, `ensure_balance` computes `cycles_to_mint = 0`, charges nothing to the rate limiter, adds nothing to `total_cycles_minted`, and deposits the full amount from the pre-existing balance. [4](#0-3) [5](#0-4) 

### Impact Explanation

**Rate-limiter bypass**: The `check_and_add_cycles` call inside `ensure_balance` only charges `cycles_to_mint` (the delta) against the per-period minting limit. An attacker who pre-funds the CMC's balance via `create_canister` can cause subsequent ICP-to-cycles conversions to consume zero rate-limit budget, allowing unlimited ICP burning within a single rate-limit window.

**`total_cycles_minted` undercount**: The governance-visible counter `total_cycles_minted` (queried via `total_cycles_minted()`) is incremented only by `cycles_to_mint`, not by the full `cycles` amount deposited. This makes the on-chain accounting of total cycles created from ICP incorrect, affecting any governance or monitoring logic that relies on it.

**ICP conservation violation**: ICP is burned via `burn_and_log` for the full `amount`, but zero new cycles are minted. The ICP supply decreases without a corresponding increase in the cycles supply, distorting the ICP/cycles exchange rate.

### Likelihood Explanation

The `create_canister` endpoint is publicly callable by any canister on the IC. Any canister with sufficient cycles can call it, leave a residue in the CMC's balance, and cause the next ICP-based conversion to bypass the rate limiter. The test at line 476 confirms this residue is a stable, observable property of the current implementation. No privileged access, governance majority, or threshold attack is required.

### Recommendation

Replace the delta-based approach in `ensure_balance` with one that always mints the full `cycles` amount and charges the full amount to the rate limiter, regardless of the CMC's current balance:

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

Alternatively, separate the `create_canister` (cycles-based) path from the ICP-based paths so that the CMC never accumulates caller-provided cycles in its own balance, and the rate limiter only tracks ICP-originated minting.

### Proof of Concept

1. Canister A calls `create_canister` on the CMC with 100 T cycles attached. The CMC mints 100 T cycles, sends them to the management canister, and accepts 100 T cycles from A. CMC balance = 100 T. Rate limiter charged: 100 T.

2. User B sends 1 ICP (= 100 T cycles at current rate) to the CMC's subaccount and calls `notify_top_up` for their canister.

3. `process_top_up` → `deposit_cycles` → `ensure_balance(100T)`:
   - `current_balance = 100T`
   - `cycles_to_mint = 100T − 100T = 0`
   - Rate limiter charged: **0**
   - `total_cycles_minted += 0`
   - CMC sends 100 T cycles from existing balance to B's canister.
   - `burn_and_log` burns 1 ICP.

4. B's canister receives 100 T cycles. 1 ICP is burned. The rate limiter was not charged. `total_cycles_minted` was not incremented. The CMC's balance is now 0.

5. Repeat from step 1 to bypass the rate limiter indefinitely, one `create_canister` call per `notify_top_up` call.

### Citations

**File:** rs/nns/cmc/src/main.rs (L1479-1515)
```rust
async fn create_canister(
    CreateCanister {
        settings,
        subnet_selection,
        subnet_type,
    }: CreateCanister,
) -> Result<CanisterId, CreateCanisterError> {
    let cycles = ic_cdk::api::call::msg_cycles_available();

    if cycles < CREATE_CANISTER_MIN_CYCLES {
        return Err(CreateCanisterError::Refunded {
            refund_amount: cycles.into(),
            create_error: "Insufficient cycles attached.".to_string(),
        });
    }
    let subnet_selection =
        get_subnet_selection(subnet_type, subnet_selection).map_err(|error_message| {
            CreateCanisterError::Refunded {
                refund_amount: cycles.into(),
                create_error: error_message,
            }
        })?;

    match do_create_canister(caller(), cycles.into(), subnet_selection, settings).await {
        Ok(canister_id) => {
            ic_cdk::api::call::msg_cycles_accept(cycles);
            Ok(canister_id)
        }
        Err(create_error) => {
            ic_cdk::api::call::msg_cycles_accept(BAD_REQUEST_CYCLES_PENALTY as u64);
            let refund_amount = ic_cdk::api::call::msg_cycles_available();
            Err(CreateCanisterError::Refunded {
                refund_amount: refund_amount.into(),
                create_error,
            })
        }
    }
```

**File:** rs/nns/cmc/src/main.rs (L1985-2011)
```rust
async fn process_top_up(
    canister_id: CanisterId,
    from: AccountIdentifier,
    amount: Tokens,
    limiter_to_use: CyclesMintingLimiterSelector,
) -> Result<Cycles, NotifyError> {
    let cycles = tokens_to_cycles(amount)?;

    let sub = Subaccount::from(&canister_id);

    print(format!(
        "Topping up canister {canister_id} by {cycles} cycles."
    ));

    match deposit_cycles(canister_id, cycles, true, limiter_to_use).await {
        Ok(()) => {
            burn_and_log(sub, amount).await;
            Ok(cycles)
        }
        Err(err) => {
            let refund_block = refund_icp(sub, from, amount, TOP_UP_CANISTER_REFUND_FEE).await?;
            Err(NotifyError::Refunded {
                reason: err.to_string(),
                block_index: refund_block,
            })
        }
    }
```

**File:** rs/nns/cmc/src/main.rs (L2245-2249)
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

**File:** rs/nns/integration_tests/src/cycles_minting_canister.rs (L475-476)
```rust
    // We minted, then used, then accepted some cycles.
    assert_eq!(cmc_cycles_balance(), Nat::from(10_000_000_000_000_u128));
```

### Title
CMC `ensure_balance` Arithmetic Underflow via Unprivileged Cycle Deposit Causes DoS - (File: rs/nns/cmc/src/main.rs)

### Summary
The Cycles Minting Canister's `ensure_balance` function computes `cycles_to_mint = cycles - current_balance` without first checking whether the CMC's current balance already meets or exceeds the required amount. Any unprivileged canister can call the management canister's `deposit_cycles` to force cycles into the CMC, inflating `current_balance` above `cycles`. This triggers an arithmetic underflow, causing every subsequent call to `notify_top_up`, `notify_create_canister`, and `notify_mint_cycles` to fail or trap, effectively DoS-ing the CMC.

### Finding Description

The `ensure_balance` function is the gating step before the CMC deposits cycles to a target canister or the cycles ledger:

```rust
fn ensure_balance(
    cycles: Cycles,
    limiter_to_use: CyclesMintingLimiterSelector,
) -> Result<(), String> {
    let now = now_system_time();

    let current_balance = Cycles::from(ic_cdk::api::canister_balance128());
    let cycles_to_mint = cycles - current_balance;   // ← underflows when current_balance > cycles

    with_state_mut(|state| {
        limiter_to_use.check_and_add_cycles(state, now, cycles_to_mint)?;
        state.total_cycles_minted += cycles_to_mint;
        Ok::<_, String>(())
    })?;

    let _minted_cycles = ic0_mint_cycles128(cycles_to_mint);
    assert!(ic_cdk::api::canister_balance128() >= cycles.get());
    Ok(())
}
``` [1](#0-0) 

The function assumes `current_balance < cycles` at all times. There is no guard of the form `if current_balance >= cycles { return Ok(()); }`. The CMC's cycles balance is a standard canister balance that any other canister can increase by calling the management canister's `deposit_cycles` method: [2](#0-1) 

`ensure_balance` is called (with `mint_cycles = true`) from `deposit_cycles`, which is itself called from `process_top_up` and the canister-creation path: [3](#0-2) 

It is also called directly from `do_mint_cycles`: [4](#0-3) 

When `current_balance > cycles`, the subtraction `cycles - current_balance` underflows. In a Rust release build the result wraps to an astronomically large `u128` value. That value is then passed to `check_and_add_cycles`: [5](#0-4) 

Because `cycles_to_mint` is near `u128::MAX`, the addition `count + cycles_to_mint` itself overflows. Depending on whether `Cycles` arithmetic is wrapping or checked:

- **Checked / panicking**: the canister traps on the subtraction or the addition, rolling back the message. Every subsequent call to `notify_top_up` / `notify_create_canister` / `notify_mint_cycles` traps identically → persistent DoS.
- **Wrapping**: `count + cycles_to_mint` wraps to a small value that may pass the rate-limit check, after which `ic0_mint_cycles128` is called with a near-`u128::MAX` argument. The IC runtime will either trap (DoS) or mint an unbounded number of cycles (supply inflation).

Either outcome is severe. The `total_cycles_minted` accounting variable is also corrupted: [6](#0-5) 

### Impact Explanation

The CMC is the sole on-chain gateway for converting ICP to cycles, creating canisters via ICP, and minting cycles to the cycles ledger. A persistent failure of `ensure_balance` blocks all three user-facing endpoints (`notify_top_up`, `notify_create_canister`, `notify_mint_cycles`) for every user on the IC. No canister can be created or topped up via the normal ICP→cycles path until the CMC's balance is drained back below the threshold or the canister is upgraded with a fix. This constitutes a protocol-level DoS of a critical NNS system canister.

### Likelihood Explanation

The attack requires no privileged access. Any canister holding sufficient cycles can call `ic00::deposit_cycles` targeting the CMC's canister ID. The CMC's

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

**File:** rs/nns/cmc/src/main.rs (L2140-2152)
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

**File:** rs/nns/cmc/src/limiter.rs (L34-56)
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
    }
```

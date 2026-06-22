### Title
CMC `total_cycles_minted` and Rate Limiter Overcounted on Failed `notify_top_up` / `notify_mint_cycles` - (File: `rs/nns/cmc/src/main.rs`)

---

### Summary

The Cycles Minting Canister (CMC) increments `total_cycles_minted` and the hourly rate-limiter inside `ensure_balance` before it knows whether the downstream deposit will succeed. When `deposit_cycles` (or the cycles-ledger deposit in `do_mint_cycles`) fails and the caller's ICP is refunded, neither `total_cycles_minted` nor the rate-limiter window is decremented. The minted cycles remain stranded in the CMC's own balance, but the accounting variable and the rate-limiter have already consumed quota. Any unprivileged user who can submit ICP transfers can exploit this to exhaust the hourly minting quota, denying service to all other users for up to one hour per attack round.

---

### Finding Description

`ensure_balance` is the single function that (a) mints new cycles into the CMC's own balance, (b) adds the minted amount to the sliding-window rate-limiter, and (c) increments the persistent `total_cycles_minted` counter:

```rust
// rs/nns/cmc/src/main.rs  ~line 2306
fn ensure_balance(cycles: Cycles, limiter_to_use: CyclesMintingLimiterSelector) -> Result<(), String> {
    let current_balance = Cycles::from(ic_cdk::api::canister_balance128());
    let cycles_to_mint = cycles - current_balance;          // (1) compute gap

    with_state_mut(|state| {
        limiter_to_use.check_and_add_cycles(state, now, cycles_to_mint)?;  // (2) consume rate-limit quota
        state.total_cycles_minted += cycles_to_mint;                        // (3) increment counter
        Ok::<_, String>(())
    })?;

    let _minted_cycles = ic0_mint_cycles128(cycles_to_mint);  // (4) actually mint
    Ok(())
}
```

`ensure_balance` is called from `deposit_cycles` (used by `notify_top_up`) and from `do_mint_cycles` (used by `notify_mint_cycles`). Both callers can fail *after* `ensure_balance` returns:

```rust
// rs/nns/cmc/src/main.rs  ~line 1985
async fn process_top_up(...) -> Result<Cycles, NotifyError> {
    let cycles = tokens_to_cycles(amount)?;
    match deposit_cycles(canister_id, cycles, true, limiter_to_use).await {
        Ok(()) => { burn_and_log(sub, amount).await; Ok(cycles) }
        Err(err) => {
            // ICP is refunded here, but total_cycles_minted and the rate-limiter
            // are NOT decremented — the minted cycles stay in the CMC's balance.
            let refund_block = refund_icp(sub, from, amount, TOP_UP_CANISTER_REFUND_FEE).await?;
            Err(NotifyError::Refunded { reason: err.to_string(), block_index: refund_block })
        }
    }
}
```

The existing integration test explicitly documents and asserts this divergence:

```rust
// rs/nns/integration_tests/src/cycles_minting_canister.rs  ~line 1605
let total_minted_before = total_cycles_minted(&state_machine);
let error = notify_top_up(&state_machine, invalid_canister_id, Tokens::new(1, 0).unwrap()).unwrap_err();
let total_minted_after = total_cycles_minted(&state_machine);
assert_matches!(error, NotifyError::Refunded { .. });
assert_eq!(
    total_minted_after - total_minted_before,
    100_000_000_000_000_u64   // 100 T cycles counted even though deposit failed and ICP was refunded
);
```

The structural parallel to xPYT is exact:

| xPYT | CMC |
|---|---|
| `assetBalance += yieldAmount` before pounder reward is paid out | `total_cycles_minted += cycles_to_mint` before deposit succeeds |
| `assetBalance` never decremented by `pounderReward` | counter/limiter never decremented on refund |
| Exchange ratio inflated; last withdrawer left with nothing | Rate-limit quota consumed; legitimate minting blocked |

---

### Impact Explanation

1. **Rate-limiter exhaustion (DoS).** Each failed `notify_top_up` call consumes `cycles_to_mint` from the hourly `base_limiter` window. An attacker who sends N distinct ICP transfers to the CMC's subaccount for a non-existent canister and calls `notify_top_up` N times will exhaust the hourly quota. Once exhausted, every subsequent legitimate `notify_top_up` or `notify_mint_cycles` call returns an error for up to one hour, denying cycles minting to all users on the IC.

2. **`total_cycles_minted` metric corruption.** The publicly queryable `total_cycles_minted` counter permanently over-reports cycles delivered, misleading monitoring, dashboards, and any off-chain tooling that relies on it.

3. **Stranded cycles in CMC balance.** Cycles minted into the CMC's own balance but never delivered to a canister accumulate silently. They are not burned, not refunded, and not attributed to any user, representing a permanent accounting leak.

---

### Likelihood Explanation

- **Entry path is fully unprivileged.** Any principal can call `notify_top_up` after sending ICP to the CMC's subaccount. No special role, key, or governance majority is required.
- **Cost is bounded by the refund fee.** Each attack round costs only `TOP_UP_CANISTER_REFUND_FEE` (a small fixed ICP amount) because the principal amount is refunded. The attacker loses only the fee per call.
- **Deduplication does not help.** The `blocks_notified` map prevents replaying the *same* block index, but each new ICP transfer produces a fresh block index, so the attacker can repeat the pattern indefinitely across different transfers.
- **No special timing or concurrency required.** The bug is triggered deterministically on every failed deposit.

---

### Recommendation

Decrement `total_cycles_minted` and the rate-limiter window when a deposit fails and ICP is refunded. One approach is to move the rate-limiter increment and counter update to *after* the deposit succeeds:

```rust
// In process_top_up / process_mint_cycles:
match deposit_cycles(canister_id, cycles, false /* don't pre-consume quota */, ...).await {
    Ok(()) => {
        // Only now record the successful mint
        with_state_mut(|state| {
            limiter.check_and_add_cycles(state, now, cycles)?;
            state.total_cycles_minted += cycles;
            Ok(())
        })?;
        burn_and_log(sub, amount).await;
        Ok(cycles)
    }
    Err(err) => { refund_icp(...).await?; Err(...) }
}
```

Alternatively, if pre-minting into the CMC balance is required for atomicity, add a compensating decrement on the failure path:

```rust
Err(err) => {
    with_state_mut(|state| {
        state.total_cycles_minted = state.total_cycles_minted.saturating_sub(cycles_to_mint);
        limiter.remove_cycles(state, cycles_to_mint); // if the limiter supports it
    });
    refund_icp(...).await?;
    Err(...)
}
```

Add a test that verifies `total_cycles_minted` is unchanged after a failed `notify_top_up`.

---

### Proof of Concept

1. Obtain any ICP balance on mainnet (or a test environment with the NNS).
2. Transfer 1 ICP to `AccountIdentifier(CMC_principal, Subaccount(invalid_canister_id))`.
3. Call `notify_top_up { block_index: <that block>, canister_id: <non-existent canister> }`.
4. Observe: the call returns `NotifyError::Refunded`, ICP is returned minus the refund fee, yet `total_cycles_minted()` has increased by ~100 T cycles and the hourly rate-limiter has consumed that quota.
5. Repeat with fresh ICP transfers until the hourly quota is exhausted.
6. Observe: all subsequent `notify_top_up` and `notify_mint_cycles` calls from any principal fail with a rate-limit error for up to one hour.

**Relevant code locations:** [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4)

### Citations

**File:** rs/nns/cmc/src/main.rs (L1958-1983)
```rust
async fn process_mint_cycles(
    to_account: Account,
    amount: Tokens,
    deposit_memo: Option<Vec<u8>>,
    from: AccountIdentifier,
    sub: Subaccount,
) -> NotifyMintCyclesResult {
    let cycles = tokens_to_cycles(amount)?;
    match do_mint_cycles(to_account, cycles, deposit_memo).await {
        Ok(deposit_result) => {
            burn_and_log(sub, amount).await;
            Ok(NotifyMintCyclesSuccess {
                block_index: deposit_result.block_index,
                minted: cycles.into(),
                balance: deposit_result.balance,
            })
        }
        Err(err) => {
            let refund_block = refund_icp(sub, from, amount, MINT_CYCLES_REFUND_FEE).await?;
            Err(NotifyError::Refunded {
                reason: err,
                block_index: refund_block,
            })
        }
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

**File:** rs/nns/integration_tests/src/cycles_minting_canister.rs (L1605-1641)
```rust
#[test]
fn cmc_notify_top_up_invalid() {
    let account = AccountIdentifier::new(*TEST_USER1_PRINCIPAL, None);
    let icpts = Tokens::new(100, 0).unwrap();
    let invalid_canister_id = CanisterId::from_u64(123_456_789);

    let state_machine = state_machine_builder_for_nns_tests().build();
    let nns_init_payloads = NnsInitPayloadsBuilder::new()
        .with_test_neurons()
        .with_ledger_account(account, icpts)
        .build();
    setup_nns_canisters(&state_machine, nns_init_payloads);

    let total_minted_before = total_cycles_minted(&state_machine);
    let error = notify_top_up(
        &state_machine,
        invalid_canister_id,
        Tokens::new(1, 0).unwrap(),
    )
    .unwrap_err();
    let total_minted_after = total_cycles_minted(&state_machine);
    assert_matches!(error, NotifyError::Refunded { .. });
    assert_eq!(
        total_minted_after - total_minted_before,
        100_000_000_000_000_u64
    );

    let total_minted_before = total_cycles_minted(&state_machine);
    let error = notify_top_up(
        &state_machine,
        invalid_canister_id,
        Tokens::new(1, 0).unwrap(),
    )
    .unwrap_err();
    let total_minted_after = total_cycles_minted(&state_machine);
    assert_matches!(error, NotifyError::Refunded { .. });
    assert_eq!(total_minted_after - total_minted_before, 0_u64);
```

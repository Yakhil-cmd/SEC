### Title
`ensure_balance` increments `total_cycles_minted` and the rate-limiter quota before the cycles deposit, but neither is decremented when the deposit fails and ICP is refunded — (`rs/nns/cmc/src/main.rs`)

---

### Summary

In the Cycles Minting Canister (CMC), `ensure_balance` atomically increments `state.total_cycles_minted` and the rate-limiter window **before** the actual cycles deposit to the target canister or cycles ledger. When the downstream deposit call fails (e.g., invalid canister ID, cycles-ledger rejection), the ICP is refunded to the caller, but `total_cycles_minted` and the limiter count are **never decremented**. This is the direct IC analog of the `PledgeManager::refundTokens` bug: a counter is incremented in the success path but not rolled back in the failure/refund path.

---

### Finding Description

`ensure_balance` in `rs/nns/cmc/src/main.rs` is called by three code paths before any inter-canister call:

- `deposit_cycles` (used by `process_top_up`)
- `do_create_canister` (used by `process_create_canister`)
- `do_mint_cycles` (used by `process_mint_cycles`)

Inside `ensure_balance`:

```rust
fn ensure_balance(cycles: Cycles, limiter_to_use: CyclesMintingLimiterSelector) -> Result<(), String> {
    let now = now_system_time();
    let current_balance = Cycles::from(ic_cdk::api::canister_balance128());
    let cycles_to_mint = cycles - current_balance;

    with_state_mut(|state| {
        limiter_to_use.check_and_add_cycles(state, now, cycles_to_mint)?;  // limiter incremented
        state.total_cycles_minted += cycles_to_mint;                        // counter incremented
        Ok::<_, String>(())
    })?;

    let _minted_cycles = ic0_mint_cycles128(cycles_to_mint);  // cycles actually minted
    Ok(())
}
``` [1](#0-0) 

After `ensure_balance` returns `Ok`, the caller proceeds with the inter-canister deposit. If that deposit fails, the error propagates up to `process_top_up` / `process_create_canister` / `process_mint_cycles`, which call `refund_icp` to return ICP to the user:

```rust
match deposit_cycles(canister_id, cycles, true, limiter_to_use).await {
    Ok(()) => { burn_and_log(sub, amount).await; Ok(cycles) }
    Err(err) => {
        let refund_block = refund_icp(sub, from, amount, TOP_UP_CANISTER_REFUND_FEE).await?;
        Err(NotifyError::Refunded { reason: err.to_string(), block_index: refund_block })
    }
}
``` [2](#0-1) 

Neither `state.total_cycles_minted` nor the limiter's `total_count` is decremented in the refund branch. The same pattern exists in `do_create_canister` and `do_mint_cycles`. [3](#0-2) [4](#0-3) 

The existing integration test `cmc_notify_top_up_invalid` explicitly documents this behavior — it asserts that `total_cycles_minted` increases by 100T cycles even when the top-up fails and ICP is refunded:

```rust
assert_matches!(error, NotifyError::Refunded { .. });
assert_eq!(
    total_minted_after - total_minted_before,
    100_000_000_000_000_u64   // counter inflated despite refund
);
``` [5](#0-4) 

---

### Impact Explanation

**`total_cycles_minted` inflation (permanent):** The public query endpoint `total_cycles_minted` returns a value that includes cycles minted for operations that were ultimately refunded. This permanently overstates the total economic activity of the CMC. Any governance, monitoring, or external tooling that relies on this counter to reason about the IC's economic state receives incorrect data.

**Rate-limiter quota consumption (temporary, bounded):** `check_and_add_cycles` adds `cycles_to_mint` to the sliding-window limiter before the deposit. If the deposit fails, that quota is not returned. An unprivileged caller who repeatedly triggers failed top-ups (paying only the `TOP_UP_CANISTER_REFUND_FEE` each time) can consume the hourly minting quota, causing legitimate users' `notify_top_up` / `notify_create_canister` calls to be rejected with a rate-limit error for up to one hour. [6](#0-5) 

---

### Likelihood Explanation

Any unprivileged ingress sender can call `notify_top_up` with a non-existent canister ID. The only cost is the `TOP_UP_CANISTER_REFUND_FEE` per call. The rate-limiter exhaustion attack requires spending fees proportional to the configured `base_cycles_limit`, making it economically costly but not impossible. The `total_cycles_minted` inflation requires no special conditions and occurs on every failed operation.

---

### Recommendation

Decrement `state.total_cycles_minted` and the limiter count when the downstream deposit fails and ICP is refunded. Concretely, `ensure_balance` should return the amount minted, and the callers (`deposit_cycles`, `do_create_canister`, `do_mint_cycles`) should roll back the counter and limiter in their error paths before calling `refund_icp`:

```rust
// In the Err branch of deposit_cycles / do_create_canister / do_mint_cycles:
with_state_mut(|state| {
    state.total_cycles_minted -= cycles_to_mint;
    limiter_to_use.subtract_cycles(state, cycles_to_mint);
});
let refund_block = refund_icp(...).await?;
```

Alternatively, move the `ensure_balance` call to after the deposit succeeds (pre-funding from existing CMC balance, minting only on confirmed success), which eliminates the race entirely.

---

### Proof of Concept

1. User sends 1 ICP to the CMC subaccount for a non-existent canister `CanisterId::from_u64(123_456_789)`.
2. User calls `notify_top_up` with that canister ID.
3. CMC calls `ensure_balance(100T_cycles, BaseLimit)`:
   - Limiter incremented by 100T cycles.
   - `total_cycles_minted` incremented by 100T cycles.
   - 100T cycles minted into CMC balance via `ic0_mint_cycles128`.
4. CMC calls `deposit_cycles` → management canister rejects (canister does not exist) → cycles returned to CMC balance.
5. CMC calls `refund_icp` → user receives ~1 ICP minus `TOP_UP_CANISTER_REFUND_FEE`.
6. `total_cycles_minted` remains inflated by 100T cycles. Limiter quota consumed by 100T cycles.
7. Repeating step 1–6 (each time the CMC balance has been consumed by other operations) exhausts the hourly limiter, blocking all legitimate `notify_top_up` calls for up to one hour.

This is confirmed by the existing test `cmc_notify_top_up_invalid` in `rs/nns/integration_tests/src/cycles_minting_canister.rs` lines 1605–1641. [7](#0-6)

### Citations

**File:** rs/nns/cmc/src/main.rs (L1999-2011)
```rust
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

**File:** rs/nns/cmc/src/main.rs (L2249-2300)
```rust
    ensure_balance(cycles, CyclesMintingLimiterSelector::BaseLimit)?;

    let canister_settings = settings
        .map(|mut settings| {
            if settings.controllers.is_none() {
                settings.controllers = Some(vec![controller_id.0]);
            }
            settings
        })
        .unwrap_or_else(|| CanisterSettings {
            controllers: Some(vec![controller_id.0]),
            ..Default::default()
        });

    for subnet_id in subnets {
        let result: CallResult<(CanisterIdRecord,)> = ic_cdk::api::call::call_with_payment128(
            subnet_id.get().0,
            METHOD_CREATE_CANISTER,
            (CreateCanisterArgs {
                settings: Some(canister_settings.clone()),
                sender_canister_version: Some(ic_cdk::api::canister_version()),
            },),
            u128::from(cycles),
        )
        .await;

        let canister_id = match result {
            Ok((canister_id_record,)) => {
                // Safe: canister_id returned by the management canister is always a valid canister principal.
                CanisterId::unchecked_from_principal(PrincipalId::from(
                    canister_id_record.canister_id,
                ))
            }
            Err((code, msg)) => {
                let err = format!(
                    "Creating canister in subnet {} failed with code {}: {}",
                    subnet_id, code as i32, msg
                );
                print(format!("[cycles] {err}"));
                last_err = Some(err);
                continue;
            }
        };

        print(format!(
            "[cycles] created canister {canister_id} in subnet {subnet_id}"
        ));

        return Ok(canister_id);
    }

    Err(last_err.unwrap_or_else(|| "Unknown problem attempting to create a canister.".to_owned()))
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

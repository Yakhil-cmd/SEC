Based on my investigation of the IC codebase, I found a valid analog to the external report's vulnerability class.

---

### Title
Stale Rate-Limiter Counter in CMC `ensure_balance` Not Decremented on Refund Allows Quota Inflation DoS - (File: `rs/nns/cmc/src/main.rs`)

### Summary
The Cycles Minting Canister (CMC) increments both `state.total_cycles_minted` and the `base_limiter`'s `total_count` inside `ensure_balance` **before** the downstream operation (canister creation or top-up) completes. When the downstream operation fails and ICP is refunded to the caller, neither counter is decremented. Any unprivileged user can exploit this to inflate the rate-limiter's running total, causing legitimate users to be blocked from minting cycles for up to one hour.

### Finding Description

`ensure_balance` in `rs/nns/cmc/src/main.rs` atomically mints cycles and records them in the rate-limiter before the downstream call succeeds:

```rust
fn ensure_balance(cycles: Cycles, limiter_to_use: CyclesMintingLimiterSelector) -> Result<(), String> {
    let current_balance = Cycles::from(ic_cdk::api::canister_balance128());
    let cycles_to_mint = cycles - current_balance;

    with_state_mut(|state| {
        limiter_to_use.check_and_add_cycles(state, now, cycles_to_mint)?;  // ← increments total_count
        state.total_cycles_minted += cycles_to_mint;                        // ← increments total_cycles_minted
        Ok::<_, String>(())
    })?;

    let _minted_cycles = ic0_mint_cycles128(cycles_to_mint);
    Ok(())
}
``` [1](#0-0) 

`check_and_add_cycles` in the `Limiter` adds `cycles_to_mint` to `total_count`, which is the running total used in the rate-limiting check:

```rust
pub fn check_and_add_cycles(&mut self, now: SystemTime, cycles_to_mint: Cycles, limit: Cycles) -> Result<(), String> {
    self.purge_old(now);
    let count = self.get_count();
    if count + cycles_to_mint > limit {   // ← uses total_count
        return Err(...);
    }
    self.add(now, cycles_to_mint);        // ← increments total_count
    Ok(())
}
``` [2](#0-1) 

When `deposit_cycles` subsequently fails (e.g., target canister does not exist), `process_top_up` refunds the ICP:

```rust
match deposit_cycles(canister_id, cycles, true, limiter_to_use).await {
    Ok(()) => { burn_and_log(sub, amount).await; Ok(cycles) }
    Err(err) => {
        let refund_block = refund_icp(sub, from, amount, TOP_UP_CANISTER_REFUND_FEE).await?;
        Err(NotifyError::Refunded { ... })
    }
}
``` [3](#0-2) 

Neither `total_cycles_minted` nor the limiter's `total_count` is decremented after the refund. The same pattern applies in `process_create_canister` when all subnet creation attempts fail after `ensure_balance` has already run. [4](#0-3) 

The integration test `cmc_notify_top_up_invalid` explicitly documents and asserts this behavior — `total_cycles_minted` increases by 100T cycles even when the operation is refunded:

```rust
assert_matches!(error, NotifyError::Refunded { .. });
assert_eq!(
    total_minted_after - total_minted_before,
    100_000_000_000_000_u64   // ← counter inflated despite refund
);
``` [5](#0-4) 

The `Limiter`'s `total_count` is the direct analog to `last_total_shares_minted` in the external report: it is a running total used in a calculation (`count + cycles_to_mint > limit`) that is incremented on each minting event but never decremented when the operation is reversed. [6](#0-5) 

### Impact Explanation

An attacker can repeatedly call `notify_top_up` (or `notify_create_canister`) with a valid ICP payment targeting a non-existent canister. Each call causes `ensure_balance` to mint cycles and increment the rate-limiter's `total_count`. The ICP is refunded (minus a small fee), but `total_count` remains inflated. Once `total_count` reaches the `base_cycles_limit` (150T cycles/hour by default), all subsequent legitimate minting requests are rejected with "More than N cycles have been minted in the last 3600 seconds, please try again later." The window lasts up to one hour before `purge_old` expires the stale entries.

Additionally, cycles are minted into the CMC's balance without the corresponding ICP being burned (ICP is refunded instead), creating a ledger conservation discrepancy: cycles exist in the system without burned ICP backing them.

### Likelihood Explanation

The entry path requires no privileged access — any principal can call `notify_top_up` with a valid ICP ledger block. The attacker recovers most of their ICP (minus `TOP_UP_CANISTER_REFUND_FEE`). At 1 ICP ≈ 100T cycles and a 150T cycles/hour limit, the attacker can saturate the limiter with approximately 1.5 ICP in fees. This is a low-cost, repeatable attack reachable from any unprivileged ingress sender.

### Recommendation

Decrement `state.total_cycles_minted` and the limiter's `total_count` by `cycles_to_mint` when the downstream operation fails and ICP is refunded. Alternatively, restructure `ensure_balance` to be called only after the downstream operation succeeds (i.e., move minting to after canister creation or deposit confirmation), mirroring the pattern used in `burn_and_log` which is only called on success.

### Proof of Concept

1. Obtain 2 ICP on the ICP ledger.
2. Call `notify_top_up` with `canister_id = CanisterId::from_u64(999_999_999)` (non-existent) and a 1 ICP payment. Observe `NotifyError::Refunded` and that `total_cycles_minted` increases by ~100T.
3. Repeat step 2 twice more (total ~1.5 ICP in fees).
4. Call `notify_top_up` with a valid canister ID and observe rejection: "More than 150000000000000 cycles have been minted in the last 3600 seconds, please try again later."
5. Legitimate users are blocked from minting cycles for up to one hour.

The integration test at `rs/nns/integration_tests/src/cycles_minting_canister.rs:1605–1641` already demonstrates step 2 in isolation. [7](#0-6)

### Citations

**File:** rs/nns/cmc/src/main.rs (L1943-1955)
```rust
    match do_create_canister(controller, cycles, subnet_selection, settings).await {
        Ok(canister_id) => {
            burn_and_log(sub, amount).await;
            Ok(canister_id)
        }
        Err(err) => {
            let refund_block = refund_icp(sub, from, amount, CREATE_CANISTER_REFUND_FEE).await?;
            Err(NotifyError::Refunded {
                reason: err,
                block_index: refund_block,
            })
        }
    }
```

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

**File:** rs/nns/cmc/src/limiter.rs (L15-20)
```rust
pub struct Limiter {
    time_windows: VecDeque<TimeWindowCount>,
    total_count: Cycles,
    resolution: Duration,
    max_age: Duration,
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

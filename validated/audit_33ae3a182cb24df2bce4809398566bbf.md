I have all the code needed to trace this vulnerability precisely. Let me analyze the execution flow.

## Execution Flow Trace

**`notify_top_up`** → **`process_top_up`** → **`deposit_cycles`** → **`ensure_balance`** (synchronous) → management canister `deposit_cycles` call (async, can fail)

The critical ordering in `deposit_cycles`: [1](#0-0) 

```
ensure_balance(cycles, limiter_to_use)?;   // limiter charged + ic0_mint_cycles128 called
...
call_with_payment128(..., METHOD_DEPOSIT_CYCLES, ...).await;  // can fail AFTER limiter is charged
```

`ensure_balance` is **synchronous** — it charges the limiter and mints cycles into the CMC's own balance atomically before any await point: [2](#0-1) 

The limiter is incremented at line 2316, `ic0_mint_cycles128` is called at line 2322, and only then does `deposit_cycles` proceed to the async management canister call. If that call fails, the cycles are returned to the CMC (IC semantics: rejected calls return attached cycles), but **the limiter is not rolled back**.

`process_top_up` then refunds the ICP on failure: [3](#0-2) 

---

### Title
Rate-Limit Consumed Without Cycle Delivery Enables Global Minting DoS — (`rs/nns/cmc/src/main.rs`, `deposit_cycles` / `ensure_balance`)

### Summary
`ensure_balance` charges `base_limiter` and calls `ic0_mint_cycles128` before the management canister `deposit_cycles` call. If that call fails (e.g., targeting a non-existent canister), the rate limit is permanently consumed for the current window while the ICP is refunded to the attacker. A single ~1500 ICP transaction (refunded minus fees) exhausts the 150P/hour global limit, blocking all legitimate minting for up to one hour.

### Finding Description

In `deposit_cycles` [4](#0-3) , the call to `ensure_balance` at line 2117 is synchronous and unconditional. Inside `ensure_balance` [5](#0-4) , `check_and_add_cycles` permanently records the minting event in the limiter's `time_windows` deque and increments `total_count` [6](#0-5) , then `ic0_mint_cycles128` mints the cycles into the CMC's own balance. There is no compensating decrement if the subsequent `call_with_payment128` to the management canister fails.

When the management canister rejects `deposit_cycles` (e.g., because the target canister ID does not exist), the IC returns the attached cycles to the CMC, but `base_limiter.total_count` retains the charged amount. `process_top_up` then calls `refund_icp`, returning the ICP to the attacker minus only `DEFAULT_TRANSFER_FEE` + `TOP_UP_CANISTER_REFUND_FEE` (a few ten-thousandths of an ICP). [7](#0-6) 

### Impact Explanation

- The 150P/hour `base_cycles_limit` is exhausted in a single `notify_top_up` call. [8](#0-7) 
- All subsequent legitimate `notify_top_up`, `notify_create_canister`, and `notify_mint_cycles` calls that go through `ensure_balance` with `BaseLimit` will be rejected with "try again later" for up to one hour. [9](#0-8) 
- The attack is repeatable each hour. The attacker's net cost per cycle is only the two ledger transfer fees (~0.0002 ICP total), not the ~1500 ICP principal (which is refunded).

**Partial self-limiting factor**: After the first attack, the CMC retains the 150P minted cycles in its own balance. On the next attack, `cycles_to_mint = cycles - current_balance` [10](#0-9)  would be reduced or zero if the CMC balance hasn't been drained by legitimate operations, limiting repeated attacks. However, the first-hour DoS is fully achievable, and subsequent hours become viable once legitimate top-ups drain the CMC's balance.

### Likelihood Explanation

- Requires no privileged access — any principal can call `notify_top_up`.
- Requires ~1500 ICP upfront (refunded), accessible to any well-funded attacker.
- A non-existent canister ID is trivially constructable.
- The `blocks_notified` deduplication prevents reuse of the same block index, but each new ICP transfer creates a new block index. [11](#0-10) 

### Recommendation

Move `ensure_balance` (limiter charge + `ic0_mint_cycles128`) to **after** the management canister call succeeds, or implement a rollback: if `call_with_payment128` returns an error, decrement `base_limiter` by the previously charged amount and subtract from `total_cycles_minted`. The invariant must be: the limiter is only charged when cycles are verifiably delivered.

### Proof of Concept

State-machine test sketch:
1. Configure CMC with a mock management canister that always rejects `deposit_cycles`.
2. Send 1500 ICP to CMC subaccount for a target canister ID.
3. Call `notify_top_up` with that block index.
4. Assert: `base_limiter.get_count()` equals 150P (limit exhausted). [12](#0-11) 
5. Assert: attacker's ICP balance is restored minus ~0.0002 ICP in fees.
6. Assert: a legitimate `notify_top_up` from a different user now returns `NotifyError::Refunded` with "try again later".

The existing test `cmc_notify_top_up_rate_limited` confirms the limiter works correctly for successful deposits [13](#0-12)  but does not test the failed-delivery path, confirming the gap.

### Citations

**File:** rs/nns/cmc/src/main.rs (L83-83)
```rust
const DEFAULT_CYCLES_LIMIT: u128 = 150e15 as u128;
```

**File:** rs/nns/cmc/src/main.rs (L1181-1206)
```rust
        match state.blocks_notified.entry(block_index) {
            Entry::Occupied(entry) => match entry.get() {
                NotificationStatus::Processing => Some(Err(NotifyError::Processing)),

                // If the user makes a duplicate request, we respond as though
                // the current request is the original one.
                NotificationStatus::NotifiedTopUp(result) => Some(result.clone()),
                NotificationStatus::NotifiedCreateCanister(_) => {
                    Some(Err(NotifyError::InvalidTransaction(
                        "The same payment is already processed as create canister request".into(),
                    )))
                }
                NotificationStatus::NotifiedMint(_) => Some(Err(NotifyError::InvalidTransaction(
                    "The same payment is already processed as mint request".into(),
                ))),
                NotificationStatus::NotMeaningfulMemo(_) => {
                    Some(Err(NotifyError::InvalidTransaction(
                        "The same payment is already processed as automatic refund".into(),
                    )))
                }
            },
            Entry::Vacant(entry) => {
                entry.insert(NotificationStatus::Processing);
                None
            }
        }
```

**File:** rs/nns/cmc/src/main.rs (L2004-2010)
```rust
        Err(err) => {
            let refund_block = refund_icp(sub, from, amount, TOP_UP_CANISTER_REFUND_FEE).await?;
            Err(NotifyError::Refunded {
                reason: err.to_string(),
                block_index: refund_block,
            })
        }
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

**File:** rs/nns/cmc/src/limiter.rs (L43-53)
```rust
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
```

**File:** rs/nns/cmc/src/limiter.rs (L54-55)
```rust
        self.add(now, cycles_to_mint);
        Ok(())
```

**File:** rs/nns/cmc/src/limiter.rs (L106-108)
```rust
    pub fn get_count(&self) -> Cycles {
        self.total_count
    }
```

**File:** rs/nns/integration_tests/src/cycles_minting_canister.rs (L1644-1686)
```rust
#[test]
fn cmc_notify_top_up_rate_limited() {
    let state_machine = state_machine_builder_for_nns_tests().build();

    let account = AccountIdentifier::new(*TEST_USER1_PRINCIPAL, None);
    // The only requirement here is to have sufficient funds. Other than that,
    // the precise number here does not matter.
    let balance = Tokens::new(1e6 as u64, 0).unwrap();
    let nns_init_payloads = NnsInitPayloadsBuilder::new()
        .with_test_neurons()
        .with_ledger_account(account, balance)
        .build();
    setup_nns_canisters(&state_machine, nns_init_payloads);

    // First top-up should succeed since it's 90P - less than the 150P/hr limit.
    let cycles = notify_top_up(
        &state_machine,
        GOVERNANCE_CANISTER_ID,
        Tokens::new(900, 0).unwrap(),
    )
    .unwrap();
    assert_eq!(cycles, Cycles::new(90e15 as u128));

    // Second top-up should also succeed after 1 hour.
    state_machine.advance_time(Duration::from_secs(4000));
    let cycles = notify_top_up(
        &state_machine,
        GOVERNANCE_CANISTER_ID,
        Tokens::new(900, 0).unwrap(),
    )
    .unwrap();
    assert_eq!(cycles, Cycles::new(90e15 as u128));

    // Third top-up should fail since the rate limit is 150e15 cycles per hour,
    // and less than an hour has passed.
    state_machine.advance_time(Duration::from_secs(3000));
    let error = notify_top_up(
        &state_machine,
        GOVERNANCE_CANISTER_ID,
        Tokens::new(900, 0).unwrap(),
    )
    .unwrap_err();
    assert_matches!(error, NotifyError::Refunded { reason, .. } if reason.contains("try again later"));
```

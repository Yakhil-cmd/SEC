### Title
Minting Rate Limiter Updated Before Delivery Confirmation in `ensure_balance` — (`rs/nns/cmc/src/main.rs`)

### Summary
In the Cycles Minting Canister (CMC), `ensure_balance` updates the minting rate limiter and mints cycles **before** the actual async delivery of those cycles to the user. If the subsequent delivery call fails (cycles ledger deposit rejected, or all subnet canister-creation attempts fail), the limiter has already been permanently incremented and `total_cycles_minted` inflated, but no cycles were delivered. This is the direct IC analog of H-07: the invariant guard is checked and mutated at the *beginning* of the operation, while the operation itself can leave the invariant violated at the *end*.

---

### Finding Description

`ensure_balance` is a synchronous helper called at the top of both `do_mint_cycles` and `do_create_canister`:

```
ensure_balance(cycles, limiter_to_use)?;   // ← limiter mutated HERE
// ... then async delivery call follows ...
```

Inside `ensure_balance`:

1. The current CMC balance is read.
2. `cycles_to_mint = cycles − current_balance` is computed.
3. `check_and_add_cycles` is called — this both **checks** the hourly cap and **permanently records** `cycles_to_mint` in the sliding-window limiter.
4. `state.total_cycles_minted` is incremented.
5. `ic0_mint_cycles128(cycles_to_mint)` is called — cycles are minted into the CMC's own balance.

Only *after* all of the above does the caller make the async delivery:

- `do_mint_cycles` → `call_with_payment128(cycles_ledger, "deposit", …)` — can fail if the cycles ledger is unavailable or rejects the deposit (e.g., amount below ledger fee).
- `do_create_canister` → loops over subnets with `call_with_payment128(subnet, "create_canister", …)` — can fail if every subnet rejects.

When delivery fails, `process_mint_cycles` / `process_create_canister` refunds the ICP to the user, but **there is no rollback of the limiter or `total_cycles_minted`**. The minted cycles remain stranded in the CMC's own balance. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) 

The limiter's `check_and_add_cycles` is irreversible once called: [5](#0-4) 

---

### Impact Explanation

The `base_cycles_limit` is 150 × 10¹⁵ cycles per hour. [6](#0-5) 

Each failed `notify_mint_cycles` or `notify_create_canister` call that triggers `ensure_balance` with a non-zero `cycles_to_mint` permanently consumes a portion of this hourly budget without delivering any cycles to any user. Once the budget is exhausted, all subsequent legitimate minting requests are rejected with a rate-limit error for up to one hour. The `total_cycles_minted` counter is also permanently inflated, corrupting the on-chain accounting observable via the `total_cycles_minted` query.

---

### Likelihood Explanation

Two realistic, unprivileged-user-reachable triggers exist:

1. **Cycles ledger temporarily unavailable** (e.g., during a routine canister upgrade): any user calling `notify_mint_cycles` during the upgrade window will cause `ensure_balance` to mint and record cycles, then receive a delivery failure and an ICP refund. Each such call consumes up to ~100 T cycles of the hourly budget per ICP sent. Two calls with 1 ICP each can exhaust the 150 T/hr base limit.

2. **Deposit amount below cycles-ledger fee**: the test suite confirms this failure path is reachable without any privileged access. [7](#0-6) 

In both cases the attacker's ICP is refunded (minus ledger fees), so the cost of exhausting the rate limit is only transaction fees.

---

### Recommendation

Move the `ensure_balance` call to **after** the delivery async call succeeds, mirroring the pattern used for `burn_and_log` (which is already called only on success). Alternatively, if pre-minting is architecturally required, add an explicit rollback path that decrements the limiter and `total_cycles_minted` when the delivery call fails.

Concretely, in `do_mint_cycles` and `do_create_canister`, restructure as:

```rust
// 1. Attempt delivery (no limiter update yet)
let result = call_with_payment128(...).await;
// 2. Only on success, record in limiter
if result.is_ok() {
    ensure_balance_and_record(...)?;
}
```

---

### Proof of Concept

**Scenario — cycles ledger upgrade window:**

1. Cycles ledger canister begins upgrade (temporarily stops accepting calls).
2. Attacker sends 1 ICP to CMC with `MEMO_MINT_CYCLES` and calls `notify_mint_cycles`.
3. `process_mint_cycles` → `do_mint_cycles` → `ensure_balance(~100T cycles, BaseLimit)`:
   - Limiter records ~100 T cycles consumed.
   - `total_cycles_minted` += ~100 T.
   - `ic0_mint_cycles128(~100T)` mints cycles into CMC balance.
4. `call_with_payment128(cycles_ledger, "deposit", …)` **fails** (ledger unavailable).
5. `process_mint_cycles` calls `refund_icp` — attacker's ICP is returned minus fees.
6. Limiter is **not** rolled back; ~100 T of the 150 T/hr budget is consumed.
7. Attacker repeats once more → budget exhausted.
8. All legitimate `notify_mint_cycles` / `notify_top_up` / `notify_create_canister` calls now fail with "try again later" for up to one hour. [8](#0-7) [9](#0-8) [10](#0-9)

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

**File:** rs/nns/cmc/src/main.rs (L2245-2300)
```rust
    // We have subnets available, so we can now mint the cycles and create the canister.

    // Always use base cycles limit for minting cycles, since the Subnet Rental Canister
    // doesn't call endpoints using this function.
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

**File:** rs/nns/integration_tests/src/cycles_minting_canister.rs (L59-63)
```rust
const CYCLES_LEDGER_FEE: u128 = 100_000_000;
const CYCLES_MINTING_LIMIT: u128 = 150e15 as u128;

// per month
const SUBNET_RENTAL_CYCLES_MINTING_LIMIT: u128 = 500e15 as u128;
```

**File:** rs/nns/integration_tests/src/cycles_minting_canister.rs (L1337-1350)
```rust
    // insufficient amount
    let notify_mint_result =
        notify_mint_cycles(&state_machine, Tokens::new(0, 1).unwrap(), None, None).unwrap_err();
    let NotifyError::Refunded {
        reason,
        block_index,
    } = notify_mint_result
    else {
        panic!("Not refunded.")
    };
    assert!(reason.contains(
        "The requested amount 1000000 to be deposited is less than the cycles ledger fee"
    ));
    assert_eq!(block_index, None); // Amount too small to refund
```

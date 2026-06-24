### Title
Cycles Minting Rate Limit Bypass via `notify_mint_cycles` Endpoint — (File: `rs/nns/cmc/src/main.rs`)

---

### Summary

The Cycles Minting Canister (CMC) enforces a per-hour rate limit on cycles minting through `notify_top_up`, but the `notify_mint_cycles` endpoint — which also converts ICP to cycles — applies **no rate limit at all**. Any unprivileged user can bypass the `base_cycles_limit` (150e15 cycles/hour) by routing their ICP-to-cycles conversion through `notify_mint_cycles` instead of `notify_top_up`, then transferring the resulting cycles from the cycles ledger to any canister.

---

### Finding Description

The CMC maintains two separate rate limiters and a selector enum to choose between them:

```rust
// rs/nns/cmc/src/main.rs:397-419
enum CyclesMintingLimiterSelector {
    BaseLimit,
    SubnetRentalLimit,
}
impl CyclesMintingLimiterSelector {
    fn check_and_add_cycles(&self, state: &mut State, now: SystemTime, cycles_to_mint: Cycles)
        -> Result<(), String> { ... }
}
``` [1](#0-0) 

The `notify_top_up` endpoint selects the appropriate limiter and passes it all the way down to `ensure_balance`, which enforces the cap before any minting occurs:

```rust
// notify_top_up selects limiter (line 1149-1155)
let limiter_to_use = if caller == src_canister_principal && canister_id.get() == src_canister_principal {
    CyclesMintingLimiterSelector::SubnetRentalLimit
} else {
    CyclesMintingLimiterSelector::BaseLimit
};
// ... eventually calls:
deposit_cycles(canister_id, cycles, true, limiter_to_use).await
// which calls:
ensure_balance(cycles, limiter_to_use)?;  // enforces the rate limit
``` [2](#0-1) [3](#0-2) [4](#0-3) 

By contrast, `notify_mint_cycles` calls `process_mint_cycles`, which calls `do_mint_cycles` **directly** — with no `limiter_to_use` parameter, no call to `ensure_balance`, and no rate-limit check of any kind:

```rust
// rs/nns/cmc/src/main.rs:1958-1983
async fn process_mint_cycles(
    to_account: Account, amount: Tokens, deposit_memo: Option<Vec<u8>>,
    from: AccountIdentifier, sub: Subaccount,
) -> NotifyMintCyclesResult {
    let cycles = tokens_to_cycles(amount)?;
    match do_mint_cycles(to_account, cycles, deposit_memo).await {  // no limiter
        Ok(deposit_result) => { burn_and_log(sub, amount).await; ... }
        ...
    }
}
``` [5](#0-4) 

The `notify_mint_cycles` endpoint is publicly callable by any principal:

```
// rs/nns/cmc/cmc.did:252
notify_mint_cycles : (NotifyMintCyclesArg) -> (NotifyMintCyclesResult);
``` [6](#0-5) 

The rate limit constants confirm the intended cap:

```rust
const DEFAULT_CYCLES_LIMIT: u128 = 150e15 as u128;  // 150T cycles/hour for base users
const SUBNET_RENTAL_DEFAULT_CYCLES_LIMIT: u128 = 500e15 as u128;  // 500T/month for SRC
``` [7](#0-6) 

---

### Impact Explanation

The `base_cycles_limit` (150e15 cycles/hour) is a safety mechanism to prevent sudden large minting events that could destabilize the ICP/cycles exchange rate. By routing through `notify_mint_cycles`, an attacker can mint an arbitrarily large number of cycles per hour — limited only by their ICP holdings — without triggering the rate limiter. The cycles deposited to the cycles ledger can then be freely transferred to any canister via ICRC-1 transfer, achieving the same end-state as an unlimited `notify_top_up`. This is a **cycles/resource accounting bug**: the rate limit is a global invariant of the CMC that is silently not enforced on one of its public minting paths.

---

### Likelihood Explanation

The attack requires no special privileges. Any principal with ICP can:
1. Send ICP to the CMC subaccount with `MEMO_MINT_CYCLES`.
2. Call `notify_mint_cycles` — a standard public update endpoint.
3. Transfer the resulting cycles from the cycles ledger to any target canister.

The two endpoints (`notify_top_up` and `notify_mint_cycles`) are functionally equivalent from an economic standpoint — both convert ICP to cycles — making this a straightforward, single-transaction bypass of the rate limit.

---

### Recommendation

Apply the same `CyclesMintingLimiterSelector` pattern to `notify_mint_cycles` / `process_mint_cycles` as is used in `notify_top_up` / `deposit_cycles`. Specifically, `process_mint_cycles` should call `ensure_balance` (or an equivalent check against `state.base_limiter`) before invoking `do_mint_cycles`, so that the global minting rate limit is enforced uniformly across all ICP-to-cycles conversion paths.

---

### Proof of Concept

```
1. Attacker holds N ICP (N >> 150T cycles worth).
2. Attacker sends ICP to CMC subaccount(caller) with memo MEMO_MINT_CYCLES.
3. Attacker calls notify_mint_cycles{block_index, to_subaccount: None, deposit_memo: None}.
   → process_mint_cycles → do_mint_cycles (no limiter check) → cycles deposited to cycles ledger.
4. Attacker calls cycles_ledger.icrc1_transfer to move cycles to any target canister.
5. Repeat steps 2-4 in rapid succession.
   → Attacker mints >> 150e15 cycles/hour, bypassing base_cycles_limit entirely.
   → base_limiter.get_count() remains 0; the rate-limit metric is never incremented.
``` [8](#0-7) [9](#0-8)

### Citations

**File:** rs/nns/cmc/src/main.rs (L83-86)
```rust
const DEFAULT_CYCLES_LIMIT: u128 = 150e15 as u128;

/// The limit for the number of cycles that can be minted by the Subnet Rental Canister in a month.
const SUBNET_RENTAL_DEFAULT_CYCLES_LIMIT: u128 = 500e15 as u128;
```

**File:** rs/nns/cmc/src/main.rs (L397-419)
```rust
enum CyclesMintingLimiterSelector {
    BaseLimit,
    SubnetRentalLimit,
}

impl CyclesMintingLimiterSelector {
    fn check_and_add_cycles(
        &self,
        state: &mut State,
        now: SystemTime,
        cycles_to_mint: Cycles,
    ) -> Result<(), String> {
        match self {
            CyclesMintingLimiterSelector::BaseLimit => state.base_limiter.check_and_add_cycles(
                now,
                cycles_to_mint,
                state.base_cycles_limit,
            ),
            CyclesMintingLimiterSelector::SubnetRentalLimit => state
                .subnet_rental_canister_limiter
                .check_and_add_cycles(now, cycles_to_mint, state.subnet_rental_cycles_limit),
        }
    }
```

**File:** rs/nns/cmc/src/main.rs (L1148-1155)
```rust
    let src_canister_principal = SUBNET_RENTAL_CANISTER_ID.get();
    let limiter_to_use =
        if caller == src_canister_principal && canister_id.get() == src_canister_principal {
            // caller and destination needs to be src_canister_principal to get alternate limiter
            CyclesMintingLimiterSelector::SubnetRentalLimit
        } else {
            CyclesMintingLimiterSelector::BaseLimit
        };
```

**File:** rs/nns/cmc/src/main.rs (L1239-1318)
```rust
async fn notify_mint_cycles(
    NotifyMintCyclesArg {
        block_index,
        to_subaccount,
        deposit_memo,
    }: NotifyMintCyclesArg,
) -> NotifyMintCyclesResult {
    let subaccount = Subaccount::from(&caller());
    let to_account = Account {
        owner: caller().into(),
        subaccount: to_subaccount,
    };

    let deposit_memo_len = deposit_memo.as_ref().map_or(0, |memo| memo.len());
    if deposit_memo_len > MAX_MEMO_LENGTH {
        return Err(NotifyError::Other {
            error_code: NotifyErrorCode::DepositMemoTooLong as u64,
            error_message: format!(
                "Memo length {deposit_memo_len} exceeds the maximum length of {MAX_MEMO_LENGTH}"
            ),
        });
    }

    let (amount, from) = fetch_transaction(block_index, subaccount, MEMO_MINT_CYCLES).await?;

    let maybe_early_result = with_state_mut(|state| {
        state.purge_old_notifications(MAX_NOTIFY_HISTORY);

        if block_index <= state.last_purged_notification {
            return Some(Err(NotifyError::TransactionTooOld(
                state.last_purged_notification + 1,
            )));
        }

        match state.blocks_notified.entry(block_index) {
            Entry::Occupied(entry) => match entry.get() {
                NotificationStatus::Processing => Some(Err(NotifyError::Processing)),
                NotificationStatus::NotifiedMint(resp) => Some(resp.clone()),
                NotificationStatus::NotifiedCreateCanister(_) => {
                    Some(Err(NotifyError::InvalidTransaction(
                        "The same payment is already processed as a create canister request."
                            .into(),
                    )))
                }
                NotificationStatus::NotifiedTopUp(_) => Some(Err(NotifyError::InvalidTransaction(
                    "The same payment is already processed as a top up request.".into(),
                ))),
                NotificationStatus::NotMeaningfulMemo(_) => {
                    Some(Err(NotifyError::InvalidTransaction(
                        "The same payment is already processed as an automatic refund.".into(),
                    )))
                }
            },
            Entry::Vacant(entry) => {
                entry.insert(NotificationStatus::Processing);
                None
            }
        }
    });

    match maybe_early_result {
        Some(result) => result,
        None => {
            let result =
                process_mint_cycles(to_account, amount, deposit_memo, from, subaccount).await;

            with_state_mut(|state| {
                state.blocks_notified.insert(
                    block_index,
                    NotificationStatus::NotifiedMint(result.clone()),
                );
                if is_transient_error(&result) {
                    state.blocks_notified.remove(&block_index);
                }
            });

            result
        }
    }
}
```

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

**File:** rs/nns/cmc/src/main.rs (L2110-2118)
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
```

**File:** rs/nns/cmc/src/main.rs (L2306-2324)
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
```

**File:** rs/nns/cmc/cmc.did (L251-253)
```text
  // Mints cycles and deposits them to the cycles ledger
  notify_mint_cycles : (NotifyMintCyclesArg) -> (NotifyMintCyclesResult);

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

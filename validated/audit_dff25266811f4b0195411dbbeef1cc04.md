### Title
Zero-Amount `icrc1_transfer` (Mint Path) Writes a Block and Inflates the Deduplication Cache — (`File: rs/ledger_suite/icrc1/ledger/src/main.rs`)

### Summary

The ICRC-1 ledger's `execute_transfer_not_async` function (called by `icrc1_transfer` and `icrc2_transfer_from`) validates zero-amount transfers for the **burn** path but has **no zero-amount guard on the mint path**. Any caller who is the minting account can submit a zero-amount mint, which succeeds, writes a real block to the chain, and — if `created_at_time` is supplied — inserts an entry into the deduplication cache (`transactions_by_hash` / `transactions_by_height`). This is the direct IC analog of the `ds_token` zero-value withdrawal that increments a counter without meaningful asset movement.

### Finding Description

In `execute_transfer_not_async` (`rs/ledger_suite/icrc1/ledger/src/main.rs`, lines 606–667), three branches handle the three operation types:

1. **Burn path** (lines 606–635): explicitly rejects zero amounts at lines 617–621:
   ```rust
   if Tokens::is_zero(&amount) {
       return Err(CoreTransferError::BadBurn {
           min_burn_amount: ledger.transfer_fee(),
       });
   }
   ```

2. **Mint path** (lines 636–647): **no zero-amount check**. A call from the minting account with `amount = 0` falls straight through to `apply_transaction`.

3. **Regular transfer path** (lines 648–667): no zero-amount check either, but the caller must pay the transfer fee, so a zero-amount transfer still costs the fee and drains the sender's balance by `fee`.

For the mint path, `apply_transaction` (`rs/ledger_suite/common/ledger_canister_core/src/ledger.rs`, lines 214–311) unconditionally:
- Calls `transaction.apply(ledger, ...)` which credits zero tokens (no-op on balances).
- Calls `ledger.blockchain_mut().add_block(block)` — a real block is appended to the chain.
- If `created_at_time` was set, inserts the transaction hash into `transactions_by_hash` and pushes to `transactions_by_height` (the deduplication window queue).

The same zero-amount mint path exists in the ICP ledger's `icrc1_send_not_async` (`rs/ledger_suite/icp/ledger/src/main.rs`, lines 322–331), which also has no zero-amount guard on the mint branch. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) 

### Impact Explanation

**Ledger conservation bug / resource accounting bug.**

1. **Spurious blocks on-chain**: Zero-amount mint blocks are permanently appended to the ledger blockchain. Downstream consumers (index canisters, Rosetta, archive nodes) must process and store these semantically meaningless blocks, inflating storage and processing costs.

2. **Deduplication cache inflation**: When `created_at_time` is supplied, each zero-amount mint inserts an entry into `transactions_by_hash` and `transactions_by_height`. The throttle logic in `throttle()` (`rs/ledger_suite/common/ledger_canister_core/src/ledger.rs`, lines 342–367) counts entries in `transactions_by_height` against `max_transactions_in_window`. Flooding this cache with zero-amount mints can push the ledger into its throttle regime, causing legitimate transactions to receive `TxThrottled` errors — a denial-of-service on the ledger's transaction throughput.

3. **`total_supply` / `token_pool` invariant noise**: `Balances::mint` subtracts from `token_pool` even for zero amounts (the subtraction is a no-op numerically, but the call path is exercised). More critically, `update_total_volume` is called with `amount = Tokens::ZERO`, which is a no-op only because of the `if amount != Tokens::ZERO` guard inside it — but this is a fragile dependency. [5](#0-4) [6](#0-5) 

### Likelihood Explanation

The minting account is a privileged principal (typically the CMC or NNS governance canister), so an **unprivileged** user cannot directly trigger the mint path. However:

- Any canister or user that **controls** the minting account (e.g., the CMC canister itself, or a governance proposal that calls the minting account) can issue zero-amount mints.
- On ledgers where the minting account is a canister under attacker influence (e.g., a custom ICRC-1 ledger deployed by a developer), this is directly reachable.
- The regular-transfer path (non-mint, non-burn) also accepts zero-amount transfers with no guard, and that path is reachable by **any** token holder who can pay the fee. Each such transfer writes a block and, with `created_at_time`, fills the dedup cache.

The regular-transfer zero-amount case is reachable by any unprivileged ingress sender who holds enough tokens to pay the fee. [7](#0-6) 

### Recommendation

1. **Mint path**: Add a zero-amount guard in `execute_transfer_not_async` for the mint branch, mirroring the burn branch:
   ```rust
   } else if &from_account == ledger.minting_account() {
       if Tokens::is_zero(&amount) {
           return Err(CoreTransferError::BadBurn { min_burn_amount: Tokens::zero() });
           // or a dedicated BadMint variant
       }
       ...
   ```

2. **Regular transfer path**: Add a zero-amount guard for the regular transfer branch as well, returning `InsufficientFunds` or a `GenericError`.

3. Apply the same fix to `icrc1_send_not_async` in `rs/ledger_suite/icp/ledger/src/main.rs` (mint branch, line 331).

4. Consider adding a zero-amount check at the top of `execute_transfer_not_async` before the branch dispatch, as a single defensive guard. [8](#0-7) [9](#0-8) 

### Proof of Concept

```
// Attacker is the minting account (or controls it).
// Call icrc1_transfer on the ICRC-1 ledger canister:
icrc1_transfer({
    from_subaccount: null,
    to: <any_account>,
    amount: 0,          // zero-value mint
    fee: null,          // minting fee is zero
    memo: null,
    created_at_time: <current_time_ns>  // triggers dedup cache insertion
})
// => Returns Ok(block_index N)
// Block N is now permanently on-chain with amount=0.
// transactions_by_height now has one more entry.
// Repeat rapidly to fill the dedup window and trigger TxThrottled for
// legitimate callers.
```

For the regular-transfer zero-amount case (any token holder):
```
icrc1_transfer({
    from_subaccount: null,
    to: <any_account>,
    amount: 0,          // zero-value transfer
    fee: null,          // fee is charged from sender's balance
    memo: null,
    created_at_time: <current_time_ns>
})
// => Returns Ok(block_index N) — sender pays fee, receives nothing,
//    block written, dedup cache entry inserted.
```

The throttle limit (`MAX_TRANSACTIONS_IN_WINDOW = 3_000_000` for the ICRC-1 ledger) means an attacker needs to sustain the flood for the full `TRANSACTION_WINDOW` (24 hours) to fully saturate the cache, but partial saturation still degrades throughput for legitimate users. [10](#0-9) [11](#0-10)

### Citations

**File:** rs/ledger_suite/icrc1/ledger/src/main.rs (L422-436)
```rust
fn update_total_volume(amount: Tokens, with_fee: bool) {
    let mut total_volume = TOTAL_VOLUME.with(|n| *n.borrow());
    let denominator = TOTAL_VOLUME_DENOMINATOR.with(|n| *n.borrow());
    if amount != Tokens::ZERO {
        let amount = tokens_to_f64(amount) / denominator;
        total_volume = f64_saturating_add(total_volume, amount);
    }
    if with_fee {
        total_volume = f64_saturating_add(
            total_volume,
            TOTAL_VOLUME_FEE_IN_DECIMALS.with(|n| *n.borrow()),
        );
    }
    TOTAL_VOLUME.with(|n| *n.borrow_mut() = total_volume);
}
```

**File:** rs/ledger_suite/icrc1/ledger/src/main.rs (L606-647)
```rust
        let (tx, effective_fee) = if &to == ledger.minting_account() {
            let expected_fee = Tokens::zero();
            if fee.is_some() && fee.as_ref() != Some(&expected_fee.into()) {
                return Err(CoreTransferError::BadFee { expected_fee });
            }

            let balance = ledger.balances().account_balance(&from_account);
            let min_burn_amount = ledger.transfer_fee().min(balance);
            if amount < min_burn_amount {
                return Err(CoreTransferError::BadBurn { min_burn_amount });
            }
            if Tokens::is_zero(&amount) {
                return Err(CoreTransferError::BadBurn {
                    min_burn_amount: ledger.transfer_fee(),
                });
            }

            (
                Transaction {
                    operation: Operation::Burn {
                        from: from_account,
                        spender,
                        amount,
                        fee: None,
                    },
                    created_at_time: created_at_time.map(|t| t.as_nanos_since_unix_epoch()),
                    memo,
                },
                Tokens::zero(),
            )
        } else if &from_account == ledger.minting_account() {
            if spender.is_some() {
                ic_cdk::trap("the minter account cannot delegate mints")
            }
            let expected_fee = Tokens::zero();
            if fee.is_some() && fee.as_ref() != Some(&expected_fee.into()) {
                return Err(CoreTransferError::BadFee { expected_fee });
            }
            (
                Transaction::mint(to, amount, created_at_time, memo),
                Tokens::zero(),
            )
```

**File:** rs/ledger_suite/icrc1/ledger/src/main.rs (L648-670)
```rust
        } else {
            let expected_fee_tokens = ledger.transfer_fee();
            if fee.is_some() && fee.as_ref() != Some(&expected_fee_tokens.into()) {
                return Err(CoreTransferError::BadFee {
                    expected_fee: expected_fee_tokens,
                });
            }
            (
                Transaction::transfer(
                    from_account,
                    to,
                    spender,
                    amount,
                    fee.map(|_| expected_fee_tokens),
                    created_at_time,
                    memo,
                ),
                expected_fee_tokens,
            )
        };

        let (block_idx, _) = apply_transaction(ledger, tx, now, effective_fee)?;
        update_total_volume(amount, effective_fee != Tokens::zero());
```

**File:** rs/ledger_suite/common/ledger_canister_core/src/ledger.rs (L256-310)
```rust
    transaction
        .apply(ledger, now, effective_fee.clone())
        .map_err(|e| match e {
            TxApplyError::InsufficientFunds { balance } => {
                TransferError::InsufficientFunds { balance }
            }
            TxApplyError::InsufficientAllowance { allowance } => {
                TransferError::InsufficientAllowance { allowance }
            }
            TxApplyError::ExpiredApproval { now } => {
                TransferError::ExpiredApproval { ledger_time: now }
            }
            TxApplyError::AllowanceChanged { current_allowance } => {
                TransferError::AllowanceChanged { current_allowance }
            }
            TxApplyError::SelfApproval => TransferError::SelfApproval,
            TxApplyError::BurnOrMintFee => TransferError::BadFee {
                expected_fee: L::Tokens::zero(),
            },
        })?;

    let fee_collector = ledger.fee_collector().cloned();
    let block = L::Block::from_transaction(
        ledger.blockchain().last_hash,
        transaction,
        now,
        effective_fee,
        fee_collector,
    );
    let block_timestamp = block.timestamp();

    let height = ledger
        .blockchain_mut()
        .add_block(block)
        .expect("failed to add block");
    if let Some(fee_collector) = ledger.fee_collector_mut().as_mut()
        && fee_collector.block_index.is_none()
    {
        fee_collector.block_index = Some(height);
    }

    if let Some((_, tx_hash)) = maybe_time_and_hash {
        // The caller requested deduplication, so we have to remember this
        // transaction within the dedup window.
        ledger.transactions_by_hash_mut().insert(tx_hash, height);

        ledger
            .transactions_by_height_mut()
            .push_back(TransactionInfo {
                block_timestamp,
                transaction_hash: tx_hash,
            });
    }

    Ok((height, ledger.blockchain().last_hash.unwrap()))
```

**File:** rs/ledger_suite/common/ledger_canister_core/src/ledger.rs (L342-367)
```rust
/// load on the ledger.
fn throttle<L: LedgerData>(ledger: &L, now: TimeStamp) -> bool {
    let num_in_window = ledger.transactions_by_height().len();
    // We admit the first half of max_transactions_in_window freely.
    // After that we start throttling on per-second basis.
    // This way we guarantee that at most max_transactions_in_window will
    // get through within the transaction window.
    if num_in_window >= ledger.max_transactions_in_window() / 2 {
        // max num of transactions allowed per second
        let max_rate = (0.5 * ledger.max_transactions_in_window() as f64
            / ledger.transaction_window().as_secs_f64())
        .ceil() as usize;

        if ledger
            .transactions_by_height()
            .get(num_in_window.saturating_sub(max_rate))
            .map(|tx| tx.block_timestamp)
            .unwrap_or_else(|| TimeStamp::from_nanos_since_unix_epoch(0))
            + Duration::from_secs(1)
            > now
        {
            return true;
        }
    }
    false
}
```

**File:** rs/ledger_suite/icp/ledger/src/main.rs (L309-313)
```rust
        if amount == Tokens::ZERO {
            return Err(CoreTransferError::BadBurn {
                min_burn_amount: ledger.transfer_fee,
            });
        }
```

**File:** rs/ledger_suite/icp/ledger/src/main.rs (L322-331)
```rust
    } else if from == minting_acc {
        if spender_account.is_some() {
            trap("the minter account cannot delegate mints");
        }
        if fee.is_some() && fee.as_ref() != Some(&Nat::from(0_u64)) {
            return Err(CoreTransferError::BadFee {
                expected_fee: Tokens::ZERO,
            });
        }
        (Operation::Mint { to, amount }, Tokens::ZERO)
```

**File:** rs/ledger_suite/icrc1/ledger/src/lib.rs (L779-790)
```rust
    fn transaction_window(&self) -> Duration {
        TRANSACTION_WINDOW
    }

    fn max_transactions_in_window(&self) -> usize {
        MAX_TRANSACTIONS_IN_WINDOW
    }

    fn max_transactions_to_purge(&self) -> usize {
        MAX_TRANSACTIONS_TO_PURGE
    }

```

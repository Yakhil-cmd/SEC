### Title
Quarantined Reimbursements Permanently Freeze User Funds Without Automated Recovery — (File: `rs/ethereum/cketh/minter/src/withdraw.rs`, `rs/bitcoin/ckbtc/minter/src/reimbursement/mod.rs`)

---

### Summary

In the ckETH and ckBTC chain-fusion minters, when a canister panic occurs after a successful ledger mint-back call but before the success event is durably recorded, a `scopeguard` cleanup callback fires and permanently transitions the reimbursement into a terminal `Quarantined` state. There is no automated retry or recovery path. The user's tokens — already burned on the ckETH/ckBTC ledger for the original withdrawal — are permanently frozen until a manual governance canister upgrade intervenes.

---

### Finding Description

Both minters implement a "prevent double-minting" guard pattern around the reimbursement mint call.

**ckETH minter** (`rs/ethereum/cketh/minter/src/withdraw.rs`):

```rust
let prevent_double_minting_guard = scopeguard::guard(index.clone(), |index| {
    mutate_state(|s| process_event(s, EventType::QuarantinedReimbursement { index }));
});
// ...
let block_index = match client.transfer(args).await {
    Ok(Ok(block_index)) => block_index...,
    Ok(Err(err)) => {
        ScopeGuard::into_inner(prevent_double_minting_guard); // defuse
        continue;
    }
    Err(err) => {
        ScopeGuard::into_inner(prevent_double_minting_guard); // defuse
        continue;
    }
};
// ← panic here fires the guard
mutate_state(|s| process_event(s, event));
ScopeGuard::into_inner(prevent_double_minting_guard); // defuse on success
``` [1](#0-0) 

If the canister panics after `client.transfer(args).await` returns `Ok(Ok(block_index))` — for example inside `mutate_state(|s| process_event(s, event))` — the IC runtime rolls back the state to the pre-await snapshot, then invokes the cleanup callback. The guard fires and emits `QuarantinedReimbursement`.

**ckBTC minter** (`rs/bitcoin/ckbtc/minter/src/reimbursement/mod.rs`) has the identical pattern:

```rust
let prevent_double_minting_guard = scopeguard::guard(burn_index, |index| {
    state::mutate_state(|s| {
        state::audit::quarantine_withdrawal_reimbursement(s, index, runtime)
    });
});
match runtime.mint_ckbtc(...).await {
    Ok(mint_index) => {
        state::mutate_state(|s| {
            state::audit::reimburse_withdrawal_completed(s, burn_index, mint_index, runtime)
        }); // ← panic here fires the guard
    }
    Err(err) => { error_count += 1; }
}
scopeguard::ScopeGuard::into_inner(prevent_double_minting_guard);
``` [2](#0-1) 

Once the guard fires, `record_quarantined_reimbursement` permanently removes the entry from `reimbursement_requests` and inserts `Err(ReimbursedError::Quarantined)` into `reimbursed`:

```rust
pub fn record_quarantined_reimbursement(&mut self, index: ReimbursementIndex) {
    self.reimbursement_requests.remove(&index);
    self.reimbursed.insert(index, Err(ReimbursedError::Quarantined));
}
``` [3](#0-2) 

The event log comment explicitly acknowledges the terminal nature:

> "The reimbursement is quarantined to prevent any double minting and **will not be processed without further manual intervention**." [4](#0-3) [5](#0-4) 

The `ReimbursedError::Quarantined` variant carries the same documentation in both minters: [6](#0-5) [7](#0-6) 

For ckBTC, the user-visible status after quarantine becomes `RetrieveBtcStatusV2::Unknown`, silently hiding the frozen state: [8](#0-7) 

For ckETH, the status remains `TxFinalized(PendingReimbursement(...))` indefinitely — misleading the user into thinking reimbursement is still in progress. [9](#0-8) 

---

### Impact Explanation

A user who:
1. Burned ckETH/ckBTC to initiate a withdrawal,
2. Whose Ethereum/Bitcoin transaction failed on-chain (triggering a reimbursement), and
3. Whose reimbursement minting call succeeded at the ledger but the minter panicked before recording the event,

loses their tokens permanently with no automated recovery. The ckETH/ckBTC was already burned from the ledger; the Ethereum/Bitcoin transaction failed; and the reimbursement mint — though it may have succeeded on the ledger — is treated as unknown and quarantined. The user's funds are frozen until a governance canister upgrade manually re-processes the quarantined entry.

**Impact: High** — permanent loss of user funds with no self-service recovery.

---

### Likelihood Explanation

The panic window is narrow but realistic. After `client.transfer(args).await` returns `Ok(Ok(block_index))`, the minter must call `mutate_state(|s| process_event(s, event))`. This call:
- Invokes `apply_state_transition`, which contains `assert_eq!` / `unwrap_or_else(|| panic!(...))` guards (e.g., in `record_finalized_reimbursement`).
- Calls `record_event`, which writes to stable memory — a write that can trap if the stable memory allocation fails or if the serialization panics.

Any instruction-limit trap, stable-memory-full trap, or assertion failure inside `mutate_state` after a successful inter-canister mint call triggers the quarantine. As the minter's state grows (many pending reimbursements, large event log), the probability of hitting instruction or memory limits increases.

**Likelihood: Low-Medium** — requires a panic in a specific post-await window, but the window exists in production code and grows more likely under load or with a latent bug.

---

### Recommendation

1. **Add a recovery endpoint**: Expose a governance-callable (or even user-callable with proof) function that can re-queue a `Quarantined` reimbursement back into `reimbursement_requests` after verifying via ledger query whether the mint block index already exists.
2. **Use `created_at_time` in the mint transfer**: Setting `created_at_time` on the `TransferArg` would allow the minter to safely retry the mint call — the ledger would return `Duplicate { duplicate_of }` if the mint already succeeded, allowing the minter to record the correct block index instead of quarantining.
3. **Improve user-facing status**: Replace `RetrieveBtcStatusV2::Unknown` for quarantined withdrawals with an explicit `Quarantined` variant so users are not silently left without information.

---

### Proof of Concept

1. User calls `withdraw_eth(amount, destination)` on the ckETH minter — ckETH is burned from the ledger.
2. The minter creates, signs, and sends an Ethereum transaction; the transaction is mined but reverts on-chain (`TransactionStatus::Failure`).
3. `record_finalized_transaction` schedules a reimbursement: the entry is placed in `reimbursement_requests`.
4. The periodic `process_reimbursement()` timer fires. For this reimbursement, `client.transfer(args).await` succeeds — the ckETH ledger mints the refund and returns `Ok(Ok(block_index))`.
5. Immediately after the await resumes, `mutate_state(|s| process_event(s, ReimbursedEthWithdrawal {...}))` panics (e.g., stable memory write trap due to a full allocation).
6. The IC runtime rolls back the post-await state mutations; the cleanup callback fires `prevent_double_minting_guard`, emitting `QuarantinedReimbursement { index }`.
7. `record_quarantined_reimbursement` removes the entry from `reimbursement_requests` and inserts `Err(Quarantined)` into `reimbursed`.
8. The user queries `retrieve_eth_status(withdrawal_id)` and receives `TxFinalized(PendingReimbursement(...))` indefinitely. Their ckETH is permanently frozen. [10](#0-9) [11](#0-10)

### Citations

**File:** rs/ethereum/cketh/minter/src/withdraw.rs (L67-141)
```rust
    for (index, reimbursement_request) in reimbursements {
        // Ensure that even if we were to panic in the callback, after having contacted the ledger to mint the tokens,
        // this reimbursement request will not be processed again.
        let prevent_double_minting_guard = scopeguard::guard(index.clone(), |index| {
            mutate_state(|s| process_event(s, EventType::QuarantinedReimbursement { index }));
        });
        let ledger_canister_id = match index {
            ReimbursementIndex::CkEth { .. } => read_state(|s| s.cketh_ledger_id),
            ReimbursementIndex::CkErc20 { ledger_id, .. } => ledger_id,
        };
        let client = ICRC1Client {
            runtime: CdkRuntime,
            ledger_canister_id,
        };
        let memo = Memo::from(reimbursement_request.clone());
        let args = TransferArg {
            from_subaccount: None,
            to: Account {
                owner: reimbursement_request.to,
                subaccount: reimbursement_request
                    .to_subaccount
                    .map(LedgerSubaccount::to_bytes),
            },
            fee: None,
            created_at_time: None,
            memo: Some(memo),
            amount: Nat::from(reimbursement_request.reimbursed_amount),
        };
        let block_index = match client.transfer(args).await {
            Ok(Ok(block_index)) => block_index
                .0
                .to_u64()
                .expect("block index should fit into u64"),
            Ok(Err(err)) => {
                log!(INFO, "[process_reimbursement] Failed to mint ckETH {err}");
                error_count += 1;
                // minting failed, defuse guard
                ScopeGuard::into_inner(prevent_double_minting_guard);
                continue;
            }
            Err(err) => {
                log!(
                    INFO,
                    "[process_reimbursement] Failed to send a message to the ledger ({ledger_canister_id}): {err:?}"
                );
                error_count += 1;
                // minting failed, defuse guard
                ScopeGuard::into_inner(prevent_double_minting_guard);
                continue;
            }
        };
        let reimbursed = Reimbursed {
            burn_in_block: reimbursement_request.ledger_burn_index,
            reimbursed_in_block: LedgerMintIndex::new(block_index),
            reimbursed_amount: reimbursement_request.reimbursed_amount,
            transaction_hash: reimbursement_request.transaction_hash,
        };
        let event = match index {
            ReimbursementIndex::CkEth {
                ledger_burn_index: _,
            } => EventType::ReimbursedEthWithdrawal(reimbursed),
            ReimbursementIndex::CkErc20 {
                cketh_ledger_burn_index,
                ledger_id,
                ckerc20_ledger_burn_index: _,
            } => EventType::ReimbursedErc20Withdrawal {
                cketh_ledger_burn_index,
                ckerc20_ledger_id: ledger_id,
                reimbursed,
            },
        };
        mutate_state(|s| process_event(s, event));
        // minting succeeded, defuse guard
        ScopeGuard::into_inner(prevent_double_minting_guard);
    }
```

**File:** rs/bitcoin/ckbtc/minter/src/reimbursement/mod.rs (L30-37)
```rust
#[derive(Clone, Eq, PartialEq, Debug)]
pub enum ReimbursedError {
    /// Whether reimbursement was minted or not is unknown,
    /// most likely because there was an unexpected panic in the callback.
    /// The reimbursement request is quarantined to avoid any double minting and
    /// will not be further processed without manual intervention.
    Quarantined,
}
```

**File:** rs/bitcoin/ckbtc/minter/src/reimbursement/mod.rs (L64-107)
```rust
    for (burn_index, reimbursement) in pending_reimbursements {
        // Ensure that even if we were to panic in the callback, after having contacted the ledger to mint the tokens,
        // this reimbursement request will not be processed again.
        let prevent_double_minting_guard = scopeguard::guard(burn_index, |index| {
            state::mutate_state(|s| {
                state::audit::quarantine_withdrawal_reimbursement(s, index, runtime)
            });
        });
        let memo = MintMemo::ReimburseWithdrawal {
            withdrawal_id: burn_index,
        };
        match runtime
            .mint_ckbtc(
                reimbursement.amount,
                reimbursement.account,
                Memo::from(crate::memo::encode(&memo)),
            )
            .await
        {
            Ok(mint_index) => {
                log!(
                    Priority::Debug,
                    "[reimburse_withdrawals]: Successfully reimbursed {:?} at mint block index {}",
                    reimbursement,
                    mint_index
                );
                state::mutate_state(|s| {
                    state::audit::reimburse_withdrawal_completed(s, burn_index, mint_index, runtime)
                });
            }
            Err(err) => {
                log!(
                    Priority::Info,
                    "[reimburse_withdrawals]: Failed to reimburse {:?}: {:?}. Will retry later",
                    reimbursement,
                    err
                );
                error_count += 1;
            }
        }
        // Defuse the guard. Note that in case of a panic in the callback (either before or after this point)
        // the defuse will not be effective (due to state rollback), and the guard that was
        // setup before the `mint_ckbtc` async call will be invoked.
        scopeguard::ScopeGuard::into_inner(prevent_double_minting_guard);
```

**File:** rs/ethereum/cketh/minter/src/state/transactions/mod.rs (L270-277)
```rust
#[derive(Clone, Eq, PartialEq, Debug)]
pub enum ReimbursedError {
    /// Whether reimbursement was minted or not is unknown,
    /// most likely because there was an unexpected panic in the callback.
    /// The reimbursement request is quarantined to avoid any double minting and
    /// will not be further processed without manual intervention.
    Quarantined,
}
```

**File:** rs/ethereum/cketh/minter/src/state/transactions/mod.rs (L680-748)
```rust
    pub fn record_finalized_transaction(
        &mut self,
        ledger_burn_index: LedgerBurnIndex,
        receipt: TransactionReceipt,
    ) {
        let sent_tx = self
            .sent_tx
            .get_alt(&ledger_burn_index)
            .expect("BUG: missing sent transactions")
            .iter()
            .find(|sent_tx| sent_tx.as_ref().hash() == receipt.transaction_hash)
            .expect("ERROR: no transaction matching receipt");
        let finalized_tx = sent_tx
            .as_ref()
            .clone()
            .try_finalize(receipt.clone())
            .expect("ERROR: invalid transaction receipt");

        let nonce = sent_tx.as_ref().nonce();
        {
            self.sent_tx.remove_entry(&nonce);
            Self::cleanup_failed_resubmitted_transactions(&mut self.created_tx, &nonce);
        }
        assert_eq!(
            self.finalized_tx
                .try_insert(nonce, ledger_burn_index, finalized_tx.clone()),
            Ok(())
        );

        assert!(
            self.maybe_reimburse.remove(&ledger_burn_index),
            "failed to remove entry from maybe_reimburse with block index: {ledger_burn_index}",
        );

        let request = self.processed_withdrawal_requests
            .get(&ledger_burn_index)
            .expect("failed to find entry from processed_withdrawal_requests with block index: {ledger_burn_index}");
        let index = ReimbursementIndex::from(request);
        match &request {
            WithdrawalRequest::CkEth(request) => {
                if receipt.status == TransactionStatus::Failure {
                    self.record_reimbursement_request(
                        index,
                        ReimbursementRequest {
                            ledger_burn_index,
                            to: request.from,
                            to_subaccount: request.from_subaccount.clone(),
                            reimbursed_amount: finalized_tx.transaction_amount().change_units(),
                            transaction_hash: Some(receipt.transaction_hash),
                        },
                    );
                }
            }
            WithdrawalRequest::CkErc20(request) => {
                if receipt.status == TransactionStatus::Failure {
                    self.record_reimbursement_request(
                        index,
                        ReimbursementRequest {
                            ledger_burn_index: request.ckerc20_ledger_burn_index,
                            reimbursed_amount: request.withdrawal_amount.change_units(),
                            to: request.from,
                            to_subaccount: request.from_subaccount.clone(),
                            transaction_hash: Some(receipt.transaction_hash),
                        },
                    );
                }
            }
        }
    }
```

**File:** rs/ethereum/cketh/minter/src/state/transactions/mod.rs (L775-779)
```rust
    pub fn record_quarantined_reimbursement(&mut self, index: ReimbursementIndex) {
        self.reimbursement_requests.remove(&index);
        self.reimbursed
            .insert(index, Err(ReimbursedError::Quarantined));
    }
```

**File:** rs/ethereum/cketh/minter/src/state/event.rs (L150-158)
```rust
    /// The minter unexpectedly panic while processing a reimbursement.
    /// The reimbursement is quarantined to prevent any double minting and
    /// will not be processed without further manual intervention.
    #[n(22)]
    QuarantinedReimbursement {
        /// The unique identifier of the reimbursement.
        #[n(0)]
        index: ReimbursementIndex,
    },
```

**File:** rs/bitcoin/ckbtc/minter/src/state/eventlog.rs (L267-274)
```rust
        /// The minter unexpectedly panicked while processing a reimbursement.
        /// The reimbursement is quarantined to prevent any double minting and
        /// will not be processed without further manual intervention.
        #[serde(rename = "quarantined_withdrawal_reimbursement")]
        QuarantinedWithdrawalReimbursement {
            /// The burn block on the ledger for that withdrawal that should have been reimbursed
            burn_block_index: u64,
        },
```

**File:** rs/bitcoin/ckbtc/minter/src/state.rs (L1770-1777)
```rust
    /// Quarantine the reimbursement request identified by its index to prevent double minting.
    /// WARNING!: It's crucial that this method does not panic,
    /// since it's called inside the clean-up callback, when an unexpected panic did occur before.
    pub fn quarantine_withdrawal_reimbursement(&mut self, burn_index: LedgerBurnIndex) {
        self.pending_withdrawal_reimbursements.remove(&burn_index);
        self.reimbursed_withdrawals
            .insert(burn_index, Err(ReimbursedError::Quarantined));
    }
```

**File:** rs/ethereum/cketh/minter/src/state/transactions/tests.rs (L2038-2057)
```rust
        #[test]
        fn should_have_status_pending_reimbursement_for_quarantined_reimbursement() {
            let mut transactions = EthTransactions::new(TransactionNonce::ZERO);
            let mut rng = reproducible_rng();
            let [withdrawal_request] = create_ck_withdrawal_requests(&mut rng);
            let reimbursement_index = ReimbursementIndex::from(&withdrawal_request);
            let receipt = withdrawal_flow(
                &mut transactions,
                withdrawal_request,
                TransactionStatus::Failure,
            );
            transactions.record_quarantined_reimbursement(reimbursement_index.clone());

            assert_eq!(
                transactions.transaction_status(&reimbursement_index.withdrawal_id()),
                RetrieveEthStatus::TxFinalized(TxFinalizedStatus::PendingReimbursement(
                    (&receipt).into()
                ))
            );
        }
```

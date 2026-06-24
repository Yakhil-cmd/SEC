Audit Report

## Title
Quarantined Reimbursements Permanently Freeze User Funds Without Automated Recovery — (File: `rs/ethereum/cketh/minter/src/withdraw.rs`, `rs/bitcoin/ckbtc/minter/src/reimbursement/mod.rs`)

## Summary
Both the ckETH and ckBTC minters use a `scopeguard` cleanup callback to prevent double-minting during reimbursement processing. If the canister panics after a successful inter-canister mint call but before the success event is durably recorded, the guard fires and permanently transitions the reimbursement to a `Quarantined` terminal state. Because `created_at_time` is `None` in the mint `TransferArg`, the minter cannot safely retry, and no automated recovery path exists. The user's ckETH/ckBTC — already burned for the original withdrawal — is permanently frozen until a manual governance canister upgrade intervenes.

## Finding Description

**ckETH minter** (`rs/ethereum/cketh/minter/src/withdraw.rs`, lines 67–141):

The `process_reimbursement` function sets up a scopeguard before calling `client.transfer(args).await`:

```rust
let prevent_double_minting_guard = scopeguard::guard(index.clone(), |index| {
    mutate_state(|s| process_event(s, EventType::QuarantinedReimbursement { index }));
});
```

The `TransferArg` is constructed with `created_at_time: None` (line 91), meaning the ledger has no deduplication key for this mint. After `client.transfer(args).await` returns `Ok(Ok(block_index))`, the code calls:

```rust
mutate_state(|s| process_event(s, event));  // line 138
ScopeGuard::into_inner(prevent_double_minting_guard);  // line 140
```

If the canister panics between lines 138 and 140 — for example inside `record_finalized_reimbursement`, which contains `unwrap_or_else(|| panic!(...))` (line 789) and `assert_eq!` (line 791) — the IC runtime rolls back the post-await state to the pre-await snapshot, then invokes the cleanup callback. The guard fires, emitting `QuarantinedReimbursement`.

`record_quarantined_reimbursement` (lines 775–779) permanently removes the entry from `reimbursement_requests` and inserts `Err(ReimbursedError::Quarantined)` into `reimbursed`. There is no code path that re-queues a quarantined entry.

After quarantine, `transaction_status` (lines 855–901) returns `TxFinalized(PendingReimbursement(...))` indefinitely, because the `find_reimbursed_transaction_by_cketh_ledger_burn_index` check at line 871 only matches `Some(Ok(reimbursed))` — the `Err(Quarantined)` entry does not match, so the code falls through to the `PendingReimbursement` branch. This is explicitly confirmed by the test `should_have_status_pending_reimbursement_for_quarantined_reimbursement` (lines 2039–2057 of `transactions/tests.rs`).

**ckBTC minter** (`rs/bitcoin/ckbtc/minter/src/reimbursement/mod.rs`, lines 64–107):

The identical pattern exists. After `runtime.mint_ckbtc(...).await` returns `Ok(mint_index)`, the code calls:

```rust
state::mutate_state(|s| {
    state::audit::reimburse_withdrawal_completed(s, burn_index, mint_index, runtime)
});
```

`reimburse_withdrawal_completed` (state.rs lines 1785–1807) contains `assert_ne!` (line 1785) and `assert_eq!` (line 1802), both of which can panic. A panic here causes the guard to fire, calling `quarantine_withdrawal_reimbursement` (state.rs lines 1773–1777), which removes the entry from `pending_withdrawal_reimbursements` and inserts `Err(ReimbursedError::Quarantined)` into `reimbursed_withdrawals`.

After quarantine, `retrieve_btc_status_v2` (state.rs line 860) returns `RetrieveBtcStatusV2::Unknown`, silently hiding the frozen state from the user. This is confirmed by the test `should_quarantine_withdrawal_reimbursement` (state/tests.rs lines 381–408).

Both event log definitions explicitly document the terminal nature: "will not be processed without further manual intervention" (ckETH: `event.rs` lines 150–152; ckBTC: `eventlog.rs` lines 267–269).

The root cause is the combination of: (1) no `created_at_time` deduplication key preventing safe retry, (2) panic-triggering assertions inside `mutate_state` after a successful mint, and (3) no automated re-queue path for quarantined entries.

## Impact Explanation
A user who burned ckETH/ckBTC to initiate a withdrawal, whose on-chain transaction failed (triggering a reimbursement), and whose reimbursement mint succeeded at the ledger but whose minter panicked before recording the event, permanently loses their tokens with no self-service recovery. The ckETH/ckBTC was burned from the ledger; the Ethereum/Bitcoin transaction failed; and the reimbursement — though potentially minted on the ledger — is treated as quarantined by the minter. This constitutes a significant Chain Fusion / ck-token security impact with concrete, permanent user fund loss, matching the **High ($2,000–$10,000)** impact class: "Significant Chain Fusion, ck-token, ledger … security impact with concrete user or protocol harm."

## Likelihood Explanation
The panic window is narrow but realistic. After `client.transfer(args).await` returns `Ok(Ok(block_index))`, the minter must call `mutate_state(|s| process_event(s, event))`. This invokes `record_finalized_reimbursement` (ckETH) or `reimburse_withdrawal_completed` (ckBTC), both of which contain `assert_eq!` / `unwrap_or_else(|| panic!(...))` guards. Additionally, `record_event` writes to stable memory — a write that can trap on stable memory allocation failure or serialization panic. As the minter's state grows (large event log, many pending reimbursements), the probability of hitting instruction or memory limits in this window increases. No special privileges are required; any user initiating a withdrawal whose on-chain transaction fails is exposed. **Likelihood: Low-Medium.**

## Recommendation
1. **Set `created_at_time` on the mint `TransferArg`**: This gives the ledger a deduplication key, allowing the minter to safely retry the mint call after a panic. On retry, the ledger returns `Duplicate { duplicate_of: block_index }`, allowing the minter to record the correct block index instead of quarantining.
2. **Add a governance-callable recovery endpoint**: Expose a function that can re-queue a `Quarantined` reimbursement back into `reimbursement_requests` after verifying via ledger query whether the mint block index already exists.
3. **Improve user-facing status**: Replace `RetrieveBtcStatusV2::Unknown` for quarantined ckBTC withdrawals with an explicit `Quarantined` variant. For ckETH, replace the misleading `PendingReimbursement` status with an explicit quarantine indicator.

## Proof of Concept
1. User calls `withdraw_eth(amount, destination)` — ckETH is burned from the ledger.
2. The minter creates, signs, and sends an Ethereum transaction; the transaction is mined but reverts (`TransactionStatus::Failure`).
3. `record_finalized_transaction` schedules a reimbursement: entry placed in `reimbursement_requests`.
4. The periodic `process_reimbursement()` timer fires. `client.transfer(args).await` succeeds — the ckETH ledger mints the refund and returns `Ok(Ok(block_index))`.
5. Immediately after the await resumes, `mutate_state(|s| process_event(s, ReimbursedEthWithdrawal {...}))` panics (e.g., stable memory write trap due to full allocation, or an `assert_eq!` failure inside `record_finalized_reimbursement`).
6. The IC runtime rolls back the post-await state mutations; the cleanup callback fires `prevent_double_minting_guard`, emitting `QuarantinedReimbursement { index }`.
7. `record_quarantined_reimbursement` removes the entry from `reimbursement_requests` and inserts `Err(Quarantined)` into `reimbursed`.
8. The user queries `retrieve_eth_status(withdrawal_id)` and receives `TxFinalized(PendingReimbursement(...))` indefinitely. Their ckETH is permanently frozen.

A deterministic reproduction can be constructed as a PocketIC integration test by injecting a panic into `record_finalized_reimbursement` (e.g., by pre-filling stable memory to capacity) after a successful mock ledger mint response, then asserting that the reimbursement status is `Quarantined` and no recovery timer re-queues it. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5) [7](#0-6) [8](#0-7) [9](#0-8) [10](#0-9) [11](#0-10) [12](#0-11) [13](#0-12)

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

**File:** rs/ethereum/cketh/minter/src/state/transactions/mod.rs (L775-779)
```rust
    pub fn record_quarantined_reimbursement(&mut self, index: ReimbursementIndex) {
        self.reimbursement_requests.remove(&index);
        self.reimbursed
            .insert(index, Err(ReimbursedError::Quarantined));
    }
```

**File:** rs/ethereum/cketh/minter/src/state/transactions/mod.rs (L781-803)
```rust
    pub fn record_finalized_reimbursement(
        &mut self,
        index: ReimbursementIndex,
        reimbursed_in_block: LedgerMintIndex,
    ) {
        let reimbursement_request = self
            .reimbursement_requests
            .remove(&index)
            .unwrap_or_else(|| panic!("BUG: missing reimbursement request with index {index:?}"));
        let burn_in_block = index.burn_in_block();
        assert_eq!(
            self.reimbursed.insert(
                index,
                Ok(Reimbursed {
                    burn_in_block,
                    reimbursed_in_block,
                    reimbursed_amount: reimbursement_request.reimbursed_amount,
                    transaction_hash: reimbursement_request.transaction_hash,
                }),
            ),
            None
        );
    }
```

**File:** rs/ethereum/cketh/minter/src/state/transactions/mod.rs (L870-892)
```rust
        if let Some(tx) = self.finalized_tx.get_alt(burn_index) {
            if let Some(Ok(reimbursed)) =
                self.find_reimbursed_transaction_by_cketh_ledger_burn_index(burn_index)
            {
                return (
                    RetrieveEthStatus::TxFinalized(TxFinalizedStatus::Reimbursed {
                        reimbursed_in_block: reimbursed.reimbursed_in_block.get().into(),
                        transaction_hash: tx.transaction_hash().to_string(),
                        reimbursed_amount: reimbursed.reimbursed_amount.into(),
                    }),
                    Some(tx.as_ref()),
                );
            }
            if tx.transaction_status() == &TransactionStatus::Failure {
                return (
                    RetrieveEthStatus::TxFinalized(TxFinalizedStatus::PendingReimbursement(
                        EthTransaction {
                            transaction_hash: tx.transaction_hash().to_string(),
                        },
                    )),
                    Some(tx.as_ref()),
                );
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

**File:** rs/bitcoin/ckbtc/minter/src/state.rs (L851-862)
```rust
        if let Some(maybe_reimbursed) = self.reimbursed_withdrawals.get(&block_index) {
            return match maybe_reimbursed {
                Ok(reimbursement) => RetrieveBtcStatusV2::Reimbursed(ReimbursedDeposit {
                    account: reimbursement.account,
                    amount: reimbursement.amount,
                    reason: map_reimbursement_reason(&reimbursement.reason),
                    mint_block_index: reimbursement.mint_block_index,
                }),
                Err(err) => match err {
                    ReimbursedError::Quarantined => RetrieveBtcStatusV2::Unknown,
                },
            };
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

**File:** rs/bitcoin/ckbtc/minter/src/state.rs (L1785-1807)
```rust
        assert_ne!(
            burn_index, mint_index,
            "BUG: mint index cannot be the same as the burn index"
        );

        let reimbursement = self
            .pending_withdrawal_reimbursements
            .remove(&burn_index)
            .unwrap_or_else(|| {
                panic!("BUG: missing pending reimbursement of withdrawal {burn_index}.")
            });
        let reimbursed = ReimbursedWithdrawal {
            account: reimbursement.account,
            amount: reimbursement.amount,
            reason: reimbursement.reason.clone(),
            mint_block_index: mint_index,
        };
        assert_eq!(
            self.reimbursed_withdrawals
                .insert(burn_index, Ok(reimbursed)),
            None,
            "BUG: Reimbursement of withdrawal {reimbursement:?} was already completed!"
        );
```

**File:** rs/bitcoin/ckbtc/minter/src/state/tests.rs (L381-408)
```rust
    #[test]
    fn should_quarantine_withdrawal_reimbursement() {
        let mut state = CkBtcMinterState::from(init_args());
        let ledger_burn_index = 1;
        let amount_to_reimburse = 1_000;
        let ledger_account = ledger_account();
        state.schedule_withdrawal_reimbursement(
            ledger_burn_index,
            reimburse_withdrawal_task(ledger_account, amount_to_reimburse),
        );

        assert_status_v1_unknown(&state, ledger_burn_index);
        assert_matches!(
            state.retrieve_btc_status_v2(ledger_burn_index),
            RetrieveBtcStatusV2::WillReimburse(reimbursement) if
            reimbursement.account == ledger_account &&
            reimbursement.amount == amount_to_reimburse
        );

        state.quarantine_withdrawal_reimbursement(ledger_burn_index);

        assert_eq!(state.pending_withdrawal_reimbursements, BTreeMap::default());
        assert_status_v1_unknown(&state, ledger_burn_index);
        assert_eq!(
            state.retrieve_btc_status_v2(ledger_burn_index),
            RetrieveBtcStatusV2::Unknown
        );
    }
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

### Title
Quarantined Deposits and Reimbursements in ckETH/ckBTC Minters Permanently Lock User Funds Without Automated Recovery - (`rs/ethereum/cketh/minter/src/withdraw.rs`, `rs/ethereum/cketh/minter/src/deposit.rs`, `rs/bitcoin/ckbtc/minter/src/reimbursement/mod.rs`)

---

### Summary

The ckETH and ckBTC chain-fusion minters implement a quarantine mechanism to prevent double-minting when an unexpected panic occurs during async ledger callbacks. When triggered, the quarantine permanently removes the affected deposit or reimbursement from automated processing with no user-accessible recovery path. This is the IC analog of the TokenBridge "no withdrawal mechanism" vulnerability: user funds (deposited ETH/ERC20 or burned ckETH/ckERC20/ckBTC) become permanently locked until an NNS governance proposal manually intervenes.

---

### Finding Description

**ckETH Minter — `QuarantinedDeposit`**

In `rs/ethereum/cketh/minter/src/deposit.rs`, the `mint()` function processes Ethereum deposit events. Before each async ledger mint call, a `scopeguard` is armed:

```rust
let prevent_double_minting_guard = scopeguard::guard(event.clone(), |event| {
    mutate_state(|s| {
        process_event(s, EventType::QuarantinedDeposit { event_source: event.source() })
    });
});
```

If the minter panics at any point after the guard is armed but before `ScopeGuard::into_inner(prevent_double_minting_guard)` is called (including during the inter-canister `client.transfer()` await), the guard fires and records a `QuarantinedDeposit` event. The user's ETH/ERC20 is already held in the minter's Ethereum address, but no ckETH/ckERC20 is minted. The event log comment states explicitly: *"will not be processed without further manual intervention."* [1](#0-0) [2](#0-1) 

**ckETH Minter — `QuarantinedReimbursement`**

In `rs/ethereum/cketh/minter/src/withdraw.rs`, `process_reimbursement()` handles reimbursements for failed ETH/ERC20 withdrawals. The same guard pattern is used:

```rust
let prevent_double_minting_guard = scopeguard::guard(index.clone(), |index| {
    mutate_state(|s| process_event(s, EventType::QuarantinedReimbursement { index }));
});
```

If the minter panics after the ledger `client.transfer(args).await` call but before the guard is defused, the reimbursement is quarantined. The user's previously burned ckETH/ckERC20 is permanently unrecoverable through any automated path. [3](#0-2) [4](#0-3) 

The `ReimbursedError::Quarantined` state is terminal — `record_quarantined_reimbursement()` removes the request from `reimbursement_requests` and inserts it into `reimbursed` as `Err(Quarantined)`, making it invisible to the retry loop: [5](#0-4) 

**ckBTC Minter — `QuarantinedWithdrawalReimbursement`**

The same pattern exists in `rs/bitcoin/ckbtc/minter/src/reimbursement/mod.rs`. If the minter panics during `runtime.mint_ckbtc()`, the reimbursement is quarantined via `quarantine_withdrawal_reimbursement()`. The user's burned ckBTC is permanently lost, and `retrieve_btc_status_v2()` returns `RetrieveBtcStatusV2::Unknown` — providing no indication to the user that their funds are stuck: [6](#0-5) [7](#0-6) 

---

### Impact Explanation

- **`QuarantinedDeposit`**: User's ETH or ERC-20 tokens are held in the minter's threshold-ECDSA-controlled Ethereum address. No ckETH/ckERC20 is minted. The user loses both their deposited asset and the corresponding IC token. Recovery requires an NNS governance proposal to upgrade the minter with a targeted fix.
- **`QuarantinedReimbursement` (ckETH/ckBTC)**: User's ckETH, ckERC20, or ckBTC was already burned on the IC ledger. The corresponding Ethereum/Bitcoin transaction failed. The reimbursement mint never completes. The user loses the burned token amount permanently. For ckBTC, the status silently becomes `Unknown`, giving the user no actionable information.

In both cases there is **no user-callable endpoint** to trigger recovery, cancel the quarantine, or retrieve funds. This is structurally identical to the TokenBridge report: funds are locked in the bridge with no withdrawal path.

---

### Likelihood Explanation

Panics in async inter-canister callbacks are a known failure mode on the IC. The ckBTC minter has experienced production stuck-withdrawal incidents requiring emergency NNS upgrade proposals (e.g., a deterministic panic during transaction resubmission documented in `rs/bitcoin/ckbtc/mainnet/minter_upgrade_2025_06_27.md` and duplicate-outpoint panics in `rs/bitcoin/ckbtc/mainnet/minter_upgrade_2026_03_20.md`). The quarantine guard is specifically designed to handle such panics, confirming the scenario is considered realistic by the developers. Any user who deposits ETH/ERC20 or initiates a ckETH/ckERC20/ckBTC withdrawal is exposed to this risk if a panic occurs during their callback window. [8](#0-7) [9](#0-8) 

---

### Recommendation

1. **Add a governance-accessible recovery endpoint** on the ckETH and ckBTC minters that can re-queue a quarantined deposit or reimbursement for retry, using the existing `created_at_time` idempotency field on ICRC-1 transfers to safely retry without double-minting.
2. **Expose quarantined items in status queries** so users and monitoring systems can detect stuck funds. Currently `QuarantinedReimbursement` in ckBTC returns `Unknown`, hiding the problem entirely.
3. **Emit an alert metric** when a quarantine event is recorded, enabling automated detection and faster manual response.
4. **Consider using `created_at_time`** in the ledger transfer args within `process_reimbursement()` and `mint()` so that a retry after a quarantine can be proven idempotent, allowing safe automated re-processing without NNS intervention.

---

### Proof of Concept

**ckETH `QuarantinedReimbursement` path:**

1. User calls `withdraw_eth` or `withdraw_erc20` on the ckETH minter. ckETH/ckERC20 is burned. An Ethereum transaction is submitted.
2. The Ethereum transaction fails (e.g., reverted on-chain). The minter records a `reimbursement_request`.
3. The minter's timer fires `process_reimbursement()`. The `prevent_double_minting_guard` is armed for the reimbursement index.
4. The minter calls `client.transfer(args).await` to mint the reimbursement on the ckETH ledger.
5. A panic occurs in the minter (e.g., due to a bug triggered by the ledger response, as has happened in production with ckBTC). The IC runtime rolls back the in-progress state mutation.
6. The `prevent_double_minting_guard` destructor fires, recording `EventType::QuarantinedReimbursement { index }` and calling `record_quarantined_reimbursement()`.
7. The reimbursement is removed from `reimbursement_requests` and inserted into `reimbursed` as `Err(ReimbursedError::Quarantined)`.
8. The user's burned ckETH/ckERC20 is permanently unrecoverable. No user-callable endpoint exists to re-trigger the reimbursement. The minter dashboard shows the withdrawal as "Quarantined." [10](#0-9) [11](#0-10)

### Citations

**File:** rs/ethereum/cketh/minter/src/deposit.rs (L40-52)
```rust
    for event in events {
        // Ensure that even if we were to panic in the callback, after having contacted the ledger to mint the tokens,
        // this event will not be processed again.
        let prevent_double_minting_guard = scopeguard::guard(event.clone(), |event| {
            mutate_state(|s| {
                process_event(
                    s,
                    EventType::QuarantinedDeposit {
                        event_source: event.source(),
                    },
                )
            });
        });
```

**File:** rs/ethereum/cketh/minter/src/state/event.rs (L141-149)
```rust
    /// The minter unexpectedly panic while processing a deposit.
    /// The deposit is quarantined to prevent any double minting and
    /// will not be processed without further manual intervention.
    #[n(21)]
    QuarantinedDeposit {
        /// The unique identifier of the deposit on the Ethereum network.
        #[n(0)]
        event_source: EventSource,
    },
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

**File:** rs/ethereum/cketh/minter/src/withdraw.rs (L46-141)
```rust
pub async fn process_reimbursement() {
    let _guard = match TimerGuard::new(TaskType::Reimbursement) {
        Ok(guard) => guard,
        Err(e) => {
            log!(DEBUG, "Failed retrieving reimbursement guard: {e:?}",);
            return;
        }
    };

    let reimbursements: Vec<(ReimbursementIndex, ReimbursementRequest)> = read_state(|s| {
        s.eth_transactions
            .reimbursement_requests_iter()
            .map(|(index, request)| (index.clone(), request.clone()))
            .collect()
    });
    if reimbursements.is_empty() {
        return;
    }

    let mut error_count = 0;

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

**File:** rs/ethereum/cketh/minter/src/state/transactions/mod.rs (L775-779)
```rust
    pub fn record_quarantined_reimbursement(&mut self, index: ReimbursementIndex) {
        self.reimbursement_requests.remove(&index);
        self.reimbursed
            .insert(index, Err(ReimbursedError::Quarantined));
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

**File:** rs/bitcoin/ckbtc/minter/src/state.rs (L859-862)
```rust
                Err(err) => match err {
                    ReimbursedError::Quarantined => RetrieveBtcStatusV2::Unknown,
                },
            };
```

**File:** rs/bitcoin/ckbtc/mainnet/minter_upgrade_2025_06_27.md (L19-33)
```markdown
Upgrade the ckBTC minter to try to unblock three transactions ckBTC → BTC (withdrawals) that are currently stuck since
2025.06.21.

After analysis, see this
forum [**post**](https://forum.dfinity.org/t/ckbtc-a-canister-issued-bitcoin-twin-token-on-the-ic-1-1-backed-by-btc/17606/202)
for more details, the problem appears to be due to the following:

1. An extremely low fee per vbyte was chosen by the minter for those transactions, which prevented them from being mined
   in the first place. We currently don’t have a satisfying explanation for how this low median fee was computed and are
   also investigating the bitcoin canister. A stop-gap solution was introduced
   in [#5742](https://github.com/dfinity/ic/pull/5742), to ensure that the fee per vbyte computed by the minter is
   always at least 1.5 sats/vbyte (for Bitcoin Mainnet).
2. There is a deterministic panic occurring in the minter when it tries to resubmit those transactions, which explains
   why those transactions are currently stuck. This should be completely fixed
   by [#5713](https://github.com/dfinity/ic/pull/5713).
```

**File:** rs/bitcoin/ckbtc/mainnet/minter_upgrade_2026_03_20.md (L19-28)
```markdown
Due to the security incident explained in this [forum post](https://forum.dfinity.org/t/proposal-140929-to-upgrade-the-ckbtc-minter/65401/3), the following ckBTC withdrawals (ckBTC -> BTC) are currently stuck:

* [3459007](https://dashboard.internetcomputer.org/bitcoin/transaction/3459007), [3459009](https://dashboard.internetcomputer.org/bitcoin/transaction/3459009), and [3459013](https://dashboard.internetcomputer.org/bitcoin/transaction/3459013) because the transaction from the minter tries to reuse the already spent output [`91bb46443799335076fbcd117f2295c7499d02dd3a59c22a531d31591114b303:5`](https://mempool.space/tx/91bb46443799335076fbcd117f2295c7499d02dd3a59c22a531d31591114b303#vout=5).
* [3489347](https://dashboard.internetcomputer.org/bitcoin/transaction/3489347) and [3489353](https://dashboard.internetcomputer.org/bitcoin/transaction/3489353) because the transaction from the minter tries to reuse the already spent output [`8942e5ef0d4ace158a4fddd5153d320701bd13370ff8fecef13795cdd8ff1dc5:1`](https://mempool.space/tx/8942e5ef0d4ace158a4fddd5153d320701bd13370ff8fecef13795cdd8ff1dc5#vout=1).

This proposal should address these issues by:
* Removing the duplicate outpoints from the minter's state.
* Discarding any transaction sent by the minter to the Bitcoin network that uses one of the duplicate outpoints. This is safe to do because those transactions are invalid and will never be accepted by the Bitcoin network.

The expected result is that the aforementioned withdrawals are considered as pending by the minter, as if they were going to be processed by the minter for the first time.
```

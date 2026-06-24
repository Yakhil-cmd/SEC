### Title
`get_total_btc_managed()` Omits Pending Reimbursement Funds, Making the BTC Conservation Metric Misleading During Withdrawal Cancellation — (File: `rs/bitcoin/ckbtc/minter/src/state.rs`)

---

### Summary

The ckBTC minter's `get_total_btc_managed()` function, which is the closest IC analog to the `fundsSafu`/`Invariable` pattern described in the report, does not account for ckBTC amounts that have been burned from users (withdrawal requests accepted) but are pending re-minting back to users via `pending_withdrawal_reimbursements`. During the window between a withdrawal being cancelled/rejected and the reimbursement mint completing, the reported "total BTC managed" figure is understated relative to the outstanding ckBTC supply, creating a transient conservation gap.

---

### Finding Description

In `rs/bitcoin/ckbtc/minter/src/state.rs`, `get_total_btc_managed()` computes the minter's BTC holdings as:

```
available_utxos (sum of values) + submitted_transactions change_output values
``` [1](#0-0) 

This value is exposed as the `ckbtc_minter_btc_balance` metric and as "Total BTC managed" in the dashboard: [2](#0-1) [3](#0-2) 

When a withdrawal request is cancelled (e.g., due to `TooManyInputs`), the flow is:

1. User's ckBTC is **burned** from the ledger (reducing ckBTC supply).
2. The minter records a `pending_withdrawal_reimbursements` entry — the amount to be re-minted to the user.
3. The minter later calls `mint_ckbtc` to reimburse the user. [4](#0-3) [5](#0-4) 

During step 2→3, the `pending_withdrawal_reimbursements` amounts are **not included** in `get_total_btc_managed()`. The `check_invariants()` implementation in `rs/bitcoin/ckbtc/minter/src/state/invariants.rs` also does not verify any conservation relationship between `tokens_minted - tokens_burned` and `get_total_btc_managed() + pending_reimbursements`: [6](#0-5) 

Similarly, `pending_reimbursements` (deposit check-fee reimbursements) are also absent from `get_total_btc_managed()`: [7](#0-6) 

---

### Impact Explanation

The `get_total_btc_managed()` value is used as the primary observable conservation metric for the ckBTC minter. When `pending_withdrawal_reimbursements` or `pending_reimbursements` are non-empty, the metric reports a value lower than the actual outstanding ckBTC obligations. Any monitoring, alerting, or off-chain tooling that uses `ckbtc_minter_btc_balance` to verify that BTC backing ≥ ckBTC supply will see a false deficit during the reimbursement window. This is a **ledger conservation accounting bug**: the invariant that `get_total_btc_managed() ≥ tokens_minted - tokens_burned` is not enforced and is transiently false when reimbursements are pending.

The `check_invariants()` function, which is the IC analog of `fundsSafu`/`Invariable`, does not check this conservation property at all, meaning the gap is invisible to the self-check mechanism.

---

### Likelihood Explanation

This is reachable by any unprivileged user who calls `retrieve_btc_with_approval` with an amount that triggers the `TooManyInputs` cancellation path (i.e., requesting withdrawal of more UTXOs than `max_num_inputs_in_transaction` allows). This is a normal, documented code path. The reimbursement window persists until the next timer tick processes `reimburse_withdrawals`. During that window, the conservation metric is incorrect. [8](#0-7) 

---

### Recommendation

1. Include `pending_reimbursements` and `pending_withdrawal_reimbursements` amounts in `get_total_btc_managed()` so the metric accurately reflects all outstanding ckBTC obligations at all times.
2. Add a conservation invariant to `CheckInvariantsImpl::check_invariants` asserting:
   ```
   get_total_btc_managed() + sum(pending_reimbursements) + sum(pending_withdrawal_reimbursements) >= tokens_minted - tokens_burned
   ```
   (modulo fees and tainted/suspended UTXOs).

---

### Proof of Concept

1. User deposits BTC → ckBTC minted, `tokens_minted` increases, `available_utxos` increases.
2. User calls `retrieve_btc_with_approval` with amount requiring > `max_num_inputs_in_transaction` UTXOs.
3. Minter accepts the request: ckBTC burned from ledger, `tokens_burned` increases.
4. Minter attempts to build BTC transaction → fails with `TooManyInputs`.
5. Minter calls `schedule_withdrawal_reimbursement` → entry added to `pending_withdrawal_reimbursements`.
6. At this point: `get_total_btc_managed()` = `available_utxos` (unchanged, UTXOs were never consumed), but `tokens_minted - tokens_burned` is now less by the withdrawal amount. The pending reimbursement amount is not reflected anywhere in `get_total_btc_managed()`, so the metric shows BTC > ckBTC obligations — but the ckBTC that was burned will be re-minted, meaning the true obligation is higher than what `get_total_btc_managed()` implies.
7. Any invariant check or monitoring during this window sees a misleading conservation figure. [1](#0-0) [9](#0-8) [6](#0-5)

### Citations

**File:** rs/bitcoin/ckbtc/minter/src/state.rs (L559-578)
```rust
    /// Map from burn block index to amount to reimburse because of
    /// check fees.
    pub pending_reimbursements: BTreeMap<u64, ReimburseDepositTask>,

    /// Map from burn block index to the the reimbursed request.
    pub reimbursed_transactions: BTreeMap<u64, ReimbursedDeposit>,

    /// Map from burn block index to the pending reimbursed withdrawal request.
    ///
    /// # Requirement
    ///
    /// A withdrawal request should only be reimbursed
    /// when it is certain that no Bitcoin transactions for that withdrawal will ever make it. That means,
    /// 1. Either the minter never issued a Bitcoin transaction including that withdrawal request;
    /// 2. Or it's guaranteed that such a transaction is no longer valid because some of its UTXOs
    ///    have been used by another transaction that is considered finalized in the meantime.
    pub pending_withdrawal_reimbursements: BTreeMap<LedgerBurnIndex, ReimburseWithdrawalTask>,

    /// Map from burn block index to the reimbursed withdrawal request.
    pub reimbursed_withdrawals: BTreeMap<LedgerBurnIndex, ReimbursedWithdrawalResult>,
```

**File:** rs/bitcoin/ckbtc/minter/src/state.rs (L1735-1744)
```rust
    pub fn get_total_btc_managed(&self) -> u64 {
        let mut total_btc = 0_u64;
        for req in self.submitted_transactions.iter() {
            if let Some(change_output) = &req.change_output {
                total_btc += change_output.value;
            }
        }
        total_btc += self.available_utxos.iter().map(|u| u.value).sum::<u64>();
        total_btc
    }
```

**File:** rs/bitcoin/ckbtc/minter/src/metrics.rs (L311-315)
```rust
    metrics.encode_gauge(
        "ckbtc_minter_btc_balance",
        state::read_state(|s| s.get_total_btc_managed()) as f64,
        "Total BTC amount locked in available UTXOs.",
    )?;
```

**File:** rs/bitcoin/ckbtc/minter/src/dashboard.rs (L387-412)
```rust
                        <th>Total {native_token} managed</th>
                        <td>{}</td>
                    </tr>
                </tbody>
            </table>",
            s.btc_network,
            s.ecdsa_public_key
                .clone()
                .map(|key| {
                    let main_account = Account {
                        owner: ic_cdk::api::canister_self(),
                        subaccount: None,
                    };
                    self.builder.display_account_address(&key, &main_account)
                })
                .unwrap_or_default(),
            s.min_confirmations,
            s.ledger_id,
            s.btc_checker_principal
                .map(|p| p.to_string())
                .unwrap_or_else(|| "N/A".to_string()),
            DisplayAmount(s.effective_deposit_min_btc_amount()),
            DisplayAmount(s.check_fee),
            DisplayAmount(s.retrieve_btc_min_amount),
            DisplayAmount(s.fee_based_retrieve_btc_min_amount),
            DisplayAmount(s.get_total_btc_managed())
```

**File:** rs/bitcoin/ckbtc/minter/src/reimbursement/mod.rs (L58-116)
```rust
pub async fn reimburse_withdrawals<R: CanisterRuntime>(runtime: &R) {
    if state::read_state(|s| s.pending_withdrawal_reimbursements.is_empty()) {
        return;
    }
    let pending_reimbursements = state::read_state(|s| s.pending_withdrawal_reimbursements.clone());
    let mut error_count = 0;
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
    }

    if error_count > 0 {
        log!(
            Priority::Info,
            "[reimburse_withdrawals] Failed to reimburse {error_count} withdrawal requests, retrying later."
        );
    }
}
```

**File:** rs/bitcoin/ckbtc/minter/src/state/invariants.rs (L10-96)
```rust
impl CheckInvariants for CheckInvariantsImpl {
    fn check_invariants(state: &CkBtcMinterState) -> Result<(), String> {
        for utxo in state.available_utxos.iter() {
            ensure!(
                state.outpoint_account.contains_key(&utxo.outpoint),
                "the output_account map is missing an entry for {:?}",
                utxo.outpoint
            );

            ensure!(
                state
                    .utxos_state_addresses
                    .iter()
                    .any(|(_, utxos)| utxos.contains(utxo)),
                "available utxo {:?} does not belong to any account",
                utxo
            );
        }

        for (addr, utxos) in state.utxos_state_addresses.iter() {
            for utxo in utxos.iter() {
                ensure_eq!(
                    state.outpoint_account.get(&utxo.outpoint),
                    Some(addr),
                    "missing outpoint account for {:?}",
                    utxo.outpoint
                );
            }
        }

        for (l, r) in state
            .pending_retrieve_btc_requests
            .iter()
            .zip(state.pending_retrieve_btc_requests.iter().skip(1))
        {
            ensure!(
                l.received_at <= r.received_at,
                "pending retrieve_btc requests are not sorted by receive time"
            );
        }

        for tx in &state.stuck_transactions {
            ensure!(
                state.replacement_txid.contains_key(&tx.txid),
                "stuck transaction {} does not have a replacement id",
                &tx.txid,
            );
        }

        for (old_txid, new_txid) in &state.replacement_txid {
            ensure!(
                state
                    .stuck_transactions
                    .iter()
                    .any(|tx| &tx.txid == old_txid),
                "not found stuck transaction {}",
                old_txid,
            );

            ensure!(
                state
                    .submitted_transactions
                    .iter()
                    .chain(state.stuck_transactions.iter())
                    .any(|tx| &tx.txid == new_txid),
                "not found replacement transaction {}",
                new_txid,
            );
        }

        ensure_eq!(
            state.replacement_txid.len(),
            state.rev_replacement_txid.len(),
            "direct and reverse TX replacement links don't match"
        );
        for (old_txid, new_txid) in &state.replacement_txid {
            ensure_eq!(
                state.rev_replacement_txid.get(new_txid),
                Some(old_txid),
                "no back link for {} -> {} TX replacement",
                old_txid,
                new_txid,
            );
        }

        Ok(())
    }
```

**File:** rs/bitcoin/ckbtc/minter/tests/tests.rs (L3108-3173)
```rust
#[test]
fn should_cancel_and_reimburse_large_withdrawal() {
    let ckbtc = CkBtcSetup::new();
    let user = Principal::from(ckbtc.caller);
    let subaccount: Option<[u8; 32]> = Some([1; 32]);
    let user_account = Account {
        owner: user,
        subaccount,
    };

    // Step 1: deposit enough small UTXOs to exceed the max inputs limit.
    // We need at least max + 1 UTXOs for the withdrawal to trigger TooManyInputs,
    // plus a small buffer so there are leftover UTXOs in the set.
    const MAX_INPUTS: usize = ic_ckbtc_minter::state::DEFAULT_MAX_NUM_INPUTS_IN_TRANSACTION;
    const NUM_UTXOS: usize = MAX_INPUTS + 100;
    let deposit_value = 100_000_u64;
    let _deposited_utxos =
        ckbtc.deposit_utxos_with_value(user_account, &[deposit_value; NUM_UTXOS]);
    let balance_after_deposit = ckbtc.balance_of(user_account);
    assert_eq!(
        balance_after_deposit,
        Nat::from(NUM_UTXOS as u64 * (deposit_value - CHECK_FEE))
    );

    let withdrawal_amount = (MAX_INPUTS as u64 + 1) * deposit_value;
    ckbtc.approve_minter(user, withdrawal_amount, subaccount);
    let balance_before_withdrawal = ckbtc.balance_of(user_account);

    let RetrieveBtcOk { block_index } = ckbtc
        .retrieve_btc_with_approval(
            WITHDRAWAL_ADDRESS.to_string(),
            withdrawal_amount,
            subaccount,
        )
        .expect("retrieve_btc failed");

    let balance_after_withdrawal = ckbtc.balance_of(user_account);
    assert_eq!(
        balance_after_withdrawal,
        balance_before_withdrawal.clone() - Nat::from(withdrawal_amount)
    );

    assert_eq!(
        ckbtc.retrieve_btc_status_v2(block_index),
        RetrieveBtcStatusV2::Pending
    );

    ckbtc.env.advance_time(MAX_TIME_IN_QUEUE);

    let mempool = ckbtc.mempool();
    assert_eq!(
        mempool.len(),
        0,
        "no transaction should appear when being reimbursed"
    );

    let reimbursement_block_index = block_index + 1;
    let reimbursement_amount = withdrawal_amount - BitcoinFeeEstimator::COST_OF_ONE_BILLION_CYCLES;

    assert_matches!(
        ckbtc.retrieve_btc_status_v2(block_index),
        RetrieveBtcStatusV2::Reimbursed(reimbursement) if
        reimbursement.account == user_account &&
        reimbursement.amount == reimbursement_amount &&
        reimbursement.mint_block_index == reimbursement_block_index
    );
```

### Title
No On-Chain Recovery Mechanism for Quarantined ckBTC Withdrawal Reimbursements — (`rs/bitcoin/ckbtc/minter/src/reimbursement/mod.rs`)

### Summary

The ckBTC minter's withdrawal reimbursement flow contains a terminal quarantine state (`ReimbursedError::Quarantined`) that permanently locks a user's ckBTC without any on-chain recovery path. When an unexpected panic occurs during the `reimburse_withdrawals` async callback, the minter quarantines the reimbursement to prevent double-minting. The quarantined entry is then reported as `RetrieveBtcStatusV2::Unknown` to the user, and no canister endpoint exists to release or retry the quarantined funds. Recovery requires a privileged NNS governance upgrade proposal to manually patch state — an off-chain, centralized intervention with no user-accessible remedy.

### Finding Description

The ckBTC minter implements a two-phase withdrawal flow: a user burns ckBTC via `retrieve_btc_with_approval`, and the minter sends BTC on-chain. If the BTC transaction cannot be sent (e.g., `TooManyInputs` cancellation), the minter schedules a reimbursement via `schedule_withdrawal_reimbursement`, placing the entry in `pending_withdrawal_reimbursements`.

The `reimburse_withdrawals` async function then attempts to mint ckBTC back to the user. To guard against double-minting in case of a panic during the async callback, a `scopeguard` is installed before the `mint_ckbtc` await:

```rust
let prevent_double_minting_guard = scopeguard::guard(burn_index, |index| {
    state::mutate_state(|s| {
        state::audit::quarantine_withdrawal_reimbursement(s, index, runtime)
    });
});
```

If a panic occurs at any point during or after the `mint_ckbtc` call (before `ScopeGuard::into_inner` is reached), the guard fires and calls `quarantine_withdrawal_reimbursement`:

```rust
pub fn quarantine_withdrawal_reimbursement(&mut self, burn_index: LedgerBurnIndex) {
    self.pending_withdrawal_reimbursements.remove(&burn_index);
    self.reimbursed_withdrawals
        .insert(burn_index, Err(ReimbursedError::Quarantined));
}
```

This permanently removes the entry from `pending_withdrawal_reimbursements` and inserts `Err(ReimbursedError::Quarantined)` into `reimbursed_withdrawals`. The status query then returns `RetrieveBtcStatusV2::Unknown` to the user:

```rust
Err(err) => match err {
    ReimbursedError::Quarantined => RetrieveBtcStatusV2::Unknown,
},
```

The code itself documents this as requiring "manual intervention":

> "The reimbursement request is quarantined to avoid any double minting and will not be further processed without manual intervention."

There is no canister endpoint (query or update) that allows the user, or even an admin, to retry or release a quarantined reimbursement. The only recovery path is an NNS governance upgrade proposal that patches the minter's state — as demonstrated by the real-world incident documented in `rs/bitcoin/ckbtc/mainnet/minter_upgrade_2026_03_20.md`, where stuck withdrawals required an NNS proposal to manually fix minter state.

### Impact Explanation

A user who initiates a ckBTC → BTC withdrawal that is subsequently cancelled (e.g., due to `TooManyInputs`) and whose reimbursement minting callback panics will have their ckBTC permanently locked. The ckBTC tokens were already burned from the ledger at withdrawal time. The minter holds the accounting obligation to re-mint them. Once quarantined, the minter will never re-mint them automatically, and the user has no on-chain mechanism to trigger recovery. The user's funds are effectively destroyed unless an NNS governance proposal is submitted and passed to manually fix the minter state — a process that is slow, centralized, and not guaranteed.

**Impact class:** chain-fusion mint/burn/replay bug — ledger conservation violation (tokens burned, never re-minted).

### Likelihood Explanation

The trigger requires two conditions to coincide:
1. A withdrawal is cancelled and enters the `pending_withdrawal_reimbursements` queue (e.g., via `TooManyInputs` cancellation — a real production path, as shown by `should_cancel_and_reimburse_large_withdrawal` test).
2. An unexpected panic occurs during the `reimburse_withdrawals` async callback after the `mint_ckbtc` inter-canister call is dispatched.

Condition 2 is low-probability under normal operation, but the code explicitly acknowledges it as a real scenario (the guard exists precisely because it can happen). The real-world upgrade proposals (`minter_upgrade_2025_06_27.md`, `minter_upgrade_2026_03_20.md`) confirm that the ckBTC minter has experienced production panics that required emergency NNS upgrades. The combination is low-to-medium likelihood, but the impact when it occurs is total and permanent loss of user funds with no self-service remedy.

### Recommendation

1. **Add a retry endpoint**: Expose an update method (callable by the affected user or any caller) that, given a `burn_block_index`, checks if the entry is `Err(ReimbursedError::Quarantined)` and, if so, re-queues it for minting. The double-mint risk can be mitigated by checking the ckBTC ledger for an existing mint with the matching `ReimburseWithdrawal` memo before re-minting.
2. **Improve panic safety**: Use a two-phase commit pattern — record a `MintAttempted` event before the `mint_ckbtc` call, then query the ledger on recovery to determine whether the mint succeeded before deciding to quarantine vs. complete.
3. **Emit an observable event**: At minimum, emit a metric or alert when a quarantine occurs so that operators can detect and respond to affected users promptly.

### Proof of Concept

**Entry path (unprivileged user):**
1. User calls `retrieve_btc_with_approval` with an amount requiring more than `max_num_inputs_in_transaction` UTXOs.
2. Minter accepts the burn, records `AcceptedRetrieveBtcRequest`, then during batch processing detects `TooManyInputs` and calls `reimburse_withdrawal` → `schedule_withdrawal_reimbursement`.
3. The periodic `reimburse_withdrawals` task runs, installs the scopeguard, and calls `mint_ckbtc` (inter-canister call to the ckBTC ledger).
4. A panic occurs in the callback (e.g., due to a bug in state mutation, as seen in production in June 2025).
5. The scopeguard fires → `quarantine_withdrawal_reimbursement` is called → `reimbursed_withdrawals[burn_index] = Err(Quarantined)`.
6. User queries `retrieve_btc_status_v2(burn_index)` → receives `Unknown`.
7. User has no further recourse. ckBTC is permanently lost without an NNS upgrade.

**Relevant code locations:** [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5)

### Citations

**File:** rs/bitcoin/ckbtc/minter/src/reimbursement/mod.rs (L30-36)
```rust
#[derive(Clone, Eq, PartialEq, Debug)]
pub enum ReimbursedError {
    /// Whether reimbursement was minted or not is unknown,
    /// most likely because there was an unexpected panic in the callback.
    /// The reimbursement request is quarantined to avoid any double minting and
    /// will not be further processed without manual intervention.
    Quarantined,
```

**File:** rs/bitcoin/ckbtc/minter/src/reimbursement/mod.rs (L57-108)
```rust
/// Reimburse withdrawals that were canceled.
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

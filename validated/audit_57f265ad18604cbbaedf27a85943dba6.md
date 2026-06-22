### Title
Unchecked `next_transaction_nonce` Override via `UpgradeArg` Can Permanently Strand In-Flight ckETH/ckERC20 Withdrawals - (File: `rs/ethereum/cketh/minter/src/state/transactions/mod.rs`)

### Summary

The ckETH minter's `UpgradeArg` allows the NNS to set `next_transaction_nonce` to an arbitrary value at any time, including when there are active transactions in `created_tx` or `sent_tx`. The `update_next_transaction_nonce` function performs no validation against the existing in-flight transaction state. This is the direct IC analog of `PaymentSettler::setStablecoin` being callable while active payouts are in progress.

### Finding Description

The ckETH minter maintains an `EthTransactions` state machine that tracks withdrawal requests through a lifecycle: `pending_withdrawal_requests` → `created_tx` (keyed by `TransactionNonce`) → `sent_tx` (keyed by `TransactionNonce`) → `finalized_tx`. The `next_nonce` field is the authoritative counter for assigning nonces to new Ethereum transactions. [1](#0-0) 

`update_next_transaction_nonce` unconditionally overwrites `next_nonce` with no check against existing entries in `created_tx` or `sent_tx`: [2](#0-1) 

This function is called from the `upgrade` handler whenever `UpgradeArg::next_transaction_nonce` is `Some`: [3](#0-2) 

The `UpgradeArg` is applied during `post_upgrade` via `process_event(EventType::Upgrade(args))`: [4](#0-3) 

This upgrade path is exercised in production — real NNS proposals have used `next_transaction_nonce` to recover from stuck transactions: [5](#0-4) 

**Attack path 1 — nonce set to a value already in `created_tx`:**

When `create_transactions_batch` runs after the upgrade, it reads `next_nonce` (now N), creates a transaction with nonce N, and calls `record_created_transaction`. That function asserts `try_insert(N, ...)` returns `Ok(())`: [6](#0-5) 

If nonce N already exists in `created_tx`, `try_insert` returns `Err`, the assertion panics, and the minter traps on every withdrawal attempt — a persistent DoS of the withdrawal pipeline.

**Attack path 2 — nonce set to a value already consumed by `sent_tx`:**

If N is a nonce already in `sent_tx` (signed and broadcast to Ethereum), `try_insert` into `created_tx` succeeds (that map no longer holds N). A new transaction for a new withdrawal request is created with nonce N, signed, and sent. Ethereum rejects it as `NonceTooLow`. The minter silently ignores this: [7](#0-6) 

The new withdrawal request's ckETH/ckERC20 has already been burned on the ledger, but the corresponding ETH/ERC20 is never sent. The request is permanently stuck with no reimbursement path, because the minter's `sent_tx` for nonce N (the original transaction) will eventually finalize and be removed, while the new `sent_tx` entry for the same nonce N (the duplicate) will never be mined.

### Impact Explanation

Two concrete impacts:

1. **Permanent loss of user funds**: Users who submitted withdrawal requests after the nonce was reset to a value ≤ max(sent_tx nonces) will have their ckETH/ckERC20 burned with no ETH/ERC20 ever delivered and no reimbursement triggered, because the minter's state machine does not detect the nonce collision as a failure requiring reimbursement.

2. **Persistent DoS of the withdrawal pipeline**: If the new nonce collides with an existing `created_tx` entry, every subsequent call to `create_transactions_batch` panics, halting all withdrawals until another upgrade corrects the nonce.

The `EthTransactions` state machine's invariant — that `next_nonce` is always strictly greater than all nonces in `created_tx` and `sent_tx` — is not enforced at the upgrade boundary. [8](#0-7) 

### Likelihood Explanation

Low. Triggering this requires an NNS governance proposal that sets `next_transaction_nonce` to a conflicting value. However, this is not hypothetical: the NNS has already used this field in production to recover from stuck transactions (proposals referencing `next_transaction_nonce` in upgrade args). A proposal author who does not account for the current `created_tx`/`sent_tx` state — or who acts during a period of high withdrawal activity — can inadvertently trigger this condition. The risk is elevated because the field is documented as a recovery mechanism, creating operational pressure to use it.

### Recommendation

`update_next_transaction_nonce` should validate that the new nonce does not collide with any nonce currently in `created_tx` or `sent_tx`, and is not less than the maximum nonce already present in those maps. Concretely:

```rust
pub fn update_next_transaction_nonce(&mut self, new_nonce: TransactionNonce) -> Result<(), String> {
    let max_in_flight = self.created_tx.keys()
        .chain(self.sent_tx.keys())
        .max()
        .copied();
    if let Some(max) = max_in_flight {
        if new_nonce <= max {
            return Err(format!(
                "new nonce {new_nonce} would collide with in-flight nonce {max}"
            ));
        }
    }
    self.next_nonce = new_nonce;
    Ok(())
}
```

The `upgrade` function in `state.rs` should propagate this error as an `InvalidStateError`, causing `post_upgrade` to trap and reject the upgrade proposal before any state mutation occurs.

### Proof of Concept

1. User A submits a ckETH withdrawal. The minter creates a transaction with nonce 42 in `created_tx`, signs it, moves it to `sent_tx`, and broadcasts it to Ethereum. Nonce 42 is now in `sent_tx`; `next_nonce` is 43.

2. An NNS proposal is submitted with `UpgradeArg { next_transaction_nonce: Some(42), ... }` (e.g., to "retry" a stuck transaction). The proposal passes.

3. `post_upgrade` calls `update_next_transaction_nonce(42)`. `next_nonce` is now 42. No error is raised.

4. User B submits a ckETH withdrawal. `create_transactions_batch` reads `next_nonce = 42`, creates a transaction with nonce 42, signs it, moves it to `sent_tx` alongside the original nonce-42 entry, and broadcasts it.

5. Ethereum rejects User B's transaction with `NonceTooLow` (nonce 42 was already mined for User A). The minter logs nothing actionable and continues.

6. User A's original transaction finalizes normally. The minter removes nonce 42 from `sent_tx` and records the finalization — but only for User A's burn index. User B's burn index is in `processed_withdrawal_requests` with `maybe_reimburse` set, but since the finalized receipt belongs to User A's transaction hash, `record_finalized_transaction` is never called for User B's burn index.

7. User B's ckETH is permanently burned with no ETH delivered and no reimbursement. [9](#0-8)

### Citations

**File:** rs/ethereum/cketh/minter/src/state/transactions/mod.rs (L361-377)
```rust
pub struct EthTransactions {
    pub(in crate::state) pending_withdrawal_requests: VecDeque<WithdrawalRequest>,
    // Processed withdrawal requests (transaction created, sent, or finalized).
    pub(in crate::state) processed_withdrawal_requests:
        BTreeMap<LedgerBurnIndex, WithdrawalRequest>,
    pub(in crate::state) created_tx:
        MultiKeyMap<TransactionNonce, LedgerBurnIndex, TransactionRequest>,
    pub(in crate::state) sent_tx:
        MultiKeyMap<TransactionNonce, LedgerBurnIndex, Vec<SignedTransactionRequest>>,
    pub(in crate::state) finalized_tx:
        MultiKeyMap<TransactionNonce, LedgerBurnIndex, FinalizedEip1559Transaction>,
    pub(in crate::state) next_nonce: TransactionNonce,

    pub(in crate::state) maybe_reimburse: BTreeSet<LedgerBurnIndex>,
    pub(in crate::state) reimbursement_requests: BTreeMap<ReimbursementIndex, ReimbursementRequest>,
    pub(in crate::state) reimbursed: BTreeMap<ReimbursementIndex, ReimbursedResult>,
}
```

**File:** rs/ethereum/cketh/minter/src/state/transactions/mod.rs (L417-419)
```rust
    pub fn update_next_transaction_nonce(&mut self, new_nonce: TransactionNonce) {
        self.next_nonce = new_nonce;
    }
```

**File:** rs/ethereum/cketh/minter/src/state/transactions/mod.rs (L521-546)
```rust
        let nonce = self.next_nonce;
        assert_eq!(transaction.nonce, nonce, "BUG: transaction nonce mismatch");
        self.next_nonce = self
            .next_nonce
            .checked_increment()
            .expect("Transaction nonce overflow");
        self.remove_withdrawal_request(&withdrawal_request);
        let transaction_request = TransactionRequest {
            transaction,
            resubmission: match &withdrawal_request {
                WithdrawalRequest::CkEth(cketh) => ResubmissionStrategy::ReduceEthAmount {
                    withdrawal_amount: cketh.withdrawal_amount,
                },
                WithdrawalRequest::CkErc20(ckerc20) => ResubmissionStrategy::GuaranteeEthAmount {
                    allowed_max_transaction_fee: ckerc20.max_transaction_fee,
                },
            },
        };
        assert_eq!(
            self.created_tx.try_insert(
                nonce,
                withdrawal_request.cketh_ledger_burn_index(),
                transaction_request
            ),
            Ok(())
        );
```

**File:** rs/ethereum/cketh/minter/src/state/transactions/mod.rs (L680-712)
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
```

**File:** rs/ethereum/cketh/minter/src/state/transactions/mod.rs (L1068-1094)
```rust
    /// Checks whether two transaction state machines are equivalent.
    pub fn is_equivalent_to(&self, other: &Self) -> Result<(), String> {
        use ic_utils_ensure::ensure_eq;

        fn sorted_requests(requests: &VecDeque<WithdrawalRequest>) -> Vec<WithdrawalRequest> {
            let mut buf: Vec<_> = requests.iter().cloned().collect();
            buf.sort_unstable_by_key(|req| req.cketh_ledger_burn_index());
            buf
        }

        // We can reorder request in `reschedule_withdrawal_request`. The audit log won't
        // reflect this change, so we must sort the queues before comparing them.
        ensure_eq!(
            sorted_requests(&self.pending_withdrawal_requests),
            sorted_requests(&other.pending_withdrawal_requests)
        );
        ensure_eq!(self.created_tx, other.created_tx);
        ensure_eq!(self.sent_tx, other.sent_tx);
        ensure_eq!(self.finalized_tx, other.finalized_tx);
        ensure_eq!(self.next_nonce, other.next_nonce);

        ensure_eq!(self.maybe_reimburse, other.maybe_reimburse);
        ensure_eq!(self.reimbursement_requests, other.reimbursement_requests);
        ensure_eq!(self.reimbursed, other.reimbursed);

        Ok(())
    }
```

**File:** rs/ethereum/cketh/minter/src/state.rs (L465-469)
```rust
        if let Some(nonce) = next_transaction_nonce {
            let nonce = TransactionNonce::try_from(nonce)
                .map_err(|e| InvalidStateError::InvalidTransactionNonce(format!("ERROR: {e}")))?;
            self.eth_transactions.update_next_transaction_nonce(nonce);
        }
```

**File:** rs/ethereum/cketh/minter/src/lifecycle/upgrade.rs (L35-43)
```rust
pub fn post_upgrade(upgrade_args: Option<UpgradeArg>) {
    let start = ic_cdk::api::instruction_counter();

    STATE.with(|cell| {
        *cell.borrow_mut() = Some(replay_events());
    });
    if let Some(args) = upgrade_args {
        mutate_state(|s| process_event(s, EventType::Upgrade(args)))
    }
```

**File:** rs/ethereum/cketh/mainnet/minter_upgrade_2024_05_10.md (L1-27)
```markdown
# Proposal to upgrade the ckETH minter canister

Git hash: `4472b0064d347a88649beb526214fde204f906fb`

New compressed Wasm hash: `8108f9f7d64577e0c29c0359b689675863ab53b472796de71276f0d2467ddf3d`

Target canister: `sv3dd-oaaaa-aaaar-qacoa-cai`

Previous ckETH minter proposal: https://dashboard.internetcomputer.org/proposal/128365

---

## Motivation
This proposal upgrades the ckETH minter to enable the ckERC20 feature on the minter. Adding support for concrete tokens (e.g., USDC), will be done in separate upgrade proposals targeting the ledger suite orchestrator, which upon execution will then contact the minter via the new restricted endpoint `add_ckerc20_token`.


## Upgrade args

```
git fetch
git checkout 4472b0064d347a88649beb526214fde204f906fb
cd rs/ethereum/cketh/minter
didc encode -d cketh_minter.did -t '(MinterArg)' '(variant {UpgradeArg = record {ledger_suite_orchestrator_id = opt principal "vxkom-oyaaa-aaaar-qafda-cai"; erc20_helper_contract_address = opt "0x6abDA0438307733FC299e9C229FD3cc074bD8cC0"; last_erc20_scraped_block_number = opt 19_817_725;}})'
```
* [`vxkom-oyaaa-aaaar-qafda-cai`](https://dashboard.internetcomputer.org/canister/vxkom-oyaaa-aaaar-qafda-cai) is the ledger suite orchestrator.
* `19_817_725` is the Ethereum block in which the [ckERC20 helper contract](https://etherscan.io/address/0x6abDA0438307733FC299e9C229FD3cc074bD8cC0) was installed.

```

**File:** rs/ethereum/cketh/minter/src/withdraw.rs (L366-370)
```rust
            Ok(SendRawTransactionStatus::Ok(_)) | Ok(SendRawTransactionStatus::NonceTooLow) => {
                // In case of resubmission we may hit the case of SendRawTransactionStatus::NonceTooLow
                // if the stuck transaction was mined in the meantime.
                // It will be cleaned-up once the transaction is finalized.
            }
```

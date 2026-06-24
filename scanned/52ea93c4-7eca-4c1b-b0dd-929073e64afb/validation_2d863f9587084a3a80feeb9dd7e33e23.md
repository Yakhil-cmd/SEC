### Title
`retrieve_btc_account_to_block_indices` Not Cleaned Up After Request Finalization — (`rs/bitcoin/ckbtc/minter/src/state.rs`, `rs/bitcoin/ckbtc/minter/src/state/audit.rs`)

---

### Summary

The ckBTC minter's `CkBtcMinterState` maintains a `retrieve_btc_account_to_block_indices` map that records every `retrieve_btc` request block index per account. Entries are inserted when a request is accepted but are **never removed** when the request is finalized (confirmed, amount-too-low, or reimbursed). Meanwhile, the `finalized_requests` VecDeque is capped at `MAX_FINALIZED_REQUESTS` and prunes old entries. After pruning, the stale block indices in `retrieve_btc_account_to_block_indices` cause `retrieve_btc_status_v2_by_account` to return `Unknown` for requests that were actually confirmed — and the map itself grows without bound.

---

### Finding Description

When a `retrieve_btc` (or `retrieve_btc_with_approval`) request is accepted, `accept_retrieve_btc_request` inserts the request's `block_index` into `retrieve_btc_account_to_block_indices`: [1](#0-0) 

No corresponding removal ever occurs. Examining every finalization path:

1. **`finalize_transaction`** — removes from `submitted_transactions`, calls `push_finalized_request`, but never touches `retrieve_btc_account_to_block_indices`: [2](#0-1) 

2. **`remove_retrieve_btc_request` (audit)** — calls `push_finalized_request` only: [3](#0-2) 

3. **`reimburse_withdrawal_completed`** — removes from `pending_withdrawal_reimbursements`, inserts into `reimbursed_withdrawals`, never touches `retrieve_btc_account_to_block_indices`: [4](#0-3) 

Meanwhile, `push_finalized_request` enforces a cap and silently drops old entries: [5](#0-4) 

The public query `retrieve_btc_status_v2_by_account` reads directly from `retrieve_btc_account_to_block_indices` and calls `retrieve_btc_status_v2` for each stored block index: [6](#0-5) 

Once a finalized request's entry is pruned from `finalized_requests`, `retrieve_btc_status_v2` falls through all checks and returns `Unknown`: [7](#0-6) 

---

### Impact Explanation

Two concrete impacts:

1. **Unbounded memory growth**: `retrieve_btc_account_to_block_indices` accumulates every `retrieve_btc` block index ever issued to an account and is never pruned. Over the lifetime of the canister, this map grows monotonically, consuming stable memory proportional to the total number of withdrawal requests ever made.

2. **Incorrect status records**: After `MAX_FINALIZED_REQUESTS` entries are evicted from `finalized_requests`, the corresponding block indices remain in `retrieve_btc_account_to_block_indices`. Calling `retrieve_btc_status_v2_by_account` for such an account returns `Unknown` for requests that were actually `Confirmed` — the administration no longer corresponds to the actual state of the requests. This is the direct IC analog of the original report's "the administration doesn't correspond to the available NFTs."

---

### Likelihood Explanation

Any unprivileged user can call `retrieve_btc` or `retrieve_btc_with_approval` to add entries to `retrieve_btc_account_to_block_indices`. The cost per entry is the ckBTC burn amount plus fees, so large-scale exploitation is economically bounded. However, the map growth is a natural consequence of normal usage over the canister's lifetime — no adversarial intent is required for the incorrect-status effect to manifest once `MAX_FINALIZED_REQUESTS` is exceeded.

---

### Recommendation

In `finalize_transaction`, `remove_retrieve_btc_request`, and `reimburse_withdrawal_completed`, after moving a request to its terminal state, remove the corresponding `block_index` from `retrieve_btc_account_to_block_indices`. If the account's vector becomes empty, remove the account entry entirely. This mirrors the pattern used in `push_finalized_request` for `finalized_requests` and is the direct fix analogous to `delete timelockERC721s[key]` in the original report.

---

### Proof of Concept

1. User calls `retrieve_btc` N times, generating block indices `[b1, b2, ..., bN]`. Each is inserted into `retrieve_btc_account_to_block_indices[account]`.
2. All N requests are eventually confirmed. Each is added to `finalized_requests`.
3. Once `finalized_requests` reaches `MAX_FINALIZED_REQUESTS`, old entries (`b1`, `b2`, …) are popped from the front.
4. User calls `retrieve_btc_status_v2_by_account(Some(account))`. The response includes entries for `b1`, `b2`, … with `status_v2 = Some(Unknown)` — even though those requests were confirmed — because `retrieve_btc_account_to_block_indices` still holds them but `finalized_requests` no longer does.
5. Simultaneously, `retrieve_btc_account_to_block_indices` has grown to hold all N entries with no upper bound. [8](#0-7) [9](#0-8)

### Citations

**File:** rs/bitcoin/ckbtc/minter/src/state/audit.rs (L17-37)
```rust
pub fn accept_retrieve_btc_request<R: CanisterRuntime>(
    state: &mut CkBtcMinterState,
    request: RetrieveBtcRequest,
    runtime: &R,
) {
    record_event(
        EventType::AcceptedRetrieveBtcRequest(request.clone()),
        runtime,
    );
    state.pending_retrieve_btc_requests.push(request.clone());
    if let Some(account) = request.reimbursement_account {
        state
            .retrieve_btc_account_to_block_indices
            .entry(account)
            .and_modify(|entry| entry.push(request.block_index))
            .or_insert(vec![request.block_index]);
    }
    if let Some(kyt_provider) = request.kyt_provider {
        *state.owed_kyt_amount.entry(kyt_provider).or_insert(0) += state.check_fee;
    }
}
```

**File:** rs/bitcoin/ckbtc/minter/src/state/audit.rs (L67-84)
```rust
pub fn remove_retrieve_btc_request<R: CanisterRuntime>(
    state: &mut CkBtcMinterState,
    request: RetrieveBtcRequest,
    status: FinalizedStatus,
    runtime: &R,
) {
    record_event(
        EventType::RemovedRetrieveBtcRequest {
            block_index: request.block_index,
        },
        runtime,
    );

    state.push_finalized_request(FinalizedBtcRequest {
        request: request.into(),
        state: status,
    });
}
```

**File:** rs/bitcoin/ckbtc/minter/src/state.rs (L462-463)
```rust
    /// Maps Account to its retrieve_btc requests burn block indices.
    pub retrieve_btc_account_to_block_indices: BTreeMap<Account, Vec<u64>>,
```

**File:** rs/bitcoin/ckbtc/minter/src/state.rs (L798-822)
```rust
    pub fn retrieve_btc_status_v2_by_account(
        &self,
        target: Option<Account>,
    ) -> Vec<BtcRetrievalStatusV2> {
        let target_account = target.unwrap_or(Account {
            owner: ic_cdk::api::msg_caller(),
            subaccount: None,
        });

        let block_indices: Vec<u64> = self
            .retrieve_btc_account_to_block_indices
            .get(&target_account)
            .unwrap_or(&vec![])
            .to_vec();

        let result: Vec<BtcRetrievalStatusV2> = block_indices
            .iter()
            .map(|&block_index| BtcRetrievalStatusV2 {
                block_index,
                status_v2: Some(self.retrieve_btc_status_v2(block_index)),
            })
            .collect();

        result
    }
```

**File:** rs/bitcoin/ckbtc/minter/src/state.rs (L907-916)
```rust
        match self
            .finalized_requests
            .iter()
            .find(|finalized_request| finalized_request.request.block_index() == block_index)
            .map(|final_req| final_req.state.clone())
        {
            Some(FinalizedStatus::AmountTooLow) => RetrieveBtcStatus::AmountTooLow,
            Some(FinalizedStatus::Confirmed { txid }) => RetrieveBtcStatus::Confirmed { txid },
            None => RetrieveBtcStatus::Unknown,
        }
```

**File:** rs/bitcoin/ckbtc/minter/src/state.rs (L1036-1047)
```rust
        match finalized_tx.requests {
            SubmittedWithdrawalRequests::ToConfirm { requests } => {
                self.finalized_requests_count += requests.len() as u64;
                for request in requests {
                    self.push_finalized_request(FinalizedBtcRequest {
                        request: request.into(),
                        state: FinalizedStatus::Confirmed { txid: *txid },
                    });
                }

                self.cleanup_tx_replacement_chain(txid);
                None
```

**File:** rs/bitcoin/ckbtc/minter/src/state.rs (L1330-1337)
```rust
    fn push_finalized_request(&mut self, req: FinalizedBtcRequest) {
        assert!(!self.has_pending_retrieve_btc_request(req.request.block_index()));

        if self.finalized_requests.len() >= MAX_FINALIZED_REQUESTS {
            self.finalized_requests.pop_front();
        }
        self.finalized_requests.push_back(req)
    }
```

**File:** rs/bitcoin/ckbtc/minter/src/state.rs (L1780-1807)
```rust
    pub fn reimburse_withdrawal_completed(
        &mut self,
        burn_index: LedgerBurnIndex,
        mint_index: LedgerMintIndex,
    ) {
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

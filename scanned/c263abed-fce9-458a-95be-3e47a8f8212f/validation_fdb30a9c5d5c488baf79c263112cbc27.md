### Title
Unbounded Growth of `retrieve_btc_account_to_block_indices` Vec Causes Permanent DoS of `retrieve_btc_status_v2_by_account` - (File: rs/bitcoin/ckbtc/minter/src/state.rs)

---

### Summary

The ckBTC minter's `CkBtcMinterState` contains a `BTreeMap<Account, Vec<u64>>` field (`retrieve_btc_account_to_block_indices`) that accumulates every historical `retrieve_btc` block index for each account and **never shrinks**. The query function `retrieve_btc_status_v2_by_account` iterates over the entire Vec for a given account, calling `retrieve_btc_status_v2` (itself a multi-lookup function) for each entry. An unprivileged user can grow this Vec without bound by repeatedly submitting and completing `retrieve_btc` requests, eventually causing `retrieve_btc_status_v2_by_account` to exhaust the IC instruction limit and permanently fail for that account.

---

### Finding Description

**Root cause — non-shrinking Vec:**

In `accept_retrieve_btc_request`, every accepted withdrawal appends the new `block_index` to the per-account Vec:

```rust
state
    .retrieve_btc_account_to_block_indices
    .entry(account)
    .and_modify(|entry| entry.push(request.block_index))
    .or_insert(vec![request.block_index]);
``` [1](#0-0) 

The field is declared as:

```rust
pub retrieve_btc_account_to_block_indices: BTreeMap<Account, Vec<u64>>,
``` [2](#0-1) 

A grep over all source files in `rs/bitcoin/ckbtc/minter/src/` confirms there is **no code path that ever removes entries from the inner `Vec<u64>`**. Finalized requests are removed from `pending_retrieve_btc_requests` and moved to `finalized_requests`, but their block indices remain in `retrieve_btc_account_to_block_indices` permanently. [3](#0-2) 

**Unbounded loop — `retrieve_btc_status_v2_by_account`:**

```rust
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
``` [4](#0-3) 

For each block index, `retrieve_btc_status_v2` performs multiple sequential map lookups (`pending_reimbursements`, `pending_withdrawal_reimbursements`, `reimbursed_transactions`, `reimbursed_withdrawals`, and a full `retrieve_btc_status` call that itself iterates `pending_retrieve_btc_requests`, `submitted_transactions`, and `finalized_requests`): [5](#0-4) 

The per-entry instruction cost is therefore non-trivial, making the exhaustion threshold reachable with a moderate number of accumulated block indices.

---

### Impact Explanation

Once the Vec for a given account grows large enough, every call to `retrieve_btc_status_v2_by_account` for that account will trap with an instruction-limit-exceeded error. This is a **permanent DoS** of the status query for that account: the account can no longer query the status of any of its withdrawal requests. Because the Vec never shrinks, there is no self-healing mechanism. The canister itself is not bricked, but the affected account loses all visibility into its withdrawal history via this endpoint.

---

### Likelihood Explanation

The `retrieve_btc` endpoint is publicly callable by any ckBTC holder. The only throttle is `MAX_CONCURRENT_PENDING_REQUESTS`, which limits the number of *incomplete* requests at any one time: [6](#0-5) 

Once requests are finalized (confirmed on Bitcoin), they are removed from the incomplete count but their block indices remain in `retrieve_btc_account_to_block_indices`. An attacker can therefore cycle through batches of requests — submit up to the concurrent limit, wait for Bitcoin confirmations, repeat — accumulating block indices indefinitely. The cost per block index is the minimum retrieve amount in ckBTC, which is a real but finite economic barrier. A well-funded attacker (or a self-targeting user who simply makes many legitimate withdrawals over time) can reach the DoS threshold.

---

### Recommendation

1. **Cap the Vec length**: Enforce a maximum number of entries per account in `retrieve_btc_account_to_block_indices` (e.g., keep only the most recent N block indices).
2. **Prune finalized entries**: When a request reaches a terminal state (confirmed, reimbursed, etc.), remove its block index from the Vec.
3. **Paginate the query**: Change `retrieve_btc_status_v2_by_account` to accept `start` and `length` pagination parameters so it never iterates the full Vec in a single call.

---

### Proof of Concept

1. Attacker holds ckBTC and calls `retrieve_btc` (or `retrieve_btc_with_approval`) repeatedly with the minimum allowed amount, up to `MAX_CONCURRENT_PENDING_REQUESTS`.
2. Each accepted request appends a new `block_index` to `retrieve_btc_account_to_block_indices[attacker_account]` via `accept_retrieve_btc_request`. [7](#0-6) 
3. The minter processes and finalizes the batch on Bitcoin. The block indices remain in the Vec.
4. Attacker repeats steps 1–3 across multiple Bitcoin confirmation cycles.
5. After enough cycles, calling `retrieve_btc_status_v2_by_account` for the attacker's account iterates the full Vec, invoking `retrieve_btc_status_v2` for each entry. The accumulated instruction cost exceeds the IC per-message limit, and the query permanently traps. [8](#0-7) 
6. The attacker's account can no longer query withdrawal status. The DoS is permanent because the Vec never shrinks.

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

**File:** rs/bitcoin/ckbtc/minter/src/state.rs (L460-463)
```rust
    pub pending_retrieve_btc_requests: Vec<RetrieveBtcRequest>,

    /// Maps Account to its retrieve_btc requests burn block indices.
    pub retrieve_btc_account_to_block_indices: BTreeMap<Account, Vec<u64>>,
```

**File:** rs/bitcoin/ckbtc/minter/src/state.rs (L807-821)
```rust
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
```

**File:** rs/bitcoin/ckbtc/minter/src/state.rs (L824-868)
```rust
    pub fn retrieve_btc_status_v2(&self, block_index: u64) -> RetrieveBtcStatusV2 {
        // Hack to avoid a Candid breaking change in `ReimbursementReason`
        // which is in the return type of `retrieve_btc_status_v2`
        fn map_reimbursement_reason(reason: &WithdrawalReimbursementReason) -> ReimbursementReason {
            match reason {
                WithdrawalReimbursementReason::InvalidTransaction(
                    InvalidTransactionError::TooManyInputs { .. },
                ) => ReimbursementReason::CallFailed,
            }
        }

        if let Some(reimbursement) = self.pending_reimbursements.get(&block_index) {
            return RetrieveBtcStatusV2::WillReimburse(reimbursement.clone());
        }

        if let Some(reimbursement) = self.pending_withdrawal_reimbursements.get(&block_index) {
            return RetrieveBtcStatusV2::WillReimburse(ReimburseDepositTask {
                account: reimbursement.account,
                amount: reimbursement.amount,
                reason: map_reimbursement_reason(&reimbursement.reason),
            });
        }

        if let Some(reimbursement) = self.reimbursed_transactions.get(&block_index) {
            return RetrieveBtcStatusV2::Reimbursed(reimbursement.clone());
        }

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
        }

        let status_v2: RetrieveBtcStatusV2 = self.retrieve_btc_status(block_index).into();

        status_v2
    }
```

**File:** rs/bitcoin/ckbtc/minter/src/updates/retrieve_btc.rs (L174-179)
```rust
    if read_state(|s| s.count_incomplete_retrieve_btc_requests() >= MAX_CONCURRENT_PENDING_REQUESTS)
    {
        return Err(RetrieveBtcError::TemporarilyUnavailable(
            "too many pending retrieve_btc requests".to_string(),
        ));
    }
```

### Title
Unbounded `retrieve_btc_account_to_block_indices` Vec Causes Instruction-Limit Exhaustion in `retrieve_btc_status_v2_by_account` Query — (File: `rs/bitcoin/ckbtc/minter/src/state.rs`)

---

### Summary

The ckBTC minter accumulates every accepted `retrieve_btc` block index into a per-account `Vec<u64>` inside `retrieve_btc_account_to_block_indices` and never prunes that list. The public query `retrieve_btc_status_v2_by_account` iterates over the entire list and calls `retrieve_btc_status_v2` — which itself performs multiple linear scans — for every entry. A user who has submitted many withdrawal requests over time will eventually cause this query to exceed the IC query instruction limit, permanently making the endpoint unusable for that account.

---

### Finding Description

**Root cause — unbounded accumulation**

Every call to `accept_retrieve_btc_request` appends the new `block_index` to the per-account `Vec<u64>`: [1](#0-0) 

```rust
state
    .retrieve_btc_account_to_block_indices
    .entry(account)
    .and_modify(|entry| entry.push(req.block_index))
    .or_insert(vec![req.block_index]);
```

The field is declared as an unbounded `Vec<u64>` per account: [2](#0-1) 

There is no corresponding removal or cap anywhere in the codebase. Finalized requests are evicted from `finalized_requests` (capped at `MAX_FINALIZED_REQUESTS = 100`) but the `retrieve_btc_account_to_block_indices` entry is never trimmed. [3](#0-2) 

**Root cause — unbounded iteration in the query**

`retrieve_btc_status_v2_by_account` fetches the full Vec and maps `retrieve_btc_status_v2` over every element: [4](#0-3) 

Each call to `retrieve_btc_status_v2` falls through to `retrieve_btc_status`, which performs three separate linear scans: [5](#0-4) 

- `pending_retrieve_btc_requests.iter().any(...)` — O(pending)
- `submitted_transactions.iter().find_map(...)` — O(submitted)
- `finalized_requests.iter().find(...)` — O(100)

Total cost per query call: **O(m × n)** where *m* = number of historical block indices for the account and *n* = current pending/submitted transaction count.

**Attacker-controlled entry path**

Any authenticated user can call `retrieve_btc` or `retrieve_btc_with_approval` repeatedly (each call burns ckBTC and appends one entry). The `MAX_CONCURRENT_PENDING_REQUESTS` guard only limits *in-flight* requests; once a batch is confirmed and finalized, the slot is freed and the attacker can submit more. The historical `block_index` entries in `retrieve_btc_account_to_block_indices` are never removed. [6](#0-5) 

The public query endpoint is: [7](#0-6) 

---

### Impact Explanation

Once the per-account Vec grows large enough, every call to `retrieve_btc_status_v2_by_account` for that account exceeds the IC query instruction limit and returns an error. The endpoint becomes permanently unavailable for that account without a canister upgrade that prunes the map. Users lose the ability to retrieve their full withdrawal history in a single call. Individual statuses remain queryable via `retrieve_btc_status` / `retrieve_btc_status_v2`, so funds themselves are not locked, but the aggregate status query is permanently DoS'd for the affected account.

**Impact: Medium** — permanent query-endpoint DoS for affected accounts; no fund loss.

---

### Likelihood Explanation

Each `retrieve_btc` call requires burning real ckBTC, so the attack has a direct economic cost. However, a legitimate power user who has processed thousands of withdrawals over the minter's lifetime will hit this organically. The minter has been live on mainnet for years, making this a realistic scenario for high-volume users or integrators. No privileged access is required.

**Likelihood: Medium**

---

### Recommendation

1. **Cap or prune `retrieve_btc_account_to_block_indices`**: When a request transitions to `Confirmed` or `AmountTooLow`, remove its `block_index` from the per-account Vec (or keep only the last N entries).
2. **Paginate `retrieve_btc_status_v2_by_account`**: Accept a `start` and `limit` parameter so callers cannot trigger unbounded iteration in a single query.
3. **Add an invariant check** that the total number of entries across all accounts in `retrieve_btc_account_to_block_indices` is bounded.

---

### Proof of Concept

1. Obtain ckBTC on mainnet (or testnet).
2. Call `retrieve_btc` (or `retrieve_btc_with_approval`) repeatedly, waiting for each batch to be confirmed before submitting the next (to stay under `MAX_CONCURRENT_PENDING_REQUESTS`).
3. After *k* confirmed withdrawals, call `retrieve_btc_status_v2_by_account(null)`.
4. Observe that for sufficiently large *k* the query traps with `CanisterInstructionLimitExceeded` because the minter iterates over all *k* block indices, each triggering three linear scans of the live transaction queues.

The exact threshold depends on the current size of `pending_retrieve_btc_requests` and `submitted_transactions`, but the cost grows without bound as *k* increases.

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

**File:** rs/bitcoin/ckbtc/minter/src/state.rs (L45-47)
```rust
/// The maximum number of finalized BTC retrieval requests that we keep in the
/// history.
const MAX_FINALIZED_REQUESTS: usize = 100;
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

**File:** rs/bitcoin/ckbtc/minter/src/state.rs (L872-917)
```rust
    pub fn retrieve_btc_status(&self, block_index: u64) -> RetrieveBtcStatus {
        if self
            .pending_retrieve_btc_requests
            .iter()
            .any(|req| req.block_index == block_index)
        {
            return RetrieveBtcStatus::Pending;
        }

        if let Some(status) = self.requests_in_flight.get(&block_index).cloned() {
            return match status {
                InFlightStatus::Signing => RetrieveBtcStatus::Signing,
            };
        }

        if let Some(txid) = self
            .submitted_transactions
            .iter()
            .find_map(|tx| match &tx.requests {
                SubmittedWithdrawalRequests::ToConfirm { requests } => {
                    (requests.iter().any(|r| r.block_index == block_index)).then_some(tx.txid)
                }
                SubmittedWithdrawalRequests::ToCancel { .. } => None,
                SubmittedWithdrawalRequests::ToConsolidate { request } => {
                    if request.block_index == block_index {
                        Some(tx.txid)
                    } else {
                        None
                    }
                }
            })
        {
            return RetrieveBtcStatus::Submitted { txid };
        }

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

**File:** rs/bitcoin/ckbtc/minter/src/main.rs (L186-189)
```rust
#[query]
fn retrieve_btc_status_v2_by_account(target: Option<Account>) -> Vec<BtcRetrievalStatusV2> {
    read_state(|s| s.retrieve_btc_status_v2_by_account(target))
}
```

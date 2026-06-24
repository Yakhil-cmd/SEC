### Title
Unbounded Per-Account Storage Growth in `retrieve_btc_account_to_block_indices` Causes O(k×n) Query DoS - (File: `rs/bitcoin/ckbtc/minter/src/state/audit.rs`, `rs/bitcoin/ckbtc/minter/src/state.rs`)

---

### Summary

The ckBTC minter canister maintains a `BTreeMap<Account, Vec<u64>>` called `retrieve_btc_account_to_block_indices` that accumulates every accepted withdrawal request's block index per account and is **never pruned**. The public query endpoint `retrieve_btc_status_v2_by_account` iterates over the entire unbounded Vec for a target account, calling `retrieve_btc_status_v2` for each entry, which itself performs multiple O(n) linear scans over `pending_retrieve_btc_requests`, `submitted_transactions`, and `finalized_requests`. Over time, a high-volume account causes this query to consume an ever-growing number of instructions, eventually hitting the IC query instruction limit and making the endpoint permanently unusable for that account.

---

### Finding Description

**Root cause — unbounded Vec growth, never pruned:**

In `accept_retrieve_btc_request`, every successful `retrieve_btc` or `retrieve_btc_with_approval` call appends the new `block_index` to the per-account Vec:

```rust
// rs/bitcoin/ckbtc/minter/src/state/audit.rs
state
    .retrieve_btc_account_to_block_indices
    .entry(account)
    .and_modify(|entry| entry.push(request.block_index))
    .or_insert(vec![request.block_index]);
``` [1](#0-0) 

The field is declared as an unbounded `Vec<u64>` per account:

```rust
pub retrieve_btc_account_to_block_indices: BTreeMap<Account, Vec<u64>>,
``` [2](#0-1) 

No code path ever removes entries from this Vec. `push_finalized_request` caps `finalized_requests` at `MAX_FINALIZED_REQUESTS` but leaves `retrieve_btc_account_to_block_indices` untouched. [3](#0-2) 

**O(k×n) query cost:**

`retrieve_btc_status_v2_by_account` iterates over every block index in the Vec without any limit, calling `retrieve_btc_status_v2` for each:

```rust
let result: Vec<BtcRetrievalStatusV2> = block_indices
    .iter()
    .map(|&block_index| BtcRetrievalStatusV2 {
        block_index,
        status_v2: Some(self.retrieve_btc_status_v2(block_index)),
    })
    .collect();
``` [4](#0-3) 

Each call to `retrieve_btc_status_v2` delegates to `retrieve_btc_status`, which performs three separate O(n) linear scans — over `pending_retrieve_btc_requests` (up to 5,000 entries), `submitted_transactions`, and `finalized_requests`: [5](#0-4) 

The query endpoint is publicly callable by any principal, including with an arbitrary `target` account:

```rust
#[query]
fn retrieve_btc_status_v2_by_account(target: Option<Account>) -> Vec<BtcRetrievalStatusV2> {
    read_state(|s| s.retrieve_btc_status_v2_by_account(target))
}
``` [6](#0-5) 

**The global cap does not prevent per-account Vec bloat:**

`MAX_CONCURRENT_PENDING_REQUESTS = 5000` limits only the number of *currently incomplete* requests across all accounts. It does not bound the historical accumulation in `retrieve_btc_account_to_block_indices`, which persists across all finalized requests indefinitely. [7](#0-6) 

---

### Impact Explanation

**Cycles/resource accounting bug.** As a single account accumulates withdrawal history (k entries), the instruction cost of `retrieve_btc_status_v2_by_account` grows as O(k × n) where n is the size of the pending/submitted/finalized queues. Once k is large enough, the query exceeds the IC per-query instruction limit (currently ~5 billion instructions), permanently breaking the endpoint for that account. Additionally, the `retrieve_btc_account_to_block_indices` map itself grows without bound in canister heap memory, contributing to long-term memory exhaustion of the minter canister.

---

### Likelihood Explanation

**Medium.** Each `retrieve_btc` call requires burning real ckBTC, so the attack is economically constrained and not free. However:
- A legitimate high-volume user (e.g., an exchange or automated service) naturally accumulates thousands of historical entries over months of operation, triggering the DoS without any malicious intent.
- An attacker with sufficient ckBTC can deliberately bloat a target account's Vec and then repeatedly call the query against it to degrade the minter's query capacity.
- The ckBTC minter is a production mainnet canister handling real Bitcoin value, making even medium-likelihood degradation significant.

---

### Recommendation

1. **Prune `retrieve_btc_account_to_block_indices` on finalization.** When a request is finalized (confirmed or reimbursed), remove its `block_index` from the per-account Vec. Only pending/in-flight indices need to be tracked for `retrieve_btc_status_v2_by_account`.

2. **Cap the Vec per account.** Enforce a maximum number of tracked block indices per account (e.g., the last N requests), evicting the oldest entries when the cap is reached.

3. **Add pagination to `retrieve_btc_status_v2_by_account`.** Accept `start` and `length` parameters so callers cannot force unbounded iteration in a single query call.

---

### Proof of Concept

1. An account (attacker or legitimate high-volume user) calls `retrieve_btc` or `retrieve_btc_with_approval` successfully N times over time. Each call appends one entry to `retrieve_btc_account_to_block_indices[account]`.
2. The global `MAX_CONCURRENT_PENDING_REQUESTS` check passes because requests are processed and finalized between calls, keeping the incomplete count low.
3. After N successful withdrawals, `retrieve_btc_account_to_block_indices[account]` contains N entries. None are ever removed.
4. Any caller invokes `retrieve_btc_status_v2_by_account(Some(account))`. The query iterates all N entries, calling `retrieve_btc_status_v2` → `retrieve_btc_status` for each, performing O(N × pending_queue_size) work.
5. When N is large enough (e.g., tens of thousands of historical withdrawals), the query exceeds the IC instruction limit and is rejected with a `CanisterError`, permanently breaking status lookup for that account. [8](#0-7) [9](#0-8) [10](#0-9)

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

**File:** rs/bitcoin/ckbtc/minter/src/main.rs (L186-189)
```rust
#[query]
fn retrieve_btc_status_v2_by_account(target: Option<Account>) -> Vec<BtcRetrievalStatusV2> {
    read_state(|s| s.retrieve_btc_status_v2_by_account(target))
}
```

**File:** rs/bitcoin/ckbtc/minter/src/updates/retrieve_btc.rs (L22-22)
```rust
const MAX_CONCURRENT_PENDING_REQUESTS: usize = 5000;
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

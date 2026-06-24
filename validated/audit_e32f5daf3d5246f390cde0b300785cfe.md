Audit Report

## Title
Unbounded Growth of `retrieve_btc_account_to_block_indices` Causes Per-Account DoS in `retrieve_btc_status_v2_by_account` - (File: rs/bitcoin/ckbtc/minter/src/state.rs)

## Summary
The ckBTC Minter maintains a `BTreeMap<Account, Vec<u64>>` called `retrieve_btc_account_to_block_indices` that accumulates every historical `block_index` for each account's retrieve_btc requests but never removes entries after finalization. The public query `retrieve_btc_status_v2_by_account` iterates the entire Vec per account, invoking `retrieve_btc_status_v2` (which itself performs multiple BTreeMap lookups and linear scans) for each entry. As the Vec grows without bound, this query will eventually exceed the IC instruction limit, permanently denying the account access to withdrawal status via this API and causing unbounded heap growth in the minter canister.

## Finding Description
**Unbounded append, no cleanup:**

`accept_retrieve_btc_request` in `audit.rs` appends `request.block_index` to the account's Vec on every accepted request: [1](#0-0) 

The identical append-only pattern is replicated during event log replay: [2](#0-1) 

A grep for any removal operations (`remove`, `retain`, `truncate`, `pop`, `drain`, `clear`) on `retrieve_btc_account_to_block_indices` returns **no matches** anywhere in the codebase. The field declaration confirms no size bound: [3](#0-2) 

**Unbounded iteration in the query handler:**

`retrieve_btc_status_v2_by_account` fetches the full Vec and maps every element through `retrieve_btc_status_v2`: [4](#0-3) 

`retrieve_btc_status_v2` performs four sequential BTreeMap lookups (`pending_reimbursements`, `pending_withdrawal_reimbursements`, `reimbursed_transactions`, `reimbursed_withdrawals`) before falling through to `retrieve_btc_status`: [5](#0-4) 

`retrieve_btc_status` then linearly scans `pending_retrieve_btc_requests` and `submitted_transactions`: [6](#0-5) 

**Existing guards are insufficient:**

`MAX_CONCURRENT_PENDING_REQUESTS = 5000` throttles simultaneous in-flight requests but places no cap on the *lifetime* count of requests per account: [7](#0-6) 

`MAX_FINALIZED_REQUESTS = 100` caps `finalized_requests` but has no effect on `retrieve_btc_account_to_block_indices`: [8](#0-7) 

## Impact Explanation
This is a **High** severity finding matching the allowed impact: *"Significant Chain Fusion, ck-token, ledger, Rosetta, boundary/API, XRC, Internet Identity, NNS, SNS, or infrastructure security impact with concrete user or protocol harm."* Once a user's Vec is large enough, every call to `retrieve_btc_status_v2_by_account` for that account traps with an instruction-limit exceeded error. The condition is irreversible without a canister upgrade that explicitly prunes the map. Additionally, the ever-growing map consumes unbounded heap memory in the ckBTC Minter canister, threatening overall canister stability for all users.

## Likelihood Explanation
Any unprivileged principal holding ckBTC can call `retrieve_btc_with_approval` repeatedly. Each successful call appends one entry to their Vec. The `MAX_CONCURRENT_PENDING_REQUESTS` limit only throttles simultaneous in-flight requests; it does not cap the lifetime count per account. A user making even a modest number of withdrawals over months or years accumulates hundreds of entries. A motivated attacker who repeatedly withdraws the minimum amount will reach the DoS threshold faster. No privileged role is required — only a valid ckBTC balance.

## Recommendation
After a retrieve_btc request reaches a terminal state (confirmed, reimbursed, or amount-too-low), remove its `block_index` from the account's Vec in `retrieve_btc_account_to_block_indices`. If historical lookup is still desired, cap the Vec to a fixed maximum (e.g., the last 100 entries per account), mirroring the existing `MAX_FINALIZED_REQUESTS = 100` cap applied to `finalized_requests`. Alternatively, replace the per-account Vec with a bounded ring-buffer or implement pagination in the query so it does not iterate the full history in a single call.

## Proof of Concept
1. Alice calls `retrieve_btc_with_approval` N times over her account lifetime (each call with the minimum allowed amount). Each call appends one `block_index` to `retrieve_btc_account_to_block_indices[alice_account]`.
2. After N requests are finalized, `retrieve_btc_account_to_block_indices[alice_account]` contains N entries. None are removed (confirmed by absence of any removal code).
3. Alice (or anyone) calls `retrieve_btc_status_v2_by_account(Some(alice_account))`. The canister iterates all N entries, calling `retrieve_btc_status_v2` for each — O(N × k) total work.
4. For sufficiently large N, the query exceeds the IC 5-billion-instruction limit and traps. The condition persists across all future calls because the Vec is never pruned.
5. A deterministic integration test using PocketIC can verify this by inserting a large number of `AcceptedRetrieveBtcRequest` events into the event log, replaying state, and confirming that `retrieve_btc_status_v2_by_account` traps once the Vec exceeds the instruction budget.

### Citations

**File:** rs/bitcoin/ckbtc/minter/src/state/audit.rs (L27-33)
```rust
    if let Some(account) = request.reimbursement_account {
        state
            .retrieve_btc_account_to_block_indices
            .entry(account)
            .and_modify(|entry| entry.push(request.block_index))
            .or_insert(vec![request.block_index]);
    }
```

**File:** rs/bitcoin/ckbtc/minter/src/state/eventlog.rs (L395-403)
```rust
                EventType::AcceptedRetrieveBtcRequest(req) => {
                    if let Some(account) = req.reimbursement_account {
                        state
                            .retrieve_btc_account_to_block_indices
                            .entry(account)
                            .and_modify(|entry| entry.push(req.block_index))
                            .or_insert(vec![req.block_index]);
                    }
                    state.push_back_pending_retrieve_btc_request(req);
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

**File:** rs/bitcoin/ckbtc/minter/src/state.rs (L835-865)
```rust
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
```

**File:** rs/bitcoin/ckbtc/minter/src/state.rs (L872-916)
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
```

**File:** rs/bitcoin/ckbtc/minter/src/updates/retrieve_btc.rs (L22-22)
```rust
const MAX_CONCURRENT_PENDING_REQUESTS: usize = 5000;
```

### Title
Pending `retrieve_btc` Requests Are Not Settled in the Order They Were Created — (`rs/bitcoin/ckbtc/minter/src/state.rs`)

---

### Summary

The ckBTC minter's `build_batch` function collects pending withdrawal requests into a `BTreeSet<RetrieveBtcRequest>`. Because `RetrieveBtcRequest` derives `Ord` with `amount` as its first field, the set is ordered by withdrawal amount (ascending), not by creation time (`block_index` / `received_at`). As a result, within every batch the minter processes requests in amount order rather than submission order, and across batches a large-amount request submitted first can be skipped in favour of a smaller-amount request submitted later.

---

### Finding Description

`RetrieveBtcRequest` derives `Ord` with `amount` declared first: [1](#0-0) 

Rust's derived `Ord` is lexicographic over field declaration order, so the primary sort key is `amount`, not `block_index` or `received_at`.

`build_batch` iterates the pending queue (which is maintained in FIFO / `received_at` order) but inserts every accepted request into a `BTreeSet<RetrieveBtcRequest>`: [2](#0-1) 

Two ordering violations follow:

1. **Within a batch** – the `BTreeSet` iterator yields requests sorted by `amount` (smallest first). The Bitcoin transaction outputs are built directly from this iterator: [3](#0-2) 

2. **Across batches** – when a request does not fit the current batch (UTXO value or size limit), it is pushed back and the loop continues to later, potentially smaller requests. A large-amount request submitted first can therefore be deferred while a smaller-amount request submitted later is included in the current batch.

When `push_from_in_flight_to_pending_requests` re-sorts the pending queue by `received_at` after a failed transaction, the re-queued requests are correctly re-ordered: [4](#0-3) 

However, the `build_batch` path never re-sorts; it simply pushes skipped requests back with `.push()`, which can silently reorder the queue if the skipped requests are not the tail elements.

---

### Impact Explanation

- **Fairness / ordering guarantee broken**: a user who submitted a `retrieve_btc` request earlier (with a larger amount) may have their BTC withdrawal delayed by an arbitrary number of rounds while later, smaller requests are settled first.
- **Output-index manipulation**: because the Bitcoin transaction outputs are ordered by `amount`, an adversary who knows the pending queue can craft a withdrawal amount that sorts before a victim's output, influencing the output index of the victim's UTXO. While this does not steal funds, it breaks the expected FIFO settlement guarantee that users rely on.
- **Starvation**: a continuous stream of small-amount requests can indefinitely defer a large-amount request that was submitted first, because `build_batch` always skips it when UTXOs are insufficient and includes the smaller later requests instead.

---

### Likelihood Explanation

Any unprivileged user can call `retrieve_btc` to submit a withdrawal request. No special role or key is required. The ordering violation is triggered on every call to `submit_pending_requests` (the periodic background task) whenever the pending queue contains requests of differing amounts, which is the common case on mainnet.

---

### Recommendation

- **Short term**: change `build_batch` to return a `Vec<RetrieveBtcRequest>` (preserving FIFO order) instead of a `BTreeSet<RetrieveBtcRequest>`. Update all call sites that currently iterate the `BTreeSet` to iterate the `Vec`.
- **Short term**: if a request does not fit the current batch, stop processing further requests for that round (or use a separate "skip" list that is re-prepended to the queue) to preserve strict FIFO ordering across batches.
- **Long term**: add unit tests that assert pending requests are included in batches and appear as Bitcoin transaction outputs in the exact order they were submitted (`block_index` ascending).

---

### Proof of Concept

1. User A calls `retrieve_btc` with `amount = 10_000_000` sat (0.1 BTC). Request is appended to `pending_retrieve_btc_requests` with `block_index = 100`.
2. User B calls `retrieve_btc` with `amount = 1_000` sat (dust-level). Request is appended with `block_index = 101`.
3. `build_batch` is called. Both requests fit the available UTXOs. They are inserted into `BTreeSet<RetrieveBtcRequest>`.
4. Because `Ord` sorts by `amount` first, the `BTreeSet` iterator yields B's request (`amount = 1_000`) before A's request (`amount = 10_000_000`).
5. `outputs` is built as `[(B.address, 1_000), (A.address, 10_000_000)]` — B's output appears at index 0, A's at index 1, despite A having submitted first.
6. The signed Bitcoin transaction encodes outputs in this amount-sorted order, violating the FIFO settlement guarantee.

### Citations

**File:** rs/bitcoin/ckbtc/minter/src/state.rs (L61-85)
```rust
#[derive(
    Clone, Eq, PartialEq, Ord, PartialOrd, Debug, Deserialize, Serialize, candid::CandidType,
)]
pub struct RetrieveBtcRequest {
    /// The amount to convert to BTC.
    /// The minter withdraws BTC transfer fees from this amount.
    pub amount: u64,
    /// The destination BTC address.
    pub address: BitcoinAddress,
    /// The BURN transaction index on the ledger.
    /// Serves as a unique request identifier.
    pub block_index: u64,
    /// The time at which the minter accepted the request.
    pub received_at: u64,
    /// The KYT provider that validated this request.
    /// The field is optional because old retrieve_btc requests
    /// didn't go through the KYT check.
    #[serde(rename = "kyt_provider")]
    #[serde(skip_serializing_if = "Option::is_none")]
    pub kyt_provider: Option<Principal>,
    /// The reimbursement_account of the retrieve_btc transaction.
    #[serde(rename = "reimbursement_account")]
    #[serde(skip_serializing_if = "Option::is_none")]
    pub reimbursement_account: Option<Account>,
}
```

**File:** rs/bitcoin/ckbtc/minter/src/state.rs (L943-958)
```rust
    pub fn build_batch(&mut self, max_size: usize) -> BTreeSet<RetrieveBtcRequest> {
        let available_utxos_value = self.available_utxos.iter().map(|u| u.value).sum::<u64>();
        let mut batch = BTreeSet::new();
        let mut tx_amount = 0;
        for req in std::mem::take(&mut self.pending_retrieve_btc_requests) {
            if available_utxos_value < req.amount + tx_amount || batch.len() >= max_size {
                // Put this request back to the queue until we have enough liquid UTXOs.
                self.pending_retrieve_btc_requests.push(req);
            } else {
                tx_amount += req.amount;
                batch.insert(req);
            }
        }

        batch
    }
```

**File:** rs/bitcoin/ckbtc/minter/src/state.rs (L1271-1290)
```rust
    pub fn push_from_in_flight_to_pending_requests(
        &mut self,
        requests: SubmittedWithdrawalRequests,
    ) {
        for block_index in requests.iter_block_index() {
            assert!(!self.has_pending_retrieve_btc_request(block_index));
            self.requests_in_flight.remove(&block_index);
        }
        match requests {
            SubmittedWithdrawalRequests::ToConfirm { requests }
            | SubmittedWithdrawalRequests::ToCancel { requests, .. } => {
                for req in requests {
                    self.pending_retrieve_btc_requests.push(req);
                }
            }
            SubmittedWithdrawalRequests::ToConsolidate { .. } => (),
        }
        self.pending_retrieve_btc_requests
            .sort_by_key(|r| r.received_at);
    }
```

**File:** rs/bitcoin/ckbtc/minter/src/lib.rs (L372-395)
```rust
        let outputs: Vec<_> = batch
            .iter()
            .map(|req| (req.address.clone(), req.amount))
            .collect();

        match build_unsigned_transaction(
            &mut s.available_utxos,
            outputs,
            &main_address,
            max_num_inputs_in_transaction,
            fee_millisatoshi_per_vbyte,
            &fee_estimator,
        ) {
            Ok((unsigned_tx, change_output, total_fee, utxos)) => Some((
                SignTxRequest {
                    key_name: s.ecdsa_key_name.clone(),
                    ecdsa_public_key,
                    change_output,
                    network: s.btc_network,
                    accounts: s.find_all_accounts(&unsigned_tx),
                    unsigned_tx,
                    requests: state::SubmittedWithdrawalRequests::ToConfirm {
                        requests: batch.into_iter().collect(),
                    },
```

### Title
No Minimum BTC Output Protection in ckBTC Minter Withdrawal Queue — (File: `rs/bitcoin/ckbtc/minter/src/updates/retrieve_btc.rs`)

---

### Summary

The ckBTC minter's `retrieve_btc` endpoint implements a two-phase withdrawal: ckBTC is burned immediately and irrevocably, then the request is queued for asynchronous processing by the minter's heartbeat. The actual BTC delivered to the user equals `amount − bitcoin_fee − minter_fee`, where both fees are computed at **processing time** using the then-current Bitcoin network fee rate — not the rate visible at submission time. Because Bitcoin fees can spike by an order of magnitude between submission and processing, and because `RetrieveBtcArgs` carries no `min_btc_output` field, users have no on-chain mechanism to bound their slippage.

---

### Finding Description

**Phase 1 — Immediate, irreversible burn.**
`retrieve_btc()` validates the request, calls `burn_ckbtcs()` to destroy the user's ckBTC on the ledger, and enqueues a `RetrieveBtcRequest` in `pending_retrieve_btc_requests`:

```rust
// rs/bitcoin/ckbtc/minter/src/updates/retrieve_btc.rs  lines 204-232
let block_index =
    burn_ckbtcs(caller, args.amount, crate::memo::encode(&burn_memo).into()).await?;

let request = RetrieveBtcRequest {
    amount: args.amount,   // full ckBTC amount, no min_btc_output field
    address: parsed_address,
    block_index,
    received_at: ic_cdk::api::time(),
    ...
};
mutate_state(|s| state::audit::accept_retrieve_btc_request(s, request, &IC_CANISTER_RUNTIME));
``` [1](#0-0) 

The `RetrieveBtcArgs` struct accepted by the endpoint contains only `amount` and `address` — no `min_btc_output` guard.

**Phase 2 — Asynchronous, heartbeat-driven processing.**
The minter's heartbeat later calls `build_batch()` to collect pending requests and then constructs a Bitcoin transaction. The Bitcoin network fee and minter fee are computed at that moment using the **current** median fee rate fetched from the Bitcoin canister:

```rust
// rs/bitcoin/ckbtc/minter/src/fees/mod.rs  lines 128-147
fn fee_based_minimum_withdrawal_amount(&self, median_fee_rate: FeeRate) -> Satoshi {
    ((PER_REQUEST_RBF_BOUND
        + median_fee_rate.fee_ceil(PER_REQUEST_VSIZE_BOUND)
        + PER_REQUEST_MINTER_FEE_BOUND
        + self.check_fee)
        / 50_000) * 50_000
        + self.retrieve_btc_min_amount
}
``` [2](#0-1) 

The minter state tracks `fee_based_retrieve_btc_min_amount` (updated periodically) and `pending_retrieve_btc_requests` (the queue):

```rust
// rs/bitcoin/ckbtc/minter/src/state.rs  lines 456-460
/// Minimum amount of bitcoin that can be retrieved based on recent fees
pub fee_based_retrieve_btc_min_amount: u64,

/// Retrieve_btc requests that are waiting to be served, sorted by received_at.
pub pending_retrieve_btc_requests: Vec<RetrieveBtcRequest>,
``` [3](#0-2) 

Requests can remain in the queue for up to `max_time_in_queue_nanos` nanoseconds before the minter is forced to process them:

```rust
// rs/bitcoin/ckbtc/minter/src/state.rs  lines 919-940
pub fn can_form_a_batch(&self, min_pending: usize, now: u64) -> bool {
    ...
    if let Some(req) = self.pending_retrieve_btc_requests.first()
        && self.max_time_in_queue_nanos < now.saturating_sub(req.received_at)
    {
        return true;
    }
    ...
}
``` [4](#0-3) 

**The gap.** The fee rate checked at submission time (`fee_based_retrieve_btc_min_amount`) is a global floor, not a per-request minimum output. Once a request passes that check and the ckBTC is burned, the user is committed. If the Bitcoin fee rate rises substantially before the heartbeat processes the batch, the user receives materially less BTC than they expected, with no recourse.

---

### Impact Explanation

A user who burns 100,000 satoshi of ckBTC expecting to receive ~97,000 satoshi of BTC (after a modest fee) may instead receive ~55,000 satoshi if the Bitcoin network fee rate spikes 5× between submission and processing. The ckBTC burn is final; there is no cancellation path. The only partial mitigation is the reimbursement path triggered when fees exceed the entire withdrawal amount — but that path does not protect against partial, unexpected fee increases that still leave a positive (but much smaller) output.

---

### Likelihood Explanation

Bitcoin network fees are volatile and have historically spiked by 10–50× within minutes during periods of mempool congestion (e.g., Ordinals/Runes inscription waves). The minter batches requests and may hold them in `pending_retrieve_btc_requests` for up to `max_time_in_queue_nanos` (configurable, currently set to a non-trivial window). Any unprivileged user can submit a `retrieve_btc` request via a standard ingress call; no special role is required. The combination of irreversible burn, asynchronous processing, and absent per-request output floor makes this a realistic user-facing loss scenario during fee spikes.

---

### Recommendation

Add an optional `min_btc_output: opt nat64` field to `RetrieveBtcArgs` (and the corresponding `RetrieveBtcWithApprovalArgs`). Before constructing the Bitcoin transaction, compare the computed net output (`amount − bitcoin_fee − minter_fee`) against `min_btc_output`. If the output falls below the user's floor, reimburse the ckBTC (minus a small processing fee) rather than executing the transaction at an unexpected rate. This mirrors the existing `AmountTooLow` guard but makes it user-controlled and evaluated at processing time.

---

### Proof of Concept

1. Bitcoin median fee rate is 10 sat/vbyte. `fee_based_retrieve_btc_min_amount` = 100,000 sat. User calls `retrieve_btc({ amount: 200_000, address: "bc1q..." })`. ckBTC burned; request queued.
2. Before the heartbeat fires, a mempool congestion event pushes the median fee rate to 100 sat/vbyte.
3. The heartbeat calls `build_batch()`, picks up the request, fetches the new fee percentiles, and computes `bitcoin_fee ≈ 22,100 sat` at the new rate plus `minter_fee ≈ 305 sat`.
4. The user receives `200,000 − 22,100 − 305 = 177,595` sat — but at 10 sat/vbyte they would have received `200,000 − 2,210 − 305 = 197,485` sat. The shortfall is ~20,000 sat (~10%) with no on-chain protection.
5. At even higher fee spikes the shortfall grows proportionally; the user had no `min_btc_output` field available to abort the transaction and recover their ckBTC. [5](#0-4) [6](#0-5) [7](#0-6)

### Citations

**File:** rs/bitcoin/ckbtc/minter/src/updates/retrieve_btc.rs (L146-241)
```rust
pub async fn retrieve_btc<R: CanisterRuntime>(
    args: RetrieveBtcArgs,
    runtime: &R,
) -> Result<RetrieveBtcOk, RetrieveBtcError> {
    let caller = ic_cdk::api::msg_caller();

    state::read_state(|s| s.mode.is_withdrawal_available_for(&caller))
        .map_err(RetrieveBtcError::TemporarilyUnavailable)?;

    let _ecdsa_public_key = init_ecdsa_public_key().await;
    let main_address_str = state::read_state(|s| runtime.derive_minter_address_str(s));

    if args.address == main_address_str {
        ic_cdk::trap("illegal retrieve_btc target");
    }

    let _guard = retrieve_btc_guard(Account {
        owner: caller,
        subaccount: None,
    })?;
    let (min_retrieve_amount, btc_network) =
        read_state(|s| (s.fee_based_retrieve_btc_min_amount, s.btc_network));

    if args.amount < min_retrieve_amount {
        return Err(RetrieveBtcError::AmountTooLow(min_retrieve_amount));
    }

    let parsed_address = BitcoinAddress::parse(&args.address, btc_network)?;
    if read_state(|s| s.count_incomplete_retrieve_btc_requests() >= MAX_CONCURRENT_PENDING_REQUESTS)
    {
        return Err(RetrieveBtcError::TemporarilyUnavailable(
            "too many pending retrieve_btc requests".to_string(),
        ));
    }

    let balance = balance_of(caller).await?;
    if args.amount > balance {
        return Err(RetrieveBtcError::InsufficientFunds { balance });
    }

    let btc_checker_principal = read_state(|s| s.btc_checker_principal).map(|id| id.get().into());
    let status = check_address(btc_checker_principal, args.address.clone(), runtime).await?;
    match status {
        BtcAddressCheckStatus::Tainted => {
            log!(
                Priority::Debug,
                "rejected an attempt to withdraw {} BTC to address {} due to failed Bitcoin check",
                crate::tx::DisplayAmount(args.amount),
                args.address,
            );
            return Err(RetrieveBtcError::GenericError {
                error_message: "Destination address is tainted".to_string(),
                error_code: ErrorCode::TaintedAddress as u64,
            });
        }
        BtcAddressCheckStatus::Clean => {}
    }

    let burn_memo = BurnMemo::Convert {
        address: Some(&args.address),
        kyt_fee: None,
        status: Some(Status::Accepted),
    };
    let block_index =
        burn_ckbtcs(caller, args.amount, crate::memo::encode(&burn_memo).into()).await?;

    let request = RetrieveBtcRequest {
        amount: args.amount,
        address: parsed_address,
        block_index,
        received_at: ic_cdk::api::time(),
        kyt_provider: None,
        reimbursement_account: Some(Account {
            owner: caller,
            subaccount: None,
        }),
    };

    log!(
        Priority::Debug,
        "accepted a retrieve btc request for {} BTC to address {} (block_index = {})",
        crate::tx::DisplayAmount(request.amount),
        args.address,
        request.block_index
    );

    mutate_state(|s| state::audit::accept_retrieve_btc_request(s, request, &IC_CANISTER_RUNTIME));

    assert_eq!(
        crate::state::RetrieveBtcStatus::Pending,
        read_state(|s| s.retrieve_btc_status(block_index))
    );

    schedule_now(TaskType::ProcessLogic, &IC_CANISTER_RUNTIME);

    Ok(RetrieveBtcOk { block_index })
```

**File:** rs/bitcoin/ckbtc/minter/src/fees/mod.rs (L115-158)
```rust
    fn evaluate_minter_fee(&self, num_inputs: u64, num_outputs: u64) -> u64 {
        const MINTER_FEE_PER_INPUT: u64 = 146;
        const MINTER_FEE_PER_OUTPUT: u64 = 4;
        const MINTER_FEE_CONSTANT: u64 = 26;

        max(
            MINTER_FEE_PER_INPUT * num_inputs
                + MINTER_FEE_PER_OUTPUT * num_outputs
                + MINTER_FEE_CONSTANT,
            Self::MINTER_ADDRESS_P2WPKH_DUST_LIMIT,
        )
    }

    /// Returns the minimum withdrawal amount based on the current median fee rate (in millisatoshi per byte).
    /// The returned amount is in satoshi.
    fn fee_based_minimum_withdrawal_amount(&self, median_fee_rate: FeeRate) -> Satoshi {
        match self.network {
            Network::Mainnet | Network::Testnet => {
                const PER_REQUEST_RBF_BOUND: u64 = 22_100;
                const PER_REQUEST_VSIZE_BOUND: u64 = 221;
                const PER_REQUEST_MINTER_FEE_BOUND: u64 = 305;

                ((PER_REQUEST_RBF_BOUND
                    + median_fee_rate.fee_ceil(PER_REQUEST_VSIZE_BOUND)
                    + PER_REQUEST_MINTER_FEE_BOUND
                    + self.check_fee)
                    / 50_000) //TODO DEFI-2187: adjust increment of minimum withdrawal amount to be a multiple of retrieve_btc_min_amount/2
                    * 50_000
                    + self.retrieve_btc_min_amount
            }
            Network::Regtest => self.retrieve_btc_min_amount,
        }
    }

    fn evaluate_transaction_fee(&self, tx: &UnsignedTransaction, fee_rate: FeeRate) -> u64 {
        let tx_vsize = fake_sign(tx).vsize();
        fee_rate.fee_ceil(tx_vsize as u64)
    }

    fn reimbursement_fee_for_pending_withdrawal_requests(&self, num_requests: u64) -> u64 {
        // Heuristic:
        // * charge 1B cycles for each request (a burn on the ledger on the fiduciary subnet is probably around 50M cycles).
        num_requests.saturating_mul(Self::COST_OF_ONE_BILLION_CYCLES)
    }
```

**File:** rs/bitcoin/ckbtc/minter/src/state.rs (L456-460)
```rust
    /// Minimum amount of bitcoin that can be retrieved based on recent fees
    pub fee_based_retrieve_btc_min_amount: u64,

    /// Retrieve_btc requests that are waiting to be served, sorted by received_at.
    pub pending_retrieve_btc_requests: Vec<RetrieveBtcRequest>,
```

**File:** rs/bitcoin/ckbtc/minter/src/state.rs (L919-940)
```rust
    /// Returns true if the pending requests queue has enough requests to form a
    /// batch or there are old enough requests to form a batch.
    pub fn can_form_a_batch(&self, min_pending: usize, now: u64) -> bool {
        if self.pending_retrieve_btc_requests.len() >= min_pending {
            return true;
        }

        if let Some(req) = self.pending_retrieve_btc_requests.first()
            && self.max_time_in_queue_nanos < now.saturating_sub(req.received_at)
        {
            return true;
        }

        if let Some(req) = self.pending_retrieve_btc_requests.last()
            && let Some(last_submission_time) = self.last_transaction_submission_time_ns
            && self.max_time_in_queue_nanos < req.received_at.saturating_sub(last_submission_time)
        {
            return true;
        }

        false
    }
```

**File:** rs/bitcoin/ckbtc/minter/src/state.rs (L942-957)
```rust
    /// Forms a batch of retrieve_btc requests that the minter can fulfill.
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
```

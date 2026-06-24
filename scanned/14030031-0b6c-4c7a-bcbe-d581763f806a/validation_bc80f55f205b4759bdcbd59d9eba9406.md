### Title
`fee_based_minimum_withdrawal_amount` single-input vsize assumption allows fragmented UTXO pool to trigger unrecoverable `AmountTooLow` burn — (`rs/bitcoin/ckbtc/minter/src/fees/mod.rs`)

---

### Summary

`fee_based_minimum_withdrawal_amount` computes the minimum safe withdrawal amount using a hardcoded `PER_REQUEST_VSIZE_BOUND = 221` vbytes, which models a single-input P2WPKH transaction. When the minter's UTXO pool is fragmented into many small UTXOs, the greedy UTXO selection algorithm selects many inputs, making the actual transaction vsize far larger. The resulting `bitcoin_fee` from `evaluate_transaction_fee` (which uses the real vsize) can exceed the withdrawal amount, triggering `BuildTxError::AmountTooLow`. Critically, the `AmountTooLow` error path in `submit_pending_requests` does **not** reimburse the user — unlike the `InvalidTransaction` path which does — so the user's ckBTC is permanently burned with no BTC sent and no refund.

---

### Finding Description

**Step 1 — Minimum amount calculation (single-input assumption)** [1](#0-0) 

`PER_REQUEST_VSIZE_BOUND = 221` models one P2WPKH input + two outputs. Each additional P2WPKH input adds ~68 vbytes to the signed transaction.

**Step 2 — Withdrawal guard passes, ckBTC is burned** [2](#0-1) 

The guard only checks `args.amount >= fee_based_retrieve_btc_min_amount`. ckBTC is burned at line 210 before any transaction is built.

**Step 3 — Greedy UTXO selection with fragmented pool** [3](#0-2) 

`greedy` selects the smallest UTXO ≥ target, or the largest available UTXO if none qualifies, accumulating UTXOs until the target is met. With N small UTXOs each worth slightly less than the withdrawal amount, it selects all N.

**Step 4 — Actual fee computed from real vsize** [4](#0-3) 

`evaluate_transaction_fee` uses `fake_sign(tx).vsize()` — the real vsize with all N inputs. If `fee + minter_fee > amount`, `BuildTxError::AmountTooLow` is returned.

**Step 5 — No reimbursement on `AmountTooLow`** [5](#0-4) 

The `AmountTooLow` branch calls `remove_retrieve_btc_request` with `FinalizedStatus::AmountTooLow` — no call to `reimburse_canceled_requests`. Compare with the `InvalidTransaction` branch at lines 400–410 which **does** call `reimburse_canceled_requests`. [6](#0-5) 

`remove_retrieve_btc_request` only records the event and pushes to `finalized_requests`. No reimbursement is scheduled.

---

### Impact Explanation

A user who submits a withdrawal at exactly `fee_based_retrieve_btc_min_amount` loses their ckBTC permanently when the minter's UTXO pool is fragmented. The ckBTC ledger burn is irreversible; the BTC transaction is never sent; and the `AmountTooLow` terminal state carries no reimbursement path. The `RetrieveBtcStatusV2::AmountTooLow` variant in the DID confirms this is a dead-end status. [7](#0-6) 

---

### Likelihood Explanation

**Precondition — UTXO fragmentation**: Any unprivileged user can deposit BTC to the minter's address in small amounts (each ≥ `deposit_btc_min_amount` ≈ `check_fee + 1` ≈ 2,001 sats on mainnet). The consolidation mechanism only triggers when `available_utxos.len() > utxo_consolidation_threshold` (default configurable, typically 1,000+). [8](#0-7) 

With only ~100 small UTXOs (well below the 1,000 threshold), consolidation does not run, yet 100 inputs at 343 sat/vbyte produces a fee of ~2.4M sats against a 200,000-sat withdrawal.

**Concrete numeric example** at `fee_rate = 343,000` millisat/vbyte (minimum = 200,000 sats):
- 100 UTXOs × 2,001 sats each → greedy selects all 100
- vsize ≈ 68 × 100 + 105 = 6,905 vbytes
- `bitcoin_fee` = ⌈6,905 × 343⌉ = 2,368,415 sats
- `minter_fee` = 146 × 100 + 34 = 14,634 sats
- 2,383,049 >> 200,000 → `AmountTooLow`

The attack cost (depositing ~200,100 sats + on-chain fees) is comparable to the victim's loss, making this a griefing/DoS vector rather than a direct-profit exploit, but the user-fund loss is real and unrecoverable.

---

### Recommendation

1. **Reimbursement parity**: The `AmountTooLow` branch in `submit_pending_requests` should call `reimburse_canceled_requests` (minus a small processing fee), matching the behavior of the `InvalidTransaction` branch. This is the minimal fix to prevent permanent fund loss.

2. **Multi-input-aware minimum**: `fee_based_minimum_withdrawal_amount` should account for the worst-case number of inputs the greedy algorithm might select for the given amount, or use a conservative upper bound on input count.

3. **Lower consolidation threshold**: Reduce `utxo_consolidation_threshold` so fragmentation is addressed before it can affect minimum-amount withdrawals.

---

### Proof of Concept

```rust
// Construct a fragmented UTXO pool: 100 UTXOs of 2_001 sats each
let mut utxos: UtxoSet = (0..100u32)
    .map(|i| Utxo {
        outpoint: OutPoint { txid: [i as u8; 32].into(), vout: 0 },
        value: 2_001,
        height: 10,
    })
    .collect();

let fee_rate = FeeRate::from_millis_per_byte(343_000);
let fee_estimator = BitcoinFeeEstimator::new(Network::Mainnet, 100_000, 2_000);

// Minimum amount at this fee rate = 200_000 sats
let min_amount = fee_estimator.fee_based_minimum_withdrawal_amount(fee_rate);
assert_eq!(min_amount, 200_000);

// Build transaction at exactly the minimum — should succeed per the invariant
let result = build_unsigned_transaction(
    &mut utxos,
    vec![(BitcoinAddress::P2wpkhV0([1; 20]), min_amount)],
    &BitcoinAddress::P2wpkhV0([0; 20]),
    DEFAULT_MAX_NUM_INPUTS_IN_TRANSACTION,
    fee_rate,
    &fee_estimator,
);

// FAILS: returns Err(BuildTxError::AmountTooLow)
// because 100 inputs × ~68 vbytes = ~6905 vbytes → fee ≈ 2.37M sats >> 200_000 sats
assert!(result.is_ok(), "invariant violated: {:?}", result);
```

### Citations

**File:** rs/bitcoin/ckbtc/minter/src/fees/mod.rs (L133-144)
```rust
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
```

**File:** rs/bitcoin/ckbtc/minter/src/updates/retrieve_btc.rs (L166-171)
```rust
    let (min_retrieve_amount, btc_network) =
        read_state(|s| (s.fee_based_retrieve_btc_min_amount, s.btc_network));

    if args.amount < min_retrieve_amount {
        return Err(RetrieveBtcError::AmountTooLow(min_retrieve_amount));
    }
```

**File:** rs/bitcoin/ckbtc/minter/src/lib.rs (L412-434)
```rust
            Err(BuildTxError::AmountTooLow) => {
                log!(
                    Priority::Info,
                    "[submit_pending_requests]: dropping requests for total BTC amount {} to addresses {} (too low to cover the fees)",
                    tx::DisplayAmount(batch.iter().map(|req| req.amount).sum::<u64>()),
                    batch
                        .iter()
                        .map(|req| req.address.display(s.btc_network))
                        .collect::<Vec<_>>()
                        .join(",")
                );

                // There is no point in retrying the request because the
                // amount is too low.
                for request in batch {
                    state::audit::remove_retrieve_btc_request(
                        s,
                        request,
                        state::FinalizedStatus::AmountTooLow,
                        runtime,
                    );
                }
                None
```

**File:** rs/bitcoin/ckbtc/minter/src/lib.rs (L1049-1057)
```rust
    if available_utxos.len() > UTXOS_COUNT_THRESHOLD {
        while input_utxos.len() < output_count + 1 {
            if let Some(min_utxo) = available_utxos.pop_first() {
                input_utxos.push(min_utxo);
            } else {
                break;
            }
        }
    }
```

**File:** rs/bitcoin/ckbtc/minter/src/lib.rs (L1070-1099)
```rust
fn greedy(target: u64, available_utxos: &mut UtxoSet) -> Vec<Utxo> {
    #[cfg(feature = "canbench-rs")]
    let _scope = canbench_rs::bench_scope("greedy");

    let mut solution = vec![];
    let mut goal = target;
    while goal > 0 {
        let candidate_utxo = available_utxos
            .find_lower_bound(goal)
            .or_else(|| available_utxos.last())
            .cloned();
        match candidate_utxo {
            Some(utxo) => {
                let utxo = available_utxos.remove(&utxo).expect("BUG: missing UTXO");
                goal = goal.saturating_sub(utxo.value);
                solution.push(utxo);
            }
            None => {
                // Not enough available UTXOs to satisfy the request.
                for u in solution {
                    available_utxos.insert(u);
                }
                return vec![];
            }
        }
    }

    debug_assert!(solution.is_empty() || solution.iter().map(|u| u.value).sum::<u64>() >= target);

    solution
```

**File:** rs/bitcoin/ckbtc/minter/src/lib.rs (L1304-1308)
```rust
    let fee = fee_estimator.evaluate_transaction_fee(&unsigned_tx, fee_rate);

    if fee + minter_fee > amount {
        return Err(BuildTxError::AmountTooLow);
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

**File:** rs/bitcoin/ckbtc/minter/src/state.rs (L259-267)
```rust
pub enum FinalizedStatus {
    /// The request amount was to low to cover the fees.
    AmountTooLow,
    /// The transaction that retrieves BTC got enough confirmations.
    Confirmed {
        /// The witness transaction identifier of the transaction.
        txid: Txid,
    },
}
```

### Title
Fee Estimate vs. Actual Fee Divergence in Batched Withdrawals — (`File: rs/bitcoin/ckbtc/minter/src/queries.rs`)

---

### Summary

The `estimate_withdrawal_fee` query in the ckBTC (and ckDOGE) minter always simulates a **single-output** transaction, but the actual `submit_pending_requests` path batches multiple withdrawal requests into a **multi-output** transaction. Because the `minter_fee` is a function of `num_outputs`, and the Bitcoin transaction fee is a function of transaction vsize (which grows with output count), the fee estimate returned to users is systematically lower than the fee actually deducted when their request is batched with others. Users who rely on the estimate to plan their withdrawal will receive less BTC/DOGE than the estimate implies.

---

### Finding Description

The public query endpoint `estimate_withdrawal_fee` (ckBTC: `rs/bitcoin/ckbtc/minter/src/main.rs:220`, ckDOGE: `rs/dogecoin/ckdoge/minter/src/main.rs:99`) calls `estimate_retrieve_btc_fee` / `estimate_retrieve_doge_fee`, which in turn calls `estimate_withdrawal_fee` in `rs/bitcoin/ckbtc/minter/src/queries.rs:46`.

That function always simulates a **single-output** transaction:

```rust
let selected_utxos = utxos_selection(withdrawal_amount, available_utxos, 1);
build_unsigned_transaction_from_inputs(
    &selected_utxos,
    vec![(recipient_address, withdrawal_amount)],  // exactly 1 output
    ...
)
```

Inside `build_unsigned_transaction_from_inputs` (`rs/bitcoin/ckbtc/minter/src/lib.rs:1264`), the minter fee is computed as:

```rust
let minter_fee =
    fee_estimator.evaluate_minter_fee(input_utxos.len() as u64, (outputs.len() + 1) as u64);
```

For the estimate, `outputs.len() == 1`, so `num_outputs == 2` (1 recipient + 1 change).

However, the actual submission path (`submit_pending_requests`, `rs/bitcoin/ckbtc/minter/src/lib.rs:366`) builds a **batch** of up to `MAX_REQUESTS_PER_BATCH` outputs:

```rust
let outputs: Vec<_> = batch
    .iter()
    .map(|req| (req.address.clone(), req.amount))
    .collect();
match build_unsigned_transaction(&mut s.available_utxos, outputs, ...) { ... }
```

When `N` requests are batched, `num_outputs == N + 1`, so:
- `minter_fee` is higher by `MINTER_FEE_PER_OUTPUT * (N - 1)` satoshis (146 sat/input + 4 sat/output formula).
- The Bitcoin transaction vsize is larger (more outputs → more bytes → higher `bitcoin_fee`).
- The total fee is distributed across all `N` outputs via `distribute(fee + minter_fee, N)`, meaning each user pays `(fee + minter_fee) / N` — but the total fee is larger than what the single-output estimate predicted.

The net effect: a user who calls `estimate_withdrawal_fee` and sees fee `F` will actually have `F' > F` deducted from their withdrawal output when their request is batched.

---

### Impact Explanation

Any unprivileged user who calls the `estimate_withdrawal_fee` query endpoint (reachable without authentication) and uses the result to plan a withdrawal will receive **less BTC or DOGE** than the estimate implies. The discrepancy grows with batch size. For example, with `MAX_REQUESTS_PER_BATCH = 5` and `MINTER_FEE_PER_OUTPUT = 4` sat, the minter fee alone is underestimated by `4 * (5-1) = 16` sat per batch, plus the additional Bitcoin network fee from the larger transaction vsize. While small per-transaction, this is a systematic and predictable divergence between the advertised fee and the actual fee charged — analogous to the `previewWithdraw` bug in the report. Users cannot accurately predict their net received amount.

---

### Likelihood Explanation

This divergence occurs whenever more than one withdrawal request is pending simultaneously, which is the normal operating condition on mainnet. The minter explicitly batches requests (`build_batch(MAX_REQUESTS_PER_BATCH)`). Any user submitting a withdrawal during a period of moderate activity will be batched and will experience the discrepancy. The entry path is fully unprivileged: call `estimate_withdrawal_fee` (query), then `retrieve_btc_with_approval` (update).

---

### Recommendation

The `estimate_withdrawal_fee` simulation should account for the possibility of batching. At minimum, the documentation should clearly state that the estimate assumes a single-output transaction and the actual fee may be higher. Ideally, the estimate should simulate a worst-case batch scenario, or the minter should guarantee that each request is processed in its own transaction (which would eliminate the divergence but reduce throughput).

---

### Proof of Concept

**Step 1 — Observe the estimate (single-output simulation):**

`estimate_withdrawal_fee` in `rs/bitcoin/ckbtc/minter/src/queries.rs:57` calls `utxos_selection(withdrawal_amount, available_utxos, 1)` and builds a transaction with exactly one recipient output. [1](#0-0) 

**Step 2 — Observe the minter fee formula (output-count dependent):**

`evaluate_minter_fee` charges `MINTER_FEE_PER_OUTPUT * num_outputs` where `num_outputs = outputs.len() + 1`. [2](#0-1) 

For the estimate: `num_outputs = 2` (1 recipient + 1 change).

**Step 3 — Observe the actual batch path (multi-output):**

`submit_pending_requests` collects all pending requests into a `batch` and builds a single transaction with `batch.len()` outputs. [3](#0-2) 

**Step 4 — Observe the minter fee calculation in the actual transaction:**

`build_unsigned_transaction_from_inputs` computes `minter_fee` using `(outputs.len() + 1)` as `num_outputs`. [4](#0-3) 

**Step 5 — Observe fee distribution across batch outputs:**

The total fee is split across all `N` outputs via `distribute(fee + minter_fee, outputs.len())`. [5](#0-4) 

**Concrete numbers (ckBTC, 5-request batch, 1 sat/vbyte):**
- Estimate: `minter_fee = max(146*I + 4*2 + 26, 300)` for `I` inputs, `bitcoin_fee` for 1-output tx vsize.
- Actual: `minter_fee = max(146*I + 4*6 + 26, 300)` for same `I` inputs, `bitcoin_fee` for 5-output tx vsize (larger).
- Per-user fee share from actual batch > per-user estimate → user receives fewer satoshis than previewed.

The ckDOGE minter has the identical issue via `estimate_retrieve_doge_fee` which delegates to the same `estimate_withdrawal_fee` function. [6](#0-5)

### Citations

**File:** rs/bitcoin/ckbtc/minter/src/queries.rs (L55-65)
```rust
    // We simulate the algorithm that selects UTXOs for the
    // specified amount.
    let selected_utxos = utxos_selection(withdrawal_amount, available_utxos, 1);

    build_unsigned_transaction_from_inputs(
        &selected_utxos,
        vec![(recipient_address, withdrawal_amount)],
        &minter_address,
        max_num_inputs_in_transaction,
        median_fee_millisatoshi_per_vbyte,
        fee_estimator,
```

**File:** rs/bitcoin/ckbtc/minter/src/fees/mod.rs (L115-126)
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
```

**File:** rs/bitcoin/ckbtc/minter/src/lib.rs (L366-384)
```rust
        let batch = s.build_batch(MAX_REQUESTS_PER_BATCH);

        if batch.is_empty() {
            return None;
        }

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
```

**File:** rs/bitcoin/ckbtc/minter/src/lib.rs (L1264-1265)
```rust
    let minter_fee =
        fee_estimator.evaluate_minter_fee(input_utxos.len() as u64, (outputs.len() + 1) as u64);
```

**File:** rs/bitcoin/ckbtc/minter/src/lib.rs (L1310-1328)
```rust
    let fee_shares = distribute(fee + minter_fee, outputs.len() as u64);

    // The last output has to match the main_address.
    debug_assert!(matches!(unsigned_tx.outputs.iter().last(),
        Some(tx::TxOut { value: _, address }) if address == main_address));

    for (output, fee_share) in unsigned_tx
        .outputs
        .iter_mut()
        .zip(fee_shares.iter())
        .take(num_outputs - 1)
    {
        if output.value <= *fee_share + F::DUST_LIMIT {
            return Err(BuildTxError::DustOutput {
                address: output.address.clone(),
                amount: output.value,
            });
        }
        output.value = output.value.saturating_sub(*fee_share);
```

**File:** rs/dogecoin/ckdoge/minter/src/fees/mod.rs (L130-151)
```rust
pub fn estimate_retrieve_doge_fee<F: FeeEstimator>(
    available_utxos: &mut UtxoSet,
    withdrawal_amount: u64,
    median_fee_millikoinu_per_byte: FeeRate,
    max_num_inputs_in_transaction: usize,
    fee_estimator: &F,
) -> Result<WithdrawalFee, BuildTxError> {
    // We simulate the algorithm that selects UTXOs for the specified amount.
    // Only the address type matters for the amount of bytes, not the actual bytes in the address.
    let dummy_minter_address = BitcoinAddress::P2pkh([u8::MAX; 20]);
    let dummy_recipient_address = BitcoinAddress::P2pkh([42_u8; 20]);

    ic_ckbtc_minter::queries::estimate_withdrawal_fee(
        available_utxos,
        withdrawal_amount,
        median_fee_millikoinu_per_byte,
        dummy_minter_address,
        dummy_recipient_address,
        max_num_inputs_in_transaction,
        fee_estimator,
    )
    .map(WithdrawalFee::from)
```

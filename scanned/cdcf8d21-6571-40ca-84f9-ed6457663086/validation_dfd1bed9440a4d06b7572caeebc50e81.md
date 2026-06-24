### Title
Sequential Per-Input `sign_with_ecdsa` Calls in ckBTC Minter Block Withdrawal Pipeline — (File: `rs/bitcoin/ckbtc/minter/src/tx.rs`)

---

### Summary

`BitcoinTransactionSigner::sign_transaction` issues one `sign_with_ecdsa` inter-canister call **per transaction input** inside a sequential `for` loop. Because the ckBTC minter processes exactly one signing job at a time, an attacker who floods the minter with many small UTXO deposits can force the next withdrawal transaction to consume a large number of inputs, serializing many slow threshold-ECDSA calls and stalling the entire withdrawal pipeline for all users during that window.

---

### Finding Description

In `rs/bitcoin/ckbtc/minter/src/tx.rs`, `BitcoinTransactionSigner::sign_transaction` iterates over every input of the unsigned Bitcoin transaction and awaits a separate `sign_with_ecdsa` management-canister call for each one:

```rust
for (input, account) in unsigned_tx.inputs.iter().zip(accounts) {
    // ...
    let sec1_signature =
        management::sign_with_ecdsa(self.key_name.clone(), path, sighash, runtime).await?;
    // ...
}
``` [1](#0-0) 

Each `sign_with_ecdsa` call is a full inter-canister round-trip to the management canister, which itself requires subnet-wide threshold-ECDSA coordination. There is no `join_all` or equivalent parallelism — the calls are strictly sequential. The total signing latency therefore scales linearly with the number of inputs.

This signing is invoked from `sign_and_submit_request` in `lib.rs`, which is the only code path that advances a withdrawal from "pending" to "submitted": [2](#0-1) 

The outer `submit_pending_requests` function, called from the heartbeat/timer, builds exactly one batch per invocation and then calls `sign_and_submit_request` for it: [3](#0-2) 

Because the minter's timer logic processes one batch at a time and the signing is sequential within that batch, the entire withdrawal pipeline is serialized behind the signing loop. While signing is in progress, no other withdrawal can advance.

---

### Impact Explanation

An attacker who has deposited many small UTXOs into the ckBTC minter (or who has caused many small UTXOs to accumulate through normal use) can trigger a withdrawal that requires a large number of inputs. The minter will then spend many consecutive timer ticks doing nothing but awaiting sequential `sign_with_ecdsa` calls. During this entire window, every other pending withdrawal request is stalled. The effect is a targeted, low-cost denial-of-service against the ckBTC withdrawal pipeline: legitimate users who submitted withdrawal requests before or during the attack will experience unbounded delays proportional to the number of inputs the attacker forced into the transaction. [4](#0-3) 

---

### Likelihood Explanation

The attack entry point is the public `retrieve_btc` / `retrieve_btc_with_approval` endpoint, callable by any unprivileged principal. The attacker's cost is the BTC required to create many small UTXOs (dust-level deposits above `deposit_btc_min_amount`). The `max_num_inputs_in_transaction` parameter bounds the worst case per transaction, but the bound is configurable and can be large; even at a moderate value (e.g., 100 inputs), 100 sequential threshold-ECDSA calls represent a significant stall. The attack is repeatable: after one transaction is signed and submitted, the attacker can trigger another batch with more small UTXOs. No privileged access, key material, or subnet-majority corruption is required. [5](#0-4) 

---

### Recommendation

Replace the sequential `for` loop in `BitcoinTransactionSigner::sign_transaction` with parallel signing using `join_all` (or equivalent), issuing all `sign_with_ecdsa` calls concurrently and collecting results. This mirrors the approach already used in the ckETH minter's `sign_transactions_batch`, which uses `join_all` to sign multiple Ethereum transactions in parallel: [6](#0-5) 

Additionally, document the expected maximum number of inputs per transaction and enforce a tight, well-reasoned bound on `max_num_inputs_in_transaction` to limit worst-case signing latency even if parallelism is not immediately feasible.

---

### Proof of Concept

1. Attacker calls `retrieve_btc_with_approval` many times with the minimum deposit amount, creating N small UTXOs in the minter's available UTXO pool.
2. Attacker (or any user) submits a withdrawal large enough that the minter's UTXO selection algorithm must consume many of those small UTXOs as inputs (up to `max_num_inputs_in_transaction`).
3. The minter's heartbeat fires `submit_pending_requests`, builds the batch, and calls `sign_and_submit_request`.
4. `BitcoinTransactionSigner::sign_transaction` enters the `for` loop and issues N sequential `sign_with_ecdsa` calls, each awaiting a full threshold-ECDSA round-trip.
5. All other withdrawal requests queued by legitimate users remain unprocessed until all N signing calls complete.
6. The attacker repeats from step 1 to sustain the stall. [7](#0-6) [8](#0-7)

### Citations

**File:** rs/bitcoin/ckbtc/minter/src/tx.rs (L686-711)
```rust
        let mut signed_inputs = Vec::with_capacity(unsigned_tx.inputs.len());
        let sighasher = tx::TxSigHasher::new(&unsigned_tx);
        for (input, account) in unsigned_tx.inputs.iter().zip(accounts) {
            let outpoint = &input.previous_output;

            let path = derivation_path(&account)
                .into_iter()
                .map(|buf| buf.to_vec())
                .collect();
            let pubkey = ByteBuf::from(
                derive_public_key_from_account(&self.ecdsa_public_key, &account).public_key,
            );
            let pkhash = tx::hash160(&pubkey);

            let sighash = sighasher.sighash(input, &pkhash);

            let sec1_signature =
                management::sign_with_ecdsa(self.key_name.clone(), path, sighash, runtime).await?;

            signed_inputs.push(tx::SignedInput {
                signature: signature::EncodedSignature::from_sec1(&sec1_signature),
                pubkey,
                previous_output: outpoint.clone(),
                sequence: input.sequence,
            });
        }
```

**File:** rs/bitcoin/ckbtc/minter/src/lib.rs (L56-57)
```rust
pub const MIN_PENDING_REQUESTS: usize = 20;
pub const MAX_REQUESTS_PER_BATCH: usize = 100;
```

**File:** rs/bitcoin/ckbtc/minter/src/lib.rs (L348-400)
```rust
async fn submit_pending_requests<R: CanisterRuntime>(runtime: &R) {
    // We make requests if we have old requests in the queue or if have enough
    // requests to fill a batch.
    if !state::read_state(|s| s.can_form_a_batch(MIN_PENDING_REQUESTS, runtime.time())) {
        return;
    }

    let ecdsa_public_key = updates::get_btc_address::init_ecdsa_public_key().await;
    let main_address = state::read_state(|s| runtime.derive_minter_address(s));

    let fee_millisatoshi_per_vbyte = match estimate_fee_per_vbyte(runtime).await {
        Some(fee) => fee,
        None => return,
    };
    let fee_estimator = read_state(|s| runtime.fee_estimator(s));
    let max_num_inputs_in_transaction = read_state(|s| s.max_num_inputs_in_transaction);

    let maybe_sign_request = state::mutate_state(|s| {
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
                    utxos,
                },
                total_fee,
            )),
            Err(BuildTxError::InvalidTransaction(err)) => {
```

**File:** rs/bitcoin/ckbtc/minter/src/lib.rs (L494-531)
```rust
async fn sign_and_submit_request<R: CanisterRuntime>(
    req: SignTxRequest,
    total_fee: WithdrawalFee,
    runtime: &R,
) -> Result<Txid, CallError> {
    log!(
        Priority::Debug,
        "[submit_pending_requests]: signing a new transaction: {}",
        hex::encode(tx::encode_into(&req.unsigned_tx, Vec::new()))
    );

    state::mutate_state(|s| {
        for block_index in req.requests.iter_block_index() {
            s.push_in_flight_request(block_index, state::InFlightStatus::Signing);
        }
    });

    // This guard ensures that we return pending requests and UTXOs back to
    // the state if the signing of a transaction fails or panics.
    let requests_guard = guard((req.requests, req.utxos), |(reqs, utxos)| {
        undo_withdrawal_request(reqs, utxos);
    });

    let signed_tx = runtime
        .sign_transaction(
            req.key_name,
            req.ecdsa_public_key,
            req.unsigned_tx,
            req.accounts,
        )
        .await
        .inspect_err(|err| {
            log!(
                Priority::Info,
                "[sign_and_submit_request]: failed to sign a Bitcoin transaction: {}",
                err
            );
        })?;
```

**File:** rs/ethereum/cketh/minter/src/withdraw.rs (L303-314)
```rust
async fn sign_transactions_batch() {
    let transactions_batch: Vec<_> = read_state(|s| {
        s.eth_transactions
            .transactions_to_sign_batch(TRANSACTIONS_TO_SIGN_BATCH_SIZE)
    });
    log!(DEBUG, "Signing transactions {transactions_batch:?}");
    let results = join_all(
        transactions_batch
            .into_iter()
            .map(|(withdrawal_id, tx)| async move { (withdrawal_id, tx.sign().await) }),
    )
    .await;
```

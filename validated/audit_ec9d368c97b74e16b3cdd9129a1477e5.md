### Title
Excess Cycles Consumed Without Performing Work in `try_fetch_tx` When Insufficient Cycles Are Available - (`File: rs/bitcoin/checker/src/fetch.rs`)

---

### Summary

In the Bitcoin Checker canister, the `try_fetch_tx` function unconditionally calls `cycles_accept(cycle_cost)` before verifying that enough cycles are available. Because `msg_cycles_accept` accepts `min(available, requested)`, when the available balance is less than `cycle_cost`, all remaining cycles are silently consumed and the function returns `NotEnoughCycles` — without performing any HTTP outcall. This is the direct IC analog of the Solidity `msg.value >= fee` pattern where excess/partial payment is absorbed rather than refunded.

---

### Finding Description

In `rs/bitcoin/checker/src/fetch.rs`, the `try_fetch_tx` method of the `FetchEnv` trait performs the following cycle check:

```rust
let cycle_cost = get_tx_cycle_cost(max_response_bytes, num_subnet_nodes);
if self.cycles_accept(cycle_cost) < cycle_cost {
    TryFetchResult::NotEnoughCycles
} else {
    TryFetchResult::ToFetch(self.fetch_tx(guard, provider, txid, max_response_bytes))
}
``` [1](#0-0) 

`cycles_accept` maps directly to `ic_cdk::api::msg_cycles_accept(cycle_cost)`: [2](#0-1) 

The IC system call `msg_cycles_accept(n)` accepts `min(available, n)` cycles and returns the amount actually accepted. When `available < cycle_cost`, it accepts **all remaining available cycles** and returns a value less than `cycle_cost`. The code then correctly identifies this as `NotEnoughCycles` — but the cycles that were available have already been irrevocably consumed by the canister, with no HTTP outcall performed in exchange.

This is compounded in `check_fetched`, which iterates over all missing input addresses and calls `try_fetch_tx` for each one in a loop:

```rust
for (index, input) in fetched.tx.inputs.iter().enumerate() {
    if fetched.input_addresses[index].is_none() {
        match self.try_fetch_tx(input.txid) {
            ...
            NotEnoughCycles => { not_enough_cycles = true; }
        }
    }
}
``` [3](#0-2) 

When the first `try_fetch_tx` call exhausts the remaining cycles, subsequent calls in the same loop also invoke `cycles_accept`, each time consuming zero (since nothing is left) but still executing the accept path. The net result is that all cycles beyond the service fee are consumed without completing the work.

The test suite explicitly documents and asserts this behavior:

```rust
// Check available cycles: we deduct all remaining cycles even when they are not enough
assert_eq!(env.cycles_available(), 0);
``` [4](#0-3) 

And for the multi-input partial case:

```rust
// Check remaining cycle: we deduct all remaining cycles when they are not enough
assert_eq!(env.cycles_available(), 0);
``` [5](#0-4) 

The canister documentation states that unspent cycles are refunded:

> "The actual cycle cost may be well less than `CHECK_TRANSACTION_CYCLES_REQUIRED`, and unspent cycles will be refunded back to the caller, minus a `CHECK_TRANSACTION_CYCLES_SERVICE_FEE`" [6](#0-5) 

This promise is violated when a transaction has enough inputs that the cycle budget is exhausted mid-way through `check_fetched`.

---

### Impact Explanation

A caller (e.g., the ckBTC minter) sends `CHECK_TRANSACTION_CYCLES_REQUIRED` (40 billion cycles) to `check_transaction`. The `check_transaction_with` wrapper accepts the 100M service fee and verifies the remaining balance is sufficient before dispatching to `check_transaction_inputs`. However, if the transaction under check has many inputs — particularly inputs whose parent transactions require the large 400 KB retry buffer (`RETRY_MAX_RESPONSE_BYTES = 400 * 1024`), each costing approximately 4.26 billion cycles — the budget can be exhausted after ~9 outcalls. The 10th `try_fetch_tx` call then consumes whatever cycles remain (e.g., hundreds of millions) without performing any outcall. The caller receives `NotEnoughCycles` and must retry, having lost cycles that should have been refunded. Over repeated retries on a complex transaction, this results in measurable financial loss to the caller. [7](#0-6) 

---

### Likelihood Explanation

The ckBTC minter calls `check_transaction` for every deposit and withdrawal. Transactions with many inputs (common in Bitcoin consolidation transactions or high-fan-in deposits) and/or inputs requiring the large 400 KB response buffer are realistic on mainnet. The `check_transaction_with` guard only checks that the total attached cycles meet the minimum threshold; it does not guarantee that the budget is sufficient for all inputs of an arbitrarily complex transaction. Any canister caller — including the production ckBTC minter — can trigger this path without any privileged access, simply by submitting a `check_transaction` call for a Bitcoin transaction with sufficiently many or large inputs. [8](#0-7) 

---

### Recommendation

Check cycle availability **before** accepting, so that cycles are only consumed when the outcall will actually proceed:

```diff
- if self.cycles_accept(cycle_cost) < cycle_cost {
-     TryFetchResult::NotEnoughCycles
- } else {
-     TryFetchResult::ToFetch(self.fetch_tx(guard, provider, txid, max_response_bytes))
- }
+ if self.cycles_available() < cycle_cost {
+     TryFetchResult::NotEnoughCycles
+ } else {
+     self.cycles_accept(cycle_cost);
+     TryFetchResult::ToFetch(self.fetch_tx(guard, provider, txid, max_response_bytes))
+ }
```

This requires adding a `cycles_available()` method to the `FetchEnv` trait (mirroring `cycles_accept`). With this change, cycles are only accepted when there is a confirmed budget for the corresponding HTTP outcall, and any remaining cycles are automatically refunded by the IC protocol at message completion. [9](#0-8) 

---

### Proof of Concept

1. Deploy the Bitcoin Checker canister in `CheckMode::Normal` with a 13-node subnet configuration.
2. Submit a `check_transaction` call for a Bitcoin transaction with 2 inputs, attaching exactly `1.5 × get_tx_cycle_cost(INITIAL_MAX_RESPONSE_BYTES, 13)` cycles (after the service fee is deducted).
3. Observe that `check_fetched` calls `try_fetch_tx` for both inputs. The first call accepts `cycle_cost` and succeeds. The second call finds `available < cycle_cost`, accepts all remaining cycles (~0.5× cost), and returns `NotEnoughCycles`.
4. Assert `cycles_available() == 0` — all cycles are gone, yet only one outcall was performed.
5. The caller must retry with fresh cycles, having lost the 0.5× remainder with no work done in exchange.

This exact scenario is already encoded in the existing test suite at `rs/bitcoin/checker/src/fetch/tests.rs` lines 381–397, which asserts `cycles_available() == 0` after the partial-budget case and explicitly comments "we deduct all remaining cycles when they are not enough." [10](#0-9)

### Citations

**File:** rs/bitcoin/checker/src/fetch.rs (L62-62)
```rust
    fn cycles_accept(&self, cycles: u128) -> u128;
```

**File:** rs/bitcoin/checker/src/fetch.rs (L97-103)
```rust
        let num_subnet_nodes = self.config().num_subnet_nodes;
        let cycle_cost = get_tx_cycle_cost(max_response_bytes, num_subnet_nodes);
        if self.cycles_accept(cycle_cost) < cycle_cost {
            TryFetchResult::NotEnoughCycles
        } else {
            TryFetchResult::ToFetch(self.fetch_tx(guard, provider, txid, max_response_bytes))
        }
```

**File:** rs/bitcoin/checker/src/fetch.rs (L196-226)
```rust
        for (index, input) in fetched.tx.inputs.iter().enumerate() {
            if fetched.input_addresses[index].is_none() {
                use TryFetchResult::*;
                match self.try_fetch_tx(input.txid) {
                    ToFetch(do_fetch) => {
                        jobs.push((index, input.txid, input.vout));
                        futures.push(do_fetch)
                    }
                    Fetched(fetched) => {
                        if let Some(address) = &fetched.tx.outputs[input.vout as usize] {
                            state::set_fetched_address(txid, index, address.clone());
                        } else {
                            // This error shouldn't happen unless blockdata is corrupted.
                            let msg = format!(
                                "Tx {} vout {} has no address, but is vin {} of tx {}",
                                input.txid, input.vout, index, txid
                            );
                            log!(WARN, "{msg}");
                            return CheckTransactionIrrecoverableError::InvalidTransaction(msg)
                                .into();
                        }
                    }
                    Pending => {}
                    HighLoad => {
                        high_load = true;
                    }
                    NotEnoughCycles => {
                        not_enough_cycles = true;
                    }
                }
            }
```

**File:** rs/bitcoin/checker/src/main.rs (L112-114)
```rust
/// The actual cycle cost may be well less than `CHECK_TRANSACTION_CYCLES_REQUIRED`, and
/// unspent cycles will be refunded back to the caller, minus a
/// `CHECK_TRANSACTION_CYCLES_SERVICE_FEE`, which is always deducted regardless.
```

**File:** rs/bitcoin/checker/src/main.rs (L144-168)
```rust
async fn check_transaction_with<F: FnOnce() -> Result<Txid, String>>(
    get_txid: F,
) -> CheckTransactionResponse {
    if ic_cdk::api::msg_cycles_accept(CHECK_TRANSACTION_CYCLES_SERVICE_FEE)
        < CHECK_TRANSACTION_CYCLES_SERVICE_FEE
    {
        return CheckTransactionStatus::NotEnoughCycles.into();
    }

    match get_txid() {
        Ok(txid) => {
            STATS.with(|s| s.borrow_mut().check_transaction_count += 1);
            if ic_cdk::api::msg_cycles_available()
                .checked_add(CHECK_TRANSACTION_CYCLES_SERVICE_FEE)
                .unwrap()
                < CHECK_TRANSACTION_CYCLES_REQUIRED
            {
                CheckTransactionStatus::NotEnoughCycles.into()
            } else {
                check_transaction_inputs(txid).await
            }
        }
        Err(err) => CheckTransactionIrrecoverableError::InvalidTransactionId(err).into(),
    }
}
```

**File:** rs/bitcoin/checker/src/main.rs (L516-518)
```rust
    fn cycles_accept(&self, cycles: u128) -> u128 {
        ic_cdk::api::msg_cycles_accept(cycles)
    }
```

**File:** rs/bitcoin/checker/src/fetch/tests.rs (L378-379)
```rust
    // Check available cycles: we deduct all remaining cycles even when they are not enough
    assert_eq!(env.cycles_available(), 0);
```

**File:** rs/bitcoin/checker/src/fetch/tests.rs (L381-397)
```rust
    // case Pending: need 2 inputs, but only able to get 1 for now
    let env =
        MockEnv::new(get_tx_cycle_cost(INITIAL_MAX_RESPONSE_BYTES, TEST_SUBNET_NODES) * 3 / 2);
    let fetched = FetchedTx {
        tx: from_tx(&tx_0),
        input_addresses: vec![None, None],
    };
    state::set_fetch_status(txid_0, FetchTxStatus::Fetched(fetched.clone()));
    env.expect_get_tx_with_reply(Ok(tx_1.clone()));
    assert!(matches!(
        env.check_fetched(txid_0, &fetched).await,
        CheckTransactionResponse::Unknown(CheckTransactionStatus::Retriable(
            CheckTransactionRetriable::Pending
        ))
    ));
    // Check remaining cycle: we deduct all remaining cycles when they are not enough
    assert_eq!(env.cycles_available(), 0);
```

**File:** rs/bitcoin/checker/lib/lib.rs (L7-10)
```rust
pub const CHECK_TRANSACTION_CYCLES_REQUIRED: u128 = 40_000_000_000;

/// One-time charge for every check_transaction call.
pub const CHECK_TRANSACTION_CYCLES_SERVICE_FEE: u128 = 100_000_000;
```

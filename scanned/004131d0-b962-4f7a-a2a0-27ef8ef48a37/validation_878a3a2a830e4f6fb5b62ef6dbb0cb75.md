### Title
Bitcoin Checker Canister Provider Rotation Has No Exhaustion Detection, Causing Permanent ckBTC Deposit Liveness Failure When All Providers Are Down - (File: rs/bitcoin/checker/src/fetch.rs)

### Summary
The Bitcoin Checker canister's `try_fetch_tx` function rotates through Bitcoin API providers indefinitely when all providers fail, with no mechanism to detect exhaustion or declare a transaction permanently unfetchable. If all providers are simultaneously and permanently unavailable, the canister perpetually returns a retriable error, causing the ckBTC minter to permanently block `update_balance` calls and lock deposited user BTC with no on-chain escape path.

### Finding Description
In `rs/bitcoin/checker/src/fetch.rs`, `try_fetch_tx` handles the `FetchTxStatus::Error` case by advancing to the next provider:

```rust
Some(FetchTxStatus::Error(err)) => (
    // An FetchTxStatus error can be retried with another provider
    err.provider.next(),
    // The next provider can use the same max_response_bytes
    err.max_response_bytes,
),
``` [1](#0-0) 

`Provider::next()` cycles through a fixed ring of three providers for Mainnet (Btcscan → Blockstream → MempoolSpace → Btcscan):

```rust
(BtcNetwork::Mainnet, ProviderId::Btcscan) => ProviderId::Blockstream,
(BtcNetwork::Mainnet, ProviderId::Blockstream) => ProviderId::MempoolSpace,
(BtcNetwork::Mainnet, ProviderId::MempoolSpace) => ProviderId::Btcscan,
``` [2](#0-1) 

`FetchTxStatusError` stores only the **last** provider that failed, not a count of how many have been tried:

```rust
pub struct FetchTxStatusError {
    pub provider: Provider,
    pub max_response_bytes: u32,
    pub error: HttpGetTxError,
}
``` [3](#0-2) 

When all three providers fail for the same `txid`, the state machine cycles indefinitely:

| Call | Stored status | Provider tried | Result |
|------|--------------|----------------|--------|
| 1 | None | A (Btcscan) | Error → store `Error{A}` |
| 2 | `Error{A}` | B (Blockstream) | Error → store `Error{B}` |
| 3 | `Error{B}` | C (MempoolSpace) | Error → store `Error{C}` |
| 4 | `Error{C}` | A (Btcscan) | Error → store `Error{A}` |
| … | … | … | … forever |

Every failed fetch stores the new error and returns `FetchResult::Error(err)`. In `check_transaction_inputs`, this is converted via `err.into_response(txid)`:

```rust
Ok(FetchResult::Error(err)) => err.into_response(txid),
``` [4](#0-3) 

`into_response` maps all non-`ResponseTooLarge` errors (including `Rejected`, `TxEncoding`, `CallPerformFailed`) to `CheckTransactionRetriable::TransientInternalError`:

```rust
HttpGetTxError::Rejected { message, .. } => {
    CheckTransactionRetriable::TransientInternalError(message).into()
}
_ => CheckTransactionRetriable::TransientInternalError(self.to_string()).into(),
``` [5](#0-4) 

The ckBTC minter's `check_utxo` treats any `Retriable` response as `TemporarilyUnavailable` and returns immediately to the caller:

```rust
CheckTransactionResponse::Unknown(CheckTransactionStatus::Retriable(status)) => {
    return Err(UpdateBalanceError::TemporarilyUnavailable(format!(
        "The Bitcoin checker canister is temporarily unavailable: {status:?}"
    )));
}
``` [6](#0-5) 

There is no counter, no per-transaction retry budget, and no terminal `PermanentlyFailed` state. The `FetchTxStatus::Error` entry persists in the heap-memory cache (up to 10,000 entries, evicted only by LRU pressure) until the canister is upgraded via a governance proposal. [7](#0-6) 

The same cycling logic applies inside `check_fetched` when fetching **input transactions** of an already-fetched main transaction, compounding the issue for multi-input Bitcoin transactions. [8](#0-7) 

### Impact Explanation
Any user who sends BTC to the ckBTC minter's address and then calls `update_balance` will receive `UpdateBalanceError::TemporarilyUnavailable` indefinitely. Because the Bitcoin Checker canister never transitions the `FetchTxStatus` to a terminal error state, the ckBTC minter has no way to mint ckBTC for the deposit. The deposited BTC is locked in the minter-controlled Bitcoin address with no on-chain mechanism for the user to recover it. The only remediation path is an NNS governance proposal to upgrade the Bitcoin Checker canister with new providers or a different retry strategy, which introduces significant delay. Withdrawals (`retrieve_btc`) are unaffected because `check_address` is a pure in-memory SDN list lookup and does not perform HTTP outcalls.

### Likelihood Explanation
The condition requires all three Bitcoin API providers (Btcscan, Blockstream, MempoolSpace) to be simultaneously and permanently unreachable from the IC subnet. This is unlikely under normal circumstances but is a realistic failure mode: all three providers could block IC egress IPs, all three could be taken down by a coordinated legal action, or a subnet-level network partition could make all external HTTPS outcalls fail. Notably, this scenario does **not** require any governance action or privileged access — it is triggered purely by external provider availability, which is outside the protocol's control. The historical record shows that single-provider failures have already caused ckETH minter outages multiple times (Cloudflare, Ankr, Pocket Network), making multi-provider simultaneous failure a credible escalation.

### Recommendation
Add a `providers_tried` counter to `FetchTxStatusError` (or store a `BTreeSet<ProviderId>` of exhausted providers). In `try_fetch_tx`, after computing the next provider, check whether it has already been tried for this `txid`. If all providers in the ring have been exhausted, transition the status to a terminal `CheckTransactionIrrecoverableError` (e.g., a new variant `AllProvidersExhausted`) instead of continuing to cycle. This gives the ckBTC minter a definitive signal to quarantine the UTXO and notify the user, rather than looping forever on a retriable status.

### Proof of Concept

1. User sends 1 BTC to the ckBTC minter's deposit address.
2. All three Bitcoin API providers (Btcscan, Blockstream, MempoolSpace) become permanently unreachable from the IC subnet.
3. User calls `update_balance` on the ckBTC minter.
4. Minter calls `check_transaction(txid)` on the Bitcoin Checker canister with `CHECK_TRANSACTION_CYCLES_REQUIRED` cycles.
5. Bitcoin Checker's `try_fetch_tx` finds no cached status → selects provider A → `http_get_tx` fails → stores `FetchTxStatus::Error { provider: A }` → returns `FetchResult::Error`.
6. `check_transaction_inputs` calls `err.into_response(txid)` → returns `CheckTransactionRetriable::TransientInternalError`.
7. Minter receives `Unknown(Retriable(...))` → returns `UpdateBalanceError::TemporarilyUnavailable` to user.
8. User retries `update_balance`. Bitcoin Checker now has `Error { provider: A }` → tries `A.next()` = B → fails → stores `Error { provider: B }` → same retriable response.
9. User retries again. Bitcoin Checker tries C → fails → stores `Error { provider: C }`.
10. User retries again. Bitcoin Checker tries `C.next()` = A → fails → stores `Error { provider: A }`.
11. Steps 8–10 repeat indefinitely. The user's 1 BTC remains locked in the minter's Bitcoin address with no on-chain recovery path. [9](#0-8) [10](#0-9) [11](#0-10)

### Citations

**File:** rs/bitcoin/checker/src/fetch.rs (L22-33)
```rust
    pub(crate) fn into_response(self, txid: Txid) -> CheckTransactionResponse {
        let txid = txid.as_ref().to_vec();
        match self {
            HttpGetTxError::Rejected { message, .. } => {
                CheckTransactionRetriable::TransientInternalError(message).into()
            }
            HttpGetTxError::ResponseTooLarge => {
                (CheckTransactionIrrecoverableError::ResponseTooLarge { txid }).into()
            }
            _ => CheckTransactionRetriable::TransientInternalError(self.to_string()).into(),
        }
    }
```

**File:** rs/bitcoin/checker/src/fetch.rs (L69-104)
```rust
    fn try_fetch_tx(
        &self,
        txid: Txid,
    ) -> TryFetchResult<impl futures::Future<Output = Result<FetchResult, Infallible>>> {
        let (provider, max_response_bytes) = match state::get_fetch_status(txid) {
            None => (
                providers::next_provider(self.config().btc_network()),
                INITIAL_MAX_RESPONSE_BYTES,
            ),
            Some(FetchTxStatus::PendingRetry {
                max_response_bytes, ..
            }) => (
                providers::next_provider(self.config().btc_network()),
                max_response_bytes,
            ),
            Some(FetchTxStatus::PendingOutcall) => return TryFetchResult::Pending,
            Some(FetchTxStatus::Error(err)) => (
                // An FetchTxStatus error can be retried with another provider
                err.provider.next(),
                // The next provider can use the same max_response_bytes
                err.max_response_bytes,
            ),
            Some(FetchTxStatus::Fetched(fetched)) => return TryFetchResult::Fetched(fetched),
        };
        let guard = match self.new_fetch_guard(txid) {
            Ok(guard) => guard,
            Err(_) => return TryFetchResult::HighLoad,
        };
        let num_subnet_nodes = self.config().num_subnet_nodes;
        let cycle_cost = get_tx_cycle_cost(max_response_bytes, num_subnet_nodes);
        if self.cycles_accept(cycle_cost) < cycle_cost {
            TryFetchResult::NotEnoughCycles
        } else {
            TryFetchResult::ToFetch(self.fetch_tx(guard, provider, txid, max_response_bytes))
        }
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

**File:** rs/bitcoin/checker/src/providers.rs (L68-83)
```rust
    // Return the next provider by cycling through all available providers.
    pub fn next(&self) -> Self {
        let btc_network = &self.btc_network;
        let provider_id = match (btc_network, self.provider_id) {
            (BtcNetwork::Mainnet, ProviderId::Btcscan) => ProviderId::Blockstream,
            (BtcNetwork::Mainnet, ProviderId::Blockstream) => ProviderId::MempoolSpace,
            (BtcNetwork::Mainnet, ProviderId::MempoolSpace) => ProviderId::Btcscan,
            (BtcNetwork::Testnet, ProviderId::Blockstream) => ProviderId::MempoolSpace,
            (BtcNetwork::Testnet, _) => ProviderId::Blockstream,
            (BtcNetwork::Regtest { .. }, _) => return self.clone(),
        };
        Self {
            btc_network: btc_network.clone(),
            provider_id,
        }
    }
```

**File:** rs/bitcoin/checker/src/state.rs (L54-59)
```rust
#[derive(Debug, Clone)]
pub struct FetchTxStatusError {
    pub provider: Provider,
    pub max_response_bytes: u32,
    pub error: HttpGetTxError,
}
```

**File:** rs/bitcoin/checker/src/state.rs (L116-133)
```rust
// Max number of concurrent http outcalls.
const MAX_CONCURRENT: u32 = 50;

// Max number of entries in the cache is set to 10_000. Since the average transaction size
// is about 400 bytes, the estimated memory usage of the cache is in the order of 10s of MBs.
const MAX_FETCH_TX_ENTRIES: usize = 10_000;

// The internal state includes:
// 1. Outcall capacity, a semaphore limiting max concurrent outcalls.
// 2. fetch transaction status, indexed by transaction id.
//
// TODO(XC-191): persist canister state
thread_local! {
    pub(crate) static OUTCALL_CAPACITY: RefCell<u32> = const { RefCell::new(MAX_CONCURRENT) };
    pub(crate) static FETCH_TX_CACHE: RefCell<FetchTxCache<FetchTxStatus>> = RefCell::new(
        FetchTxCache::new(MAX_FETCH_TX_ENTRIES)
    );
}
```

**File:** rs/bitcoin/checker/src/main.rs (L547-558)
```rust
                TryFetchResult::ToFetch(do_fetch) => {
                    match do_fetch.await {
                        Ok(FetchResult::Fetched(fetched)) => {
                            env.check_fetched(txid, &fetched).await
                        }
                        Ok(FetchResult::Error(err)) => err.into_response(txid),
                        Ok(FetchResult::RetryWithBiggerBuffer) => {
                            CheckTransactionRetriable::Pending.into()
                        }
                        Err(_) => unreachable!(), // should never happen
                    }
                }
```

**File:** rs/bitcoin/ckbtc/minter/src/updates/update_balance.rs (L400-455)
```rust
    for i in 0..MAX_CHECK_TRANSACTION_RETRY {
        match runtime
            .check_transaction(
                btc_checker_principal,
                utxo,
                CHECK_TRANSACTION_CYCLES_REQUIRED,
            )
            .await
            .map_err(|call_err| {
                UpdateBalanceError::TemporarilyUnavailable(format!(
                    "Failed to call Bitcoin checker canister: {call_err}"
                ))
            })? {
            CheckTransactionResponse::Failed(addresses) => {
                log!(
                    Priority::Info,
                    "Discovered a tainted UTXO {} (due to input addresses {}) for update_balance({:?}) call",
                    DisplayOutpoint(&utxo.outpoint),
                    addresses.join(","),
                    args,
                );
                return Ok(UtxoCheckStatus::Tainted);
            }
            CheckTransactionResponse::Passed => return Ok(UtxoCheckStatus::Clean),
            CheckTransactionResponse::Unknown(CheckTransactionStatus::NotEnoughCycles) => {
                log!(
                    Priority::Debug,
                    "The Bitcoin checker canister requires more cycles, Remaining tries: {}",
                    MAX_CHECK_TRANSACTION_RETRY - i - 1
                );
                continue;
            }
            CheckTransactionResponse::Unknown(CheckTransactionStatus::Retriable(status)) => {
                log!(
                    Priority::Debug,
                    "The Bitcoin checker canister is temporarily unavailable: {:?}",
                    status
                );
                return Err(UpdateBalanceError::TemporarilyUnavailable(format!(
                    "The Bitcoin checker canister is temporarily unavailable: {status:?}"
                )));
            }
            CheckTransactionResponse::Unknown(CheckTransactionStatus::Error(error)) => {
                log!(Priority::Debug, "Bitcoin checker error: {:?}", error);
                return Err(UpdateBalanceError::GenericError {
                    error_code: ErrorCode::KytError as u64,
                    error_message: format!("Bitcoin checker error: {error:?}"),
                });
            }
        }
    }
    Err(UpdateBalanceError::GenericError {
        error_code: ErrorCode::KytError as u64,
        error_message: "The Bitcoin checker canister required too many calls to check_transaction"
            .to_string(),
    })
```

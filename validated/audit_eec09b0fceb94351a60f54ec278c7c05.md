### Title
Unprivileged Semaphore Exhaustion via Concurrent `check_transaction` Calls Causes Sustained HighLoad DoS Against ckBTC Minter — (`rs/bitcoin/checker/src/state.rs`)

---

### Summary

An unprivileged attacker can exhaust the global `OUTCALL_CAPACITY` semaphore (capacity = 50) by submitting 50 concurrent `check_transaction` calls with distinct TXIDs and delaying the HTTP outcall responses. While the semaphore is fully consumed, every subsequent call — including those from the ckBTC minter — returns `CheckTransactionRetriable::HighLoad`, halting ckBTC deposit processing for the duration of the attack. The attack can be sustained continuously at a fixed cycle cost.

---

### Finding Description

**Root cause — no per-caller rate limiting and a fully-exhaustible global semaphore.**

`OUTCALL_CAPACITY` is a single global `u32` counter initialized to `MAX_CONCURRENT = 50`: [1](#0-0) 

`FetchGuard::new` decrements it unconditionally for any caller that passes the cycles check: [2](#0-1) 

`FetchGuard::Drop` restores the slot only when the HTTP outcall completes (or the guard is dropped): [3](#0-2) 

In `try_fetch_tx`, when `new_fetch_guard` returns `NoCapacity`, the function immediately returns `TryFetchResult::HighLoad` — no retry, no queue: [4](#0-3) 

`check_transaction_inputs` maps this directly to the wire response: [5](#0-4) 

`check_transaction_with` performs **no caller identity check and no per-caller rate limiting** — only a cycles balance check: [6](#0-5) 

The required cycles per call is 40 B: [7](#0-6) 

**Attack path:**

1. Attacker submits 50 `check_transaction` calls, each with a distinct valid Bitcoin TXID and ≥ 40 B cycles attached.
2. Each call enters `check_transaction_inputs` → `try_fetch_tx` → `FetchGuard::new`, decrementing `OUTCALL_CAPACITY` from 50 → 0 and setting each TXID's status to `PendingOutcall`.
3. Each call then `await`s `http_get_tx`. On the IC, awaiting an async operation yields execution, so all 50 calls are simultaneously suspended mid-execution with their `FetchGuard`s live.
4. The attacker controls (or selects) HTTP providers that do not respond promptly, keeping all 50 guards alive.
5. `OUTCALL_CAPACITY` = 0. Any new call to `try_fetch_tx` for a TXID not already in `PendingOutcall` hits `Err(FetchGuardError::NoCapacity)` → `TryFetchResult::HighLoad`.
6. The ckBTC minter's `check_transaction` call returns `CheckTransactionRetriable::HighLoad`, causing `update_balance` to treat the check as temporarily unavailable and halt deposit processing.

The TXID deduplication in `try_fetch_tx` (line 84 of `fetch.rs`) returns `Pending` — not `HighLoad` — for a TXID already in `PendingOutcall`, so the attacker must use 50 **distinct** TXIDs. This is trivially achievable given Bitcoin's transaction history. [8](#0-7) 

---

### Impact Explanation

- **ckBTC deposit processing halted**: The ckBTC minter's `update_balance` flow calls `check_transaction` on the checker canister. While `OUTCALL_CAPACITY` = 0, every such call returns `HighLoad`, and the minter cannot complete deposit checks for any user.
- **Sustained attack**: HTTP outcalls on the IC have a timeout (typically ~30 s). The attacker can submit a fresh batch of 50 calls before the previous batch times out, sustaining the DoS indefinitely at a fixed ongoing cost.
- **Cost to attacker**: 50 × 40 B = 2 T cycles per ~30 s window. This is non-trivial but not prohibitive for a motivated attacker.
- **No impact on other canister functions** (e.g., `check_address`) — only `check_transaction` is gated by the semaphore.

---

### Likelihood Explanation

- Requires no privileged access — `check_transaction` is a public update method.
- Requires only 50 concurrent calls with valid TXIDs and cycles, achievable from a single principal.
- No per-caller rate limiting, no reserved capacity for the ckBTC minter, no caller allowlist.
- The IC's message queue per canister is large enough to accommodate 50 concurrent in-flight update calls.
- Likelihood: **Medium-High** (low technical barrier, moderate cycle cost, clear financial motivation to disrupt ckBTC deposits).

---

### Recommendation

1. **Reserve capacity for the ckBTC minter**: Check `ic_cdk::api::caller()` and reserve a portion of `OUTCALL_CAPACITY` exclusively for the known ckBTC minter principal, so it can never be starved by unprivileged callers.
2. **Per-caller rate limiting**: Track in-flight outcall count per caller principal and reject calls that exceed a per-caller cap (e.g., 5 concurrent calls per principal).
3. **Reduce `MAX_CONCURRENT` per-caller share**: Even without identity checks, a simple per-principal counter stored in a `BTreeMap<Principal, u32>` would prevent a single caller from consuming all 50 slots.

---

### Proof of Concept

```rust
// PocketIC pseudocode
let checker = deploy_checker_canister(&pic);
let attacker = random_principal();

// Step 1: exhaust all 50 slots with distinct TXIDs
let txids: Vec<[u8;32]> = (0..50).map(|i| make_valid_txid(i)).collect();
let handles: Vec<_> = txids.iter().map(|txid| {
    pic.submit_call_with_cycles(
        checker,
        attacker,
        "check_transaction",
        encode_args(CheckTransactionArgs { txid: txid.to_vec() }),
        40_000_000_000, // CHECK_TRANSACTION_CYCLES_REQUIRED
    )
}).collect();

// Step 2: do NOT advance HTTP outcall mock — keep all 50 in PendingOutcall

// Step 3: ckBTC minter calls check_transaction
let minter = ckbtc_minter_principal();
let result = pic.update_call(
    checker, minter, "check_transaction",
    encode_args(CheckTransactionArgs { txid: another_valid_txid() }),
    40_000_000_000,
);

// Step 4: assert HighLoad
assert_eq!(
    result,
    CheckTransactionResponse::Unknown(
        CheckTransactionStatus::Retriable(CheckTransactionRetriable::HighLoad)
    )
);
```

### Citations

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

**File:** rs/bitcoin/checker/src/state.rs (L244-258)
```rust
impl FetchGuard {
    pub fn new(txid: Txid) -> Result<Self, FetchGuardError> {
        let guard = OUTCALL_CAPACITY.with(|capacity| {
            let mut capacity = capacity.borrow_mut();
            if *capacity > 0 {
                *capacity -= 1;
                Ok(FetchGuard(txid))
            } else {
                Err(FetchGuardError::NoCapacity)
            }
        })?;
        set_fetch_status(txid, FetchTxStatus::PendingOutcall);
        Ok(guard)
    }
}
```

**File:** rs/bitcoin/checker/src/state.rs (L260-272)
```rust
impl Drop for FetchGuard {
    fn drop(&mut self) {
        OUTCALL_CAPACITY.with(|capacity| {
            let mut capacity = capacity.borrow_mut();
            *capacity += 1;
        });
        let txid = self.0;
        if let Some(FetchTxStatus::PendingOutcall) = get_fetch_status(txid) {
            // Only clear the status when it is still `PendingOutcall`
            clear_fetch_status(txid);
        }
    }
}
```

**File:** rs/bitcoin/checker/src/fetch.rs (L73-96)
```rust
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

**File:** rs/bitcoin/checker/src/main.rs (L543-545)
```rust
                TryFetchResult::Pending => CheckTransactionRetriable::Pending.into(),
                TryFetchResult::HighLoad => CheckTransactionRetriable::HighLoad.into(),
                TryFetchResult::NotEnoughCycles => CheckTransactionStatus::NotEnoughCycles.into(),
```

**File:** rs/bitcoin/checker/lib/lib.rs (L7-10)
```rust
pub const CHECK_TRANSACTION_CYCLES_REQUIRED: u128 = 40_000_000_000;

/// One-time charge for every check_transaction call.
pub const CHECK_TRANSACTION_CYCLES_SERVICE_FEE: u128 = 100_000_000;
```

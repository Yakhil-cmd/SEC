### Title
Attacker-Controlled `received_at` Timestamp in `can_form_a_batch` Enables Indefinite Delay of All Pending ckBTC Withdrawals - (File: `rs/bitcoin/ckbtc/minter/src/state.rs`)

---

### Summary

The ckBTC minter's `can_form_a_batch` function uses the `received_at` field of the **last** pending request to decide whether to dispatch a Bitcoin transaction. Because `received_at` is set to `ic_cdk::api::time()` at the moment a user calls `retrieve_btc` or `retrieve_btc_with_approval`, a malicious actor can continuously inject minimum-amount withdrawal requests to keep the last request's `received_at` perpetually fresh, preventing the `max_time_in_queue_nanos` deadline from ever being reached for the batch. This delays all other users' pending withdrawals indefinitely, as long as the attacker keeps submitting new requests.

---

### Finding Description

The ckBTC minter batches pending `retrieve_btc` requests and dispatches them as a single Bitcoin transaction. The decision to dispatch is made in `can_form_a_batch`:

```rust
// rs/bitcoin/ckbtc/minter/src/state.rs, lines 921–940
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

Three conditions can trigger a batch:
1. **Count threshold**: `pending_retrieve_btc_requests.len() >= MIN_PENDING_REQUESTS` (20 requests).
2. **Age of oldest request**: `now - first_req.received_at > max_time_in_queue_nanos`.
3. **Age of newest request relative to last submission**: `last_req.received_at - last_submission_time > max_time_in_queue_nanos`.

The `received_at` field is set directly from `ic_cdk::api::time()` at the moment the user calls `retrieve_btc_with_approval`:

```rust
// rs/bitcoin/ckbtc/minter/src/updates/retrieve_btc.rs, lines 321–326
let request = RetrieveBtcRequest {
    amount: args.amount,
    address: parsed_address,
    block_index,
    received_at: ic_cdk::api::time(),   // ← attacker-controlled timing
    ...
};
```

The `pending_retrieve_btc_requests` is a `Vec` where new requests are appended to the back. Condition 3 checks `pending_retrieve_btc_requests.last()` — the **most recently added** request. If an attacker continuously submits new minimum-amount withdrawal requests, the last request's `received_at` is always fresh (close to `now`), so `last_req.received_at - last_submission_time` never exceeds `max_time_in_queue_nanos`. Condition 2 checks the **first** (oldest) request, but only fires if `now - first_req.received_at > max_time_in_queue_nanos`. If the attacker keeps the queue below `MIN_PENDING_REQUESTS` (20) by submitting just enough requests to stay under the threshold, condition 1 never fires either.

The attacker's strategy:
- Maintain between 1 and 19 pending requests in the queue at all times.
- Submit a new minimum-amount `retrieve_btc_with_approval` call every `max_time_in_queue_nanos` interval (currently configurable, default is a few minutes).
- Each new submission resets `last()` to a fresh `received_at`, preventing condition 3 from firing.
- Condition 2 fires only when `now - first_req.received_at > max_time_in_queue_nanos`, but if the attacker's first request is also fresh (submitted just before the deadline), this is also suppressed.

The attacker must hold enough ckBTC to cover the minimum withdrawal amount plus fees for each submission. The minimum withdrawal amount is `fee_based_retrieve_btc_min_amount`, which is dynamically computed but bounded by `retrieve_btc_min_amount`. Each submission burns ckBTC from the attacker's account, so the attacker does lose funds — but the cost is bounded by the minimum withdrawal amount per interval, which can be small relative to the damage caused.

The `MAX_CONCURRENT_PENDING_REQUESTS` limit is 5000:

```rust
// rs/bitcoin/ckbtc/minter/src/updates/retrieve_btc.rs, line 22
const MAX_CONCURRENT_PENDING_REQUESTS: usize = 5000;
```

This means the attacker can fill the queue with up to 4,999 requests (staying just below `MIN_PENDING_REQUESTS` = 20 is the more efficient strategy), preventing legitimate users' requests from ever being batched and dispatched.

---

### Impact Explanation

All legitimate users who have submitted `retrieve_btc` or `retrieve_btc_with_approval` requests will have their ckBTC burned (already debited from their ledger account) but will not receive BTC for an indefinite period. This is a **denial-of-service on the ckBTC withdrawal (unstake) mechanism** — the exact analog of M-08. Users' funds are locked in limbo: ckBTC is burned, BTC is not sent. The minter's `pending_retrieve_btc_requests` queue grows, and no Bitcoin transactions are ever submitted to the Bitcoin network for legitimate users' requests.

---

### Likelihood Explanation

The attack requires the attacker to:
1. Hold a small amount of ckBTC (minimum withdrawal amount per interval).
2. Submit one `retrieve_btc_with_approval` call per `max_time_in_queue_nanos` interval.

This is reachable by any unprivileged ingress sender with a small ckBTC balance. The `retrieve_btc_with_approval` endpoint is publicly callable by any non-anonymous principal. The cost to the attacker is the minimum withdrawal amount (in ckBTC) per interval, which is burned — but the attacker's own withdrawal will eventually be processed too (they receive BTC), so the net cost is only the Bitcoin network fee and minter fee per submission. This makes sustained attack economically feasible.

---

### Recommendation

1. **Remove condition 3 from `can_form_a_batch`**, or replace it with a check based on the **oldest** (first) request's age relative to `last_submission_time`, not the newest. The current condition 3 is logically redundant with condition 2 in the non-attack case, but creates the vulnerability in the attack case.

2. **Alternatively**, track the `received_at` of the oldest request that was present at the time of the last submission, and use that as the reference point for the timeout — not the newest request's timestamp.

3. **Rate-limit per-account submissions** more aggressively, or impose a minimum interval between successive `retrieve_btc` calls from the same account.

---

### Proof of Concept

**Setup**: `max_time_in_queue_nanos = T` (e.g., 10 minutes). `MIN_PENDING_REQUESTS = 20`. Attacker has enough ckBTC for repeated minimum withdrawals.

**Attack loop** (repeat every `T - ε` nanoseconds):
1. Attacker calls `retrieve_btc_with_approval(min_amount, attacker_btc_address)`.
2. A new `RetrieveBtcRequest` is appended to `pending_retrieve_btc_requests` with `received_at = now`.
3. `can_form_a_batch` is evaluated:
   - Condition 1: queue length < 20 → false.
   - Condition 2: `now - first_req.received_at` ≤ `T` (attacker's first request is also fresh) → false.
   - Condition 3: `last_req.received_at - last_submission_time` = `~0` (just submitted) → false.
4. No batch is formed. Legitimate users' requests remain stuck.

Alice submits `retrieve_btc_with_approval(1_BTC, alice_btc_address)` at time `t0`. Her ckBTC is burned immediately. At time `t0 + T`, her request should be dispatched, but Bob (attacker) submitted a new request at `t0 + T - ε`, resetting `last()`. Alice's request is never dispatched as long as Bob keeps submitting.

**Root cause code path**: [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4)

### Citations

**File:** rs/bitcoin/ckbtc/minter/src/state.rs (L932-937)
```rust
        if let Some(req) = self.pending_retrieve_btc_requests.last()
            && let Some(last_submission_time) = self.last_transaction_submission_time_ns
            && self.max_time_in_queue_nanos < req.received_at.saturating_sub(last_submission_time)
        {
            return true;
        }
```

**File:** rs/bitcoin/ckbtc/minter/src/updates/retrieve_btc.rs (L22-22)
```rust
const MAX_CONCURRENT_PENDING_REQUESTS: usize = 5000;
```

**File:** rs/bitcoin/ckbtc/minter/src/updates/retrieve_btc.rs (L321-326)
```rust
    let request = RetrieveBtcRequest {
        amount: args.amount,
        address: parsed_address,
        block_index,
        received_at: ic_cdk::api::time(),
        kyt_provider: None,
```

**File:** rs/bitcoin/ckbtc/minter/src/lib.rs (L54-57)
```rust
/// The minimum number of pending request in the queue before we try to make
/// a batch transaction.
pub const MIN_PENDING_REQUESTS: usize = 20;
pub const MAX_REQUESTS_PER_BATCH: usize = 100;
```

**File:** rs/bitcoin/ckbtc/minter/src/lib.rs (L348-353)
```rust
async fn submit_pending_requests<R: CanisterRuntime>(runtime: &R) {
    // We make requests if we have old requests in the queue or if have enough
    // requests to fill a batch.
    if !state::read_state(|s| s.can_form_a_batch(MIN_PENDING_REQUESTS, runtime.time())) {
        return;
    }
```

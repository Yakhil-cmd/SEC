### Title
Single Principal Can Exhaust Global `MAX_CONCURRENT_PENDING_REQUESTS` Queue, DoSing All Withdrawals — (`rs/bitcoin/ckbtc/minter/src/updates/retrieve_btc.rs`)

---

### Summary

The `retrieve_doge_with_approval` endpoint enforces a single global cap of 5 000 pending withdrawal requests with no per-principal sub-limit. A single funded attacker can sequentially fill the entire queue, causing every subsequent legitimate caller to receive `TemporarilyUnavailable` until the minter drains the queue.

---

### Finding Description

`retrieve_doge_with_approval` in the ckDOGE minter delegates directly to `retrieve_btc_with_approval` in the shared ckBTC minter library. [1](#0-0) 

Inside `retrieve_btc_with_approval`, the only global throttle is:

```rust
const MAX_CONCURRENT_PENDING_REQUESTS: usize = 5000;
// ...
if read_state(|s| s.count_incomplete_retrieve_btc_requests() >= MAX_CONCURRENT_PENDING_REQUESTS) {
    return Err(RetrieveBtcWithApprovalError::TemporarilyUnavailable(
        "too many pending retrieve_btc requests".to_string(),
    ));
}
``` [2](#0-1) [3](#0-2) 

The `retrieve_btc_guard` that precedes this check is keyed on `Account { owner, subaccount }` and is held only for the duration of the async call execution — it is released as soon as the function returns. [4](#0-3) 

Once a call returns `Ok`, the guard is dropped and the same account can immediately submit another request. There is **no per-principal limit** on how many requests can reside in the pending queue simultaneously. The global counter `count_incomplete_retrieve_btc_requests` aggregates requests from all principals without any per-principal accounting.

**Attack sequence:**

1. Attacker acquires `5000 × retrieve_doge_min_amount` ckDOGE (by depositing DOGE).
2. Attacker calls `icrc2_approve` + `retrieve_doge_with_approval` 5 000 times sequentially (or in parallel using different subaccounts). Each call burns ckDOGE and enqueues a `RetrieveBtcRequest`.
3. The global counter reaches 5 000.
4. Any subsequent call from any principal — including legitimate users — returns `TemporarilyUnavailable("too many pending retrieve_btc requests")`.
5. The minter processes requests in the background, but on a slow Dogecoin network this can take a very long time, sustaining the DoS.

The attacker's ckDOGE is burned but they receive DOGE back at their chosen address once the minter processes the queue. The net cost to the attacker is only the ledger transfer fees (5 000 × `LEDGER_TRANSFER_FEE`), which is negligible relative to the impact.

---

### Impact Explanation

All ckDOGE withdrawal requests from all users are blocked for the duration of queue drainage. This is a complete withdrawal DoS on the chain-fusion asset bridge. Users cannot convert ckDOGE back to DOGE, which constitutes a cross-chain asset availability compromise.

---

### Likelihood Explanation

The attack requires only a sufficient ckDOGE balance (obtainable by depositing DOGE) and the ability to call a public update endpoint. No privileged access, governance majority, or cryptographic material is needed. The economic cost (ledger fees only) is low relative to the disruption caused. The Dogecoin network's characteristically slow confirmation times extend the DoS window.

---

### Recommendation

Introduce a **per-principal pending request limit** (e.g., 10–50 requests per principal) enforced inside `retrieve_btc_with_approval` before the global check. Track per-principal pending counts in minter state alongside the global counter. This prevents any single actor from monopolising the global queue while preserving the global cap as a secondary safety valve.

---

### Proof of Concept

State-machine test sketch:

```rust
// Fill the queue from a single attacker principal
for _ in 0..MAX_CONCURRENT_PENDING_REQUESTS {
    icrc2_approve(attacker, min_amount, minter_id);
    assert!(minter.retrieve_doge_with_approval(attacker, min_amount, addr).is_ok());
}

// Legitimate user is now blocked
let result = minter.retrieve_doge_with_approval(victim, min_amount, addr);
assert_eq!(
    result,
    Err(RetrieveDogeWithApprovalError::TemporarilyUnavailable(
        "too many pending retrieve_btc requests".to_string()
    ))
);
```

The global counter check at line 274 of `retrieve_btc.rs` will fire for the victim while the attacker's 5 000 requests remain unprocessed. [3](#0-2)

### Citations

**File:** rs/dogecoin/ckdoge/minter/src/main.rs (L131-143)
```rust
async fn retrieve_doge_with_approval(
    args: RetrieveDogeWithApprovalArgs,
) -> Result<RetrieveDogeOk, RetrieveDogeWithApprovalError> {
    check_anonymous_caller();
    let result = ic_ckbtc_minter::updates::retrieve_btc::retrieve_btc_with_approval(
        args.into(),
        &DOGECOIN_CANISTER_RUNTIME,
    )
    .await
    .map(RetrieveDogeOk::from)
    .map_err(RetrieveDogeWithApprovalError::from);
    check_postcondition(result)
}
```

**File:** rs/bitcoin/ckbtc/minter/src/updates/retrieve_btc.rs (L22-22)
```rust
const MAX_CONCURRENT_PENDING_REQUESTS: usize = 5000;
```

**File:** rs/bitcoin/ckbtc/minter/src/updates/retrieve_btc.rs (L259-263)
```rust
    let caller_account = Account {
        owner: caller,
        subaccount: args.from_subaccount,
    };
    let _guard = retrieve_btc_guard(caller_account)?;
```

**File:** rs/bitcoin/ckbtc/minter/src/updates/retrieve_btc.rs (L274-279)
```rust
    if read_state(|s| s.count_incomplete_retrieve_btc_requests() >= MAX_CONCURRENT_PENDING_REQUESTS)
    {
        return Err(RetrieveBtcWithApprovalError::TemporarilyUnavailable(
            "too many pending retrieve_btc requests".to_string(),
        ));
    }
```

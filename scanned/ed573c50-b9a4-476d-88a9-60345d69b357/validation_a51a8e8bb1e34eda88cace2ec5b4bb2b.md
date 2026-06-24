### Title
Global Pending-Queue Exhaustion by Single Unprivileged Principal — (`rs/bitcoin/ckbtc/minter/src/updates/retrieve_btc.rs`)

---

### Summary

`retrieve_doge_with_approval` (and the underlying `retrieve_btc_with_approval`) enforces only a **concurrent in-flight** guard (`MAX_CONCURRENT = 100`) that is released as soon as each call completes. There is no per-principal cap on how many **accepted/pending** requests can accumulate in the queue. A single attacker with sufficient ckDOGE balance can sequentially fill the global `MAX_CONCURRENT_PENDING_REQUESTS = 5000` limit, causing every subsequent withdrawal from any user to return `TemporarilyUnavailable`.

---

### Finding Description

`retrieve_doge_with_approval` in the ckDOGE minter delegates directly to `retrieve_btc_with_approval`: [1](#0-0) 

That function applies two guards:

**Guard 1 — concurrent in-flight guard** (`retrieve_btc_guard`): [2](#0-1) 

This guard is backed by `Guard<RetrieveBtcUpdates>`, which tracks accounts in `state.retrieve_btc_accounts` and caps total concurrent in-flight calls at `MAX_CONCURRENT = 100`: [3](#0-2) 

Critically, the guard is **dropped** (via `Drop`) when the async function returns — whether successfully or not: [4](#0-3) 

**Guard 2 — global pending queue cap**: [5](#0-4) 

`MAX_CONCURRENT_PENDING_REQUESTS = 5000` is a **global** ceiling with **no per-principal sub-limit**: [6](#0-5) 

**The gap**: Guard 1 only prevents two simultaneous in-flight calls for the same account. Once a call succeeds and the request is accepted into `pending_retrieve_btc_requests`, the guard is released and the attacker can immediately submit another request. Guard 2 is a global counter with no per-principal accounting. There is nothing preventing a single principal (using one or more subaccounts) from accumulating all 5000 slots.

---

### Impact Explanation

Once the attacker holds all 5000 pending slots, every call to `retrieve_doge_with_approval` from any user returns:

```
TemporarilyUnavailable("too many pending retrieve_btc requests")
```

The attacker can sustain the DoS by refilling slots as the minter processes and drains their requests. The net cost per cycle is `5000 × withdrawal_fee` (the ckDOGE principal is returned as DOGE). This is a **withdrawal DoS** affecting all users of the ckDOGE chain-fusion bridge.

---

### Likelihood Explanation

- Requires no privileged access — only a funded ckDOGE account and ICRC-2 approvals.
- The attack is fully sequential: submit → wait for acceptance → repeat. No concurrency tricks needed.
- The Dogecoin network's slow confirmation time (up to 60 blocks, ~1 hour) means the pending queue drains slowly, maximizing DoS duration per attack cycle.
- Economic cost is bounded and recoverable (attacker receives DOGE back minus fees), making sustained attacks feasible for a motivated adversary.

---

### Recommendation

1. **Add a per-principal pending-request cap**: before accepting a new request into `pending_retrieve_btc_requests`, count how many existing pending/in-progress requests belong to `caller_account.owner` and reject if above a threshold (e.g., 10–20).
2. **Alternatively, track per-principal accepted-request counts** in `CkBtcMinterState` and enforce the limit inside `accept_retrieve_btc_request`.
3. The ckETH minter already has a `MAX_PENDING` guard that checks total pending requests before accepting a new one — the ckBTC/ckDOGE minter should adopt an analogous **per-principal** variant. [7](#0-6) 

---

### Proof of Concept

```
// State-machine test sketch
for subaccount in 0..50 {
    for _ in 0..100 {  // 50 subaccounts × 100 sequential calls = 5000
        icrc2_approve(minter, min_amount, subaccount);
        let result = retrieve_doge_with_approval(min_amount, doge_addr, subaccount);
        assert!(result.is_ok());
    }
}
// Now the queue has 5000 pending requests
let victim_result = retrieve_doge_with_approval_as(victim_principal, min_amount, doge_addr);
assert!(matches!(victim_result, Err(TemporarilyUnavailable(_))));
```

The invariant `"a single principal cannot fill the entire pending queue"` is violated because `retrieve_btc_with_approval` checks only a global counter with no per-principal accounting. [8](#0-7)

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

**File:** rs/bitcoin/ckbtc/minter/src/updates/retrieve_btc.rs (L244-279)
```rust
pub async fn retrieve_btc_with_approval<R: CanisterRuntime>(
    args: RetrieveBtcWithApprovalArgs,
    runtime: &R,
) -> Result<RetrieveBtcOk, RetrieveBtcWithApprovalError> {
    let caller = ic_cdk::api::msg_caller();

    state::read_state(|s| s.mode.is_withdrawal_available_for(&caller))
        .map_err(RetrieveBtcWithApprovalError::TemporarilyUnavailable)?;

    let _ecdsa_public_key = init_ecdsa_public_key().await;
    let main_address_str = state::read_state(|s| runtime.derive_minter_address_str(s));

    if args.address == main_address_str {
        ic_cdk::trap("illegal retrieve_btc target");
    }
    let caller_account = Account {
        owner: caller,
        subaccount: args.from_subaccount,
    };
    let _guard = retrieve_btc_guard(caller_account)?;
    let (min_retrieve_amount, btc_network) =
        read_state(|s| (s.fee_based_retrieve_btc_min_amount, s.btc_network));
    if args.amount < min_retrieve_amount {
        return Err(RetrieveBtcWithApprovalError::AmountTooLow(
            min_retrieve_amount,
        ));
    }
    let parsed_address = runtime
        .parse_address(&args.address, btc_network)
        .map_err(RetrieveBtcWithApprovalError::MalformedAddress)?;
    if read_state(|s| s.count_incomplete_retrieve_btc_requests() >= MAX_CONCURRENT_PENDING_REQUESTS)
    {
        return Err(RetrieveBtcWithApprovalError::TemporarilyUnavailable(
            "too many pending retrieve_btc requests".to_string(),
        ));
    }
```

**File:** rs/bitcoin/ckbtc/minter/src/guard.rs (L41-60)
```rust
impl<PR: PendingRequests> Guard<PR> {
    /// Attempts to create a new guard for the current block. Fails if there is
    /// already a pending request for the specified [principal] or if there
    /// are at least [MAX_CONCURRENT] pending requests.
    pub fn new(account: Account) -> Result<Self, GuardError> {
        mutate_state(|s| {
            let accounts = PR::pending_requests(s);
            if accounts.contains(&account) {
                return Err(GuardError::AlreadyProcessing);
            }
            if accounts.len() >= MAX_CONCURRENT {
                return Err(GuardError::TooManyConcurrentRequests);
            }
            accounts.insert(account);
            Ok(Self {
                account,
                _marker: PhantomData,
            })
        })
    }
```

**File:** rs/bitcoin/ckbtc/minter/src/guard.rs (L63-67)
```rust
impl<PR: PendingRequests> Drop for Guard<PR> {
    fn drop(&mut self) {
        mutate_state(|s| PR::pending_requests(s).remove(&self.account));
    }
}
```

**File:** rs/ethereum/cketh/minter/src/guard/mod.rs (L9-17)
```rust
pub const MAX_CONCURRENT: usize = 100;
pub const MAX_PENDING: usize = 100;

#[derive(Eq, PartialEq, Debug)]
pub enum GuardError {
    AlreadyProcessing,
    TooManyConcurrentRequests,
    TooManyPendingRequests,
}
```

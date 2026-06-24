Let me look at the key details more carefully - specifically the `count_incomplete_retrieve_btc_requests` function and whether there's any per-principal limit on pending requests.

Based on my investigation of the code, here is my determination:

---

### Title
Single-Principal Pending Queue Exhaustion via `retrieve_doge_with_approval` — (`rs/bitcoin/ckbtc/minter/src/updates/retrieve_btc.rs`)

### Summary

There is no per-principal limit on the number of accepted (pending) withdrawal requests. A single unprivileged actor with sufficient ckDOGE capital can fill all `MAX_CONCURRENT_PENDING_REQUESTS = 5000` slots, causing every subsequent legitimate withdrawal to receive `TemporarilyUnavailable`.

### Finding Description

`retrieve_doge_with_approval` in `rs/dogecoin/ckdoge/minter/src/main.rs` delegates directly to `retrieve_btc_with_approval` in `rs/bitcoin/ckbtc/minter/src/updates/retrieve_btc.rs`. [1](#0-0) 

Two independent guards exist, and neither prevents a single principal from filling the queue:

**Guard 1 — `retrieve_btc_guard` (`MAX_CONCURRENT = 100`):** [2](#0-1) 

This guard tracks accounts **currently in-flight** (during async execution) in `retrieve_btc_accounts`, a `BTreeSet<Account>`. It is dropped via `Drop` the moment the function returns: [3](#0-2) 

Once a request is accepted into the pending queue and the function returns, the guard is released. The same account (or any other subaccount of the same principal) can immediately submit another request.

**Guard 2 — `MAX_CONCURRENT_PENDING_REQUESTS = 5000` (global, no per-principal sub-limit):** [4](#0-3) [5](#0-4) 

This is a **global** ceiling on all accepted-but-unconfirmed requests. There is no per-principal sub-limit anywhere in the codebase (confirmed: no `per_principal`, `per-principal`, or `principal.*limit` patterns exist in the minter source).

**Attack sequence:**
1. Attacker acquires 5000 × `min_retrieve_amount` of ckDOGE and sets icrc2 approvals.
2. Attacker calls `retrieve_doge_with_approval` sequentially (or with different subaccounts to exploit the 100-concurrent-in-flight window), each time with a valid address and amount ≥ `min_retrieve_amount`.
3. Each call burns ckDOGE, adds a `RetrieveBtcRequest` to the pending queue, and drops the guard.
4. After 5000 accepted requests, `count_incomplete_retrieve_btc_requests() >= MAX_CONCURRENT_PENDING_REQUESTS` is true.
5. Every subsequent call from any principal returns `TemporarilyUnavailable("too many pending retrieve_btc requests")`. [6](#0-5) 

### Impact Explanation

The withdrawal path for all ckDOGE users is completely blocked until the minter processes all 5000 attacker requests and confirms them on the Dogecoin network. Given Dogecoin's block times and the minter's batching logic, this could take hours to days. The attacker can re-fill the queue as slots are freed, sustaining the DoS indefinitely as long as they have capital.

### Likelihood Explanation

The attacker does not permanently lose funds — ckDOGE is burned and DOGE is returned to their address. The only cost is the opportunity cost of capital and Dogecoin network fees. A well-funded attacker (or one motivated by competitive or griefing reasons) can sustain this attack. The attack requires no privileged access, no key compromise, and no governance manipulation — only an ingress call available to any principal.

### Recommendation

Add a per-principal (or per-`Account`) cap on the number of accepted pending requests, e.g.:

```rust
const MAX_PENDING_REQUESTS_PER_PRINCIPAL: usize = 50;

if read_state(|s| s.count_pending_requests_for(caller) >= MAX_PENDING_REQUESTS_PER_PRINCIPAL) {
    return Err(TemporarilyUnavailable("too many pending requests for this principal".into()));
}
```

This should be checked **after** the guard is acquired and **before** the burn, so it is enforced atomically within the async execution window.

### Proof of Concept

```rust
// State-machine test sketch
let attacker = Principal::from_slice(&[1u8; 29]);
for i in 0..MAX_CONCURRENT_PENDING_REQUESTS {
    let subaccount = Some([i as u8; 32]);
    ledger.icrc2_approve(attacker, min_amount, minter_id, subaccount);
    let result = minter.retrieve_doge_with_approval(attacker, RetrieveDogeWithApprovalArgs {
        amount: min_amount,
        address: attacker_doge_address.clone(),
        from_subaccount: subaccount,
    });
    assert!(result.is_ok(), "slot {i} should be accepted");
}

// Legitimate user is now blocked
let victim = Principal::from_slice(&[2u8; 29]);
ledger.icrc2_approve(victim, min_amount, minter_id, None);
let result = minter.retrieve_doge_with_approval(victim, RetrieveDogeWithApprovalArgs {
    amount: min_amount,
    address: victim_doge_address.clone(),
    from_subaccount: None,
});
assert_eq!(result, Err(RetrieveDogeWithApprovalError::TemporarilyUnavailable(
    "too many pending retrieve_btc requests".to_string()
)));
``` [7](#0-6) [8](#0-7)

### Citations

**File:** rs/dogecoin/ckdoge/minter/src/main.rs (L130-143)
```rust
#[update]
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

**File:** rs/bitcoin/ckbtc/minter/src/guard.rs (L6-6)
```rust
const MAX_CONCURRENT: usize = 100;
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

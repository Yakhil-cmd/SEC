I have all the information needed. Let me trace the exact code path.

**Key facts from the code:**

1. `retrieve_doge_with_approval` delegates directly to `retrieve_btc_with_approval` in the ckBTC minter.

2. The guard is keyed on the full `Account{owner, subaccount}`: [1](#0-0) 

3. The guard uses a `BTreeSet<Account>` with a hard cap of `MAX_CONCURRENT = 100`: [2](#0-1) [3](#0-2) 

4. The guard is acquired **before** the async `check_address` and `burn_ckbtcs_icrc2` await points, meaning it is held across those yields: [4](#0-3) 

5. `MAX_CONCURRENT_PENDING_REQUESTS = 5000` is a **separate** check on the completed withdrawal queue — the guard's own cap is only 100: [5](#0-4) 

---

### Title
Single-Principal Multi-Subaccount Guard Exhaustion in `retrieve_doge_with_approval` / `retrieve_btc_with_approval` — (`rs/bitcoin/ckbtc/minter/src/guard.rs`, `rs/bitcoin/ckbtc/minter/src/updates/retrieve_btc.rs`)

### Summary

The `retrieve_btc_guard` is keyed on `Account{owner, subaccount}`, so a single principal holding N distinct subaccounts can acquire N independent guard slots simultaneously. Because the guard's `MAX_CONCURRENT` cap is 100 and is shared across **all** callers, one unprivileged principal can exhaust all 100 slots using 100 different subaccounts, causing every other user's `retrieve_doge_with_approval` call to return `TemporarilyUnavailable`.

### Finding Description

`retrieve_btc_with_approval` constructs the guard key as:

```rust
// rs/bitcoin/ckbtc/minter/src/updates/retrieve_btc.rs:259-263
let caller_account = Account {
    owner: caller,
    subaccount: args.from_subaccount,
};
let _guard = retrieve_btc_guard(caller_account)?;
```

The guard implementation inserts this `Account` into a `BTreeSet<Account>` and rejects new entries once `len() >= MAX_CONCURRENT` (100):

```rust
// rs/bitcoin/ckbtc/minter/src/guard.rs:45-59
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
        ...
    })
}
```

Because the IC canister yields at every `await` point and can interleave other messages, an attacker can have 100 concurrent in-flight calls — each with a distinct `from_subaccount` — all holding a guard slot simultaneously. The guard is held across both the `check_address` inter-canister call and the `burn_ckbtcs_icrc2` inter-canister call, which are the two await points after guard acquisition.

Critically, **no ckDOGE balance is required** to hold the guard. The `InsufficientAllowance` / `InsufficientFunds` errors only arise from `burn_ckbtcs_icrc2`, which executes *after* the guard is already held. The attacker only needs to send syntactically valid requests (valid address, `amount >= min_retrieve_amount`).

### Impact Explanation

Once all 100 guard slots are occupied, every other user calling `retrieve_doge_with_approval` receives:

```
TemporarilyUnavailable("too many concurrent requests")
```

This is a complete denial-of-service on the withdrawal path for all other users. The attacker can sustain the attack by continuously re-submitting calls as old ones complete, since the guard is released only when the async call finishes.

### Likelihood Explanation

- Requires no privileged access, no funds, no governance majority.
- Requires only 100 concurrent update calls with distinct `from_subaccount` bytes — trivially scripted.
- The IC's per-canister message queue is large enough to accommodate 100 concurrent in-flight update calls.
- The attack is cheap: no ckDOGE is burned because calls fail at `burn_ckbtcs_icrc2` with `InsufficientAllowance`, but the guard is already held at that point.
- Sustained DoS is achievable by looping the attack.

### Recommendation

Key the guard on `owner` (principal) only, not on the full `Account{owner, subaccount}`. This restores the original intent (one pending withdrawal per principal) and prevents subaccount-based slot multiplication:

```rust
let _guard = retrieve_btc_guard(Account {
    owner: caller,
    subaccount: None, // normalize to principal-only
})?;
```

Alternatively, enforce a per-principal cap on the number of concurrent guard slots regardless of subaccount, or increase `MAX_CONCURRENT` while adding per-principal rate limiting.

### Proof of Concept

State-machine test (no mainnet required):

1. Initialize minter with `MAX_CONCURRENT = 100`.
2. From a single attacker principal, send 100 concurrent `retrieve_doge_with_approval` calls, each with a distinct `from_subaccount` byte array (`[i; 32]` for `i` in `0..100`), a valid DOGE address, and `amount = min_retrieve_amount`. No `icrc2_approve` needed.
3. Each call passes the guard check (distinct `Account` keys), acquires a slot, and suspends at `check_address(...).await`.
4. Assert: `retrieve_btc_accounts.len() == 100`.
5. From a different principal (victim), call `retrieve_doge_with_approval`.
6. Assert: response is `Err(TemporarilyUnavailable("too many concurrent requests"))`.
7. Assert: victim's call never reaches the ledger.

This directly maps to `GuardError::TooManyConcurrentRequests` → `RetrieveBtcWithApprovalError::TemporarilyUnavailable` at: [6](#0-5)

### Citations

**File:** rs/bitcoin/ckbtc/minter/src/updates/retrieve_btc.rs (L22-22)
```rust
const MAX_CONCURRENT_PENDING_REQUESTS: usize = 5000;
```

**File:** rs/bitcoin/ckbtc/minter/src/updates/retrieve_btc.rs (L123-131)
```rust
impl From<GuardError> for RetrieveBtcWithApprovalError {
    fn from(e: GuardError) -> Self {
        match e {
            GuardError::AlreadyProcessing => Self::AlreadyProcessing,
            GuardError::TooManyConcurrentRequests => {
                Self::TemporarilyUnavailable("too many concurrent requests".to_string())
            }
        }
    }
```

**File:** rs/bitcoin/ckbtc/minter/src/updates/retrieve_btc.rs (L259-279)
```rust
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

**File:** rs/bitcoin/ckbtc/minter/src/guard.rs (L6-6)
```rust
const MAX_CONCURRENT: usize = 100;
```

**File:** rs/bitcoin/ckbtc/minter/src/guard.rs (L45-60)
```rust
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

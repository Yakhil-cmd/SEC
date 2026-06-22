### Title
Single Principal Exhausts Global `retrieve_btc_accounts` Guard via Subaccount Enumeration in `retrieve_btc_with_approval` — (`rs/bitcoin/ckbtc/minter/src/guard.rs`, `rs/bitcoin/ckbtc/minter/src/updates/retrieve_btc.rs`)

---

### Summary

The `retrieve_btc_with_approval` endpoint keys its concurrency guard on the full ICRC-1 `Account` (owner + subaccount), not on the caller's `Principal` alone. Because `MAX_CONCURRENT = 100` and a single principal can supply 100 distinct `from_subaccount` values, one unprivileged attacker can occupy every slot in `retrieve_btc_accounts` while each call is suspended at the `check_address` inter-canister await point — with zero ckBTC funds required. All subsequent callers receive `TemporarilyUnavailable("too many concurrent requests")` until the attacker's calls drain.

---

### Finding Description

**Guard implementation**

`Guard::new` in `guard.rs` tracks in-flight requests in a `BTreeSet<Account>`. It rejects a new request if the set already contains the exact `Account` (`AlreadyProcessing`) or if the set has reached `MAX_CONCURRENT` entries (`TooManyConcurrentRequests`): [1](#0-0) [2](#0-1) 

**Asymmetry between the two withdrawal endpoints**

`retrieve_btc` always passes `subaccount: None`, so one principal can hold at most one slot: [3](#0-2) 

`retrieve_btc_with_approval` passes the caller-supplied `args.from_subaccount` directly into the guard key: [4](#0-3) 

Because `Account` equality is `(owner, subaccount)`, a single principal with 100 distinct subaccounts is treated as 100 independent accounts by the guard.

**Guard is acquired before any balance or allowance check**

The guard is taken at line 263, but the `burn_ckbtcs_icrc2` call (which would fail with `InsufficientAllowance` for a fundless attacker) does not occur until line 314 — after the `check_address` inter-canister await at line 283: [5](#0-4) 

The guard slot is held for the entire duration of the `check_address` call. No ckBTC balance or ICRC-2 approval is needed to hold a slot.

---

### Impact Explanation

With all 100 slots occupied, every new caller — regardless of principal — receives:

```
TemporarilyUnavailable("too many concurrent requests")
``` [6](#0-5) 

This blocks **all** ckBTC withdrawals (both `retrieve_btc` and `retrieve_btc_with_approval`) for the duration of the attack. The attacker can sustain the DoS by continuously re-submitting calls as old ones complete, since each `check_address` round-trip takes at least two consensus rounds (~4 s on mainnet), giving ample time to refill slots.

---

### Likelihood Explanation

- **No funds required**: the guard is acquired before any balance/allowance check.
- **Single principal sufficient**: 100 subaccounts from one principal exhaust the global limit.
- **Repeatable**: the attacker re-submits as slots free up; the DoS is sustained, not one-shot.
- **No privileged access**: the endpoint is open to any ingress caller.
- **Low cost**: IC ingress fees are negligible; calls that fail at `InsufficientAllowance` cost only the ingress fee.

---

### Recommendation

Key the `retrieve_btc_with_approval` guard on `caller` (`Principal`) rather than on the full `Account`, matching the behavior of `retrieve_btc`. Alternatively, enforce a per-principal slot cap (e.g., max 1 concurrent slot per principal regardless of subaccount) before checking the global `MAX_CONCURRENT` limit. The guard in `retrieve_btc` already demonstrates the correct pattern:

```rust
// retrieve_btc (correct — one slot per principal)
let _guard = retrieve_btc_guard(Account { owner: caller, subaccount: None })?;

// retrieve_btc_with_approval (vulnerable — one slot per subaccount)
let _guard = retrieve_btc_guard(Account { owner: caller, subaccount: args.from_subaccount })?;
```

The fix is to change the guard key in `retrieve_btc_with_approval` to `Account { owner: caller, subaccount: None }`, or add a pre-check that rejects any caller who already has any in-flight request regardless of subaccount.

---

### Proof of Concept

State-machine test sketch (no real IC needed):

```rust
// 1. Initialize minter state
init(init_args(), &IC_CANISTER_RUNTIME);

// 2. Attacker principal acquires all 100 slots via distinct subaccounts
let attacker = Principal::from_text("...").unwrap();
let guards: Vec<_> = (0u8..100)
    .map(|i| retrieve_btc_guard(Account {
        owner: attacker,
        subaccount: Some([i; 32]),
    }).expect("slot should be available"))
    .collect();

// 3. Legitimate user is blocked
let victim = Principal::from_text("...").unwrap();
assert_eq!(
    retrieve_btc_guard(Account { owner: victim, subaccount: None }),
    Err(GuardError::TooManyConcurrentRequests)   // all 100 slots exhausted
);
``` [7](#0-6) 

The existing unit test `guard_prevents_more_than_max_concurrent_accounts` already proves the mechanism works exactly this way — it fills 50 slots with subaccounts of principal `0` and 50 slots with distinct principals, confirming a single principal's subaccounts count against the global cap. [8](#0-7)

### Citations

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

**File:** rs/bitcoin/ckbtc/minter/src/guard.rs (L140-162)
```rust
    #[test]
    fn guard_prevents_more_than_max_concurrent_accounts() {
        // test that at most MAX_CONCURRENT guards can be created if each one
        // is for a different principal

        init(init_args(), &IC_CANISTER_RUNTIME);
        let guards: Vec<_> = (0..MAX_CONCURRENT / 2)
            .map(|id| {
                balance_update_guard(test_account(0, Some(id as u8))).unwrap_or_else(|e| {
                    panic!("Could not create guard for subaccount num {id}: {e:#?}")
                })
            })
            .chain((MAX_CONCURRENT / 2..MAX_CONCURRENT).map(|id| {
                balance_update_guard(test_account(id as u64, None)).unwrap_or_else(|e| {
                    panic!("Could not create guard for principal num {id}: {e:#?}")
                })
            }))
            .collect();
        assert_eq!(guards.len(), MAX_CONCURRENT);
        let account = test_account(MAX_CONCURRENT as u64 + 1, None);
        let res = balance_update_guard(account).err();
        assert_eq!(res, Some(GuardError::TooManyConcurrentRequests));
    }
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

**File:** rs/bitcoin/ckbtc/minter/src/updates/retrieve_btc.rs (L162-165)
```rust
    let _guard = retrieve_btc_guard(Account {
        owner: caller,
        subaccount: None,
    })?;
```

**File:** rs/bitcoin/ckbtc/minter/src/updates/retrieve_btc.rs (L259-263)
```rust
    let caller_account = Account {
        owner: caller,
        subaccount: args.from_subaccount,
    };
    let _guard = retrieve_btc_guard(caller_account)?;
```

**File:** rs/bitcoin/ckbtc/minter/src/updates/retrieve_btc.rs (L281-307)
```rust
    let btc_checker_principal = read_state(|s| s.btc_checker_principal).map(|id| id.get().into());

    match check_address(
        btc_checker_principal,
        parsed_address.display(btc_network),
        runtime,
    )
    .await
    {
        Err(error) => {
            return Err(RetrieveBtcWithApprovalError::GenericError {
                error_message: format!(
                    "Failed to call Bitcoin checker canister with error: {error:?}"
                ),
                error_code: ErrorCode::CheckCallFailed as u64,
            });
        }
        Ok(status) => match status {
            BtcAddressCheckStatus::Tainted => {
                return Err(RetrieveBtcWithApprovalError::GenericError {
                    error_message: "Destination address is tainted".to_string(),
                    error_code: ErrorCode::TaintedAddress as u64,
                });
            }
            BtcAddressCheckStatus::Clean => {}
        },
    }
```

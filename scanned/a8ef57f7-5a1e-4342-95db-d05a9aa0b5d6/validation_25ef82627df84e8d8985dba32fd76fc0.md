### Title
Unprivileged Guard Exhaustion DoS in `update_balance` — (`rs/bitcoin/ckbtc/minter/src/guard.rs`, `rs/bitcoin/ckbtc/minter/src/updates/update_balance.rs`)

### Summary

A single non-anonymous attacker can exhaust the global `PendingBalanceUpdates` guard set (capped at `MAX_CONCURRENT = 100`) by submitting 100 concurrent `update_balance` calls with 100 distinct `Account` values, causing all subsequent calls — including those from legitimate depositors — to receive `UpdateBalanceError::TemporarilyUnavailable`. The attack is repeatable and can be sustained indefinitely.

### Finding Description

The guard mechanism in `guard.rs` enforces a global cap of 100 concurrent `update_balance` executions: [1](#0-0) [2](#0-1) 

The guard is keyed by `Account` (owner + subaccount), **not** by the ingress caller's principal. In `update_balance`, the account is constructed from the caller-supplied `args.owner` field: [3](#0-2) 

There is no requirement that `args.owner` equals the caller. The only caller check is `check_anonymous_caller()`, which merely rejects the anonymous principal: [4](#0-3) 

The guard is acquired **before** the async `get_utxos` inter-canister call and held across it: [5](#0-4) 

On the IC, a canister processes other queued ingress messages while awaiting inter-canister calls. This means a single attacker principal can:

1. Submit 100 `update_balance` calls, each with a distinct `args.owner` (or distinct `args.subaccount`).
2. Each call acquires a guard for a unique `Account` and suspends at `get_utxos`.
3. While suspended, the canister dequeues and processes the next attacker message.
4. After 100 in-flight calls, `accounts.len() >= MAX_CONCURRENT` is true.
5. Any subsequent call — from any principal — hits `GuardError::TooManyConcurrentRequests`, mapped to: [6](#0-5) 

The attacker can sustain the DoS by immediately re-submitting calls as guards are released, keeping the set perpetually full.

### Impact Explanation

Legitimate depositors calling `update_balance` receive `UpdateBalanceError::TemporarilyUnavailable("too many concurrent requests")` and cannot notify the minter of their Bitcoin deposits. While the error is designed to be retried, the attacker can continuously cycle through 100 distinct accounts to maintain the guard set at capacity, indefinitely delaying deposit credit for all other users. No funds are directly stolen, but deposit finality is blocked for the duration of the attack.

### Likelihood Explanation

The attack requires only a single non-anonymous principal and 100 ingress messages — well within the IC's per-canister ingress queue capacity. The attacker pays `get_utxos` cycle costs, but these are modest. No privileged access, governance majority, or key material is needed. The attack is fully reproducible in a local state-machine test.

### Recommendation

Partition the concurrent guard limit by caller principal rather than using a single global pool. For example, enforce a per-caller cap (e.g., 1–5 concurrent calls per principal) in addition to the global cap, so no single principal can exhaust the shared resource. Alternatively, require that `args.owner` equals the ingress caller (or a principal the caller is authorized to act on behalf of), preventing an attacker from occupying guard slots for arbitrary accounts.

### Proof of Concept

```rust
// State-machine test sketch
for i in 0..100u64 {
    env.submit_ingress_as(
        attacker_principal,
        minter_id,
        "update_balance",
        Encode!(&UpdateBalanceArgs {
            owner: Some(Principal::from_slice(&i.to_le_bytes())), // 100 distinct owners
            subaccount: None,
        }).unwrap(),
    );
}
// All 100 calls are now in-flight, each holding a PendingBalanceUpdates guard
// while awaiting get_utxos from the Bitcoin canister.

// Victim's call:
let res = env.execute_ingress_as(
    victim_principal,
    minter_id,
    "update_balance",
    Encode!(&UpdateBalanceArgs { owner: None, subaccount: None }).unwrap(),
).unwrap();
let decoded = Decode!(&res.bytes(), Result<Vec<UtxoStatus>, UpdateBalanceError>).unwrap();
assert!(matches!(decoded, Err(UpdateBalanceError::TemporarilyUnavailable(_))));
```

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

**File:** rs/bitcoin/ckbtc/minter/src/updates/update_balance.rs (L108-116)
```rust
impl From<GuardError> for UpdateBalanceError {
    fn from(e: GuardError) -> Self {
        match e {
            GuardError::AlreadyProcessing => Self::AlreadyProcessing,
            GuardError::TooManyConcurrentRequests => {
                Self::TemporarilyUnavailable("too many concurrent requests".to_string())
            }
        }
    }
```

**File:** rs/bitcoin/ckbtc/minter/src/updates/update_balance.rs (L164-183)
```rust
    let caller_account = Account {
        owner: args.owner.unwrap_or(caller),
        subaccount: args.subaccount,
    };
    let _guard = balance_update_guard(caller_account)?;

    let address = state::read_state(|s| runtime.derive_user_address(s, &caller_account));

    let (btc_network, min_confirmations) =
        state::read_state(|s| (s.btc_network, s.min_confirmations));

    let utxos = get_utxos(
        btc_network,
        &address,
        min_confirmations,
        CallSource::Client,
        runtime,
    )
    .await?
    .utxos;
```

**File:** rs/bitcoin/ckbtc/minter/src/main.rs (L117-121)
```rust
fn check_anonymous_caller() {
    if ic_cdk::api::msg_caller() == Principal::anonymous() {
        panic!("anonymous caller not allowed")
    }
}
```

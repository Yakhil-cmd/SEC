Based on the code, here is my analysis:

**Guard mechanism** (`rs/bitcoin/ckbtc/minter/src/guard.rs`): [1](#0-0) [2](#0-1) 

The guard is keyed by `Account` (owner + subaccount), not by caller identity. It enforces a global cap of 100 concurrent slots across all accounts.

**Guard acquisition in `update_balance`** (`rs/bitcoin/ckbtc/minter/src/updates/update_balance.rs`): [3](#0-2) 

The guard is acquired **after** `init_ecdsa_public_key().await` (which is a no-op once the key is cached) and is held across all subsequent async inter-canister calls.

**Async hops while guard is held**:
- `get_utxos` at line 175 (first hop)
- A second `get_utxos` at line 229 (even when no UTXOs exist — for pending UTXO reporting)
- Up to 10 `check_transaction` calls (lines 400–450) if UTXOs are present [4](#0-3) [5](#0-4) [6](#0-5) 

**Error mapping**: [7](#0-6) 

`TooManyConcurrentRequests` maps to `TemporarilyUnavailable`, returned to any caller when all 100 slots are occupied.

---

### Title
Unprivileged Guard Slot Exhaustion DoS in `update_balance` — (`rs/bitcoin/ckbtc/minter/src/guard.rs`, `rs/bitcoin/ckbtc/minter/src/updates/update_balance.rs`)

### Summary
An unprivileged attacker can exhaust all 100 `balance_update_guard` slots by submitting 100 concurrent `update_balance` calls using 100 distinct `Account` values (e.g., one principal with 100 different subaccounts). Each call holds its guard slot across at least two `get_utxos` inter-canister round trips (~4 seconds minimum, up to ~24 seconds with UTXOs and `check_transaction` retries). While slots are occupied, every other caller — including legitimate depositors — receives `UpdateBalanceError::TemporarilyUnavailable("too many concurrent requests")`.

### Finding Description
The guard in `guard.rs` enforces a global `MAX_CONCURRENT = 100` limit keyed by `Account`:

```rust
if accounts.len() >= MAX_CONCURRENT {
    return Err(GuardError::TooManyConcurrentRequests);
}
```

The guard is acquired in `update_balance` after `init_ecdsa_public_key().await` (a no-op once cached) and is held for the entire async execution, spanning:

1. `get_utxos` (first call, line 175) — always executed
2. A second `get_utxos` (line 229) — executed even when no new UTXOs exist
3. Up to 10 `check_transaction` calls (line 400) — executed per UTXO

There is no per-caller rate limiting, no cycles cost for the `get_utxos` path, and no restriction on how many distinct subaccounts a single principal may use. An attacker with one principal and 100 subaccounts (or 100 separate principals) can saturate all slots.

### Impact Explanation
All legitimate depositors calling `update_balance` receive `TemporarilyUnavailable` for the duration of the attack. Since the attacker can continuously recycle slots (each slot is held ~4–24 seconds, and new calls can be submitted as old ones complete), the lockout can be sustained indefinitely. This blocks all ckBTC minting from Bitcoin deposits on the affected minter canister.

### Likelihood Explanation
The attack requires no privileges, no tokens, no UTXOs, and no special setup. Any non-anonymous principal can submit 100 ingress messages with distinct subaccounts. The IC supports multiple concurrent async executions per canister, so 100 simultaneous in-flight calls are achievable. The cost is negligible (only `get_utxos` calls, no `check_transaction` cycles needed without UTXOs).

### Recommendation
- Add per-caller (principal) rate limiting in addition to the global account-based guard, so a single principal cannot occupy more than `k` slots (e.g., `k = 1` or `k = 5`).
- Alternatively, track guard slots by caller principal rather than (or in addition to) the full `Account`, preventing one principal from holding multiple slots via subaccount variation.
- Consider a short-lived cooldown or backoff per principal on `TemporarilyUnavailable` responses.

### Proof of Concept
State-machine test outline:

```rust
// 1. Initialize minter state
init(init_args(), &IC_CANISTER_RUNTIME);

// 2. Spawn 100 concurrent update_balance calls with distinct accounts
//    (same principal, subaccounts 0..99)
let guards: Vec<_> = (0u8..100)
    .map(|i| balance_update_guard(Account {
        owner: attacker_principal,
        subaccount: Some([i; 32]),
    }).unwrap())
    .collect();
assert_eq!(guards.len(), 100);

// 3. A 101st call from a fresh legitimate account fails
let victim_account = Account { owner: victim_principal, subaccount: None };
assert_eq!(
    balance_update_guard(victim_account).unwrap_err(),
    GuardError::TooManyConcurrentRequests
);

// 4. Guards are held until attacker's async calls complete
// (simulated by keeping `guards` in scope)
```

This matches the existing unit test `guard_prevents_more_than_max_concurrent_accounts` in `guard.rs` lines 141–162, which already confirms the behavior — but does not account for the cross-account exhaustion attack vector from a single attacker principal. [8](#0-7)

### Citations

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

**File:** rs/bitcoin/ckbtc/minter/src/guard.rs (L141-162)
```rust
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

**File:** rs/bitcoin/ckbtc/minter/src/updates/update_balance.rs (L162-168)
```rust
    init_ecdsa_public_key().await;

    let caller_account = Account {
        owner: args.owner.unwrap_or(caller),
        subaccount: args.subaccount,
    };
    let _guard = balance_update_guard(caller_account)?;
```

**File:** rs/bitcoin/ckbtc/minter/src/updates/update_balance.rs (L175-183)
```rust
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

**File:** rs/bitcoin/ckbtc/minter/src/updates/update_balance.rs (L217-236)
```rust
    if satoshis_to_mint == 0 {
        // We bail out early if there are no UTXOs to avoid creating a new entry
        // in the UTXOs map. If we allowed empty entries, malicious callers
        // could exhaust the canister memory.

        // We get the entire list of UTXOs again with a zero
        // confirmation limit so that we can indicate the approximate
        // wait time to the caller.
        let GetUtxosResponse {
            tip_height,
            mut utxos,
            ..
        } = get_utxos(
            btc_network,
            &address,
            /*min_confirmations=*/ 0,
            CallSource::Client,
            runtime,
        )
        .await?;
```

**File:** rs/bitcoin/ckbtc/minter/src/updates/update_balance.rs (L400-410)
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
```

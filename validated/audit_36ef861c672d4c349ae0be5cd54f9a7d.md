Audit Report

## Title
Unprivileged Caller Can DoS `update_balance` by Exhausting Guard Slots via Arbitrary `owner` Parameter - (File: `rs/bitcoin/ckbtc/minter/src/updates/update_balance.rs`)

## Summary
The ckBTC minter's `update_balance` endpoint keys its concurrency guard on the caller-supplied `args.owner` rather than on the actual `msg_caller()`. Any non-anonymous principal can supply 100 distinct arbitrary principals as `owner`, filling the `MAX_CONCURRENT = 100` guard set and causing every legitimate `update_balance` call to fail with `TooManyConcurrentRequests`. Since `update_balance` is the sole mechanism for minting ckBTC after a BTC deposit, this constitutes a complete, sustained denial of the deposit flow.

## Finding Description
In `rs/bitcoin/ckbtc/minter/src/updates/update_balance.rs` at lines 164–168, the guard account is constructed from the caller-supplied `args.owner`:

```rust
let caller_account = Account {
    owner: args.owner.unwrap_or(caller),   // attacker-controlled
    subaccount: args.subaccount,
};
let _guard = balance_update_guard(caller_account)?;
``` [1](#0-0) 

`balance_update_guard` calls `Guard::new(account)`, which inserts the account into `state.update_balance_accounts`. It returns `TooManyConcurrentRequests` once the set reaches `MAX_CONCURRENT = 100`: [2](#0-1) [3](#0-2) 

The guard is released only when the `Guard` value is dropped at the end of the async call: [4](#0-3) 

The only access control on the endpoint is `check_anonymous_caller()`, meaning any non-anonymous principal can call with any `owner`: [5](#0-4) 

By contrast, `retrieve_btc` correctly keys its guard on `caller` directly, not on any user-supplied field: [6](#0-5) 

**Exploit flow:**
1. Attacker generates 100 distinct principals `P_1…P_100`.
2. Attacker submits 100 concurrent ingress calls to `update_balance` with `owner = Some(P_i)` for each. Each call acquires a guard slot and then awaits the async `get_utxos` response from the Bitcoin canister.
3. While all 100 calls are in-flight, `update_balance_accounts.len() == 100`.
4. Any legitimate user calling `update_balance` receives `UpdateBalanceError::TemporarilyUnavailable("too many concurrent requests")`.
5. As slots free up, the attacker re-submits to maintain the full set.

The `init_ecdsa_public_key().await` call at line 162 occurs *before* the guard is acquired, meaning the guard acquisition itself is not protected by any prior async barrier that would serialize attackers. [7](#0-6) 

## Impact Explanation
`update_balance` is the only mechanism by which users mint ckBTC after depositing BTC on-chain. A sustained DoS of this endpoint prevents all BTC→ckBTC conversions for all users. This matches the allowed High impact: **"Significant Chain Fusion, ck-token, ledger... security impact with concrete user or protocol harm"** and **"Application/platform-level DoS... not based on raw volumetric DDoS"**. The attack exploits a specific application logic flaw (guard keyed on attacker-controlled input), not volumetric traffic.

**Severity: High ($2,000–$10,000)**

## Likelihood Explanation
No special privileges, tokens, or prior state are required. Any non-anonymous principal on mainnet can submit ingress messages to the minter. The attack requires only 100 concurrent calls — well within normal IC ingress capacity and not subject to volumetric rate limiting. The attacker can maintain the DoS indefinitely by cycling through fresh principals as slots free up. The same pattern exists in the ckDOGE minter.

## Recommendation
Key the guard on the actual `msg_caller()` (i.e., `caller`) rather than on `args.owner`. The `owner` parameter legitimately allows third parties to trigger minting on behalf of others, but the concurrency guard should track the actual caller to prevent abuse:

```rust
// Fixed: guard on actual caller, not on user-supplied owner
let _guard = balance_update_guard(Account { owner: caller, subaccount: None })?;
let caller_account = Account {
    owner: args.owner.unwrap_or(caller),
    subaccount: args.subaccount,
};
```

This mirrors the pattern already used in `retrieve_btc` at lines 162–165. [6](#0-5) 

## Proof of Concept
1. Obtain any non-anonymous IC principal (e.g., a self-authenticating identity).
2. Generate 100 distinct principals `P_1…P_100` via `Principal::self_authenticating`.
3. Submit 100 concurrent ingress `update_balance` calls to the ckBTC minter, each with `owner = Some(P_i)` and `subaccount = None`. These principals need no UTXOs — each call will await `get_utxos` and eventually return `NoNewUtxos`, but only after the async round-trip.
4. While all 100 calls are in-flight, submit a legitimate `update_balance` call from a real depositor. Observe `UpdateBalanceError::TemporarilyUnavailable("too many concurrent requests")`.
5. Repeat step 3 as slots free up to maintain the DoS.

A deterministic integration test can reproduce this using PocketIC by spawning 100 concurrent update calls with distinct owner principals and asserting that the 101st call returns `TooManyConcurrentRequests`.

### Citations

**File:** rs/bitcoin/ckbtc/minter/src/updates/update_balance.rs (L162-168)
```rust
    init_ecdsa_public_key().await;

    let caller_account = Account {
        owner: args.owner.unwrap_or(caller),
        subaccount: args.subaccount,
    };
    let _guard = balance_update_guard(caller_account)?;
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

**File:** rs/bitcoin/ckbtc/minter/src/guard.rs (L63-67)
```rust
impl<PR: PendingRequests> Drop for Guard<PR> {
    fn drop(&mut self) {
        mutate_state(|s| PR::pending_requests(s).remove(&self.account));
    }
}
```

**File:** rs/bitcoin/ckbtc/minter/src/main.rs (L196-200)
```rust
#[update]
async fn update_balance(args: UpdateBalanceArgs) -> Result<Vec<UtxoStatus>, UpdateBalanceError> {
    check_anonymous_caller();
    check_postcondition(updates::update_balance::update_balance(args, &IC_CANISTER_RUNTIME).await)
}
```

**File:** rs/bitcoin/ckbtc/minter/src/updates/retrieve_btc.rs (L162-165)
```rust
    let _guard = retrieve_btc_guard(Account {
        owner: caller,
        subaccount: None,
    })?;
```

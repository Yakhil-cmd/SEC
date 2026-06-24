Audit Report

## Title
Unprivileged Caller Can DoS `update_balance` by Exhausting Guard Slots via Arbitrary `owner` Parameter - (File: `rs/bitcoin/ckbtc/minter/src/updates/update_balance.rs`)

## Summary
The `update_balance` endpoint keys its concurrency guard on the caller-supplied `args.owner` rather than on the actual `msg_caller()`. Any non-anonymous principal can flood 100 concurrent calls with distinct victim principals as `owner`, exhausting all `MAX_CONCURRENT = 100` guard slots and causing every subsequent legitimate `update_balance` call to fail with `TooManyConcurrentRequests`. The attacker can re-submit continuously to maintain the denial of service indefinitely, blocking all BTC→ckBTC minting.

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

The guard is released only when the `Guard` value is dropped (i.e., when the async call completes): [4](#0-3) 

The endpoint is publicly callable by any non-anonymous principal with no other preconditions: [5](#0-4) 

Because `args.owner` is entirely attacker-controlled, any caller can submit 100 concurrent calls each with a distinct `owner = Some(P_i)` for freshly generated principals with no UTXOs. Each call acquires one guard slot and then awaits an async `get_utxos` response. While all 100 calls are in-flight, `update_balance_accounts.len() == 100`, and every legitimate user calling `update_balance` receives `TemporarilyUnavailable("too many concurrent requests")`. The attacker re-submits as slots free up to maintain the DoS. A targeted variant also works: calling `update_balance(owner = Some(victim_principal))` before the victim causes the victim's own call to return `AlreadyProcessing`.

Existing checks are insufficient: `check_anonymous_caller()` only blocks the anonymous principal; it does not rate-limit or restrict the `owner` parameter in any way. [6](#0-5) 

## Impact Explanation
`update_balance` is the sole mechanism by which users mint ckBTC after depositing BTC on-chain. Blocking it prevents all users from completing BTC→ckBTC conversions for as long as the attacker maintains the DoS. This matches the allowed ICP bounty impact: **High — Application/platform-level DoS with concrete user and protocol harm** (complete disruption of the ckBTC deposit flow, a core Chain Fusion financial integration). The same vulnerability exists in the ckDOGE minter, which exposes an identical `update_balance` signature.

**Severity: High ($2,000–$10,000)**

## Likelihood Explanation
There are no preconditions. Any non-anonymous principal can call `update_balance` with an arbitrary `owner` on mainnet. No special permissions, tokens, funds, or prior state are required. The attacker needs only the ability to submit ingress messages to the minter canister, which is open to the public. The attack is cheap: calls to accounts with no UTXOs complete quickly (after one `get_utxos` round-trip), so the attacker can cheaply cycle through slots. The DoS is repeatable and maintainable indefinitely.

**Likelihood: High**

## Recommendation
Key the guard on the actual `msg_caller()` rather than on `args.owner`. The `owner` parameter legitimately allows third parties to trigger minting on behalf of others, but the concurrency guard should track the actual caller to prevent abuse:

```rust
// After (fixed):
let _guard = balance_update_guard(Account { owner: caller, subaccount: None })?;
let caller_account = Account {
    owner: args.owner.unwrap_or(caller),
    subaccount: args.subaccount,
};
```

This mirrors the approach used in `retrieve_btc`, which correctly guards on `caller` directly: [7](#0-6) 

Apply the same fix to the ckDOGE minter's `update_balance`.

## Proof of Concept
1. Generate 100 distinct IC principals `P_1 … P_100` (e.g., via `Principal::self_authenticating` with 100 different public keys).
2. Submit 100 concurrent ingress update calls to the ckBTC minter's `update_balance` endpoint, each with `owner = Some(P_i)` and `subaccount = None`. These principals have no UTXOs, so each call will return `NoNewUtxos` after awaiting the async `get_utxos` response.
3. While all 100 calls are in-flight, `state.update_balance_accounts.len() == 100`.
4. Any legitimate user calling `update_balance` receives `UpdateBalanceError::TemporarilyUnavailable("too many concurrent requests")`.
5. As each attacker call completes and frees a slot, immediately re-submit a new call for a fresh principal to keep the set full.
6. The victim's BTC deposit is confirmed on-chain but ckBTC cannot be minted for as long as the attacker maintains the DoS.

This can be validated with a deterministic PocketIC integration test: initialize the minter, spawn 100 concurrent `update_balance` calls with distinct owner principals, then assert that a 101st call from a legitimate user returns `TooManyConcurrentRequests`. The existing unit test `guard_prevents_more_than_max_concurrent_accounts` in `rs/bitcoin/ckbtc/minter/src/guard.rs` already demonstrates the guard saturation behavior: [8](#0-7)

### Citations

**File:** rs/bitcoin/ckbtc/minter/src/updates/update_balance.rs (L164-168)
```rust
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

**File:** rs/bitcoin/ckbtc/minter/src/main.rs (L117-121)
```rust
fn check_anonymous_caller() {
    if ic_cdk::api::msg_caller() == Principal::anonymous() {
        panic!("anonymous caller not allowed")
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

**File:** rs/bitcoin/ckbtc/minter/src/updates/retrieve_btc.rs (L150-165)
```rust
    let caller = ic_cdk::api::msg_caller();

    state::read_state(|s| s.mode.is_withdrawal_available_for(&caller))
        .map_err(RetrieveBtcError::TemporarilyUnavailable)?;

    let _ecdsa_public_key = init_ecdsa_public_key().await;
    let main_address_str = state::read_state(|s| runtime.derive_minter_address_str(s));

    if args.address == main_address_str {
        ic_cdk::trap("illegal retrieve_btc target");
    }

    let _guard = retrieve_btc_guard(Account {
        owner: caller,
        subaccount: None,
    })?;
```

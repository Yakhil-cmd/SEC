### Title
No Deadline or Cancellation Mechanism for Scheduled XCC Promises - (`File: etc/xcc-router/src/lib.rs`)

### Summary

The XCC router's `execute_scheduled` function has no deadline check and no cancellation mechanism. Once a user schedules a NEAR cross-contract call via `CrossContractCallArgs::Delayed`, the stored promise and any NEAR tokens attached to it are locked in the router indefinitely with no way to revoke or expire them. Any external account can trigger execution at any future time.

### Finding Description

When a user's EVM contract calls the XCC precompile with `CrossContractCallArgs::Delayed`, the engine calls `router.schedule(promise)`, which stores the `PromiseArgs` in `scheduled_promises` keyed by a monotonically incrementing nonce. [1](#0-0) 

The stored promise can then be executed by **anyone** at any future time via `execute_scheduled`: [2](#0-1) 

The `PromiseArgs` stored in `scheduled_promises` can carry an `attached_balance` (NEAR tokens). The XCC precompile computes `attached_near = call.total_near()` and withdraws that amount from the user's wNEAR balance, transferring it to the router sub-account at scheduling time: [3](#0-2) 

The `PromiseArgs` type that is stored has no expiry field: [4](#0-3) 

There is no function to cancel a scheduled promise and recover the attached NEAR. The router's `schedule` and `execute_scheduled` are the only two lifecycle methods for delayed promises. [5](#0-4) 

### Impact Explanation

NEAR tokens attached to a scheduled promise are locked in the router sub-account with no cancellation path. If a user schedules a delayed XCC call (e.g., an `ft_transfer` with 1 NEAR attached) and later wishes to abort — because the recipient is compromised, market conditions changed, or the call was made in error — there is no mechanism to recover the locked NEAR. The funds remain frozen in the router until some external party calls `execute_scheduled`, which can happen at any arbitrarily distant future time. This satisfies **High: Temporary freezing of funds** (and can become permanent if the target contract is deleted or the call permanently fails after execution).

### Likelihood Explanation

Any Aurora user who uses the `CrossContractCallArgs::Delayed` XCC path is exposed. The XCC feature is a documented, production-facing capability. The user has no on-chain recourse once the EVM transaction that triggered `schedule` is confirmed. The nonce of the scheduled promise is logged on-chain (`"Promise scheduled at nonce {}"`) making it trivially discoverable by any observer who can then choose the most disadvantageous moment to call `execute_scheduled`.

### Recommendation

1. Add an optional `deadline: Option<u64>` (block timestamp) field to `PromiseArgs` or to the router's stored entry. In `execute_scheduled`, reject execution if `env::block_timestamp() > deadline`.
2. Add a `cancel_scheduled(nonce: U64)` method callable only by the parent (Aurora Engine), triggered by a new EVM precompile call, so users can recover locked NEAR tokens.
3. Alternatively, follow the pattern recommended in the reference report: allow nonces to be invalidated directly so conflicting or stale scheduled promises can be voided.

### Proof of Concept

1. Alice's EVM contract calls the XCC precompile with `CrossContractCallArgs::Delayed(promise)` where `promise.attached_balance = 5 NEAR`. The engine calls `router.schedule(promise)`. Alice's wNEAR is burned and 5 NEAR is transferred to the router. The nonce `0` is logged.
2. Alice realizes the target contract is malicious and wants to cancel. There is no `cancel_scheduled` function. Her 5 NEAR is locked.
3. Six months later, an external account calls `router.execute_scheduled({"nonce": "0"})`. The promise executes, sending Alice's 5 NEAR to the malicious contract. Alice has no recourse.

The relevant code path confirming no deadline or cancellation exists: [6](#0-5) [1](#0-0)

### Citations

**File:** etc/xcc-router/src/lib.rs (L50-62)
```rust
pub struct Router {
    /// The account id of the Aurora Engine instance that controls this router.
    parent: LazyOption<AccountId>,
    /// The version of the router contract that was last deployed
    version: LazyOption<u32>,
    /// A sequential id to keep track of how many scheduled promises this router has executed.
    /// This allows multiple promises to be scheduled before any of them are executed.
    nonce: LazyOption<u64>,
    /// The storage for the scheduled promises.
    scheduled_promises: LookupMap<u64, PromiseArgs>,
    /// Account ID for the wNEAR contract.
    wnear_account: AccountId,
}
```

**File:** etc/xcc-router/src/lib.rs (L136-144)
```rust
    pub fn schedule(&mut self, #[serializer(borsh)] promise: PromiseArgs) {
        self.assert_preconditions();

        let nonce = self.nonce.get().unwrap_or_default();
        self.scheduled_promises.insert(nonce, promise);
        self.nonce.set(&(nonce + 1));

        near_sdk::log!("Promise scheduled at nonce {}", nonce);
    }
```

**File:** etc/xcc-router/src/lib.rs (L146-156)
```rust
    /// It is intentional that this function can be called by anyone (not just the parent).
    /// There is no security risk to allowing this function to be open because it can only
    /// act on promises that were created via `schedule`.
    #[payable]
    pub fn execute_scheduled(&mut self, nonce: U64) {
        let Some(promise) = self.scheduled_promises.remove(&nonce.0) else {
            env::panic_str("ERR_PROMISE_NOT_FOUND")
        };
        let promise_id = Self::promise_create(promise);
        env::promise_return(promise_id);
    }
```

**File:** engine-precompiles/src/xcc.rs (L159-172)
```rust
            CrossContractCallArgs::Delayed(call) => {
                let attached_near = call.total_near();
                let promise = PromiseCreateArgs {
                    target_account_id,
                    method: consts::ROUTER_SCHEDULE_NAME.into(),
                    args: borsh::to_vec(&call)
                        .map_err(|_| ExitError::Other(Cow::from(consts::ERR_SERIALIZE)))?,
                    attached_balance: ZERO_YOCTO,
                    // We don't need to add any gas to the amount need for the schedule call
                    // since the promise is not executed right away.
                    attached_gas: costs::ROUTER_SCHEDULE,
                };
                (promise, attached_near)
            }
```

**File:** engine-types/src/parameters/promise.rs (L275-285)
```rust
#[derive(Debug, BorshSerialize, BorshDeserialize)]
pub enum CrossContractCallArgs {
    /// The promise is to be executed immediately (as part of the same NEAR transaction as the EVM call).
    Eager(PromiseArgs),
    /// The promise is to be stored in the router contract, and can be executed in a future transaction.
    /// The purpose of this is to expand how much NEAR gas can be made available to a cross contract call.
    /// For example, if an expensive EVM call ends with a NEAR cross contract call, then there may not be
    /// much gas left to perform it. In this case, the promise could be `Delayed` (stored in the router)
    /// and executed in a separate transaction with a fresh 300 Tgas available for it.
    Delayed(PromiseArgs),
}
```

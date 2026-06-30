### Title
Unrestricted `execute_scheduled` in XCC Router Allows Forced Premature Execution of Scheduled Promises — (File: `etc/xcc-router/src/lib.rs`)

---

### Summary

The `execute_scheduled` function in the XCC router contract has no caller access control. Any NEAR account can force premature execution of a user's scheduled promise. Because the function removes the promise from storage before executing it, a failed premature execution permanently destroys the scheduled entry, temporarily freezing the user's funds and, in one-time-claim scenarios, permanently destroying unclaimed yield.

---

### Finding Description

The XCC subsystem allows EVM users to schedule NEAR cross-contract calls via the `CrossContractCallArgs::Delayed` path in the XCC precompile. The precompile constructs a promise targeting the user's router sub-account and calls its `schedule` method. `schedule` is correctly gated behind `assert_preconditions`, which enforces that only the Aurora Engine (the `parent`) may call it. [1](#0-0) 

However, `execute_scheduled` — the function that actually fires the stored promise — carries no such guard: [2](#0-1) 

The code comment explicitly states this is intentional, reasoning that the function "can only act on promises that were created via `schedule`." This mirrors the flawed reasoning in the `safeFunctionCall` report: the content is user-controlled, but the *timing* of execution is not.

The function unconditionally removes the promise from storage before creating the NEAR promise:

```rust
let Some(promise) = self.scheduled_promises.remove(&nonce.0) else {
    env::panic_str("ERR_PROMISE_NOT_FOUND")
};
let promise_id = Self::promise_create(promise);
env::promise_return(promise_id);
``` [3](#0-2) 

If the resulting NEAR promise fails (e.g., the target contract panics because a required condition — block height, staking epoch boundary, vesting cliff, one-time claim flag — is not yet met), the promise result is `Failed`, the attached NEAR is refunded to the router, but **the scheduled entry is gone permanently**. The user must re-enter the XCC precompile flow, pay gas again, and potentially re-fund the router.

The nonce counter is public state readable by any NEAR account, so an attacker can trivially discover pending nonces.

The XCC precompile confirms that the promise content (target account, method, args, attached NEAR) is fully user-controlled and can target any NEAR contract: [4](#0-3) 

The router's `promise_create` dispatches the stored args verbatim to any target: [5](#0-4) 

---

### Impact Explanation

**High — Temporary freezing of funds / Theft of unclaimed yield.**

- **General case (temporary freeze):** An attacker calls `execute_scheduled` before the user intends to, at a moment when the target contract will reject the call (paused contract, wrong epoch, insufficient state). The promise is consumed and deleted. The user's wNEAR or NEAR that was attached to the promise is refunded to the router, but the user cannot access it without re-scheduling — a temporary freeze requiring additional gas expenditure.

- **One-time-claim case (theft of unclaimed yield):** If the scheduled promise targets a contract with a one-time-claim mechanism (staking reward, airdrop, vesting release), a forced premature execution that fails permanently destroys the user's ability to claim. The yield is irrecoverably lost.

---

### Likelihood Explanation

**Medium.** The attacker must:
1. Observe that a router sub-account has a pending scheduled promise (trivially done by reading on-chain state or watching `schedule` call receipts).
2. Identify a window where the target contract will reject the call (e.e., before a time-lock expires).
3. Submit `execute_scheduled` with the correct nonce before the user does.

NEAR's transaction ordering is deterministic per shard, and there is no mempool privacy, so front-running is straightforward for an attacker monitoring the chain.

---

### Recommendation

1. **Restrict the caller.** Add a caller check analogous to `assert_preconditions` — only the `parent` (Aurora Engine) or the EVM address that owns the router (derivable from the sub-account name) should be permitted to call `execute_scheduled`.
2. **Alternatively, add a time-lock.** Store a `not_before` block height alongside each scheduled promise and enforce it inside `execute_scheduled`.
3. **Do not remove before confirming execution.** Consider a two-phase approach: mark the promise as "in-flight" rather than deleting it, and only delete it in a callback that confirms success.

---

### Proof of Concept

1. User A calls the XCC precompile with `CrossContractCallArgs::Delayed(PromiseArgs::Create(claim_reward_promise))`, where `claim_reward_promise` targets a NEAR staking contract's `claim` method that is only callable after epoch `E`. [4](#0-3) 

2. Aurora Engine calls `schedule` on User A's router (`{user_a_address}.aurora`) with the promise. The promise is stored at nonce `N`. [6](#0-5) 

3. Attacker reads nonce `N` from the router's public storage before epoch `E` arrives.

4. Attacker calls `execute_scheduled(nonce: N)` on the router. The function removes the promise from storage and fires the NEAR call to the staking contract. [7](#0-6) 

5. The staking contract panics (`ERR_EPOCH_NOT_REACHED`). The NEAR promise result is `Failed`. The attached NEAR is refunded to the router.

6. The scheduled entry at nonce `N` no longer exists. User A cannot re-execute it without going through the XCC precompile again. If the staking contract marks the claim as attempted-and-failed, User A permanently loses the staking reward.

### Citations

**File:** etc/xcc-router/src/lib.rs (L135-144)
```rust
    /// Similar security considerations here as for `execute`.
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

**File:** etc/xcc-router/src/lib.rs (L210-215)
```rust
    fn promise_create(promise: PromiseArgs) -> PromiseIndex {
        match promise {
            PromiseArgs::Create(call) => Self::base_promise_create(&call),
            PromiseArgs::Callback(cb) => Self::cb_promise_create(&cb),
            PromiseArgs::Recursive(p) => Self::recursive_promise_create(&p),
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

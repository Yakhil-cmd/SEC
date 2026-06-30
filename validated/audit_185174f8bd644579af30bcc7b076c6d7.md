### Title
XCC Router `execute_scheduled` Accepts NEAR Deposits Without Using Them, Permanently Locking Funds - (File: etc/xcc-router/src/lib.rs)

---

### Summary

The `execute_scheduled` function in the XCC router contract is marked `#[payable]`, allowing any caller to attach NEAR tokens to the call. However, the function never reads `env::attached_deposit()` and does not forward the attached NEAR to the scheduled promise. The router has no general withdrawal mechanism, so any NEAR attached to `execute_scheduled` is permanently absorbed into the router sub-account's balance with no recovery path.

---

### Finding Description

The `execute_scheduled` function in `etc/xcc-router/src/lib.rs` is annotated `#[payable]`, which in the NEAR SDK means the function explicitly accepts attached NEAR tokens from callers: [1](#0-0) 

The function removes the stored `PromiseArgs` from `scheduled_promises` and creates a promise from it. The `attached_balance` for the outgoing promise is embedded inside the stored `PromiseArgs` struct — it is entirely independent of any NEAR attached to the `execute_scheduled` call itself. The function never calls `env::attached_deposit()`, so any NEAR the caller attaches is silently absorbed into the router contract's own balance.

The comment above the function explicitly states this function is intentionally open to any caller: [2](#0-1) 

The router contract exposes no general withdrawal function. The complete set of methods that can move NEAR out of the router are:

1. `send_refund` — sends a hardcoded constant `REFUND_AMOUNT` (2 NEAR) back to the parent engine account, and only when called by the parent after a successful promise chain: [3](#0-2) 

2. Promises created by `execute` / `execute_scheduled` — these forward the `attached_balance` stored inside `PromiseArgs`, not any NEAR attached to the call itself.

There is no path by which a caller who attached NEAR to `execute_scheduled` can recover those tokens.

By contrast, `deploy_upgrade` is also `#[payable]` and is called by the engine with `fund_amount` attached intentionally (to fund the router's balance for storage staking), so that usage is by design: [4](#0-3) 

The `execute_scheduled` case has no such justification — the attached deposit serves no purpose and is never consumed.

---

### Impact Explanation

**Permanent freezing of funds.** Any NEAR attached to a call to `execute_scheduled` is absorbed into the router sub-account's balance. Because the router has no withdrawal function and `send_refund` transfers only a fixed 2 NEAR constant, the absorbed NEAR cannot be recovered by the caller. The router sub-account is a NEAR account controlled by the Aurora engine, not by the EVM user, so the EVM user has no independent mechanism to reclaim the locked NEAR. If the user ceases XCC activity, the absorbed NEAR remains locked indefinitely in the router sub-account.

---

### Likelihood Explanation

`execute_scheduled` is callable by any NEAR account — this is explicitly documented in the source. The `#[payable]` annotation signals to callers that attaching NEAR is permitted and potentially expected (e.g., a user might believe the attached NEAR will be forwarded to the scheduled promise's target). This is a realistic mistake. The entry path requires no special privilege: any unprivileged NEAR account can call `execute_scheduled` with attached NEAR and trigger the lock.

---

### Recommendation

Remove the `#[payable]` attribute from `execute_scheduled`. Since the function does not use `env::attached_deposit()` and the scheduled promise's `attached_balance` is already encoded in the stored `PromiseArgs`, there is no reason for the function to accept attached NEAR. Removing `#[payable]` causes the NEAR SDK to automatically panic if any NEAR is attached, preventing accidental locking.

If there is a future use case requiring attached NEAR, add explicit handling: either forward the deposit to the promise target or refund it to the predecessor.

---

### Proof of Concept

1. The Aurora engine schedules a promise for EVM address `0xABCD…` by calling `schedule` on the router sub-account `abcd….aurora`.
2. An unprivileged NEAR account (e.g., `attacker.near`) calls `execute_scheduled` on `abcd….aurora` with `nonce = 0` and attaches 5 NEAR.
3. The function executes successfully: the stored promise is dispatched with its own `attached_balance` (e.g., 1 yoctoNEAR from `PromiseArgs`).
4. The 5 NEAR attached by the caller is absorbed into `abcd….aurora`'s balance.
5. `send_refund` can only return exactly 2 NEAR to the parent engine, not the absorbed 5 NEAR.
6. The 5 NEAR is permanently locked in the router sub-account with no recovery path for the caller. [1](#0-0) [3](#0-2)

### Citations

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

**File:** etc/xcc-router/src/lib.rs (L160-174)
```rust
    #[payable]
    pub fn deploy_upgrade(&mut self, #[serializer(borsh)] args: DeployUpgradeParams) {
        self.assert_preconditions();

        let promise_id = env::promise_batch_create(&env::current_account_id());
        env::promise_batch_action_deploy_contract(promise_id, &args.code);
        env::promise_batch_action_function_call(
            promise_id,
            INITIALIZE,
            &args.initialize_args,
            NearToken::default(),
            INITIALIZE_GAS,
        );
        env::promise_return(promise_id);
    }
```

**File:** etc/xcc-router/src/lib.rs (L176-184)
```rust
    pub fn send_refund(&self) -> Promise {
        let parent = self.get_parent().unwrap_or_else(env_panic);

        require_caller(&parent)
            .and_then(|_| require_no_failed_promises())
            .unwrap_or_else(env_panic);

        Promise::new(parent).transfer(REFUND_AMOUNT)
    }
```

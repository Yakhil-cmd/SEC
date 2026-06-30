### Title
Unrestricted `execute_scheduled` Allows Any Caller to Prematurely Execute Scheduled XCC Promises, Temporarily Freezing Attached NEAR — (File: `etc/xcc-router/src/lib.rs`)

---

### Summary

The XCC router contract's `execute_scheduled` function is intentionally callable by any account with no access restriction. Because scheduled-promise nonces are publicly logged at scheduling time, an attacker can monitor the chain, identify any pending scheduled promise, and call `execute_scheduled` before the owning user intends to. If the prematurely triggered promise fails (e.g., the target contract is not yet in the expected state), the NEAR attached to that promise is returned to the router contract while the scheduled-promise storage entry is already deleted. The user's NEAR is then temporarily frozen inside the router sub-account with no direct withdrawal path.

---

### Finding Description

`execute_scheduled` in the XCC router contract is marked `#[payable]` and carries an explicit comment stating it is safe to leave open to any caller:

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
``` [1](#0-0) 

The design assumption — "it can only act on promises that were created via `schedule`" — ignores the timing dimension entirely. The `schedule` function is gated to the parent (Aurora Engine) via `assert_preconditions`, so only the engine can write a scheduled promise. However, once written, any external account can trigger execution at any time. [2](#0-1) 

The nonce assigned to each scheduled promise is emitted as a public log:

```rust
near_sdk::log!("Promise scheduled at nonce {}", nonce);
``` [3](#0-2) 

This makes every pending nonce trivially discoverable by any observer.

On the engine side, when a user submits a `CrossContractCallArgs::Delayed` call through the XCC precompile, the engine first unwraps the user's wNEAR into real NEAR and transfers it to the router sub-account via `withdraw_wnear_to_router`. The router then holds that NEAR while the promise sits in `scheduled_promises`. [4](#0-3) [5](#0-4) 

When `execute_scheduled` is called, the entry is **removed from storage before the promise executes**. If the promise subsequently fails (e.g., the target contract is not ready, or gas is insufficient), NEAR protocol returns the attached NEAR to the router contract. Because the storage entry is already gone, the user cannot retry through the normal XCC flow. The router contract exposes no general NEAR withdrawal function; the only recovery path is `send_refund`, which is restricted to the parent (Aurora Engine) and transfers only the fixed `REFUND_AMOUNT` (2 NEAR), not an arbitrary amount. [6](#0-5) 

The attacker bears zero cost beyond NEAR gas fees and holds no tokens at risk — a direct structural parallel to M-30, where the 0.5 ETH `lpTokenETH` balance check was trivially satisfiable and reversible.

---

### Impact Explanation

**High — Temporary freezing of funds.**

Any NEAR that was unwrapped from the user's wNEAR and transferred to the router in preparation for a delayed XCC call can be temporarily frozen inside the router sub-account. The user must schedule a new XCC call (spending additional wNEAR for gas/storage) to recover the stranded NEAR. A persistent attacker can repeat this cycle, continuously disrupting the user's cross-contract operations and keeping their NEAR locked in the router.

---

### Likelihood Explanation

**High.** The attack requires:
- No special privileges or token holdings.
- No capital at risk (only NEAR gas fees, which are negligible).
- Only the ability to read public NEAR chain logs to obtain the nonce.

The nonce is sequential and logged on every `schedule` call, making enumeration trivial. Any unprivileged NEAR account can execute this attack against any router sub-account at any time.

---

### Recommendation

Remove the open-access design of `execute_scheduled`. The simplest fix is to restrict callers to either the parent (Aurora Engine) or the EVM address that owns the router sub-account:

```rust
pub fn execute_scheduled(&mut self, nonce: U64) {
    let parent = self.get_parent().unwrap_or_else(env_panic);
    // Only the parent engine may trigger scheduled execution.
    require_caller(&parent).unwrap_or_else(env_panic);
    ...
}
```

Alternatively, introduce a time-lock: record the block timestamp at scheduling time and reject `execute_scheduled` calls that arrive before the lock expires. This preserves the open-caller design while preventing premature execution.

---

### Proof of Concept

1. **User** calls the Aurora Engine XCC precompile with `CrossContractCallArgs::Delayed(call)` where `call` has NEAR attached (e.g., 5 NEAR to fund a cross-contract call).
2. The engine unwraps the user's wNEAR and transfers 5 NEAR to the router sub-account (`{user_address}.aurora`), then calls `schedule` on the router. The router logs: `"Promise scheduled at nonce 3"`.
3. **Attacker** observes the log, learns nonce = 3.
4. Attacker immediately calls `execute_scheduled(3)` on the router.
5. Inside `execute_scheduled`: the promise is removed from `scheduled_promises` and `promise_create` is called. The target contract is not yet in the expected state, so the promise fails.
6. NEAR protocol returns the 5 NEAR to the router contract. The storage entry for nonce 3 is already deleted.
7. The user's 5 NEAR is now frozen inside the router. The only recovery path is for the user to schedule a new XCC call (requiring additional wNEAR) to transfer the NEAR out — which the attacker can again front-run. [1](#0-0) [4](#0-3) [7](#0-6)

### Citations

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

**File:** engine/src/xcc.rs (L289-330)
```rust
    let withdraw_id = if required_near == ZERO_YOCTO {
        setup_id
    } else {
        let withdraw_call_args = WithdrawWnearToRouterArgs {
            target: sender,
            amount: required_near,
        };
        let withdraw_call = PromiseCreateArgs {
            target_account_id: current_account_id.clone(),
            method: "withdraw_wnear_to_router".into(),
            args: borsh::to_vec(&withdraw_call_args).unwrap(),
            attached_balance: ZERO_YOCTO,
            attached_gas: WITHDRAW_GAS,
        };
        // Safety: This promise is safe. Even though this is a call from the engine account to
        // itself invoking the `call` method (which could be dangerous), the argument to `call`
        // is controlled entirely by us (not any user). This call will only execute the wnear
        // exit precompile, and only for the necessary amount. Note that this amount will always
        // be present, otherwise the user's call to the xcc precompile would have failed.
        let id = match setup_id {
            None => handler.promise_create_call(&withdraw_call),
            Some(setup_id) => handler.promise_attach_callback(setup_id, &withdraw_call),
        };
        let refund_needed = match deploy_needed {
            AddressVersionStatus::DeployNeeded { create_needed } => create_needed,
            AddressVersionStatus::UpToDate => false,
        };
        if refund_needed {
            let refund_call = PromiseCreateArgs {
                target_account_id: promise.target_account_id.clone(),
                method: "send_refund".into(),
                args: Vec::new(),
                attached_balance: ZERO_YOCTO,
                attached_gas: REFUND_GAS,
            };
            // Safety: This call is safe because the router's `send_refund` method
            // does not violate any security invariants. It only sends NEAR back to this contract.
            Some(handler.promise_attach_callback(id, &refund_call))
        } else {
            Some(id)
        }
    };
```

**File:** engine/src/xcc.rs (L382-393)
```rust
pub fn withdraw_wnear_to_router<I: IO + Copy, E: Env, M: ModExpAlgorithm, H: PromiseHandler>(
    recipient: &AccountId,
    amount: Yocto,
    wnear_address: Address,
    engine: &mut Engine<I, E, M>,
    handler: &mut H,
) -> EngineResult<(SubmitResult, Vec<PromiseId>)> {
    let mut interceptor = PromiseInterceptor::new(handler);
    let withdraw_call_args = withdraw_wnear_call_args(recipient, amount, wnear_address);
    let result = engine.call_with_args(withdraw_call_args, &mut interceptor)?;
    Ok((result, interceptor.promises))
}
```

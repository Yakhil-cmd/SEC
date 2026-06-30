### Title
wNEAR Permanently Frozen in Engine's Implicit Address When XCC Promise Chain Fails - (`engine-precompiles/src/xcc.rs`, `engine/src/xcc.rs`)

---

### Summary

When a user invokes the Cross-Contract Call (XCC) precompile, the precompile immediately and irrevocably transfers the user's wNEAR to the engine's implicit EVM address within the same EVM execution context. The subsequent NEAR async promise chain (`withdraw_wnear_to_router` → router `execute`) is scheduled separately. If any step in that promise chain fails, the wNEAR transferred from the user is permanently frozen in the engine's implicit EVM address with no refund path.

---

### Finding Description

**Step 1 — EVM-level wNEAR transfer (committed atomically with the EVM transaction)**

In `engine-precompiles/src/xcc.rs`, when `required_near > 0`, the precompile calls `transferFrom` on the wNEAR ERC-20 contract to move the user's wNEAR to the engine's implicit address: [1](#0-0) 

`required_near` is computed as: [2](#0-1) 

For a first-time XCC user (no router deployed), `required_near = attached_near + STORAGE_AMOUNT` (2 NEAR). This EVM state change is committed as part of the `submit` NEAR transaction and is **not reversible**.

**Step 2 — NEAR async promise chain (runs in separate NEAR transactions)**

`handle_precompile_promise` in `engine/src/xcc.rs` constructs the following async chain:

1. (If new router) Deploy batch → `factory_update_address_version` callback
2. `withdraw_wnear_to_router` → (if new router) `send_refund` callback
3. Router's `execute` (user's actual cross-contract call) [3](#0-2) 

**Step 3 — Failure propagation with no refund**

`withdraw_wnear_to_router` in `engine/src/contract_methods/xcc.rs` explicitly checks whether the previous promise (the deploy batch) failed and returns an error if so: [4](#0-3) 

When `withdraw_wnear_to_router` fails (either because the deploy batch failed, or because the wNEAR `withdrawToNear` EVM call itself fails), the NEAR runtime marks it as a failed promise. All subsequent callbacks (`send_refund`, router `execute`) also fail because they check `require_no_failed_promises()`: [5](#0-4) 

**The `send_refund` only returns the 2 NEAR storage-staking amount back to the engine — it does not refund the user's wNEAR.** There is no callback anywhere in the chain that returns the wNEAR from the engine's implicit EVM address back to the user: [6](#0-5) 

The engine's implicit EVM address (`near_account_to_evm_address(engine_account_id)`) is not controlled by any user or admin key. No contract method exists to recover wNEAR stranded there. The funds are permanently frozen.

---

### Impact Explanation

**Critical — Permanent freezing of user funds.**

The user's wNEAR (up to `attached_near + STORAGE_AMOUNT`) is irrevocably transferred to the engine's implicit EVM address during the EVM execution phase. If the NEAR async promise chain fails for any reason, there is no code path that returns this wNEAR to the user. The engine's implicit address is not user-accessible and has no admin recovery function. The frozen amount equals whatever `required_near` was charged — for a new router user this is `attached_near + 2 NEAR`.

---

### Likelihood Explanation

**Medium.** The promise chain can fail due to:

1. **Router deploy batch failure**: The batch includes `CreateAccount`, `Transfer`, `DeployContract`, and `FunctionCall("initialize")`. Any of these can fail due to gas exhaustion in the NEAR callback, a NEAR runtime error, or the `initialize` call panicking. This is the highest-impact case because `required_near = attached_near + STORAGE_AMOUNT`.

2. **`withdraw_wnear_to_router` EVM call failure**: The inner `engine.call_with_args` call executes the wNEAR ERC-20 `withdrawToNear` function. If the wNEAR contract is paused, has a bug, or the EVM execution runs out of gas, `result.status.is_ok()` is false and the method returns `ERR_WITHDRAW_FAILED`. [7](#0-6) 

Both failure modes are reachable by an unprivileged user simply by calling the XCC precompile under adverse conditions (e.g., gas-constrained environment, wNEAR contract issues).

---

### Recommendation

Attach a failure-handling callback after `withdraw_wnear_to_router` that, on failure, calls back into the engine to re-credit the user's wNEAR EVM balance (analogous to the `error_refund` feature already present in the `ExitToNear` precompile path): [8](#0-7) 

Specifically:
- Add a `xcc_refund_on_error` engine method that mints wNEAR back to the original sender's EVM address.
- Attach it as a callback to the `withdraw_wnear_to_router` promise (checking `promise_result_check() == Some(false)`).
- Pass the original sender address and `required_near` amount as arguments to this callback via the promise args.

---

### Proof of Concept

1. Alice (EVM address `0xAlice`) calls the XCC precompile for the first time (no router deployed). She attaches 1 wNEAR to her cross-contract call. `required_near = 1 wNEAR + 2 wNEAR (STORAGE_AMOUNT) = 3 wNEAR`.

2. The XCC precompile executes `transferFrom(0xAlice, engine_implicit_address, 3e24)` on the wNEAR ERC-20 contract. This is committed as part of the `submit` NEAR transaction. Alice's wNEAR balance decreases by 3 wNEAR.

3. The NEAR async promise chain is scheduled: deploy batch → `factory_update_address_version` → `withdraw_wnear_to_router` → `send_refund` → router `execute`.

4. The deploy batch fails (e.g., gas exhaustion in the `initialize` callback, or a NEAR runtime error during `DeployContract`).

5. `withdraw_wnear_to_router` is called as a callback, sees `promise_result_check() == Some(false)`, and returns `ERR_CALLBACK_OF_FAILED_PROMISE`. It panics.

6. `send_refund` is called as a callback, sees a failed promise via `require_no_failed_promises()`, and panics.

7. Router `execute` is called as a callback, sees a failed promise, and panics.

8. Alice's 3 wNEAR sits permanently in the engine's implicit EVM address. No code path exists to return it. Alice has lost 3 wNEAR permanently.

### Citations

**File:** engine-precompiles/src/xcc.rs (L177-182)
```rust
        let required_near =
            match state::get_code_version_of_address(&self.io, &Address::new(sender)) {
                // If there is no deployed version of the router contract then we need to charge for storage staking
                None => attached_near + state::STORAGE_AMOUNT,
                Some(_) => attached_near,
            };
```

**File:** engine-precompiles/src/xcc.rs (L184-216)
```rust
        if required_near != ZERO_YOCTO {
            let engine_implicit_address = aurora_engine_sdk::types::near_account_to_evm_address(
                self.engine_account_id.as_bytes(),
            );
            let tx_data = transfer_from_args(
                sender.0.into(),
                engine_implicit_address.raw().0.into(),
                required_near.as_u128().into(),
            );
            let wnear_address = state::get_wnear_address(&self.io);
            let context = aurora_evm::Context {
                address: wnear_address.raw(),
                caller: cross_contract_call::ADDRESS.raw(),
                apparent_value: U256::zero(),
            };
            let (exit_reason, return_value) =
                handle.call(wnear_address.raw(), None, tx_data, None, false, &context);
            match exit_reason {
                // Transfer successful, nothing to do
                aurora_evm::ExitReason::Succeed(_) => (),
                aurora_evm::ExitReason::Revert(r) => {
                    return Err(PrecompileFailure::Revert {
                        exit_status: r,
                        output: return_value,
                    });
                }
                aurora_evm::ExitReason::Error(e) => {
                    return Err(PrecompileFailure::Error { exit_status: e });
                }
                aurora_evm::ExitReason::Fatal(f) => {
                    return Err(PrecompileFailure::Fatal { exit_status: f });
                }
            }
```

**File:** engine/src/xcc.rs (L289-340)
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
    // 3. Finally we can do the call the user wanted to do.

    // Safety: this call is safe because the promise comes from the XCC precompile, not the
    // user directly. The XCC precompile will only construct promises that target the `execute`
    // and `schedule` methods of the user's router contract. Therefore, the user cannot have
    // the engine make arbitrary calls.
    match withdraw_id {
        None => handler.promise_create_call(promise),
        Some(withdraw_id) => handler.promise_attach_callback(withdraw_id, promise),
    }
```

**File:** engine/src/contract_methods/xcc.rs (L33-35)
```rust
        if matches!(handler.promise_result_check(), Some(false)) {
            return Err(b"ERR_CALLBACK_OF_FAILED_PROMISE".into());
        }
```

**File:** engine/src/contract_methods/xcc.rs (L58-60)
```rust
        if !result.status.is_ok() {
            return Err(b"ERR_WITHDRAW_FAILED".into());
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

**File:** etc/xcc-router/src/lib.rs (L382-394)
```rust
fn require_no_failed_promises() -> Result<(), Error> {
    let num_promises = env::promise_results_count();
    for index in 0..num_promises {
        // We can use deprecated `promise_result` rather than `promise_result_checked` safely here,
        // because the promise result could be received from the Aurora Engine itself,
        // and we can be sure that the len of the promise result is within bounds.
        #[allow(deprecated)]
        if env::promise_result(index) == PromiseResult::Failed {
            return Err(Error::CallbackOfFailedPromise);
        }
    }
    Ok(())
}
```

**File:** engine/src/contract_methods/connector.rs (L231-239)
```rust
        } else if let Some(args) = args.refund {
            // Exit call failed; need to refund tokens
            let refund_result = engine::refund_on_error(io, env, state, &args, handler)?;

            if !refund_result.status.is_ok() {
                return Err(errors::ERR_REFUND_FAILURE.into());
            }

            Some(refund_result)
```

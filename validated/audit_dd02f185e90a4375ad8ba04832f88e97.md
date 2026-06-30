### Title
NEAR Permanently Locked in XCC Router After Failed Cross-Contract Call - (File: `engine/src/xcc.rs`)

### Summary
When a user makes an XCC call with attached NEAR (via the XCC precompile), the engine burns the user's wNEAR ERC-20 tokens and sends the equivalent NEAR to the router sub-account. If the downstream XCC call fails, NEAR is returned to the router by the NEAR runtime, but the router has no mechanism to return it to the user. The user's wNEAR is already permanently burned, and the NEAR is permanently locked in the router.

### Finding Description

The XCC flow in `handle_precompile_promise` (`engine/src/xcc.rs`, lines 179–341) proceeds as follows when `required_near > 0`:

**Step 1 – wNEAR is burned, NEAR sent to router:**
`withdraw_wnear_to_router` (`engine/src/xcc.rs`, lines 382–393) calls the wNEAR ERC-20 contract's `withdrawToNear` function via the EVM engine. This burns the user's wNEAR ERC-20 balance and sends the equivalent NEAR to the router sub-account. This is an irreversible EVM state change. [1](#0-0) 

**Step 2 – Router executes the XCC call using its NEAR balance:**
The engine calls `execute` on the router (`etc/xcc-router/src/lib.rs`, lines 128–133), which creates a promise to the target contract with the user-specified `attached_balance` drawn from the router's own NEAR balance (funded in Step 1). [2](#0-1) 

**Step 3 – XCC call fails, NEAR returned to router:**
Per NEAR protocol semantics, if the target function call fails, the attached deposit is returned to the predecessor — the router. The router now holds excess NEAR.

**Step 4 – No recovery path exists:**
The router's only outbound NEAR transfer is `send_refund`, which hardcodes `REFUND_AMOUNT = NearToken::from_near(2)` (the storage staking amount) and can only be called by the parent engine: [3](#0-2) [4](#0-3) 

There is no general withdrawal function in the router. The user cannot call the router directly — `execute` and `schedule` are restricted to the parent engine: [5](#0-4) 

The engine itself has no callback attached to the final `execute` promise to detect failure and refund the user: [6](#0-5) 

Furthermore, on subsequent XCC calls, the engine always calls `withdraw_wnear_to_router` afresh — it never checks whether the router already holds sufficient NEAR balance — so the stranded NEAR is never consumed: [7](#0-6) 

### Impact Explanation
**Critical — Permanent freezing of funds.** The user's wNEAR ERC-20 tokens are burned (EVM state is committed) before the XCC call is dispatched. If the XCC call fails, the equivalent NEAR is returned to the router sub-account and has no reachable withdrawal path accessible to the user. The funds are locked indefinitely without admin intervention (router upgrade or account deletion).

### Likelihood Explanation
**Medium.** XCC calls to arbitrary NEAR contracts can fail for many ordinary reasons: the target contract panics, runs out of gas, or rejects the call. Any user who attaches NEAR to an XCC call that subsequently fails will lose those funds permanently. This is a normal operational scenario, not an edge case.

### Recommendation
1. Attach a failure-handling callback to the final `execute` promise in `handle_precompile_promise`. If the XCC call fails, the callback should re-mint the equivalent wNEAR to the user's EVM address (analogous to the `exit_to_near_precompile_callback` refund path in `engine/src/contract_methods/connector.rs`, lines 196–246).
2. Alternatively, add a `withdraw_near(amount, recipient)` function to the router callable only by the parent engine, and invoke it in the failure callback to return stranded NEAR. [8](#0-7) 

### Proof of Concept

1. User holds 1 wNEAR ERC-20 on Aurora.
2. User submits an EVM transaction invoking the XCC precompile, specifying a promise to call `some_method` on `target.near` with `attached_balance = 1 NEAR`.
3. Engine executes `handle_precompile_promise` with `required_near = 1 NEAR`.
4. Engine calls `withdraw_wnear_to_router` → EVM burns 1 wNEAR from user's balance; 1 NEAR is sent to `{user_address}.aurora`.
5. Engine calls `execute` on `{user_address}.aurora` with the user's promise (0 NEAR attached to this call).
6. Router calls `target.near::some_method` with 1 NEAR attached from its own balance.
7. `target.near::some_method` panics (e.g., assertion failure).
8. NEAR runtime returns 1 NEAR to `{user_address}.aurora`.
9. User's wNEAR is gone (burned in step 4). The 1 NEAR sits in `{user_address}.aurora` with no user-accessible withdrawal path. `send_refund` only returns 2 NEAR (storage staking), not the user's 1 NEAR. The engine attaches no failure callback. The NEAR is permanently locked. [9](#0-8) [1](#0-0) [3](#0-2)

### Citations

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

**File:** engine/src/xcc.rs (L337-340)
```rust
    match withdraw_id {
        None => handler.promise_create_call(promise),
        Some(withdraw_id) => handler.promise_attach_callback(withdraw_id, promise),
    }
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

**File:** etc/xcc-router/src/lib.rs (L40-40)
```rust
const REFUND_AMOUNT: NearToken = NearToken::from_near(2);
```

**File:** etc/xcc-router/src/lib.rs (L123-133)
```rust
    /// This function can only be called by the parent account (i.e. Aurora engine) to ensure that
    /// no one can create calls on behalf of the user this router contract is deployed for.
    /// The engine only calls this function when the special precompile in the EVM for NEAR cross
    /// contract calls is used by the address associated with the sub-account this router contract
    /// is deployed at.
    pub fn execute(&self, #[serializer(borsh)] promise: PromiseArgs) {
        self.assert_preconditions();

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

**File:** engine/src/contract_methods/connector.rs (L196-246)
```rust
pub fn exit_to_near_precompile_callback<I: IO + Copy, E: Env, H: PromiseHandler>(
    io: I,
    env: &E,
    handler: &mut H,
) -> Result<Option<SubmitResult>, ContractError> {
    with_hashchain(io, env, function_name!(), |io| {
        let state = state::get_state(&io)?;
        require_running(&state)?;
        env.assert_private_call()?;

        // This function should only be called as the callback of
        // exactly one promise.
        if handler.promise_results_count() != 1 {
            return Err(errors::ERR_PROMISE_COUNT.into());
        }

        let args: ExitToNearPrecompileCallbackArgs = io.read_input_borsh()?;

        let maybe_result = if let Some(PromiseResult::Successful(_)) = handler.promise_result(0) {
            if let Some(args) = args.transfer_near {
                let action = PromiseAction::Transfer {
                    amount: Yocto::new(args.amount),
                };
                let promise = PromiseBatchAction {
                    target_account_id: args.target_account_id,
                    actions: vec![action],
                };

                // Safety: this call is safe because it comes from the exit to near precompile, not users.
                // The call is to transfer the unwrapped wNEAR tokens.
                let promise_id = handler.promise_create_batch(&promise);
                handler.promise_return(promise_id);
            }

            None
        } else if let Some(args) = args.refund {
            // Exit call failed; need to refund tokens
            let refund_result = engine::refund_on_error(io, env, state, &args, handler)?;

            if !refund_result.status.is_ok() {
                return Err(errors::ERR_REFUND_FAILURE.into());
            }

            Some(refund_result)
        } else {
            None
        };

        Ok(maybe_result)
    })
}
```

### Title
wNEAR ERC-20 Balance Permanently Deducted When XCC NEAR Promise Chain Fails - (File: `engine-precompiles/src/xcc.rs`, `engine/src/xcc.rs`)

### Summary

The XCC precompile deducts wNEAR from the user's EVM balance as a committed EVM state change during EVM execution. If the subsequent NEAR promise chain (router deployment → `withdraw_wnear_to_router` → `execute`) fails, the wNEAR is not refunded and becomes permanently stuck at the engine's implicit EVM address. The user's intended cross-contract call never executes, and their funds are frozen.

### Finding Description

The XCC precompile (`engine-precompiles/src/xcc.rs`) operates in two distinct, non-atomic phases:

**Phase 1 – EVM execution (committed atomically):**
When `required_near > 0`, the precompile calls `handle.call()` to execute a wNEAR ERC-20 `transferFrom`, moving the user's wNEAR to the engine's implicit EVM address (`near_account_to_evm_address(engine_account_id)`). This EVM state change is committed as part of the EVM transaction. [1](#0-0) 

**Phase 2 – NEAR promise scheduling (not atomic with Phase 1):**
After EVM execution completes, `filter_promises_from_logs` calls `handle_precompile_promise`, which schedules a chain of NEAR promises:
1. Router deployment batch (if needed)
2. `factory_update_address_version` callback
3. `withdraw_wnear_to_router` (burns wNEAR, sends NEAR to router)
4. `send_refund` (if new router created)
5. Router's `execute` (user's actual XCC call) [2](#0-1) [3](#0-2) 

**The gap:** `withdraw_wnear_to_router` explicitly checks whether the prior promise succeeded and returns an error if it failed — but this only halts further execution; it does **not** refund the wNEAR to the user. [4](#0-3) 

When `withdraw_wnear_to_router` returns an error, the wNEAR remains at the engine's implicit EVM address. The router's `execute` is still called as a callback but fails immediately via `require_no_failed_promises()`. [5](#0-4) 

There is no automatic refund path in the XCC flow analogous to the `exit_to_near_precompile_callback` refund mechanism that exists for the exit-to-NEAR precompile. [6](#0-5) 

### Impact Explanation

The user's wNEAR is moved to the engine's implicit EVM address (`near_account_to_evm_address(engine_account_id.as_bytes())`) during EVM execution. This address has no private key and cannot initiate EVM calls autonomously. Recovering the stuck wNEAR requires admin intervention via a custom `call` transaction — there is no automatic recovery. The user's XCC call never executes. This constitutes **permanent freezing of user funds** (Critical) absent an out-of-band admin action.

### Likelihood Explanation

The promise chain can fail in realistic conditions:
- **Gas exhaustion** during router deployment: the gas budget is estimated via `ROUTER_EXEC_BASE + ROUTER_EXEC_PER_CALLBACK * callback_count` but is not guaranteed to cover all sub-promise execution.
- **Router `initialize` failure**: if the wNEAR account is not registered or the wnear account lookup fails.
- **Account creation conflict**: if the sub-account already exists in an unexpected state.

The vulnerability is triggered on any user's **first XCC call** (when `create_needed = true`) with `attached_near > 0`, which is the standard usage pattern. Likelihood is low-to-medium but the impact is severe. [7](#0-6) [8](#0-7) 

### Recommendation

Add a failure-recovery callback at the end of the XCC promise chain. If any promise in the chain fails, the callback should transfer the wNEAR back from the engine's implicit address to the original caller's EVM address. This mirrors the existing `exit_to_near_precompile_callback` refund pattern. Specifically:

1. Record the original caller's EVM address and the `required_near` amount in engine storage before scheduling the promise chain.
2. Attach a final callback that checks `promise_result_check()` and, on failure, calls the wNEAR ERC-20 `transfer` from the engine's implicit address back to the user.

### Proof of Concept

1. User (EVM address `A`) calls the XCC precompile for the first time with `attached_near = 1 NEAR` worth of wNEAR.
2. **EVM phase**: `transferFrom(A, engine_implicit_addr, required_near)` executes on wNEAR ERC-20. User's wNEAR balance decreases. EVM transaction succeeds and state is committed.
3. **NEAR phase**: `handle_precompile_promise` schedules: `[CreateAccount+Transfer+Deploy+Initialize] → factory_update_address_version → withdraw_wnear_to_router → send_refund → execute`.
4. The router deployment batch fails (e.g., gas exhaustion during `initialize`).
5. `factory_update_address_version` is called as a callback with a failed promise result → returns error.
6. `withdraw_wnear_to_router` is called → `promise_result_check()` returns `Some(false)` → returns `ERR_CALLBACK_OF_FAILED_PROMISE` without burning wNEAR.
7. `execute` is called → `require_no_failed_promises()` panics → XCC call never happens.
8. **Result**: User's wNEAR is permanently stuck at `engine_implicit_addr` in the EVM. User's XCC call never executed. No refund issued. [9](#0-8) [10](#0-9)

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

**File:** engine/src/engine.rs (L1692-1717)
```rust
            } else if log.address == cross_contract_call::ADDRESS.raw() {
                if log.topics[0] == cross_contract_call::AMOUNT_TOPIC {
                    // NEAR balances are 128-bit, so the leading 16 bytes of the 256-bit topic
                    // value should always be zero.
                    assert_eq!(&log.topics[1].as_bytes()[0..16], &[0; 16]);
                    let required_near =
                        Yocto::new(U256::from_big_endian(log.topics[1].as_bytes()).low_u128());
                    if let Ok(promise) = PromiseCreateArgs::try_from_slice(&log.data) {
                        let id = crate::xcc::handle_precompile_promise(
                            io,
                            handler,
                            previous_promise,
                            &promise,
                            required_near,
                            current_account_id,
                        );
                        previous_promise = Some(id);
                    }
                }
                // do not pass on these "internal logs" to the caller
                None
            } else {
                Some(evm_log_to_result_log(log))
            }
        })
        .collect()
```

**File:** engine/src/xcc.rs (L178-341)
```rust
#[allow(clippy::too_many_lines)]
pub fn handle_precompile_promise<I, P>(
    io: &I,
    handler: &mut P,
    base_id: Option<PromiseId>,
    promise: &PromiseCreateArgs,
    required_near: Yocto,
    current_account_id: &AccountId,
) -> PromiseId
where
    P: PromiseHandler,
    I: IO + Copy,
{
    let target_account: &str = promise.target_account_id.as_ref();
    let sender = Address::decode(&target_account[0..40]).expect(ERR_INVALID_ACCOUNT);

    // Confirm target_account is of the form `{address}.{aurora}`
    // Address prefix parsed above, so only need to check `.{aurora}`
    assert_eq!(&target_account[40..41], ".", "{ERR_INVALID_ACCOUNT}");
    assert_eq!(
        &target_account[41..],
        current_account_id.as_ref(),
        "{ERR_INVALID_ACCOUNT}"
    );
    // Confirm there is 0 NEAR attached to the promise
    // (the precompile should not drain the engine's balance).
    assert_eq!(promise.attached_balance, ZERO_YOCTO, "{ERR_ATTACHED_NEAR}");

    let latest_code_version = get_latest_code_version(io);
    let sender_code_version = get_code_version_of_address(io, &sender);
    let deploy_needed = AddressVersionStatus::new(io, latest_code_version, sender_code_version);
    // 1. If the router contract account does not exist or is out of date then we start
    //    with a batch transaction to deploy the router. This batch also has an attached
    //    callback to update the engine's storage with the new version of that router account.
    let setup_id = match &deploy_needed {
        AddressVersionStatus::DeployNeeded { create_needed } => {
            let mut promise_actions = Vec::with_capacity(4);
            let code = get_router_code(io).0.into_owned();
            // After the deployment we will call the contract's initialize function
            let wnear_address = get_wnear_address(io);
            let wnear_account = crate::engine::nep141_erc20_map(*io)
                .lookup_right(&crate::engine::ERC20Address(wnear_address))
                .expect("wnear account not found");
            let init_args = format!(
                r#"{{"wnear_account": "{}", "must_register": {}}}"#,
                wnear_account.0.as_ref(),
                create_needed,
            );
            if *create_needed {
                promise_actions.push(PromiseAction::CreateAccount);
                promise_actions.push(PromiseAction::Transfer {
                    amount: STORAGE_AMOUNT,
                });
                promise_actions.push(PromiseAction::DeployContract { code });
                promise_actions.push(PromiseAction::FunctionCall {
                    name: "initialize".into(),
                    args: init_args.into_bytes(),
                    attached_yocto: ZERO_YOCTO,
                    gas: INITIALIZE_GAS,
                });
            } else {
                let deploy_args = DeployUpgradeParams {
                    code,
                    initialize_args: init_args.into_bytes(),
                };
                promise_actions.push(PromiseAction::FunctionCall {
                    name: "deploy_upgrade".into(),
                    args: borsh::to_vec(&deploy_args).expect(ERR_UPGRADE_ARG_SERIALIZATION),
                    attached_yocto: ZERO_YOCTO,
                    gas: UPGRADE_GAS + INITIALIZE_GAS,
                });
            }

            let batch = PromiseBatchAction {
                target_account_id: promise.target_account_id.clone(),
                actions: promise_actions,
            };
            // Safety: This batch creation is safe because it only acts on the router sub-account
            // (not the main engine account), and the actions performed are only (1) create it
            // for the first time and/or (2) deploy the code from our storage (i.e. the deployed
            // code is controlled by us, not the user).
            let promise_id = match base_id {
                Some(id) => handler.promise_attach_batch_callback(id, &batch),
                None => handler.promise_create_batch(&batch),
            };
            // Add a callback here to update the version of the account
            let args = AddressVersionUpdateArgs {
                address: sender,
                version: latest_code_version,
            };
            let callback = PromiseCreateArgs {
                target_account_id: current_account_id.clone(),
                method: "factory_update_address_version".into(),
                args: borsh::to_vec(&args).unwrap(),
                attached_balance: ZERO_YOCTO,
                attached_gas: VERSION_UPDATE_GAS,
            };

            // Safety: A call from the engine to the engine's `factory_update_address_version`
            // method is safe because that method only writes the specific router sub-account
            // metadata that has just been deployed above.
            Some(handler.promise_attach_callback(promise_id, &callback))
        }
        AddressVersionStatus::UpToDate => base_id,
    };
    // 2. If some NEAR is required for this call (from storage staking for a new account
    //    and/or attached NEAR to the call the user wants to make), then we need to have the
    //    engine withdraw that amount of wNEAR to the router account and then have the router
    //    unwrap it into actual NEAR. In the case of storage staking, the engine contract
    //    covered the cost initially (see setup batch above), so the unwrapping also sends
    //    a refund back to the engine.
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
}
```

**File:** engine/src/contract_methods/xcc.rs (L23-65)
```rust
#[named]
pub fn withdraw_wnear_to_router<I: IO + Copy, E: Env, H: PromiseHandler>(
    io: I,
    env: &E,
    handler: &mut H,
) -> Result<SubmitResult, ContractError> {
    with_logs_hashchain(io, env, function_name!(), |io| {
        let state = state::get_state(&io)?;
        require_running(&state)?;
        env.assert_private_call()?;
        if matches!(handler.promise_result_check(), Some(false)) {
            return Err(b"ERR_CALLBACK_OF_FAILED_PROMISE".into());
        }
        let args: WithdrawWnearToRouterArgs = io.read_input_borsh()?;
        let current_account_id = env.current_account_id();
        let recipient = AccountId::try_from(format!(
            "{}.{}",
            args.target.encode(),
            current_account_id.as_ref()
        ))?;
        let wnear_address = aurora_engine_precompiles::xcc::state::get_wnear_address(&io);
        let mut engine: Engine<_, E, AuroraModExp> = Engine::new_with_state(
            state,
            predecessor_address(&current_account_id),
            current_account_id,
            io,
            env,
        );
        let (result, ids) = xcc::withdraw_wnear_to_router(
            &recipient,
            args.amount,
            wnear_address,
            &mut engine,
            handler,
        )?;
        if !result.status.is_ok() {
            return Err(b"ERR_WITHDRAW_FAILED".into());
        }
        let id = ids.last().ok_or(b"ERR_NO_PROMISE_CREATED")?;
        handler.promise_return(*id);
        Ok(result)
    })
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

### Title
wNEAR Permanently Frozen in Engine Implicit Address When XCC Router Deployment Fails - (File: engine-precompiles/src/xcc.rs)

### Summary

The XCC precompile commits an EVM-level wNEAR `transferFrom` (user → engine implicit address) synchronously during `submit`, then schedules an asynchronous NEAR promise chain to deploy the router and withdraw the wNEAR to it. If any step in that promise chain fails, `withdraw_wnear_to_router` returns early without executing the wNEAR withdrawal, leaving the user's wNEAR permanently frozen in the engine's implicit EVM address with no user-accessible recovery path.

### Finding Description

**Step 1 – EVM state change committed synchronously.**

In `engine-precompiles/src/xcc.rs`, when `required_near != ZERO_YOCTO`, the precompile calls the wNEAR ERC-20's `transferFrom` inside the EVM execution, moving the user's wNEAR to the engine's implicit address: [1](#0-0) 

This EVM state change is committed as part of the `submit` receipt. It is not rolled back regardless of what happens to the subsequent NEAR promise chain.

**Step 2 – Asynchronous promise chain is constructed.**

In `engine/src/xcc.rs`, `handle_precompile_promise` builds the following callback chain when a new router must be deployed (`create_needed = true`) and `required_near > 0`:

```
Deploy batch → factory_update_address_version → withdraw_wnear_to_router → execute/schedule
``` [2](#0-1) 

**Step 3 – `withdraw_wnear_to_router` aborts on any prior failure.**

In `engine/src/contract_methods/xcc.rs`, the `withdraw_wnear_to_router` callback checks whether the preceding promise (i.e., `factory_update_address_version`) succeeded: [3](#0-2) 

If the deploy batch failed (e.g., insufficient gas, account-creation collision, or `initialize` panic), `factory_update_address_version` itself returns `ERR_ROUTER_DEPLOY_FAILED`: [4](#0-3) 

This causes `withdraw_wnear_to_router` to return `ERR_CALLBACK_OF_FAILED_PROMISE` without ever calling the wNEAR ERC-20's `withdrawToNear`. The wNEAR that was transferred to the engine's implicit address in Step 1 is never moved to the router and is never returned to the user.

**Step 4 – No recovery mechanism exists.**

The engine's implicit address (`near_account_to_evm_address(engine_account_id.as_bytes())`) is not controlled by any user private key. There is no engine entrypoint that allows a user to reclaim wNEAR stranded in that address. The only theoretical recovery would require the engine owner to manually craft a `call` transaction — an out-of-band, privileged action that is not part of the protocol.

### Impact Explanation

**Critical – Permanent freezing of funds.**

The user's wNEAR (representing real NEAR tokens) is irreversibly transferred to the engine's implicit EVM address during the `submit` call. If the router deployment fails for any reason, those tokens are frozen forever. The amount at risk is at minimum `STORAGE_AMOUNT` (2 NEAR) plus any `attached_near` the user specified for the cross-contract call. [5](#0-4) [6](#0-5) 

### Likelihood Explanation

Any first-time XCC user whose router has never been deployed is exposed. The deploy batch includes four sequential actions (CreateAccount, Transfer, DeployContract, FunctionCall("initialize")), each consuming NEAR gas. If the total gas attached to the outer `submit` transaction is insufficient to cover the full chain — deploy batch + `factory_update_address_version` + `withdraw_wnear_to_router` + `execute`/`schedule` — the deploy batch will fail mid-chain. The fixed gas constants used: [7](#0-6) 

are estimates; real execution costs can exceed them. Additionally, if the wNEAR contract's `storage_deposit` (called during `initialize`) fails, the entire batch fails. Both scenarios are reachable by an ordinary unprivileged EVM user simply by submitting an XCC transaction with insufficient gas or targeting a wNEAR contract in an unexpected state.

### Recommendation

Implement a fallback recovery mechanism analogous to the `error_refund` feature used in the `ExitToNear` precompile. Specifically:

1. In `withdraw_wnear_to_router`, when the prior promise has failed, instead of returning an error, schedule a refund EVM call that transfers the stranded wNEAR from the engine's implicit address back to the original sender's EVM address.
2. Alternatively, record the pending wNEAR amount in engine storage at the time of the `transferFrom` and expose a user-callable `reclaim_xcc_wnear` entrypoint that refunds it if the promise chain never completed successfully.

The existing `exit_to_near_precompile_callback` refund pattern demonstrates the correct approach: [8](#0-7) 

### Proof of Concept

1. Alice holds 10 wNEAR in her EVM address on Aurora. She has never used XCC before (no router deployed).
2. Alice submits an EVM transaction calling the XCC precompile (`0x516cded1...`) with `CrossContractCallArgs::Eager(...)` and `attached_near = 1 NEAR`. The precompile computes `required_near = 1 NEAR + 2 NEAR (STORAGE_AMOUNT) = 3 NEAR`.
3. The precompile calls `wNEAR.transferFrom(alice, engine_implicit_address, 3e24)` inside the EVM. Alice's wNEAR balance drops by 3 NEAR. This EVM state change is committed.
4. The engine schedules the promise chain: deploy batch → `factory_update_address_version` → `withdraw_wnear_to_router` → `execute`.
5. The deploy batch runs out of gas (or `initialize` panics). The batch receipt is marked Failed.
6. `factory_update_address_version` runs as a callback, sees `promise_result_check() == Some(false)`, returns `ERR_ROUTER_DEPLOY_FAILED`. Its receipt is marked Failed.
7. `withdraw_wnear_to_router` runs as a callback, sees `promise_result_check() == Some(false)`, returns `ERR_CALLBACK_OF_FAILED_PROMISE` **without calling `withdrawToNear`**. The 3 wNEAR remain in the engine's implicit EVM address.
8. `execute` on the router also fails.
9. Alice's 3 wNEAR are permanently frozen. She has no entrypoint to recover them. [9](#0-8) [10](#0-9) [11](#0-10)

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

**File:** engine-precompiles/src/xcc.rs (L183-217)
```rust
        // if some NEAR payment is needed, transfer it from the caller to the engine's implicit address
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
        }
```

**File:** engine-precompiles/src/xcc.rs (L241-255)
```rust
pub mod state {
    //! Functions for reading state related to the cross-contract call feature

    use aurora_engine_sdk::error::ReadU32Error;
    use aurora_engine_sdk::io::{IO, StorageIntermediate};
    use aurora_engine_types::parameters::xcc::CodeVersion;
    use aurora_engine_types::storage::{self, KeyPrefix};
    use aurora_engine_types::types::{Address, Yocto};

    pub const ERR_CORRUPTED_STORAGE: &str = "ERR_CORRUPTED_XCC_STORAGE";
    pub const ERR_MISSING_WNEAR_ADDRESS: &str = "ERR_MISSING_WNEAR_ADDRESS";
    pub const VERSION_KEY: &[u8] = b"version";
    pub const WNEAR_KEY: &[u8] = b"wnear";
    /// Amount of NEAR needed to cover storage for a router contract.
    pub const STORAGE_AMOUNT: Yocto = Yocto::new(2_000_000_000_000_000_000_000_000);
```

**File:** engine/src/xcc.rs (L25-29)
```rust
pub const VERSION_UPDATE_GAS: NearGas = NearGas::new(5_000_000_000_000);
pub const INITIALIZE_GAS: NearGas = NearGas::new(15_000_000_000_000);
pub const UPGRADE_GAS: NearGas = NearGas::new(20_000_000_000_000);
pub const REFUND_GAS: NearGas = NearGas::new(5_000_000_000_000);
pub const WITHDRAW_GAS: NearGas = NearGas::new(40_000_000_000_000);
```

**File:** engine/src/xcc.rs (L209-341)
```rust
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

**File:** engine/src/contract_methods/xcc.rs (L90-95)
```rust
        let check_deploy: Result<(), &[u8]> = match handler.promise_result_check() {
            Some(true) => Ok(()),
            Some(false) => Err(b"ERR_ROUTER_DEPLOY_FAILED"),
            None => Err(b"ERR_ROUTER_UPDATE_NOT_CALLBACK"),
        };
        check_deploy?;
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

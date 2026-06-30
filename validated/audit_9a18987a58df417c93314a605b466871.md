### Title
Missing Incentive to Execute Scheduled XCC Promises Causes Permanent Freezing of User NEAR Funds - (File: etc/xcc-router/src/lib.rs)

### Summary

When an EVM user schedules a cross-contract call (XCC) using `CrossContractCallArgs::Delayed`, the Aurora Engine moves the user's wNEAR into the router sub-account as real NEAR before storing the promise. A separate call to `execute_scheduled` is required to actually dispatch the promise. No incentive exists for any party to make this call, and there is no cancellation or withdrawal path in the router contract. If `execute_scheduled` is never called, the NEAR transferred to the router is permanently frozen.

### Finding Description

The XCC subsystem supports two dispatch modes. In the `Eager` mode (`CrossContractCallArgs::Eager`), the promise is executed atomically within the same NEAR transaction. In the `Delayed` mode (`CrossContractCallArgs::Delayed`), the promise is only stored in the router's `scheduled_promises` map and must be dispatched later by a separate call to `execute_scheduled`. [1](#0-0) 

When the user's EVM transaction invokes the XCC precompile with `Delayed` args and the promise requires attached NEAR (e.g., for storage staking on a new account or for the call itself), the precompile first performs a `transferFrom` on the wNEAR ERC-20 to move the required amount from the user to the engine's implicit address. [2](#0-1) [3](#0-2) 

The engine then builds a promise chain that calls `withdraw_wnear_to_router` to unwrap the wNEAR into real NEAR and transfer it to the router sub-account, followed by a call to `schedule` on the router to store the promise. [4](#0-3) 

After this chain completes, the NEAR is sitting in the router contract and the promise is stored in `scheduled_promises`. The only way to dispatch it is `execute_scheduled`: [5](#0-4) 

The comment explicitly acknowledges that this function is intentionally open to anyone. However, there is no reward, refund, or any other incentive for a third party to pay the NEAR gas required to call it. The function is `#[payable]` but any attached deposit goes to the router contract, not to the caller.

There is no function in the router contract to cancel a scheduled promise or to withdraw the NEAR that was deposited for it. The only NEAR-withdrawal path is `send_refund`, which is restricted to the parent (Aurora engine) and only returns the fixed 2 NEAR storage-staking amount: [6](#0-5) 

Any NEAR beyond the storage-staking amount that was moved to the router for the cross-contract call has no recovery path if `execute_scheduled` is never called.

### Impact Explanation

A user who is purely an EVM participant (no NEAR account) cannot call `execute_scheduled` themselves. They must rely on a third party. Because there is no on-chain incentive for any third party to pay the gas for `execute_scheduled`, the NEAR that was unwrapped from the user's wNEAR and deposited into the router sub-account can be permanently frozen. The user's wNEAR balance was already debited at the time of the EVM transaction; the cross-contract call never executes; and the NEAR cannot be recovered through any other router method.

**Impact: High — Permanent freezing of user funds (NEAR/wNEAR).**

### Likelihood Explanation

The `Delayed` mode is a documented, production-supported feature intended for use when an expensive EVM call leaves insufficient gas for immediate promise execution. Any EVM-native user who uses this mode with a non-zero NEAR attachment and who lacks a NEAR account (or whose relayer stops operating) is exposed. The likelihood is medium: the scenario requires use of the `Delayed` path with attached NEAR, but this is a normal and expected usage pattern.

### Recommendation

1. **Add a cancellation function** to the router contract that allows the parent (Aurora engine) to remove a scheduled promise and return the deposited NEAR to the originating EVM address (via wNEAR minting or direct transfer).
2. **Add an on-chain incentive** for callers of `execute_scheduled`, for example by forwarding a small tip from the deposited NEAR to `env::predecessor_account_id()`.
3. **Document the dependency** on an external executor (relayer/keeper) in the contract and in the AIP specification, and ensure the Aurora relayer infrastructure guarantees execution of all scheduled promises.
4. **Consider a deadline**: if `execute_scheduled` is not called within N blocks, allow the deposited NEAR to be reclaimed by the router's parent.

### Proof of Concept

1. Alice (EVM-only user, no NEAR account) calls the XCC precompile with `CrossContractCallArgs::Delayed` and a promise that requires 1 NEAR attached.
2. The precompile deducts 1 NEAR worth of wNEAR from Alice's EVM balance via `transferFrom`.
3. The engine's promise chain calls `withdraw_wnear_to_router`, unwrapping the wNEAR and depositing 1 NEAR into Alice's router sub-account (`{alice_address}.aurora`).
4. The router's `schedule` is called; the promise is stored at nonce 0.
5. No one calls `execute_scheduled({"nonce": "0"})` because there is no incentive to pay the NEAR gas.
6. Alice's 1 NEAR is permanently locked in the router. Alice's wNEAR was already burned. The cross-contract call never executes.
7. Alice cannot recover the NEAR: `send_refund` is restricted to the parent engine and only returns the 2 NEAR storage-staking amount; there is no cancel or withdraw function. [7](#0-6) [6](#0-5)

### Citations

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

**File:** engine-precompiles/src/xcc.rs (L177-217)
```rust
        let required_near =
            match state::get_code_version_of_address(&self.io, &Address::new(sender)) {
                // If there is no deployed version of the router contract then we need to charge for storage staking
                None => attached_near + state::STORAGE_AMOUNT,
                Some(_) => attached_near,
            };
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

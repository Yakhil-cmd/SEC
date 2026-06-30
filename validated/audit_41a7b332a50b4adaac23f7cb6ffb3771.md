### Title
wNEAR Burned Before XCC Call Confirmation — User Loses Funds on Failed Cross-Contract Calls - (`engine-precompiles/src/xcc.rs`)

### Summary

The XCC precompile deducts (burns) the user's wNEAR ERC-20 tokens **before** the downstream NEAR cross-contract call is confirmed to have succeeded. If the downstream call fails, the NEAR is refunded to the router sub-account, but the wNEAR is never re-minted to the user. The user permanently loses the wNEAR they attached to the failed call.

### Finding Description

When a user invokes the XCC precompile with `attached_near > 0`, the following sequence occurs:

**Step 1 — wNEAR burned from user's EVM balance (immediate, within the EVM transaction):**

In `engine-precompiles/src/xcc.rs`, when `required_near != ZERO_YOCTO`, the precompile immediately executes an ERC-20 `transferFrom` call inside the EVM, moving the user's wNEAR to the engine's implicit address: [1](#0-0) 

This EVM state change is committed atomically with the EVM transaction. The user's wNEAR balance is reduced immediately.

**Step 2 — wNEAR converted to NEAR and sent to the router (async NEAR promise):**

In `engine/src/xcc.rs`, `handle_precompile_promise` builds a promise chain. When `required_near > 0`, it schedules a `withdraw_wnear_to_router` callback that burns the wNEAR from the engine's implicit address and sends real NEAR to the router: [2](#0-1) 

**Step 3 — Router executes the user's actual XCC call (async NEAR promise):**

The final step in the chain calls `execute` on the router: [3](#0-2) 

The router's `execute` method creates the downstream promise and returns it: [4](#0-3) 

**The missing refund:** There is **no failure callback** attached after the router's `execute` call. If the downstream XCC call fails (target contract panics, runs out of gas, rejects the call, etc.), the NEAR attached to it is refunded by the NEAR runtime to the router sub-account (`{user_address}.aurora`). However:

1. The wNEAR ERC-20 tokens were already burned from the EVM state in Step 2.
2. The router has no function to convert its NEAR balance back to wNEAR and return it to the user.
3. The router is a sub-account of Aurora Engine — the user holds no keys to it and cannot directly withdraw from it. [5](#0-4) 

The only NEAR-exit from the router is `send_refund` (which sends a fixed `REFUND_AMOUNT` back to the engine, not the user) and NEAR attached to future XCC calls. There is no path for the user to recover NEAR that accumulated in the router from failed calls.

### Impact Explanation

A user who attaches NEAR to an XCC call (e.g., for a storage deposit on a target contract) and whose call fails will:
- Lose the wNEAR ERC-20 tokens (burned from EVM state, irreversible).
- Have the NEAR equivalent stranded in the router sub-account, inaccessible without making additional XCC calls.

This constitutes **direct theft of user funds in motion**: the user's wNEAR is consumed for an operation that did not succeed, with no refund path. The amount lost equals `call.total_near()` — the total NEAR the user attached to their XCC call.

### Likelihood Explanation

Any XCC call with `attached_near > 0` is vulnerable. Failures are common in practice:
- Target contract panics (e.g., insufficient storage deposit, wrong arguments).
- Target contract runs out of gas.
- Target contract rejects the call for business logic reasons.

This is a standard user-facing feature (the XCC precompile at `0x516cded1d16af10cad47d6d49128e2eb7d27b372`) reachable by any EVM user who holds wNEAR and calls the precompile with a non-zero `attached_balance` in their `PromiseCreateArgs`. [6](#0-5) 

### Recommendation

Attach a failure-handling callback after the router's `execute` promise. On failure, the callback should re-mint the burned wNEAR amount back to the original sender's EVM address (analogous to how `exit_to_near_precompile_callback` calls `engine::refund_on_error` when the exit promise fails): [7](#0-6) 

The same `refund_on_error` pattern should be applied: detect the failed promise result in a new callback, and re-mint the wNEAR ERC-20 tokens to the user's address.

### Proof of Concept

1. User holds 1 wNEAR (bridged) in their EVM address on Aurora.
2. User calls the XCC precompile with `CrossContractCallArgs::Eager`, targeting a NEAR contract method that will panic, with `attached_balance = 1_000_000_000_000_000_000_000_000` (1 NEAR).
3. The precompile executes `transferFrom` of 1 wNEAR from user → engine implicit address. **User's wNEAR balance is now 0.**
4. The NEAR promise chain fires: `withdraw_wnear_to_router` burns the wNEAR and sends 1 NEAR to the router.
5. The router calls the target contract with 1 NEAR attached. The target contract panics.
6. NEAR runtime refunds 1 NEAR to the router.
7. **Result:** User has 0 wNEAR. 1 NEAR is stranded in `{user_address}.aurora`. No refund callback fires. User's funds are lost. [8](#0-7) [9](#0-8)

### Citations

**File:** engine-precompiles/src/xcc.rs (L79-97)
```rust
pub mod cross_contract_call {
    use aurora_engine_types::{
        H256,
        types::{Address, make_address},
    };

    /// NEAR Cross Contract Call precompile address
    ///
    /// Address: `0x516cded1d16af10cad47d6d49128e2eb7d27b372`
    /// This address is computed as: `&keccak("nearCrossContractCall")[12..]`
    pub const ADDRESS: Address = make_address(0x516cded1, 0xd16af10cad47d6d49128e2eb7d27b372);

    /// Sentinel value used to indicate the following topic field is how much NEAR the
    /// cross-contract call will require.
    pub const AMOUNT_TOPIC: H256 = crate::make_h256(
        0x0072657175697265645f6e656172,
        0x0072657175697265645f6e656172,
    );
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

**File:** engine/src/xcc.rs (L337-341)
```rust
    match withdraw_id {
        None => handler.promise_create_call(promise),
        Some(withdraw_id) => handler.promise_attach_callback(withdraw_id, promise),
    }
}
```

**File:** etc/xcc-router/src/lib.rs (L128-133)
```rust
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

### Title
XCC Precompile Uses Hardcoded Gas Constants for Router `execute` Call That Become Stale After Router Upgrades, Permanently Freezing User NEAR - (`engine-precompiles/src/xcc.rs`)

### Summary

The Cross-Contract Call (XCC) precompile hardcodes the NEAR gas attached to the router's `execute` promise using compile-time constants (`ROUTER_EXEC_BASE` = 7 TGas, `ROUTER_EXEC_PER_CALLBACK` = 12 TGas). These constants are calibrated for the current router bytecode. Because the router contract is independently upgradeable via `factory_update`, a router upgrade that increases execution gas requirements will cause every subsequent `CrossContractCallArgs::Eager` XCC call to fail at the `execute` step — after the user's wNEAR has already been irreversibly converted to NEAR and deposited into the router sub-account, with no recovery path.

### Finding Description

**Root cause — hardcoded gas constants in the XCC precompile:**

In `engine-precompiles/src/xcc.rs`, the `CrossContractCallArgs::Eager` branch computes the gas to attach to the router's `execute` promise using two compile-time constants:

```rust
pub const ROUTER_EXEC_BASE: NearGas = NearGas::new(7_000_000_000_000);
pub const ROUTER_EXEC_PER_CALLBACK: NearGas = NearGas::new(12_000_000_000_000);
``` [1](#0-0) 

These are used to build the promise that will call `execute` on the router:

```rust
let router_exec_cost = costs::ROUTER_EXEC_BASE
    + NearGas::new(callback_count * costs::ROUTER_EXEC_PER_CALLBACK.as_u64());
let promise = PromiseCreateArgs {
    target_account_id,
    method: consts::ROUTER_EXEC_NAME.into(),
    ...
    attached_gas: router_exec_cost.saturating_add(call_gas),
};
``` [2](#0-1) 

**The router is independently upgradeable:**

The engine owner can replace the router bytecode at any time via `factory_update`: [3](#0-2) 

When a new router is deployed, `handle_precompile_promise` in `engine/src/xcc.rs` deploys it and records the new version, but the precompile's gas constants are **not updated** — they remain at the values baked into the engine binary. [4](#0-3) 

**The irreversible fund transfer happens before the promise executes:**

Inside `run_with_handle`, before the promise log is emitted, the precompile performs an EVM sub-call that transfers `required_near` wNEAR from the user to the engine's implicit address: [5](#0-4) 

Then, in `filter_promises_from_logs` → `handle_precompile_promise`, the engine schedules a `withdraw_wnear_to_router` NEAR promise that converts that wNEAR to actual NEAR and deposits it into the router sub-account: [6](#0-5) 

Only after this withdrawal succeeds does the `execute` callback fire: [7](#0-6) 

**If `execute` runs out of gas, the NEAR is stranded:**

The `execute` call carries `attached_balance: ZERO_YOCTO`. The NEAR is already in the router account's balance (deposited by the preceding `withdraw_wnear_to_router` step). When `execute` panics due to insufficient prepaid gas, NEAR protocol refunds only the unused *prepaid gas* cost — not the router's balance. The NEAR deposited into the router account stays there permanently. There is no general recovery path: only the engine can call `execute` or `schedule` on the router, and any retry will fail identically until the engine binary is upgraded with corrected constants.

The `send_refund` mechanism only covers the storage-staking deposit for newly created router accounts, not arbitrary NEAR balances: [8](#0-7) 

### Impact Explanation

**Critical — Permanent freezing of user funds.**

Any user who calls the XCC precompile with `CrossContractCallArgs::Eager` after a router upgrade that increases `execute`'s gas requirements will:
1. Have their wNEAR irreversibly burned (converted to NEAR and sent to the router sub-account).
2. Have their intended NEAR cross-contract call silently fail.
3. Have no mechanism to recover the NEAR from the router sub-account.

The NEAR is permanently frozen in the router sub-account until the engine binary itself is upgraded — an action that is not guaranteed to happen promptly and that requires a separate governance/deployment cycle.

### Likelihood Explanation

**Moderate.** The router contract is explicitly designed to be upgradeable, and upgrades have already occurred in production (CHANGES.md references multiple router upgrades). Any router upgrade that adds new storage reads, additional SDK calls, or more complex promise-building logic can push the actual gas consumption above the hardcoded 7 TGas base. The engine owner may upgrade the router without simultaneously upgrading the engine binary (e.g., a hotfix to the router), leaving the constants stale. Every EVM user who subsequently calls the XCC precompile with an `Eager` call is affected.

### Recommendation

1. **Make the router execution gas constants configurable on-chain.** Store `ROUTER_EXEC_BASE` and `ROUTER_EXEC_PER_CALLBACK` in engine storage (similar to how `WNEAR_KEY` is stored) so they can be updated via an owner-only transaction without a full engine upgrade.

2. **Atomically update gas constants when upgrading the router.** The `factory_update` entrypoint should accept updated gas constants alongside the new bytecode, or a companion transaction should be required.

3. **Add a recovery path for stranded NEAR.** Implement a method (callable by the router owner or the user) to sweep NEAR from a router sub-account back to the user's wNEAR balance in the event of a failed `execute` call.

### Proof of Concept

**Setup:**
- Aurora Engine is deployed with the current router (version N).
- `ROUTER_EXEC_BASE = 7_000_000_000_000` (7 TGas) is baked into the engine binary.

**Step 1 — Owner upgrades the router to version N+1:**
```
aurora.factory_update(new_router_wasm_bytes)  // new router needs 10 TGas base
```
The engine's `ROUTER_EXEC_BASE` constant remains 7 TGas.

**Step 2 — User calls the XCC precompile:**
```solidity
// EVM transaction targeting cross_contract_call::ADDRESS
// CrossContractCallArgs::Eager(PromiseArgs::Create(some_promise))
```

**Step 3 — Precompile transfers user's wNEAR:**
Inside `run_with_handle`, `transferFrom(user → engine_implicit_address, required_near)` succeeds. The user's wNEAR is gone.

**Step 4 — `handle_precompile_promise` schedules the promise chain:**
- `withdraw_wnear_to_router` fires and deposits NEAR into the router sub-account. ✓
- `execute` fires with `attached_gas = 7 TGas + call_gas`. The new router needs 10 TGas. ✗ — out of gas panic.

**Result:** The user's NEAR sits in the router sub-account (`{user_address}.aurora`) permanently. The user's wNEAR is gone. The intended cross-contract call never executed. No event is emitted indicating failure. [9](#0-8) [10](#0-9) [11](#0-10)

### Citations

**File:** engine-precompiles/src/xcc.rs (L22-50)
```rust
pub mod costs {
    use crate::prelude::types::{EthGas, NearGas};

    /// Base EVM gas cost for calling this precompile.
    /// Value obtained from the following methodology:
    /// 1. Estimate the cost of calling this precompile in terms of NEAR gas.
    ///    This is done by calling the precompile with inputs of different lengths
    ///    and performing a linear regression to obtain a function
    ///    `NEAR_gas = CROSS_CONTRACT_CALL_BASE + (input_length) * (CROSS_CONTRACT_CALL_BYTE)`.
    /// 2. Convert the NEAR gas cost into an EVM gas cost using the conversion ratio below
    ///    (`CROSS_CONTRACT_CALL_NEAR_GAS`).
    ///
    /// This process is done in the `test_xcc_eth_gas_cost` test in
    /// `engine-tests/src/tests/xcc.rs`.
    pub const CROSS_CONTRACT_CALL_BASE: EthGas = EthGas::new(343_650);
    /// Additional EVM gas cost per bytes of input given.
    /// See `CROSS_CONTRACT_CALL_BASE` for estimation methodology.
    pub const CROSS_CONTRACT_CALL_BYTE: EthGas = EthGas::new(4);
    /// EVM gas cost per NEAR gas attached to the created promise.
    /// This value is derived from the gas report `https://hackmd.io/@birchmd/Sy4piXQ29`
    /// The units on this quantity are `NEAR Gas / EVM Gas`.
    /// The report gives a value `0.175 T(NEAR_gas) / k(EVM_gas)`. To convert the units to
    /// `NEAR Gas / EVM Gas`, we simply multiply `0.175 * 10^12 / 10^3 = 175 * 10^6`.
    pub const CROSS_CONTRACT_CALL_NEAR_GAS: u64 = 175_000_000;

    pub const ROUTER_EXEC_BASE: NearGas = NearGas::new(7_000_000_000_000);
    pub const ROUTER_EXEC_PER_CALLBACK: NearGas = NearGas::new(12_000_000_000_000);
    pub const ROUTER_SCHEDULE: NearGas = NearGas::new(5_000_000_000_000);
}
```

**File:** engine-precompiles/src/xcc.rs (L139-157)
```rust
        let (promise, attached_near) = match args {
            CrossContractCallArgs::Eager(call) => {
                let call_gas = call.total_gas();
                let attached_near = call.total_near();
                let callback_count = call
                    .promise_count()
                    .checked_sub(1)
                    .ok_or_else(|| ExitError::Other(Cow::from(consts::ERR_INVALID_INPUT)))?;
                let router_exec_cost = costs::ROUTER_EXEC_BASE
                    + NearGas::new(callback_count * costs::ROUTER_EXEC_PER_CALLBACK.as_u64());
                let promise = PromiseCreateArgs {
                    target_account_id,
                    method: consts::ROUTER_EXEC_NAME.into(),
                    args: borsh::to_vec(&call)
                        .map_err(|_| ExitError::Other(Cow::from(consts::ERR_SERIALIZE)))?,
                    attached_balance: ZERO_YOCTO,
                    attached_gas: router_exec_cost.saturating_add(call_gas),
                };
                (promise, attached_near)
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

**File:** engine/src/contract_methods/xcc.rs (L67-77)
```rust
#[named]
pub fn factory_update<I: IO + Copy, E: Env>(io: I, env: &E) -> Result<(), ContractError> {
    with_hashchain(io, env, function_name!(), |mut io| {
        let state = state::get_state(&io)?;
        require_running(&state)?;
        require_owner_only(&state, &env.predecessor_account_id())?;
        let bytes = io.read_input().to_vec();
        let router_bytecode = xcc::RouterCode::new(bytes);
        xcc::update_router_code(&mut io, &router_bytecode);
        Ok(())
    })
```

**File:** engine/src/xcc.rs (L209-237)
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
```

**File:** engine/src/xcc.rs (L283-340)
```rust
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
```

**File:** etc/xcc-router/src/lib.rs (L39-40)
```rust
/// Must match aurora_engine_precompiles::xcc::state::STORAGE_AMOUNT
const REFUND_AMOUNT: NearToken = NearToken::from_near(2);
```

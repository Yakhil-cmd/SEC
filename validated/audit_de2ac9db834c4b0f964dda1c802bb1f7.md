### Title
XCC Router Pre-Creation Frontrun Causes Victim to Permanently Lose `STORAGE_AMOUNT` wNEAR - (File: `engine/src/xcc.rs`, `engine-precompiles/src/xcc.rs`, `etc/xcc-router/src/lib.rs`)

### Summary
An unprivileged NEAR account can call `fund_xcc_sub_account` (which is publicly callable when `wnear_account_id` is `None`) to pre-create the XCC router sub-account for any victim EVM address. If this is done after the victim's EVM transaction has already been processed by the engine (which charged the victim `STORAGE_AMOUNT` worth of wNEAR) but before the resulting NEAR promise executes, the victim permanently loses `STORAGE_AMOUNT` (2 NEAR) worth of wNEAR ERC-20 tokens with no refund path.

### Finding Description

**Step 1 — EVM precompile charges the victim at EVM-tx time.**

In `engine-precompiles/src/xcc.rs`, the `CrossContractCall` precompile checks whether the sender's router already exists:

```rust
let required_near =
    match state::get_code_version_of_address(&self.io, &Address::new(sender)) {
        None => attached_near + state::STORAGE_AMOUNT,
        Some(_) => attached_near,
    };
if required_near != ZERO_YOCTO {
    // transferFrom victim's wNEAR to engine's implicit EVM address
    ...
}
```

When no router exists (`None`), `required_near = attached_near + STORAGE_AMOUNT` (2 NEAR extra). The precompile immediately calls `transferFrom` on the wNEAR ERC-20 contract, moving `required_near` worth of wNEAR from the victim's EVM address to the engine's implicit EVM address. This `required_near` value is then encoded into the emitted promise log topic for use by the NEAR-side promise handler. [1](#0-0) 

**Step 2 — The promise executes asynchronously (next block or later).**

NEAR cross-contract calls are asynchronous. After the EVM transaction is processed in block N, the resulting NEAR promise executes in block N+1 or later. `handle_precompile_promise` in `engine/src/xcc.rs` re-reads `get_code_version_of_address` at promise-execution time:

```rust
let sender_code_version = get_code_version_of_address(io, &sender);
let deploy_needed = AddressVersionStatus::new(io, latest_code_version, sender_code_version);
``` [2](#0-1) 

**Step 3 — Attacker frontruns by calling `fund_xcc_sub_account` for the victim.**

`fund_xcc_sub_account` is publicly callable when `wnear_account_id` is `None`:

```rust
if args.wnear_account_id.is_some() {
    require_owner_only(&state, &env.predecessor_account_id())?;
}
xcc::fund_xcc_sub_account(&io, handler, env, args)?;
``` [3](#0-2) 

The attacker submits `fund_xcc_sub_account` targeting the victim's EVM address (attaching ≥ 2 NEAR). The engine creates the router sub-account, deploys the router contract, calls `initialize`, and the `factory_update_address_version` callback sets the code version for the victim's address in the engine's storage. [4](#0-3) 

**Step 4 — Victim's promise executes and sees `UpToDate`.**

When the victim's promise now executes, `get_code_version_of_address` returns `Some(version)` → `AddressVersionStatus::UpToDate`. No `CreateAccount` batch is issued, and critically, `refund_needed = false`:

```rust
let refund_needed = match deploy_needed {
    AddressVersionStatus::DeployNeeded { create_needed } => create_needed,
    AddressVersionStatus::UpToDate => false,
};
``` [5](#0-4) 

The `withdraw_wnear_to_router` call then withdraws the full `required_near` (which includes `STORAGE_AMOUNT`) from the engine's implicit EVM address and sends it to the router. No `send_refund` is triggered. The victim's extra `STORAGE_AMOUNT` (2 NEAR) worth of wNEAR is permanently transferred to the router sub-account with no recovery path for the victim. [6](#0-5) 

**Step 5 — The router's `initialize` correctly sets `parent` to the Aurora Engine.**

The router's `initialize` function sets `parent` to `env::predecessor_account_id()`, which in the batch context is the Aurora Engine account. So the attacker does not gain control of the router — the attack is purely economic harm to the victim. [7](#0-6) 

### Impact Explanation

The victim permanently loses `STORAGE_AMOUNT` = 2 NEAR worth of wNEAR ERC-20 tokens per attack. The tokens are converted to NEAR and sent to the router sub-account, which is controlled by the Aurora Engine. There is no refund mechanism for the victim. This constitutes direct theft of user funds (High impact — theft of user funds in motion).

### Likelihood Explanation

- `fund_xcc_sub_account` with `wnear_account_id: None` is callable by any NEAR account with no access restriction.
- NEAR's asynchronous promise model guarantees at least one block between EVM tx processing and promise execution, giving the attacker a reliable window.
- The victim's EVM address is deterministically derivable from the router sub-account name (`{hex_address}.{aurora_engine_account}`), making target identification trivial.
- The attacker pays 2 NEAR to cause the victim to lose 2 NEAR — economically rational for targeted attacks against high-value addresses.
- No special privileges, leaked keys, or oracle errors are required.

### Recommendation

1. **Re-check `get_code_version_of_address` at promise execution time and recompute `required_near` accordingly.** If the router already exists when the promise executes, the `required_near` should be reduced by `STORAGE_AMOUNT` before calling `withdraw_wnear_to_router`, and the excess wNEAR at the engine's implicit address should be returned to the victim's EVM address.

2. **Alternatively**, add a refund path: if `deploy_needed = UpToDate` but `required_near` (from the log) includes `STORAGE_AMOUNT`, refund `STORAGE_AMOUNT` worth of wNEAR back to the sender's EVM address before proceeding.

### Proof of Concept

1. Victim (`0xVICTIM`) submits an EVM transaction calling the XCC precompile with `attached_near = 0`.
2. Engine processes the EVM tx: `get_code_version_of_address(0xVICTIM)` → `None`; `required_near = 2 NEAR`; `transferFrom(0xVICTIM, engine_implicit, 2 NEAR wNEAR)` succeeds; promise log emitted with `required_near = 2 NEAR`.
3. Attacker calls `fund_xcc_sub_account(target = 0xVICTIM, wnear_account_id = None)` with 2 NEAR attached. Router `{0xvictim}.aurora` is created; `factory_update_address_version` sets version for `0xVICTIM`.
4. Victim's promise executes: `get_code_version_of_address(0xVICTIM)` → `Some(v)` → `UpToDate`; `withdraw_wnear_to_router(amount = 2 NEAR)` sends 2 NEAR to the router; `refund_needed = false`; no refund issued.
5. Victim's XCC call completes (with 0 NEAR attached to the actual call), but the victim has permanently lost 2 NEAR worth of wNEAR with no recovery path.

### Citations

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

**File:** engine/src/xcc.rs (L115-130)
```rust
        if create_needed {
            if fund_amount < STORAGE_AMOUNT {
                return Err(FundXccError::InsufficientBalance);
            }

            promise_actions.push(PromiseAction::CreateAccount);
            promise_actions.push(PromiseAction::Transfer {
                amount: fund_amount,
            });
            promise_actions.push(PromiseAction::DeployContract { code });
            promise_actions.push(PromiseAction::FunctionCall {
                name: "initialize".into(),
                args: init_args.into_bytes(),
                attached_yocto: ZERO_YOCTO,
                gas: INITIALIZE_GAS,
            });
```

**File:** engine/src/xcc.rs (L206-208)
```rust
    let latest_code_version = get_latest_code_version(io);
    let sender_code_version = get_code_version_of_address(io, &sender);
    let deploy_needed = AddressVersionStatus::new(io, latest_code_version, sender_code_version);
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

**File:** engine/src/contract_methods/xcc.rs (L142-147)
```rust
        if args.wnear_account_id.is_some() {
            require_owner_only(&state, &env.predecessor_account_id())?;
        }

        xcc::fund_xcc_sub_account(&io, handler, env, args)?;
        Ok(())
```

**File:** etc/xcc-router/src/lib.rs (L76-89)
```rust
        let caller = env::predecessor_account_id();
        let mut parent = LazyOption::new(StorageKey::Parent, None);
        match parent.get() {
            None => {
                parent.set(&caller);
            }
            Some(parent) => {
                // Allow self-calls to `initialize` also.
                // This happens during the upgrade flow.
                if (caller != parent) && (caller != env::current_account_id()) {
                    env::panic_str(ERR_ILLEGAL_CALLER);
                }
            }
        }
```

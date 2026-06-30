The code path is fully traceable. Let me confirm the exact behavior.

### Title
Unhandled Transfer Failure in `fund_xcc_sub_account` UpToDate Path Freezes Caller's Attached NEAR — (`engine/src/xcc.rs`)

---

### Summary

When `fund_xcc_sub_account` is called for a target address whose engine-storage version marks it as `AddressVersionStatus::UpToDate`, the function issues a bare NEAR `Transfer` to the router sub-account with **no callback attached**. If the actual on-chain sub-account no longer exists (engine storage and on-chain state have diverged), the NEAR transfer fails silently at the NEAR runtime level. The attached deposit is refunded to the Aurora Engine contract itself — not to the original caller — permanently stranding the caller's funds in the engine balance with no recovery path.

---

### Finding Description

The entry point is the public `fund_xcc_sub_account` NEAR method. When called with `wnear_account_id = None`, there is **no owner check**: [1](#0-0) 

Control passes to `xcc::fund_xcc_sub_account`. The version status is determined entirely from **engine storage**, not from the actual on-chain NEAR account state: [2](#0-1) 

`AddressVersionStatus::UpToDate` is returned whenever engine storage holds a `CodeVersion` for the address that is `>= latest_code_version`, or `< first_upgradable_version`: [3](#0-2) 

In the `UpToDate` branch, the only promise action constructed is a plain `Transfer`: [4](#0-3) 

The batch promise is dispatched, and the callback block is **guarded by `DeployNeeded`** — meaning no callback is ever attached for the `UpToDate` path: [5](#0-4) 

In NEAR Protocol, a `Transfer` action to a non-existent account fails. On failure, the runtime creates a refund receipt directed at the `predecessor_id` of the failed receipt — which is the Aurora Engine contract, **not the original transaction signer**. The caller's attached deposit is absorbed into the engine's own balance with no mechanism to return it.

---

### Impact Explanation

The caller's attached NEAR deposit is irretrievably stranded in the Aurora Engine contract balance. There is no automatic refund, no error surfaced to the caller (the function returns `Ok(())`), and no on-chain recovery path for the affected user. This constitutes **temporary freezing of funds** (High impact per scope).

---

### Likelihood Explanation

The precondition — engine storage holding a `CodeVersion` for an address whose actual NEAR sub-account no longer exists — can arise in at least two realistic ways:

1. **Router self-deletion**: If the deployed XCC router contract exposes any `delete_account`-equivalent method (common in NEAR contracts for cleanup), the account owner can delete the sub-account while engine storage retains the version entry.
2. **`first_upgradable_version` path**: Addresses with a stored version below `first_upgradable_version` are unconditionally treated as `UpToDate` (line 465–468), even if the account was deleted. These are legacy routers that cannot be upgraded, so the engine never re-creates them.

The call is permissionless when `wnear_account_id = None`, so any user can trigger the loss once the diverged state exists.

---

### Recommendation

Attach a failure-handling callback in the `UpToDate` branch, mirroring the pattern already used in `DeployNeeded`. The callback should check `promise_result_check()` and, on failure, refund `fund_amount` back to the caller (`env.predecessor_account_id()`). Alternatively, verify on-chain account existence before issuing the transfer (e.g., via a `promise_batch_action_function_call` to a view method), or treat a failed transfer as a signal to re-deploy the router (resetting the version status).

---

### Proof of Concept

```
// Setup: engine storage has CodeVersion(N) for address A, but the NEAR
// sub-account A.<engine> does not exist on-chain.

// Step 1: Any user calls fund_xcc_sub_account with wnear_account_id=None,
//         target=A, and attaches 5 NEAR.
fund_xcc_sub_account({ target: A, wnear_account_id: None }, deposit=5_NEAR)

// Step 2: get_code_version_of_address(A) returns Some(N) from engine storage.
//         AddressVersionStatus::new returns UpToDate.

// Step 3: promise_actions = [Transfer { amount: 5_NEAR }]
//         No CreateAccount, no callback.

// Step 4: promise_create_batch fires Transfer to non-existent A.<engine>.
//         NEAR runtime: receipt fails, refund goes to predecessor = Aurora Engine.

// Step 5: fund_xcc_sub_account returned Ok(()) to the caller.
//         Caller's 5 NEAR is now in Aurora Engine's balance, not returned.
//         No error, no refund receipt to the caller.
```

In a mock/unit test: set `get_code_version_of_address` to return `Some(latest_version)` for address `A`, simulate `promise_create_batch` returning a failed result, and assert that `env.predecessor_account_id()` balance is unchanged — demonstrating the missing refund. [6](#0-5)

### Citations

**File:** engine/src/contract_methods/xcc.rs (L142-144)
```rust
        if args.wnear_account_id.is_some() {
            require_owner_only(&state, &env.predecessor_account_id())?;
        }
```

**File:** engine/src/xcc.rs (L68-176)
```rust
pub fn fund_xcc_sub_account<I, P, E>(
    io: &I,
    handler: &mut P,
    env: &E,
    args: FundXccArgs,
) -> Result<(), FundXccError>
where
    P: PromiseHandler,
    I: IO + Copy,
    E: Env,
{
    let current_account_id = env.current_account_id();
    let target_account_id = AccountId::try_from(format!(
        "{}.{}",
        args.target.encode(),
        current_account_id.as_ref()
    ))?;

    let latest_code_version = get_latest_code_version(io);
    let target_code_version = get_code_version_of_address(io, &args.target);
    let deploy_needed = AddressVersionStatus::new(io, latest_code_version, target_code_version);

    let fund_amount = Yocto::new(env.attached_deposit());

    let mut promise_actions = Vec::with_capacity(4);

    // If account needs to be created and/or updated then include those actions.
    if let AddressVersionStatus::DeployNeeded { create_needed } = deploy_needed {
        let code = get_router_code(io).0.into_owned();
        // wnear_account is needed for initialization so we must assume it is set
        // in the Engine, or we need to accept it as input.
        let wnear_account = if let Some(wnear_account) = args.wnear_account_id {
            wnear_account
        } else {
            // If the wnear account is not specified then we must look it up based on the
            // bridged token registry for the engine.
            let wnear_address = get_wnear_address(io);
            crate::engine::nep141_erc20_map(*io)
                .lookup_right(&crate::engine::ERC20Address(wnear_address))
                .ok_or(FundXccError::MissingWNearAddress)?
                .0
        };
        let init_args = format!(
            r#"{{"wnear_account": "{}", "must_register": {}}}"#,
            wnear_account.as_ref(),
            create_needed,
        );
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
        } else {
            let deploy_args = DeployUpgradeParams {
                code,
                initialize_args: init_args.into_bytes(),
            };
            promise_actions.push(PromiseAction::FunctionCall {
                name: "deploy_upgrade".into(),
                args: borsh::to_vec(&deploy_args).expect(ERR_UPGRADE_ARG_SERIALIZATION),
                attached_yocto: fund_amount,
                gas: UPGRADE_GAS + INITIALIZE_GAS,
            });
        }
    } else {
        // No matter what include the transfer of the funding amount
        promise_actions.push(PromiseAction::Transfer {
            amount: fund_amount,
        });
    }

    let batch = PromiseBatchAction {
        target_account_id,
        actions: promise_actions,
    };
    // Safety: same as safety in `handle_precompile_promise`
    let promise_id = handler.promise_create_batch(&batch);

    if let AddressVersionStatus::DeployNeeded { .. } = deploy_needed {
        // If a creation and/or deployment were needed, then we must attach a callback to update
        // the Engine's record of the account.

        let args = AddressVersionUpdateArgs {
            address: args.target,
            version: latest_code_version,
        };
        let callback = PromiseCreateArgs {
            target_account_id: current_account_id,
            method: "factory_update_address_version".into(),
            args: borsh::to_vec(&args).map_err(|_| FundXccError::SerializationFailure)?,
            attached_balance: ZERO_YOCTO,
            attached_gas: VERSION_UPDATE_GAS,
        };
        let _promise_id = handler.promise_attach_callback(promise_id, &callback);
    }

    Ok(())
}
```

**File:** engine/src/xcc.rs (L461-474)
```rust
        match target_code_version {
            None => Self::DeployNeeded {
                create_needed: true,
            },
            Some(version) if version < first_upgradable_version => {
                // It is impossible to upgrade the initial XCC routers because
                // they lack the upgrade method.
                Self::UpToDate
            }
            Some(version) if version < latest_code_version => Self::DeployNeeded {
                create_needed: false,
            },
            Some(_version) => Self::UpToDate,
        }
```

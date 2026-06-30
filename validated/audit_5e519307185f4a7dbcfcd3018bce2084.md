### Title
Stale Cached Router Code Version Causes Overcharge of `STORAGE_AMOUNT` in XCC Precompile - (File: `engine-precompiles/src/xcc.rs`)

---

### Summary

The XCC precompile charges users `STORAGE_AMOUNT` (2 NEAR) for router contract storage staking based on a locally cached `code_version_of_address` value. This cached value is only updated asynchronously via the `factory_update_address_version` callback after a router deployment promise resolves on NEAR. During the window between the initial XCC call and the callback completion, the cached state is stale, causing any subsequent XCC call for the same sender to be overcharged by `STORAGE_AMOUNT` even though the router already exists or is being deployed. The overcharged wNEAR is transferred to the engine's implicit address and is not returned to the user.

---

### Finding Description

In `engine-precompiles/src/xcc.rs` at lines 177–182, the XCC precompile reads `get_code_version_of_address` to decide whether to charge the user `STORAGE_AMOUNT`:

```rust
let required_near =
    match state::get_code_version_of_address(&self.io, &Address::new(sender)) {
        // If there is no deployed version of the router contract then we need to charge for storage staking
        None => attached_near + state::STORAGE_AMOUNT,
        Some(_) => attached_near,
    };
```

`STORAGE_AMOUNT` is defined as `Yocto::new(2_000_000_000_000_000_000_000_000)` — 2 NEAR — in `engine-precompiles/src/xcc.rs` at line 255.

`get_code_version_of_address` reads a value from the engine's local NEAR storage (a key derived from the sender's address). This value is **only written** by `factory_update_address_version` in `engine/src/contract_methods/xcc.rs` at line 97:

```rust
xcc::set_code_version_of_address(&mut io, &args.address, args.version);
```

`factory_update_address_version` is a **callback** that runs asynchronously in a subsequent NEAR transaction, after the router deployment promise resolves. It is attached to the deployment batch in `engine/src/xcc.rs` at lines 268–279.

The stale window is the gap between:
1. The initial XCC call that triggers router deployment (local version = `None`)
2. The `factory_update_address_version` callback completing (local version = `Some(version)`)

During this window — which spans at least one NEAR block — any XCC call for the same sender address reads `None` from `get_code_version_of_address` and charges `STORAGE_AMOUNT` again. The same stale read occurs in `handle_precompile_promise` at `engine/src/xcc.rs` line 207, which also calls `get_code_version_of_address` and constructs a second deployment batch with `CreateAccount` + `Transfer { amount: STORAGE_AMOUNT }`.

The user's wNEAR is transferred to the engine's implicit address via `transferFrom` at lines 183–216 of `engine-precompiles/src/xcc.rs`. The second deployment attempt fails because the router account already exists on-chain. The `factory_update_address_version` callback for the second call sees `Some(false)` from `promise_result_check()` and returns `ERR_ROUTER_DEPLOY_FAILED` without updating the version. The overcharged NEAR remains in the engine's implicit address and is not refunded to the user.

---

### Impact Explanation

**Critical. Direct theft of user funds.**

The user's wNEAR ERC-20 balance is debited by `STORAGE_AMOUNT` (2 NEAR ≈ $6–10 at current prices) via `transferFrom` into the engine's implicit address. The corresponding NEAR deployment fails, and no refund path exists for the overcharged amount. The funds are permanently inaccessible to the user. Any user who submits two XCC calls before their router deployment callback is processed loses 2 NEAR per extra call.

---

### Likelihood Explanation

**Medium.** The stale window spans at least one NEAR block (~1 second). A user who retries a failed or slow XCC transaction, or who submits two XCC calls in rapid succession (e.g., via a script or wallet that auto-retries), will trigger the overcharge. This is a realistic scenario for any active XCC user. No special privileges or attacker cooperation are required — the user's own normal usage pattern is sufficient to trigger the loss.

---

### Recommendation

1. **Introduce a "pending deployment" flag**: When a router deployment is initiated, write a sentinel value to the engine's storage for that sender address (distinct from `None` and from a valid `CodeVersion`). The XCC precompile should check for this flag and not charge `STORAGE_AMOUNT` if a deployment is already in flight.
2. **Refund overcharged STORAGE_AMOUNT**: In `factory_update_address_version`, if the deployment failed because the account already exists (i.e., a duplicate deployment was attempted), credit the overcharged `STORAGE_AMOUNT` back to the sender's wNEAR balance.
3. **Document the risk**: Until a fix is deployed, document that users should not submit multiple XCC calls for the same address before the first router deployment callback is confirmed.

---

### Proof of Concept

1. User submits EVM transaction calling the XCC precompile (`0x516cded1...`) for the first time.
2. `get_code_version_of_address` returns `None` → `required_near = attached_near + STORAGE_AMOUNT` (2 NEAR).
3. The precompile calls `wNEAR.transferFrom(user, engine_implicit_address, required_near)` — user's wNEAR balance is debited by 2 NEAR.
4. `handle_precompile_promise` creates a batch promise: `CreateAccount` + `Transfer { amount: STORAGE_AMOUNT }` + `DeployContract` + `initialize`, with a callback to `factory_update_address_version`.
5. **Before the callback executes** (same block or next block), user submits a second EVM transaction calling the XCC precompile again.
6. `get_code_version_of_address` still returns `None` (stale) → user is charged `STORAGE_AMOUNT` again → another 2 NEAR is transferred from user's wNEAR to engine's implicit address.
7. The second deployment batch fails (`CreateAccount` on an already-existing account).
8. `factory_update_address_version` for the second call receives `Some(false)` → returns `ERR_ROUTER_DEPLOY_FAILED` → version not updated.
9. User has permanently lost 2 NEAR worth of wNEAR with no refund path.

Relevant code locations: [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5)

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

**File:** engine-precompiles/src/xcc.rs (L183-216)
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
```

**File:** engine-precompiles/src/xcc.rs (L255-255)
```rust
    pub const STORAGE_AMOUNT: Yocto = Yocto::new(2_000_000_000_000_000_000_000_000);
```

**File:** engine/src/xcc.rs (L206-209)
```rust
    let latest_code_version = get_latest_code_version(io);
    let sender_code_version = get_code_version_of_address(io, &sender);
    let deploy_needed = AddressVersionStatus::new(io, latest_code_version, sender_code_version);
    // 1. If the router contract account does not exist or is out of date then we start
```

**File:** engine/src/xcc.rs (L263-279)
```rust
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
```

**File:** engine/src/contract_methods/xcc.rs (L81-100)
```rust
pub fn factory_update_address_version<I: IO + Copy, E: Env, H: PromiseHandler>(
    io: I,
    env: &E,
    handler: &H,
) -> Result<(), ContractError> {
    with_hashchain(io, env, function_name!(), |mut io| {
        require_running(&state::get_state(&io)?)?;
        // The function is only set to be private, otherwise callback error will happen.
        env.assert_private_call()?;
        let check_deploy: Result<(), &[u8]> = match handler.promise_result_check() {
            Some(true) => Ok(()),
            Some(false) => Err(b"ERR_ROUTER_DEPLOY_FAILED"),
            None => Err(b"ERR_ROUTER_UPDATE_NOT_CALLBACK"),
        };
        check_deploy?;
        let args: xcc::AddressVersionUpdateArgs = io.read_input_borsh()?;
        xcc::set_code_version_of_address(&mut io, &args.address, args.version);
        Ok(())
    })
}
```

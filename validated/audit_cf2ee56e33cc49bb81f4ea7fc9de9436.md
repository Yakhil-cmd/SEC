### Title
XCC Precompile Panics on Missing wNEAR Address While `fund_xcc_sub_account` Handles It Gracefully — (`File: engine-precompiles/src/xcc.rs`)

---

### Summary

The `CrossContractCall::run_with_handle` precompile unconditionally calls `state::get_wnear_address`, which **panics** (NEAR-level abort) when the wNEAR address has not been set in engine storage. The sibling admin function `fund_xcc_sub_account` accepts an optional `wnear_account_id` argument and can operate without the wNEAR address being present in storage. The missing guard in the precompile path means that any EVM transaction invoking the XCC precompile for a new user (or any user attaching NEAR) will unconditionally abort the NEAR transaction when wNEAR is unset, making the entire XCC subsystem non-functional.

---

### Finding Description

**`get_wnear_address` panics on missing storage key:** [1](#0-0) 

```rust
pub fn get_wnear_address<I: IO>(io: &I) -> Address {
    let key = storage::bytes_to_key(KeyPrefix::CrossContractCall, WNEAR_KEY);
    io.read_storage(&key).map_or_else(
        || panic!("{ERR_MISSING_WNEAR_ADDRESS}"),   // ← hard panic, no Result
        |bytes| Address::try_from_slice(&bytes.to_vec()).expect(ERR_CORRUPTED_STORAGE),
    )
}
```

**XCC precompile calls it unconditionally when `required_near != ZERO_YOCTO`:** [2](#0-1) 

For every first-time XCC caller, `required_near = attached_near + STORAGE_AMOUNT` (2 NEAR), which is always non-zero. Therefore `get_wnear_address` is **always** called for new users, with no guard and no fallback.

**`handle_precompile_promise` has the same unguarded call when `DeployNeeded`:** [3](#0-2) 

```rust
let wnear_address = get_wnear_address(io);
let wnear_account = crate::engine::nep141_erc20_map(*io)
    .lookup_right(&crate::engine::ERC20Address(wnear_address))
    .expect("wnear account not found");
```

**`fund_xcc_sub_account` — the function that DOES have the guard:** [4](#0-3) 

```rust
let wnear_account = if let Some(wnear_account) = args.wnear_account_id {
    wnear_account          // ← caller-supplied fallback; no storage read needed
} else {
    let wnear_address = get_wnear_address(io);   // only reached if arg absent
    crate::engine::nep141_erc20_map(*io)
        .lookup_right(&crate::engine::ERC20Address(wnear_address))
        .ok_or(FundXccError::MissingWNearAddress)?
        .0
};
```

`fund_xcc_sub_account` can succeed without wNEAR being set in storage (caller provides `wnear_account_id`). The XCC precompile has no equivalent escape hatch.

---

### Impact Explanation

**High — Temporary freezing of funds.**

When the wNEAR address is absent from engine storage (e.g., a freshly deployed Aurora silo or any engine instance where `factory_set_wnear_address` has not yet been called), every EVM transaction that invokes the XCC precompile for a new user will trigger a NEAR-level `panic!`, aborting the NEAR transaction. The entire XCC subsystem — the mechanism by which Aurora EVM users interact with NEAR contracts, including withdrawing assets bridged to NEAR — is completely non-functional. Users holding assets that require XCC to retrieve cannot do so.

---

### Likelihood Explanation

**Medium.**

Any Aurora Engine deployment (including Aurora Silos, which are explicitly supported) that has not yet called `factory_set_wnear_address` is affected. The wNEAR address is not set during engine initialization by default; it requires a separate privileged call. A deployment window exists between engine launch and that call during which XCC is broken for all users. The condition is also permanent if the operator forgets to set it.

---

### Recommendation

In `CrossContractCall::run_with_handle`, replace the unconditional `state::get_wnear_address` call with a fallible read that returns a `PrecompileFailure::Revert` (not a panic) when the wNEAR address is absent:

```rust
// Instead of:
let wnear_address = state::get_wnear_address(&self.io);

// Use:
let wnear_address = state::try_get_wnear_address(&self.io)
    .ok_or_else(|| revert_with_message(state::ERR_MISSING_WNEAR_ADDRESS))?;
```

Apply the same fix to `handle_precompile_promise` in `engine/src/xcc.rs` so that a missing wNEAR address produces a recoverable error rather than a NEAR-level abort.

---

### Proof of Concept

1. Deploy an Aurora Engine instance (or silo) without calling `factory_set_wnear_address`.
2. From any EVM account, submit a transaction calling the XCC precompile address (`0x516cded1d16af10cad47d6d49128e2eb7d27b372`) with a valid `CrossContractCallArgs::Eager` payload.
3. Because the caller has no deployed router, `get_code_version_of_address` returns `None`, so `required_near = attached_near + STORAGE_AMOUNT > 0`.
4. The branch `if required_near != ZERO_YOCTO` is entered; `state::get_wnear_address` is called.
5. Storage key `CrossContractCall/wnear` is absent; `get_wnear_address` executes `panic!("{ERR_MISSING_WNEAR_ADDRESS}")`.
6. The NEAR transaction aborts with `ERR_MISSING_WNEAR_ADDRESS`. No XCC call can ever succeed until the admin sets the wNEAR address. [5](#0-4) [6](#0-5) [1](#0-0)

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

**File:** engine-precompiles/src/xcc.rs (L262-268)
```rust
    pub fn get_wnear_address<I: IO>(io: &I) -> Address {
        let key = storage::bytes_to_key(KeyPrefix::CrossContractCall, WNEAR_KEY);
        io.read_storage(&key).map_or_else(
            || panic!("{ERR_MISSING_WNEAR_ADDRESS}"),
            |bytes| Address::try_from_slice(&bytes.to_vec()).expect(ERR_CORRUPTED_STORAGE),
        )
    }
```

**File:** engine/src/xcc.rs (L96-110)
```rust
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
```

**File:** engine/src/xcc.rs (L217-220)
```rust
            let wnear_address = get_wnear_address(io);
            let wnear_account = crate::engine::nep141_erc20_map(*io)
                .lookup_right(&crate::engine::ERC20Address(wnear_address))
                .expect("wnear account not found");
```

### Title
Stale wNEAR ERC-20 Approval After `factory_set_wnear_address` Change Breaks All XCC Calls Requiring NEAR Payment - (File: `engine-precompiles/src/xcc.rs`)

---

### Summary

The XCC precompile reads the wNEAR ERC-20 address dynamically from NEAR storage and calls `transferFrom` on it to pull wNEAR from the user. This requires the user to have pre-approved the XCC precompile address (`cross_contract_call::ADDRESS`) on that specific wNEAR ERC-20 contract. The owner can change the wNEAR ERC-20 address at any time via `factory_set_wnear_address`. After such a change, every user's existing approval is on the old contract, while the precompile now targets the new contract — causing all XCC calls that require NEAR payment to revert.

---

### Finding Description

**Root cause — `engine-precompiles/src/xcc.rs`, lines 183–216:**

When the XCC precompile is invoked and `required_near != ZERO_YOCTO`, it:

1. Derives `engine_implicit_address` from the engine account ID.
2. Builds a `transferFrom(sender, engine_implicit_address, required_near)` calldata.
3. Reads the wNEAR ERC-20 address from NEAR storage via `state::get_wnear_address(&self.io)`.
4. Executes an EVM sub-call to that address, with `caller = cross_contract_call::ADDRESS`. [1](#0-0) 

The `transferFrom` succeeds only if the user (`sender`) has previously called `approve(cross_contract_call::ADDRESS, amount)` on the wNEAR ERC-20 contract at the address stored in NEAR state.

**The mutable config — `engine/src/contract_methods/xcc.rs`, lines 102–115:**

`factory_set_wnear_address` is an owner-callable function that overwrites the stored wNEAR ERC-20 address with an arbitrary new address. [2](#0-1) 

It calls `xcc::set_wnear_address`, which simply writes the new address to NEAR storage with no side-effects on existing EVM-level approvals. [3](#0-2) 

**The stale-approval gap:**

After `factory_set_wnear_address` is called with a new ERC-20 contract address:

- Every user's `approve(cross_contract_call::ADDRESS, ...)` is recorded in the **old** ERC-20 contract's storage.
- The XCC precompile now calls `transferFrom` on the **new** ERC-20 contract.
- The new contract has no allowance record for any user → `transferFrom` reverts.
- The EVM sub-call failure propagates as `PrecompileFailure::Revert`, aborting the entire XCC transaction. [4](#0-3) 

`required_near` is always non-zero for first-time XCC users (it includes `STORAGE_AMOUNT = 2 NEAR`) and for any call that attaches NEAR, so the broken path is hit by the vast majority of XCC usage. [5](#0-4) 

---

### Impact Explanation

**High — Temporary freezing of funds.**

After a `factory_set_wnear_address` call:

- All XCC calls that require NEAR payment revert immediately.
- Users' wNEAR ERC-20 balances remain on the old contract and cannot be consumed by the XCC precompile.
- Users must manually: (a) withdraw wNEAR from the old contract back to NEAR, (b) bridge NEAR into the new wNEAR ERC-20 contract, (c) re-approve `cross_contract_call::ADDRESS` on the new contract.
- Until users complete these steps, their wNEAR is inaccessible for XCC purposes and all cross-contract call functionality is frozen for them.

---

### Likelihood Explanation

**Medium.**

`factory_set_wnear_address` is a legitimate owner operation — it would be called when the wNEAR ERC-20 contract is upgraded or replaced (a realistic operational event). The owner has no on-chain mechanism to notify users or re-approve on their behalf. Every user who had approved the old contract is silently broken with no on-chain indication of what changed. [6](#0-5) 

---

### Recommendation

When `factory_set_wnear_address` is called, the engine should emit an event or log that the wNEAR address has changed so users know to re-approve. More robustly, the XCC precompile should check whether the `transferFrom` failed due to insufficient allowance and return a descriptive error pointing users to re-approve the new wNEAR contract. Alternatively, the protocol documentation should explicitly state that any change to the wNEAR address requires all XCC users to re-approve the new contract before their next XCC call.

---

### Proof of Concept

1. User calls `approve(cross_contract_call::ADDRESS, large_amount)` on the current wNEAR ERC-20 contract (address `W1`).
2. Owner calls `factory_set_wnear_address(W2)` — a new wNEAR ERC-20 contract address.
3. User submits an EVM transaction calling the XCC precompile with any NEAR-requiring promise.
4. The precompile reads `W2` from storage via `get_wnear_address`.
5. The precompile executes `W2.transferFrom(user, engine_implicit, required_near)` with `caller = cross_contract_call::ADDRESS`.
6. `W2` has no allowance for the user → `transferFrom` reverts.
7. The XCC precompile returns `PrecompileFailure::Revert` → the user's EVM transaction fails.
8. The user's wNEAR balance on `W1` is untouched but inaccessible for XCC until they manually migrate and re-approve. [7](#0-6) [2](#0-1)

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

**File:** engine/src/contract_methods/xcc.rs (L102-115)
```rust
#[named]
pub fn factory_set_wnear_address<I: IO + Copy, E: Env>(
    io: I,
    env: &E,
) -> Result<(), ContractError> {
    with_hashchain(io, env, function_name!(), |mut io| {
        let state = state::get_state(&io)?;
        require_running(&state)?;
        require_owner_only(&state, &env.predecessor_account_id())?;
        let address = io.read_input_arr20()?;
        xcc::set_wnear_address(&mut io, &Address::from_array(address));
        Ok(())
    })
}
```

**File:** engine/src/xcc.rs (L369-373)
```rust
/// Set the address of the `wNEAR` ERC-20 contract
pub fn set_wnear_address<I: IO>(io: &mut I, address: &Address) {
    let key = storage::bytes_to_key(KeyPrefix::CrossContractCall, WNEAR_KEY);
    io.write_storage(&key, address.as_bytes());
}
```

**File:** engine/src/lib.rs (L329-338)
```rust
    /// Sets the address for the `wNEAR` ERC-20 contract. This contract will be used by the
    /// cross-contract calls feature to have users pay for their NEAR transactions.
    #[unsafe(no_mangle)]
    pub extern "C" fn factory_set_wnear_address() {
        let io = Runtime;
        let env = Runtime;
        contract_methods::xcc::factory_set_wnear_address(io, &env)
            .map_err(ContractError::msg)
            .sdk_unwrap();
    }
```

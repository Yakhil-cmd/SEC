### Title
No way to preserve user wNEAR ERC-20 approvals after `factory_set_wnear_address` changes the wNEAR contract address — (`engine/src/contract_methods/xcc.rs`)

---

### Summary

`factory_set_wnear_address` allows the owner to point the XCC subsystem at a new wNEAR ERC-20 contract. However, the XCC precompile calls `transferFrom` on whichever wNEAR address is currently stored, and every user must have individually approved the XCC precompile address on that specific ERC-20 contract. After the address is changed, all existing per-user approvals on the old contract become worthless against the new contract, causing every subsequent XCC call to revert.

---

### Finding Description

The XCC precompile (`engine-precompiles/src/xcc.rs`) collects wNEAR payment from the calling EVM address by issuing an internal `transferFrom` call against the stored wNEAR ERC-20 contract:

```rust
// engine-precompiles/src/xcc.rs  lines 188-216
let tx_data = transfer_from_args(
    sender.0.into(),
    engine_implicit_address.raw().0.into(),
    required_near.as_u128().into(),
);
let wnear_address = state::get_wnear_address(&self.io);   // reads stored address
let (exit_reason, return_value) =
    handle.call(wnear_address.raw(), None, tx_data, None, false, &context);
``` [1](#0-0) 

For `transferFrom` to succeed, each user must have previously called `approve(cross_contract_call::ADDRESS, amount)` on that exact ERC-20 contract. The test suite makes this requirement explicit:

```rust
// engine-tests/src/tests/xcc.rs  lines 1015-1029
let approve_tx = wnear_erc20.approve(
    cross_contract_call::ADDRESS,
    WNEAR_AMOUNT.as_yoctonear().into(),
    signer.use_nonce().into(),
);
``` [2](#0-1) 

The setter `factory_set_wnear_address` only writes the new address to storage and does nothing else:

```rust
// engine/src/contract_methods/xcc.rs  lines 103-115
pub fn factory_set_wnear_address<I: IO + Copy, E: Env>(io: I, env: &E) -> Result<(), ContractError> {
    with_hashchain(io, env, function_name!(), |mut io| {
        ...
        let address = io.read_input_arr20()?;
        xcc::set_wnear_address(&mut io, &Address::from_array(address));
        Ok(())
    })
}
``` [3](#0-2) 

`set_wnear_address` itself is a plain storage write with no side-effects:

```rust
// engine/src/xcc.rs  lines 370-373
pub fn set_wnear_address<I: IO>(io: &mut I, address: &Address) {
    let key = storage::bytes_to_key(KeyPrefix::CrossContractCall, WNEAR_KEY);
    io.write_storage(&key, address.as_bytes());
}
``` [4](#0-3) 

There is no mechanism to migrate, revoke, or re-establish the per-user ERC-20 allowances that were set on the old contract. The new contract starts with a clean allowance table, so every `transferFrom` call issued by the XCC precompile against the new address will fail with an insufficient-allowance revert.

---

### Impact Explanation

**High — Temporary freezing of funds.**

After `factory_set_wnear_address` is called with a new address, every EVM user who had previously approved the XCC precompile on the old wNEAR ERC-20 contract loses the ability to execute cross-contract calls. Their wNEAR balances on the old contract are inaccessible to the XCC precompile, and any XCC transaction they submit will revert at the `transferFrom` step. The freeze persists until each user individually re-approves the XCC precompile on the new wNEAR contract and bridges NEAR into the new contract — steps that are not prompted or documented by the setter itself.

---

### Likelihood Explanation

**Medium.** The owner must call `factory_set_wnear_address` with a new address. This is a legitimate administrative action that would occur whenever the wNEAR NEP-141 contract is upgraded or replaced (a realistic operational event). The impact is immediate and affects every XCC user simultaneously with no on-chain warning.

---

### Recommendation

In `factory_set_wnear_address`, after writing the new address, emit a log or structured event so off-chain tooling can alert users. More robustly, the function should reject the call unless a migration plan is in place, or it should be paired with a helper that re-approves the XCC precompile on behalf of the engine's implicit address on the new wNEAR contract. At minimum, the function's documentation must state that all users must re-approve `cross_contract_call::ADDRESS` on the new wNEAR ERC-20 contract before XCC will work again.

---

### Proof of Concept

1. User calls `wnear_old.approve(cross_contract_call::ADDRESS, large_amount)` — approval stored in old ERC-20 contract.
2. Owner calls `factory_set_wnear_address(new_wnear_address)` — engine now reads `new_wnear_address` from storage.
3. User submits an XCC transaction targeting the XCC precompile.
4. XCC precompile reads `get_wnear_address(&self.io)` → returns `new_wnear_address`.
5. Precompile calls `transferFrom(user, engine_implicit, amount)` on `new_wnear_address`.
6. `new_wnear_address.allowance(user, cross_contract_call::ADDRESS) == 0` → `transferFrom` reverts.
7. XCC precompile propagates the revert; user's cross-contract call fails.
8. All XCC calls for all existing users fail identically until each user re-approves on the new contract. [5](#0-4) [3](#0-2)

### Citations

**File:** engine-precompiles/src/xcc.rs (L188-216)
```rust
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

**File:** engine-tests/src/tests/xcc.rs (L1008-1029)
```rust
    /// The signer approves the XCC precompile to spend its wrapped NEAR
    async fn approve_xcc_precompile(
        wnear_erc20: &ERC20,
        aurora: &EngineContract,
        chain_id: u64,
        signer: &mut utils::Signer,
    ) -> anyhow::Result<()> {
        let approve_tx = wnear_erc20.approve(
            cross_contract_call::ADDRESS,
            WNEAR_AMOUNT.as_yoctonear().into(),
            signer.use_nonce().into(),
        );
        let signed_transaction =
            utils::sign_transaction(approve_tx, Some(chain_id), &signer.secret_key);
        let result = aurora
            .submit(rlp::encode(&signed_transaction).to_vec())
            .transact()
            .await?;
        if !result.is_success() {
            return Err(anyhow::Error::msg("Failed Approve transaction"));
        }
        Ok(())
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

### Title
Missing Zero-Address Check in `factory_set_wnear_address` Allows Owner to Silently Brick All XCC Operations - (File: engine/src/contract_methods/xcc.rs)

### Summary
The `factory_set_wnear_address` function in the Aurora Engine accepts and persists any 20-byte value as the wNEAR ERC-20 address, including the zero address (`0x0000...0000`), without any validation. If the owner accidentally supplies the zero address, every subsequent XCC operation that requires NEAR (i.e., any call that must deploy or upgrade a router sub-account) will panic inside `handle_precompile_promise`, temporarily freezing all cross-contract call functionality for every user of the engine.

### Finding Description

`factory_set_wnear_address` is the owner-controlled setter for the wNEAR ERC-20 contract address used by the XCC subsystem:

```rust
// engine/src/contract_methods/xcc.rs  lines 103-115
pub fn factory_set_wnear_address<I: IO + Copy, E: Env>(
    io: I,
    env: &E,
) -> Result<(), ContractError> {
    with_hashchain(io, env, function_name!(), |mut io| {
        let state = state::get_state(&io)?;
        require_running(&state)?;
        require_owner_only(&state, &env.predecessor_account_id())?;
        let address = io.read_input_arr20()?;          // ← any 20 bytes accepted
        xcc::set_wnear_address(&mut io, &Address::from_array(address)); // ← no zero check
        Ok(())
    })
}
``` [1](#0-0) 

The underlying storage writer is equally unchecked:

```rust
// engine/src/xcc.rs  lines 370-373
pub fn set_wnear_address<I: IO>(io: &mut I, address: &Address) {
    let key = storage::bytes_to_key(KeyPrefix::CrossContractCall, WNEAR_KEY);
    io.write_storage(&key, address.as_bytes());   // ← zero address written verbatim
}
``` [2](#0-1) 

Every time a user initiates an XCC call that requires deploying or upgrading a router sub-account, `handle_precompile_promise` reads the stored wNEAR address and immediately performs a hard `.expect()` lookup:

```rust
// engine/src/xcc.rs  lines 217-220
let wnear_address = get_wnear_address(io);          // returns Address::zero()
let wnear_account = crate::engine::nep141_erc20_map(*io)
    .lookup_right(&crate::engine::ERC20Address(wnear_address))
    .expect("wnear account not found");             // ← panics; no ERC-20 at zero address
``` [3](#0-2) 

A NEAR contract panic aborts the entire transaction and reverts state, so the XCC precompile becomes permanently non-functional until the owner issues a corrective `factory_set_wnear_address` call. During that window every user-initiated XCC transaction that triggers a router deploy or upgrade will revert.

Additionally, `withdraw_wnear_to_router` (called for any XCC operation that needs NEAR attached) passes the zero address directly as the EVM contract to call:

```rust
// engine/src/xcc.rs  lines 382-393
pub fn withdraw_wnear_to_router<...>(
    ...
    wnear_address: Address,   // Address::zero() if misconfigured
    engine: &mut Engine<I, E, M>,
    ...
) -> EngineResult<...> {
    let withdraw_call_args = withdraw_wnear_call_args(recipient, amount, wnear_address);
    let result = engine.call_with_args(withdraw_call_args, &mut interceptor)?;
    ...
}
``` [4](#0-3) 

Calling the zero address in the EVM succeeds as a no-op (no code there), so no wNEAR is actually withdrawn, the router never receives NEAR, and the XCC call silently produces a wrong result or reverts downstream.

### Impact Explanation

**High — Temporary freezing of funds.**

All XCC operations that require NEAR (router creation, router upgrade, or any call with `required_near > 0`) will revert for every user of the engine until the owner issues a corrective transaction. Users cannot execute cross-contract calls, and any in-flight XCC workflows are stalled. User EVM balances (ETH, ERC-20 tokens) are not permanently lost because the NEAR panic reverts state, but the XCC subsystem is completely non-functional during the misconfiguration window.

### Likelihood Explanation

Low-to-medium. The owner must supply an all-zero 20-byte input to `factory_set_wnear_address`. This is a realistic operational mistake (e.g., a deployment script that passes an uninitialized variable, a copy-paste error, or a Borsh-encoded zero value). The original Trail of Bits report identifies exactly this class of accidental misconfiguration as a realistic exploit scenario. There is no on-chain guard that would catch the mistake before it is committed to storage.

### Recommendation

Add an explicit zero-address guard at the top of `factory_set_wnear_address`:

```rust
let address = io.read_input_arr20()?;
if address == [0u8; 20] {
    return Err(b"ERR_INVALID_WNEAR_ADDRESS".into());
}
xcc::set_wnear_address(&mut io, &Address::from_array(address));
```

Optionally, add the same guard inside `set_wnear_address` itself so the invariant is enforced at the storage layer regardless of the call site.

### Proof of Concept

1. Owner calls `factory_set_wnear_address` with input `[0u8; 20]` (all-zero address).
2. `set_wnear_address` writes `Address::zero()` to storage under `WNEAR_KEY` with no error.
3. Any user submits an EVM transaction that triggers the XCC precompile for a new or outdated router sub-account.
4. `handle_precompile_promise` calls `get_wnear_address` → returns `Address::zero()`.
5. `nep141_erc20_map(*io).lookup_right(&ERC20Address(Address::zero()))` returns `None`.
6. `.expect("wnear account not found")` panics → NEAR runtime aborts the transaction.
7. All XCC calls that need router deployment or NEAR withdrawal revert until the owner corrects the stored address. [5](#0-4) [2](#0-1) [6](#0-5)

### Citations

**File:** engine/src/contract_methods/xcc.rs (L103-115)
```rust
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

**File:** engine/src/xcc.rs (L215-221)
```rust
            let code = get_router_code(io).0.into_owned();
            // After the deployment we will call the contract's initialize function
            let wnear_address = get_wnear_address(io);
            let wnear_account = crate::engine::nep141_erc20_map(*io)
                .lookup_right(&crate::engine::ERC20Address(wnear_address))
                .expect("wnear account not found");
            let init_args = format!(
```

**File:** engine/src/xcc.rs (L370-373)
```rust
pub fn set_wnear_address<I: IO>(io: &mut I, address: &Address) {
    let key = storage::bytes_to_key(KeyPrefix::CrossContractCall, WNEAR_KEY);
    io.write_storage(&key, address.as_bytes());
}
```

**File:** engine/src/xcc.rs (L382-393)
```rust
pub fn withdraw_wnear_to_router<I: IO + Copy, E: Env, M: ModExpAlgorithm, H: PromiseHandler>(
    recipient: &AccountId,
    amount: Yocto,
    wnear_address: Address,
    engine: &mut Engine<I, E, M>,
    handler: &mut H,
) -> EngineResult<(SubmitResult, Vec<PromiseId>)> {
    let mut interceptor = PromiseInterceptor::new(handler);
    let withdraw_call_args = withdraw_wnear_call_args(recipient, amount, wnear_address);
    let result = engine.call_with_args(withdraw_call_args, &mut interceptor)?;
    Ok((result, interceptor.promises))
}
```

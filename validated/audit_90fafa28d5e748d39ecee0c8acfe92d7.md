### Title
Unauthorized Access to `deploy_erc20_token` Allows Any Caller to Register Arbitrary NEP-141 ↔ ERC-20 Mappings - (File: engine/src/contract_methods/connector.rs)

### Summary

The `deploy_erc20_token` function in `engine/src/contract_methods/connector.rs` is exposed as a public NEAR contract method with no caller restriction. Any NEAR account can invoke it to deploy an ERC-20 token and register a NEP-141 ↔ ERC-20 mapping in the Aurora engine's token registry. The code's own comment acknowledges the function "could be executed by the owner of the contract only," but no such check is implemented. This is a direct analog to the reported `proposeUpdateTransmitters` auth bypass.

### Finding Description

The `deploy_erc20_token` function performs only a liveness check (`require_running`) before proceeding to deploy an ERC-20 contract in the Aurora EVM and permanently register the NEP-141 ↔ ERC-20 mapping via `engine.register_token(address, nep141)`. [1](#0-0) 

The `Legacy` variant path calls `engine::deploy_erc20_token` directly with no owner check: [2](#0-1) 

The `WithMetadata` variant path contains a comment explicitly stating the function should be owner-restricted, but no such restriction is enforced: [3](#0-2) 

The function is exposed as an unrestricted public NEAR contract entry point: [4](#0-3) 

Contrast this with other privileged functions in the same codebase that correctly enforce `require_owner_only` or `require_owner_and_running`: [5](#0-4) 

The internal `engine::deploy_erc20_token` function deploys ERC-20 bytecode and calls `engine.register_token(address, nep141)` to write the permanent NEP-141 ↔ ERC-20 mapping into storage: [6](#0-5) 

### Impact Explanation

An unprivileged attacker can:

1. **Pre-register any NEP-141 token** before the legitimate bridge operator does. Once a NEP-141 ↔ ERC-20 mapping is written, a subsequent legitimate `deploy_erc20_token` call for the same NEP-141 will fail (duplicate registration). This permanently blocks the correct ERC-20 from being deployed for that token, freezing the bridging path for all users of that token.

2. **Corrupt the token registry** by registering arbitrary NEP-141 account IDs (including non-existent or attacker-controlled ones) against freshly deployed ERC-20 contracts. Users who subsequently bridge via `ft_on_transfer` will have their tokens credited to the wrong ERC-20 or fail entirely, causing permanent loss of bridged funds.

The `ft_on_transfer` path uses the stored NEP-141 ↔ ERC-20 mapping to credit tokens: [7](#0-6) 

If the mapping is corrupted, incoming bridge transfers for the affected NEP-141 token will be misrouted or rejected, constituting a permanent freeze of those bridged funds.

### Likelihood Explanation

The attack requires no privileged access, no special role, and no tokens. Any NEAR account can call `deploy_erc20_token` with a crafted `DeployErc20TokenArgs::Legacy(target_nep141)` payload. The call costs only standard NEAR gas fees. The attacker can target any NEP-141 token that has not yet been bridged to Aurora, including high-value tokens anticipated to be bridged in the future.

### Recommendation

Add a `require_owner_only` check at the top of `deploy_erc20_token`, consistent with the existing pattern used by all other privileged administrative functions:

```rust
pub fn deploy_erc20_token<I: IO + Copy, E: Env, H: PromiseHandler>(
    io: I,
    env: &E,
    handler: &mut H,
) -> Result<PromiseOrValue<Address>, ContractError> {
    with_hashchain(io, env, function_name!(), |mut io| {
        let state = state::get_state(&io)?;
        require_running(&state)?;
        require_owner_only(&state, &env.predecessor_account_id())?; // ADD THIS
        ...
    })
}
```

This matches the pattern already used by `factory_update`, `factory_set_wnear_address`, `mirror_erc20_token`, and other privileged functions. [8](#0-7) 

### Proof of Concept

1. Identify a high-value NEP-141 token (`target.near`) not yet bridged to Aurora.
2. From any NEAR account, call the Aurora engine's `deploy_erc20_token` method with `DeployErc20TokenArgs::Legacy("target.near")`.
3. The call succeeds: an ERC-20 is deployed in the Aurora EVM and the NEP-141 ↔ ERC-20 mapping is written to storage.
4. The legitimate bridge operator subsequently calls `deploy_erc20_token` for `target.near` — this fails because the mapping already exists.
5. All users attempting to bridge `target.near` tokens to Aurora via `ft_on_transfer` are now permanently blocked or misrouted, freezing their funds.

### Citations

**File:** engine/src/contract_methods/connector.rs (L80-90)
```rust
        let args: FtOnTransferArgs = read_json_args(&io)?;
        let result = if predecessor_account_id == get_connector_account_id(&io)? {
            engine.receive_base_tokens(&args)
        } else {
            engine.receive_erc20_tokens(
                &predecessor_account_id,
                &args,
                &current_account_id,
                handler,
            )
        };
```

**File:** engine/src/contract_methods/connector.rs (L112-121)
```rust
pub fn deploy_erc20_token<I: IO + Copy, E: Env, H: PromiseHandler>(
    io: I,
    env: &E,
    handler: &mut H,
) -> Result<PromiseOrValue<Address>, ContractError> {
    with_hashchain(io, env, function_name!(), |mut io| {
        require_running(&state::get_state(&io)?)?;
        let bytes = io.read_input().to_vec();
        let args =
            DeployErc20TokenArgs::deserialize(&bytes).map_err(|_| errors::ERR_BORSH_DESERIALIZE)?;
```

**File:** engine/src/contract_methods/connector.rs (L123-131)
```rust
        match args {
            DeployErc20TokenArgs::Legacy(nep141) => {
                let address = engine::deploy_erc20_token(nep141, None, io, env, handler)?;

                io.return_output(
                    &borsh::to_vec(address.as_bytes()).map_err(|_| errors::ERR_SERIALIZE)?,
                );
                Ok(PromiseOrValue::Value(address))
            }
```

**File:** engine/src/contract_methods/connector.rs (L148-156)
```rust
                // Safe because these promises are read-only calls to the main engine contract
                // and this transaction could be executed by the owner of the contract only.
                let promise_args = PromiseWithCallbackArgs { base, callback };
                let promise_id = handler.promise_create_with_callback(&promise_args);

                handler.promise_return(promise_id);

                Ok(PromiseOrValue::Promise(promise_args))
            }
```

**File:** engine/src/lib.rs (L613-621)
```rust
    #[unsafe(no_mangle)]
    pub extern "C" fn deploy_erc20_token() {
        let io = Runtime;
        let env = Runtime;
        let mut handler = Runtime;
        contract_methods::connector::deploy_erc20_token(io, &env, &mut handler)
            .map_err(ContractError::msg)
            .sdk_unwrap();
    }
```

**File:** engine/src/contract_methods/mod.rs (L79-87)
```rust
pub fn require_owner_only(
    state: &state::EngineState,
    predecessor_account_id: &AccountId,
) -> Result<(), ContractError> {
    if &state.owner_id != predecessor_account_id {
        return Err(errors::ERR_NOT_ALLOWED.into());
    }
    Ok(())
}
```

**File:** engine/src/engine.rs (L1340-1375)
```rust
pub fn deploy_erc20_token<I: IO + Copy, E: Env, P: PromiseHandler>(
    nep141: AccountId,
    metadata: Option<Erc20Metadata>,
    io: I,
    env: &E,
    handler: &mut P,
) -> Result<Address, DeployErc20Error> {
    let current_account_id = env.current_account_id();
    let input = setup_deploy_erc20_input(&current_account_id, metadata);
    let mut engine: Engine<_, _> = Engine::new(
        aurora_engine_sdk::types::near_account_to_evm_address(
            env.predecessor_account_id().as_bytes(),
        ),
        current_account_id,
        io,
        env,
    )
    .map_err(DeployErc20Error::State)?;

    let address = match engine.deploy_code_with_input(input, None, handler) {
        Ok(result) => match result.status {
            TransactionStatus::Succeed(ret) => {
                Address::new(H160(ret.as_slice().try_into().unwrap()))
            }
            other => return Err(DeployErc20Error::Failed(other)),
        },
        Err(e) => return Err(DeployErc20Error::Engine(e)),
    };

    sdk::log!("Deployed ERC-20 in Aurora at: {:#?}", address);
    engine
        .register_token(address, nep141)
        .map_err(DeployErc20Error::Register)?;

    Ok(address)
}
```

**File:** engine/src/contract_methods/xcc.rs (L68-78)
```rust
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
}
```

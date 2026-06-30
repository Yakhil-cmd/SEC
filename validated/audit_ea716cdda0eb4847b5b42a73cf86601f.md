### Title
`deploy_erc20_token()` Lacks Access Control, Allowing Anyone to Deploy ERC-20 Tokens for Arbitrary NEP-141 Accounts - (File: engine/src/contract_methods/connector.rs)

### Summary
The `deploy_erc20_token()` function is callable by any NEAR account without owner-level access control. It accepts an arbitrary, unvalidated NEP-141 account ID as input, allowing an attacker to deploy an ERC-20 token backed by a malicious NEP-141 contract. A developer comment inside the function explicitly states "this transaction could be executed by the owner of the contract only," but no such restriction is enforced in code.

### Finding Description
`deploy_erc20_token()` at lines 112–159 of `engine/src/contract_methods/connector.rs` only checks `require_running` and performs no caller authentication:

```rust
pub fn deploy_erc20_token<I: IO + Copy, E: Env, H: PromiseHandler>(
    io: I,
    env: &E,
    handler: &mut H,
) -> Result<PromiseOrValue<Address>, ContractError> {
    with_hashchain(io, env, function_name!(), |mut io| {
        require_running(&state::get_state(&io)?)?;
        // ← No require_owner_only here
        let bytes = io.read_input().to_vec();
        let args = DeployErc20TokenArgs::deserialize(&bytes)
            .map_err(|_| errors::ERR_BORSH_DESERIALIZE)?;
        match args {
            DeployErc20TokenArgs::Legacy(nep141) => {
                let address = engine::deploy_erc20_token(nep141, None, io, env, handler)?;
                ...
            }
            DeployErc20TokenArgs::WithMetadata(nep141) => {
                ...
                // Safe because these promises are read-only calls to the main engine contract
                // and this transaction could be executed by the owner of the contract only.
                let promise_args = PromiseWithCallbackArgs { base, callback };
                ...
            }
        }
    })
}
``` [1](#0-0) 

The comment at line 148–149 is a safety justification that assumes owner-only access, but the assumption is never enforced. Compare this to every other state-mutating function in the same file and in `admin.rs`, all of which call `require_owner_only` or `env.assert_private_call()`:

- `set_eth_connector_contract_account` — enforces `require_owner_only` or `assert_private_call` [2](#0-1) 
- `set_erc20_metadata` — enforces `require_owner_only` or `assert_private_call` [3](#0-2) 
- `mirror_erc20_token` — enforces `require_owner_only` [4](#0-3) 

`deploy_erc20_token` is the sole state-writing connector function that omits this guard entirely.

The function accepts two variants:
- `Legacy(nep141)` — directly calls `engine::deploy_erc20_token(nep141, None, ...)` with the attacker-supplied account ID.
- `WithMetadata(nep141)` — issues a cross-contract call to `ft_metadata` on the attacker-supplied account, then registers the ERC-20 via `deploy_erc20_token_callback`. The callback is protected by `assert_private_call`, but the initial dispatch is not protected at all. [5](#0-4) 

In both paths, the NEP-141 account ID is taken verbatim from calldata with no whitelist check, no registry lookup, and no validation that the account is a legitimate fungible token.

### Impact Explanation
An attacker who controls a malicious NEP-141 contract can call `deploy_erc20_token` and register it as the backing asset for a new ERC-20 on Aurora. If the internal `engine::deploy_erc20_token` permits overwriting an existing NEP-141→ERC-20 mapping (a common pattern in token registries that do not guard against re-registration), the attacker can redirect an already-deployed, user-held ERC-20 to their malicious NEP-141 contract. Any subsequent `ft_on_transfer` or withdrawal that routes through the connector will interact with the attacker's contract, enabling direct theft of bridged user funds. Even if overwriting is blocked, the attacker can deploy a lookalike ERC-20 for a spoofed NEP-141 account, permanently trapping any funds deposited into it. Both scenarios fall within the Critical impact tier (direct theft or permanent freeze of user funds).

### Likelihood Explanation
High. The entry point is a standard NEAR contract call requiring no special role, no attached deposit, and no prior state. Any NEAR account — including a freshly created one — can invoke it at any time while the engine is running. The attacker only needs to deploy a malicious NEP-141 contract (trivial on NEAR) and submit a single transaction.

### Recommendation
Add `require_owner_only(&state, &env.predecessor_account_id())?;` immediately after `require_running` in `deploy_erc20_token`, consistent with the developer's stated intent and with every other state-mutating function in the connector module. If permissionless deployment is desired in the future, introduce a validated NEP-141 whitelist (analogous to the `is4PoolGauge` mapping in the mitigated report) rather than removing the guard entirely.

### Proof of Concept
1. Attacker creates NEAR account `evil.near` and deploys a malicious NEP-141 contract that returns arbitrary balances and accepts arbitrary transfers.
2. Attacker calls `deploy_erc20_token` on the Aurora Engine with `DeployErc20TokenArgs::Legacy(AccountId::from("evil.near"))`.
3. The engine, lacking any caller check, executes `engine::deploy_erc20_token("evil.near", None, ...)` and registers a new ERC-20 address mapped to `evil.near`.
4. If the registry allows re-registration of an existing NEP-141 (e.g., `usdc.near`), the attacker repeats step 2 with `evil.near` impersonating `usdc.near`, overwriting the stored ERC-20 address.
5. All subsequent Aurora users who call `ft_on_transfer` or `withdraw` for the affected ERC-20 now interact with `evil.near`, which can drain or freeze their tokens.

### Citations

**File:** engine/src/contract_methods/connector.rs (L112-159)
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

        match args {
            DeployErc20TokenArgs::Legacy(nep141) => {
                let address = engine::deploy_erc20_token(nep141, None, io, env, handler)?;

                io.return_output(
                    &borsh::to_vec(address.as_bytes()).map_err(|_| errors::ERR_SERIALIZE)?,
                );
                Ok(PromiseOrValue::Value(address))
            }
            DeployErc20TokenArgs::WithMetadata(nep141) => {
                let args = borsh::to_vec(&nep141).map_err(|_| errors::ERR_SERIALIZE)?;
                let base = PromiseCreateArgs {
                    target_account_id: nep141,
                    method: "ft_metadata".to_string(),
                    args: vec![],
                    attached_balance: ZERO_YOCTO,
                    attached_gas: READ_PROMISE_ATTACHED_GAS,
                };
                let callback = PromiseCreateArgs {
                    target_account_id: env.current_account_id(),
                    method: "deploy_erc20_token_callback".to_string(),
                    args,
                    attached_balance: ZERO_YOCTO,
                    attached_gas: DEPLOY_ERC20_TOKEN_CALLBACK_ATTACHED_GAS,
                };
                // Safe because these promises are read-only calls to the main engine contract
                // and this transaction could be executed by the owner of the contract only.
                let promise_args = PromiseWithCallbackArgs { base, callback };
                let promise_id = handler.promise_create_with_callback(&promise_args);

                handler.promise_return(promise_id);

                Ok(PromiseOrValue::Promise(promise_args))
            }
        }
    })
}
```

**File:** engine/src/contract_methods/connector.rs (L368-397)
```rust
#[named]
pub fn set_erc20_metadata<I: IO + Copy, E: Env, H: PromiseHandler>(
    io: I,
    env: &E,
    handler: &mut H,
) -> Result<SubmitResult, ContractError> {
    with_hashchain(io, env, function_name!(), |io| {
        let state = state::get_state(&io)?;
        require_running(&state)?;
        // TODO: Define special role for this transaction. Potentially via multisig?
        let is_private = env.assert_private_call();
        if is_private.is_err() {
            require_owner_only(&state, &env.predecessor_account_id())?;
        }

        let args: SetErc20MetadataArgs = serde_json::from_slice(&io.read_input().to_vec())
            .map_err(Into::<ParseArgsError>::into)?;
        let current_account_id = env.current_account_id();
        let mut engine: Engine<_, E, AuroraModExp> = Engine::new_with_state(
            state,
            predecessor_address(&current_account_id),
            current_account_id,
            io,
            env,
        );
        let result = engine.set_erc20_metadata(&args.erc20_identifier, args.metadata, handler)?;

        Ok(result)
    })
}
```

**File:** engine/src/contract_methods/connector.rs (L418-438)
```rust
pub fn set_eth_connector_contract_account<I: IO + Copy, E: Env>(
    io: I,
    env: &E,
) -> Result<(), ContractError> {
    with_hashchain(io, env, function_name!(), |io| {
        let state = state::get_state(&io)?;
        require_running(&state)?;
        let is_private = env.assert_private_call();

        if is_private.is_err() {
            require_owner_only(&state, &env.predecessor_account_id())?;
        }

        let args: SetEthConnectorContractAccountArgs = io.read_input_borsh()?;

        set_connector_account_id(io, &args.account);
        set_connector_withdraw_serialization_type(io, &args.withdraw_serialize_type);

        Ok(())
    })
}
```

**File:** engine/src/contract_methods/connector.rs (L456-463)
```rust
pub fn mirror_erc20_token<I: IO + Env + Copy, H: PromiseHandler>(
    io: I,
    handler: &mut H,
) -> Result<(), ContractError> {
    let state = state::get_state(&io)?;
    require_running(&state)?;
    // TODO: Add an admin access list of accounts allowed to do it.
    require_owner_only(&state, &io.predecessor_account_id())?;
```

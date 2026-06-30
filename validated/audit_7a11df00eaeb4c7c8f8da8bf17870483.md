### Title
Unauthorized ERC-20 Token Registration via Missing Access Control in `deploy_erc20_token` — (`engine/src/contract_methods/connector.rs`)

---

### Summary

The `deploy_erc20_token` NEAR contract method is publicly callable with no access control. Any unprivileged NEAR account can invoke it with an arbitrary NEP-141 account ID, permanently registering a NEP-141 → ERC-20 mapping in the engine's storage. Because `register_token` enforces a one-time-write invariant (`TokenAlreadyRegistered`), a successful front-run permanently blocks the legitimate deployment for that token.

---

### Finding Description

`deploy_erc20_token` is exposed as a `#[no_mangle]` NEAR contract entry point in `engine/src/lib.rs` and dispatches to `contract_methods::connector::deploy_erc20_token`. [1](#0-0) 

The implementation performs only a liveness check (`require_running`) and then immediately proceeds to deploy an ERC-20 contract and write the bidirectional NEP-141 ↔ ERC-20 mapping into storage: [2](#0-1) 

The comment in the `WithMetadata` branch explicitly acknowledges the intended restriction ("this transaction could be executed by the owner of the contract only") but no `require_owner_only` call is present in either branch: [3](#0-2) 

The underlying `engine::deploy_erc20_token` deploys the ERC-20 bytecode and calls `register_token`, which enforces a strict one-time-write: if a mapping already exists for the given NEP-141, it returns `TokenAlreadyRegistered` and reverts: [4](#0-3) [5](#0-4) 

Once the mapping is written, it is permanent. There is no administrative function to overwrite or delete a NEP-141 → ERC-20 entry.

---

### Impact Explanation

**Critical — Permanent freezing of funds / Permanent DoS on token bridging.**

An attacker who front-runs the legitimate `deploy_erc20_token` call for any NEP-141 token (e.g., `usdc.near`, `wrap.near`) permanently occupies the mapping slot. All subsequent legitimate deployment attempts fail with `TokenAlreadyRegistered`. The NEP-141 token is then permanently bound to the attacker-triggered ERC-20 contract at an unexpected address. Users who bridge that NEP-141 token via `ft_on_transfer` receive minted ERC-20 tokens on the attacker-chosen contract address, while the canonical ERC-20 address expected by wallets, DEXes, and integrators is never deployed. Funds bridged into the attacker-registered ERC-20 are effectively stranded from the ecosystem's perspective, and the legitimate token can never be properly registered on Aurora. [6](#0-5) 

---

### Likelihood Explanation

**High.** The entry point is unconditionally public on mainnet. Any NEAR account can call `deploy_erc20_token` with any NEP-141 account ID at any time. No deposit, staking, or special key is required. An attacker monitoring the NEAR mempool for pending `deploy_erc20_token` transactions can trivially front-run them, or can pre-emptively register any token that has not yet been bridged. [7](#0-6) 

---

### Recommendation

Add `require_owner_only` (or an equivalent authorized-caller check) at the top of `deploy_erc20_token`, consistent with the pattern used by every other state-mutating administrative method in the engine:

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
        // ...
    })
}
``` [8](#0-7) 

---

### Proof of Concept

1. Observe that `usdc.near` has not yet been registered on Aurora.
2. As an unprivileged NEAR account, call:
   ```
   aurora.deploy_erc20_token(DeployErc20TokenArgs::Legacy("usdc.near"))
   ```
3. The engine deploys an ERC-20 contract and writes the `usdc.near → <attacker-triggered ERC-20 address>` mapping into storage.
4. The Aurora team subsequently attempts the legitimate deployment:
   ```
   aurora.deploy_erc20_token(DeployErc20TokenArgs::Legacy("usdc.near"))
   ```
5. `register_token` returns `TokenAlreadyRegistered`; the call fails.
6. `usdc.near` is permanently bound to the attacker-triggered ERC-20 address. All future `ft_on_transfer` calls from `usdc.near` mint tokens on that contract. The canonical deployment is permanently blocked. [9](#0-8) [10](#0-9)

### Citations

**File:** engine/src/lib.rs (L612-621)
```rust
    /// Deploy ERC20 token mapped to a NEP141
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

**File:** engine/src/contract_methods/connector.rs (L61-109)
```rust
#[named]
pub fn ft_on_transfer<I: IO + Copy, E: Env, H: PromiseHandler>(
    io: I,
    env: &E,
    handler: &mut H,
) -> Result<Option<SubmitResult>, ContractError> {
    with_hashchain(io, env, function_name!(), |mut io| {
        require_running(&state::get_state(&io)?)?;
        let current_account_id = env.current_account_id();
        let predecessor_account_id = env.predecessor_account_id();
        let mut engine: Engine<_, _> = Engine::new(
            predecessor_address(&predecessor_account_id),
            current_account_id.clone(),
            io,
            env,
        )?;

        sdk::log!("Call ft_on_transfer");

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

        #[allow(clippy::used_underscore_binding)]
        let amount_to_return = if let Err(_err) = &result {
            sdk::log!("Error in ft_on_transfer: {_err:?}");
            // An error occurred, so we need to return the amount of tokens to the sender.
            args.amount.as_u128()
        } else {
            // Everything is ok, so return 0.
            0
        };

        let output = crate::prelude::format!("\"{amount_to_return}\"");
        io.return_output(output.as_bytes());

        // In case of an error, we just return Ok(None) to avoid a panic in the contract. It's ok
        // because in case of an error, we already returned the amount of tokens to the sender.
        Ok(result.unwrap_or(None))
    })
}
```

**File:** engine/src/contract_methods/connector.rs (L111-131)
```rust
#[named]
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
```

**File:** engine/src/contract_methods/connector.rs (L132-158)
```rust
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
```

**File:** engine/src/engine.rs (L722-741)
```rust
    pub fn register_token(
        &mut self,
        erc20_token: Address,
        nep141_token: AccountId,
    ) -> Result<(), RegisterTokenError> {
        match get_erc20_from_nep141(&self.io, &nep141_token) {
            Err(GetErc20FromNep141Error::Nep141NotFound) => (),
            Err(GetErc20FromNep141Error::InvalidNep141AccountId) => {
                return Err(RegisterTokenError::InvalidNep141AccountId);
            }
            Err(GetErc20FromNep141Error::InvalidAddress) => {
                return Err(RegisterTokenError::InvalidAddress);
            }
            Ok(_) => return Err(RegisterTokenError::TokenAlreadyRegistered),
        }

        let erc20_token = ERC20Address(erc20_token);
        let nep141_token = NEP141Account(nep141_token);
        nep141_erc20_map(self.io).insert(&nep141_token, &erc20_token);
        Ok(())
```

**File:** engine/src/engine.rs (L1340-1374)
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
```

**File:** engine/src/engine.rs (L1498-1510)
```rust
pub fn get_erc20_from_nep141<I: IO>(
    io: &I,
    nep141_account_id: &AccountId,
) -> Result<Address, GetErc20FromNep141Error> {
    let key = bytes_to_key(KeyPrefix::Nep141Erc20Map, nep141_account_id.as_bytes());
    io.read_storage(&key)
        .map(|v| {
            let mut buf = [0u8; 20];
            v.copy_to_slice(&mut buf);
            Address::from_array(buf)
        })
        .ok_or(GetErc20FromNep141Error::Nep141NotFound)
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

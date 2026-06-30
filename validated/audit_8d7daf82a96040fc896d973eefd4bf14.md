### Title
Unbacked ERC-20 Token Minting via Direct `ft_on_transfer` Call Without Transfer Verification — (File: engine/src/contract_methods/connector.rs)

---

### Summary

The `ft_on_transfer` entrypoint on Aurora Engine accepts an attacker-supplied `amount` field and mints ERC-20 tokens equal to that value without verifying that any NEP-141 tokens were actually transferred to Aurora. Because `deploy_erc20_token` is also unrestricted, any NEAR account can register itself as a NEP-141 token and then call `ft_on_transfer` directly to mint an arbitrary quantity of ERC-20 tokens on Aurora with no backing.

---

### Finding Description

`ft_on_transfer` is the NEP-141 standard callback Aurora uses to credit ERC-20 tokens when a user bridges a NEP-141 token via `ft_transfer_call`. The intended flow is:

```
User → ft_transfer_call (NEP-141 token) → tokens transferred to Aurora → NEP-141 calls ft_on_transfer on Aurora
```

The implementation in `engine/src/contract_methods/connector.rs` is:

```rust
pub fn ft_on_transfer<I: IO + Copy, E: Env, H: PromiseHandler>(
    io: I, env: &E, handler: &mut H,
) -> Result<Option<SubmitResult>, ContractError> {
    with_hashchain(io, env, function_name!(), |mut io| {
        require_running(&state::get_state(&io)?)?;
        let current_account_id = env.current_account_id();
        let predecessor_account_id = env.predecessor_account_id();
        ...
        let args: FtOnTransferArgs = read_json_args(&io)?;   // amount is caller-supplied
        let result = if predecessor_account_id == get_connector_account_id(&io)? {
            engine.receive_base_tokens(&args)
        } else {
            engine.receive_erc20_tokens(
                &predecessor_account_id,   // used as the NEP-141 token identity
                &args,                     // args.amount is fully attacker-controlled
                &current_account_id,
                handler,
            )
        };
``` [1](#0-0) 

`receive_erc20_tokens` then mints exactly `args.amount` ERC-20 tokens:

```rust
pub fn receive_erc20_tokens<P: PromiseHandler>(
    &mut self, token: &AccountId, args: &FtOnTransferArgs, ...
) -> Result<Option<SubmitResult>, ContractError> {
    let amount = args.amount.as_u128();   // taken verbatim from caller input
    ...
    let erc20_token = get_erc20_from_nep141(&self.io, token)?;
    ...
    setup_receive_erc20_tokens_input(&recipient, amount)  // mints `amount` tokens
``` [2](#0-1) 

The function performs **no check** that:
- The call arrived as a callback from a legitimate `ft_transfer_call` (no `promise_results_count` guard)
- Any NEP-141 tokens were actually transferred to Aurora before this call
- `args.amount` is bounded by any real on-chain transfer

The prerequisite — having a registered NEP-141 → ERC-20 mapping — is trivially satisfied because `deploy_erc20_token` has no access-control guard beyond `require_running`:

```rust
pub fn deploy_erc20_token<I: IO + Copy, E: Env, H: PromiseHandler>(
    io: I, env: &E, handler: &mut H,
) -> Result<PromiseOrValue<Address>, ContractError> {
    with_hashchain(io, env, function_name!(), |mut io| {
        require_running(&state::get_state(&io)?)?;
        // No owner check — any caller may register any NEP-141 account ID
``` [3](#0-2) 

The public WASM entrypoint exposes both methods without restriction: [4](#0-3) 

---

### Impact Explanation

An attacker who registers their own NEAR account as a NEP-141 token on Aurora can call `ft_on_transfer` directly — with `predecessor_account_id` equal to their registered account — and pass any `amount` value. Aurora will mint that many ERC-20 tokens to the attacker's chosen EVM address with no corresponding NEP-141 token backing. These unbacked ERC-20 tokens can then be:

- Swapped on Aurora DEXes for legitimate bridged assets (ETH, USDC, etc.), draining liquidity providers
- Deposited as collateral in Aurora lending protocols to borrow real assets without repayment
- Sold to users who do not independently verify the token contract address

This constitutes **direct theft of user funds** held in Aurora DeFi protocols.

---

### Likelihood Explanation

The attack requires only two NEAR transactions, both callable by any unprivileged account:

1. `deploy_erc20_token` — registers the attacker's account as a NEP-141 token (no permission check)
2. `ft_on_transfer` — called directly by the attacker's account with an inflated `amount`

No admin access, no leaked keys, and no governance capture are required. The entry path is fully reachable by any NEAR account holder.

---

### Recommendation

Add a guard inside `ft_on_transfer` (or inside `receive_erc20_tokens`) that verifies the call is arriving as a genuine callback from a `ft_transfer_call` promise chain. In NEAR, this can be enforced by checking `handler.promise_results_count() > 0` and that the promise result is `Successful`. Alternatively, restrict `deploy_erc20_token` to the contract owner or a governance-controlled whitelist so that only audited NEP-141 tokens can be registered, reducing the attack surface.

---

### Proof of Concept

```
1. Attacker creates NEAR account `evil.near`.

2. Attacker calls `deploy_erc20_token` on Aurora with input = borsh(evil.near).
   → Aurora deploys an ERC-20 contract and records the mapping:
     evil.near ↔ <erc20_address>

3. Attacker calls `ft_on_transfer` on Aurora directly
   (predecessor_account_id = evil.near, no actual ft_transfer_call involved)
   with JSON args:
     { "sender_id": "evil.near",
       "amount": "1000000000000000000000000",
       "msg": "<attacker_evm_address_hex>" }

4. Aurora executes:
   - predecessor_account_id ("evil.near") ≠ connector_account_id → ERC-20 branch
   - get_erc20_from_nep141("evil.near") → succeeds (registered in step 2)
   - setup_receive_erc20_tokens_input(attacker_address, 1e24) → mint call
   - ERC-20 contract mints 1e24 tokens to attacker's EVM address

5. Attacker swaps the minted ERC-20 tokens on an Aurora DEX for ETH/USDC,
   draining the liquidity pool.
```

### Citations

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

**File:** engine/src/contract_methods/connector.rs (L111-159)
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

**File:** engine/src/engine.rs (L796-837)
```rust
    pub fn receive_erc20_tokens<P: PromiseHandler>(
        &mut self,
        token: &AccountId,
        args: &FtOnTransferArgs,
        current_account_id: &AccountId,
        handler: &mut P,
    ) -> Result<Option<SubmitResult>, ContractError> {
        let amount = args.amount.as_u128();
        // Parse message to determine recipient
        let mut recipient = {
            // The message should contain the recipient EOA address.
            let message = args.msg.strip_prefix("0x").unwrap_or(&args.msg);
            // Recipient - 40 characters (Address in hex without '0x' prefix)
            if message.len() < 40 {
                return Err(ParseOnTransferMessageError::WrongMessageFormat.into());
            }
            let mut address_bytes = [0; 20];
            hex::decode_to_slice(&message[..40], &mut address_bytes)
                .map_err(|_| ParseOnTransferMessageError::WrongMessageFormat)?;
            Address::from_array(address_bytes)
        };

        if let Some(fallback_address) = silo::get_erc20_fallback_address(&self.io)
            && !silo::is_allow_receive_erc20_tokens(&self.io, &recipient)
        {
            recipient = fallback_address;
        }

        let erc20_token = get_erc20_from_nep141(&self.io, token)?;
        let erc20_admin_address = current_address(current_account_id);
        let result = self
            .call(
                &erc20_admin_address,
                &erc20_token,
                Wei::zero(),
                setup_receive_erc20_tokens_input(&recipient, amount),
                u64::MAX,
                Vec::new(), // TODO: are there values we should put here?
                Vec::new(),
                handler,
            )
            .and_then(submit_result_or_err)?;
```

**File:** engine/src/lib.rs (L602-610)
```rust
    #[unsafe(no_mangle)]
    pub extern "C" fn ft_on_transfer() {
        let io = Runtime;
        let env = Runtime;
        let mut handler = Runtime;
        contract_methods::connector::ft_on_transfer(io, &env, &mut handler)
            .map_err(ContractError::msg)
            .sdk_unwrap();
    }
```

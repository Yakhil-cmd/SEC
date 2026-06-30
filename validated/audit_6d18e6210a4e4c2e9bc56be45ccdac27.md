### Title
Unrestricted `ft_on_transfer` Caller Allows Minting Unbacked ERC-20 Bridge Tokens Without Depositing NEP-141 — (`engine/src/contract_methods/connector.rs`)

---

### Summary

Any NEAR account can call `ft_on_transfer` directly on the Aurora engine without actually transferring NEP-141 tokens. Combined with the unrestricted `deploy_erc20_token` entrypoint, an attacker can register their own NEP-141 account, then call `ft_on_transfer` from that account with an arbitrary `amount`, causing Aurora to mint unbacked ERC-20 tokens. This breaks the 1:1 accounting invariant between NEP-141 tokens held by Aurora and ERC-20 tokens minted on Aurora, leading to insolvency of the bridged token.

---

### Finding Description

The `ft_on_transfer` entrypoint in `engine/src/contract_methods/connector.rs` is the NEP-141 callback that Aurora uses to mint ERC-20 tokens when a NEP-141 token is bridged in. The function performs no validation that the caller is a legitimate NEP-141 contract that has actually transferred tokens. It simply reads `predecessor_account_id` from the environment and uses it as the NEP-141 token identifier: [1](#0-0) 

The branch at line 81 checks only whether the caller is the registered ETH connector account. For any other caller, it unconditionally proceeds to `receive_erc20_tokens`, passing the caller's account ID as the NEP-141 token: [2](#0-1) 

Inside `receive_erc20_tokens`, the engine looks up the ERC-20 address registered for that NEP-141 and calls `mint(recipient, amount)` on it, using the Aurora engine's own EVM address as the admin caller: [3](#0-2) 

The `deploy_erc20_token` entrypoint, which registers a NEP-141→ERC-20 mapping, also has no access control — only a liveness check: [4](#0-3) 

The ERC-20 contract deployed is `EvmErc20` / `EvmErc20V2`, whose `mint` function is gated by `onlyAdmin`, where admin is set to `current_address(current_account_id)` — the Aurora engine's own EVM address — at deploy time: [5](#0-4) [6](#0-5) 

Because the Aurora engine's EVM address is the admin, and because `receive_erc20_tokens` calls `mint` from that address, any NEAR account that has a registered ERC-20 mapping can trigger minting by calling `ft_on_transfer` directly.

---

### Impact Explanation

**Insolvency / ERC-20 mirror accounting bug (Critical).**

The bridge's core invariant is: `ERC-20 total supply == NEP-141 tokens held by Aurora`. An attacker who mints ERC-20 tokens without depositing NEP-141 tokens breaks this invariant. The minted ERC-20 tokens can be traded on Aurora DEXes. Any user who acquires them and attempts to withdraw back to NEAR will find Aurora holds no corresponding NEP-141 tokens, making the withdrawal fail. The token is insolvent: more ERC-20 tokens exist than NEP-141 tokens backing them.

---

### Likelihood Explanation

**High.** The attack requires only:
1. Creating a NEAR account (trivial, costs fractions of a NEAR).
2. Calling `deploy_erc20_token` with that account ID (no access control, open to anyone).
3. Calling `ft_on_transfer` directly from that account with an arbitrary `amount` and a target EVM address.

No privileged access, leaked keys, or governance capture is required. The entire attack is executable by any unprivileged NEAR account in a single block.

---

### Recommendation

Add a caller whitelist or a cryptographic proof requirement to `ft_on_transfer`. The simplest fix is to require that `ft_on_transfer` can only be called by accounts that are registered NEP-141 tokens **and** that the call originates from a `ft_transfer_call` cross-contract callback (i.e., the NEP-141 contract must have actually locked tokens before calling Aurora). At minimum, maintain a registry of approved NEP-141 token accounts and reject `ft_on_transfer` calls from unregistered callers. Alternatively, mirror the pattern used for base-token minting: require the caller to be a specific, pre-approved account.

---

### Proof of Concept

```
# Step 1: Attacker creates a NEAR account
near create-account attacker-nep141.near --initialBalance 1

# Step 2: Attacker registers their account as a NEP-141 on Aurora (no access control)
near call aurora deploy_erc20_token \
  '{"nep141": "attacker-nep141.near"}' \
  --accountId attacker.near

# Step 3: Attacker calls ft_on_transfer directly from attacker-nep141.near
# (no actual NEP-141 transfer occurs)
near call aurora ft_on_transfer \
  '{"sender_id": "attacker.near", "amount": "1000000000000000000000000", "msg": "<attacker_evm_address_hex>"}' \
  --accountId attacker-nep141.near

# Result: Aurora mints 1,000,000 ERC-20 tokens for the attacker's EVM address
# with zero NEP-141 tokens deposited. The ERC-20 is now insolvent.
```

The root cause is at `engine/src/contract_methods/connector.rs` line 81–90: the `else` branch unconditionally mints ERC-20 tokens for any caller that is not the ETH connector, with no verification that tokens were actually transferred. [2](#0-1)

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

**File:** engine/src/engine.rs (L796-844)
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

        sdk::log!("Mint {amount} ERC-20 tokens for: {}", recipient.encode());

        // Return SubmitResult so that it can be accessed in standalone engine.
        // This is used to help with the indexing of bridge transactions.
        Ok(Some(result))
    }
```

**File:** engine/src/engine.rs (L1326-1334)
```rust
    let erc20_admin_address = current_address(current_account_id);
    let erc20_metadata = erc20_metadata.unwrap_or_default();

    let deploy_args = ethabi::encode(&[
        ethabi::Token::String(erc20_metadata.name),
        ethabi::Token::String(erc20_metadata.symbol),
        ethabi::Token::Uint(erc20_metadata.decimals.into()),
        ethabi::Token::Address(erc20_admin_address.raw().0.into()),
    ]);
```

**File:** etc/eth-contracts/contracts/EvmErc20.sol (L49-51)
```text
    function mint(address account, uint256 amount) public onlyAdmin {
        _mint(account, amount);
    }
```

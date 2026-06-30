### Title
Unbacked ERC-20 Minting via Unauthenticated `ft_on_transfer` Callback — (`engine/src/contract_methods/connector.rs`)

---

### Summary

The `ft_on_transfer` function in Aurora Engine implements the NEP-141 receiver callback. It is intended to be called by a legitimate NEP-141 token contract after a user initiates a `ft_transfer_call`. However, the function does not verify that the caller actually transferred any tokens to the engine. Any NEAR account that is registered as a NEP-141 token in Aurora's ERC-20 map can call `ft_on_transfer` directly with an arbitrary `amount`, causing Aurora to mint the corresponding ERC-20 tokens to any EVM address — without any real token transfer occurring.

---

### Finding Description

The `ft_on_transfer` entrypoint in `engine/src/contract_methods/connector.rs` (lines 62–109) handles two cases based on the `predecessor_account_id`:

1. If the caller is the authorized `eth-connector` account → calls `receive_base_tokens` (ETH minting).
2. Otherwise → calls `receive_erc20_tokens` using the caller's account ID as the NEP-141 token identifier. [1](#0-0) 

The `else` branch at line 83 passes the attacker-controlled `args.amount` directly into `receive_erc20_tokens`: [2](#0-1) 

Inside `receive_erc20_tokens`, the engine:
1. Parses the recipient EVM address from `args.msg` (attacker-controlled).
2. Looks up the registered ERC-20 contract for the calling account via `get_erc20_from_nep141`.
3. Calls the ERC-20 contract's `mint` function with the attacker-supplied `amount`. [3](#0-2) 

There is **no check** that the caller actually transferred `amount` tokens to the Aurora Engine account. The function blindly trusts the `amount` field in the JSON args.

The `deploy_erc20_token` function has no access control, so any account can register itself as a NEP-141 token in Aurora's ERC-20 map: [4](#0-3) 

---

### Impact Explanation

An attacker can mint an unbounded quantity of any ERC-20 token mirrored on Aurora without depositing the corresponding NEP-141 tokens. These unbacked tokens can be swapped in Aurora DeFi protocols for legitimate assets, draining liquidity pools. This constitutes **direct theft of user funds** and **insolvency** of the ERC-20 bridge peg.

**Impact: Critical** — Direct theft of funds / insolvency of bridged ERC-20 token pegs.

---

### Likelihood Explanation

The attack is fully permissionless and requires no special privileges:
- `deploy_erc20_token` has no access control.
- `ft_on_transfer` is a public NEAR contract method callable by any account.
- No admin compromise, oracle error, or governance capture is required.

**Likelihood: High** — Any NEAR account holder can execute this with a small amount of NEAR for gas.

---

### Recommendation

The `ft_on_transfer` callback must not trust the `amount` field from the caller. Instead, Aurora Engine should independently track the balance of each NEP-141 token it holds (via storage reads before and after the transfer), and only mint ERC-20 tokens equal to the **actual increase** in its NEP-141 balance. Alternatively, the engine should reject direct calls to `ft_on_transfer` that do not originate from a cross-contract call chain initiated by a legitimate `ft_transfer_call` — though this is harder to enforce in NEAR's execution model. The safest fix is balance-delta accounting.

---

### Proof of Concept

1. Attacker deploys a contract at `attacker-token.near` (any valid NEAR account).
2. Attacker calls `deploy_erc20_token` on Aurora Engine with `attacker-token.near` as the NEP-141 account ID. This registers `attacker-token.near` in Aurora's `nep141_erc20_map` and deploys a mirrored ERC-20 contract. No access control prevents this.
3. From the `attacker-token.near` account, attacker directly calls `ft_on_transfer` on Aurora Engine with JSON args:
   ```json
   {"sender_id": "attacker.near", "amount": "1000000000000000000000000", "msg": "0xAttackerEVMAddress"}
   ```
4. In `ft_on_transfer`, `predecessor_account_id` = `attacker-token.near`, which is not the `eth-connector`, so the `else` branch executes.
5. `receive_erc20_tokens` is called with `token = attacker-token.near`, `amount = 1e24`, `recipient = 0xAttackerEVMAddress`.
6. `get_erc20_from_nep141` succeeds (registered in step 2). The ERC-20 `mint` selector is called, minting `1e24` tokens to the attacker's EVM address.
7. No NEP-141 tokens were ever transferred to Aurora. The attacker holds `1e24` unbacked ERC-20 tokens and can swap them for legitimate assets in Aurora DeFi. [5](#0-4) [2](#0-1)

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

**File:** engine/src/contract_methods/connector.rs (L112-131)
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

### Title
`ft_on_transfer` Callable by Any Registered NEP-141 Account Without Actual Token Deposit — (File: `engine/src/contract_methods/connector.rs`)

---

### Summary

The `ft_on_transfer` NEAR contract entrypoint in the Aurora Engine connector is publicly callable by any account. It is designed to be invoked only by a NEP-141 token contract as the second step of `ft_transfer_call` (after tokens have already been transferred to Aurora). However, there is no authentication check verifying that the caller has actually transferred tokens. Any account whose NEAR account ID is registered as a NEP-141 token in Aurora can call `ft_on_transfer` directly, causing the engine to mint ERC-20 tokens for an arbitrary recipient without any real NEP-141 token deposit. This breaks the 1:1 backing invariant of the bridge and leads to insolvency.

---

### Finding Description

`ft_on_transfer` is exposed as a public `extern "C"` NEAR contract method and is dispatched to `connector::ft_on_transfer`. The function reads the predecessor account ID from the NEAR runtime and uses it as the NEP-141 token identity: [1](#0-0) 

When the predecessor is not the ETH connector account, the engine calls `receive_erc20_tokens` with the predecessor as the token: [2](#0-1) 

Inside `receive_erc20_tokens`, the only guard is a lookup of the ERC-20 address for the given NEP-141 account ID. If the lookup succeeds, the engine immediately calls the ERC-20 contract's `mint` selector with the attacker-controlled `amount` and `recipient`: [3](#0-2) 

The mint call is issued from the Aurora Engine's own EVM admin address, so the ERC-20 contract's admin check passes unconditionally: [4](#0-3) 

There is no check anywhere in this path that the predecessor account has actually transferred NEP-141 tokens to Aurora before this call. The return value of `ft_on_transfer` (the refund amount) is only meaningful when the call originates from a real `ft_transfer_call` on the NEP-141 contract; a direct call has no refund mechanism, so the attacker loses nothing.

---

### Impact Explanation

**Critical — Insolvency / Theft of user funds.**

An attacker who controls a NEAR account registered as a NEP-141 token in Aurora can:

1. Call `ft_on_transfer` directly, supplying an arbitrary `amount` and a recipient address they control.
2. The engine mints that amount of ERC-20 tokens for the recipient with no NEP-141 tokens ever deposited.
3. The attacker then calls the `exitToNear` precompile (or equivalent) to burn the ERC-20 tokens and withdraw real NEP-141 tokens that other users legitimately deposited.

This drains the NEP-141 token reserves held by Aurora, making the bridge insolvent for honest depositors. The attacker-controlled `amount` field is unbounded, so the entire reserve can be drained in a single transaction.

---

### Likelihood Explanation

**Medium.** The prerequisite is that the attacker's NEAR account ID is registered as a NEP-141 token in Aurora (i.e., an ERC-20 has been deployed for it via `deploy_erc20_token`). If this registration is permissionless — which is the common design for open bridge protocols — any attacker can self-register and immediately exploit the vulnerability. Even if registration requires owner approval, any already-registered NEP-141 token operator (including third-party token projects) can exploit this path. No private keys, governance capture, or social engineering is required beyond having a registered token.

---

### Recommendation

Add a caller-authentication check at the top of `ft_on_transfer` (or inside `receive_erc20_tokens`) that verifies the predecessor account ID is a registered NEP-141 token **and** that the call originates from a genuine `ft_transfer_call` context. The standard mitigation is to maintain an allowlist of registered NEP-141 token account IDs and reject any `ft_on_transfer` call whose predecessor is not in that list. Alternatively, require a one-yocto deposit (as a proof-of-intent pattern) or use NEAR's `assert_called_back_by` pattern to ensure the call is a genuine callback from the token contract.

---

### Proof of Concept

```
Attacker NEAR account: attacker.near
Attacker controls a NEP-141 token contract at: attacker.near
Aurora has registered attacker.near → ERC-20 at 0xABCD...

1. Attacker calls aurora.near::ft_on_transfer directly:
   predecessor = attacker.near
   input = { "sender_id": "attacker.near", "amount": "1000000000000000000000", "msg": "<attacker EVM address>" }

2. ft_on_transfer dispatches to receive_erc20_tokens("attacker.near", args, ...)

3. get_erc20_from_nep141("attacker.near") → 0xABCD...  (succeeds, no token transfer required)

4. Engine calls ERC20(0xABCD).mint(<attacker EVM address>, 1000000000000000000000)
   → succeeds because caller is Aurora Engine admin address

5. Attacker now holds 1e21 ERC-20 tokens backed by zero NEP-141 tokens.

6. Attacker calls exitToNear precompile → burns ERC-20, withdraws real NEP-141 tokens
   from Aurora's reserve → other users' deposits are drained.
``` [5](#0-4) [6](#0-5)

### Citations

**File:** engine/src/contract_methods/connector.rs (L61-108)
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

**File:** engine/src/engine.rs (L1305-1314)
```rust
#[must_use]
pub fn setup_receive_erc20_tokens_input(recipient: &Address, amount: u128) -> Vec<u8> {
    let selector = ERC20_MINT_SELECTOR;
    let tail = ethabi::encode(&[
        ethabi::Token::Address(recipient.raw().0.into()),
        ethabi::Token::Uint(amount.into()),
    ]);

    [selector, tail.as_slice()].concat()
}
```

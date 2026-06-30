### Title
Insolvency via Rebasing/Fee-on-Transfer NEP-141 Token Deposit: `args.amount` Trusted Without Balance-Difference Verification — (`engine/src/engine.rs`)

### Summary

`receive_erc20_tokens` mints ERC-20 tokens on Aurora using the `amount` field reported by the NEP-141 callback (`ft_on_transfer`) without verifying the actual NEP-141 balance change in Aurora's account. For rebasing or fee-on-transfer NEP-141 tokens, the amount credited to Aurora's account can be less than `args.amount`, causing Aurora to mint more ERC-20 tokens than it holds NEP-141 backing — a direct insolvency condition.

### Finding Description

The `ft_on_transfer` entry point in `engine/src/contract_methods/connector.rs` dispatches to `receive_erc20_tokens` for any NEP-141 token that is not the base connector token: [1](#0-0) 

Inside `receive_erc20_tokens`, the amount used to mint ERC-20 tokens is taken directly from `args.amount` — the value the NEP-141 contract reported in the callback — without any before/after balance check: [2](#0-1) 

Specifically, line 803 captures the reported amount: [3](#0-2) 

And line 831 passes that same amount to the ERC-20 mint call: [4](#0-3) 

The NEP-141 `ft_transfer_call` standard requires the token contract to call `ft_on_transfer(sender_id, amount, msg)` with the amount it transferred. For a rebasing or fee-on-transfer NEP-141 token, the actual balance credited to Aurora's account can be `amount - delta`, while the callback still reports `amount`. Aurora trusts this reported value and mints the full `amount` of ERC-20 tokens.

Any user can register a new NEP-141 token with Aurora via `deploy_erc20_token`, which has no owner restriction: [5](#0-4) 

### Impact Explanation

**Critical — Insolvency.** Aurora's ERC-20 token supply for the affected NEP-141 becomes unbacked. Aurora holds `X - delta` NEP-141 tokens but has minted `X` ERC-20 tokens. Early exiters can withdraw their full share; later exiters cannot, as Aurora's NEP-141 reserve is exhausted before all ERC-20 holders are served. This is a direct, permanent insolvency of the bridge reserve for that token.

### Likelihood Explanation

**Medium.** Any unprivileged user can deploy a rebasing or fee-on-transfer NEP-141 token and register it with Aurora. The `ft_on_transfer` path is the standard bridge deposit flow and is exercised by every user who bridges NEP-141 tokens into Aurora. No admin compromise or special privilege is required.

### Recommendation

In `receive_erc20_tokens`, record Aurora's NEP-141 balance before the `ft_on_transfer` callback is processed and use the actual balance difference (post-minus-pre) as the mint amount, rather than trusting `args.amount`. This mirrors the fix recommended in the original report for the deposit case: use the balance difference around the transfer, not the reported amount.

### Proof of Concept

1. Attacker deploys a NEP-141 token contract that charges a 10% fee on every transfer (or rebases downward).
2. Attacker calls `deploy_erc20_token` on Aurora to register the NEP-141 token, obtaining an ERC-20 mirror address.
3. Attacker calls `ft_transfer_call(aurora, 1000, recipient_hex)` on the NEP-141 contract.
4. The NEP-141 contract credits Aurora's account with 900 tokens (after 10% fee) but calls `ft_on_transfer(attacker, 1000, recipient_hex)` on Aurora.
5. Aurora executes `receive_erc20_tokens` with `amount = 1000`, minting 1000 ERC-20 tokens for the recipient.
6. Aurora's actual NEP-141 balance is only 900.
7. Repeating this process drains the reserve. When legitimate users attempt to exit (burn ERC-20 → receive NEP-141), Aurora's `ft_transfer` call to the NEP-141 contract fails or delivers less than owed, permanently freezing or destroying user funds.

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

**File:** engine/src/contract_methods/connector.rs (L112-130)
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

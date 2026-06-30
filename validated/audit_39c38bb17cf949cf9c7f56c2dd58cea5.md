### Title
Fee-on-Transfer NEP-141 Token Deposit Mints More ERC-20 Than Tokens Held in Escrow - (`engine/src/engine.rs`)

### Summary

When a NEP-141 token with a transfer fee is bridged into Aurora via `ft_on_transfer`, both `receive_base_tokens` and `receive_erc20_tokens` mint ERC-20 tokens equal to `args.amount` — the amount the sender specified — rather than the actual amount Aurora received after the fee deduction. This inflates the ERC-20 supply beyond the NEP-141 tokens held in escrow, causing insolvency and eventual permanent fund freeze for the last users to exit.

### Finding Description

The NEP-141 `ft_transfer_call` standard works as follows:
1. The sender calls `ft_transfer_call(aurora_engine, amount, msg)` on the NEP-141 token contract.
2. The token contract transfers tokens to Aurora. If the token charges a fee on transfer, Aurora receives `amount - fee` tokens.
3. The token contract then calls `ft_on_transfer(sender_id, amount, msg)` on Aurora, passing the **original** `amount` (not the net received amount).

Aurora's `ft_on_transfer` handler dispatches to either `receive_base_tokens` or `receive_erc20_tokens`, both of which unconditionally use `args.amount` as the mint quantity:

In `receive_base_tokens` (`engine/src/engine.rs`, line 778):
```rust
let amount = Wei::new_u128(args.amount.as_u128());
``` [1](#0-0) 

In `receive_erc20_tokens` (`engine/src/engine.rs`, line 803):
```rust
let amount = args.amount.as_u128();
// ...
setup_receive_erc20_tokens_input(&recipient, amount),
``` [2](#0-1) 

Neither function queries the actual NEP-141 balance of the Aurora contract before and after the transfer to determine the true received amount. The `ft_on_transfer` entry point in `connector.rs` passes `args` straight through without any balance reconciliation: [3](#0-2) 

### Impact Explanation

**Critical — Insolvency and Permanent Fund Freeze.**

For every deposit of a fee-on-transfer NEP-141 token, the ERC-20 supply minted on Aurora exceeds the NEP-141 tokens held in escrow by the fee amount. Over multiple deposits the gap compounds. When users attempt to exit back to NEAR via `withdrawToNear` (which burns ERC-20 and triggers an `ft_transfer` of the NEP-141), the last users to exit will find the Aurora contract's NEP-141 balance insufficient to cover the burn amount. Their funds are permanently frozen inside Aurora with no recovery path, and the bridge is insolvent with respect to that token.

### Likelihood Explanation

**High.** The NEAR ecosystem supports permissionless NEP-141 token deployment. Any token that charges a fee on `ft_transfer` or `ft_transfer_call` — a common pattern for DeFi tokens, tax tokens, and rebasing tokens — triggers this bug automatically when bridged to Aurora. No special privilege is required; any token holder can call `ft_transfer_call` on such a token targeting Aurora. The Aurora bridge explicitly supports arbitrary NEP-141 tokens via `deploy_erc20_token`, making the attack surface broad. [4](#0-3) 

### Recommendation

In both `receive_base_tokens` and `receive_erc20_tokens`, replace the use of `args.amount` with the actual received amount, computed as the difference in the Aurora contract's NEP-141 balance before and after the transfer. Because `ft_on_transfer` is a callback invoked after the transfer has already occurred, the simplest fix is to query the NEP-141 balance of the Aurora contract at the start of `ft_on_transfer` and again at the point of minting, using the delta as the authoritative amount. Alternatively, document and enforce that fee-on-transfer NEP-141 tokens are not supported, and reject deposits from token contracts that are not on an explicit allowlist.

### Proof of Concept

1. Deploy a NEP-141 token `FeeToken` that charges a 10% fee on every `ft_transfer` / `ft_transfer_call`.
2. Deploy the corresponding ERC-20 on Aurora via `deploy_erc20_token(FeeToken)`.
3. Call `FeeToken.ft_transfer_call(aurora_engine, 1000, "<evm_address>")`.
4. `FeeToken` transfers 900 tokens to Aurora (deducting 100 as fee) and calls `ft_on_transfer(sender, 1000, "<evm_address>")`.
5. Aurora's `receive_erc20_tokens` reads `args.amount = 1000` and mints 1000 ERC-20 tokens to `<evm_address>`.
6. Aurora now holds 900 `FeeToken` NEP-141 tokens but has 1000 ERC-20 tokens in circulation.
7. Repeat N times. The deficit grows by 100 per deposit.
8. When the 10th depositor tries to exit via `withdrawToNear(1000)`, the ERC-20 burn succeeds but the subsequent `ft_transfer` of 1000 `FeeToken` from Aurora fails because Aurora only holds `1000 * 0.9^N` tokens — the last depositors' funds are permanently frozen. [5](#0-4) [6](#0-5)

### Citations

**File:** engine/src/engine.rs (L773-789)
```rust
    pub fn receive_base_tokens(
        &mut self,
        args: &FtOnTransferArgs,
    ) -> Result<Option<SubmitResult>, ContractError> {
        let message_data = FtTransferMessageData::try_from(args.msg.as_str())?;
        let amount = Wei::new_u128(args.amount.as_u128());
        let receipient = message_data.recipient;
        let balance = get_balance(&self.io, &receipient);
        let new_balance = balance
            .checked_add(amount)
            .ok_or(errors::ERR_BALANCE_OVERFLOW)?;

        set_balance(&mut self.io, &receipient, &new_balance);

        sdk::log!("Mint {amount} base tokens for: {}", receipient.encode());

        Ok(None)
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

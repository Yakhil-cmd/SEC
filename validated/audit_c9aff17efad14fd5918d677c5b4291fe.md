### Title
Fee-on-Transfer NEP-141 Token Causes ERC-20 Over-Minting and Bridge Insolvency in `receive_erc20_tokens` - (`engine/src/engine.rs`)

---

### Summary

`Engine::receive_erc20_tokens` unconditionally mints ERC-20 tokens equal to `args.amount` — the value reported by the calling NEP-141 contract in the `ft_on_transfer` callback — without verifying the actual NEP-141 balance received by Aurora. For any fee-on-transfer NEP-141 token (one that deducts a fee from the transferred amount but reports the pre-fee amount in `ft_on_transfer`), Aurora mints more ERC-20 tokens than it holds NEP-141 tokens. This creates a permanent accounting deficit that makes the bridge insolvent for that token.

---

### Finding Description

The entry point is `ft_on_transfer` in `engine/src/contract_methods/connector.rs`, which dispatches to `Engine::receive_erc20_tokens` for any registered NEP-141 token that is not the base ETH connector: [1](#0-0) 

Inside `receive_erc20_tokens`, the amount to mint is taken directly from `args.amount`: [2](#0-1) 

This value is passed unchanged into `setup_receive_erc20_tokens_input`, which encodes it as the `mint(recipient, amount)` call on the ERC-20 contract: [3](#0-2) 

`args.amount` is the value the NEP-141 token contract supplies in the `ft_on_transfer` callback. The NEP-141 standard does not prohibit fee-on-transfer tokens. A token that deducts a fee from the transferred amount and calls `ft_on_transfer` with the pre-fee `amount` will cause Aurora to:

1. Receive `amount - fee` NEP-141 tokens into its account.
2. Mint `amount` ERC-20 tokens to the recipient.

The same pattern applies to `receive_base_tokens` for the ETH connector path: [4](#0-3) 

The exit path (`exit_to_near` precompile) burns ERC-20 tokens and calls `ft_transfer` on the NEP-141 contract for the full burned amount: [5](#0-4) 

Because the total ERC-20 supply exceeds Aurora's actual NEP-141 balance, the last users to exit will find Aurora cannot fulfill their `ft_transfer` call — the bridge is insolvent for that token.

---

### Impact Explanation

**Critical — Insolvency.**

For every deposit of a fee-on-transfer NEP-141 token, Aurora mints `fee` more ERC-20 tokens than it holds NEP-141 tokens. The deficit accumulates with each deposit. When users exit, the NEP-141 balance held by Aurora is exhausted before all ERC-20 holders can redeem. The last users to exit lose their funds permanently. There is no recovery path because the ERC-20 tokens are already minted and circulating.

---

### Likelihood Explanation

**Medium.**

Any NEP-141 token with a transfer fee can be registered with Aurora via `deploy_erc20_token`. The registration itself is permissionless in the sense that any account can call it for any NEP-141. Once registered, any token holder can call `ft_transfer_call` on the fee-on-transfer NEP-141 with Aurora as the receiver, triggering the over-mint. The attacker does not need any special privilege — only a balance of the fee-on-transfer token. Fee-on-transfer tokens are a well-established token design pattern on NEAR (analogous to deflationary ERC-20s on Ethereum).

---

### Recommendation

After the `ft_on_transfer` callback is received, Aurora should verify the actual NEP-141 balance change rather than trusting `args.amount`. Concretely, `receive_erc20_tokens` should:

1. Query Aurora's NEP-141 balance before and after the transfer (or use a balance-check cross-contract call pattern).
2. Mint only `actual_received = balance_after - balance_before` ERC-20 tokens.
3. Return `args.amount - actual_received` to the NEP-141 contract as the "unused" amount, so the NEP-141 contract refunds the fee to the sender.

Alternatively, document that fee-on-transfer NEP-141 tokens are explicitly unsupported and add a registry-level check or warning.

---

### Proof of Concept

1. Deploy a NEP-141 token `fee_token.near` that charges a 5% fee on every transfer (i.e., when `ft_transfer_call(aurora, 100, msg)` is called, it transfers 95 tokens to Aurora but calls `ft_on_transfer(sender, 100, msg)` with the pre-fee amount).
2. Register `fee_token.near` with Aurora via `deploy_erc20_token`.
3. Call `ft_transfer_call` on `fee_token.near` with `receiver_id = aurora`, `amount = 100`, `msg = <evm_address>`.
4. Aurora's `ft_on_transfer` is called with `args.amount = 100`.
5. `receive_erc20_tokens` mints 100 ERC-20 tokens to `<evm_address>`.
6. Aurora's actual NEP-141 balance of `fee_token.near` increased by only 95.
7. Repeat N times. After N deposits of 100, Aurora holds `95*N` NEP-141 but has minted `100*N` ERC-20.
8. The first `95*N / 100` users who call `exit_to_near` with 100 ERC-20 each succeed. The remaining users' `ft_transfer` calls fail because Aurora's NEP-141 balance is 0.

The root cause is at: [6](#0-5)

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

**File:** engine/src/engine.rs (L778-785)
```rust
        let amount = Wei::new_u128(args.amount.as_u128());
        let receipient = message_data.recipient;
        let balance = get_balance(&self.io, &receipient);
        let new_balance = balance
            .checked_add(amount)
            .ok_or(errors::ERR_BALANCE_OVERFLOW)?;

        set_balance(&mut self.io, &receipient, &new_balance);
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

**File:** engine/src/engine.rs (L1306-1313)
```rust
pub fn setup_receive_erc20_tokens_input(recipient: &Address, amount: u128) -> Vec<u8> {
    let selector = ERC20_MINT_SELECTOR;
    let tail = ethabi::encode(&[
        ethabi::Token::Address(recipient.raw().0.into()),
        ethabi::Token::Uint(amount.into()),
    ]);

    [selector, tail.as_slice()].concat()
```

**File:** engine-precompiles/src/native.rs (L630-646)
```rust
            (
                nep141_account_id,
                format!(
                    r#"{{"receiver_id":"{}","amount":"{}"}}"#,
                    exit_params.receiver_account_id,
                    exit_params.amount.as_u128()
                ),
                "ft_transfer",
                None,
                events::ExitToNear::Legacy(ExitToNearLegacy {
                    sender: Address::new(erc20_address),
                    erc20_address: Address::new(erc20_address),
                    dest: exit_params.receiver_account_id.to_string(),
                    amount: exit_params.amount,
                }),
            )
        }
```

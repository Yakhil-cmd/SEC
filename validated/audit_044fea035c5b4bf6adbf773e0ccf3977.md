### Title
ERC-20 Bridge Over-Minting via Fee-on-Transfer NEP-141 Tokens - (File: `engine/src/engine.rs`)

### Summary
The `receive_erc20_tokens` function in Aurora Engine blindly trusts the `amount` field from `ft_on_transfer` callbacks to mint ERC-20 mirror tokens, without verifying the actual NEP-141 balance change Aurora received. A fee-on-transfer (deflationary) NEP-141 token causes Aurora to mint more ERC-20 tokens than the NEP-141 tokens it actually holds, enabling an attacker to withdraw more tokens than deposited and rendering the bridge insolvent for other users.

### Finding Description

When a NEP-141 token is bridged into Aurora via `ft_transfer_call`, the NEP-141 contract calls `ft_on_transfer` on Aurora. Aurora's `ft_on_transfer` entrypoint (connector.rs:62–109) routes non-base tokens to `engine.receive_erc20_tokens`. [1](#0-0) 

Inside `receive_erc20_tokens`, the mint quantity is taken directly and unconditionally from `args.amount`: [2](#0-1) 

Specifically, line 803 extracts `amount = args.amount.as_u128()` and line 831 passes it to `setup_receive_erc20_tokens_input`, which constructs a call to `mint(recipient, amount)` on the ERC-20 mirror contract (the same `ERC20_MINT_SELECTOR` pattern visible at line 1167): [3](#0-2) 

The `EvmErc20.mint` function mints exactly the supplied amount with no further checks: [4](#0-3) 

For a standard NEP-141 token, `args.amount` equals the tokens actually credited to Aurora's account, so accounting is correct. However, for a **fee-on-transfer NEP-141 token** — one that deducts a protocol fee during `ft_transfer`, crediting the receiver only `amount - fee` while still invoking `ft_on_transfer` with the gross `amount` — Aurora mints `amount` ERC-20 tokens while its actual NEP-141 balance only increased by `amount - fee`. There is no balance-before/balance-after check anywhere in the deposit path.

The NEP-141 standard does not prohibit fee-on-transfer semantics, and such tokens exist in practice. Any token deployer can register such a token with Aurora via `deploy_erc20_token`: [5](#0-4) 

### Impact Explanation

**Critical — Insolvency / Direct theft of user funds.**

Aurora's ERC-20 mirror supply becomes unbacked: for every deposit of a fee-on-transfer token, the total ERC-20 supply exceeds the NEP-141 reserves held by Aurora. When honest users later call `withdrawToNear` (burning ERC-20 tokens and triggering `ft_transfer` on the NEP-141 contract), Aurora cannot fulfill all withdrawals. The attacker's over-minted ERC-20 tokens are redeemed against other users' deposits, directly stealing their funds. [6](#0-5) 

### Likelihood Explanation

**Medium.** The precondition is that a fee-on-transfer NEP-141 token is registered with Aurora. `deploy_erc20_token` is callable by any account (it is not owner-restricted in the production path shown at lines 112–131 of connector.rs). A malicious token deployer can create and register such a token, then exploit the accounting gap. The exploit requires no privileged access, no governance capture, and no external oracle — only a deployed NEP-141 token with fee-on-transfer behavior. [7](#0-6) 

### Recommendation

In `receive_erc20_tokens`, replace the blind use of `args.amount` with a balance-check pattern: record Aurora's NEP-141 balance before the `ft_on_transfer` callback is processed and mint only the actual delta. Alternatively, enforce at `deploy_erc20_token` time that the registered NEP-141 token does not implement fee-on-transfer semantics (e.g., by performing a test transfer and comparing the credited amount). At minimum, document that fee-on-transfer NEP-141 tokens are unsupported and add a registry-level blocklist.

### Proof of Concept

1. Deploy a NEP-141 token `fee_token.near` that deducts a 10% fee on every `ft_transfer`/`ft_transfer_call`, crediting the receiver `amount * 0.9` but calling `ft_on_transfer` with the gross `amount`.
2. Call `deploy_erc20_token(fee_token.near)` on Aurora to register the mirror ERC-20.
3. Attacker calls `ft_transfer_call(aurora, 1000, "0x<attacker_evm_addr>")` on `fee_token.near`.
   - `fee_token.near` credits Aurora with **900** tokens (after 10% fee).
   - `fee_token.near` calls `ft_on_transfer(attacker, 1000, "0x<attacker_evm_addr>")` on Aurora.
4. Aurora's `receive_erc20_tokens` reads `args.amount = 1000` and mints **1000** ERC-20 tokens to the attacker's EVM address. Aurora's actual NEP-141 balance is only **900**.
5. An honest user deposits 1000 tokens; Aurora receives 900, mints 1000 ERC-20. Aurora now holds **1800** NEP-141 tokens but has issued **2000** ERC-20 tokens.
6. Attacker calls `withdrawToNear(1000)` on the ERC-20 contract — burns 1000 ERC-20, Aurora calls `ft_transfer(1000)` on `fee_token.near`. Aurora's NEP-141 balance drops to **800**.
7. Honest user attempts `withdrawToNear(1000)` — Aurora only holds **800** NEP-141 tokens; the withdrawal fails or partially fails. The honest user's funds are permanently lost. [8](#0-7)

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

**File:** engine/src/engine.rs (L1165-1174)
```rust
#[must_use]
pub fn setup_refund_on_error_input(amount: U256, refund_address: Address) -> Vec<u8> {
    let selector = ERC20_MINT_SELECTOR;
    let mint_args = ethabi::encode(&[
        ethabi::Token::Address(refund_address.raw().0.into()),
        ethabi::Token::Uint(amount.to_big_endian().into()),
    ]);

    [selector, mint_args.as_slice()].concat()
}
```

**File:** etc/eth-contracts/contracts/EvmErc20.sol (L49-51)
```text
    function mint(address account, uint256 amount) public onlyAdmin {
        _mint(account, amount);
    }
```

**File:** etc/eth-contracts/contracts/EvmErc20.sol (L53-63)
```text
    function withdrawToNear(bytes memory recipient, uint256 amount) external override {
        _burn(_msgSender(), amount);

        bytes32 amount_b = bytes32(amount);
        bytes memory input = abi.encodePacked("\x01", amount_b, recipient);
        uint input_size = 1 + 32 + recipient.length;

        assembly {
            let res := call(gas(), 0xe9217bc70b7ed1f598ddd3199e80b093fa71124f, 0, add(input, 32), input_size, 0, 32)
        }
    }
```

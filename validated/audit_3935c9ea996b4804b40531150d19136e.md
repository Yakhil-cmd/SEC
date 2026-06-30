### Title
Fee-on-Transfer NEP-141 Token Causes ERC-20 Over-Issuance and Bridge Insolvency - (File: `engine/src/engine.rs`)

### Summary

The `receive_erc20_tokens` function in `engine/src/engine.rs` mints ERC-20 tokens equal to `args.amount` — the amount reported by the calling NEP-141 contract — without verifying that Aurora's actual NEP-141 balance increased by that amount. If the NEP-141 token charges a transfer fee, Aurora receives fewer tokens than it mints, creating an unbacked ERC-20 surplus and making the bridge insolvent for that token pair.

### Finding Description

When a NEP-141 token is bridged into Aurora via the `ft_transfer_call` → `ft_on_transfer` flow, the NEP-141 contract calls Aurora's `ft_on_transfer` entry point with an `args.amount` field representing the amount the *sender* transferred. Aurora's `ft_on_transfer` dispatches to `receive_erc20_tokens`: [1](#0-0) 

Inside `receive_erc20_tokens`, the amount used for minting is taken directly from `args.amount` with no balance-delta check: [2](#0-1) 

This amount is then forwarded verbatim to `setup_receive_erc20_tokens_input`, which encodes a `mint(recipient, amount)` call to the EVM-side `EvmErc20` contract: [3](#0-2) 

The `EvmErc20.mint` function unconditionally increases the ERC-20 total supply by that amount: [4](#0-3) 

If the NEP-141 token deducts a fee during transfer, Aurora's actual NEP-141 balance increases by `amount - fee`, but the ERC-20 supply increases by `amount`. Each deposit widens the gap between ERC-20 supply and NEP-141 backing. When users later call `withdrawToNear` (which burns ERC-20 and triggers an `ft_transfer` of NEP-141 back to the user), the last users to exit find Aurora holds insufficient NEP-141 to honor the redemption.

### Impact Explanation

**Critical — Insolvency.** The ERC-20 total supply for a fee-on-transfer NEP-141 pair permanently exceeds the NEP-141 collateral held by Aurora. Every deposit with such a token widens the deficit. Users who exit last cannot redeem their ERC-20 tokens; their funds are permanently frozen inside the bridge. The deficit is proportional to the fee rate and the total volume bridged.

### Likelihood Explanation

**Medium.** The `deploy_erc20_token` (Legacy path) has no owner-only access control — only `require_running` is checked — so any unprivileged NEAR account can register a fee-on-transfer NEP-141 token: [5](#0-4) 

Fee-on-transfer tokens are a well-known NEP-141/ERC-20 pattern (e.g., deflationary tokens, tokens with protocol fees). Once such a token is registered and users begin bridging it, the insolvency accumulates silently with every `ft_transfer_call` invocation. No privileged access is required after registration.

### Recommendation

After the NEP-141 `ft_transfer_call` completes and `ft_on_transfer` is invoked, Aurora should verify the actual balance delta rather than trusting `args.amount`. Concretely, record Aurora's NEP-141 balance before and after the transfer (via a cross-contract read or by comparing the balance reported in the callback), and mint only the verified received amount. Alternatively, maintain an explicit accounting ledger of NEP-141 balances per token and reject any `ft_on_transfer` where `args.amount` exceeds the observed balance increase.

### Proof of Concept

1. Deploy a NEP-141 token `fee_token.near` that charges a 10% fee on every `ft_transfer_call` (deducted from the receiver's credited amount).
2. Call `deploy_erc20_token` on Aurora with `fee_token.near` as the NEP-141 — succeeds with no access control.
3. Alice calls `ft_transfer_call(receiver_id="aurora", amount=1000, msg=<alice_evm_address>)` on `fee_token.near`.
4. `fee_token.near` credits Aurora with 900 tokens, then calls `ft_on_transfer(sender_id=alice, amount=1000, msg=<alice_evm_address>)` on Aurora.
5. Aurora's `receive_erc20_tokens` reads `amount = 1000` and calls `EvmErc20.mint(alice_evm_address, 1000)`.
6. Aurora holds 900 NEP-141 tokens; Alice holds 1000 ERC-20 tokens. Deficit: 100.
7. Repeat step 3–6 ten times. Aurora holds 9000 NEP-141; ERC-20 supply is 10000. Deficit: 1000.
8. Alice calls `withdrawToNear` for 1000 ERC-20. Aurora burns 1000 ERC-20 and attempts `ft_transfer(alice, 1000)` on `fee_token.near`. This succeeds for early exiters.
9. The last user to exit finds Aurora's NEP-141 balance is insufficient; their `ft_transfer` call fails and their ERC-20 tokens are permanently stranded. [6](#0-5) [7](#0-6)

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

**File:** engine/src/contract_methods/connector.rs (L117-125)
```rust
    with_hashchain(io, env, function_name!(), |mut io| {
        require_running(&state::get_state(&io)?)?;
        let bytes = io.read_input().to_vec();
        let args =
            DeployErc20TokenArgs::deserialize(&bytes).map_err(|_| errors::ERR_BORSH_DESERIALIZE)?;

        match args {
            DeployErc20TokenArgs::Legacy(nep141) => {
                let address = engine::deploy_erc20_token(nep141, None, io, env, handler)?;
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

**File:** etc/eth-contracts/contracts/EvmErc20.sol (L49-51)
```text
    function mint(address account, uint256 amount) public onlyAdmin {
        _mint(account, amount);
    }
```

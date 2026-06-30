### Title
ERC-20 Mirror Over-Minting for Fee-on-Transfer NEP-141 Tokens Due to Trusting `ft_on_transfer` Amount Without Verifying Actual Balance Received - (File: `engine/src/engine.rs`)

---

### Summary

`receive_erc20_tokens` mints ERC-20 mirror tokens equal to `args.amount` — the value supplied by the NEP-141 token contract in the `ft_on_transfer` callback — without verifying the actual NEP-141 balance increase credited to the Aurora Engine account. For any fee-on-transfer NEP-141 token, the engine receives fewer tokens than `args.amount` but mints the full `args.amount` in ERC-20 mirrors, creating a permanent accounting deficit that leads to insolvency and permanent fund freeze for the last withdrawers.

---

### Finding Description

When a user bridges a NEP-141 token into Aurora, the flow is:

1. User calls `ft_transfer_call(receiver_id: aurora, amount, msg: evm_address)` on the NEP-141 contract.
2. The NEP-141 contract transfers `amount` tokens to Aurora Engine and calls `ft_on_transfer(sender_id, amount, msg)` on Aurora Engine.
3. Aurora Engine's `ft_on_transfer` handler dispatches to `engine.receive_erc20_tokens(...)`.

Inside `receive_erc20_tokens`:

```rust
let amount = args.amount.as_u128();
``` [1](#0-0) 

This `amount` is taken verbatim from the `FtOnTransferArgs` struct, which is the value the NEP-141 token contract chose to pass — not the actual balance increase observed by Aurora Engine. The engine then calls `mint(recipient, amount)` on the ERC-20 mirror contract:

```rust
let result = self
    .call(
        &erc20_admin_address,
        &erc20_token,
        Wei::zero(),
        setup_receive_erc20_tokens_input(&recipient, amount),
        ...
    )
``` [2](#0-1) 

The `setup_receive_erc20_tokens_input` function encodes a `mint(recipient, amount)` call using the ERC20_MINT_SELECTOR, so the ERC-20 mirror supply is inflated by the full `args.amount`. [3](#0-2) 

For a fee-on-transfer NEP-141 token (e.g., one that deducts 1% on every transfer), the Aurora Engine account receives only `amount * 0.99` NEP-141 tokens, but mints `amount` ERC-20 mirror tokens. The ERC-20 mirror supply is permanently inflated relative to the actual NEP-141 reserve held by Aurora Engine.

On the withdrawal side, `withdrawToNear` in `EvmErc20.sol` burns the ERC-20 tokens and calls the `ExitToNear` precompile with the full stated amount:

```solidity
function withdrawToNear(bytes memory recipient, uint256 amount) external override {
    _burn(_msgSender(), amount);
    bytes32 amount_b = bytes32(amount);
    bytes memory input = abi.encodePacked("\x01", amount_b, recipient);
    ...
    call(gas(), 0xe9217bc70b7ed1f598ddd3199e80b093fa71124f, ...)
}
``` [4](#0-3) 

The `ExitToNear` precompile then issues an `ft_transfer` for `amount` NEP-141 tokens to the recipient: [5](#0-4) 

Since the Aurora Engine holds fewer NEP-141 tokens than the total ERC-20 mirror supply, the last users to withdraw will find the engine's NEP-141 balance insufficient, causing their withdrawals to fail permanently.

The `ft_on_transfer` entry point that triggers this path: [6](#0-5) 

---

### Impact Explanation

**Critical — Insolvency and Permanent Fund Freeze.**

Every deposit of a fee-on-transfer NEP-141 token inflates the ERC-20 mirror supply by the fee amount. Over time (or in a single large deposit), the cumulative deficit means the Aurora Engine cannot honor all outstanding ERC-20 mirror redemptions. The last users to call `withdrawToNear` or `withdrawToEthereum` will have their transactions fail because the engine's NEP-141 balance is exhausted. Their ERC-20 mirror tokens are burned but no NEP-141 tokens are transferred — a permanent loss of funds.

---

### Likelihood Explanation

**Medium.** Any NEP-141 token can be registered via `deploy_erc20_token` without any validation of its transfer behavior. Fee-on-transfer tokens exist in the NEAR ecosystem. A single user depositing such a token triggers the deficit immediately. No special privileges are required — any unprivileged user who calls `ft_transfer_call` on a registered fee-on-transfer NEP-141 token initiates the accounting error. [7](#0-6) 

---

### Recommendation

In `receive_erc20_tokens`, do not trust `args.amount` as the mint quantity. Instead, query the Aurora Engine's NEP-141 balance before and after the transfer and mint only the observed delta. Alternatively, reject registration of fee-on-transfer NEP-141 tokens by validating that a test transfer of a known amount results in the expected balance change before allowing `deploy_erc20_token` to complete.

---

### Proof of Concept

1. Deploy a NEP-141 token `fee_token.near` that deducts 10% on every `ft_transfer` / `ft_transfer_call`.
2. Call `deploy_erc20_token` on Aurora Engine for `fee_token.near` → ERC-20 mirror deployed at `0xABC`.
3. Alice calls `fee_token.near::ft_transfer_call(receiver_id: aurora, amount: 1000, msg: alice_evm_addr)`.
   - `fee_token.near` transfers 900 tokens to Aurora Engine (10% fee deducted).
   - `fee_token.near` calls `aurora::ft_on_transfer(alice, 1000, alice_evm_addr)`.
   - Aurora Engine executes `receive_erc20_tokens`: `amount = 1000`, mints 1000 ERC-20 tokens for Alice.
   - **Deficit: Aurora holds 900 NEP-141, but 1000 ERC-20 tokens exist.**
4. Alice calls `EvmErc20(0xABC).withdrawToNear(alice_near_addr, 1000)`.
   - Burns 1000 ERC-20 tokens.
   - `ExitToNear` precompile issues `ft_transfer(receiver_id: alice_near_addr, amount: 1000)` to `fee_token.near`.
   - `fee_token.near` transfers 1000 tokens from Aurora Engine's balance (900 available) → **transfer fails** or, if partial transfers are allowed, Alice receives 900 and 100 tokens are permanently lost.
5. Any subsequent depositor's funds are at risk of being used to cover Alice's over-minted balance, constituting theft of other users' funds. [8](#0-7)

### Citations

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

**File:** engine/src/engine.rs (L1166-1174)
```rust
pub fn setup_refund_on_error_input(amount: U256, refund_address: Address) -> Vec<u8> {
    let selector = ERC20_MINT_SELECTOR;
    let mint_args = ethabi::encode(&[
        ethabi::Token::Address(refund_address.raw().0.into()),
        ethabi::Token::Uint(amount.to_big_endian().into()),
    ]);

    [selector, mint_args.as_slice()].concat()
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

**File:** engine-precompiles/src/native.rs (L627-646)
```rust
        _ => {
            // There is no way to inject json, given the encoding of both arguments
            // as decimal and valid account id respectively.
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

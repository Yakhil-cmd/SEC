### Title
Missing Zero-Address Validation in `receive_base_tokens` Allows Permanent ETH Freeze - (File: engine/src/engine.rs)

### Summary
The `receive_base_tokens` function, called during the NEP-141 `ft_on_transfer` bridge deposit flow, does not validate that the parsed recipient EVM address is non-zero. A caller who supplies `msg = "0000000000000000000000000000000000000000"` causes ETH to be minted permanently to `Address::zero()` — an address with no controlling private key — while the NEP-141 tokens are irrevocably consumed by the engine. The function is explicitly designed to never return an error (to avoid blocking the NEP-141 refund path), so the zero-address mint silently succeeds and the funds are unrecoverable.

### Finding Description

`FtTransferMessageData::try_from` parses any valid 40-hex-character string as a recipient address with no zero-address guard: [1](#0-0) 

The parsed `recipient` is then used directly in `receive_base_tokens` without any zero-address check: [2](#0-1) 

The function's own design comment ("should not panic, otherwise it won't be possible to return the tokens to the sender") means it is intentionally structured to always return `Ok`. When `recipient == Address::zero()`, `set_balance` writes a non-zero ETH balance to the zero-address storage slot, the function returns `Ok(None)`, the NEP-141 `ft_on_transfer` callback reports success, and the eth-connector does **not** refund the NEP-141 tokens to the sender. The ETH credited to `Address::zero()` is permanently inaccessible.

The same silent-zero-address path exists in the standalone engine's `FtOnTransfer` normalization, where a parse failure falls back to `unwrap_or_default()` — also yielding `Address::zero()`: [3](#0-2) 

### Impact Explanation

**Critical — Permanent freezing of funds.**

Any NEP-141 ETH deposited via `ft_transfer_call` with a zero-address `msg` is permanently frozen inside the Aurora EVM state at `Address::zero()`. No private key controls that address; no EVM transaction can move those funds. The NEP-141 tokens are simultaneously consumed (transferred to the engine contract) and never refunded, so the loss is total and irreversible.

### Likelihood Explanation

**Low-to-Medium.** The trigger is externally reachable by any unprivileged NEAR account calling `ft_transfer_call` on the eth-connector with `receiver_id = aurora` and `msg = "0000000000000000000000000000000000000000"`. This can occur through:
- A buggy or malicious DApp/smart contract that constructs the `msg` field incorrectly (e.g., zero-initialised buffer).
- A user who mistakenly passes an all-zero hex string.
- A relayer or bridge adapter that fails to validate the destination address before forwarding.

No admin compromise or privileged access is required.

### Recommendation

Add an explicit zero-address rejection in `receive_base_tokens` (or inside `FtTransferMessageData::try_from`) before any balance mutation:

```rust
if receipient == Address::zero() {
    return Err(errors::ERR_ZERO_ADDRESS_RECIPIENT.into());
}
```

Because `receive_base_tokens` is in the `ft_on_transfer` callback path, returning an `Err` here will cause the NEP-141 contract to refund the full token amount to the original sender, preventing any fund loss.

### Proof of Concept

1. User holds NEP-141 ETH on NEAR (deposited via the Aurora bridge).
2. User (or a contract acting on their behalf) calls:
   ```
   ft_transfer_call(
     receiver_id = "aurora",
     amount      = "1000000000000000000",   // 1 ETH in wei
     msg         = "0000000000000000000000000000000000000000"
   )
   ```
   on the eth-connector contract.
3. The eth-connector transfers 1 ETH worth of NEP-141 tokens from the user to Aurora and calls `ft_on_transfer` on Aurora.
4. Aurora's `ft_on_transfer` dispatches to `receive_base_tokens`.
5. `FtTransferMessageData::try_from("0000000000000000000000000000000000000000")` succeeds, returning `recipient = Address::zero()`.
6. `set_balance` credits 1 ETH to `Address::zero()` in Aurora's EVM state.
7. `receive_base_tokens` returns `Ok(None)`; `ft_on_transfer` reports `"0"` tokens to refund.
8. The eth-connector finalises the transfer — the user's NEP-141 ETH is gone, and the 1 ETH at `Address::zero()` is permanently frozen. [4](#0-3) [1](#0-0)

### Citations

**File:** engine-types/src/parameters/connector.rs (L40-57)
```rust
    fn try_from(message: &str) -> Result<Self, Self::Error> {
        if message.len() == 40 {
            // Parse message to determine recipient
            let recipient = {
                // Message format:
                // Recipient of the transaction - 40 characters (Address in hex)
                let mut address_bytes = [0; 20];
                hex::decode_to_slice(message, &mut address_bytes)
                    .map_err(|_| errors::ParseOnTransferMessageError::InvalidHexData)?;
                Address::from_array(address_bytes)
            };

            #[allow(deprecated)]
            return Ok(Self {
                recipient,
                fee: None,
            });
        }
```

**File:** engine/src/engine.rs (L773-790)
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
    }
```

**File:** engine-standalone-storage/src/sync/types.rs (L270-289)
```rust
            Self::FtOnTransfer(args) => {
                if engine_account == caller {
                    let recipient = FtTransferMessageData::try_from(args.msg.as_str())
                        .map(|data| data.recipient)
                        .unwrap_or_default();
                    let value = Wei::new(U256::from(args.amount.as_u128()));
                    // This transaction mints new ETH, so we'll say it comes from the zero address.
                    NormalizedEthTransaction {
                        address: Address::default(),
                        chain_id: None,
                        nonce: U256::zero(),
                        gas_limit: U256::from(u64::MAX),
                        max_priority_fee_per_gas: U256::zero(),
                        max_fee_per_gas: U256::zero(),
                        to: Some(recipient),
                        value,
                        data: Vec::new(),
                        access_list: Vec::new(),
                        authorization_list: Vec::new(),
                    }
```

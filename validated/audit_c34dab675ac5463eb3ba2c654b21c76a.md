### Title
Missing Zero-Address Validation in `receive_base_tokens` Allows Permanent Freezing of Bridged ETH — (File: `engine/src/engine.rs`)

---

### Summary

`receive_base_tokens` in `engine/src/engine.rs` mints bridged ETH to a recipient address parsed directly from user-controlled calldata (`args.msg`) without ever checking whether that address is `address(0)`. An unprivileged user who passes `"0000000000000000000000000000000000000000"` as the `msg` field in a `ft_transfer_call` to the ETH connector will have their NEP-141 ETH tokens consumed on the NEAR side while the corresponding Aurora-side ETH is minted permanently to `address(0)`, making it irrecoverable.

---

### Finding Description

The `ft_on_transfer` entrypoint in `engine/src/contract_methods/connector.rs` dispatches to `engine.receive_base_tokens(&args)` when the predecessor account is the registered ETH connector: [1](#0-0) 

`receive_base_tokens` then parses the recipient from the user-supplied `args.msg` string via `FtTransferMessageData::try_from` and immediately mints tokens to whatever address results: [2](#0-1) 

The parsing logic in `FtTransferMessageData::try_from` accepts any valid 40-character hex string — including 40 zeros — and returns it as the `recipient` field with no zero-address guard: [3](#0-2) 

There is no downstream check in `receive_base_tokens` or anywhere in the `ft_on_transfer` call chain that rejects `address(0)` as a mint target. The same structural gap exists in `receive_erc20_tokens` for ERC-20 bridge deposits: [4](#0-3) 

---

### Impact Explanation

**Critical — Permanent freezing of funds.**

When ETH is minted to `address(0)`, no private key exists that can sign transactions from that address. The minted Wei is permanently inaccessible. The user's NEP-141 ETH balance on the NEAR connector is already debited at the point `ft_on_transfer` is called, so there is no refund path. The total supply tracked by the connector decreases, but the Aurora-side balance at `address(0)` accumulates and can never be spent or withdrawn.

---

### Likelihood Explanation

**Low-Medium.** The `msg` field in `ft_transfer_call` is entirely user-controlled. Realistic triggers include:

- A buggy or malicious dApp/frontend that does not validate the recipient EVM address before constructing the NEAR cross-contract call.
- A smart contract on NEAR that programmatically bridges ETH and passes a zero-initialized address buffer as the recipient.
- A user who manually constructs the call with a zeroed address by mistake.

No privileged access is required. Any NEAR account that holds ETH connector NEP-141 tokens can trigger this path.

---

### Recommendation

Add an explicit zero-address guard at the start of `receive_base_tokens` (and symmetrically in `receive_erc20_tokens`) immediately after the recipient is parsed:

```rust
// In receive_base_tokens, after line 779:
let receipient = message_data.recipient;
if receipient == Address::zero() {
    return Err(errors::ERR_ZERO_ADDRESS_RECIPIENT);
}
```

Equivalently, the guard can be placed inside `FtTransferMessageData::try_from` in `engine-types/src/parameters/connector.rs` so that all callers benefit automatically. [5](#0-4) 

---

### Proof of Concept

**Attacker-controlled entry path:**

1. Attacker holds any amount of NEP-141 ETH tokens issued by the Aurora ETH connector.
2. Attacker calls `ft_transfer_call` on the ETH connector contract, specifying:
   - `receiver_id`: the Aurora Engine contract account (`aurora`)
   - `amount`: any non-zero amount (e.g., `1000000000000000000` = 1 ETH)
   - `msg`: `"0000000000000000000000000000000000000000"` (40 hex zeros)
3. The ETH connector transfers the NEP-141 tokens to Aurora and calls `ft_on_transfer` on the engine.
4. `ft_on_transfer` routes to `receive_base_tokens` because `predecessor_account_id == get_connector_account_id`.
5. `FtTransferMessageData::try_from("0000000000000000000000000000000000000000")` succeeds, returning `recipient = address(0)`.
6. `set_balance` is called, crediting 1 ETH to `address(0)` in Aurora's EVM state.
7. The attacker's NEP-141 balance is permanently reduced; the 1 ETH on Aurora is permanently frozen at `address(0)`. [6](#0-5) [7](#0-6)

### Citations

**File:** engine/src/contract_methods/connector.rs (L81-83)
```rust
        let result = if predecessor_account_id == get_connector_account_id(&io)? {
            engine.receive_base_tokens(&args)
        } else {
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

**File:** engine/src/engine.rs (L805-816)
```rust
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
```

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

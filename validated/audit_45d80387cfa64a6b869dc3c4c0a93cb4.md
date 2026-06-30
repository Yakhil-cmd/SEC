### Title
Missing Zero-Address Validation in `receive_base_tokens` Allows Permanent ETH Freeze - (File: engine/src/engine.rs)

### Summary

The `receive_base_tokens` function in Aurora Engine mints bridged ETH to whatever recipient address is parsed from the `ft_on_transfer` message, including `Address::zero()`, without any zero-address guard. An unprivileged user can trigger this path by specifying the all-zeros hex string as the recipient when initiating a bridge transfer, permanently freezing their ETH at the zero address with no recovery path.

### Finding Description

The vulnerability class from the reference report is a **missing null/zero-address check** on a recipient field that is assumed to always be non-null. The exact analog exists in Aurora Engine's ETH bridge deposit path.

**Root cause — `FtTransferMessageData::try_from`** in `engine-types/src/parameters/connector.rs`:

The parser accepts any 40-hex-character string as a valid recipient address. It performs no check that the decoded bytes are non-zero. [1](#0-0) 

When the caller supplies `"0000000000000000000000000000000000000000"`, `Address::from_array([0u8; 20])` is returned — i.e., `Address::zero()` — and the function returns `Ok(...)` with no error.

**Root cause — `receive_base_tokens`** in `engine/src/engine.rs`:

The function unconditionally mints ETH to whatever `message_data.recipient` is, including the zero address: [2](#0-1) 

There is no guard of the form `if receipient == Address::zero() { return Err(...) }` anywhere in this path.

**Same class in `receive_erc20_tokens`** in `engine/src/engine.rs`:

The ERC-20 minting path has the same omission — it parses the recipient from the first 40 hex characters of `args.msg` and mints tokens to it without a zero-address check: [3](#0-2) 

**Entry point — `ft_on_transfer`** in `engine/src/contract_methods/connector.rs`:

This is the public NEAR function that dispatches to `receive_base_tokens` when the predecessor is the ETH connector: [4](#0-3) 

The `msg` field of `FtOnTransferArgs` is fully user-controlled — it is the message the user supplies when calling `ft_transfer_call` on the ETH connector NEP-141 contract. [5](#0-4) 

### Impact Explanation

When a user bridges ETH from NEAR to Aurora and specifies `"0000000000000000000000000000000000000000"` as the recipient in the `msg` field:

1. The ETH connector calls `ft_on_transfer` on Aurora with the user-supplied `msg`.
2. `receive_base_tokens` parses `Address::zero()` as the recipient.
3. `set_balance` writes the minted ETH balance to the storage key for the zero address.
4. The ETH is permanently locked at `Address::zero()` — there is no private key for this address, no recovery mechanism, and no admin function to reclaim it.

**Impact: High — Permanent freezing of funds.** The bridged ETH is irreversibly lost. The `set_balance` function writes to a storage slot keyed by the zero address, and no contract method exists to drain or reassign that balance. [6](#0-5) 

### Likelihood Explanation

The entry path is reachable by any unprivileged user who holds ETH on NEAR and initiates a bridge transfer to Aurora. The `msg` field is a plain string parameter under full user control. A user could trigger this accidentally (e.g., by passing an uninitialized or default address) or deliberately (e.g., to grief themselves or test the system). No admin privilege, key compromise, or governance action is required.

### Recommendation

Add a zero-address guard in `receive_base_tokens` immediately after parsing the recipient:

```rust
let receipient = message_data.recipient;
if receipient == Address::zero() {
    return Err(errors::ERR_INVALID_RECIPIENT);
}
```

Apply the same guard in `receive_erc20_tokens` after the recipient is decoded from the message bytes. Optionally, add the check inside `FtTransferMessageData::try_from` so the validation is centralized and cannot be bypassed by future callers.

### Proof of Concept

1. User holds ETH on NEAR (as NEP-141 tokens on the ETH connector contract).
2. User calls `ft_transfer_call` on the ETH connector, specifying:
   - `receiver_id`: the Aurora Engine account (`aurora`)
   - `amount`: any non-zero amount
   - `msg`: `"0000000000000000000000000000000000000000"` (40 hex zeros)
3. The ETH connector calls `ft_on_transfer` on Aurora with `predecessor_account_id == eth_connector_account_id`.
4. `ft_on_transfer` routes to `receive_base_tokens`.
5. `FtTransferMessageData::try_from("0000000000000000000000000000000000000000")` succeeds, returning `recipient = Address::zero()`.
6. `set_balance(&mut self.io, &Address::zero(), &new_balance)` is called, writing the ETH to the zero-address storage slot.
7. The ETH is permanently frozen. The user's NEP-141 tokens have been consumed by the connector, and the minted Aurora ETH is unrecoverable. [7](#0-6) [1](#0-0)

### Citations

**File:** engine-types/src/parameters/connector.rs (L40-56)
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
```

**File:** engine-types/src/parameters/connector.rs (L194-199)
```rust
#[derive(Debug, Clone, BorshSerialize, BorshDeserialize, Deserialize, Serialize, PartialEq, Eq)]
pub struct FtOnTransferArgs {
    pub sender_id: AccountId,
    pub amount: Balance,
    pub msg: String,
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

**File:** engine/src/engine.rs (L1523-1528)
```rust
pub fn set_balance<I: IO>(io: &mut I, address: &Address, balance: &Wei) {
    io.write_storage(
        &address_to_key(KeyPrefix::Balance, address),
        &balance.to_bytes(),
    );
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

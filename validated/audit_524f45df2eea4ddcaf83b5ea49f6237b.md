### Title
Missing Zero-Address Recipient Check in `receive_base_tokens` Allows Permanent Freezing of Bridged ETH — (`File: engine/src/engine.rs`)

### Summary
`receive_base_tokens` in `engine/src/engine.rs` mints ETH to whatever address is parsed from the `msg` field of an `ft_on_transfer` call, including `Address::zero()`, with no zero-address guard. An unprivileged user who holds ETH on NEAR (as a NEP-141 token) can permanently freeze their own funds — or grief the protocol — by specifying the zero address as the recipient.

### Finding Description
`FtTransferMessageData::try_from` in `engine-types/src/parameters/connector.rs` parses the `msg` field of an `ft_on_transfer` call into a recipient `Address`. When the message is exactly 40 hex characters, it decodes them directly into bytes and constructs an `Address` with no zero-address check: [1](#0-0) 

If the caller supplies `msg = "0000000000000000000000000000000000000000"`, the function returns `Address::zero()` as the `recipient` field without error.

`receive_base_tokens` then uses this address unconditionally: [2](#0-1) 

`set_balance` credits the minted ETH to `Address::zero()`. No private key controls that address, so the funds are permanently frozen.

The same pattern exists in `receive_erc20_tokens` for bridged NEP-141 tokens: [3](#0-2) 

Again, no zero-address check before minting ERC-20 tokens to the parsed recipient.

### Impact Explanation
ETH (or bridged ERC-20 tokens) minted to `Address::zero()` are irrecoverable. The NEP-141 tokens are consumed by Aurora (transferred in), but the corresponding EVM-side balance is credited to an address no one controls. This constitutes **permanent freezing of funds**.

### Likelihood Explanation
The entry path is the standard NEP-141 bridge flow: any user holding ETH on NEAR calls `ft_transfer_call` on the ETH connector contract, specifying Aurora as the receiver and `"0000000000000000000000000000000000000000"` as the `msg`. This is a fully permissionless, externally reachable call requiring no special privileges. [4](#0-3) 

The `predecessor_account_id == get_connector_account_id` branch routes to `receive_base_tokens`, completing the vulnerable path.

### Recommendation
Add a zero-address check in `receive_base_tokens` (and `receive_erc20_tokens`) before crediting the balance:

```rust
if receipient == Address::zero() {
    return Err(errors::ERR_ZERO_ADDRESS_RECIPIENT);
}
```

Alternatively, add the check inside `FtTransferMessageData::try_from` so that any zero-address recipient is rejected at parse time, preventing the issue across all callers.

### Proof of Concept
1. User holds ETH on NEAR as NEP-141 (e.g., via the Aurora ETH connector).
2. User calls `ft_transfer_call` on the ETH connector contract:
   - `receiver_id`: `aurora` (the Aurora Engine contract)
   - `amount`: any nonzero amount
   - `msg`: `"0000000000000000000000000000000000000000"` (40 hex zeros)
3. The ETH connector calls `ft_on_transfer` on Aurora with `predecessor = eth_connector_account_id`.
4. `ft_on_transfer` routes to `receive_base_tokens`.
5. `FtTransferMessageData::try_from("0000000000000000000000000000000000000000")` succeeds and returns `recipient = Address::zero()`.
6. `set_balance` credits the ETH to `Address::zero()`.
7. The user's NEP-141 ETH is consumed; the EVM-side ETH is permanently frozen at the zero address. [5](#0-4) [6](#0-5)

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

**File:** engine/src/contract_methods/connector.rs (L61-90)
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
```

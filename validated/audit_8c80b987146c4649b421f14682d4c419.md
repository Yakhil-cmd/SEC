### Title
Missing Zero-Address Recipient Validation in `receive_erc20_tokens()` Allows Permanent Freezing of Bridged Funds - (File: engine/src/engine.rs)

### Summary
`receive_erc20_tokens()` parses the recipient EVM address from the `msg` field of `FtOnTransferArgs` but performs no check that the resulting address is non-zero. When a user bridges a NEP-141 token to Aurora with `msg = "0000000000000000000000000000000000000000"`, the function proceeds to mint the corresponding ERC-20 tokens to `Address::zero()`. The NEP-141 tokens remain locked inside Aurora's custody while the minted ERC-20 tokens are irrecoverable, permanently freezing the bridged funds.

### Finding Description
In `engine/src/engine.rs`, `receive_erc20_tokens()` is the handler for the `ft_on_transfer` NEAR callback that fires whenever a NEP-141 token is transferred to the Aurora contract. It parses the recipient address from `args.msg`:

```rust
let mut recipient = {
    let message = args.msg.strip_prefix("0x").unwrap_or(&args.msg);
    if message.len() < 40 {
        return Err(ParseOnTransferMessageError::WrongMessageFormat.into());
    }
    let mut address_bytes = [0; 20];
    hex::decode_to_slice(&message[..40], &mut address_bytes)
        .map_err(|_| ParseOnTransferMessageError::WrongMessageFormat)?;
    Address::from_array(address_bytes)
};
```

The only validation performed is:
1. The message is at least 40 characters long.
2. The first 40 characters are valid hex.

There is **no check that the decoded address is non-zero**. A 40-character string of `"0"` passes both checks and produces `Address::zero()`.

The subsequent silo-mode guard:

```rust
if let Some(fallback_address) = silo::get_erc20_fallback_address(&self.io)
    && !silo::is_allow_receive_erc20_tokens(&self.io, &recipient)
{
    recipient = fallback_address;
}
```

only redirects to a fallback address when silo mode is active **and** the address is not whitelisted. On standard Aurora (non-silo), this block is a no-op and the zero address is used as-is.

The function then calls the ERC-20 mirror contract's `mint` selector with the zero address as the recipient:

```rust
let result = self
    .call(
        &erc20_admin_address,
        &erc20_token,
        Wei::zero(),
        setup_receive_erc20_tokens_input(&recipient, amount),
        u64::MAX,
        ...
    )
    .and_then(submit_result_or_err)?;
```

The ERC-20 mint succeeds, tokens are credited to `0x0000…0000`, and the NEP-141 tokens remain locked in Aurora's custody. Neither side of the bridge position can be unwound: the zero-address ERC-20 tokens cannot be burned to trigger a withdrawal, and the NEP-141 tokens cannot be reclaimed.

### Impact Explanation
**Permanent freezing of bridged ERC-20 funds.** The bridge invariant — "NEP-141 tokens locked in Aurora ≡ ERC-20 tokens in user-accessible circulation" — is broken. The NEP-141 tokens are permanently held by the Aurora contract with no corresponding redeemable ERC-20 position. The user loses their entire bridged amount with no recovery path.

### Likelihood Explanation
Any user who calls `ft_transfer_call` on a NEP-141 token with `receiver_id = aurora` and `msg = "0000000000000000000000000000000000000000"` triggers this path. This can happen through:
- A user error (e.g., forgetting to set the recipient address).
- A malicious front-end or integration that supplies a zeroed `msg`.
- A smart contract that constructs the `msg` incorrectly.

The call is fully permissionless and requires no special privileges. Likelihood is **medium** (requires a specific malformed input, but the input is trivially constructable and the error is easy to make).

### Recommendation
Add an explicit early-exit guard immediately after the recipient address is decoded, before any state-mutating logic executes:

```rust
if recipient == Address::zero() {
    return Err(errors::ERR_INVALID_RECIPIENT.into());
}
```

This mirrors the fix applied in the referenced report: functions should exit early when an essential parameter resolves to an invalid/degenerate value (empty address / zero address).

### Proof of Concept
1. Deploy or use any existing NEP-141 token on NEAR that has Aurora registered as a receiver.
2. Call `ft_transfer_call` with:
   - `receiver_id`: Aurora engine account (`aurora`)
   - `amount`: any non-zero amount (e.g., `1000`)
   - `msg`: `"0000000000000000000000000000000000000000"`
3. Aurora's `ft_on_transfer` fires → `receive_erc20_tokens` is called.
4. Recipient is decoded as `Address::zero()`.
5. The ERC-20 mirror's `mint(0x0000…0000, 1000)` is executed successfully.
6. Observe: NEP-141 balance of Aurora increased by 1000; ERC-20 balance of `0x0000…0000` increased by 1000; the user's ERC-20 balance is 0 and the NEP-141 tokens are permanently locked. [1](#0-0) [2](#0-1) [3](#0-2)

### Citations

**File:** engine/src/engine.rs (L796-816)
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
```

**File:** engine/src/engine.rs (L818-837)
```rust
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
```

**File:** engine/src/contract_methods/connector.rs (L62-109)
```rust
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

        #[allow(clippy::used_underscore_binding)]
        let amount_to_return = if let Err(_err) = &result {
            sdk::log!("Error in ft_on_transfer: {_err:?}");
            // An error occurred, so we need to return the amount of tokens to the sender.
            args.amount.as_u128()
        } else {
            // Everything is ok, so return 0.
            0
        };

        let output = crate::prelude::format!("\"{amount_to_return}\"");
        io.return_output(output.as_bytes());

        // In case of an error, we just return Ok(None) to avoid a panic in the contract. It's ok
        // because in case of an error, we already returned the amount of tokens to the sender.
        Ok(result.unwrap_or(None))
    })
}
```

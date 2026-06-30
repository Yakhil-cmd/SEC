### Title
Missing Zero Address Check in `set_erc20_fallback_address` and `set_silo_params` Can Permanently Freeze Bridged ERC-20 Tokens - (File: engine/src/contract_methods/silo/mod.rs)

### Summary
The Silo mode functions `set_erc20_fallback_address` and `set_silo_params` accept an EVM `Address` parameter that is stored without any zero-address validation. If the owner accidentally sets `erc20_fallback_address` to `Address::zero()`, all subsequent NEP-141 → ERC-20 bridge transfers destined for non-whitelisted recipients will have their tokens minted to the zero address, permanently freezing them.

### Finding Description
In Silo mode, `receive_erc20_tokens` in `engine/src/engine.rs` redirects ERC-20 mints to the configured `erc20_fallback_address` whenever the intended recipient is not in the whitelist:

```rust
if let Some(fallback_address) = silo::get_erc20_fallback_address(&self.io)
    && !silo::is_allow_receive_erc20_tokens(&self.io, &recipient)
{
    recipient = fallback_address;
}
``` [1](#0-0) 

The `erc20_fallback_address` is set via two entry points. The first is `set_erc20_fallback_address` in `engine/src/lib.rs`, which reads the `Erc20FallbackAddressArgs` and passes `args.address` directly to `silo::set_erc20_fallback_address` with no zero-address check:

```rust
let args: Erc20FallbackAddressArgs = io.read_input_borsh().sdk_unwrap();
silo::set_erc20_fallback_address(&mut io, args.address);
``` [2](#0-1) 

The second is `set_silo_params`, which accepts `Option<SiloParamsArgs>` where `erc20_fallback_address: Address` is a **non-optional** field, and passes it through without validation:

```rust
let args: Option<SiloParamsArgs> = io.read_input_borsh().sdk_unwrap();
silo::set_silo_params(&mut io, args);
``` [3](#0-2) 

Inside `silo::set_silo_params`, the address is forwarded to `set_erc20_fallback_address`:

```rust
pub fn set_silo_params<I: IO>(io: &mut I, args: Option<SiloParamsArgs>) {
    let (cost, address) = args.map_or((None, None), |params| {
        (Some(params.fixed_gas), Some(params.erc20_fallback_address))
    });
    set_fixed_gas(io, cost);
    set_erc20_fallback_address(io, address);
}
``` [4](#0-3) 

And `set_erc20_fallback_address` writes any non-`None` address to storage unconditionally:

```rust
pub fn set_erc20_fallback_address<I: IO>(io: &mut I, address: Option<Address>) {
    let key = erc20_fallback_address_key();
    if let Some(address) = address {
        io.write_storage(&key, address.as_bytes());
    } else {
        io.remove_storage(&key);
    }
}
``` [5](#0-4) 

The `SiloParamsArgs` struct makes the risk concrete: `erc20_fallback_address` is a bare `Address` (not `Option<Address>`), so the Rust default value and any zero-initialized struct will silently produce `Address::zero()`: [6](#0-5) 

### Impact Explanation
When `erc20_fallback_address` is set to `Address::zero()`, every call to `ft_on_transfer` for a NEP-141 token whose intended EVM recipient is not whitelisted will cause `receive_erc20_tokens` to mint the bridged ERC-20 tokens to `0x0000000000000000000000000000000000000000`. No private key controls the zero address; those tokens are permanently frozen. The underlying NEP-141 tokens remain locked in the Aurora contract with no corresponding recoverable ERC-20 balance. This matches the **Critical — Permanent freezing of funds** impact class.

### Likelihood Explanation
The owner is the only account that can call `set_erc20_fallback_address` or `set_silo_params`. The risk of accidental misconfiguration is elevated because `SiloParamsArgs.erc20_fallback_address` is a non-optional `Address` field — a Rust default-initialization, a copy-paste error, or an off-by-one in calldata encoding silently produces the zero address. Unlike the original Canto finding where the initializer could only be called once, here the owner can correct the address in a follow-up transaction; however, any tokens minted to the zero address during the misconfiguration window are irrecoverably lost.

### Recommendation
Add an explicit zero-address guard in `set_erc20_fallback_address` before writing to storage:

```rust
pub fn set_erc20_fallback_address<I: IO>(io: &mut I, address: Option<Address>) {
    let key = erc20_fallback_address_key();
    if let Some(address) = address {
        assert!(address != Address::zero(), "ERR_ZERO_FALLBACK_ADDRESS");
        io.write_storage(&key, address.as_bytes());
    } else {
        io.remove_storage(&key);
    }
}
```

Apply the same guard in `set_silo_params` before delegating to `set_erc20_fallback_address`.

### Proof of Concept
1. Owner calls `set_silo_params` (or `set_erc20_fallback_address`) with `erc20_fallback_address = Address::zero()`. No error is returned; the zero address is written to storage.
2. The Address whitelist is enabled (Silo mode active).
3. A user bridges NEP-141 tokens to a non-whitelisted EVM address by calling `ft_on_transfer` on the Aurora contract.
4. `receive_erc20_tokens` reads `get_erc20_fallback_address` → returns `Some(Address::zero())`. The recipient is not whitelisted, so `recipient` is overwritten with `Address::zero()`.
5. `setup_receive_erc20_tokens_input` encodes a `mint(0x000...000, amount)` call; the ERC-20 contract mints `amount` tokens to the zero address.
6. The user's bridged tokens are permanently frozen at `0x0000000000000000000000000000000000000000` with no recovery path. [7](#0-6) [8](#0-7)

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

**File:** engine/src/lib.rs (L813-814)
```rust
        let args: Erc20FallbackAddressArgs = io.read_input_borsh().sdk_unwrap();
        silo::set_erc20_fallback_address(&mut io, args.address);
```

**File:** engine/src/lib.rs (L837-838)
```rust
        let args: Option<SiloParamsArgs> = io.read_input_borsh().sdk_unwrap();
        silo::set_silo_params(&mut io, args);
```

**File:** engine/src/contract_methods/silo/mod.rs (L31-38)
```rust
pub fn set_silo_params<I: IO>(io: &mut I, args: Option<SiloParamsArgs>) {
    let (cost, address) = args.map_or((None, None), |params| {
        (Some(params.fixed_gas), Some(params.erc20_fallback_address))
    });

    set_fixed_gas(io, cost);
    set_erc20_fallback_address(io, address);
}
```

**File:** engine/src/contract_methods/silo/mod.rs (L59-73)
```rust
pub fn get_erc20_fallback_address<I: IO>(io: &I) -> Option<Address> {
    let key = erc20_fallback_address_key();
    io.read_storage(&key)?.to_value().ok()
}

/// Set ERC-20 fallback address.
pub fn set_erc20_fallback_address<I: IO>(io: &mut I, address: Option<Address>) {
    let key = erc20_fallback_address_key();

    if let Some(address) = address {
        io.write_storage(&key, address.as_bytes());
    } else {
        io.remove_storage(&key);
    }
}
```

**File:** engine-types/src/parameters/silo.rs (L15-24)
```rust
#[derive(Debug, Default, Clone, PartialEq, Eq, BorshSerialize, BorshDeserialize)]
pub struct SiloParamsArgs {
    /// Fixed amount of gas per transaction.
    pub fixed_gas: EthGas,
    /// EVM address, which is used for withdrawing ERC-20 base tokens in case
    /// a recipient of the tokens is not in the silo white list.
    /// Note: the logic described above works only if the fallback address
    /// is set by `set_silo_params` function. In other words, in Silo mode.
    pub erc20_fallback_address: Address,
}
```

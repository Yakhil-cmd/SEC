### Title
ERC-20 Fallback Address Accepts Zero Address, Permanently Freezing Bridged Tokens - (File: `engine/src/contract_methods/silo/mod.rs`)

### Summary
In Silo mode, `set_erc20_fallback_address` and `set_silo_params` accept `Address::zero()` as a valid fallback address without any zero-address validation. When the `Address` whitelist is active and the fallback is set to the zero address, every `ft_on_transfer` call targeting a non-whitelisted EVM address will mint ERC-20 tokens to `Address::zero()`, permanently freezing the bridged funds.

### Finding Description
`SiloParamsArgs` derives `Default`, making `erc20_fallback_address` default to `Address::zero()`:

```rust
#[derive(Debug, Default, Clone, PartialEq, Eq, BorshSerialize, BorshDeserialize)]
pub struct SiloParamsArgs {
    pub fixed_gas: EthGas,
    pub erc20_fallback_address: Address,  // defaults to Address::zero()
}
``` [1](#0-0) 

`set_erc20_fallback_address` stores whatever address is provided, including `Address::zero()`, with no validation:

```rust
pub fn set_erc20_fallback_address<I: IO>(io: &mut I, address: Option<Address>) {
    let key = erc20_fallback_address_key();
    if let Some(address) = address {
        io.write_storage(&key, address.as_bytes()); // no zero-address check
    } else {
        io.remove_storage(&key);
    }
}
``` [2](#0-1) 

`set_silo_params` similarly passes the address through without validation: [3](#0-2) 

In `receive_erc20_tokens`, when the fallback address is set and the recipient is not whitelisted, the recipient is unconditionally replaced with the stored fallback address — including `Address::zero()`:

```rust
if let Some(fallback_address) = silo::get_erc20_fallback_address(&self.io)
    && !silo::is_allow_receive_erc20_tokens(&self.io, &recipient)
{
    recipient = fallback_address;  // could be Address::zero()
}
``` [4](#0-3) 

The ERC-20 mint call then executes with `recipient = Address::zero()`, burning the tokens inside the EVM state: [5](#0-4) 

The test suite itself demonstrates this misconfiguration is realistic — it uses `Address::zero()` as the fallback in unit tests:

```rust
const ERC20_FALLBACK_ADDRESS: Address = Address::zero();
const SILO_PARAMS_ARGS: SiloParamsArgs = SiloParamsArgs {
    fixed_gas: FIXED_GAS,
    erc20_fallback_address: ERC20_FALLBACK_ADDRESS,
};
``` [6](#0-5) 

### Impact Explanation
Any NEP-141 token bridged into Aurora via `ft_on_transfer` targeting a non-whitelisted EVM address will have its ERC-20 tokens minted to `Address::zero()`. Those tokens are permanently inaccessible — no private key controls `Address::zero()`. The NEP-141 tokens are already debited from the sender on the NEAR side, so the loss is permanent. This is **Critical: Permanent freezing of funds**.

### Likelihood Explanation
The `SiloParamsArgs` struct derives `Default`, making `Address::zero()` the natural default value for `erc20_fallback_address`. An operator initializing Silo mode with default struct values, or forgetting to populate the fallback field, silently configures the zero address. The `set_erc20_fallback_address` entrypoint also accepts `Some(Address::zero())` directly with no rejection. Once the `Address` whitelist is enabled, every subsequent `ft_on_transfer` to a non-whitelisted address triggers the freeze. The entry path (`ft_on_transfer`) is reachable by any unprivileged token holder.

### Recommendation
Add a zero-address guard in both `set_erc20_fallback_address` and `set_silo_params`:

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

Similarly, validate `params.erc20_fallback_address != Address::zero()` inside `set_silo_params` before storing. [2](#0-1) 

### Proof of Concept
1. Owner calls `set_silo_params(Some(SiloParamsArgs { fixed_gas: X, erc20_fallback_address: Address::zero() }))` — accepted without error.
2. Owner calls `set_whitelist_status(WhitelistStatusArgs { kind: WhitelistKind::Address, active: true })` — enables the address whitelist.
3. Any user calls NEP-141 `ft_transfer_call` targeting Aurora with a non-whitelisted EVM recipient address in the `msg` field.
4. Aurora's `ft_on_transfer` → `receive_erc20_tokens` executes. `get_erc20_fallback_address` returns `Some(Address::zero())`. `is_allow_receive_erc20_tokens` returns `false` (address not whitelisted). `recipient` is overwritten with `Address::zero()`.
5. The ERC-20 `mint` call executes with `to = Address::zero()`. Tokens are credited to the zero address inside the EVM and are permanently unrecoverable. The NEP-141 balance on NEAR is already debited. [7](#0-6) [8](#0-7)

### Citations

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

**File:** engine-tests/src/tests/silo.rs (L28-32)
```rust
const ERC20_FALLBACK_ADDRESS: Address = Address::zero();
const SILO_PARAMS_ARGS: SiloParamsArgs = SiloParamsArgs {
    fixed_gas: FIXED_GAS,
    erc20_fallback_address: ERC20_FALLBACK_ADDRESS,
};
```

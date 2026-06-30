### Title
Missing Zero-Address Validation for ERC-20 Fallback Address in Silo Mode - (`engine/src/contract_methods/silo/mod.rs`)

### Summary
The `set_erc20_fallback_address` and `set_silo_params` functions in Aurora Engine's Silo mode accept `Address::zero()` as a valid ERC-20 fallback address without any non-zero validation. When Silo mode is active with a zero fallback address, any ERC-20 token transfer to a non-whitelisted recipient is permanently redirected to `Address::zero()`, freezing those funds irreversibly.

### Finding Description
In Silo mode, the `erc20_fallback_address` is the EVM address that receives ERC-20 tokens when the intended recipient is not on the address whitelist. This address is set via two entry points:

1. `set_erc20_fallback_address` in `engine/src/lib.rs` (lines 806–815), which reads `Erc20FallbackAddressArgs { address: Option<Address> }` and calls `silo::set_erc20_fallback_address`.
2. `set_silo_params` in `engine/src/lib.rs` (lines 830–839), which reads `Option<SiloParamsArgs>` containing `erc20_fallback_address: Address` and calls `silo::set_silo_params`.

Neither entry point validates that the provided address is non-zero. The underlying storage function `silo::set_erc20_fallback_address` simply writes whatever `Some(address)` is provided:

```rust
// engine/src/contract_methods/silo/mod.rs, lines 65-73
pub fn set_erc20_fallback_address<I: IO>(io: &mut I, address: Option<Address>) {
    let key = erc20_fallback_address_key();
    if let Some(address) = address {
        io.write_storage(&key, address.as_bytes()); // no zero-check
    } else {
        io.remove_storage(&key);
    }
}
```

When `Address::zero()` is stored, `get_erc20_fallback_address` returns `Some(Address::zero())`. In `receive_erc20_tokens` (`engine/src/engine.rs`, lines 818–822), the fallback logic then redirects tokens to the zero address:

```rust
if let Some(fallback_address) = silo::get_erc20_fallback_address(&self.io)
    && !silo::is_allow_receive_erc20_tokens(&self.io, &recipient)
{
    recipient = fallback_address; // Address::zero() — tokens are burned
}
```

The test suite itself uses `const ERC20_FALLBACK_ADDRESS: Address = Address::zero()` as a test constant, confirming the contract accepts this value without error.

### Impact Explanation
**Critical — Permanent freezing of funds.**

When Silo mode is active with `erc20_fallback_address = Address::zero()` and the address whitelist is enabled, every ERC-20 token transfer via `ft_transfer_call` / `ft_on_transfer` to a non-whitelisted recipient mints tokens to `Address::zero()`. The zero address has no private key; tokens sent there are permanently unrecoverable. This affects all bridged NEP-141 tokens operating through the Silo ERC-20 bridge path.

### Likelihood Explanation
**Medium.** The owner must call `set_silo_params` or `set_erc20_fallback_address` with a zero address — this is a realistic misconfiguration during deployment or testing (the test suite itself uses `Address::zero()` as the fallback address constant). There is no on-chain guard to prevent it. Once set and the whitelist is enabled, every subsequent non-whitelisted ERC-20 transfer silently burns tokens.

### Recommendation
Add a non-zero address check in `silo::set_erc20_fallback_address` and in the `set_silo_params` path before writing to storage:

```rust
pub fn set_erc20_fallback_address<I: IO>(io: &mut I, address: Option<Address>) {
    let key = erc20_fallback_address_key();
    if let Some(address) = address {
        if address.is_zero() {
            // Return an error or panic — zero address is not a valid fallback
            sdk::panic_utf8(b"ERR_ZERO_FALLBACK_ADDRESS");
        }
        io.write_storage(&key, address.as_bytes());
    } else {
        io.remove_storage(&key);
    }
}
```

Apply the same guard inside `set_silo_params` when `erc20_fallback_address` is extracted from `SiloParamsArgs`.

### Proof of Concept

**Root cause — no zero-check in storage setter:** [1](#0-0) 

**Contract entrypoint for `set_erc20_fallback_address` — no validation before calling setter:** [2](#0-1) 

**Contract entrypoint for `set_silo_params` — no validation before calling setter:** [3](#0-2) 

**Token redirection logic — uses stored fallback address unconditionally:** [4](#0-3) 

**`SiloParamsArgs` struct — `erc20_fallback_address` is a plain `Address` with no constraints:** [5](#0-4) 

**Test constant confirming zero address is accepted without error:** [6](#0-5) 

**Attack path:**
1. Owner (legitimately or by mistake) calls `set_silo_params` with `erc20_fallback_address: Address::zero()` and a non-zero `fixed_gas`, or calls `set_erc20_fallback_address(Some(Address::zero()))`.
2. Owner enables the `Address` whitelist via `set_whitelist_status`.
3. Any user calls `ft_transfer_call` on a NEP-141 token targeting a non-whitelisted EVM address.
4. `receive_erc20_tokens` fires, detects the recipient is not whitelisted, and redirects to `Address::zero()`.
5. ERC-20 tokens are minted to `Address::zero()` — permanently frozen with no recovery path.

### Citations

**File:** engine/src/contract_methods/silo/mod.rs (L65-73)
```rust
pub fn set_erc20_fallback_address<I: IO>(io: &mut I, address: Option<Address>) {
    let key = erc20_fallback_address_key();

    if let Some(address) = address {
        io.write_storage(&key, address.as_bytes());
    } else {
        io.remove_storage(&key);
    }
}
```

**File:** engine/src/lib.rs (L805-815)
```rust
    #[unsafe(no_mangle)]
    pub extern "C" fn set_erc20_fallback_address() {
        let mut io = Runtime;
        let state = state::get_state(&io).sdk_unwrap();
        require_owner_and_running(&state, &io.predecessor_account_id())
            .map_err(ContractError::msg)
            .sdk_unwrap();

        let args: Erc20FallbackAddressArgs = io.read_input_borsh().sdk_unwrap();
        silo::set_erc20_fallback_address(&mut io, args.address);
    }
```

**File:** engine/src/lib.rs (L829-839)
```rust
    #[unsafe(no_mangle)]
    pub extern "C" fn set_silo_params() {
        let mut io = Runtime;
        let state = state::get_state(&io).sdk_unwrap();
        require_owner_and_running(&state, &io.predecessor_account_id())
            .map_err(ContractError::msg)
            .sdk_unwrap();

        let args: Option<SiloParamsArgs> = io.read_input_borsh().sdk_unwrap();
        silo::set_silo_params(&mut io, args);
    }
```

**File:** engine/src/engine.rs (L818-822)
```rust
        if let Some(fallback_address) = silo::get_erc20_fallback_address(&self.io)
            && !silo::is_allow_receive_erc20_tokens(&self.io, &recipient)
        {
            recipient = fallback_address;
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

**File:** engine-tests/src/tests/silo.rs (L28-32)
```rust
const ERC20_FALLBACK_ADDRESS: Address = Address::zero();
const SILO_PARAMS_ARGS: SiloParamsArgs = SiloParamsArgs {
    fixed_gas: FIXED_GAS,
    erc20_fallback_address: ERC20_FALLBACK_ADDRESS,
};
```

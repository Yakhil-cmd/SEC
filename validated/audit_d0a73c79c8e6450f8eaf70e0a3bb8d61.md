### Title
Missing Zero-Address Validation in `set_silo_params` / `set_erc20_fallback_address` Enables Permanent Burning of Bridged ERC-20 Tokens - (File: engine/src/contract_methods/silo/mod.rs)

### Summary
The `set_silo_params` and `set_erc20_fallback_address` contract entry points accept an `erc20_fallback_address` parameter with no zero-address guard. If the contract owner accidentally supplies `Address::zero()`, every subsequent NEP-141 → ERC-20 bridge transfer whose intended EVM recipient is not whitelisted will have its tokens permanently redirected to the zero address, constituting an irreversible loss of bridged user funds.

### Finding Description
`set_silo_params` (engine/src/lib.rs:830-839) and `set_erc20_fallback_address` (engine/src/lib.rs:806-815) both gate on `require_owner_and_running` and then delegate directly to the storage layer with no further validation:

```rust
// engine/src/lib.rs:837-838
let args: Option<SiloParamsArgs> = io.read_input_borsh().sdk_unwrap();
silo::set_silo_params(&mut io, args);
```

The underlying `set_erc20_fallback_address` in `engine/src/contract_methods/silo/mod.rs` (lines 65-73) writes any non-`None` address, including `Address::zero()`, directly to storage:

```rust
pub fn set_erc20_fallback_address<I: IO>(io: &mut I, address: Option<Address>) {
    let key = erc20_fallback_address_key();
    if let Some(address) = address {
        io.write_storage(&key, address.as_bytes()); // no zero-address check
    } else {
        io.remove_storage(&key);
    }
}
```

At token-receipt time, `receive_erc20_tokens` in `engine/src/engine.rs` (lines 818-822) reads the stored fallback and unconditionally substitutes it for any non-whitelisted recipient:

```rust
if let Some(fallback_address) = silo::get_erc20_fallback_address(&self.io)
    && !silo::is_allow_receive_erc20_tokens(&self.io, &recipient)
{
    recipient = fallback_address;   // zero address if misconfigured
}
```

The ERC-20 `mint` call then targets `Address::zero()`. Tokens minted to the zero address are permanently inaccessible.

The `SiloParamsArgs` struct itself imposes no constraint on `erc20_fallback_address`:

```rust
// engine-types/src/parameters/silo.rs:15-24
pub struct SiloParamsArgs {
    pub fixed_gas: EthGas,
    pub erc20_fallback_address: Address,   // no invariant enforced
}
```

Notably, the test suite itself hard-codes `Address::zero()` as the fallback constant, confirming the code path is reachable with a zero address:

```rust
// engine-tests/src/tests/silo.rs:28-32
const ERC20_FALLBACK_ADDRESS: Address = Address::zero();
const SILO_PARAMS_ARGS: SiloParamsArgs = SiloParamsArgs {
    fixed_gas: FIXED_GAS,
    erc20_fallback_address: ERC20_FALLBACK_ADDRESS,
};
```

### Impact Explanation
**Permanent freezing of bridged ERC-20 funds.** Once `erc20_fallback_address` is set to zero and the Address whitelist is active, every `ft_on_transfer` call from a NEP-141 contract whose intended EVM recipient is not whitelisted will mint ERC-20 tokens to `Address::zero()`. Those tokens are permanently unrecoverable. The NEP-141 side has already transferred the tokens to Aurora, so the sender loses them with no recourse.

### Likelihood Explanation
The owner must supply `Address::zero()` (or omit a proper address) when calling `set_silo_params` or `set_erc20_fallback_address`. This is an accidental-misconfiguration scenario — exactly the class of mistake the external report targets. The absence of any guard makes the mistake trivially possible during deployment or reconfiguration of a Silo instance. Likelihood is low-to-medium given that Silo mode is an active production feature and the zero address is a natural default/placeholder value an operator might inadvertently use.

### Recommendation
Add a zero-address guard in both `set_erc20_fallback_address` and `set_silo_params`:

```rust
// engine/src/contract_methods/silo/mod.rs
pub fn set_erc20_fallback_address<I: IO>(io: &mut I, address: Option<Address>) {
    let key = erc20_fallback_address_key();
    if let Some(address) = address {
        // Analog of: require(vibeTreasury_ != address(0))
        assert!(!address.is_zero(), "ERR_ZERO_FALLBACK_ADDRESS");
        io.write_storage(&key, address.as_bytes());
    } else {
        io.remove_storage(&key);
    }
}
```

Apply the same guard inside `set_silo_params` before delegating to `set_erc20_fallback_address`.

### Proof of Concept
1. Owner calls `set_silo_params(Some(SiloParamsArgs { fixed_gas: X, erc20_fallback_address: Address::zero() }))` — accepted without error.
2. Owner enables the Address whitelist via `set_whitelist_status`.
3. Any user sends NEP-141 tokens to Aurora via `ft_on_transfer` specifying a non-whitelisted EVM recipient.
4. `receive_erc20_tokens` (engine/src/engine.rs:818-822) detects the recipient is not whitelisted, reads `fallback_address = Address::zero()`, and substitutes it.
5. The ERC-20 `mint` call executes with `recipient = 0x0000…0000`.
6. Tokens are minted to the zero address and permanently burned; the user's NEP-141 tokens are gone. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5)

### Citations

**File:** engine/src/contract_methods/silo/mod.rs (L64-73)
```rust
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

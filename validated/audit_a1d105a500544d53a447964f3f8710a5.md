### Title
ERC-20 Tokens Permanently Burned When `erc20_fallback_address` Is Set to Zero Address - (File: `engine/src/engine.rs`)

---

### Summary

In Silo mode, `Engine::receive_erc20_tokens` redirects incoming ERC-20 token mints to a configured `erc20_fallback_address` when the intended recipient is not whitelisted. No check is performed to ensure this fallback address is non-zero before using it as the mint target. Because `SiloParamsArgs` derives `Default` (making `erc20_fallback_address` default to `Address::zero()`), and `set_erc20_fallback_address` accepts and stores `Address::zero()` without rejection, any non-whitelisted `ft_on_transfer` call will permanently burn the bridged ERC-20 tokens by minting them to the zero address.

---

### Finding Description

`Engine::receive_erc20_tokens` in `engine/src/engine.rs` handles incoming NEP-141 token transfers via `ft_on_transfer`. In Silo mode, if the intended recipient is not in the address whitelist, the code substitutes the `erc20_fallback_address` as the mint target:

```rust
// engine/src/engine.rs lines 818-822
if let Some(fallback_address) = silo::get_erc20_fallback_address(&self.io)
    && !silo::is_allow_receive_erc20_tokens(&self.io, &recipient)
{
    recipient = fallback_address;   // ← no zero-address check
}
```

The fallback address is then passed directly to `setup_receive_erc20_tokens_input(&recipient, amount)` and used as the ERC-20 `mint(address, uint256)` target.

The setter `set_erc20_fallback_address` in `engine/src/contract_methods/silo/mod.rs` (lines 65–72) stores any non-`None` address unconditionally:

```rust
if let Some(address) = address {
    io.write_storage(&key, address.as_bytes());  // ← zero address accepted
}
```

`SiloParamsArgs` in `engine-types/src/parameters/silo.rs` (lines 15–24) derives `Default`, so `erc20_fallback_address` defaults to `Address::zero()`. The test suite itself uses this default:

```rust
// engine-tests/src/tests/silo.rs lines 28-32
const ERC20_FALLBACK_ADDRESS: Address = Address::zero();
const SILO_PARAMS_ARGS: SiloParamsArgs = SiloParamsArgs {
    fixed_gas: FIXED_GAS,
    erc20_fallback_address: ERC20_FALLBACK_ADDRESS,
};
```

When `set_silo_params(Some(SiloParamsArgs::default()))` is called (or any call where `erc20_fallback_address` is left as zero), the zero address is persisted. Subsequently, every `ft_on_transfer` call whose recipient is not whitelisted will mint the bridged ERC-20 tokens to `address(0)`, permanently destroying them.

---

### Impact Explanation

**Critical — Permanent freezing of funds.**

ERC-20 tokens minted to `address(0)` inside the Aurora EVM are irrecoverable. The NEP-141 tokens have already been transferred into the Aurora contract on the NEAR side (the bridge has accepted them), but the corresponding ERC-20 balance is credited to the zero address, which no private key controls. The funds are permanently frozen with no recovery path.

---

### Likelihood Explanation

**Low.**

The scenario requires the silo operator to configure `erc20_fallback_address` as `Address::zero()`. This can happen:
1. By explicitly passing `SiloParamsArgs::default()` (the zero address is the Rust default).
2. By calling `set_silo_params` with a struct where `erc20_fallback_address` was not explicitly initialized.
3. By calling `set_erc20_fallback_address(Some(Address::zero()))` directly.

The operator must also have the address whitelist enabled (otherwise the fallback path is never triggered). Once both conditions hold, any unprivileged user bridging NEP-141 tokens to a non-whitelisted address triggers the burn — no further attacker action is needed.

---

### Recommendation

Add a zero-address guard in `receive_erc20_tokens` before substituting the fallback:

```rust
if let Some(fallback_address) = silo::get_erc20_fallback_address(&self.io)
    && !fallback_address.is_zero()                          // ← add this guard
    && !silo::is_allow_receive_erc20_tokens(&self.io, &recipient)
{
    recipient = fallback_address;
}
```

Additionally, reject the zero address in `set_erc20_fallback_address` and `set_silo_params` so the misconfiguration is caught at write time rather than at token-transfer time.

---

### Proof of Concept

1. Silo operator calls `set_silo_params` with `erc20_fallback_address: Address::zero()` and enables the `Address` whitelist.
2. A NEP-141 token contract calls `ft_on_transfer` on the Aurora engine with `sender_id = alice`, `amount = 1000`, `msg = <non-whitelisted EVM address hex>`.
3. `ft_on_transfer` → `connector::ft_on_transfer` → `engine.receive_erc20_tokens`.
4. `silo::get_erc20_fallback_address` returns `Some(Address::zero())`.
5. `silo::is_allow_receive_erc20_tokens` returns `false` (recipient not whitelisted).
6. `recipient` is overwritten with `Address::zero()`.
7. `setup_receive_erc20_tokens_input(&Address::zero(), 1000)` encodes a `mint(0x0000…0000, 1000)` call.
8. The ERC-20 contract mints 1000 tokens to `address(0)` — permanently burned.
9. The NEP-141 side returns `"0"` (success), so the bridge does not refund Alice. Funds are lost. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3)

### Citations

**File:** engine/src/engine.rs (L818-822)
```rust
        if let Some(fallback_address) = silo::get_erc20_fallback_address(&self.io)
            && !silo::is_allow_receive_erc20_tokens(&self.io, &recipient)
        {
            recipient = fallback_address;
        }
```

**File:** engine/src/contract_methods/silo/mod.rs (L65-72)
```rust
pub fn set_erc20_fallback_address<I: IO>(io: &mut I, address: Option<Address>) {
    let key = erc20_fallback_address_key();

    if let Some(address) = address {
        io.write_storage(&key, address.as_bytes());
    } else {
        io.remove_storage(&key);
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

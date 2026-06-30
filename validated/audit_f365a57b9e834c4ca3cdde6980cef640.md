### Title
Silent U256-to-u128 Truncation in `exitToNear` ERC-20 Bridge Precompile Causes Permanent Token Loss - (File: `engine-precompiles/src/native.rs`)

---

### Summary

The `exitToNear` precompile silently truncates a U256 ERC-20 token amount to u128 when constructing the NEAR-side `ft_transfer` call. Because the ERC-20 burn on Aurora consumes the full U256 amount while NEAR only receives the low 128 bits, any user bridging an ERC-20 token whose amount exceeds `u128::MAX` permanently loses the high-order portion of their tokens.

---

### Finding Description

The Aurora Engine type system defines several wrapped numeric types — `Wei(U256)`, `NEP141Wei(u128)`, and `Fee(NEP141Wei)` — to distinguish token domains. The EVM layer operates on U256 for all token amounts, while the NEAR connector layer uses u128 (`NEP141Wei`). The boundary conversion is performed in two functions inside `engine-precompiles/src/native.rs`:

**`json_args` (line 659–666):** [1](#0-0) 

**`borsh_args` (line 668–674):** [2](#0-1) 

Both call `amount.as_u128()` on a raw `U256` value with **no overflow check**. In Rust, `U256::as_u128()` silently discards the upper 128 bits when the value exceeds `u128::MAX` (≈ 3.4 × 10³⁸).

The same truncation occurs in `exit_erc20_token_to_near` for the wNEAR unwrap path: [3](#0-2) 

And in `exit_base_token_to_near` for the legacy ETH path: [4](#0-3) 

The `NEP141Wei` type itself is defined as a `u128` wrapper: [5](#0-4) 

The `Fee` type adds a second layer of wrapping over `NEP141Wei`: [6](#0-5) 

This double-indirection (`Fee(NEP141Wei(u128))`) alongside the EVM's native U256 creates an inconsistent representation boundary that is crossed without validation.

The execution path for ERC-20 exit is:

1. An ERC-20 contract burns `amount` (U256) tokens on Aurora.
2. The ERC-20 contract calls the `exitToNear` precompile with the full U256 `amount`.
3. `exit_erc20_token_to_near` is invoked; it calls `exit_params.amount.as_u128()` to build the NEAR `ft_transfer` JSON argument.
4. The NEAR-side `ft_transfer` call is issued with only the low 128 bits of `amount`.
5. The burned tokens on Aurora are gone; NEAR credits only the truncated value.

The `ExitToNear` precompile entry point: [7](#0-6) 

---

### Impact Explanation

**Critical — Permanent freezing of funds.**

For any ERC-20 token whose per-user balance or transfer amount exceeds `u128::MAX` base units, the difference `amount - (amount % 2^128)` is permanently destroyed: burned on Aurora with no corresponding credit on NEAR. The tokens cannot be recovered because the burn is irreversible and the NEAR-side transfer carries only the truncated value.

---

### Likelihood Explanation

**Low-to-Medium.** Most production ERC-20 tokens have total supplies well within u128::MAX. However:

- Any ERC-20 token deployer on Aurora can set an arbitrary U256 total supply.
- A token with 0 decimals and a supply of `2^128 + 1` (or any token with 18 decimals and supply > 3.4 × 10²⁰ tokens) triggers the bug.
- The deployer need not be malicious; a legitimate high-supply meme token or a token with non-standard decimals can reach this threshold.
- The victim is any token holder who calls `exitToNear` with such an amount — an ordinary, unprivileged EVM user action.

---

### Recommendation

Replace the silent `.as_u128()` casts with checked conversions that return an error if the U256 value exceeds `u128::MAX`. For example:

```rust
fn borsh_args(address: Address, amount: U256) -> Result<Vec<u8>, ExitError> {
    let amount_u128 = amount
        .try_into()  // U256 → u128, errors if > u128::MAX
        .map_err(|_| ExitError::Other(Cow::from("ERR_AMOUNT_OVERFLOW_U128")))?;
    borsh::to_vec(&WithdrawCallArgs {
        recipient_address: address,
        amount: NEP141Wei::new(amount_u128),
    })
    .map_err(|_| ExitError::Other(Cow::from("ERR_BORSH_SERIALIZE")))
}
```

Apply the same pattern to `json_args` and both inline `as_u128()` calls in `exit_base_token_to_near` and `exit_erc20_token_to_near`. This causes the precompile to revert cleanly rather than silently truncating the amount and destroying funds.

Additionally, consider whether `NEP141Wei` should be widened to U256 to eliminate the representation mismatch at the type level, consistent with the external report's recommendation to remove unnecessary indirection layers.

---

### Proof of Concept

1. Deploy an ERC-20 token on Aurora with total supply = `2^128 + 1000` (a valid U256).
2. Mint `2^128 + 1000` tokens to address `0xAlice`.
3. Alice calls the ERC-20's `withdrawToNear` (or equivalent burn+exit function), passing amount = `2^128 + 1000`.
4. The ERC-20 contract burns `2^128 + 1000` tokens from Alice's balance and calls the `exitToNear` precompile with that U256 value.
5. `exit_erc20_token_to_near` executes `exit_params.amount.as_u128()` → returns `999` (the low 128 bits).
6. NEAR receives an `ft_transfer` for `999` tokens.
7. Alice's Aurora balance is zero; her NEAR balance is `999`. The `2^128` tokens are permanently lost. [2](#0-1) [8](#0-7) [5](#0-4)

### Citations

**File:** engine-precompiles/src/native.rs (L444-447)
```rust
                ExitToNearParams::Erc20TokenParams(ref exit_params) => {
                    exit_erc20_token_to_near(context, exit_params, &self.io)?
                }
            };
```

**File:** engine-precompiles/src/native.rs (L536-554)
```rust
        None => Ok((
            eth_connector_account_id,
            // There is no way to inject json, given the encoding of both arguments
            // as decimal and valid account id respectively.
            format!(
                r#"{{"receiver_id":"{}","amount":"{}"}}"#,
                exit_params.receiver_account_id,
                context.apparent_value.as_u128()
            ),
            events::ExitToNear::Legacy(ExitToNearLegacy {
                sender: Address::new(context.caller),
                erc20_address: events::ETH_ADDRESS,
                dest: exit_params.receiver_account_id.to_string(),
                amount: context.apparent_value,
            }),
            "ft_transfer".to_string(),
            None,
        )),
        _ => Err(ExitError::Other(Cow::from("ERR_INVALID_MESSAGE"))),
```

**File:** engine-precompiles/src/native.rs (L594-601)
```rust
            (
                nep141_account_id,
                format!(r#"{{"amount":"{}"}}"#, exit_params.amount.as_u128()),
                "near_withdraw",
                Some(TransferNearArgs {
                    target_account_id: exit_params.receiver_account_id.clone(),
                    amount: exit_params.amount.as_u128(),
                }),
```

**File:** engine-precompiles/src/native.rs (L658-666)
```rust
#[allow(clippy::unnecessary_wraps)]
fn json_args(address: Address, amount: U256) -> Result<Vec<u8>, ExitError> {
    Ok(format!(
        r#"{{"amount":"{}","recipient":"{}"}}"#,
        amount.as_u128(),
        address.encode(),
    )
    .into_bytes())
}
```

**File:** engine-precompiles/src/native.rs (L668-674)
```rust
fn borsh_args(address: Address, amount: U256) -> Result<Vec<u8>, ExitError> {
    borsh::to_vec(&WithdrawCallArgs {
        recipient_address: address,
        amount: NEP141Wei::new(amount.as_u128()),
    })
    .map_err(|_| ExitError::Other(Cow::from("ERR_BORSH_SERIALIZE")))
}
```

**File:** engine-types/src/types/wei.rs (L16-19)
```rust
#[derive(
    Default, Debug, Clone, Copy, Eq, PartialEq, Ord, PartialOrd, BorshSerialize, BorshDeserialize,
)]
pub struct NEP141Wei(u128);
```

**File:** engine-types/src/types/fee.rs (L6-10)
```rust
#[derive(
    Default, Debug, Clone, Copy, Eq, PartialEq, Ord, PartialOrd, BorshSerialize, BorshDeserialize,
)]
/// Engine `fee` type which wraps an underlying u128.
pub struct Fee(NEP141Wei);
```

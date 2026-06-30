### Title
Unchecked Exit Precompile Return Value in `EvmErc20V2.withdrawToNear` Causes Permanent Token Burn Without NEAR Transfer - (File: `etc/eth-contracts/contracts/EvmErc20V2.sol`)

---

### Summary

`EvmErc20V2.withdrawToNear` burns the caller's ERC-20 tokens and then calls the `ExitToNear` precompile via a low-level assembly `call`. The return value of that assembly call is captured in `res` but is never checked. If the precompile call fails for any reason, the tokens are permanently destroyed with no corresponding NEP-141 transfer to NEAR, freezing the user's funds forever.

---

### Finding Description

`EvmErc20V2.withdrawToNear` performs two sequential steps:

1. Burns the caller's tokens via `_burn(sender, amount)`.
2. Calls the `ExitToNear` precompile (`0xe9217bc70b7ed1f598ddd3199e80b093fa71124f`) via inline assembly.

```solidity
function withdrawToNear(bytes memory recipient, uint256 amount) external override {
    address sender = _msgSender();
    _burn(sender, amount);                          // tokens destroyed here

    bytes32 amount_b = bytes32(amount);
    bytes memory input = abi.encodePacked("\x01", sender, amount_b, recipient);
    uint input_size = 1 + 20 + 32 + recipient.length;

    assembly {
        let res := call(gas(), 0xe9217bc70b7ed1f598ddd3199e80b093fa71124f, 0, add(input, 32), input_size, 0, 32)
        // res is never checked
    }
}
``` [1](#0-0) 

The `res` variable is assigned but never inspected. A Solidity low-level `call` returns `0` on failure, but since the result is silently discarded, the outer function returns successfully regardless of whether the precompile actually processed the withdrawal.

The `ExitToNear` precompile can fail for several reachable reasons:

- **Input encoding mismatch when `error_refund` feature is disabled.** The contract always encodes `sender` (20 bytes) between the flag byte and the amount. The precompile's `parse_input` only strips those 20 bytes when compiled with `#[cfg(feature = "error_refund")]`. Without that feature, the precompile interprets the last 12 bytes of `sender` concatenated with the first 20 bytes of `amount_b` as the 32-byte amount field. This produces a value that almost certainly exceeds `u128::MAX`, causing `parse_amount` to return `ERR_INVALID_AMOUNT` and the precompile to abort. [2](#0-1) [3](#0-2) 

- **NEP-141 mapping absent.** If the ERC-20 contract's address is not registered in the engine's `Erc20Nep141Map`, `get_nep141_from_erc20` returns `ERR_TARGET_TOKEN_NOT_FOUND` and the precompile aborts. [4](#0-3) 

- **Invalid recipient account ID.** A malformed NEAR account ID string causes `parse_recipient` to return an error.

In every failure case the ERC-20 tokens are already burned (irreversible), but no `ft_transfer` or `ft_transfer_call` promise is ever scheduled on the NEP-141 contract, so the user receives nothing on NEAR.

---

### Impact Explanation

**Critical — Permanent freezing of funds.**

Any holder of an `EvmErc20V2`-based bridged token who calls `withdrawToNear` under a condition that causes the precompile to fail will have their tokens permanently destroyed. The burn is committed to EVM state before the precompile is invoked; there is no rollback path. The tokens cease to exist on Aurora and are never credited on NEAR.

---

### Likelihood Explanation

**Medium.**

- The encoding mismatch is deterministic when the engine is compiled without `error_refund`. Whether that feature is active in a given deployment is a compile-time decision, not visible to the caller.
- Even with `error_refund` enabled, any user who supplies a recipient string that fails `AccountId` parsing (e.g., an empty string, or a string with invalid characters) will silently lose their tokens.
- The function is `external` and callable by any token holder with no access control, making the attack surface wide.

---

### Recommendation

1. **Check the precompile return value** and revert if it indicates failure:

```solidity
assembly {
    let res := call(gas(), 0xe9217bc70b7ed1f598ddd3199e80b093fa71124f, 0, add(input, 32), input_size, 0, 32)
    if iszero(res) { revert(0, 0) }
}
```

2. **Validate the recipient** before burning, so the burn only occurs when the precompile call is expected to succeed.

3. **Align the input encoding** with the exact format expected by the deployed precompile binary (with or without the `error_refund` refund-address field), or make the encoding conditional on the same feature flag.

---

### Proof of Concept

1. Deploy `EvmErc20V2` on Aurora (or use an existing bridged token).
2. Call `withdrawToNear` with a recipient string that is not a valid NEAR account ID (e.g., `""`).
3. Observe: `_burn` executes and the caller's balance drops to zero.
4. The assembly `call` to the precompile returns `0` (failure), but `res` is never checked.
5. The function returns without reverting.
6. The caller's ERC-20 tokens are gone; no NEP-141 tokens appear on NEAR.

The same outcome occurs deterministically when the engine binary is compiled without `error_refund`, because the 20-byte sender field shifts the amount field, producing a value rejected by `parse_amount`. [1](#0-0) [5](#0-4)

### Citations

**File:** etc/eth-contracts/contracts/EvmErc20V2.sol (L53-64)
```text
    function withdrawToNear(bytes memory recipient, uint256 amount) external override {
        address sender = _msgSender();
        _burn(sender, amount);

        bytes32 amount_b = bytes32(amount);
        bytes memory input = abi.encodePacked("\x01", sender, amount_b, recipient);
        uint input_size = 1 + 20 + 32 + recipient.length;

        assembly {
            let res := call(gas(), 0xe9217bc70b7ed1f598ddd3199e80b093fa71124f, 0, add(input, 32), input_size, 0, 32)
        }
    }
```

**File:** engine-precompiles/src/native.rs (L302-309)
```rust
fn get_nep141_from_erc20<I: IO>(erc20_token: &[u8], io: &I) -> Result<AccountId, ExitError> {
    AccountId::try_from(
        io.read_storage(bytes_to_key(KeyPrefix::Erc20Nep141Map, erc20_token).as_slice())
            .map(|s| s.to_vec())
            .ok_or(ExitError::Other(Cow::Borrowed(ERR_TARGET_TOKEN_NOT_FOUND)))?,
    )
    .map_err(|_| ExitError::Other(Cow::Borrowed("ERR_INVALID_NEP141_ACCOUNT")))
}
```

**File:** engine-precompiles/src/native.rs (L337-345)
```rust
fn parse_amount(input: &[u8]) -> Result<U256, ExitError> {
    let amount = U256::from_big_endian(input);

    if amount > U256::from(u128::MAX) {
        return Err(ExitError::Other(Cow::from("ERR_INVALID_AMOUNT")));
    }

    Ok(amount)
}
```

**File:** engine-precompiles/src/native.rs (L758-776)
```rust
            0x1 => {
                let amount = parse_amount(&input[..32])?;
                let Recipient {
                    receiver_account_id,
                    message,
                } = parse_recipient(&input[32..])?;

                Ok(Self::Erc20TokenParams(Erc20TokenParams {
                    #[cfg(feature = "error_refund")]
                    refund_address,
                    receiver_account_id,
                    amount,
                    message,
                }))
            }
            _ => Err(ExitError::Other(Cow::from("ERR_INVALID_FLAG"))),
        }
    }
}
```

**File:** engine-precompiles/src/native.rs (L787-791)
```rust
#[cfg(not(feature = "error_refund"))]
fn parse_input(input: &[u8]) -> Result<&[u8], ExitError> {
    validate_input_size(input, MIN_INPUT_SIZE, MAX_INPUT_SIZE)?;
    Ok(&input[1..])
}
```

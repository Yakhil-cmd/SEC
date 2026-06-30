### Title
Silent Precompile Failure After `_burn` in `withdrawToNear` Causes Permanent Token Loss — (`etc/eth-contracts/contracts/EvmErc20.sol`)

---

### Summary

`withdrawToNear` burns the caller's ERC-20 tokens **before** calling the `ExitToNear` precompile, and the assembly `call` return value is **never checked**. When the precompile rejects the input (e.g., invalid UTF-8 or invalid NEAR account ID in `recipient`), the EVM `call` opcode returns 0 to the caller but does not revert the caller's execution. The `_burn` is already committed, so the user's tokens are permanently destroyed with no corresponding NEP-141 transfer on the NEAR side.

---

### Finding Description

**Burn-before-call with unchecked result:**

```solidity
// EvmErc20.sol lines 53-63
function withdrawToNear(bytes memory recipient, uint256 amount) external override {
    _burn(_msgSender(), amount);          // ← tokens destroyed here

    bytes32 amount_b = bytes32(amount);
    bytes memory input = abi.encodePacked("\x01", amount_b, recipient);
    uint input_size = 1 + 32 + recipient.length;

    assembly {
        let res := call(gas(), 0xe9217bc70b7ed1f598ddd3199e80b093fa71124f,
                        0, add(input, 32), input_size, 0, 32)
        // res is stored but NEVER CHECKED — no revert on failure
    }
}
``` [1](#0-0) 

**How the precompile failure propagates (or fails to):**

`process_precompile` maps any `ExitError` returned by the precompile to `PrecompileFailure::Error`:

```rust
p.run(input, gas_limit.map(EthGas::new), context, is_static)
    .map_err(|exit_status| PrecompileFailure::Error { exit_status })
``` [2](#0-1) 

`PrecompileFailure::Error` is standard EVM semantics: the inner `call` opcode returns 0 to the caller, but the **caller's execution and state changes are not reverted**. The `_burn` is already committed.

**Conditions that cause the precompile to return `ExitError`:**

1. **Invalid UTF-8 bytes** in `recipient` — `parse_recipient` calls `str::from_utf8` and returns `ERR_INVALID_RECEIVER_ACCOUNT_ID` on failure. [3](#0-2) 

2. **Invalid NEAR account ID** — any recipient that fails `AccountId::validate` (uppercase letters, `@`, leading/trailing separators, length < 2 or > 64, etc.). [4](#0-3) 

3. **Oversized input** — if `recipient.length > 991` bytes, `validate_input_size` rejects with `ERR_INVALID_INPUT` (MAX_INPUT_SIZE = 1024, flag = 1 byte, amount = 32 bytes). [5](#0-4) 

The test suite itself confirms these rejection paths: [6](#0-5) 

---

### Impact Explanation

After a failed precompile call:

| Side | State |
|---|---|
| ERC-20 supply | Decreased by `amount` (burn committed) |
| NEP-141 in Aurora's NEAR account | Unchanged (transfer never issued) |

The user's tokens are permanently destroyed. The NEP-141 tokens remain locked in Aurora's NEAR account with no on-chain mechanism to reclaim them for the affected user. This is **permanent freezing of user funds**, not insolvency (the system is over-collateralized after the event, not under-collateralized).

The "supply drift" framing in the question is accurate: ERC-20 total supply no longer matches the NEP-141 backing attributable to live token holders.

---

### Likelihood Explanation

- Any user who passes a `recipient` containing non-UTF-8 bytes, an invalid NEAR account ID (e.g., `"Alice"`, `"user@domain"`, a string > 64 chars), or a byte array > 991 bytes will trigger this silently.
- The function signature accepts raw `bytes memory recipient` with no on-chain validation before the burn.
- Accidental triggering is realistic (e.g., passing an Ethereum address hex string with uppercase letters, or a URL-encoded string).
- A malicious contract that holds ERC-20 tokens (e.g., a wrapper or aggregator) could deliberately trigger this to destroy tokens it controls.

---

### Recommendation

1. **Check the precompile call result and revert on failure:**
   ```solidity
   assembly {
       let res := call(gas(), 0xe9217bc70b7ed1f598ddd3199e80b093fa71124f,
                       0, add(input, 32), input_size, 0, 32)
       if iszero(res) { revert(0, 0) }
   }
   ```
2. **Or, validate `recipient` before burning** — ensure it is valid UTF-8 and a valid NEAR account ID (length 2–64, only `[a-z0-9._-]`, no leading/trailing separators).
3. Apply the same fix to `EvmErc20V2.sol`, which has the identical pattern. [7](#0-6) 

---

### Proof of Concept

```solidity
// SPDX-License-Identifier: MIT
pragma solidity ^0.8.0;

interface IEvmErc20 {
    function withdrawToNear(bytes memory recipient, uint256 amount) external;
    function balanceOf(address) external view returns (uint256);
    function totalSupply() external view returns (uint256);
}

contract PoC {
    function exploit(address token, uint256 amount) external {
        IEvmErc20 erc20 = IEvmErc20(token);

        uint256 supplyBefore = erc20.totalSupply();
        uint256 balBefore    = erc20.balanceOf(address(this));

        // Pass invalid UTF-8 bytes as recipient — precompile will reject
        // but _burn already executed; no revert occurs.
        bytes memory badRecipient = hex"c2";   // lone continuation byte
        erc20.withdrawToNear(badRecipient, amount);

        uint256 supplyAfter = erc20.totalSupply();
        uint256 balAfter    = erc20.balanceOf(address(this));

        // Supply decreased, balance decreased, but no NEAR transfer happened.
        assert(supplyAfter == supplyBefore - amount);  // burn committed
        assert(balAfter    == balBefore    - amount);  // user's tokens gone
        // NEP-141 on NEAR side: unchanged — tokens permanently locked.
    }
}
```

**Trigger variants:**
- `hex"c2"` — invalid UTF-8 (lone continuation byte)
- `"Alice"` — uppercase letter, rejected by `AccountId::validate`
- `"user@domain.near"` — `@` is not in `[a-z0-9._-]`
- A byte string of length > 991 — exceeds `MAX_INPUT_SIZE`

All of these cause `parse_recipient` or `validate_input_size` to return `ExitError`, which maps to `PrecompileFailure::Error`, which causes the EVM `call` to return 0 — silently, with the burn already finalized.

### Citations

**File:** etc/eth-contracts/contracts/EvmErc20.sol (L53-63)
```text
    function withdrawToNear(bytes memory recipient, uint256 amount) external override {
        _burn(_msgSender(), amount);

        bytes32 amount_b = bytes32(amount);
        bytes memory input = abi.encodePacked("\x01", amount_b, recipient);
        uint input_size = 1 + 32 + recipient.length;

        assembly {
            let res := call(gas(), 0xe9217bc70b7ed1f598ddd3199e80b093fa71124f, 0, add(input, 32), input_size, 0, 32)
        }
    }
```

**File:** engine-precompiles/src/lib.rs (L173-174)
```rust
    p.run(input, gas_limit.map(EthGas::new), context, is_static)
        .map_err(|exit_status| PrecompileFailure::Error { exit_status })
```

**File:** engine-precompiles/src/native.rs (L295-299)
```rust
fn validate_input_size(input: &[u8], min: usize, max: usize) -> Result<(), ExitError> {
    if input.len() < min || input.len() > max {
        return Err(ExitError::Other(Cow::from("ERR_INVALID_INPUT")));
    }
    Ok(())
```

**File:** engine-precompiles/src/native.rs (L359-362)
```rust
fn parse_recipient(recipient: &[u8]) -> Result<Recipient<'_>, ExitError> {
    let recipient = str::from_utf8(recipient)
        .map_err(|_| ExitError::Other(Cow::from("ERR_INVALID_RECEIVER_ACCOUNT_ID")))?;
    let (receiver_account_id, message) = recipient.split_once(':').map_or_else(
```

**File:** engine-precompiles/src/native.rs (L1135-1140)
```rust
    #[test]
    fn test_parse_invalid_recipient() {
        assert!(parse_recipient(b"test@.near").is_err());
        assert!(parse_recipient(b"test@.near:msg").is_err());
        assert!(parse_recipient(&[0xc2]).is_err());
    }
```

**File:** engine-types/src/account_id.rs (L32-64)
```rust
    pub fn validate(account_id: &str) -> Result<(), ParseAccountError> {
        if account_id.len() < MIN_ACCOUNT_ID_LEN {
            Err(ParseAccountError::TooShort)
        } else if account_id.len() > MAX_ACCOUNT_ID_LEN {
            Err(ParseAccountError::TooLong)
        } else {
            // Adapted from https://github.com/near/near-sdk-rs/blob/fd7d4f82d0dfd15f824a1cf110e552e940ea9073/near-sdk/src/environment/env.rs#L819

            // NOTE: We don't want to use Regex here, because it requires extra time to compile it.
            // The valid account ID regex is /^(([a-z\d]+[-_])*[a-z\d]+\.)*([a-z\d]+[-_])*[a-z\d]+$/
            // Instead the implementation is based on the previous character checks.

            // We can safely assume that last char was a separator.
            let mut last_char_is_separator = true;

            for c in account_id.bytes() {
                let current_char_is_separator = match c {
                    b'a'..=b'z' | b'0'..=b'9' => false,
                    b'-' | b'_' | b'.' => true,
                    _ => {
                        return Err(ParseAccountError::Invalid);
                    }
                };
                if current_char_is_separator && last_char_is_separator {
                    return Err(ParseAccountError::Invalid);
                }
                last_char_is_separator = current_char_is_separator;
            }

            (!last_char_is_separator)
                .then_some(())
                .ok_or(ParseAccountError::Invalid)
        }
```

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

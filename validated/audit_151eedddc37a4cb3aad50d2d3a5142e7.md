### Title
Unchecked Precompile Call Return Value in ERC-20 Withdrawal Burns Tokens With No Assets Received - (`etc/eth-contracts/contracts/EvmErc20.sol`)

---

### Summary

In `EvmErc20.sol` and `EvmErc20V2.sol`, both `withdrawToNear` and `withdrawToEthereum` first burn the caller's ERC-20 tokens via `_burn`, then invoke the exit precompile via a low-level `assembly { let res := call(...) }`. The return value `res` is **never checked**. If the precompile call fails for any reason, the burn is permanent and the user receives no assets in return — an exact structural analog to the reported vulnerability.

---

### Finding Description

In `EvmErc20.sol`:

```solidity
function withdrawToNear(bytes memory recipient, uint256 amount) external override {
    _burn(_msgSender(), amount);          // tokens destroyed here

    bytes32 amount_b = bytes32(amount);
    bytes memory input = abi.encodePacked("\x01", amount_b, recipient);
    uint input_size = 1 + 32 + recipient.length;

    assembly {
        let res := call(gas(), 0xe9217bc70b7ed1f598ddd3199e80b093fa71124f, 0, add(input, 32), input_size, 0, 32)
        // res is captured but NEVER checked — silent failure
    }
}
``` [1](#0-0) 

The same pattern appears in `withdrawToEthereum` in `EvmErc20.sol` and in both withdrawal functions in `EvmErc20V2.sol`: [2](#0-1) [3](#0-2) [4](#0-3) 

The `ExitToNear` precompile (`engine-precompiles/src/native.rs`) returns `Err(ExitError::...)` in multiple reachable conditions, including when the recipient is not a valid NEAR account ID (`ERR_INVALID_RECEIVER_ACCOUNT_ID`), when the ERC-20 → NEP-141 mapping is absent (`ERR_TARGET_TOKEN_NOT_FOUND`), or when the precompile is paused (`ERR_PAUSED`): [5](#0-4) [6](#0-5) 

When the EVM `call` to a precompile fails, it returns `0` to the caller. Because the Solidity assembly block does not check `res` and does not revert, the outer transaction succeeds with the burn committed and no NEP-141 transfer initiated.

---

### Impact Explanation

**Critical — permanent loss of user funds.**

A user's ERC-20 mirror tokens are irreversibly destroyed by `_burn`. If the subsequent precompile call returns failure (for any of the reasons above), no NEP-141 tokens are transferred to the recipient and no refund occurs. The tokens are gone with no recourse. This matches the "burn tokens, receive no assets" impact of the reference finding.

---

### Likelihood Explanation

**Medium.** The most realistic unprivileged trigger is a user supplying a recipient string that fails NEAR account-ID validation inside `parse_recipient` (e.g., an empty string, a string with invalid characters, or a string that is too long). The `recipient` parameter is entirely user-controlled calldata passed directly into the precompile input: [7](#0-6) 

`parse_recipient` will return `ERR_INVALID_RECEIVER_ACCOUNT_ID` for any malformed input, causing the precompile call to fail silently after the burn has already executed: [8](#0-7) 

A secondary trigger (requiring an authorized but not necessarily admin account) is the precompile being paused via `pause_precompiles`, which sets `ERR_PAUSED` on the exit precompile address: [6](#0-5) 

---

### Recommendation

Check the return value of the low-level `call` and revert if it is zero, mirroring the fix recommended in the reference report:

```solidity
function withdrawToNear(bytes memory recipient, uint256 amount) external override {
    _burn(_msgSender(), amount);

    bytes32 amount_b = bytes32(amount);
    bytes memory input = abi.encodePacked("\x01", amount_b, recipient);
    uint input_size = 1 + 32 + recipient.length;

    assembly {
        let res := call(gas(), 0xe9217bc70b7ed1f598ddd3199e80b093fa71124f, 0, add(input, 32), input_size, 0, 32)
        if iszero(res) { revert(0, 0) }
    }
}
```

Apply the same fix to `withdrawToEthereum` in both `EvmErc20.sol` and `EvmErc20V2.sol`.

---

### Proof of Concept

1. Deploy `EvmErc20` with a valid NEP-141 mapping.
2. Mint tokens to `alice`.
3. `alice` calls `withdrawToNear("", amount)` — an empty string is not a valid NEAR account ID.
4. `_burn` executes: `alice`'s balance decreases by `amount`, total supply decreases.
5. The precompile call fails with `ERR_INVALID_RECEIVER_ACCOUNT_ID`; `res = 0`.
6. No `if iszero(res) { revert(...) }` check exists; the transaction succeeds.
7. `alice` has lost `amount` tokens permanently. No NEP-141 transfer was scheduled. [1](#0-0) [8](#0-7)

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

**File:** etc/eth-contracts/contracts/EvmErc20.sol (L65-76)
```text
    function withdrawToEthereum(address recipient, uint256 amount) external override {
        _burn(_msgSender(), amount);

        bytes32 amount_b = bytes32(amount);
        bytes20 recipient_b = bytes20(recipient);
        bytes memory input = abi.encodePacked("\x01", amount_b, recipient_b);
        uint input_size = 1 + 32 + 20;

        assembly {
            let res := call(gas(), 0xb0bd02f6a392af548bdf1cfaee5dfa0eefcc8eab, 0, add(input, 32), input_size, 0, 32)
        }
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

**File:** etc/eth-contracts/contracts/EvmErc20V2.sol (L66-77)
```text
    function withdrawToEthereum(address recipient, uint256 amount) external override {
        _burn(_msgSender(), amount);

        bytes32 amount_b = bytes32(amount);
        bytes20 recipient_b = bytes20(recipient);
        bytes memory input = abi.encodePacked("\x01", amount_b, recipient_b);
        uint input_size = 1 + 32 + 20;

        assembly {
            let res := call(gas(), 0xb0bd02f6a392af548bdf1cfaee5dfa0eefcc8eab, 0, add(input, 32), input_size, 0, 32)
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

**File:** engine-precompiles/src/native.rs (L359-378)
```rust
fn parse_recipient(recipient: &[u8]) -> Result<Recipient<'_>, ExitError> {
    let recipient = str::from_utf8(recipient)
        .map_err(|_| ExitError::Other(Cow::from("ERR_INVALID_RECEIVER_ACCOUNT_ID")))?;
    let (receiver_account_id, message) = recipient.split_once(':').map_or_else(
        || (recipient, None),
        |(recipient, msg)| {
            if msg == UNWRAP_WNEAR_MSG {
                (recipient, Some(Message::UnwrapWnear))
            } else {
                (recipient, Some(Message::Omni(msg)))
            }
        },
    );

    Ok(Recipient {
        receiver_account_id: receiver_account_id
            .parse()
            .map_err(|_| ExitError::Other(Cow::from("ERR_INVALID_RECEIVER_ACCOUNT_ID")))?,
        message,
    })
```

**File:** engine-precompiles/src/lib.rs (L140-144)
```rust
        if self.is_paused(&address) {
            return Some(Err(PrecompileFailure::Fatal {
                exit_status: ExitFatal::Other(prelude::Cow::Borrowed("ERR_PAUSED")),
            }));
        }
```

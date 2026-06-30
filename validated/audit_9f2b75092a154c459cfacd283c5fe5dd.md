### Title
Unchecked Precompile Call Return Value in `withdrawToNear` / `withdrawToEthereum` Causes Permanent ERC-20 Token Loss — (File: `etc/eth-contracts/contracts/EvmErc20.sol`)

---

### Summary

`EvmErc20.sol` burns the caller's tokens **before** invoking the Aurora exit precompile via inline assembly. The return value of the `call` opcode is captured in a local variable `res` but is **never checked**. If the precompile fails for any reason, the EVM transaction still succeeds, permanently destroying the user's tokens with no corresponding transfer on the NEAR or Ethereum side.

---

### Finding Description

Both `withdrawToNear` and `withdrawToEthereum` follow the same unsafe pattern:

```solidity
// etc/eth-contracts/contracts/EvmErc20.sol
function withdrawToNear(bytes memory recipient, uint256 amount) external override {
    _burn(_msgSender(), amount);                          // (1) irreversible burn

    bytes32 amount_b = bytes32(amount);
    bytes memory input = abi.encodePacked("\x01", amount_b, recipient);
    uint input_size = 1 + 32 + recipient.length;

    assembly {
        let res := call(gas(), 0xe9217bc70b7ed1f598ddd3199e80b093fa71124f,
                        0, add(input, 32), input_size, 0, 32)
        // (2) res is NEVER checked — no revert on failure
    }
}
``` [1](#0-0) 

The same pattern appears in `withdrawToEthereum`: [2](#0-1) 

The exit precompile (`ExitToNear`) can return failure (`ExitError`) for several reasons that are entirely within user-controlled input:

- `validate_input_size` rejects input outside `[MIN_INPUT_SIZE, MAX_INPUT_SIZE]` (3–1024 bytes)
- `parse_recipient` rejects any byte sequence that is not a valid NEAR account ID
- `get_nep141_from_erc20` returns `ERR_TARGET_TOKEN_NOT_FOUND` if the ERC-20→NEP-141 mapping is absent [3](#0-2) 

When the precompile returns 0 (failure), the EVM `call` opcode simply places 0 in `res`. Because `res` is never inspected and there is no `if iszero(res) { revert(0,0) }` guard, the outer EVM transaction **succeeds**. The `_burn` that already executed is not rolled back. The tokens are permanently destroyed.

---

### Impact Explanation

**Critical — Permanent freezing / destruction of funds.**

The user's ERC-20 mirror tokens are burned on the Aurora EVM side. No corresponding NEP-141 tokens are released on NEAR, and no ETH is released on Ethereum. The tokens cease to exist on either chain. There is no recovery path: the burn is final, the promise is never scheduled, and no refund callback is triggered (the `error_refund` feature path is not exercised because the precompile call itself never returns a promise log).

---

### Likelihood Explanation

**Medium.** The `recipient` parameter of `withdrawToNear` is a raw `bytes` value with no on-chain validation before the burn. A user who passes:

- an empty byte array,
- bytes containing characters illegal in NEAR account IDs (e.g., uppercase letters, spaces, `!`),
- or a recipient string longer than 1024 − 33 = 991 bytes,

will silently lose all tokens. The `amount` parameter has no minimum, so even large balances are at risk. Because the function signature accepts arbitrary `bytes`, off-chain tooling or contract integrators that construct the call programmatically are realistic vectors for accidental misuse.

---

### Recommendation

**Short term:** Check the return value of the precompile `call` and revert on failure, preserving the tokens:

```solidity
assembly {
    let res := call(gas(), 0xe9217bc70b7ed1f598ddd3199e80b093fa71124f,
                    0, add(input, 32), input_size, 0, 32)
    if iszero(res) { revert(0, 0) }
}
```

**Long term:** Validate the `recipient` bytes against NEAR account ID rules (length, character set) before executing `_burn`, following the checks-effects-interactions pattern: validate → burn → call.

---

### Proof of Concept

1. Alice holds 1 000 USDC mirror tokens on Aurora EVM.
2. Alice (or a contract acting on her behalf) calls `withdrawToNear(bytes("INVALID ACCOUNT!!"), 1000)` — the recipient contains uppercase letters and spaces, which are illegal in NEAR account IDs.
3. `_burn(Alice, 1000)` executes; Alice's EVM balance drops to 0.
4. The precompile call is made. Inside `ExitToNear::run`, `parse_recipient` calls `AccountId::try_from` on the invalid string, which fails, returning `ExitError::Other("ERR_INVALID_RECEIVER_ACCOUNT_ID")`. The precompile returns 0 to the EVM.
5. The assembly block does not revert. The EVM transaction completes successfully.
6. Alice's 1 000 tokens are permanently destroyed. No NEP-141 transfer is scheduled on NEAR. [4](#0-3) [1](#0-0)

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

**File:** engine-precompiles/src/native.rs (L295-309)
```rust
fn validate_input_size(input: &[u8], min: usize, max: usize) -> Result<(), ExitError> {
    if input.len() < min || input.len() > max {
        return Err(ExitError::Other(Cow::from("ERR_INVALID_INPUT")));
    }
    Ok(())
}

fn get_nep141_from_erc20<I: IO>(erc20_token: &[u8], io: &I) -> Result<AccountId, ExitError> {
    AccountId::try_from(
        io.read_storage(bytes_to_key(KeyPrefix::Erc20Nep141Map, erc20_token).as_slice())
            .map(|s| s.to_vec())
            .ok_or(ExitError::Other(Cow::Borrowed(ERR_TARGET_TOKEN_NOT_FOUND)))?,
    )
    .map_err(|_| ExitError::Other(Cow::Borrowed("ERR_INVALID_NEP141_ACCOUNT")))
}
```

**File:** engine-precompiles/src/native.rs (L359-379)
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
}
```

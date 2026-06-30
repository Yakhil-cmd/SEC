### Title
Unchecked Precompile Call Return Value After ERC-20 Burn Causes Permanent Fund Loss — (`File: etc/eth-contracts/contracts/EvmErc20.sol`, `etc/eth-contracts/contracts/EvmErc20V2.sol`)

---

### Summary

Both `EvmErc20.sol` and `EvmErc20V2.sol` burn the caller's tokens before calling the Aurora exit precompile via inline assembly. The return value of that `call` opcode is captured in a local variable (`res`) but is **never checked and never causes a revert**. If the precompile call fails for any reason, the ERC-20 tokens are permanently destroyed while the corresponding NEP-141 tokens on NEAR are never released to the recipient. The user suffers an irreversible total loss of the bridged asset.

---

### Finding Description

In `withdrawToNear` (and identically in `withdrawToEthereum`), the contract first burns the caller's balance, then encodes a payload and calls the exit precompile at a hardcoded address using a raw `call` opcode in an `assembly` block:

```solidity
// EvmErc20.sol  lines 53-63
function withdrawToNear(bytes memory recipient, uint256 amount) external override {
    _burn(_msgSender(), amount);                          // ← tokens destroyed here

    bytes32 amount_b = bytes32(amount);
    bytes memory input = abi.encodePacked("\x01", amount_b, recipient);
    uint input_size = 1 + 32 + recipient.length;

    assembly {
        let res := call(gas(), 0xe9217bc70b7ed1f598ddd3199e80b093fa71124f,
                        0, add(input, 32), input_size, 0, 32)
        // res is never read; no revert on failure
    }
}
``` [1](#0-0) 

The identical pattern appears in `withdrawToEthereum`: [2](#0-1) 

And in both functions of `EvmErc20V2.sol`: [3](#0-2) [4](#0-3) 

On the Rust side, the `ExitToNear` precompile's `run()` method performs several validations that can return `ExitError` — including input-size checks, static-call rejection, delegate-call rejection, invalid-flag rejection, invalid recipient account-ID parsing, and amount overflow checks: [5](#0-4) 

When the precompile returns an `ExitError`, the EVM `call` opcode sets `res = 0` (failure). Because the Solidity code never inspects `res` and never executes a `revert`, the outer `withdrawToNear` / `withdrawToEthereum` call **succeeds** at the EVM level. The `_burn` that already executed is committed. No NEAR-side promise is ever created, so no NEP-141 tokens are transferred to the recipient.

The `error_refund` feature in the precompile only handles the case where the NEAR-side `ft_transfer` promise fails *after* the precompile has already succeeded and emitted a promise log. It provides no protection when the precompile itself returns an error before any promise is created: [6](#0-5) 

---

### Impact Explanation

**Critical — Permanent freezing (destruction) of funds.**

When the precompile call fails silently:
- The ERC-20 tokens are burned and gone from the EVM side.
- The NEP-141 tokens remain locked in the NEAR-side connector and are never released to the intended recipient.
- There is no recovery path: no refund, no retry, no admin escape hatch in the Solidity contract.

The user's bridged asset is permanently destroyed.

---

### Likelihood Explanation

**Medium.** Any unprivileged token holder can trigger this by calling `withdrawToNear` or `withdrawToEthereum` with:
- A recipient string that is not a valid NEAR account ID (e.g., too long, contains illegal characters, or is empty after the flag byte).
- A recipient that exceeds `MAX_INPUT_SIZE` (1 024 bytes), causing the precompile's `validate_input_size` check to fail. [7](#0-6) 

These are ordinary user mistakes (typos, copy-paste errors, programmatic bugs in a calling contract) that are entirely realistic on mainnet. The Solidity contract provides no input validation before burning, so the burn always succeeds even when the subsequent precompile call will inevitably fail.

---

### Recommendation

1. **Check the return value and revert on failure.** Replace the unchecked assembly block with a checked pattern in both functions and both contract versions:

```solidity
assembly {
    let res := call(gas(), 0xe9217bc70b7ed1f598ddd3199e80b093fa71124f,
                    0, add(input, 32), input_size, 0, 32)
    if iszero(res) { revert(0, 0) }
}
```

2. **Validate inputs before burning.** Perform recipient length and format checks before calling `_burn`, so that obviously invalid inputs are rejected without touching the token balance.

3. **Invert the operation order (burn-after-confirm).** Ideally, confirm the precompile call will succeed (or use a two-step commit/reveal) before irreversibly burning tokens.

---

### Proof of Concept

1. Alice holds 100 units of a bridged NEP-141 token represented by an `EvmErc20` contract on Aurora.
2. Alice calls `withdrawToNear(invalidRecipient, 100)` where `invalidRecipient` is a byte string that is not a valid NEAR account ID (e.g., `"!!!"` or a 2000-byte string exceeding `MAX_INPUT_SIZE`).
3. `_burn(Alice, 100)` executes — Alice's ERC-20 balance drops to 0.
4. The assembly `call` to the `ExitToNear` precompile is made. The precompile's `parse_recipient` (or `validate_input_size`) returns `ExitError`, so the EVM `call` returns `res = 0`.
5. The Solidity function does not check `res`, does not revert, and returns normally.
6. The EVM transaction is committed: Alice has 0 ERC-20 tokens.
7. No NEAR-side promise was created; the NEP-141 connector never releases tokens to Alice or any recipient.
8. Alice's 100 tokens are permanently destroyed with no recourse. [1](#0-0) [7](#0-6) [8](#0-7)

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

**File:** engine-precompiles/src/native.rs (L295-300)
```rust
fn validate_input_size(input: &[u8], min: usize, max: usize) -> Result<(), ExitError> {
    if input.len() < min || input.len() > max {
        return Err(ExitError::Other(Cow::from("ERR_INVALID_INPUT")));
    }
    Ok(())
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

**File:** engine-precompiles/src/native.rs (L404-419)
```rust
        let required_gas = Self::required_gas(input)?;

        if let Some(target_gas) = target_gas
            && required_gas > target_gas
        {
            return Err(ExitError::OutOfGas);
        }

        // It's not allowed to call exit precompiles in static mode
        if is_static {
            return Err(ExitError::Other(Cow::from("ERR_INVALID_IN_STATIC")));
        } else if context.address != exit_to_near::ADDRESS.raw() {
            return Err(ExitError::Other(Cow::from("ERR_INVALID_IN_DELEGATE")));
        }

        let exit_to_near_params = ExitToNearParams::try_from(input)?;
```

**File:** engine-precompiles/src/native.rs (L449-455)
```rust
        let callback_args = ExitToNearPrecompileCallbackArgs {
            #[cfg(feature = "error_refund")]
            refund: refund_call_args(&exit_to_near_params, &exit_event),
            #[cfg(not(feature = "error_refund"))]
            refund: None,
            transfer_near: transfer_near_args,
        };
```

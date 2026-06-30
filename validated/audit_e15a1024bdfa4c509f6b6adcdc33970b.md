### Title
Unchecked Precompile Call Return Value in `withdrawToNear`/`withdrawToEthereum` Causes Permanent Token Loss - (File: `etc/eth-contracts/contracts/EvmErc20V2.sol`)

---

### Summary

Both `EvmErc20.sol` and `EvmErc20V2.sol` burn the caller's ERC-20 tokens **before** invoking the `ExitToNear` or `ExitToEthereum` precompile via a low-level assembly `call`. The return value of that assembly call is captured in a local variable (`res`) but is **never checked**. If the precompile call fails for any reason, the ERC-20 tokens are permanently destroyed while the corresponding NEP-141 (or ETH) tokens are never released to the recipient — a silent, unrecoverable loss of funds.

---

### Finding Description

In `EvmErc20V2.sol`, the `withdrawToNear` function:

```solidity
function withdrawToNear(bytes memory recipient, uint256 amount) external override {
    address sender = _msgSender();
    _burn(sender, amount);                          // ← tokens destroyed here

    bytes32 amount_b = bytes32(amount);
    bytes memory input = abi.encodePacked("\x01", sender, amount_b, recipient);
    uint input_size = 1 + 20 + 32 + recipient.length;

    assembly {
        let res := call(gas(), 0xe9217bc70b7ed1f598ddd3199e80b093fa71124f,
                        0, add(input, 32), input_size, 0, 32)
        // res is NEVER checked — silent failure is possible
    }
}
``` [1](#0-0) 

The same pattern appears in `EvmErc20V2.withdrawToEthereum` (calling the `ExitToEthereum` precompile at `0xb0bd02f6...`) and in the legacy `EvmErc20.withdrawToNear`. [2](#0-1) [3](#0-2) 

These contracts are compiled and embedded directly into the engine binary via `include_bytes!`: [4](#0-3) 

The `ExitToNear` precompile (`engine-precompiles/src/native.rs`) validates input and can return failure for several reasons — invalid recipient account ID, input too short/long, invalid flag byte, or static-call context. When it does, the EVM `call` opcode returns `0`, but the Solidity code never inspects `res` and does not revert. [5](#0-4) [6](#0-5) 

---

### Impact Explanation

**Critical — Permanent freezing / direct loss of user funds.**

When the precompile call silently fails:
- The ERC-20 mirror tokens are irreversibly burned (`_burn` has already executed).
- The NEP-141 (or ETH) tokens are never transferred to the recipient; they remain locked in the NEP-141 contract with no mechanism to reclaim them.
- The user suffers a total, unrecoverable loss of the bridged asset.

This matches the same root pattern as the reference report: a function that consumes the user's input tokens and is supposed to deliver an equivalent output, but lacks any check that the output delivery actually succeeded.

---

### Likelihood Explanation

**Medium.**

The `recipient` parameter is a raw `bytes` argument. Any caller who passes bytes that do not decode to a valid NEAR account ID (e.g., a string that is too long, contains illegal characters, or is empty) will trigger a precompile failure. This can happen through:

1. **User error** — a frontend bug, copy-paste mistake, or encoding mismatch in the recipient string.
2. **Deliberate griefing** — a contract that calls `withdrawToNear` with a crafted invalid recipient to permanently destroy another user's tokens (if it holds approval).
3. **Gas exhaustion** — if the transaction is submitted with insufficient gas for the precompile, the inner `call` fails silently.

The `ExitToNear` precompile enforces strict input validation (`MIN_INPUT_SIZE`, `MAX_INPUT_SIZE`, account-ID parsing), making silent failure a realistic outcome for any malformed input. [7](#0-6) [8](#0-7) 

---

### Recommendation

Check the return value of every low-level assembly `call` to a precompile and revert on failure, so that the `_burn` is rolled back atomically:

```solidity
assembly {
    let res := call(gas(), 0xe9217bc70b7ed1f598ddd3199e80b093fa71124f,
                    0, add(input, 32), input_size, 0, 32)
    if iszero(res) { revert(0, 0) }
}
```

Apply the same fix to `withdrawToEthereum` in both `EvmErc20.sol` and `EvmErc20V2.sol`.

---

### Proof of Concept

1. Deploy an `EvmErc20V2` token (as the engine does for any bridged NEP-141).
2. Mint tokens to `alice`.
3. `alice` calls `withdrawToNear(bytes("!!!invalid!!!"), 1000)`.
4. `_burn(alice, 1000)` executes — alice's balance drops to 0.
5. The `ExitToNear` precompile rejects the invalid account ID and returns failure; the assembly `call` returns `0`.
6. `res` is never checked; the function returns normally.
7. Alice has lost 1000 tokens permanently. The NEP-141 contract still holds the corresponding balance with no claimant.

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

**File:** etc/eth-contracts/contracts/EvmErc20.sol (L53-65)
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

    function withdrawToEthereum(address recipient, uint256 amount) external override {
```

**File:** engine/src/engine.rs (L1321-1324)
```rust
    #[cfg(feature = "error_refund")]
    let erc20_contract = include_bytes!("../../etc/eth-contracts/res/EvmErc20V2.bin");
    #[cfg(not(feature = "error_refund"))]
    let erc20_contract = include_bytes!("../../etc/eth-contracts/res/EvmErc20.bin");
```

**File:** engine-precompiles/src/native.rs (L37-40)
```rust
const MIN_INPUT_SIZE: usize = 3;
#[cfg(feature = "error_refund")]
const MIN_INPUT_SIZE: usize = 21;
const MAX_INPUT_SIZE: usize = 1_024;
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

**File:** engine-precompiles/src/native.rs (L404-417)
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
```

### Title
Unchecked Return Value of Precompile `call()` in `withdrawToNear` and `withdrawToEthereum` Causes Permanent Fund Freeze - (File: `etc/eth-contracts/contracts/EvmErc20V2.sol`)

---

### Summary

In `EvmErc20V2.sol`, both `withdrawToNear` and `withdrawToEthereum` burn the caller's ERC-20 tokens **before** invoking the Aurora exit precompile via a low-level `call()`. The return value of that `call()` — which is `0` on failure and `1` on success — is captured in a local variable `res` but is **never checked**. If the precompile call fails for any reason, the burn is not reverted, the NEAR/ETH transfer never occurs, and the user's funds are permanently destroyed.

This is the direct analog of the M-03 report: in that report, a return value was checked against the wrong constant (causing success to be treated as failure); here, the return value is not checked at all (causing failure to be treated as success). Both are incorrect return-value-handling bugs in a bridge/adapter context.

---

### Finding Description

`EvmErc20V2.sol` implements the ERC-20 mirror token used by the Aurora bridge. Its two withdrawal functions follow this pattern:

```solidity
// withdrawToNear (lines 53–64)
function withdrawToNear(bytes memory recipient, uint256 amount) external override {
    address sender = _msgSender();
    _burn(sender, amount);                          // ← tokens destroyed here

    bytes32 amount_b = bytes32(amount);
    bytes memory input = abi.encodePacked("\x01", sender, amount_b, recipient);
    uint input_size = 1 + 20 + 32 + recipient.length;

    assembly {
        let res := call(gas(), 0xe9217bc70b7ed1f598ddd3199e80b093fa71124f,
                        0, add(input, 32), input_size, 0, 32)
        // res is NEVER checked — failure is silently ignored
    }
}
```

```solidity
// withdrawToEthereum (lines 66–77)
function withdrawToEthereum(address recipient, uint256 amount) external override {
    _burn(_msgSender(), amount);                    // ← tokens destroyed here

    bytes32 amount_b = bytes32(amount);
    bytes20 recipient_b = bytes20(recipient);
    bytes memory input = abi.encodePacked("\x01", amount_b, recipient_b);
    uint input_size = 1 + 32 + 20;

    assembly {
        let res := call(gas(), 0xb0bd02f6a392af548bdf1cfaee5dfa0eefcc8eab,
                        0, add(input, 32), input_size, 0, 32)
        // res is NEVER checked — failure is silently ignored
    }
}
```

The EVM `call()` opcode returns `0` on failure. Because `res` is never inspected and no `revert` is issued on `res == 0`, the outer transaction completes successfully even when the precompile rejects the call. The `_burn()` that already executed is **not** rolled back, because it is in the caller's frame, not the callee's.

The ExitToNear precompile (`native.rs`) can fail with errors such as `ERR_INVALID_RECEIVER_ACCOUNT_ID`, `ERR_INVALID_AMOUNT`, `ERR_ETH_ATTACHED_FOR_ERC20_EXIT`, or any paused-state error. Any of these causes `call()` to return `0`, which the contract ignores.

---

### Impact Explanation

When the precompile call fails:
- The ERC-20 tokens are **permanently burned** (supply reduced, user balance zeroed).
- No corresponding NEP-141 or ETH tokens are released on the destination chain.
- The user has no recourse; the state is irrecoverable.

This constitutes **permanent freezing (destruction) of user funds** — a Critical impact under the allowed scope.

---

### Likelihood Explanation

The entry path is fully unprivileged: any holder of the ERC-20 mirror token can call `withdrawToNear` or `withdrawToEthereum`. Realistic failure triggers include:

1. **Invalid NEAR account ID** — a recipient string that fails `AccountId` parsing inside the precompile (e.g., uppercase letters, leading/trailing dots, length > 64).
2. **Paused precompile** — if the Aurora engine's pause flag covers the exit precompile, the `call()` returns `0`.
3. **Connector accounting mismatch** — if the connector's NEP-141 balance is insufficient, the precompile reverts.

Scenario 1 is trivially reachable by any user who mistypes or programmatically constructs an invalid recipient string.

---

### Recommendation

Check `res` after each precompile `call()` and revert if it is `0`:

```solidity
assembly {
    let res := call(gas(), 0xe9217bc70b7ed1f598ddd3199e80b093fa71124f,
                    0, add(input, 32), input_size, 0, 32)
    if iszero(res) { revert(0, 0) }
}
```

Apply the same fix to `withdrawToEthereum`. Alternatively, restructure both functions to call the precompile **before** burning, so that a failed precompile call causes the entire transaction to revert without any token loss.

---

### Proof of Concept

1. Alice holds 100 units of an `EvmErc20V2` mirror token on Aurora.
2. Alice calls `withdrawToNear("INVALID ACCOUNT!!!", 100)` — the recipient string contains spaces and exclamation marks, which are not valid NEAR account ID characters.
3. `_burn(alice, 100)` executes: Alice's ERC-20 balance drops to 0, total supply decreases by 100.
4. The `call()` to the ExitToNear precompile at `0xe9217bc70b7ed1f598ddd3199e80b093fa71124f` fails because `parse_recipient()` in `native.rs` returns `ERR_INVALID_RECEIVER_ACCOUNT_ID`; the precompile reverts internally, and `call()` returns `0`.
5. `res` is never read; no `revert` is issued; the outer transaction succeeds.
6. Alice's 100 ERC-20 tokens are gone. No NEAR tokens arrive at any account. Funds are permanently frozen. [1](#0-0) [2](#0-1) [3](#0-2)

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

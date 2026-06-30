### Title
Unchecked Precompile Call Return Value in `withdrawToNear`/`withdrawToEthereum` Causes Permanent Token Burn Without Exit - (File: `etc/eth-contracts/contracts/EvmErc20.sol`, `etc/eth-contracts/contracts/EvmErc20V2.sol`)

---

### Summary

Both `EvmErc20.sol` and `EvmErc20V2.sol` burn the caller's ERC-20 tokens before calling the exit precompile via inline assembly, but never check the return value of that `call()`. If the precompile call fails for any reason (e.g., invalid NEAR recipient account ID, out-of-gas in the precompile, or delegate-call context mismatch), the burn is committed and the transaction succeeds, but no corresponding NEP-141 transfer is scheduled on NEAR. The user's tokens are permanently destroyed with no recovery path.

---

### Finding Description

In `EvmErc20.sol`, `withdrawToNear` executes:

```solidity
function withdrawToNear(bytes memory recipient, uint256 amount) external override {
    _burn(_msgSender(), amount);          // tokens destroyed here

    bytes32 amount_b = bytes32(amount);
    bytes memory input = abi.encodePacked("\x01", amount_b, recipient);
    uint input_size = 1 + 32 + recipient.length;

    assembly {
        let res := call(gas(), 0xe9217bc70b7ed1f598ddd3199e80b093fa71124f, 0, add(input, 32), input_size, 0, 32)
        // res is NEVER checked — no require(res), no revert on failure
    }
}
``` [1](#0-0) 

The identical pattern exists in `EvmErc20V2.sol`: [2](#0-1) 

And in `withdrawToEthereum` in both contracts: [3](#0-2) [4](#0-3) 

The `ExitToNear` precompile (`engine-precompiles/src/native.rs`) returns an `ExitError` — causing the low-level `call()` to return `0` — in multiple reachable conditions:

- `ExitToNearParams::try_from(input)` fails if the recipient bytes do not parse as a valid NEAR account ID.
- `get_nep141_from_erc20` fails if the ERC-20 is not registered.
- The precompile rejects calls where `context.address != exit_to_near::ADDRESS.raw()` (delegate-call context). [5](#0-4) 

In contrast, the Rust-side `receive_erc20_tokens` (the deposit path) correctly propagates errors via `.and_then(submit_result_or_err)`: [6](#0-5) 

No equivalent guard exists on the withdrawal path in the Solidity contracts.

---

### Impact Explanation

When the precompile call returns `0` (failure), the EVM assembly block exits silently. Because `_burn` already executed and Solidity has no automatic rollback triggered by an unchecked low-level `call` failure, the transaction finalizes successfully: the user's ERC-20 balance is reduced to zero, but no NEP-141 `ft_transfer` promise is ever created on NEAR. The bridged value is permanently destroyed with no refund mechanism. This constitutes **permanent freezing of funds**.

---

### Likelihood Explanation

Any unprivileged ERC-20 token holder can trigger this by calling `withdrawToNear` with a syntactically invalid NEAR account ID (e.g., an empty byte string, a string exceeding 64 characters, or one containing disallowed characters). The NEAR account ID validation occurs inside the precompile, not in the Solidity contract, so the contract provides no input guard. No special privilege or coordination is required; a single self-directed transaction is sufficient.

---

### Recommendation

After the precompile `call()`, check `res` and revert if it is zero:

```solidity
assembly {
    let res := call(gas(), 0xe9217bc70b7ed1f598ddd3199e80b093fa71124f, 0, add(input, 32), input_size, 0, 32)
    if iszero(res) { revert(0, 0) }
}
```

Apply the same fix to `withdrawToEthereum` in both `EvmErc20.sol` and `EvmErc20V2.sol`. This mirrors the `safeTransfer` pattern recommended in the original report: always verify the success of the external call before allowing state changes (the burn) to persist.

---

### Proof of Concept

1. Deploy `EvmErc20` on Aurora (standard bridge flow: NEP-141 → ERC-20).
2. Mint tokens to address `A`.
3. From address `A`, call `withdrawToNear(bytes(""), amount)` — an empty byte string is not a valid NEAR account ID.
4. The `ExitToNear` precompile's `ExitToNearParams::try_from` fails to parse the recipient, returns `ExitError`, and the low-level `call` returns `0`.
5. The assembly block exits without reverting; `withdrawToNear` returns successfully.
6. Observe: `A`'s ERC-20 balance is `0`; no NEP-141 transfer was scheduled; the NEP-141 balance held by Aurora is unchanged. Funds are permanently frozen.

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

**File:** engine-precompiles/src/native.rs (L413-419)
```rust
        if is_static {
            return Err(ExitError::Other(Cow::from("ERR_INVALID_IN_STATIC")));
        } else if context.address != exit_to_near::ADDRESS.raw() {
            return Err(ExitError::Other(Cow::from("ERR_INVALID_IN_DELEGATE")));
        }

        let exit_to_near_params = ExitToNearParams::try_from(input)?;
```

**File:** engine/src/engine.rs (L826-837)
```rust
        let result = self
            .call(
                &erc20_admin_address,
                &erc20_token,
                Wei::zero(),
                setup_receive_erc20_tokens_input(&recipient, amount),
                u64::MAX,
                Vec::new(), // TODO: are there values we should put here?
                Vec::new(),
                handler,
            )
            .and_then(submit_result_or_err)?;
```

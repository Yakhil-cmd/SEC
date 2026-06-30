The exploit path is concrete and verifiable. Let me confirm the exact mechanics:

### Title
Silent Precompile Failure After `_burn` in `withdrawToNear` Permanently Destroys User Tokens — (`etc/eth-contracts/contracts/EvmErc20.sol`)

---

### Summary

`withdrawToNear` burns the caller's ERC-20 tokens before invoking the `ExitToNear` precompile. When the `recipient` argument causes the assembled input to exceed `MAX_INPUT_SIZE` (1 024 bytes), the precompile rejects the call with `ERR_INVALID_INPUT`, the EVM `call` opcode returns `0`, but the Solidity function never inspects the return value and does not revert. The burn is committed, no NEAR-side promise is scheduled, and the user's tokens are irrecoverably destroyed.

---

### Finding Description

**Step 1 — Entry point (`EvmErc20.sol`, `withdrawToNear`)**

Any EVM account that holds a balance may call `withdrawToNear`. There is no access control on this function. [1](#0-0) 

The function:
1. Burns the caller's tokens unconditionally (`_burn` at line 54).
2. Builds `input = 0x01 ‖ amount_b (32 bytes) ‖ recipient`, so `input_size = 1 + 32 + recipient.length`.
3. Calls the `ExitToNear` precompile via inline assembly and stores the return value in `res` — but `res` is **never read or checked**.

**Step 2 — Size guard in the precompile (`native.rs`, `parse_input` / `validate_input_size`)**

`MAX_INPUT_SIZE` is 1 024 bytes. [2](#0-1) 

`parse_input` (both feature variants) calls `validate_input_size` as its first action: [3](#0-2) 

`validate_input_size` returns `Err(ExitError::Other("ERR_INVALID_INPUT"))` whenever `input.len() > MAX_INPUT_SIZE`: [4](#0-3) 

This error propagates through `ExitToNearParams::try_from` (line 742) and out of `run`, causing the EVM sub-call to return `0`. [5](#0-4) 

**Step 3 — Unchecked return value**

Back in `withdrawToNear`, the assembly block captures `res` but performs no conditional revert: [6](#0-5) 

The outer transaction succeeds. The `_burn` state change is kept. No `promise_log` is ever emitted, so the Aurora engine schedules no `ft_transfer` on NEAR.

**Trigger threshold**

`input_size = 1 + 32 + recipient.length > 1024` ⟹ `recipient.length > 991 bytes`.

Any caller who supplies a `recipient` of 992 bytes or more (while holding a non-zero balance) will silently lose their tokens.

---

### Impact Explanation

The EVM-side tokens are burned and gone. The corresponding NEP-141 tokens remain locked inside the connector contract on NEAR with no scheduled release. The user suffers a permanent, unrecoverable loss of their bridged assets. No admin function in the in-scope code can re-mint the burned EVM tokens or retroactively schedule the missing promise.

---

### Likelihood Explanation

- No privilege is required; any token holder can trigger this.
- The only precondition is a `recipient` argument longer than 991 bytes, which is trivially constructable.
- A malicious actor could craft such a call deliberately; an unsophisticated user could also hit this accidentally if a dApp passes an unusually long NEAR account ID or message string.

---

### Recommendation

Check the precompile call return value and revert if it fails:

```solidity
function withdrawToNear(bytes memory recipient, uint256 amount) external override {
    require(1 + 32 + recipient.length <= 1024, "recipient too long");
    _burn(_msgSender(), amount);

    bytes32 amount_b = bytes32(amount);
    bytes memory input = abi.encodePacked("\x01", amount_b, recipient);
    uint input_size = 1 + 32 + recipient.length;

    assembly {
        let res := call(gas(), 0xe9217bc70b7ed1f598ddd3199e80b093fa71124f,
                        0, add(input, 32), input_size, 0, 32)
        if iszero(res) { revert(0, 0) }
    }
}
```

Either guard (the `require` on length, or the `if iszero(res) { revert }`) independently prevents the loss. Both together are defense-in-depth. The same pattern applies to `withdrawToEthereum`. [7](#0-6) 

---

### Proof of Concept

```solidity
// SPDX-License-Identifier: MIT
pragma solidity ^0.8.0;

interface IEvmErc20 {
    function withdrawToNear(bytes memory recipient, uint256 amount) external;
    function balanceOf(address) external view returns (uint256);
}

contract PoC {
    function exploit(address token, uint256 amount) external {
        // Build a recipient of 992 bytes (> 991 threshold)
        bytes memory oversizedRecipient = new bytes(992);
        for (uint i = 0; i < 992; i++) oversizedRecipient[i] = 0x61; // 'a'

        uint256 before = IEvmErc20(token).balanceOf(address(this));

        // This call succeeds (no revert) but the precompile silently fails.
        // Tokens are burned; no NEAR promise is created.
        IEvmErc20(token).withdrawToNear(oversizedRecipient, amount);

        uint256 after_ = IEvmErc20(token).balanceOf(address(this));
        // assert: before - after_ == amount  (tokens burned)
        // assert: no ExitToNear promise log was emitted
        require(before - after_ == amount, "burn did not happen");
    }
}
```

Fuzz assertion: for all `recipientLen` in `[992, 2000]`, `balanceOf` decreases by `amount` and the Aurora engine emits zero `promise_log` entries for the transaction — confirming the burn-without-promise invariant violation.

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

**File:** engine-precompiles/src/native.rs (L40-40)
```rust
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

**File:** engine-precompiles/src/native.rs (L419-419)
```rust
        let exit_to_near_params = ExitToNearParams::try_from(input)?;
```

**File:** engine-precompiles/src/native.rs (L787-791)
```rust
#[cfg(not(feature = "error_refund"))]
fn parse_input(input: &[u8]) -> Result<&[u8], ExitError> {
    validate_input_size(input, MIN_INPUT_SIZE, MAX_INPUT_SIZE)?;
    Ok(&input[1..])
}
```

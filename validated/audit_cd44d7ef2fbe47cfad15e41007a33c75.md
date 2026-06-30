## Tracing the Exact Code Path

**Step 1 — `withdrawToNear` in `EvmErc20.sol` (lines 53–63)**

```solidity
function withdrawToNear(bytes memory recipient, uint256 amount) external override {
    _burn(_msgSender(), amount);          // ← burn happens FIRST

    bytes memory input = abi.encodePacked("\x01", amount_b, recipient);
    uint input_size = 1 + 32 + recipient.length;

    assembly {
        let res := call(gas(), 0xe9217bc70b7ed1f598ddd3199e80b093fa71124f,
                        0, add(input, 32), input_size, 0, 32)
        // res is NEVER checked — no require, no revert
    }
}
``` [1](#0-0) 

**Step 2 — `validate_input_size` in the precompile**

`MAX_INPUT_SIZE = 1_024`. For the ERC-20 path the full input is `1 (flag) + 32 (amount) + recipient.length`. The check is strict:

```rust
fn validate_input_size(input: &[u8], min: usize, max: usize) -> Result<(), ExitError> {
    if input.len() < min || input.len() > max {
        return Err(ExitError::Other(Cow::from("ERR_INVALID_INPUT")));
    }
    Ok(())
}
``` [2](#0-1) [3](#0-2) 

`validate_input_size` is called inside `parse_input`, which is the very first thing `ExitToNearParams::try_from` does: [4](#0-3) [5](#0-4) 

**Boundary:** `recipient.length = 991` → total = 1024 → passes. `recipient.length = 992` → total = 1025 → `Err(ExitError::Other("ERR_INVALID_INPUT"))`.

**Step 3 — How the EVM surfaces the precompile error**

`process_precompile` maps the `ExitError` to `PrecompileFailure::Error`: [6](#0-5) 

`PrecompileFailure::Error` causes the EVM's `CALL` opcode to return `0` (failure) in the caller's stack — but it does **not** revert the calling frame. The caller (`EvmErc20`) continues executing normally.

**Step 4 — The silent-fail invariant break**

Because `res` is captured but never inspected in the assembly block, `withdrawToNear` returns successfully even when the precompile rejected the input. The `_burn` on line 54 is already committed; no NEAR promise log is emitted; the NEP-141 tokens remain locked in Aurora's account with no corresponding ERC-20 balance to redeem them. [7](#0-6) 

---

### Title
Unchecked precompile call return value in `withdrawToNear` allows burn-and-silent-fail at the `MAX_INPUT_SIZE` boundary — (`etc/eth-contracts/contracts/EvmErc20.sol`)

### Summary
`EvmErc20.withdrawToNear` burns the caller's ERC-20 tokens before invoking the `ExitToNear` precompile, and never checks the `call` return value. When the recipient byte-string is ≥ 992 bytes, `validate_input_size` rejects the input and the precompile returns `PrecompileFailure::Error`, which the EVM surfaces as `call` returning `0`. Because `res` is never tested, the Solidity function returns successfully: tokens are burned, no NEAR promise is scheduled, and the underlying NEP-141 tokens are permanently stranded in Aurora's account.

### Finding Description
**Root cause — `EvmErc20.sol` lines 53–63:**
The `_burn` is unconditional and precedes the precompile call. The inline assembly captures `res` but never branches on it. Any `recipient` whose encoded length causes `1 + 32 + recipient.length > 1024` will trigger `ERR_INVALID_INPUT` inside `parse_input` → `ExitToNearParams::try_from` → `ExitToNear::run`, which propagates as `PrecompileFailure::Error`. The EVM sets the subcall success flag to `0` and returns control to `withdrawToNear`, which exits without reverting.

**Boundary (no `error_refund` feature):**
- Max valid recipient length: `1024 − 1 − 32 = 991 bytes`
- First over-limit: `992 bytes` → total input `1025 > 1024`

**Affected precompile path:**
`parse_input` (line 788) → `validate_input_size` (line 295) → `Err` propagated through `ExitToNearParams::try_from` (line 730) → `ExitToNear::run` (line 419) → `process_precompile` (line 173) → `PrecompileFailure::Error`. [8](#0-7) [9](#0-8) 

### Impact Explanation
The caller's ERC-20 balance is reduced to zero (burned). The corresponding NEP-141 tokens remain in Aurora's custody on NEAR with no mechanism for the user to reclaim them, because the ERC-20 balance that would authorize a future withdrawal no longer exists. This constitutes at minimum **temporary freezing** (NEP-141 tokens stuck in Aurora's account) and in practice **permanent loss** of the user's funds unless an admin manually intervenes.

### Likelihood Explanation
The attack surface is fully unprivileged: `withdrawToNear` is `external` with no access control. Any token holder can trigger this by passing a recipient argument of ≥ 992 bytes. The condition is deterministic and requires no special timing or race. A user could trigger it accidentally (e.g., a long NEAR account ID with a message suffix) or maliciously against themselves or others via a wrapper contract.

### Recommendation
Add a revert on precompile call failure inside the assembly block:

```solidity
assembly {
    let res := call(gas(), 0xe9217bc70b7ed1f598ddd3199e80b093fa71124f,
                    0, add(input, 32), input_size, 0, 32)
    if iszero(res) { revert(0, 0) }
}
```

This ensures the burn is atomically rolled back whenever the precompile rejects the input, restoring the invariant that tokens are either burned-and-promised or not burned at all.

### Proof of Concept
```solidity
// SPDX-License-Identifier: CC0-1.0
pragma solidity ^0.8.0;

interface IEvmErc20 {
    function withdrawToNear(bytes memory recipient, uint256 amount) external;
    function balanceOf(address account) external view returns (uint256);
}

contract BoundaryExploit {
    IEvmErc20 token;
    constructor(address _token) { token = IEvmErc20(_token); }

    function exploit(uint256 amount) external {
        // Build a 992-byte recipient (1 byte over the 991-byte limit)
        bytes memory recipient = new bytes(992);
        for (uint i = 0; i < 992; i++) recipient[i] = 0x61; // 'a'

        uint256 before = token.balanceOf(address(this));
        token.withdrawToNear(recipient, amount);
        uint256 after_ = token.balanceOf(address(this));

        // Tokens are burned (before > after_) but no NEAR promise was emitted.
        // assert(before - after_ == amount);  // burn succeeded
        // No ExitToNear log → no NEP-141 transfer → funds frozen.
    }
}
```

Boundary fuzz assertion: iterate `recipient.length` from 989 to 993. At length ≤ 991 the precompile emits a promise log; at length ≥ 992 no promise log is emitted yet the ERC-20 balance decreases by `amount`.

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

**File:** engine-precompiles/src/native.rs (L727-742)
```rust
impl<'a> TryFrom<&'a [u8]> for ExitToNearParams<'a> {
    type Error = ExitError;

    fn try_from(input: &'a [u8]) -> Result<Self, Self::Error> {
        // The first byte of the input is a flag, selecting the behavior to be triggered:
        // 0x00 -> Eth(base) token withdrawal
        // 0x01 -> ERC-20 token withdrawal
        let flag = input
            .first()
            .copied()
            .ok_or_else(|| ExitError::Other(Cow::from("ERR_MISSING_FLAG")))?;

        #[cfg(feature = "error_refund")]
        let (refund_address, input) = parse_input(input)?;
        #[cfg(not(feature = "error_refund"))]
        let input = parse_input(input)?;
```

**File:** engine-precompiles/src/native.rs (L787-791)
```rust
#[cfg(not(feature = "error_refund"))]
fn parse_input(input: &[u8]) -> Result<&[u8], ExitError> {
    validate_input_size(input, MIN_INPUT_SIZE, MAX_INPUT_SIZE)?;
    Ok(&input[1..])
}
```

**File:** engine-precompiles/src/lib.rs (L164-175)
```rust
fn process_precompile(
    p: &dyn Precompile,
    handle: &impl PrecompileHandle,
) -> Result<PrecompileOutput, PrecompileFailure> {
    let input = handle.input();
    let gas_limit = handle.gas_limit();
    let context = handle.context();
    let is_static = handle.is_static();

    p.run(input, gas_limit.map(EthGas::new), context, is_static)
        .map_err(|exit_status| PrecompileFailure::Error { exit_status })
}
```

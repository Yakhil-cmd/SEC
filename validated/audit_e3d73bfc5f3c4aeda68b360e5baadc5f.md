### Title
Silent Permanent Token Loss via Calldata Encoding Mismatch Between `EvmErc20V2.withdrawToNear` and the Non-`error_refund` Exit Precompile — (File: `etc/eth-contracts/contracts/EvmErc20V2.sol`)

---

### Summary

`EvmErc20V2.sol` encodes a 20-byte `sender` address into the calldata it sends to the `ExitToNear` precompile, while `EvmErc20.sol` does not. The precompile's input parser is conditioned on the compile-time `error_refund` feature flag: with the flag absent, it does not expect the sender prefix and misparses the amount field. Because neither contract checks the return value of the low-level `call()` to the precompile, a failed precompile invocation silently succeeds at the EVM level — after the user's tokens have already been burned. The result is permanent, irrecoverable token loss.

---

### Finding Description

`EvmErc20.sol` and `EvmErc20V2.sol` both implement the `IExit` interface and expose an identical external signature for `withdrawToNear(bytes memory recipient, uint256 amount)`. However, the binary payload each contract sends to the `ExitToNear` precompile at `0xe9217bc70b7ed1f598ddd3199e80b093fa71124f` differs structurally.

**`EvmErc20.sol` (V1) encodes:**
```
[flag: 0x01 (1 byte)] [amount (32 bytes)] [recipient (variable)]
``` [1](#0-0) 

**`EvmErc20V2.sol` (V2) encodes:**
```
[flag: 0x01 (1 byte)] [sender address (20 bytes)] [amount (32 bytes)] [recipient (variable)]
``` [2](#0-1) 

The precompile's `parse_input` function is gated by the `error_refund` compile-time feature:

- **With `error_refund`**: expects the 20-byte sender prefix, extracts it as the refund address, then reads the amount from the correct offset.
- **Without `error_refund`**: skips the flag byte and reads the next 32 bytes directly as the amount. [3](#0-2) 

When `EvmErc20V2.sol` is used against a precompile compiled **without** `error_refund`, the precompile reads bytes `[1..33]` as the amount. Those bytes are `[sender_address_bytes(20)][first_12_bytes_of_actual_amount]`. The resulting `U256` value has the sender's address occupying the high 160 bits, making it astronomically larger than `u128::MAX`. The guard:

```rust
if amount > U256::from(u128::MAX) {
    return Err(ExitError::Other(Cow::from("ERR_INVALID_AMOUNT")));
}
``` [4](#0-3) 

causes the precompile to return an error. The EVM `call()` opcode returns `0` (failure). However, both contracts capture the return value in a local variable `res` and never branch on it — there is no `require(res != 0)` or equivalent revert:

```solidity
assembly {
    let res := call(gas(), 0xe9217bc70b7ed1f598ddd3199e80b093fa71124f, 0, add(input, 32), input_size, 0, 32)
}
``` [5](#0-4) 

The outer transaction therefore succeeds. The `_burn` on line 55 has already executed and is not rolled back. The user's ERC-20 tokens are destroyed with no corresponding NEP-141 transfer on NEAR. [6](#0-5) 

The symmetric case also applies: if `EvmErc20.sol` (V1) is used against a precompile compiled **with** `error_refund`, bytes `[1..21]` (the first 20 bytes of the amount) are misread as the refund address, and bytes `[21..53]` (the last 12 bytes of the amount concatenated with the first 20 bytes of the recipient string) are misread as the amount, producing an incorrect NEP-141 transfer quantity.

---

### Impact Explanation

**Critical — Permanent freezing of funds.**

Any token holder who calls `withdrawToNear` on an `EvmErc20V2` token when the deployed precompile was compiled without the `error_refund` feature (or on an `EvmErc20` token when the precompile was compiled with `error_refund`) will have their ERC-20 tokens permanently burned with no NEAR-side credit. There is no refund path: without `error_refund`, `callback_args.refund` is `None`, so no `exit_to_near_precompile_callback` is scheduled. [7](#0-6) 

The burned tokens cannot be recovered. The loss is bounded only by the user's token balance at the time of the call.

---

### Likelihood Explanation

The two contract versions (`EvmErc20.sol`, `EvmErc20V2.sol`) coexist in the repository and both target the same hardcoded precompile address. There is no on-chain mechanism that enforces which contract version must be paired with which precompile build. A token deployer using the wrong contract version, or a precompile upgrade that changes the `error_refund` feature without migrating existing token contracts, silently activates the bug for every subsequent `withdrawToNear` call. Any unprivileged token holder can trigger the loss simply by calling the standard `withdrawToNear` function.

---

### Recommendation

1. **Check the precompile return value in both contracts.** Add `require(res != 0, "precompile call failed")` immediately after the `call()` assembly block in both `EvmErc20.sol` and `EvmErc20V2.sol`. This ensures the `_burn` is reverted if the precompile rejects the input.

2. **Unify the calldata encoding.** Eliminate the two-version divergence. Either always include the sender prefix (and always compile with `error_refund`) or never include it. Document the required pairing explicitly.

3. **Add a version sentinel.** Encode a version byte in the precompile input so the precompile can detect and reject mismatched contract versions with a clear error rather than silently misinterpreting the payload.

---

### Proof of Concept

1. Deploy `EvmErc20V2.sol` as a bridged token on Aurora (precompile compiled without `error_refund`).
2. Mint 1000 tokens to `attacker_address`.
3. Call `withdrawToNear(b"victim.near", 1000)` from `attacker_address`.
4. Internally: `_burn(attacker_address, 1000)` executes; calldata `[0x01][attacker_addr_20_bytes][0x00...03E8][victim.near]` is sent to the precompile.
5. Precompile reads bytes `[1..33]` as amount = `attacker_addr_as_uint256 >> 96 | 0x3E8_prefix` >> `u128::MAX` → returns `ERR_INVALID_AMOUNT`.
6. EVM `call()` returns `0`; Solidity does not revert.
7. Transaction succeeds. `attacker_address` ERC-20 balance is 0. No NEP-141 transfer occurred. Funds are permanently lost.

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

**File:** engine-precompiles/src/native.rs (L778-791)
```rust
#[cfg(feature = "error_refund")]
fn parse_input(input: &[u8]) -> Result<(Address, &[u8]), ExitError> {
    validate_input_size(input, MIN_INPUT_SIZE, MAX_INPUT_SIZE)?;
    let mut buffer = [0; 20];
    buffer.copy_from_slice(&input[1..21]);
    let refund_address = Address::from_array(buffer);
    Ok((refund_address, &input[21..]))
}

#[cfg(not(feature = "error_refund"))]
fn parse_input(input: &[u8]) -> Result<&[u8], ExitError> {
    validate_input_size(input, MIN_INPUT_SIZE, MAX_INPUT_SIZE)?;
    Ok(&input[1..])
}
```

### Title
`EvmErc20` (v1) Sends Malformed Calldata to `ExitToNear` Precompile When `error_refund` Is Enabled, Causing Permanent Loss of Bridged Tokens - (File: `etc/eth-contracts/contracts/EvmErc20.sol`)

---

### Summary

When the `error_refund` feature is compiled in, the `ExitToNear` precompile expects a 20-byte `refund_address` field immediately after the flag byte. The legacy `EvmErc20` (v1) contract does **not** include this field, so the precompile misparses the calldata: it reads the first 20 bytes of the big-endian amount as the refund address, and then attempts to parse the remaining bytes (last 12 bytes of amount concatenated with the first 20 bytes of the NEAR recipient string) as the 32-byte amount. This almost always produces a value exceeding `u128::MAX`, causing the precompile call to fail with `ERR_INVALID_AMOUNT`. Because `EvmErc20` does not check the return value of the assembly `call`, the ERC-20 burn succeeds and the transaction completes, but no bridge transfer is initiated and no refund callback is registered. The user's tokens are permanently destroyed.

---

### Finding Description

**`EvmErc20.withdrawToNear` (v1) — wrong calldata layout for `error_refund` mode**

`EvmErc20.sol` constructs the precompile input as:

```
[0x01] [amount_b (32 bytes)] [recipient (bytes)]
``` [1](#0-0) 

`EvmErc20V2.sol` (the corrected version) constructs it as:

```
[0x01] [sender (20 bytes)] [amount_b (32 bytes)] [recipient (bytes)]
``` [2](#0-1) 

When `error_refund` is enabled, `parse_input` reads bytes `[1..21]` as the `refund_address` and returns `&input[21..]` as the remaining slice: [3](#0-2) 

For a v1 call, bytes `[1..21]` are the first 20 bytes of the big-endian amount (typically all zeros for amounts fitting in 12 bytes, i.e., `address(0)`). The remaining slice `&input[21..]` is then `[amount_b[20..32], recipient...]`. `parse_amount` reads the first 32 bytes of this slice: [4](#0-3) 

This 32-byte value is `amount_b[20..32] || recipient[0..20]`. For any NEAR account ID (e.g., `"alice.near"` → `0x616c6963652e6e656172...`), the low 20 bytes are non-zero ASCII, making the parsed value far exceed `u128::MAX`. `parse_amount` returns `ERR_INVALID_AMOUNT`: [5](#0-4) 

The precompile call returns failure (0). `EvmErc20` does not check the return value: [6](#0-5) 

The `_burn` on line 54 already executed. No promise is created, no `exit_to_near_precompile_callback` is registered, and no refund path exists. The tokens are permanently gone. [1](#0-0) 

---

### Impact Explanation

**Critical — Permanent freezing/destruction of user funds.**

Every call to `EvmErc20.withdrawToNear()` on a v1-deployed ERC-20 contract, when the engine is compiled with `error_refund`, results in the caller's tokens being burned with no bridge transfer and no refund. The tokens are irrecoverably lost. This affects all NEP-141-backed ERC-20 tokens deployed via the v1 factory that have not been migrated to `EvmErc20V2`.

---

### Likelihood Explanation

**High.** The `error_refund` feature is a production compile-time flag (the `MIN_INPUT_SIZE` constant changes from 3 to 21 when enabled, confirming it is a real deployment variant). Any user interacting with a v1 `EvmErc20` contract on an engine build with `error_refund` enabled triggers the loss. No special attacker action is required — ordinary token withdrawal is sufficient. [7](#0-6) 

---

### Recommendation

1. **`EvmErc20.sol`**: Update `withdrawToNear` to include `msg.sender` as the refund address in the precompile input, matching `EvmErc20V2`:
   ```solidity
   bytes memory input = abi.encodePacked("\x01", _msgSender(), amount_b, recipient);
   uint input_size = 1 + 20 + 32 + recipient.length;
   ```
2. **Check the assembly `call` return value** and revert if the precompile call fails, so the burn is also reverted:
   ```solidity
   assembly {
       let res := call(...)
       if iszero(res) { revert(0, 0) }
   }
   ```
3. Migrate all v1 `EvmErc20` deployments to `EvmErc20V2` before enabling `error_refund` in production.

---

### Proof of Concept

1. Deploy `EvmErc20` (v1) backed by a NEP-141 token.
2. Mint ERC-20 tokens to `alice` (EVM address).
3. Compile the Aurora engine with `error_refund` feature enabled.
4. `alice` calls `EvmErc20.withdrawToNear("alice.near", 1_000_000)`.
5. `_burn(alice, 1_000_000)` executes — alice's ERC-20 balance drops to 0.
6. Precompile receives `[0x01, 0x00...00 (20 bytes of amount high), 0x00...0F4240 (12 bytes of amount low), 0x616c6963652e6e656172 (alice.near)]`.
7. `parse_input` extracts `refund_address = address(0)`, remaining = `[0x00...0F4240, 0x616c6963652e6e656172...]`.
8. `parse_amount` reads 32 bytes = `0x000F4240616c6963652e6e656172...` >> `u128::MAX` → `ERR_INVALID_AMOUNT`.
9. Precompile call returns 0; `EvmErc20` ignores it; transaction succeeds.
10. Alice's 1,000,000 tokens are permanently destroyed. No NEP-141 transfer occurred. No refund callback was registered. [1](#0-0) [2](#0-1) [8](#0-7) [3](#0-2)

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

**File:** engine-precompiles/src/native.rs (L36-39)
```rust
#[cfg(not(feature = "error_refund"))]
const MIN_INPUT_SIZE: usize = 3;
#[cfg(feature = "error_refund")]
const MIN_INPUT_SIZE: usize = 21;
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

**File:** engine-precompiles/src/native.rs (L739-742)
```rust
        #[cfg(feature = "error_refund")]
        let (refund_address, input) = parse_input(input)?;
        #[cfg(not(feature = "error_refund"))]
        let input = parse_input(input)?;
```

**File:** engine-precompiles/src/native.rs (L758-763)
```rust
            0x1 => {
                let amount = parse_amount(&input[..32])?;
                let Recipient {
                    receiver_account_id,
                    message,
                } = parse_recipient(&input[32..])?;
```

**File:** engine-precompiles/src/native.rs (L778-785)
```rust
#[cfg(feature = "error_refund")]
fn parse_input(input: &[u8]) -> Result<(Address, &[u8]), ExitError> {
    validate_input_size(input, MIN_INPUT_SIZE, MAX_INPUT_SIZE)?;
    let mut buffer = [0; 20];
    buffer.copy_from_slice(&input[1..21]);
    let refund_address = Address::from_array(buffer);
    Ok((refund_address, &input[21..]))
}
```

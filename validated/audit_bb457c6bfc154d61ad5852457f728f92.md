### Title
`EvmErc20` (V1) Sends Wrong Input Format to `ExitToNear` Precompile When `error_refund` Is Enabled, Causing Permanent Fund Freeze — (File: `etc/eth-contracts/contracts/EvmErc20.sol`)

---

### Summary

`EvmErc20.sol` (V1) and `EvmErc20V2.sol` (V2) are both deployed as ERC-20 wrappers for NEP-141 tokens on Aurora. Their `withdrawToNear` functions encode the precompile call input differently. The `ExitToNear` precompile at `0xe9217bc70b7ed1f598ddd3199e80b093fa71124f` has two distinct parsing modes controlled by the compile-time `error_refund` feature flag. When `error_refund` is enabled in the production engine, V1 tokens send input in the wrong format, causing the precompile to misparse or reject the call. Because the assembly `call` return value is never checked, the ERC-20 tokens are burned but the corresponding NEP-141 tokens are never transferred — a permanent fund freeze.

---

### Finding Description

**Two incompatible input formats exist for the same precompile:**

`EvmErc20.sol` (V1) encodes:
```
\x01 | amount (32 bytes) | recipient (N bytes)
``` [1](#0-0) 

`EvmErc20V2.sol` (V2) encodes:
```
\x01 | sender (20 bytes) | amount (32 bytes) | recipient (N bytes)
``` [2](#0-1) 

The `ExitToNear` precompile enforces a minimum input size that is **feature-flag-dependent**:

```rust
#[cfg(not(feature = "error_refund"))]
const MIN_INPUT_SIZE: usize = 3;   // flag only
#[cfg(feature = "error_refund")]
const MIN_INPUT_SIZE: usize = 21;  // flag + 20-byte refund_address
``` [3](#0-2) 

When `error_refund` is enabled, the precompile expects bytes `[1..21]` to be a 20-byte refund address, bytes `[21..53]` to be the 32-byte amount, and bytes `[53..]` to be the recipient. A V1 token sends no refund address, so:

- Bytes `[1..21]` → parsed as refund address = **first 20 bytes of the actual amount** (garbage)
- Bytes `[21..53]` → parsed as amount = **last 12 bytes of amount + first 20 bytes of recipient** (garbage)
- Bytes `[53..]` → parsed as recipient = **truncated/empty recipient string**

An empty or invalid recipient causes `parse_recipient` to return `ExitError`, which causes the precompile to return failure. [4](#0-3) 

**The assembly call return value is never checked in either contract version:**

```solidity
assembly {
    let res := call(gas(), 0xe9217bc70b7ed1f598ddd3199e80b093fa71124f, 0, add(input, 32), input_size, 0, 32)
    // res is never inspected — no revert on failure
}
``` [5](#0-4) 

Because `_burn` executes **before** the assembly call, and the call failure is silently swallowed, the ERC-20 tokens are destroyed while the NEP-141 tokens on NEAR remain locked in the Aurora engine account with no recovery path. [6](#0-5) 

---

### Impact Explanation

**Permanent fund freeze (Critical).** Any user holding V1 `EvmErc20` tokens who calls `withdrawToNear` when the production engine is compiled with `error_refund` enabled will have their ERC-20 tokens burned with no corresponding NEP-141 transfer. The NEP-141 tokens remain locked in the Aurora engine account. There is no recovery function. The loss is permanent and proportional to the amount withdrawn.

---

### Likelihood Explanation

**Medium.** V1 `EvmErc20` contracts are immutable once deployed. The existence of V2 — which was created specifically to add the sender field required by `error_refund` — strongly implies that `error_refund` is enabled in the production engine build. Any user of a V1-deployed token who attempts a NEAR withdrawal triggers the freeze. The entry path requires only a standard ERC-20 `withdrawToNear` call, which is the normal user-facing bridge exit function.

---

### Recommendation

1. **Check the return value of the assembly `call`** in both `EvmErc20.sol` and `EvmErc20V2.sol`. If the precompile call fails, revert the transaction so the `_burn` is also reverted:
   ```solidity
   assembly {
       let res := call(gas(), 0xe9217bc70b7ed1f598ddd3199e80b093fa71124f, 0, add(input, 32), input_size, 0, 32)
       if iszero(res) { revert(0, 0) }
   }
   ```
2. **Migrate or upgrade V1 tokens** to V2 before enabling `error_refund` in production, or ensure the precompile can handle both input formats gracefully.
3. **Document the format dependency** between the ERC-20 contract version and the engine feature flag so future upgrades do not silently break existing deployed tokens.

---

### Proof of Concept

1. Deploy a V1 `EvmErc20` token (already deployed on Aurora mainnet for legacy NEP-141 tokens).
2. Bridge NEP-141 tokens into Aurora, receiving V1 ERC-20 tokens.
3. Call `withdrawToNear(recipient_bytes, amount)` on the V1 token.
4. `_burn(msg.sender, amount)` executes — ERC-20 balance is destroyed.
5. The assembly `call` to the precompile sends `\x01 | amount_b | recipient` (V1 format).
6. With `error_refund` enabled, the precompile parses bytes `[1..21]` as refund address (garbage from amount), bytes `[21..53]` as amount (garbage), and bytes `[53..]` as recipient (empty or truncated).
7. `parse_recipient` fails on the malformed recipient → precompile returns `ExitError`.
8. Assembly `call` returns `0`; `res` is never checked; transaction succeeds.
9. ERC-20 tokens are permanently burned. NEP-141 tokens remain locked in the Aurora engine account. No recovery is possible. [6](#0-5) [7](#0-6) [3](#0-2) [8](#0-7) [4](#0-3)

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

**File:** engine-precompiles/src/native.rs (L295-299)
```rust
fn validate_input_size(input: &[u8], min: usize, max: usize) -> Result<(), ExitError> {
    if input.len() < min || input.len() > max {
        return Err(ExitError::Other(Cow::from("ERR_INVALID_INPUT")));
    }
    Ok(())
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

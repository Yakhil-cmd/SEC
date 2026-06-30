The exploit path is concrete and traceable through the production code. Let me verify each step.

**Step 1 — Entrypoint: `EvmErc20V2.sol::withdrawToNear`**

Any token holder can call this with an empty `recipient` byte array. The function burns first, then calls the precompile, and **never checks the return value `res`**: [1](#0-0) 

```
input = \x01 || sender(20) || amount_b(32) || b""   → 53 bytes total
```

**Step 2 — `parse_input` size check passes**

With `error_refund` feature (which `EvmErc20V2` is designed for — it embeds `sender` as the refund address):

- `MIN_INPUT_SIZE = 21`, `MAX_INPUT_SIZE = 1024`
- 53 bytes passes the check
- Returns `&input[21..]` = `amount_b(32)` (32 bytes, empty recipient stripped) [2](#0-1) [3](#0-2) 

**Step 3 — `parse_recipient(b"")` fails**

For flag `0x1`, after extracting the 32-byte amount, `parse_recipient` is called on the remaining zero bytes: [4](#0-3) 

Inside `parse_recipient`:
- `str::from_utf8(b"")` → `Ok("")` — empty slice is valid UTF-8, **no error here**
- `"".parse::<AccountId>()` → **`Err`** — empty string is not a valid NEAR account ID
- Returns `Err(ExitError::Other("ERR_INVALID_RECEIVER_ACCOUNT_ID"))` [5](#0-4) 

**Step 4 — Error propagates, precompile call returns 0**

The `?` in `run()` propagates the error: [6](#0-5) 

The EVM treats a precompile `Err(ExitError::Other(...))` as a failed inner call — the `call` opcode returns 0. The outer Solidity execution continues because `res` is never checked.

**Step 5 — `error_refund` does NOT save here**

The `error_refund` refund mechanism only triggers when the precompile **successfully creates a NEAR promise** that later fails. Here the precompile fails before any promise is created, so no refund callback is ever scheduled. [7](#0-6) 

---

### Title
Unchecked precompile return value in `withdrawToNear` allows burning ERC-20 tokens with no NEAR-side transfer — (`etc/eth-contracts/contracts/EvmErc20V2.sol`)

### Summary
Any token holder can call `withdrawToNear(b'', amount)` with an empty recipient. The ERC-20 tokens are burned, the `ExitToNear` precompile rejects the empty account ID, the inner `call` silently returns 0, and the NEP-141 tokens remain locked in Aurora's account with no transfer issued.

### Finding Description
`EvmErc20V2::withdrawToNear` burns the caller's tokens unconditionally before invoking the `ExitToNear` precompile. The assembly `call` result is stored in `res` but never inspected. When `recipient` is empty (or any other value that fails `AccountId` parsing), `parse_recipient` returns `Err(ExitError::Other("ERR_INVALID_RECEIVER_ACCOUNT_ID"))`, the precompile call returns 0, and the outer function returns normally. The burn is committed; no NEAR promise is created.

The `error_refund` mechanism does not cover this case because it only handles promise-level failures, not precompile-level parse failures.

### Impact Explanation
The caller's ERC-20 balance is permanently decremented. The corresponding NEP-141 tokens remain in Aurora's account on NEAR with no mechanism for the user to recover them without admin intervention. This constitutes **temporary (potentially permanent) freezing of user funds**.

### Likelihood Explanation
The call requires no privilege — any token holder can trigger it. It can be done accidentally (passing an empty bytes argument) or deliberately as a griefing/self-harm vector. The `IExit` interface accepts `bytes memory recipient` with no on-chain length validation. [8](#0-7) 

### Recommendation
Add a recipient length guard in `withdrawToNear` before burning:

```solidity
require(recipient.length > 0, "ERR_EMPTY_RECIPIENT");
```

Additionally, check the precompile return value and revert on failure:

```solidity
assembly {
    let res := call(...)
    if iszero(res) { revert(0, 0) }
}
```

This ensures the burn is atomic with a successful precompile invocation.

### Proof of Concept

**Unit test (Rust) — `parse_recipient` rejects empty input:**
```rust
#[test]
fn test_parse_recipient_empty_fails() {
    let result = parse_recipient(b"");
    assert!(result.is_err());
}
```

**Integration test (Solidity/NEAR sandbox):**
1. Deploy `EvmErc20V2` with a valid NEP-141 mapping.
2. Mint tokens to `alice`.
3. Call `withdrawToNear(b'', 100)` from `alice`.
4. Assert: EVM transaction status is `Succeed` (no revert).
5. Assert: `alice`'s ERC-20 balance decreased by 100.
6. Assert: No `ft_transfer` promise log emitted.
7. Assert: NEP-141 balance of Aurora's account unchanged (tokens stuck). [1](#0-0) [5](#0-4)

### Citations

**File:** etc/eth-contracts/contracts/EvmErc20V2.sol (L53-63)
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
```

**File:** engine-precompiles/src/native.rs (L36-40)
```rust
#[cfg(not(feature = "error_refund"))]
const MIN_INPUT_SIZE: usize = 3;
#[cfg(feature = "error_refund")]
const MIN_INPUT_SIZE: usize = 21;
const MAX_INPUT_SIZE: usize = 1_024;
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

**File:** engine-precompiles/src/native.rs (L419-419)
```rust
        let exit_to_near_params = ExitToNearParams::try_from(input)?;
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

**File:** engine/src/contract_methods/connector.rs (L214-242)
```rust
        let maybe_result = if let Some(PromiseResult::Successful(_)) = handler.promise_result(0) {
            if let Some(args) = args.transfer_near {
                let action = PromiseAction::Transfer {
                    amount: Yocto::new(args.amount),
                };
                let promise = PromiseBatchAction {
                    target_account_id: args.target_account_id,
                    actions: vec![action],
                };

                // Safety: this call is safe because it comes from the exit to near precompile, not users.
                // The call is to transfer the unwrapped wNEAR tokens.
                let promise_id = handler.promise_create_batch(&promise);
                handler.promise_return(promise_id);
            }

            None
        } else if let Some(args) = args.refund {
            // Exit call failed; need to refund tokens
            let refund_result = engine::refund_on_error(io, env, state, &args, handler)?;

            if !refund_result.status.is_ok() {
                return Err(errors::ERR_REFUND_FAILURE.into());
            }

            Some(refund_result)
        } else {
            None
        };
```

**File:** etc/eth-contracts/contracts/IExit.sol (L5-5)
```text
    function withdrawToNear(bytes memory recipient, uint256 amount) external;
```

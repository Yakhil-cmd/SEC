### Title
Unchecked Precompile Return Value in `withdrawToNear` Causes Permanent ERC-20 Token Loss — (File: `etc/eth-contracts/contracts/EvmErc20V2.sol`)

---

### Summary

`EvmErc20V2.withdrawToNear` burns the caller's ERC-20 tokens **before** calling the `ExitToNear` precompile, and never checks whether that precompile call succeeded. When the precompile returns an error (e.g., invalid NEAR account ID, oversized recipient, amount exceeding `u128::MAX`), the EVM `call` silently returns `0` without reverting the calling context. The tokens are permanently destroyed with no corresponding NEAR transfer and no recovery path.

---

### Finding Description

In `etc/eth-contracts/contracts/EvmErc20V2.sol`, `withdrawToNear` is structured as follows:

```solidity
function withdrawToNear(bytes memory recipient, uint256 amount) external override {
    address sender = _msgSender();
    _burn(sender, amount);                          // ← tokens destroyed here

    bytes32 amount_b = bytes32(amount);
    bytes memory input = abi.encodePacked("\x01", sender, amount_b, recipient);
    uint input_size = 1 + 20 + 32 + recipient.length;

    assembly {
        let res := call(gas(), 0xe9217bc70b7ed1f598ddd3199e80b093fa71124f, 0, add(input, 32), input_size, 0, 32)
        // res is never checked — no revert on failure
    }
}
``` [1](#0-0) 

The `ExitToNear` precompile (`engine-precompiles/src/native.rs`) returns `Err(ExitError)` — causing the EVM `call` to return `0` — in several reachable cases:

1. **Input too large:** `validate_input_size` rejects inputs where `53 + recipient.length > MAX_INPUT_SIZE` (1 024 bytes), i.e., `recipient.length > 971`. [2](#0-1) 

2. **Invalid NEAR account ID:** `parse_recipient` fails with `ERR_INVALID_RECEIVER_ACCOUNT_ID` for any `recipient` bytes that are not valid UTF-8 or do not satisfy NEAR account-ID rules. [3](#0-2) 

3. **Amount overflow:** `parse_amount` rejects any `amount > u128::MAX`. [4](#0-3) 

In all three cases the precompile returns `Err(ExitError)`, the EVM `call` opcode returns `0`, but because `res` is never inspected and no `if iszero(res) { revert(0,0) }` guard exists, `withdrawToNear` returns successfully. The `_burn` that already executed is not rolled back. The ERC-20 tokens are gone with no NEAR-side credit and no refund.

The `error_refund` feature (which sets up a callback to re-mint tokens if the downstream NEAR promise fails) does **not** help here: it only fires when the precompile successfully schedules a NEAR promise that later fails. If the precompile itself errors out before scheduling any promise, no callback is ever registered. [5](#0-4) 

---

### Impact Explanation

**Critical — Permanent freezing/loss of funds.**

Any ERC-20 tokens burned by `_burn` before a failing precompile call are irrecoverably destroyed. There is no on-chain mechanism to re-mint them or recover the NEAR-side balance. The total supply on Aurora decreases while the NEAR-side NEP-141 supply is unchanged, creating a permanent accounting discrepancy and a direct loss of user funds.

---

### Likelihood Explanation

**Medium.** The `recipient` parameter is `bytes memory` with no length or format validation in Solidity. A user who:
- passes a recipient longer than 971 bytes,
- passes bytes that are not a valid NEAR account ID (e.g., uppercase letters, leading/trailing special characters, non-UTF-8 bytes), or
- holds a balance exceeding `u128::MAX` (theoretical but possible with low-decimal tokens)

will silently lose all tokens passed to the function. Wallet UIs and integrations that construct `recipient` programmatically from arbitrary user input are realistic trigger paths.

---

### Recommendation

Check the return value of the precompile `call` and revert on failure:

```solidity
assembly {
    let res := call(gas(), 0xe9217bc70b7ed1f598ddd3199e80b093fa71124f,
                    0, add(input, 32), input_size, 0, 32)
    if iszero(res) { revert(0, 0) }
}
```

This ensures `_burn` is atomically rolled back whenever the precompile rejects the call, matching the behaviour users expect: either the full bridge operation succeeds or nothing changes.

---

### Proof of Concept

1. Deploy `EvmErc20V2` on Aurora (or use an existing bridged token).
2. Mint `1000` tokens to `alice`.
3. `alice` calls:
   ```solidity
   token.withdrawToNear(bytes("INVALID NEAR ACCOUNT!!"), 1000);
   ```
4. `_burn(alice, 1000)` executes; `alice`'s ERC-20 balance drops to `0`.
5. The `ExitToNear` precompile rejects the call with `ERR_INVALID_RECEIVER_ACCOUNT_ID` (the bytes contain spaces and uppercase letters, which are not valid NEAR account-ID characters).
6. The assembly `call` returns `0`; `withdrawToNear` returns without reverting.
7. `alice` has `0` ERC-20 tokens and `0` NEP-141 tokens on NEAR. The `1000` tokens are permanently lost.

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

**File:** engine-precompiles/src/native.rs (L295-299)
```rust
fn validate_input_size(input: &[u8], min: usize, max: usize) -> Result<(), ExitError> {
    if input.len() < min || input.len() > max {
        return Err(ExitError::Other(Cow::from("ERR_INVALID_INPUT")));
    }
    Ok(())
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

**File:** engine-precompiles/src/native.rs (L419-447)
```rust
        let exit_to_near_params = ExitToNearParams::try_from(input)?;

        let (nep141_address, args, exit_event, method, transfer_near_args) =
            match exit_to_near_params {
                // ETH(base) token transfer
                //
                // Input slice format:
                //  recipient_account_id (bytes) - the NEAR recipient account which will receive
                //  NEP-141 (base) tokens, or also can contain the `:unwrap` suffix in case of
                //  withdrawing wNEAR, or another message of JSON in case of OMNI, or address of
                //  receiver in case of transfer tokens to another engine contract.
                ExitToNearParams::BaseToken(ref exit_params) => {
                    let eth_connector_account_id = self.get_eth_connector_contract_account()?;
                    exit_base_token_to_near(eth_connector_account_id, context, exit_params)?
                }
                // ERC-20 token transfer
                //
                // This precompile branch is expected to be called from the ERC-20 burn function.
                //
                // Input slice format:
                //  amount (U256 big-endian bytes) - the amount that was burned
                //  recipient_account_id (bytes) - the NEAR recipient account which will receive
                //  NEP-141 tokens, or also can contain the `:unwrap` suffix in case of withdrawing
                //  wNEAR, or another message of JSON in case of OMNI, or address of receiver in case
                //  of transfer tokens to another engine contract.
                ExitToNearParams::Erc20TokenParams(ref exit_params) => {
                    exit_erc20_token_to_near(context, exit_params, &self.io)?
                }
            };
```

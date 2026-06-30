### Title
Silent Token Burn Without NEP-141 Transfer in `EvmErc20V2.withdrawToNear` Due to Precompile Input Format Mismatch — (`etc/eth-contracts/contracts/EvmErc20V2.sol`)

---

### Summary

`EvmErc20V2.sol`'s `withdrawToNear` encodes the caller's `sender` address into the precompile input, producing a byte layout that the `ExitToNear` precompile cannot parse correctly when the `error_refund` feature is disabled. The precompile call silently fails (the inline `assembly` block does not check the return value), but the preceding `_burn` is not reverted. The result is that ERC-20 tokens are permanently destroyed on the EVM side with no corresponding NEP-141 `ft_transfer` issued on the NEAR side.

---

### Finding Description

**`EvmErc20.sol` (V1) `withdrawToNear` input layout** (what the precompile expects without `error_refund`):

```
[0x01][amount (32 bytes)][recipient (variable)]
``` [1](#0-0) 

**`EvmErc20V2.sol` (V2) `withdrawToNear` input layout** (what is actually sent):

```
[0x01][sender (20 bytes)][amount (32 bytes)][recipient (variable)]
``` [2](#0-1) 

The `ExitToNear` precompile's `TryFrom<&[u8]>` for `ExitToNearParams`, when the `error_refund` feature is **disabled**, strips only the 1-byte flag and then immediately reads `input[..32]` as the amount:

```rust
0x1 => {
    let amount = parse_amount(&input[..32])?;
    let Recipient { receiver_account_id, message } = parse_recipient(&input[32..])?;
``` [3](#0-2) 

When V2 calls the precompile, `input[..32]` is `[sender_address (20 bytes) ‖ first_12_bytes_of_amount]`. Interpreted as a big-endian U256, the high 160 bits are the sender address, making the value far exceed `u128::MAX`. `parse_amount` enforces this bound:

```rust
if amount > U256::from(u128::MAX) {
    return Err(ExitError::Other(Cow::from("ERR_INVALID_AMOUNT")));
}
``` [4](#0-3) 

The precompile returns `ExitError`, the EVM `call` opcode returns `0`, but `EvmErc20V2.sol` never inspects `res`:

```solidity
assembly {
    let res := call(gas(), 0xe9217bc70b7ed1f598ddd3199e80b093fa71124f, 0, add(input, 32), input_size, 0, 32)
}
``` [5](#0-4) 

The `_burn` executed before the assembly block is **not** rolled back, so the user's ERC-20 balance is permanently reduced with no NEP-141 transfer ever scheduled.

**Why the `error_refund` path would save it**: when that feature is compiled in, `parse_input` extracts bytes `[1..21]` as a `refund_address` before handing the remainder to the flag dispatcher, which then correctly sees `[amount (32 bytes)][recipient]`. V2 was written for that code path. Without it, the layouts are incompatible. [6](#0-5) 

---

### Impact Explanation

Any user who calls `EvmErc20V2.withdrawToNear` on a deployment where the engine is compiled **without** the `error_refund` feature loses their tokens permanently: they are burned on the EVM side, and no `ft_transfer` promise is ever created on the NEAR side. This is a **permanent freeze / destruction of user funds**.

---

### Likelihood Explanation

`EvmErc20V2.sol` is a production contract in `etc/eth-contracts/contracts/`. Any bridged NEP-141 token whose EVM mirror is deployed from the V2 template exposes every holder to this loss the moment they call `withdrawToNear`. The call path is fully permissionless — any token holder can trigger it. The only precondition is that the engine binary was compiled without `error_refund`, which is the default (`#[cfg(not(feature = "error_refund"))]` guards the safe path). [7](#0-6) 

---

### Recommendation

1. **Guard deployment**: prevent `EvmErc20V2.sol` from being used as the ERC-20 mirror template unless the engine is compiled with `error_refund`.
2. **Check the return value**: add a `require(res != 0, "precompile call failed")` after the assembly block in both `withdrawToNear` and `withdrawToEthereum` in all ERC-20 mirror contracts so that a failed precompile call reverts the burn.
3. **Unify input formats**: align V2's `withdrawToNear` input layout with V1 when `error_refund` is not in use, or add a compile-time or runtime guard that prevents the mismatch.

---

### Proof of Concept

1. Engine is compiled **without** the `error_refund` feature (default build).
2. A NEP-141 token is bridged; its EVM mirror is deployed from `EvmErc20V2.sol`.
3. Alice holds 1000 units of the ERC-20 mirror token.
4. Alice calls `withdrawToNear("alice.near", 1000)`.
5. `_burn(alice, 1000)` executes — Alice's balance drops to 0.
6. The contract calls the `ExitToNear` precompile at `0xe9217bc7...` with payload `[0x01][alice_addr (20 B)][1000 as bytes32 (32 B)]["alice.near"]`.
7. The precompile reads `input[..32]` = `[alice_addr ‖ 0x00…03E8]`, interprets it as a U256 ≫ `u128::MAX`, and returns `ERR_INVALID_AMOUNT`.
8. The `call` opcode returns `0`; `res` is discarded; `withdrawToNear` returns normally.
9. No NEAR promise is scheduled; Alice's NEP-141 balance is never credited.
10. Alice has permanently lost 1000 tokens. [2](#0-1) [3](#0-2) [4](#0-3)

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

**File:** engine-precompiles/src/native.rs (L758-775)
```rust
            0x1 => {
                let amount = parse_amount(&input[..32])?;
                let Recipient {
                    receiver_account_id,
                    message,
                } = parse_recipient(&input[32..])?;

                Ok(Self::Erc20TokenParams(Erc20TokenParams {
                    #[cfg(feature = "error_refund")]
                    refund_address,
                    receiver_account_id,
                    amount,
                    message,
                }))
            }
            _ => Err(ExitError::Other(Cow::from("ERR_INVALID_FLAG"))),
        }
    }
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

### Title
Unchecked Exit-Precompile Call Return Value in `withdrawToNear`/`withdrawToEthereum` Causes Permanent Fund Freeze — (`File: etc/eth-contracts/contracts/EvmErc20.sol`, `etc/eth-contracts/contracts/EvmErc20V2.sol`)

---

### Summary

`EvmErc20` and `EvmErc20V2` burn the caller's ERC-20 tokens **before** calling the Aurora exit precompile, and never check the `call` return value. If the precompile call fails for any reason — including a user-supplied invalid NEAR recipient, a paused precompile, or any other reachable error — the burn is irreversible and the corresponding NEP-141 tokens are never released. The user's funds are permanently destroyed.

---

### Finding Description

Both `withdrawToNear` and `withdrawToEthereum` in `EvmErc20.sol` and `EvmErc20V2.sol` follow the same pattern:

1. `_burn(_msgSender(), amount)` — tokens are destroyed from the caller's ERC-20 balance.
2. An inline-assembly `call` is made to the exit precompile address.
3. The return value `res` is **assigned but never inspected**.

`EvmErc20.sol` `withdrawToNear`: [1](#0-0) 

`EvmErc20V2.sol` `withdrawToNear`: [2](#0-1) 

`EvmErc20.sol` `withdrawToEthereum`: [3](#0-2) 

In the EVM, a failed `call` returns `0` in `res` but does **not** revert the calling frame. Because `res` is never checked and no `require(res == 1)` guard exists, the function returns successfully even when the precompile rejects the call.

The `ExitToNear` precompile (`engine-precompiles/src/native.rs`) returns `ExitError` — which translates to a `call` return value of `0` — in multiple reachable conditions:

- **`ERR_PAUSED`**: when the precompile is paused via `pause_precompiles`, the precompile set returns `PrecompileFailure::Fatal` immediately. [4](#0-3) 

- **Input parsing failure**: `ExitToNearParams::try_from(input)` fails if the recipient bytes do not form a valid NEAR `AccountId` (e.g., too long, illegal characters, empty). [5](#0-4) 

- **`ERR_TARGET_TOKEN_NOT_FOUND`**: if the ERC-20 → NEP-141 mapping is absent in storage. [6](#0-5) 

- **`ERR_ETH_ATTACHED_FOR_ERC20_EXIT`**: non-zero `apparent_value` on an ERC-20 exit. [7](#0-6) 

In every case the `_burn` has already committed, so the ERC-20 supply is reduced but no NEP-141 `ft_transfer` promise is ever scheduled.

---

### Impact Explanation

**Critical — Permanent freezing of funds.**

When the precompile call fails silently:
- The caller's ERC-20 tokens are burned and gone from Aurora's EVM state.
- No `ft_transfer` / `ft_transfer_call` NEAR promise is created.
- The NEP-141 tokens remain locked in the Aurora engine contract with no mechanism to recover them.
- The user has permanently lost the bridged value with no recourse.

This affects every holder of any NEP-141-backed ERC-20 token deployed via `EvmErc20` or `EvmErc20V2`.

---

### Likelihood Explanation

**High.** There are multiple independently reachable trigger paths:

1. **User-triggered (no admin required):** A user calls `withdrawToNear` with a `recipient` bytes value that is not a valid NEAR account ID (e.g., a 65-byte string, a string with uppercase letters, or an empty byte array). The precompile's `parse_recipient` / `AccountId::try_from` rejects it, the `call` returns `0`, and the burn is final. [8](#0-7) 

2. **Precompile-paused path:** When `EXIT_TO_NEAR` or `EXIT_TO_ETHEREUM` is paused by an authorized account, every subsequent `withdrawToNear` / `withdrawToEthereum` call burns tokens and silently discards the exit. Users have no on-chain signal that the precompile is paused before their tokens are destroyed. [9](#0-8) 

---

### Recommendation

**Short term:** Add a `require` check on the assembly `call` return value in both `withdrawToNear` and `withdrawToEthereum` in `EvmErc20.sol` and `EvmErc20V2.sol`:

```solidity
assembly {
    let res := call(gas(), EXIT_PRECOMPILE_ADDRESS, 0, add(input, 32), input_size, 0, 32)
    if iszero(res) { revert(0, 0) }
}
```

This ensures the burn is atomically reverted if the precompile rejects the call, preserving the user's balance.

**Long term:** Restructure the exit flow so that the precompile call is validated **before** the burn, or use a try/catch pattern that mints back the tokens on failure. Consider emitting a revert-safe error message so users can diagnose the failure reason.

---

### Proof of Concept

1. Deploy `EvmErc20` for a registered NEP-141 token.
2. Mint 1000 tokens to `alice`.
3. Alice calls `withdrawToNear(bytes("INVALID ACCOUNT ID WITH SPACES"), 1000)`.
4. `_burn(alice, 1000)` executes — Alice's balance drops to 0.
5. The precompile call fails (`AccountId::try_from` rejects the recipient) — `res = 0`.
6. `res` is never checked; the function returns `success`.
7. Alice's 1000 ERC-20 tokens are permanently destroyed. No NEP-141 `ft_transfer` promise was created. The NEP-141 balance of the Aurora engine contract is unchanged — the tokens are frozen forever. [1](#0-0) [2](#0-1) [10](#0-9)

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

**File:** engine-precompiles/src/lib.rs (L140-144)
```rust
        if self.is_paused(&address) {
            return Some(Err(PrecompileFailure::Fatal {
                exit_status: ExitFatal::Other(prelude::Cow::Borrowed("ERR_PAUSED")),
            }));
        }
```

**File:** engine-precompiles/src/native.rs (L302-309)
```rust
fn get_nep141_from_erc20<I: IO>(erc20_token: &[u8], io: &I) -> Result<AccountId, ExitError> {
    AccountId::try_from(
        io.read_storage(bytes_to_key(KeyPrefix::Erc20Nep141Map, erc20_token).as_slice())
            .map(|s| s.to_vec())
            .ok_or(ExitError::Other(Cow::Borrowed(ERR_TARGET_TOKEN_NOT_FOUND)))?,
    )
    .map_err(|_| ExitError::Other(Cow::Borrowed("ERR_INVALID_NEP141_ACCOUNT")))
}
```

**File:** engine-precompiles/src/native.rs (L412-419)
```rust
        // It's not allowed to call exit precompiles in static mode
        if is_static {
            return Err(ExitError::Other(Cow::from("ERR_INVALID_IN_STATIC")));
        } else if context.address != exit_to_near::ADDRESS.raw() {
            return Err(ExitError::Other(Cow::from("ERR_INVALID_IN_DELEGATE")));
        }

        let exit_to_near_params = ExitToNearParams::try_from(input)?;
```

**File:** engine-precompiles/src/native.rs (L576-580)
```rust
    if context.apparent_value != U256::zero() {
        return Err(ExitError::Other(Cow::from(
            "ERR_ETH_ATTACHED_FOR_ERC20_EXIT",
        )));
    }
```

**File:** engine-precompiles/src/native.rs (L727-775)
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

        match flag {
            0x0 => {
                let Recipient {
                    receiver_account_id,
                    message,
                } = parse_recipient(input)?;

                Ok(Self::BaseToken(BaseTokenParams {
                    #[cfg(feature = "error_refund")]
                    refund_address,
                    receiver_account_id,
                    message,
                }))
            }
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

**File:** engine/src/pausables.rs (L13-16)
```rust
    pub struct PrecompileFlags: u32 {
        const EXIT_TO_NEAR        = 0b01;
        const EXIT_TO_ETHEREUM    = 0b10;
    }
```

### Title
V1 `EvmErc20.withdrawToNear` Sends Refund to Garbled Address When `error_refund` Is Enabled — (`etc/eth-contracts/contracts/EvmErc20.sol`, `engine-precompiles/src/native.rs`)

---

### Summary

When the Aurora engine is compiled with the `error_refund` feature, the `ExitToNear` precompile expects bytes `[1..21]` of the call input to be the sender's Ethereum address (the refund target). `EvmErc20V2` correctly encodes this. `EvmErc20` (V1) does not — it omits the sender field entirely. When a V1 contract calls the precompile and the downstream `ft_transfer` fails, the refund is minted to an address derived from the first 20 bytes of the token amount, not the original sender. For typical amounts those bytes are all zeros, so the refund is minted to `address(0)` and the user's tokens are permanently destroyed.

---

### Finding Description

**V1 input layout** (`EvmErc20.withdrawToNear`, line 57):
```
[flag 0x01 (1 byte)] [amount_b (32 bytes)] [recipient (variable)]
``` [1](#0-0) 

**V2 input layout** (`EvmErc20V2.withdrawToNear`, line 58):
```
[flag 0x01 (1 byte)] [sender (20 bytes)] [amount_b (32 bytes)] [recipient (variable)]
``` [2](#0-1) 

**Precompile parser with `error_refund` enabled** (`native.rs`, lines 779–784):
```rust
fn parse_input(input: &[u8]) -> Result<(Address, &[u8]), ExitError> {
    validate_input_size(input, MIN_INPUT_SIZE, MAX_INPUT_SIZE)?;
    let mut buffer = [0; 20];
    buffer.copy_from_slice(&input[1..21]);   // ← always reads bytes [1..21] as refund_address
    let refund_address = Address::from_array(buffer);
    Ok((refund_address, &input[21..]))
}
``` [3](#0-2) 

When V1 input arrives with `error_refund` enabled:

| Field parsed | Bytes consumed | Actual content |
|---|---|---|
| `refund_address` | `[1..21]` | First 20 bytes of `amount_b` (big-endian) |
| `amount` | `[21..53]` | Last 12 bytes of `amount_b` + first 20 bytes of `recipient` |
| `receiver_account_id` | `[53..]` | Tail of `recipient` after byte 20 |

For any amount ≤ 2^96 (all practical token amounts), `input[1..21]` is `0x0000…0000` — i.e., `address(0)`. [4](#0-3) 

The garbled `receiver_account_id` and `amount` cause the `ft_transfer` promise to fail. The callback then calls `refund_call_args`, which reads `params.refund_address` — the garbled value — and mints the refund tokens to it. [5](#0-4) 

The burn in the Solidity contract executes unconditionally before the precompile call, so the sender's balance is already zero at the point of failure. [6](#0-5) 

---

### Impact Explanation

Tokens are burned from the sender, the `ft_transfer` fails due to garbled parameters, and the refund is minted to `address(0)` (or another uncontrolled address). The tokens are permanently destroyed. This breaks the invariant that a failed exit refunds the original sender, constituting **insolvency**: the ERC-20 total supply decreases without a corresponding decrease in the NEP-141 balance held by Aurora, or vice versa.

---

### Likelihood Explanation

- `EvmErc20` (V1) is still a deployable, production contract in the repository with no deprecation guard or deployment block.
- The `error_refund` feature is the motivation for V2's existence; it is intended for production use.
- Any `ft_transfer` failure (e.g., recipient not registered with the NEP-141, storage deposit exhausted) triggers the refund path.
- No attacker capability is required beyond calling `withdrawToNear` on a V1 contract with any recipient that causes `ft_transfer` to fail — a condition that can arise organically.

---

### Recommendation

1. **Remove or tombstone `EvmErc20` (V1)** from the deployable contract set, or add a compile-time/runtime guard that prevents V1 deployment when `error_refund` is active.
2. **Add a version byte or magic prefix** to the precompile input so the parser can detect and reject V1-format payloads when `error_refund` is enabled.
3. **Alternatively**, derive `refund_address` from `context.caller` inside the precompile itself (the ERC-20 contract address is the caller; the actual sender can be recovered from `context` or passed via a separate mechanism), removing the dependency on the Solidity contract to supply it correctly.

---

### Proof of Concept

```solidity
// 1. Deploy EvmErc20 (V1) — no sender field in precompile input.
// 2. Mint tokens to alice.
// 3. Alice calls withdrawToNear("unregistered.near", amount).
//    - V1 encodes: [0x01][amount_b(32)]["unregistered.near"]
//    - Precompile (error_refund ON) reads input[1..21] = first 20 bytes of amount_b
//      → refund_address = 0x0000000000000000000000000000000000000000
//    - ft_transfer fails (unregistered recipient).
//    - Refund minted to address(0).
// 4. Assert: alice's ERC-20 balance == 0 (burned, not refunded).
// 5. Assert: address(0) ERC-20 balance == amount  (or tokens simply vanish if address(0) is untracked).
// 6. Invariant broken: NEP-141 balance on Aurora unchanged, ERC-20 supply decreased.
``` [6](#0-5) [7](#0-6) [8](#0-7)

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

**File:** etc/eth-contracts/contracts/EvmErc20V2.sol (L53-59)
```text
    function withdrawToNear(bytes memory recipient, uint256 amount) external override {
        address sender = _msgSender();
        _burn(sender, amount);

        bytes32 amount_b = bytes32(amount);
        bytes memory input = abi.encodePacked("\x01", sender, amount_b, recipient);
        uint input_size = 1 + 20 + 32 + recipient.length;
```

**File:** engine-precompiles/src/native.rs (L700-724)
```rust
#[allow(clippy::unnecessary_wraps)]
fn refund_call_args(
    params: &ExitToNearParams,
    event: &events::ExitToNear,
) -> Option<RefundCallArgs> {
    Some(RefundCallArgs {
        recipient_address: match params {
            ExitToNearParams::BaseToken(params) => params.refund_address,
            ExitToNearParams::Erc20TokenParams(params) => params.refund_address,
        },
        erc20_address: match params {
            ExitToNearParams::BaseToken(_) => None,
            ExitToNearParams::Erc20TokenParams(_) => {
                let erc20_address = match event {
                    events::ExitToNear::Legacy(legacy) => legacy.erc20_address,
                    events::ExitToNear::Omni(omni) => omni.erc20_address,
                };
                Some(erc20_address)
            }
        },
        amount: types::u256_to_arr(&match event {
            events::ExitToNear::Legacy(legacy) => legacy.amount,
            events::ExitToNear::Omni(omni) => omni.amount,
        }),
    })
```

**File:** engine-precompiles/src/native.rs (L739-742)
```rust
        #[cfg(feature = "error_refund")]
        let (refund_address, input) = parse_input(input)?;
        #[cfg(not(feature = "error_refund"))]
        let input = parse_input(input)?;
```

**File:** engine-precompiles/src/native.rs (L758-771)
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

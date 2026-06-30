Let me investigate the relevant code to verify this exploit claim.

Now I have all the information needed. Let me verify the key interaction between the Solidity contract and the precompile's error handling.

### Title
Unchecked Precompile Return Value in `withdrawToNear` Burns ERC-20 Tokens Without Creating NEAR-Side Exit Promise — (`etc/eth-contracts/contracts/EvmErc20.sol`)

---

### Summary

`withdrawToNear` in `EvmErc20.sol` burns the caller's tokens unconditionally before invoking the ExitToNear precompile via a raw assembly `call`. The assembly block captures the return value in `res` but never checks it. When `amount > u128::MAX`, the precompile's `parse_amount` guard in `engine-precompiles/src/native.rs` returns `ERR_INVALID_AMOUNT`, causing the inner EVM `call` to return `0`. Because `EvmErc20` never inspects `res`, the outer transaction succeeds: tokens are burned on the EVM side with no corresponding NEP-141 `ft_transfer` promise scheduled on NEAR.

---

### Finding Description

**Step 1 — Burn before precompile call.**
`withdrawToNear` calls `_burn` at line 54 before the precompile is invoked. There is no guard on `amount` at the Solidity level. [1](#0-0) 

**Step 2 — Unchecked assembly call.**
The assembly block at lines 60–62 stores the `call` result in `res` but never branches on it. No `if iszero(res) { revert(0,0) }` follows. [2](#0-1) 

**Step 3 — Precompile rejects amounts exceeding `u128::MAX`.**
`parse_amount` in `native.rs` enforces a hard ceiling: any `U256` value above `u128::MAX` returns `Err(ExitError::Other("ERR_INVALID_AMOUNT"))`. [3](#0-2) 

**Step 4 — Error propagated immediately via `?` in the ERC-20 branch.**
In `TryFrom<&'a [u8]> for ExitToNearParams`, the ERC-20 path (flag `0x1`) calls `parse_amount` with `?`, so the precompile returns an error before any promise is constructed. [4](#0-3) 

**Step 5 — No refund path in `EvmErc20`.**
The Solidity contract has no `try/catch`, no balance restoration, and no second attempt. Once `_burn` succeeds, the tokens are gone regardless of what the precompile does. [5](#0-4) 

---

### Impact Explanation

The ERC-20 tokens are permanently destroyed on the EVM side. The corresponding NEP-141 tokens remain locked inside Aurora's connector with no reachable claim path for the affected user. This constitutes permanent loss of user funds. The question labels it "temporary freezing" because the NEP-141 balance technically still exists in Aurora's custody, but from the user's perspective the ERC-20 tokens are irrecoverably burned with no refund mechanism.

---

### Likelihood Explanation

The precondition — a user holding a balance exceeding `u128::MAX` — requires the admin to have minted that quantity. `u128::MAX ≈ 3.4 × 10^38`, which is astronomically large for any token with standard decimals. In practice this is an extremely unlikely but non-zero scenario (e.g., a token with 0 decimals, or a deliberate large-supply design). The attack requires no privilege beyond holding the balance; any EVM user satisfying the precondition can trigger it unintentionally or deliberately.

---

### Recommendation

1. **Check the precompile return value in Solidity.** Replace the unchecked assembly with a revert on failure:
   ```solidity
   assembly {
       let res := call(gas(), 0xe9217bc70b7ed1f598ddd3199e80b093fa71124f, 0, add(input, 32), input_size, 0, 32)
       if iszero(res) { revert(0, 0) }
   }
   ```
   This ensures `_burn` is atomically rolled back if the precompile rejects the call.

2. **Add an explicit `amount <= type(uint128).max` guard in `withdrawToNear`** before `_burn` to fail fast with a clear error message.

3. Apply the same fix to `withdrawToEthereum`, which has the identical unchecked assembly pattern. [6](#0-5) 

---

### Proof of Concept

```rust
// Rust integration test outline (local Aurora sandbox)
// 1. Deploy EvmErc20 with admin key.
// 2. Admin mints (u128::MAX + 1) tokens to `attacker` address.
// 3. attacker calls withdrawToNear(b"alice.near", u128::MAX + 1).
// 4. Assert: attacker ERC-20 balance == 0  (tokens burned).
// 5. Assert: no ExitToNear event log emitted by the precompile
//            (promise_log and exit_event_log are absent from tx receipts).
// 6. Assert: alice.near NEP-141 balance unchanged (no ft_transfer executed).
// Expected: steps 4–6 all pass, confirming tokens destroyed with no NEAR credit.
```

The root cause is the missing return-value check at `EvmErc20.sol` line 61, combined with the `parse_amount` ceiling at `native.rs` line 340. [7](#0-6) [8](#0-7)

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

**File:** engine-precompiles/src/native.rs (L758-759)
```rust
            0x1 => {
                let amount = parse_amount(&input[..32])?;
```

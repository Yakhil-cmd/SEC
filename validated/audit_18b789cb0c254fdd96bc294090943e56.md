### Title
Missing Zero-Address Validation for Ethereum Recipient in `ExitToEthereum` Precompile — (File: `engine-precompiles/src/native.rs`)

---

### Summary

The `ExitToEthereum` precompile does not validate that the caller-supplied Ethereum recipient address is non-zero. Any unprivileged EVM user can trigger a withdrawal that directs bridged ETH or ERC-20 tokens to `address(0)` on Ethereum, permanently destroying the funds.

---

### Finding Description

In `ExitToEthereum::run()`, both the ETH base-token path (flag `0x0`) and the ERC-20 path (flag `0x1`) parse a 20-byte Ethereum recipient address directly from user-controlled calldata and pass it into the withdrawal promise without any non-zero check.

**ETH base-token path (flag `0x0`):**

```rust
let recipient_address: Address = input
    .try_into()
    .map_err(|_| ExitError::Other(Cow::from("ERR_INVALID_RECIPIENT_ADDRESS")))?;
// ← no check: recipient_address != Address::zero()
``` [1](#0-0) 

The parsed `recipient_address` is then forwarded verbatim into the serialized withdrawal args sent to the eth-connector contract:

```rust
serialize_fn(recipient_address, context.apparent_value)?
``` [2](#0-1) 

**ERC-20 path (flag `0x1`):**

```rust
let recipient_address = Address::try_from_slice(input)
    .map_err(|_| ExitError::Other(Cow::from("ERR_WRONG_ADDRESS")))?;
// ← no check: recipient_address != Address::zero()
``` [3](#0-2) 

In both cases the precompile constructs a `PromiseCreateArgs` targeting the eth-connector's `withdraw` method with the zero address as the recipient, then returns successfully:

```rust
let withdraw_promise = PromiseCreateArgs {
    target_account_id: nep141_address,
    method: "withdraw".to_string(),
    args: serialized_args,
    ...
};
``` [4](#0-3) 

The tokens are burned on Aurora at the point the precompile executes. The NEAR-side `withdraw` call then instructs the eth-connector to release the corresponding amount to `0x0000000000000000000000000000000000000000` on Ethereum. Those funds are irrecoverable.

The analogous missing check in the Connext report was `recovery != 0`: if the recovery address is zero, `sendToRecovery()` sends funds to the zero address. Here, if `recipient_address` is zero, `withdraw` sends funds to the zero address on Ethereum. The root cause and impact class are identical.

---

### Impact Explanation

**Critical — Permanent freezing/destruction of funds.**

When a user calls `ExitToEthereum` with a zero recipient:

1. Their ETH (or ERC-20 mirror tokens) are burned inside the Aurora EVM — the balance is debited immediately and irreversibly.
2. The NEAR promise instructs the eth-connector to release the equivalent amount to `address(0)` on Ethereum.
3. No recovery path exists: the Aurora balance is gone, and the Ethereum-side funds are sent to an address no one controls.

---

### Likelihood Explanation

**Medium.** The precompile is reachable by any EVM user without privilege. A user can accidentally pass a zero recipient (e.g., from a buggy frontend or contract integration), or a malicious contract can deliberately trigger this to destroy another user's tokens after obtaining their approval. The input format is simple (1 flag byte + 20 address bytes), making accidental zero-address submission realistic.

---

### Recommendation

Add an explicit non-zero check for `recipient_address` in both the ETH and ERC-20 branches of `ExitToEthereum::run()`, immediately after parsing:

```rust
if recipient_address == Address::zero() {
    return Err(ExitError::Other(Cow::from("ERR_ZERO_RECIPIENT_ADDRESS")));
}
```

This should be inserted at:
- `engine-precompiles/src/native.rs` line ~897 (ETH path, after `recipient_address` is parsed)
- `engine-precompiles/src/native.rs` line ~947 (ERC-20 path, after `recipient_address` is parsed)

---

### Proof of Concept

**Attacker-controlled entry path:**

1. Any EVM account sends a transaction to the `ExitToEthereum` precompile address `0xb0bd02f6a392af548bdf1cfaee5dfa0eefcc8eab` with ETH value and input:
   ```
   0x00                                         // flag: ETH base token
   0000000000000000000000000000000000000000     // recipient: address(0)
   ``` [5](#0-4) 

2. `ExitToEthereum::run()` parses `recipient_address = Address::zero()`. No validation rejects it. [6](#0-5) 

3. The precompile serializes a `withdraw` call with `recipient = 0x000...000` and emits it as a NEAR promise log. The EVM deducts the ETH from the caller's balance. [7](#0-6) 

4. The NEAR runtime executes the promise, calling `withdraw` on the eth-connector with the zero Ethereum address. The eth-connector releases the funds to `address(0)` on Ethereum.

5. The caller's ETH is permanently destroyed. No refund or recovery mechanism exists.

### Citations

**File:** engine-precompiles/src/native.rs (L857-864)
```rust
        // ETH (Base token) transfer input format (min size 21 bytes)
        //  - flag (1 byte)
        //  - eth_recipient (20 bytes)
        // ERC-20 transfer input format: max 53 bytes
        //  - flag (1 byte)
        //  - amount (32 bytes)
        //  - eth_recipient (20 bytes)
        validate_input_size(input, 21, 53)?;
```

**File:** engine-precompiles/src/native.rs (L894-897)
```rust
                let recipient_address: Address = input
                    .try_into()
                    .map_err(|_| ExitError::Other(Cow::from("ERR_INVALID_RECIPIENT_ADDRESS")))?;
                let serialize_fn = match get_withdraw_serialize_type(&self.io)? {
```

**File:** engine-precompiles/src/native.rs (L907-907)
```rust
                    serialize_fn(recipient_address, context.apparent_value)?,
```

**File:** engine-precompiles/src/native.rs (L946-947)
```rust
                    let recipient_address = Address::try_from_slice(input)
                        .map_err(|_| ExitError::Other(Cow::from("ERR_WRONG_ADDRESS")))?;
```

**File:** engine-precompiles/src/native.rs (L977-990)
```rust
        let withdraw_promise = PromiseCreateArgs {
            target_account_id: nep141_address,
            method: "withdraw".to_string(),
            args: serialized_args,
            attached_balance: Yocto::new(1),
            attached_gas: costs::WITHDRAWAL_GAS,
        };

        let promise = borsh::to_vec(&PromiseArgs::Create(withdraw_promise)).unwrap();
        let promise_log = Log {
            address: exit_to_ethereum::ADDRESS.raw(),
            topics: Vec::new(),
            data: promise,
        };
```

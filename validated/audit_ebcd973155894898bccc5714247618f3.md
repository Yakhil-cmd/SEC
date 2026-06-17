### Title
Missing Address Validation for `reserved[1]` (Refund Recipient) Causes Panic and Chain Halt in L1 Transaction Processing - (`basic_bootloader/src/bootloader/transaction/abi_encoded/mod.rs`, `basic_bootloader/src/bootloader/transaction_flow/zk/process_l1_transaction.rs`)

---

### Summary

The ZKsync OS bootloader processes L1→L2 priority transactions without validating that the `reserved[1]` field (the refund recipient address) is a valid 160-bit address. The structural parser explicitly skips this check with a `// TODO: validate address?` comment. Later, during refund disbursement, the code calls `u256_to_b160_checked()` which uses a hard `assert!` (panics on failure) rather than a graceful error path. If an L1 transaction carries a `reserved[1]` value with high bits set (i.e., not representable as a 20-byte address), the bootloader panics and halts block processing entirely.

---

### Finding Description

**Weakness 1 — Missing address validation in `validate_structure`**

`AbiEncodedTransaction::validate_structure()` explicitly skips address validation for `reserved[1]`:

```rust
// reserved[1] = refund recipient for l1 to l2 and upgrade txs
match tx_type {
    Self::L1_L2_TX_TYPE | Self::UPGRADE_TX_TYPE => {
        // TODO: validate address?
    }
    _ => unreachable!(),
}
```

The field is parsed as a raw `U256` and accepted without any constraint that it fits within 160 bits. [1](#0-0) 

**Weakness 2 — Panicking assertion in `u256_to_b160_checked`**

The utility function used to convert `reserved[1]` to an address uses `assert!`, which panics (aborts the process) rather than returning a `Result`:

```rust
pub fn u256_to_b160_checked(src: U256) -> B160 {
    assert!(src.as_limbs()[3] == 0 && src.as_limbs()[2] < (1u64 << 32));
    ...
}
``` [2](#0-1) 

**Weakness 3 — Unconditional call without prior validation**

In `process_l1_transaction`, the refund path calls `u256_to_b160_checked` directly on the unvalidated `reserved[1]` value whenever a refund is owed:

```rust
if to_refund_recipient > U256::ZERO {
    let refund_recipient = u256_to_b160_checked(transaction.reserved[1].read());
    mint_base_token::<S, Config>(..., &to_refund_recipient, &refund_recipient, ...)?;
}
``` [3](#0-2) 

A refund is owed whenever `gas_limit > gas_used` (the common case for any transaction that does not exhaust all gas) or when the transaction body reverts (in which case `total_deposited - pay_to_operator` is refunded). [4](#0-3) 

---

### Impact Explanation

When an L1 priority transaction with `reserved[1]` containing a value whose upper 96 bits are non-zero is processed by the bootloader, and any refund amount is non-zero (the common case), the `assert!` in `u256_to_b160_checked` fires. In Rust, `assert!` panics unconditionally in both debug and release builds. Running as a RISC-V binary for proving, a panic aborts the program, halting block production. Because L1 transactions "cannot be invalidated" (the code explicitly states this to avoid halting the priority queue), the sequencer has no recovery path short of a protocol-level intervention.

This is a **chain-halting** vulnerability: a single malformed L1 transaction can permanently stall block processing.

---

### Likelihood Explanation

The L1 `Mailbox` contract accepts a `_refundRecipient` parameter typed as Solidity `address` (20 bytes), which normally prevents high-bit values. However:

1. The ZKsync OS code itself has no defense — the `// TODO: validate address?` comment is an explicit acknowledgment of the gap.
2. Any future upgrade to L1 contracts that relaxes this constraint, or any alternative transaction submission path, would immediately expose the panic.
3. The `u256_to_b160_checked` function is used in a post-execution, post-revert path that runs on `FORMAL_INFINITE` resources and is expected to be infallible — making the panic especially dangerous since there is no surrounding error handler.

---

### Recommendation

1. **In `validate_structure`**: Replace the `// TODO: validate address?` stub with an actual check that `reserved[1]` fits in 160 bits for `L1_L2_TX_TYPE` and `UPGRADE_TX_TYPE`:
   ```rust
   Self::L1_L2_TX_TYPE | Self::UPGRADE_TX_TYPE => {
       let r1 = self.reserved[1].read();
       if r1.as_limbs()[3] != 0 || r1.as_limbs()[2] >= (1u64 << 32) {
           return Err(());
       }
   }
   ```

2. **Replace `u256_to_b160_checked` with `u256_try_to_b160`** at the call site in `process_l1_transaction`, propagating an internal error rather than panicking:
   ```rust
   let refund_recipient = u256_try_to_b160(transaction.reserved[1].read())
       .ok_or(internal_error!("reserved[1] is not a valid address"))?;
   ```
   The safe variant `u256_try_to_b160` already exists in the same file. [5](#0-4) 

---

### Proof of Concept

**Entry path:**

1. Craft an L1 priority transaction (`tx_type = 0x7f`) with `reserved[1]` set to `U256::MAX` (all bits set — clearly not a valid address).
2. Set `gas_limit` to any non-zero value and `gas_price` to any non-zero value so that `to_refund_recipient > 0` after execution.
3. Submit the transaction to the L1 priority queue (or inject it directly into the bootloader input in a test/simulation context).
4. The bootloader calls `try_from_buffer` → `validate_structure` → passes (no address check for `reserved[1]`).
5. `process_l1_transaction` executes the transaction body, computes `to_refund_recipient > 0`.
6. `u256_to_b160_checked(U256::MAX)` is called: `assert!(0 == 0 && u64::MAX < (1u64 << 32))` → `assert!(false)` → **panic**.
7. Block processing halts.

**Relevant code path summary:**

```
try_from_buffer (no address check for reserved[1])
  └─ validate_structure (// TODO: validate address? — skipped)
       └─ process_l1_transaction
            └─ to_refund_recipient > 0
                 └─ u256_to_b160_checked(reserved[1])  ← assert! PANICS
``` [6](#0-5) [1](#0-0) [7](#0-6) [8](#0-7)

### Citations

**File:** basic_bootloader/src/bootloader/transaction/abi_encoded/mod.rs (L156-159)
```rust
        let reserved_0 = parser.parse_u256()?;
        let reserved_1 = parser.parse_u256()?;
        let reserved_2 = parser.parse_u256()?;
        let reserved_3 = parser.parse_u256()?;
```

**File:** basic_bootloader/src/bootloader/transaction/abi_encoded/mod.rs (L267-273)
```rust
        // reserved[1] = refund recipient for l1 to l2 and upgrade txs
        match tx_type {
            Self::L1_L2_TX_TYPE | Self::UPGRADE_TX_TYPE => {
                // TODO: validate address?
            }
            _ => unreachable!(),
        }
```

**File:** zk_ee/src/utils/integer_utils.rs (L133-143)
```rust
pub fn u256_to_b160_checked(src: U256) -> B160 {
    assert!(src.as_limbs()[3] == 0 && src.as_limbs()[2] < (1u64 << 32));
    let mut result = B160::ZERO;
    unsafe {
        result.as_limbs_mut()[0] = src.as_limbs()[0];
        result.as_limbs_mut()[1] = src.as_limbs()[1];
        result.as_limbs_mut()[2] = src.as_limbs()[2];
    }

    result
}
```

**File:** zk_ee/src/utils/integer_utils.rs (L145-158)
```rust
#[inline(always)]
pub fn u256_try_to_b160(src: U256) -> Option<B160> {
    if src.as_limbs()[3] != 0 || src.as_limbs()[2] >= (1u64 << 32) {
        return None;
    }
    let mut result = B160::ZERO;
    unsafe {
        result.as_limbs_mut()[0] = src.as_limbs()[0];
        result.as_limbs_mut()[1] = src.as_limbs()[1];
        result.as_limbs_mut()[2] = src.as_limbs()[2];
    }

    Some(result)
}
```

**File:** basic_bootloader/src/bootloader/transaction_flow/zk/process_l1_transaction.rs (L312-334)
```rust
    let to_refund_recipient = if !is_success {
        // Upgrade transactions must always succeed
        if !is_priority_op {
            return Err(internal_error!("Upgrade transaction must succeed").into());
        }
        // If the transaction reverts, then the minting of the deposit
        // reverted too. Thus, we need to refund the entire deposit minus
        // the fee (`pay_to_operator`).
        total_deposited
            .checked_sub(pay_to_operator)
            .ok_or(internal_error!("td-pto"))
    } else {
        // If the transaction succeeds, then it is assumed that the
        // mint to `from` address was transferred correctly too.
        // In this case, we just refund the unused gas that the
        // transaction paid for initially.
        let prepaid_fee = gas_price
            .checked_mul(U256::from(transaction.gas_limit.read()))
            .ok_or(internal_error!("gp*gl"))?;
        prepaid_fee
            .checked_sub(pay_to_operator)
            .ok_or(internal_error!("pf-pto"))
    }?;
```

**File:** basic_bootloader/src/bootloader/transaction_flow/zk/process_l1_transaction.rs (L336-344)
```rust
    if to_refund_recipient > U256::ZERO {
        let refund_recipient = u256_to_b160_checked(transaction.reserved[1].read());
        mint_base_token::<S, Config>(
            system,
            system_functions,
            memories.reborrow(),
            &to_refund_recipient,
            &refund_recipient,
            l1_chain_id,
```

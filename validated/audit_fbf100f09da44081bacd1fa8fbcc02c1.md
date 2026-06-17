### Title
Incomplete Deposit Sufficiency Check in L1→L2 Transaction Processing Omits `value` Component - (File: `basic_bootloader/src/bootloader/transaction_flow/zk/process_l1_transaction.rs`)

### Summary

In `process_l1_transaction`, the deposit sufficiency guard checks only that `total_deposited >= gas_price * gas_limit` (the fee), but never includes the transaction's `value` field. The `value` variable is read on the very next line yet is silently dropped from the comparison. The type-correct helper `AbiEncodedTransaction::required_balance()` — which returns `value + fee` — exists but is never called here. As a result, any L1→L2 priority transaction whose deposit covers only the fee but not the value will pass the guard, execute, fail at the ETH-transfer step (because `from` was minted zero tokens), and cause the sender to lose the gas fees consumed by the failed execution.

### Finding Description

`process_l1_transaction` performs a deposit sufficiency check before executing the transaction body:

```rust
// process_l1_transaction.rs  lines 128-137
let tx_internal_cost = gas_price
    .checked_mul(U256::from(gas_limit))
    .ok_or(internal_error!("gp*gl"))?;
let value = transaction.value.read();          // ← value is read …
let total_deposited = transaction.reserved[0].read();
require_internal!(
    total_deposited >= tx_internal_cost,       // ← … but never used here
    "Deposited amount too low",
    system
)?;
``` [1](#0-0) 

The guard only enforces `total_deposited >= gas_price * gas_limit`. The `value` variable is read on line 131 but is never incorporated into the comparison.

The correct total required is `gas_price * gas_limit + value`. This is already computed by the existing helper on the same struct:

```rust
// abi_encoded/mod.rs  lines 365-371
pub fn required_balance(&self) -> Option<U256> {
    let fee_amount = self.max_fee_per_gas.read()
        .checked_mul(U256::from(self.gas_limit.read()))?;
    self.value.read().checked_add(U256::from(fee_amount))
}
``` [2](#0-1) 

That helper is never called in `process_l1_transaction`.

Later, inside `execute_l1_transaction_and_notify_result`, the amount actually minted to `from` is:

```rust
// lines 607-613
let max_fee_commitment = gas_price
    .checked_mul(U256::from(transaction.gas_limit.read()))
    .ok_or(internal_error!("gp*gl"))?;
let total_deposited = transaction.reserved[0].read();
let to_transfer = total_deposited
    .checked_sub(max_fee_commitment)
    .ok_or(internal_error!("td-mfc"))?;
``` [3](#0-2) 

When `total_deposited == gas_price * gas_limit` (exactly the fee), `to_transfer == 0`. Nothing is minted to `from`. The transaction body then attempts to transfer `value` from `from` to `to`, but `from` has zero balance, so the call reverts.

On revert, the operator is still paid for gas consumed:

```rust
// lines 277-279
let pay_to_operator = U256::from(gas_used)
    .checked_mul(U256::from(gas_price))
    .ok_or(internal_error!("gu*gp"))?;
``` [4](#0-3) 

And the refund to the sender is only `total_deposited - pay_to_operator`:

```rust
// lines 320-322
total_deposited
    .checked_sub(pay_to_operator)
    .ok_or(internal_error!("td-pto"))
``` [5](#0-4) 

The sender permanently loses `pay_to_operator` (gas fees for a transaction that was guaranteed to fail).

### Impact Explanation

Any L1→L2 priority transaction with `value > 0` and `total_deposited == gas_price * gas_limit` will:

1. Pass the deposit guard (incomplete check).
2. Have `to_transfer = 0` minted to `from`.
3. Revert during execution because `from` cannot cover `value`.
4. Cause the sender to lose `gas_used * gas_price` in fees for a transaction that could never succeed.

The operator is paid from the treasury for work done on a transaction that was structurally doomed. The sender's deposit is partially consumed with no useful outcome. This is a direct resource accounting loss for the sender and an incorrect state transition from the protocol's perspective.

### Likelihood Explanation

The code comment at line 70-71 explicitly states the intent: *"The invariant that the user deposited more than the value needed for the transaction must be enforced on L1, but we double-check it here."* The double-check is the safety net. The safety net is incomplete — it checks only the fee component, not `fee + value`. [6](#0-5) 

If the L1 priority-queue contracts enforce the full `total_deposited >= fee + value` invariant correctly, this path is not reachable in normal operation. However:

- The L2 safety net is the last line of defense if the L1 contracts are upgraded with a regression.
- The `value` variable being read and then silently unused is a strong indicator of an unintentional omission rather than a deliberate design choice.
- The existing `required_balance()` helper already encodes the correct formula, confirming the intent was to check both components.

### Recommendation

Replace the incomplete guard with one that includes `value`:

```rust
let required = tx_internal_cost
    .checked_add(value)
    .ok_or(internal_error!("tc+v overflow"))?;
require_internal!(
    total_deposited >= required,
    "Deposited amount too low",
    system
)?;
```

Alternatively, call the already-correct `transaction.required_balance()` helper and compare against `total_deposited`.

### Proof of Concept

**Setup:** Submit an L1→L2 priority transaction with:
- `gas_price = 1000`
- `gas_limit = 50_000`
- `value = 1_000_000` (1 ETH-equivalent)
- `total_deposited = reserved[0] = 50_000_000` (= `gas_price * gas_limit`, covers fee only)

**Step 1 — Guard passes:**
`total_deposited (50_000_000) >= tx_internal_cost (50_000_000)` → `true`. Check passes.

**Step 2 — Zero tokens minted to sender:**
`to_transfer = 50_000_000 - 50_000_000 = 0`. Nothing is minted to `from`.

**Step 3 — Execution reverts:**
The transaction body attempts to transfer `value = 1_000_000` from `from` (balance = 0) to `to`. Transfer fails. Execution reverts.

**Step 4 — Operator is paid, sender loses fees:**
`gas_used ≈ 21_000` (intrinsic). `pay_to_operator = 21_000 * 1000 = 21_000_000`.
Sender receives refund: `50_000_000 - 21_000_000 = 29_000_000`.
Sender loses `21_000_000` in fees for a transaction that was guaranteed to fail because the deposit check did not account for `value`.

### Citations

**File:** basic_bootloader/src/bootloader/transaction_flow/zk/process_l1_transaction.rs (L70-75)
```rust
    // The invariant that the user deposited more than the value needed
    // for the transaction must be enforced on L1, but we double-check it here
    // Note, that for now the property of block.base <= tx.maxFeePerGas does not work
    // for L1->L2 transactions. For now, these transactions are processed with the same gasPrice
    // they were provided on L1. In the future, we may apply a new logic for it.
    let gas_price = transaction.max_fee_per_gas.read();
```

**File:** basic_bootloader/src/bootloader/transaction_flow/zk/process_l1_transaction.rs (L128-137)
```rust
    let tx_internal_cost = gas_price
        .checked_mul(U256::from(gas_limit))
        .ok_or(internal_error!("gp*gl"))?;
    let value = transaction.value.read();
    let total_deposited = transaction.reserved[0].read();
    require_internal!(
        total_deposited >= tx_internal_cost,
        "Deposited amount too low",
        system
    )?;
```

**File:** basic_bootloader/src/bootloader/transaction_flow/zk/process_l1_transaction.rs (L277-279)
```rust
    let pay_to_operator = U256::from(gas_used)
        .checked_mul(U256::from(gas_price))
        .ok_or(internal_error!("gu*gp"))?;
```

**File:** basic_bootloader/src/bootloader/transaction_flow/zk/process_l1_transaction.rs (L320-322)
```rust
        total_deposited
            .checked_sub(pay_to_operator)
            .ok_or(internal_error!("td-pto"))
```

**File:** basic_bootloader/src/bootloader/transaction_flow/zk/process_l1_transaction.rs (L607-613)
```rust
    let max_fee_commitment = gas_price
        .checked_mul(U256::from(transaction.gas_limit.read()))
        .ok_or(internal_error!("gp*gl"))?;
    let total_deposited = transaction.reserved[0].read();
    let to_transfer = total_deposited
        .checked_sub(max_fee_commitment)
        .ok_or(internal_error!("td-mfc"))?;
```

**File:** basic_bootloader/src/bootloader/transaction/abi_encoded/mod.rs (L365-371)
```rust
    pub fn required_balance(&self) -> Option<U256> {
        let fee_amount = self
            .max_fee_per_gas
            .read()
            .checked_mul(U256::from(self.gas_limit.read()))?;
        self.value.read().checked_add(U256::from(fee_amount))
    }
```

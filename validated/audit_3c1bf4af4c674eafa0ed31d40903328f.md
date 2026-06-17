### Title
L1 Transaction Sender Can Bypass Pubdata Cost Enforcement via `gas_per_pubdata_limit=0` — (`basic_bootloader/src/bootloader/transaction_flow/zk/process_l1_transaction.rs`)

---

### Summary

An L1→L2 transaction sender can set `gas_per_pubdata_limit=0` in their transaction. The bootloader reads this value directly and derives `native_per_pubdata=0`, which causes every pubdata sufficiency check to trivially pass. The transaction can then generate arbitrarily large pubdata without being reverted or charged for it. Because L1 priority-queue transactions cannot be censored by the operator, this forces the operator to publish unbounded pubdata to L1 without compensation — a direct resource-accounting loss.

---

### Finding Description

**Step 1 — Unvalidated field read.**

In `process_l1_transaction`, `gas_per_pubdata` is read from the transaction with no lower-bound check:

```rust
let gas_per_pubdata = transaction.gas_per_pubdata_limit.read();
``` [1](#0-0) 

The `validate_structure()` function in `abi_encoded/mod.rs` explicitly leaves this field unvalidated for L1 transactions, noting only a `// TODO: validate address?` for the adjacent `reserved[1]` field:

```rust
// reserved[1] = refund recipient for l1 to l2 and upgrade txs
match tx_type {
    Self::L1_L2_TX_TYPE | Self::UPGRADE_TX_TYPE => {
        // TODO: validate address?
    }
    ...
}
``` [2](#0-1) 

There is no corresponding check that `gas_per_pubdata_limit != 0`.

**Step 2 — Zero propagates to `native_per_pubdata`.**

In `prepare_and_check_resources`, when `gas_per_pubdata == 0`:

```rust
let native_per_pubdata = (gas_per_pubdata as u64)   // == 0
    .checked_mul(native_per_gas)                     // 0 × anything == 0
    .unwrap_or_else(|| { ... u64::MAX });
// → native_per_pubdata == 0
``` [3](#0-2) 

**Step 3 — Pubdata sufficiency check is trivially bypassed.**

`check_enough_resources_for_pubdata` calls `get_resources_to_charge_for_pubdata`, which computes:

```rust
let native = current_pubdata_spent
    .checked_mul(native_per_pubdata)   // N × 0 == 0
    .ok_or(out_of_native_resources!())?;
// resources_for_pubdata == 0  →  has_enough == true always
``` [4](#0-3) 

This is called both during execution and in the post-execution pubdata check: [5](#0-4) 

With `native_per_pubdata=0`, neither check can ever revert the transaction for pubdata overuse.

**Step 4 — Operator fee is computed only on gas, not pubdata.**

The fee minted to the operator is:

```rust
let pay_to_operator = U256::from(gas_used)
    .checked_mul(U256::from(gas_price))
    .ok_or(internal_error!("gu*gp"))?;
``` [6](#0-5) 

There is no pubdata component in this payment. The operator receives only `gas_used × gas_price` regardless of how many pubdata bytes the transaction generated.

---

### Impact Explanation

An attacker submits an L1 priority-queue transaction with `gas_per_pubdata_limit=0`, a non-zero `gas_price`, and a `to` address pointing to a contract that writes to many storage slots. Because `native_per_pubdata=0`, the transaction executes to completion regardless of pubdata generated. The operator:

1. Is forced to include the transaction (priority queue cannot be censored).
2. Receives only `gas_used × gas_price` tokens.
3. Must publish all generated pubdata to L1 (Ethereum calldata / blobs), paying real ETH for data availability that was never priced into the transaction.

Repeated submissions drain operator funds proportional to the pubdata generated per transaction. The treasury is also drawn down by `total_deposited` per transaction, amplifying the loss.

---

### Likelihood Explanation

- `gas_per_pubdata_limit` is a plain `u32` field freely set by the L1 transaction sender with no on-chain floor enforced in ZKsync OS itself.
- The `L1TxBuilder` test helper defaults `gas_per_pubdata_byte_limit` to `0`, confirming the value is accepted without error.
- Any user who can submit an L1→L2 transaction (i.e., any Ethereum address) can exploit this.
- No privileged role, leaked key, or governance action is required. [7](#0-6) 

---

### Recommendation

1. **Add a lower-bound check in `validate_structure()`** for L1 and upgrade transactions: reject any transaction where `gas_per_pubdata_limit == 0`.
2. **Alternatively, enforce a protocol-level floor** in `prepare_and_check_resources`: if `gas_per_pubdata == 0`, substitute a minimum value derived from the current block's `pubdata_price / gas_price` ratio (mirroring the `FREE_L1_TX_NATIVE_PER_GAS` fallback already used for `gas_price == 0`).
3. **Remove the `// TODO: validate address?` placeholder** and complete the `reserved[1]` / `gas_per_pubdata_limit` validation pass for ABI-encoded transactions.

---

### Proof of Concept

```
1. Deploy a contract at address T on L2 that writes to 1 000 storage slots in its fallback.
2. Submit an L1 priority-queue transaction:
     from  = attacker_address
     to    = T
     gas_price           = 1 000
     gas_limit           = 2 000 000
     gas_per_pubdata_limit = 0          ← key parameter
     to_mint             = gas_limit × gas_price  (covers max fee)
3. ZKsync OS processes the transaction:
     native_per_pubdata = 0 × (1000/10) = 0
     pubdata check      → has_enough = true  (always)
     1 000 SSTORE ops   → ~32 000 bytes of pubdata generated
     pay_to_operator    = gas_used × 1 000  (no pubdata component)
4. Operator must publish ~32 KB to L1 (≈ 512 000 gas at 16 gas/byte on Ethereum)
   but received only the L2 gas fee — a net loss per transaction.
5. Repeat with many such transactions to drain operator funds.
``` [8](#0-7) [9](#0-8) [10](#0-9)

### Citations

**File:** basic_bootloader/src/bootloader/transaction_flow/zk/process_l1_transaction.rs (L75-80)
```rust
    let gas_price = transaction.max_fee_per_gas.read();

    // For L1->L2 transactions we always use the pubdata price provided by the transaction.
    // This is needed to ensure DDoS protection. All the excess expenditure
    // will be refunded to the user.
    let gas_per_pubdata = transaction.gas_per_pubdata_limit.read();
```

**File:** basic_bootloader/src/bootloader/transaction_flow/zk/process_l1_transaction.rs (L277-279)
```rust
    let pay_to_operator = U256::from(gas_used)
        .checked_mul(U256::from(gas_price))
        .ok_or(internal_error!("gu*gp"))?;
```

**File:** basic_bootloader/src/bootloader/transaction_flow/zk/process_l1_transaction.rs (L455-488)
```rust
    let native_per_gas = if is_priority_op {
        if gas_price.is_zero() {
            if Config::SIMULATION {
                u256_try_to_u64(&system.get_eip1559_basefee().div_ceil(native_price))
                    .unwrap_or_else(|| {
                        system_log!(
                            system,
                            "Native per gas calculation for L1 tx overflows, using saturated arithmetic instead");
                        u64::MAX
                    })
            } else {
                FREE_L1_TX_NATIVE_PER_GAS
            }
        } else {
            u256_try_to_u64(&gas_price.div_ceil(native_price)).unwrap_or_else(|| {
                system_log!(
                    system,
                    "Native per gas calculation for L1 tx overflows, using saturated arithmetic instead");
                u64::MAX
            })
        }
    } else {
        // Upgrade txs are paid by the protocol, so we use a fixed native per gas
        FREE_L1_TX_NATIVE_PER_GAS
    };

    let native_per_pubdata = (gas_per_pubdata as u64)
        .checked_mul(native_per_gas)
        .unwrap_or_else(|| {
            system_log!(
                system,
                "Native per pubdata calculation for L1 tx overflows, using saturated arithmetic instead");
                u64::MAX
        });
```

**File:** basic_bootloader/src/bootloader/transaction_flow/zk/process_l1_transaction.rs (L713-715)
```rust
    let (enough, to_charge_for_pubdata, pubdata_used) =
        check_enough_resources_for_pubdata(system, native_per_pubdata, resources, None)?;
    let is_success = !reverted && enough;
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

**File:** basic_bootloader/src/bootloader/transaction_flow/gas_helpers.rs (L422-435)
```rust
pub fn get_resources_to_charge_for_pubdata<S: EthereumLikeTypes>(
    system: &mut System<S>,
    native_per_pubdata: u64,
    base_pubdata: Option<u64>,
) -> Result<(u64, S::Resources), SystemError> {
    let current_pubdata_spent = system
        .net_pubdata_used()?
        .saturating_sub(base_pubdata.unwrap_or(0));
    let native = current_pubdata_spent
        .checked_mul(native_per_pubdata)
        .ok_or(out_of_native_resources!())?;
    let native = <S::Resources as zk_ee::system::Resources>::Native::from_computational(native);
    Ok((current_pubdata_spent, S::Resources::from_native(native)))
}
```

**File:** tests/rig/src/utils/mod.rs (L326-338)
```rust
        Self {
            from: Default::default(),
            to: Default::default(),
            gas_price: 0,
            gas_limit: 0,
            input: Vec::new(),
            value: Default::default(),
            nonce: 0,
            refund_recipient: Default::default(),
            to_mint: Default::default(),
            factory_deps: Vec::new(),
            gas_per_pubdata_byte_limit: 0,
        }
```

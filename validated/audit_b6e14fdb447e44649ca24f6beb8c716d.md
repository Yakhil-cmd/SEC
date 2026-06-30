### Title
Silo `fixed_gas` Used as EVM Gas Limit Substitute Allows Cheap Computation and Relayer Yield Theft — (`engine/src/engine.rs`)

### Summary

In silo mode, `fixed_gas` is used exclusively for fee accounting (what the sender pays and what the relayer earns), but the EVM executor is still given the full `transaction.gas_limit`. When `gas_used > fixed_gas`, the relayer is compensated only for `fixed_gas` units of work despite performing `gas_used` units of actual computation, and the sender runs arbitrarily expensive EVM computation for the price of `fixed_gas`.

---

### Finding Description

**`charge_gas` — fee deduction uses `fixed_gas`:** [1](#0-0) 

When `fixed_gas` is set, the sender is charged `fixed_gas * effective_gas_price`, not `gas_limit * effective_gas_price`.

**EVM execution — gas limit is `transaction.gas_limit`, not `fixed_gas`:** [2](#0-1) 

The EVM receives `gas_limit = transaction.gas_limit` (the full user-supplied value). The only guard is: [3](#0-2) 

This rejects `fixed_gas > gas_limit` but explicitly allows `gas_limit >> fixed_gas`.

**`refund_unused_gas` — reward uses `fixed_gas` regardless of `gas_used`:** [4](#0-3) 

The `gas_to_wei` closure always picks `EthGas::as_u256` (i.e., `fixed_gas`) when `fixed_gas` is `Some`, ignoring `gas_used` entirely. Both `spent_amount` and `reward_amount` are computed from `fixed_gas`, not `gas_used`.

**Concrete arithmetic with `fixed_gas = 21_000`, `gas_used = 500_000`, `effective_gas_price = P`, `priority_fee = F`:**

| Variable | Value |
|---|---|
| `prepaid_amount` | `21_000 * P` |
| `spent_amount` | `21_000 * P` |
| `refund` | `0` |
| `relayer_reward` | `21_000 * F` |
| Actual EVM work | `500_000` gas |
| Relayer shortfall | `(500_000 − 21_000) * F` |

The sender pays for 21 000 gas but the EVM executes 500 000 gas. The relayer absorbs the NEAR gas cost of the extra 479 000 EVM gas units but receives no compensation for them.

---

### Impact Explanation

- **Sender:** runs up to `gas_limit` gas of EVM computation while paying only `fixed_gas * effective_gas_price`. The ratio `gas_limit / fixed_gas` is the free-computation multiplier; with `fixed_gas = 21_000` and `gas_limit = 10_000_000` this is ~476×.
- **Relayer:** earns `fixed_gas * priority_fee_per_gas` regardless of actual work. The shortfall `(gas_used − fixed_gas) * priority_fee_per_gas` is permanently lost yield — matching the **High: Theft of unclaimed yield** impact category.

---

### Likelihood Explanation

Silo mode with `fixed_gas` is a documented, production-deployed feature. Any address permitted by the silo whitelist (or any address if the whitelist is disabled) can submit a transaction with `gas_limit >> fixed_gas`. No admin compromise is required; the attacker only needs to be an allowed submitter in the silo.

---

### Recommendation

Choose one of:

1. **Cap the EVM gas limit to `fixed_gas`** — replace `transaction.gas_limit` with `fixed_gas.as_u64()` when `fixed_gas` is set, so the EVM cannot consume more gas than what was paid for.
2. **Use `max(gas_used, fixed_gas)` for reward calculation** — in `refund_unused_gas`, compute `reward_amount` as `max(gas_used, fixed_gas.as_u64()) * priority_fee_per_gas` so the relayer is never underpaid relative to actual work.

Option 1 is safer because it also prevents the sender from getting cheap computation.

---

### Proof of Concept

```rust
// Pseudocode integration test
set_silo_params(fixed_gas = 21_000, ...);

// Deploy a contract that burns ~100_000 gas (e.g., a tight loop)
let heavy_contract = deploy_gas_heavy_contract();

// Submit EIP-1559 tx with gas_limit >> fixed_gas
let tx = Transaction1559 {
    gas_limit: 500_000,
    max_priority_fee_per_gas: 10,
    max_fee_per_gas: 10,
    to: Some(heavy_contract),
    ..
};
let result = submit(tx);

// gas_used reported by EVM
assert!(result.gas_used > 21_000); // e.g. 100_000

// Relayer balance increased by only fixed_gas * priority_fee, not gas_used * priority_fee
let relayer_balance = get_balance(relayer);
assert_eq!(relayer_balance, 21_000 * 10);  // NOT 100_000 * 10
// Relayer lost (100_000 - 21_000) * 10 = 790_000 wei of yield
```

The test at `engine/src/engine.rs:2794` (`test_refund_fixed_gas_pays_expected_amount`) only covers the case `gas_used (4000) < fixed_gas (7000)` and does not test the `gas_used > fixed_gas` path, leaving the bug undetected. [5](#0-4)

### Citations

**File:** engine/src/engine.rs (L496-500)
```rust
        let prepaid_amount = fixed_gas
            .map_or(transaction.gas_limit, EthGas::as_u256)
            .checked_mul(effective_gas_price)
            .map(Wei::new)
            .ok_or(GasPaymentError::EthAmountOverflow)?;
```

**File:** engine/src/engine.rs (L1066-1068)
```rust
    if fixed_gas.is_some_and(|gas| gas.as_u256() > transaction.gas_limit) {
        return Err(EngineErrorKind::FixedGasOverflow.into());
    }
```

**File:** engine/src/engine.rs (L1107-1126)
```rust
    let gas_limit = transaction
        .gas_limit
        .try_into()
        .map_err(|_| EngineErrorKind::GasOverflow)?;
    let access_list = transaction
        .access_list
        .into_iter()
        .map(|a| (a.address, a.storage_keys))
        .collect();
    let result = if let Some(receiver) = transaction.to {
        engine.call(
            &sender,
            &receiver,
            transaction.value,
            transaction.data,
            gas_limit,
            access_list,
            transaction.authorization_list,
            handler,
        )
```

**File:** engine/src/engine.rs (L1274-1291)
```rust
    let (refund, relayer_reward) = {
        let gas_to_wei = |price: U256| {
            fixed_gas
                .map_or_else(|| gas_used.into(), EthGas::as_u256)
                .checked_mul(price)
                .map(Wei::new)
                .ok_or(GasPaymentError::EthAmountOverflow)
        };

        let spent_amount = gas_to_wei(gas_result.effective_gas_price)?;
        let reward_amount = gas_to_wei(gas_result.priority_fee_per_gas)?;

        let refund = gas_result
            .prepaid_amount
            .checked_sub(spent_amount)
            .ok_or(GasPaymentError::EthAmountOverflow)?;

        (refund, reward_amount)
```

**File:** engine/src/engine.rs (L2793-2817)
```rust
    #[test]
    fn test_refund_fixed_gas_pays_expected_amount() {
        let origin = Address::zero();
        let storage = RefCell::new(Storage::default());
        let mut io = StoragePointer(&storage);
        let expected_state = EngineState::default();
        state::set_state(&mut io, &expected_state).unwrap();
        let relayer = make_address(1, 1);
        let gas_result = GasPaymentResult {
            prepaid_amount: Wei::new_u64(8000),
            effective_gas_price: 1.into(),
            priority_fee_per_gas: 2.into(),
        };
        let gas_used = 4000;
        let fixed_gas = Some(EthGas::new(7000));

        refund_unused_gas(&mut io, &origin, gas_used, &gas_result, &relayer, fixed_gas).unwrap();

        let actual_refund = get_balance(&io, &origin);
        let expected_refund = Wei::new_u64(1000);
        assert_eq!(expected_refund, actual_refund);

        let actual_refund = get_balance(&io, &relayer);
        let expected_refund = Wei::new_u64(7000 * 2);
        assert_eq!(expected_refund, actual_refund);
```

### Title
Excess Cycles Sent to `create_canister` Are Not Refunded to the Caller — (`rs/nns/cmc/src/main.rs`)

---

### Summary

The `create_canister` endpoint of the Cycles Minting Canister (CMC) checks that the caller has attached at least `CREATE_CANISTER_MIN_CYCLES` (100 billion cycles), but on success it accepts **all** available cycles from the message rather than only the minimum required. Any cycles sent in excess of the minimum are permanently consumed (forwarded to the newly created canister) without being refunded to the caller.

---

### Finding Description

In `rs/nns/cmc/src/main.rs`, the `create_canister` function reads the full amount of cycles available in the incoming message and stores it in `cycles`:

```rust
let cycles = ic_cdk::api::call::msg_cycles_available();
```

It then enforces a minimum:

```rust
if cycles < CREATE_CANISTER_MIN_CYCLES { ... }
```

But on the success path it accepts the **entire** `cycles` value — not just `CREATE_CANISTER_MIN_CYCLES`:

```rust
Ok(canister_id) => {
    ic_cdk::api::call::msg_cycles_accept(cycles);   // accepts ALL, not just minimum
    Ok(canister_id)
}
```

The full `cycles` amount is also forwarded to `do_create_canister` as the initial funding for the new canister:

```rust
match do_create_canister(caller(), cycles.into(), subnet_selection, settings).await {
```

On the Internet Computer, any cycles attached to a call that are **not** explicitly accepted by the callee are automatically refunded to the caller. Because the CMC calls `msg_cycles_accept(cycles)` with the full available amount, no automatic refund occurs. A caller who sends, say, 1 trillion cycles expecting to pay the 100-billion-cycle minimum will have all 900 billion excess cycles silently forwarded to the newly created canister rather than returned. [1](#0-0) [2](#0-1) [3](#0-2) 

The minimum constant is defined at: [4](#0-3) 

---

### Impact Explanation

Any caller (canister or user) who attaches more than `CREATE_CANISTER_MIN_CYCLES` to a successful `create_canister` call permanently loses the excess cycles. Those cycles are transferred to the newly created canister rather than refunded. From the caller's perspective the excess is irrecoverable: the IC protocol's automatic refund mechanism is bypassed because `msg_cycles_accept` is called with the full available amount. This is a **cycles conservation bug** — the caller's cycles balance is reduced by more than the actual cost of the operation.

---

### Likelihood Explanation

The `create_canister` endpoint is publicly callable by any canister or principal. Callers routinely attach a safety buffer of extra cycles to avoid failed calls due to rate changes or rounding. Any such caller will silently lose the buffer. The entry path requires no privilege: an unprivileged canister calls `create_canister` on the CMC with cycles attached via `call_with_payment128`. The bug is triggered on every successful canister creation where the attached cycles exceed `CREATE_CANISTER_MIN_CYCLES`.

---

### Recommendation

Accept only the exact cost of the operation, not the full available amount. Replace:

```rust
ic_cdk::api::call::msg_cycles_accept(cycles);
```

with:

```rust
ic_cdk::api::call::msg_cycles_accept(CREATE_CANISTER_MIN_CYCLES);
```

This allows the IC runtime to automatically refund the unaccepted remainder to the caller. If the intent is to let callers voluntarily over-fund the new canister, this should be explicitly documented and the function interface should make the behavior unambiguous (e.g., via a separate `initial_cycles` parameter). [5](#0-4) 

---

### Proof of Concept

1. Caller canister attaches 1,000,000,000,000 cycles (1 trillion) to a call to `create_canister` on the CMC.
2. `CREATE_CANISTER_MIN_CYCLES` is 100,000,000,000 (100 billion). The check at line 1488 passes.
3. `do_create_canister` is called with `cycles = 1_000_000_000_000` as the initial funding.
4. On success, `msg_cycles_accept(1_000_000_000_000)` is called — the CMC accepts all 1 trillion cycles.
5. The new canister receives 1 trillion cycles as its initial balance.
6. The caller's balance is reduced by 1 trillion cycles instead of the expected ~100 billion.
7. The 900 billion excess cycles are permanently gone from the caller's balance with no refund. [6](#0-5)

### Citations

**File:** rs/nns/cmc/src/main.rs (L78-78)
```rust
const CREATE_CANISTER_MIN_CYCLES: u64 = 100_000_000_000;
```

**File:** rs/nns/cmc/src/main.rs (L1486-1515)
```rust
    let cycles = ic_cdk::api::call::msg_cycles_available();

    if cycles < CREATE_CANISTER_MIN_CYCLES {
        return Err(CreateCanisterError::Refunded {
            refund_amount: cycles.into(),
            create_error: "Insufficient cycles attached.".to_string(),
        });
    }
    let subnet_selection =
        get_subnet_selection(subnet_type, subnet_selection).map_err(|error_message| {
            CreateCanisterError::Refunded {
                refund_amount: cycles.into(),
                create_error: error_message,
            }
        })?;

    match do_create_canister(caller(), cycles.into(), subnet_selection, settings).await {
        Ok(canister_id) => {
            ic_cdk::api::call::msg_cycles_accept(cycles);
            Ok(canister_id)
        }
        Err(create_error) => {
            ic_cdk::api::call::msg_cycles_accept(BAD_REQUEST_CYCLES_PENALTY as u64);
            let refund_amount = ic_cdk::api::call::msg_cycles_available();
            Err(CreateCanisterError::Refunded {
                refund_amount: refund_amount.into(),
                create_error,
            })
        }
    }
```

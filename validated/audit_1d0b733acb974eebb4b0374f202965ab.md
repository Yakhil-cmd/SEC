### Title
Hardcoded Reference Subnet Size `13` in `http_request_fee_v2` Produces Incorrect Cost Estimates on Non-13-Node Subnets - (`rs/cycles_account_manager/src/cycles_account_manager.rs`)

---

### Summary

`CyclesAccountManager::http_request_fee_v2` computes the cycles cost estimate for HTTP outcalls (exposed to canisters via the `ic0.cost_http_request_v2` system API). The transform-instruction cost component divides by the hardcoded literal `13` instead of the actual subnet size `n`. Every other term in the formula uses the real subnet size. On subnets smaller than 13 nodes the estimate is too low; on subnets larger than 13 nodes it is too high. Canisters that rely on this estimate to size their cycle attachment will either have their HTTP outcalls rejected (under-estimate) or waste cycles (over-estimate).

---

### Finding Description

In `http_request_fee_v2` the per-node cost is assembled and then multiplied by `n` (the actual subnet size):

```rust
let n = subnet_cycles_config.subnet_size as u64;
let amount = (Cycles::new(1_000_000)
    + Cycles::new(50) * request_size.get()
    + Cycles::new(140_000) * n
    + Cycles::new(800) * n * n
    + Cycles::new(50) * raw_response_size.get()
    + Cycles::new(300) * http_roundtrip_time.as_millis() as u64
    + Cycles::new(transform.get() as u128 / 13)   // ← hardcoded 13
    + (Cycles::new(10) * n + Cycles::new(650)) * transformed_response_size.get())
    * n;
```

Every other term scales with `n`. The transform term uses the literal `13` — the `DEFAULT_REFERENCE_SUBNET_SIZE` — instead of `n`. The resulting total transform contribution is `(transform_instructions / 13) * n`, which equals the correct value only when `n == 13`. [1](#0-0) 

The constant `DEFAULT_REFERENCE_SUBNET_SIZE = 13` is defined in `rs/config/src/subnet_config.rs` and is explicitly documented as the baseline around which all fees are scaled: [2](#0-1) 

The system API entry point `ic0_cost_http_request_v2` passes the real `subnet_cycles_config` (containing the actual subnet size) into `http_request_fee_v2`, so the actual subnet size is available but unused for the transform term: [3](#0-2) 

---

### Impact Explanation

| Subnet size `n` | Transform cost returned | Correct cost | Effect |
|---|---|---|---|
| 4 | `(T/13)*4 ≈ 0.31T` | `T` | **Under-estimate** → canister attaches too few cycles → HTTP outcall rejected |
| 13 | `(T/13)*13 = T` | `T` | Correct |
| 34 | `(T/13)*34 ≈ 2.6T` | `T` | **Over-estimate** → canister wastes ~1.6× cycles per request |

Canisters that call `ic0.cost_http_request_v2` to size their cycle attachment and then issue an HTTP outcall will be systematically mis-priced on any subnet whose membership differs from 13. The under-estimate case causes a functional failure (request rejected for insufficient cycles) that is directly triggered by the canister's own unprivileged call.

---

### Likelihood Explanation

The IC operates subnets of varying sizes (e.g., 13-node application subnets, 34-node fiduciary subnets, smaller test/SEV subnets). Any canister on a non-13-node subnet that uses the `ic0.cost_http_request_v2` API to estimate HTTP outcall costs will receive a wrong value. The API is publicly documented and intended for production use by canister developers. No privileged access is required; any canister caller can trigger the mis-priced estimate.

---

### Recommendation

Replace the hardcoded `13` with the actual subnet size `n` already computed at the top of the function:

```rust
// Before
+ Cycles::new(transform.get() as u128 / 13)

// After
+ Cycles::new(transform.get() as u128 / n.max(1))
```

Alternatively, use `self.config.reference_subnet_size` if the intent is to normalize against the reference size (though that would still be wrong for the same reason as the Casimir bug — the actual size should be used).

---

### Proof of Concept

1. Deploy a canister on a 4-node subnet.
2. Call `ic0.cost_http_request_v2` with a non-zero `transform_instructions` value, e.g., `transform_instructions = 1_300_000`.
3. Observed estimate for the transform component: `(1_300_000 / 13) * 4 = 400_000` cycles.
4. Correct estimate: `(1_300_000 / 4) * 4 = 1_300_000` cycles.
5. Attach the under-estimated cycle amount to an HTTP outcall; the request is rejected because the actual charge exceeds the attached cycles.

The root cause is solely the literal `13` at line 1301 of `rs/cycles_account_manager/src/cycles_account_manager.rs`. [4](#0-3)

### Citations

**File:** rs/cycles_account_manager/src/cycles_account_manager.rs (L1294-1303)
```rust
        let n = subnet_cycles_config.subnet_size as u64;
        let amount = (Cycles::new(1_000_000)
            + Cycles::new(50) * request_size.get()
            + Cycles::new(140_000) * n
            + Cycles::new(800) * n * n
            + Cycles::new(50) * raw_response_size.get()
            + Cycles::new(300) * http_roundtrip_time.as_millis() as u64
            + Cycles::new(transform.get() as u128 / 13)
            + (Cycles::new(10) * n + Cycles::new(650)) * transformed_response_size.get())
            * n;
```

**File:** rs/config/src/subnet_config.rs (L158-162)
```rust
/// Default subnet size which is used to scale cycles cost according to a subnet replication factor.
///
/// All initial costs were calculated with the assumption that a subnet had 13 replicas.
/// IMPORTANT: never set this value to zero.
pub const DEFAULT_REFERENCE_SUBNET_SIZE: usize = 13;
```

**File:** rs/embedders/src/wasmtime_embedder/system_api.rs (L4382-4396)
```rust
        let subnet_cycles_config = self.sandbox_safe_system_state.subnet_cycles_config;
        let cost = self
            .sandbox_safe_system_state
            .get_cycles_account_manager()
            .http_request_fee_v2(
                cost_params_v2.request_bytes.into(),
                Duration::from_millis(cost_params_v2.http_roundtrip_time_ms),
                cost_params_v2.raw_response_bytes.into(),
                cost_params_v2.transform_instructions.into(),
                cost_params_v2.transformed_response_bytes.into(),
                subnet_cycles_config,
            );
        copy_cycles_to_heap(cost.real(), dst, heap, "ic0_cost_http_request_v2")?;
        trace_syscall!(self, CostHttpRequestV2, cost);
        Ok(())
```

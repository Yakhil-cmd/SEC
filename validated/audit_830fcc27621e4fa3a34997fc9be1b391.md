### Title
Permanent ERC-20 Token Loss Due to Missing Refund Callback in `ExitToEthereum` Precompile — (`engine-precompiles/src/native.rs`)

---

### Summary

The `ExitToEthereum` precompile burns ERC-20 tokens on the Aurora EVM side and then schedules a bare NEAR promise (`PromiseArgs::Create`) to call `withdraw` on the NEP-141 contract. Unlike `ExitToNear`, which conditionally wraps its promise in a `PromiseArgs::Callback` that invokes `exit_to_near_precompile_callback` to re-mint burned tokens on failure, `ExitToEthereum` has **no error callback and no refund mechanism whatsoever**. If the NEAR-side `withdraw` promise fails for any reason, the ERC-20 tokens are permanently destroyed with no recovery path.

---

### Finding Description

**Step 1 — ERC-20 tokens are burned before the NEAR promise executes.**

In `EvmErc20.sol`, `withdrawToEthereum` burns the caller's tokens first, then calls the precompile:

```solidity
function withdrawToEthereum(address recipient, uint256 amount) external override {
    _burn(_msgSender(), amount);          // ERC-20 burned unconditionally
    // ... encodes input and calls ExitToEthereum precompile
}
```

The same pattern exists in `EvmErc20V2.sol`.

**Step 2 — `ExitToEthereum` schedules a bare promise with no callback.**

In `ExitToEthereum::run()`, after parsing the input, the precompile creates only a `PromiseArgs::Create`:

```rust
let withdraw_promise = PromiseCreateArgs {
    target_account_id: nep141_address,
    method: "withdraw".to_string(),
    args: serialized_args,
    attached_balance: Yocto::new(1),
    attached_gas: costs::WITHDRAWAL_GAS,
};

let promise = borsh::to_vec(&PromiseArgs::Create(withdraw_promise)).unwrap();
// No callback. No error handling. No refund.
```

**Step 3 — Contrast with `ExitToNear`, which has a refund path.**

`ExitToNear::run()` conditionally wraps its promise in a `PromiseArgs::Callback` that calls `exit_to_near_precompile_callback` on the engine contract. That callback, when the base promise fails, invokes `engine::refund_on_error`, which re-mints the burned ERC-20 tokens to the original sender:

```rust
let promise = if callback_args == ExitToNearPrecompileCallbackArgs::default() {
    PromiseArgs::Create(transfer_promise)
} else {
    PromiseArgs::Callback(PromiseWithCallbackArgs {
        base: transfer_promise,
        callback: PromiseCreateArgs {
            method: "exit_to_near_precompile_callback".to_string(),
            // ...
        },
    })
};
```

And in `exit_to_near_precompile_callback`:

```rust
} else if let Some(args) = args.refund {
    // Exit call failed; need to refund tokens
    let refund_result = engine::refund_on_error(io, env, state, &args, handler)?;
```

`refund_on_error` re-mints the burned ERC-20 tokens:

```rust
if let Some(erc20_address) = args.erc20_address {
    // ERC-20 exit; re-mint burned tokens
    engine.call(&erc20_admin_address, &erc20_address, Wei::zero(), input, ...)
```

**Step 4 — The `ExitToEthereum` path has no equivalent.**

The `ExitToEthereum` precompile has no `error_refund` feature guard, no callback promise, and no call to `refund_on_error`. The test suite itself documents this asymmetry explicitly for `ExitToNear`:

```rust
// If the refund feature is not enabled then there is no refund in the EVM
#[cfg(not(feature = "error_refund"))]
let balance = (FT_TRANSFER_AMOUNT - FT_EXIT_AMOUNT).into();
```

But for `ExitToEthereum`, there is **never** a refund path, regardless of any feature flag.

**Step 5 — Conditions under which the `withdraw` promise can fail.**

The NEAR-side `withdraw` promise (called on the NEP-141 contract) can fail if:

- Aurora's NEP-141 balance in the token contract is less than the requested withdrawal amount (e.g., due to any accounting discrepancy between ERC-20 supply and NEP-141 holdings).
- The NEP-141 contract's `withdraw` method panics or returns an error for any reason (e.g., storage deposit not met, contract upgrade, minimum amount check).
- The `WITHDRAWAL_GAS` constant (100 TGas) is insufficient for the specific NEP-141 contract's `withdraw` implementation.
- The eth-connector's `engine_withdraw` feature is paused between the EVM execution receipt and the promise execution receipt (these are separate NEAR receipts).

In all of these cases, the ERC-20 tokens are already burned and cannot be recovered.

---

### Impact Explanation

**Critical — Permanent freezing/loss of user funds.**

A user calling `withdrawToEthereum` on any `EvmErc20` or `EvmErc20V2` contract loses their ERC-20 tokens permanently if the NEAR-side `withdraw` promise fails. The tokens are burned on the Aurora EVM side, the NEP-141 tokens remain in Aurora's account (not released to Ethereum), and there is no mechanism to recover either. This is a direct, irreversible loss of bridged assets.

This is the exact analog of the Reserve Protocol bug: in both cases, a "redemption" operation destroys the user's tokens on one side of the bridge/protocol, but the backing asset is not delivered, and there is no refund path.

---

### Likelihood Explanation

**Medium.**

Under normal operating conditions, the `withdraw` call on a well-functioning NEP-141 contract succeeds. However:

1. The eth-connector's `engine_withdraw` feature is explicitly pausable (as shown in the connector tests), and a pause between EVM execution and promise execution would silently destroy user funds.
2. Any accounting discrepancy between ERC-20 total supply and Aurora's NEP-141 balance — which could arise from bugs in `ft_on_transfer`, `ExitToNear`, or contract upgrades — would cause `withdraw` to fail for all subsequent `ExitToEthereum` callers.
3. The `WITHDRAWAL_GAS` constant is hardcoded and not validated against the actual gas requirements of the target NEP-141 contract.

The vulnerability is reachable by any unprivileged EVM user calling `withdrawToEthereum` on any bridged ERC-20 token.

---

### Recommendation

Add an error callback to `ExitToEthereum` analogous to the `ExitToNear` refund mechanism:

1. In `ExitToEthereum::run()`, replace `PromiseArgs::Create(withdraw_promise)` with a `PromiseArgs::Callback` that calls a new `exit_to_ethereum_precompile_callback` method on the engine contract.
2. In that callback, if the `withdraw` promise failed, call `engine::refund_on_error` to re-mint the burned ERC-20 tokens (or restore the ETH balance for the base token case).
3. For the ERC-20 case, the `EvmErc20.sol` / `EvmErc20V2.sol` contracts must pass the refund address in the precompile input (as `EvmErc20V2.sol::withdrawToNear` already does for the `ExitToNear` path).

---

### Proof of Concept

**Attacker-reachable path:**

1. User holds 1,000 units of a bridged NEP-141 token as ERC-20 on Aurora.
2. User calls `erc20.withdrawToEthereum(recipient, 1000)`.
3. `EvmErc20.sol` calls `_burn(msg.sender, 1000)` — ERC-20 tokens destroyed.
4. `ExitToEthereum` precompile schedules `withdraw` on the NEP-141 contract via `PromiseArgs::Create`.
5. The NEP-141 contract's `withdraw` fails (e.g., Aurora's NEP-141 balance is 999 due to a prior accounting discrepancy, or the contract is paused).
6. NEAR runtime discards the failed promise result. No callback is registered.
7. User has 0 ERC-20 tokens, 0 NEP-141 tokens on Ethereum, and no recourse.

**Key code references:**

`ExitToEthereum` — no callback, bare `Create` promise: [1](#0-0) 

`ExitToNear` — conditional `Callback` promise with refund: [2](#0-1) 

`exit_to_near_precompile_callback` — refund on failure: [3](#0-2) 

`refund_on_error` — re-mints burned ERC-20 tokens: [4](#0-3) 

`EvmErc20.sol::withdrawToEthereum` — burns before calling precompile: [5](#0-4) 

Test confirming `ExitToNear` refund is feature-gated (and `ExitToEthereum` has no equivalent): [6](#0-5)

### Citations

**File:** engine-precompiles/src/native.rs (L470-483)
```rust
        let promise = if callback_args == ExitToNearPrecompileCallbackArgs::default() {
            PromiseArgs::Create(transfer_promise)
        } else {
            PromiseArgs::Callback(PromiseWithCallbackArgs {
                base: transfer_promise,
                callback: PromiseCreateArgs {
                    target_account_id: self.current_account_id.clone(),
                    method: "exit_to_near_precompile_callback".to_string(),
                    args: borsh::to_vec(&callback_args).unwrap(),
                    attached_balance: Yocto::new(0),
                    attached_gas: costs::EXIT_TO_NEAR_CALLBACK_GAS,
                },
            })
        };
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

**File:** engine/src/contract_methods/connector.rs (L231-239)
```rust
        } else if let Some(args) = args.refund {
            // Exit call failed; need to refund tokens
            let refund_result = engine::refund_on_error(io, env, state, &args, handler)?;

            if !refund_result.status.is_ok() {
                return Err(errors::ERR_REFUND_FAILURE.into());
            }

            Some(refund_result)
```

**File:** engine/src/engine.rs (L1184-1203)
```rust
    if let Some(erc20_address) = args.erc20_address {
        // ERC-20 exit; re-mint burned tokens
        let erc20_admin_address = current_address(&current_account_id);
        let mut engine: Engine<_, _> =
            Engine::new_with_state(state, erc20_admin_address, current_account_id, io, env);

        let refund_address = args.recipient_address;
        let amount = U256::from_big_endian(&args.amount);
        let input = setup_refund_on_error_input(amount, refund_address);

        engine.call(
            &erc20_admin_address,
            &erc20_address,
            Wei::zero(),
            input,
            u64::MAX,
            Vec::new(),
            Vec::new(),
            handler,
        )
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

**File:** engine-tests/src/tests/erc20_connector.rs (L656-665)
```rust
        #[cfg(feature = "error_refund")]
        let balance = FT_TRANSFER_AMOUNT.into();
        // If the refund feature is not enabled then there is no refund in the EVM
        #[cfg(not(feature = "error_refund"))]
        let balance = (FT_TRANSFER_AMOUNT - FT_EXIT_AMOUNT).into();

        assert_eq!(
            erc20_balance(&erc20, ft_owner_address, &aurora).await,
            balance
        );
```

### Title
Permanent Loss of ERC-20 Tokens When `ExitToEthereum` Withdraw Promise Fails — (`engine-precompiles/src/native.rs`)

### Summary

The `ExitToEthereum` precompile burns a user's ERC-20 tokens synchronously in the EVM, then schedules a NEAR-side `withdraw` cross-contract call with **no callback and no refund path**. If the `withdraw` promise fails for any reason, the burned tokens are permanently unrecoverable. This is the direct Aurora analog of the Beanstalk accounting bug: value is removed from one ledger (EVM balance) but the corresponding cross-chain action that should deliver equivalent value can silently fail, with no mechanism to restore the original balance.

---

### Finding Description

**Root cause — `engine-precompiles/src/native.rs`, `ExitToEthereum::run`:**

When a user calls `withdrawToEthereum` on a bridged ERC-20 (`EvmErc20.sol` or `EvmErc20V2.sol`), the Solidity contract first burns the tokens:

```solidity
// etc/eth-contracts/contracts/EvmErc20.sol lines 65-76
function withdrawToEthereum(address recipient, uint256 amount) external override {
    _burn(_msgSender(), amount);          // ← tokens destroyed here, synchronously
    ...
    assembly {
        let res := call(gas(), 0xb0bd02f6a392af548bdf1cfaee5dfa0eefcc8eab, ...);
        // res is never checked
    }
}
```

The `ExitToEthereum` precompile then creates a NEAR promise to call `withdraw` on the eth-connector:

```rust
// engine-precompiles/src/native.rs lines 977-990
let withdraw_promise = PromiseCreateArgs {
    target_account_id: nep141_address,
    method: "withdraw".to_string(),
    args: serialized_args,
    attached_balance: Yocto::new(1),
    attached_gas: costs::WITHDRAWAL_GAS,
};

let promise = borsh::to_vec(&PromiseArgs::Create(withdraw_promise)).unwrap();
// ↑ Always PromiseArgs::Create — no callback, no refund path
```

This is **always** `PromiseArgs::Create` — there is no `PromiseArgs::Callback` variant, no `exit_to_ethereum_precompile_callback`, and no `refund_on_error` call anywhere in the `ExitToEthereum` code path.

**Contrast with `ExitToNear`** (`engine-precompiles/src/native.rs` lines 470–483), which conditionally wraps the promise in a `PromiseArgs::Callback` pointing to `exit_to_near_precompile_callback`, which in turn calls `engine::refund_on_error` to re-mint burned tokens. `ExitToEthereum` has no equivalent.

The `exit_to_near_precompile_callback` handler in `engine/src/contract_methods/connector.rs` (lines 196–245) handles the refund case:

```rust
} else if let Some(args) = args.refund {
    // Exit call failed; need to refund tokens
    let refund_result = engine::refund_on_error(io, env, state, &args, handler)?;
    ...
}
```

No such handler exists for `ExitToEthereum`.

---

### Impact Explanation

**Critical — Permanent freezing of funds.**

If the NEAR-side `withdraw` call fails after the EVM-side burn succeeds:
- The ERC-20 tokens no longer exist in the EVM (burned).
- The NEP-141 tokens are not released on the NEAR side (withdraw failed).
- There is no callback, no retry, and no refund path.
- The user's funds are permanently destroyed.

---

### Likelihood Explanation

The `withdraw` promise can fail under realistic conditions:
1. **Eth-connector paused**: The eth-connector contract has a pause mechanism. If it is paused between the EVM burn and the NEAR promise execution (NEAR receipts are asynchronous), the `withdraw` call fails.
2. **Insufficient attached gas**: `costs::WITHDRAWAL_GAS` may be insufficient for the eth-connector's `withdraw` logic in certain states, causing the promise to fail with out-of-gas.
3. **Eth-connector upgrade or interface change**: If the eth-connector is upgraded and the `withdraw` interface changes, existing in-flight exits fail.
4. **Any revert in the eth-connector `withdraw` method**: e.g., proof validation failure, storage issues.

Any unprivileged EVM user who calls `withdrawToEthereum` is exposed to this risk on every call.

---

### Recommendation

Add a callback to `ExitToEthereum` analogous to the one in `ExitToNear`. Specifically:

1. Change `PromiseArgs::Create(withdraw_promise)` to `PromiseArgs::Callback(PromiseWithCallbackArgs { base: withdraw_promise, callback: refund_callback })` where `refund_callback` calls a new `exit_to_ethereum_precompile_callback` method on the engine.
2. Implement `exit_to_ethereum_precompile_callback` to call `engine::refund_on_error` (re-minting the burned ERC-20 tokens) when the `withdraw` promise result is not `Successful`.
3. Pass the `RefundCallArgs` (recipient address, ERC-20 address, amount) through the callback args, mirroring the `ExitToNearPrecompileCallbackArgs` pattern.

---

### Proof of Concept

1. User holds 100 units of a bridged ERC-20 token on Aurora.
2. User calls `withdrawToEthereum(recipient, 100)` on `EvmErc20.sol`.
3. `_burn(msg.sender, 100)` executes — EVM balance drops to 0.
4. `ExitToEthereum` precompile schedules `PromiseArgs::Create(withdraw_promise)` targeting the eth-connector.
5. The eth-connector is paused (or the `withdraw` call fails for any other reason).
6. The NEAR promise fails. No callback fires. No `refund_on_error` is called.
7. User has 0 ERC-20 tokens on Aurora and 0 tokens on Ethereum. Funds are permanently lost.

Relevant code locations:
- Burn without return-value check: [1](#0-0) 
- `ExitToEthereum` always uses `PromiseArgs::Create` with no callback: [2](#0-1) 
- `ExitToNear` has a conditional callback with refund: [3](#0-2) 
- `exit_to_near_precompile_callback` refund path (no equivalent exists for Ethereum): [4](#0-3) 
- `refund_on_error` re-mints burned ERC-20 tokens: [5](#0-4)

### Citations

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

**File:** engine/src/engine.rs (L1176-1224)
```rust
pub fn refund_on_error<I: IO + Copy, E: Env, P: PromiseHandler>(
    io: I,
    env: &E,
    state: EngineState,
    args: &RefundCallArgs,
    handler: &mut P,
) -> EngineResult<SubmitResult> {
    let current_account_id = env.current_account_id();
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
    } else {
        // ETH exit; transfer ETH back from precompile address
        let exit_address = exit_to_near::ADDRESS;
        let mut engine: Engine<_, _> =
            Engine::new_with_state(state, exit_address, current_account_id, io, env);
        let refund_address = args.recipient_address;
        let amount = Wei::new(U256::from_big_endian(&args.amount));
        engine.call(
            &exit_address,
            &refund_address,
            amount,
            Vec::new(),
            u64::MAX,
            vec![
                (exit_address.raw(), Vec::new()),
                (refund_address.raw(), Vec::new()),
            ],
            Vec::new(),
            handler,
        )
    }
```

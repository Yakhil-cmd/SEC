### Title
Unchecked Exit-Precompile Return Value After Irreversible `_burn` Causes Permanent ERC-20 Token Loss - (File: `etc/eth-contracts/contracts/EvmErc20.sol`)

---

### Summary

In `EvmErc20.sol`, both `withdrawToNear` and `withdrawToEthereum` irreversibly burn the caller's ERC-20 tokens **before** invoking the exit precompile. The `res` value returned by the assembly `call` to the precompile is never inspected. If the precompile call fails for any reason, the burn is already committed, no NEAR-side promise is ever scheduled, and the `error_refund` callback path is never reached — leaving the user's tokens permanently destroyed with no recovery.

---

### Finding Description

Both withdrawal functions follow the same two-step pattern:

**Step 1 — irreversible burn:**
```solidity
_burn(_msgSender(), amount);
```

**Step 2 — precompile call whose result is silently discarded:**
```solidity
assembly {
    let res := call(gas(), 0xe9217bc70b7ed1f598ddd3199e80b093fa71124f,
                    0, add(input, 32), input_size, 0, 32)
    // `res` is never read or checked
}
``` [1](#0-0) [2](#0-1) 

The exit precompile (`ExitToNear`) can return failure (EVM `call` returns `0`) in several reachable conditions:

- `get_nep141_from_erc20` returns `Nep141NotFound` — e.g., a contract that inherits `EvmErc20` but whose address was never registered in the engine's bijection map.
- The precompile's `required_gas` check fires when `target_gas < EXIT_TO_NEAR_GAS` — reachable when the outer transaction is submitted with a gas limit that is just sufficient for `_burn` but not for the precompile.
- Any other internal `ExitError` path inside `ExitToNear::run`. [3](#0-2) [4](#0-3) 

When the precompile call returns `0`, the EVM transaction itself does **not** revert (because the `res` is never checked with `if iszero(res) { revert(0,0) }`). The `_burn` is therefore committed. Because no `PromiseArgs` log is ever emitted, the engine's `filter_promises_from_logs` loop schedules nothing, and `exit_to_near_precompile_callback` is never invoked. [5](#0-4) 

The `error_refund` / `refund_on_error` path is only reachable when the precompile **succeeds** and the downstream NEAR promise subsequently fails. It provides zero protection when the precompile call itself returns failure. [6](#0-5) [7](#0-6) 

---

### Impact Explanation

**Permanent freezing of funds (Critical).** The user's ERC-20 tokens are destroyed by `_burn`. No NEP-141 tokens are released on the NEAR side. There is no on-chain recovery path: the engine has no record of the failed exit, and the `error_refund` callback is never triggered. The tokens are gone forever.

---

### Likelihood Explanation

**Low.** In the normal deployment path the engine always registers the NEP-141↔ERC-20 mapping immediately after deploying the contract, so `Nep141NotFound` is unlikely for engine-deployed tokens. However, the path is reachable by:

1. Any ERC-20 contract that inherits `EvmErc20` but whose address was never inserted into the engine's bijection map (e.g., a custom or mirrored deployment).
2. A transaction submitted with a gas limit that passes the `_burn` but starves the precompile of its `EXIT_TO_NEAR_GAS` budget.
3. Any future internal precompile error that causes `ExitToNear::run` to return `Err(...)`.

Because the `res` is unconditionally discarded, **any** future precompile failure mode automatically becomes a permanent-loss vector without any code change to `EvmErc20.sol`.

---

### Recommendation

Revert the transaction if the precompile call fails, so that `_burn` is also rolled back:

```solidity
function withdrawToNear(bytes memory recipient, uint256 amount) external override {
    _burn(_msgSender(), amount);

    bytes32 amount_b = bytes32(amount);
    bytes memory input = abi.encodePacked("\x01", amount_b, recipient);
    uint input_size = 1 + 32 + recipient.length;

    assembly {
        let res := call(gas(), 0xe9217bc70b7ed1f598ddd3199e80b093fa71124f,
                        0, add(input, 32), input_size, 0, 32)
        if iszero(res) { revert(0, 0) }   // <-- add this
    }
}
```

Apply the same fix to `withdrawToEthereum`. This ensures atomicity: either both the burn and the precompile call succeed, or neither does.

---

### Proof of Concept

1. Deploy (or inherit) an `EvmErc20` contract whose address is **not** present in the engine's `Nep141Erc20Map` storage (e.g., a silo mirror whose registration was skipped).
2. Call `withdrawToNear(recipient, amount)` from an account holding `amount` tokens.
3. `_burn` executes successfully — the caller's balance drops to zero.
4. The assembly `call` to the exit precompile returns `0` because `get_nep141_from_erc20` returns `Nep141NotFound`.
5. `res` is never checked; the EVM transaction completes with status `Succeed`.
6. No `PromiseArgs` log is emitted; `filter_promises_from_logs` schedules nothing; `exit_to_near_precompile_callback` is never called.
7. The caller has lost `amount` tokens permanently with no on-chain recourse. [1](#0-0) [8](#0-7) [9](#0-8)

### Citations

**File:** etc/eth-contracts/contracts/EvmErc20.sol (L53-63)
```text
    function withdrawToNear(bytes memory recipient, uint256 amount) external override {
        _burn(_msgSender(), amount);

        bytes32 amount_b = bytes32(amount);
        bytes memory input = abi.encodePacked("\x01", amount_b, recipient);
        uint input_size = 1 + 32 + recipient.length;

        assembly {
            let res := call(gas(), 0xe9217bc70b7ed1f598ddd3199e80b093fa71124f, 0, add(input, 32), input_size, 0, 32)
        }
    }
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

**File:** engine-precompiles/src/native.rs (L404-410)
```rust
        let required_gas = Self::required_gas(input)?;

        if let Some(target_gas) = target_gas
            && required_gas > target_gas
        {
            return Err(ExitError::OutOfGas);
        }
```

**File:** engine-precompiles/src/native.rs (L444-447)
```rust
                ExitToNearParams::Erc20TokenParams(ref exit_params) => {
                    exit_erc20_token_to_near(context, exit_params, &self.io)?
                }
            };
```

**File:** engine-precompiles/src/native.rs (L558-583)
```rust
fn exit_erc20_token_to_near<I: IO>(
    context: &Context,
    exit_params: &Erc20TokenParams,
    io: &I,
) -> Result<
    (
        AccountId,
        String,
        events::ExitToNear,
        String,
        Option<TransferNearArgs>,
    ),
    ExitError,
> {
    // In case of withdrawing ERC-20 tokens, the `apparent_value` should be zero. In opposite way
    // the funds will be locked in the address of the precompile without any possibility
    // to withdraw them in the future. So, in case if the `apparent_value` is not zero, the error
    // will be returned to prevent that.
    if context.apparent_value != U256::zero() {
        return Err(ExitError::Other(Cow::from(
            "ERR_ETH_ATTACHED_FOR_ERC20_EXIT",
        )));
    }

    let erc20_address = context.caller; // because ERC-20 contract calls the precompile.
    let nep141_account_id = get_nep141_from_erc20(erc20_address.as_bytes(), io)?;
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

**File:** engine/src/engine.rs (L1494-1510)
```rust
pub const fn nep141_erc20_map<I: IO>(io: I) -> BijectionMap<NEP141Account, ERC20Address, I> {
    BijectionMap::new(KeyPrefix::Nep141Erc20Map, KeyPrefix::Erc20Nep141Map, io)
}

pub fn get_erc20_from_nep141<I: IO>(
    io: &I,
    nep141_account_id: &AccountId,
) -> Result<Address, GetErc20FromNep141Error> {
    let key = bytes_to_key(KeyPrefix::Nep141Erc20Map, nep141_account_id.as_bytes());
    io.read_storage(&key)
        .map(|v| {
            let mut buf = [0u8; 20];
            v.copy_to_slice(&mut buf);
            Address::from_array(buf)
        })
        .ok_or(GetErc20FromNep141Error::Nep141NotFound)
}
```

**File:** engine/src/engine.rs (L1647-1685)
```rust
        .filter_map(|log| {
            if log.address == exit_to_near::ADDRESS.raw()
                || log.address == exit_to_ethereum::ADDRESS.raw()
            {
                if log.topics.is_empty() {
                    if let Ok(promise) = PromiseArgs::try_from_slice(&log.data) {
                        match promise {
                            PromiseArgs::Create(promise) => {
                                // Safety: this promise creation is safe because it does not come from
                                // users directly. The exit precompile only create promises which we
                                // are able to execute without violating any security invariants.
                                let id = match previous_promise {
                                    Some(base_id) => {
                                        schedule_promise_callback(handler, base_id, &promise)
                                    }
                                    None => schedule_promise(handler, &promise),
                                };
                                previous_promise = Some(id);
                            }
                            PromiseArgs::Callback(promise) => {
                                // Safety: This is safe because the promise data comes from our own
                                // exit precompiles. See note above.
                                let base_id = match previous_promise {
                                    Some(base_id) => {
                                        schedule_promise_callback(handler, base_id, &promise.base)
                                    }
                                    None => schedule_promise(handler, &promise.base),
                                };
                                let id =
                                    schedule_promise_callback(handler, base_id, &promise.callback);
                                previous_promise = Some(id);
                            }
                            PromiseArgs::Recursive(_) => {
                                unreachable!("Exit precompiles do not produce recursive promises")
                            }
                        }
                    }
                    // do not pass on these "internal logs" to the caller
                    None
```

**File:** engine/src/contract_methods/connector.rs (L231-242)
```rust
        } else if let Some(args) = args.refund {
            // Exit call failed; need to refund tokens
            let refund_result = engine::refund_on_error(io, env, state, &args, handler)?;

            if !refund_result.status.is_ok() {
                return Err(errors::ERR_REFUND_FAILURE.into());
            }

            Some(refund_result)
        } else {
            None
        };
```

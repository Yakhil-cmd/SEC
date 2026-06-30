### Title
Unvalidated Caller and Amount in `ft_on_transfer` Allows Minting Unbacked ERC-20 Tokens - (`engine/src/contract_methods/connector.rs`)

---

### Summary

The `ft_on_transfer` function in the Aurora Engine's connector module lacks both access control and value validation. Any NEAR account that is registered as a NEP-141 token in Aurora can call this function directly — bypassing the legitimate `ft_transfer_call` bridge flow — and supply an arbitrary `args.amount`, causing the engine to mint that amount of ERC-20 tokens on the EVM side without any corresponding NEP-141 token transfer having occurred. This breaks the 1:1 bridge accounting invariant and constitutes an ERC-20 mirror accounting bug.

---

### Finding Description

`ft_on_transfer` is the NEP-141 callback that Aurora exposes as a public NEAR contract method. In the legitimate bridge flow, a user calls `ft_transfer_call` on a NEP-141 token contract, which transfers tokens to Aurora and then calls `ft_on_transfer` on Aurora with the actual transferred amount. The security assumption is that only the NEP-141 token contract (as `predecessor_account_id`) calls this function, and that `args.amount` reflects a real transfer.

However, the implementation enforces neither of these assumptions:

```rust
// engine/src/contract_methods/connector.rs, lines 62-109
pub fn ft_on_transfer<I: IO + Copy, E: Env, H: PromiseHandler>(
    io: I,
    env: &E,
    handler: &mut H,
) -> Result<Option<SubmitResult>, ContractError> {
    with_hashchain(io, env, function_name!(), |mut io| {
        require_running(&state::get_state(&io)?)?;
        let current_account_id = env.current_account_id();
        let predecessor_account_id = env.predecessor_account_id();
        // ...
        let args: FtOnTransferArgs = read_json_args(&io)?;
        let result = if predecessor_account_id == get_connector_account_id(&io)? {
            engine.receive_base_tokens(&args)
        } else {
            engine.receive_erc20_tokens(
                &predecessor_account_id,
                &args,
                &current_account_id,
                handler,
            )
        };
``` [1](#0-0) 

For the ERC-20 branch (any predecessor that is not the connector account), there is:
- **No access control**: any NEAR account can be the `predecessor_account_id`
- **No value validation**: `args.amount` is taken directly from attacker-controlled JSON input and is never checked against any actual token transfer

Inside `receive_erc20_tokens`, the engine looks up the ERC-20 contract for the NEP-141 token identified by `predecessor_account_id`, then calls the ERC-20 `mint` function using the engine's own admin address with the attacker-supplied `args.amount`:

```rust
// engine/src/engine.rs, lines 796-837
let erc20_token = get_erc20_from_nep141(&self.io, token)?;
let erc20_admin_address = current_address(current_account_id);
let result = self.call(
    &erc20_admin_address,
    &erc20_token,
    Wei::zero(),
    setup_receive_erc20_tokens_input(&recipient, amount),
    u64::MAX,
    ...
``` [2](#0-1) 

Additionally, `deploy_erc20_token` — which registers a NEP-141 account as a bridged token and deploys its ERC-20 — has no access control beyond `require_running`, meaning any NEAR account can register itself as a NEP-141 token in Aurora:

```rust
// engine/src/contract_methods/connector.rs, lines 112-121
pub fn deploy_erc20_token<I: IO + Copy, E: Env, H: PromiseHandler>(
    io: I, env: &E, handler: &mut H,
) -> Result<PromiseOrValue<Address>, ContractError> {
    with_hashchain(io, env, function_name!(), |mut io| {
        require_running(&state::get_state(&io)?)?;
        // No caller check
``` [3](#0-2) 

**Complete attacker-controlled entry path:**

1. Attacker creates NEAR account `attacker.near`
2. Attacker calls `deploy_erc20_token` on Aurora for `attacker.near` (no access control)
3. Attacker calls `ft_on_transfer` on Aurora directly from `attacker.near` with `args.amount = <arbitrary large value>` and `args.msg = <attacker EVM address>`
4. Aurora's engine mints `args.amount` of the ERC-20 token (backed by `attacker.near`) to the attacker's EVM address — with zero NEP-141 tokens ever transferred

The contrast with protected callbacks is clear: `exit_to_near_precompile_callback`, `deploy_erc20_token_callback`, and `mirror_erc20_token_callback` all call `env.assert_private_call()` to enforce that only the engine itself can invoke them. `ft_on_transfer` has no equivalent guard. [4](#0-3) [5](#0-4) 

---

### Impact Explanation

The bridge accounting invariant — that every ERC-20 token on Aurora is backed 1:1 by a NEP-141 token held by the Aurora contract — is broken. An attacker can mint an unbounded supply of ERC-20 tokens on Aurora without depositing any NEP-141 tokens. These inflated tokens exist within the EVM and can be used in any DeFi protocol deployed on Aurora (AMMs, lending markets, etc.) to drain real user funds (ETH, bridged stablecoins) from those protocols. This is a direct ERC-20 mirror accounting bug with a realistic path to theft of user funds held in Aurora DeFi.

**Impact: High — ERC-20 mirror accounting bug enabling theft of user funds in Aurora DeFi protocols.**

---

### Likelihood Explanation

The attack requires no special privileges, no admin compromise, and no governance capture. Any NEAR account holder can execute it in two public transactions (`deploy_erc20_token` then `ft_on_transfer`). The only prerequisite is a NEAR account, which is freely obtainable.

---

### Recommendation

1. **Add caller validation**: In `ft_on_transfer`, verify that the call arrives as a genuine NEP-141 callback. One approach is to track pending `ft_transfer_call` operations and only accept `ft_on_transfer` from accounts with an active pending transfer. Alternatively, restrict `ft_on_transfer` to only accounts that are registered NEP-141 tokens AND have an in-flight `ft_transfer_call` promise.

2. **Add value validation**: Ensure `args.amount` cannot exceed the amount that was actually locked in the pending transfer. The NEP-141 standard guarantees the token contract provides the correct amount, but Aurora must not trust arbitrary callers to provide this value.

3. **Restrict `deploy_erc20_token`**: Add access control (e.g., owner-only or a whitelist) so that arbitrary accounts cannot register themselves as bridged NEP-141 tokens, which is a prerequisite for the attack.

---

### Proof of Concept

```
# Step 1: Create attacker.near (standard NEAR account creation)

# Step 2: Call deploy_erc20_token on Aurora from any account
near call aurora deploy_erc20_token \
  '{"nep141": "attacker.near"}' \
  --accountId anyone.near

# Step 3: Call ft_on_transfer directly from attacker.near
near call aurora ft_on_transfer \
  '{"sender_id": "attacker.near", "amount": "1000000000000000000000000", "msg": "<attacker_evm_hex_address>"}' \
  --accountId attacker.near

# Result: Aurora mints 1e24 ERC-20 tokens (backed by attacker.near) to the
# attacker's EVM address. No NEP-141 tokens were ever transferred to Aurora.
# The attacker can now use these tokens in any Aurora DeFi protocol.
```

### Citations

**File:** engine/src/contract_methods/connector.rs (L62-109)
```rust
pub fn ft_on_transfer<I: IO + Copy, E: Env, H: PromiseHandler>(
    io: I,
    env: &E,
    handler: &mut H,
) -> Result<Option<SubmitResult>, ContractError> {
    with_hashchain(io, env, function_name!(), |mut io| {
        require_running(&state::get_state(&io)?)?;
        let current_account_id = env.current_account_id();
        let predecessor_account_id = env.predecessor_account_id();
        let mut engine: Engine<_, _> = Engine::new(
            predecessor_address(&predecessor_account_id),
            current_account_id.clone(),
            io,
            env,
        )?;

        sdk::log!("Call ft_on_transfer");

        let args: FtOnTransferArgs = read_json_args(&io)?;
        let result = if predecessor_account_id == get_connector_account_id(&io)? {
            engine.receive_base_tokens(&args)
        } else {
            engine.receive_erc20_tokens(
                &predecessor_account_id,
                &args,
                &current_account_id,
                handler,
            )
        };

        #[allow(clippy::used_underscore_binding)]
        let amount_to_return = if let Err(_err) = &result {
            sdk::log!("Error in ft_on_transfer: {_err:?}");
            // An error occurred, so we need to return the amount of tokens to the sender.
            args.amount.as_u128()
        } else {
            // Everything is ok, so return 0.
            0
        };

        let output = crate::prelude::format!("\"{amount_to_return}\"");
        io.return_output(output.as_bytes());

        // In case of an error, we just return Ok(None) to avoid a panic in the contract. It's ok
        // because in case of an error, we already returned the amount of tokens to the sender.
        Ok(result.unwrap_or(None))
    })
}
```

**File:** engine/src/contract_methods/connector.rs (L112-121)
```rust
pub fn deploy_erc20_token<I: IO + Copy, E: Env, H: PromiseHandler>(
    io: I,
    env: &E,
    handler: &mut H,
) -> Result<PromiseOrValue<Address>, ContractError> {
    with_hashchain(io, env, function_name!(), |mut io| {
        require_running(&state::get_state(&io)?)?;
        let bytes = io.read_input().to_vec();
        let args =
            DeployErc20TokenArgs::deserialize(&bytes).map_err(|_| errors::ERR_BORSH_DESERIALIZE)?;
```

**File:** engine/src/contract_methods/connector.rs (L161-169)
```rust
#[named]
pub fn deploy_erc20_token_callback<I: IO + Copy, E: Env, H: PromiseHandler>(
    io: I,
    env: &E,
    handler: &mut H,
) -> Result<Address, ContractError> {
    with_hashchain(io, env, function_name!(), |mut io| {
        require_running(&state::get_state(&io)?)?;
        env.assert_private_call()?;
```

**File:** engine/src/contract_methods/connector.rs (L196-210)
```rust
pub fn exit_to_near_precompile_callback<I: IO + Copy, E: Env, H: PromiseHandler>(
    io: I,
    env: &E,
    handler: &mut H,
) -> Result<Option<SubmitResult>, ContractError> {
    with_hashchain(io, env, function_name!(), |io| {
        let state = state::get_state(&io)?;
        require_running(&state)?;
        env.assert_private_call()?;

        // This function should only be called as the callback of
        // exactly one promise.
        if handler.promise_results_count() != 1 {
            return Err(errors::ERR_PROMISE_COUNT.into());
        }
```

**File:** engine/src/engine.rs (L824-837)
```rust
        let erc20_token = get_erc20_from_nep141(&self.io, token)?;
        let erc20_admin_address = current_address(current_account_id);
        let result = self
            .call(
                &erc20_admin_address,
                &erc20_token,
                Wei::zero(),
                setup_receive_erc20_tokens_input(&recipient, amount),
                u64::MAX,
                Vec::new(), // TODO: are there values we should put here?
                Vec::new(),
                handler,
            )
            .and_then(submit_result_or_err)?;
```

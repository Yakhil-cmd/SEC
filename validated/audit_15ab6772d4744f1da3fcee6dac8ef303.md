### Title
Unrestricted `deploy_erc20_token` + Unchecked `ft_on_transfer` Enables Unbounded ERC-20 Minting Without Real Token Deposit — (`engine/src/contract_methods/connector.rs`)

---

### Summary

Any unprivileged NEAR account can call `deploy_erc20_token` to register itself as a NEP-141 token on Aurora, then repeatedly call `ft_on_transfer` with an arbitrary `amount` to mint unbounded ERC-20 tokens on Aurora without ever depositing real NEP-141 tokens. This is the direct Aurora analog of the Tokensoft `initializeDistributionRecord()` re-entrancy/re-call bug: a public, access-control-free entry point that inflates an on-chain token balance without a corresponding real-asset backing.

---

### Finding Description

**Step 1 — No access control on `deploy_erc20_token`**

`deploy_erc20_token` in `engine/src/contract_methods/connector.rs` performs only a liveness check (`require_running`) and no caller authentication:

```rust
pub fn deploy_erc20_token<I: IO + Copy, E: Env, H: PromiseHandler>(
    io: I, env: &E, handler: &mut H,
) -> Result<PromiseOrValue<Address>, ContractError> {
    with_hashchain(io, env, function_name!(), |mut io| {
        require_running(&state::get_state(&io)?)?;   // ← only guard
        ...
        DeployErc20TokenArgs::Legacy(nep141) => {
            let address = engine::deploy_erc20_token(nep141, None, io, env, handler)?;
``` [1](#0-0) 

Any NEAR account can supply any `AccountId` as the `nep141` argument. `engine::deploy_erc20_token` then deploys an `EvmErc20` contract at a deterministic address and writes the NEP-141 → ERC-20 bijection into storage via `register_token`:

```rust
engine
    .register_token(address, nep141)
    .map_err(DeployErc20Error::Register)?;
``` [2](#0-1) 

**Step 2 — `ft_on_transfer` trusts the caller's account ID and the `amount` field unconditionally**

`ft_on_transfer` is a public NEAR method. Its only routing logic is:

```rust
let result = if predecessor_account_id == get_connector_account_id(&io)? {
    engine.receive_base_tokens(&args)
} else {
    engine.receive_erc20_tokens(
        &predecessor_account_id,   // ← attacker's account ID
        &args,
        &current_account_id,
        handler,
    )
};
``` [3](#0-2) 

`receive_erc20_tokens` looks up the ERC-20 address for the predecessor account and calls `mint(recipient, amount)` on it — the `amount` comes directly from the JSON args with no verification that real tokens were transferred:

```rust
let erc20_token = get_erc20_from_nep141(&self.io, token)?;
let erc20_admin_address = current_address(current_account_id);
let result = self.call(
    &erc20_admin_address,
    &erc20_token,
    Wei::zero(),
    setup_receive_erc20_tokens_input(&recipient, amount),  // ← mint(recipient, amount)
    ...
``` [4](#0-3) 

`setup_receive_erc20_tokens_input` encodes the ERC-20 `mint` selector with the attacker-controlled `recipient` and `amount`:

```rust
pub fn setup_receive_erc20_tokens_input(recipient: &Address, amount: u128) -> Vec<u8> {
    let selector = ERC20_MINT_SELECTOR;
    let tail = ethabi::encode(&[
        ethabi::Token::Address(recipient.raw().0.into()),
        ethabi::Token::Uint(amount.into()),
    ]);
    [selector, tail.as_slice()].concat()
}
``` [5](#0-4) 

The `EvmErc20` (and `EvmErc20V2`) `mint` function is `onlyAdmin`, and the admin is the Aurora Engine contract address — which is exactly the caller used in `receive_erc20_tokens`:

```solidity
function mint(address account, uint256 amount) public onlyAdmin {
    _mint(account, amount);
}
``` [6](#0-5) 

---

### Impact Explanation

An attacker can mint an unbounded supply of an ERC-20 token on Aurora with zero real NEP-141 backing. The 1:1 bijection invariant that the bridge is designed to maintain is completely broken for the attacker-registered token. Any DeFi protocol on Aurora that accepts arbitrary ERC-20 tokens (AMMs, lending markets, etc.) can be drained of real assets by the attacker depositing the inflated fake token as collateral or liquidity. This constitutes **theft of user funds** held in those protocols and **insolvency** of the bridge accounting for the affected token.

---

### Likelihood Explanation

The attack requires only two public NEAR function calls with no special privileges, no leaked keys, and no governance capture. Any NEAR account holder can execute it. The only prerequisite is that the target `AccountId` has not already been registered as a NEP-141 token on Aurora. The attacker can use any fresh NEAR account.

---

### Recommendation

1. **Add access control to `deploy_erc20_token`**: Restrict callers to the contract owner or a governance-approved whitelist, analogous to how `mirror_erc20_token` uses `require_owner_only`.
2. **Verify real token receipt in `ft_on_transfer`**: The function should only be callable as a NEAR callback from a legitimate `ft_transfer_call` flow. Consider using `env.assert_private_call()` or checking that the predecessor is a pre-approved NEP-141 contract.
3. **Validate NEP-141 existence before registration**: Before writing the bijection map entry, verify on-chain that the supplied `AccountId` is a real, deployed NEP-141 contract (e.g., via a cross-contract view call to `ft_metadata`).

---

### Proof of Concept

```
1. Attacker controls NEAR account `attacker.near`.

2. Attacker calls `deploy_erc20_token` on Aurora Engine:
   args = DeployErc20TokenArgs::Legacy("attacker.near")
   → Aurora deploys EvmErc20 at address 0xABCD... and writes:
     Nep141Erc20Map["attacker.near"] = 0xABCD...

3. Attacker calls `ft_on_transfer` on Aurora Engine from `attacker.near`:
   predecessor = "attacker.near"
   args = { sender_id: "attacker.near", amount: "1000000000000000000000000", msg: "<attacker_evm_addr>" }
   → get_erc20_from_nep141("attacker.near") = 0xABCD... ✓
   → setup_receive_erc20_tokens_input(attacker_evm_addr, 1e24) encodes mint(attacker, 1e24)
   → Engine calls mint() as admin on 0xABCD...
   → Attacker receives 1e24 ERC-20 tokens with zero real deposit.

4. Repeat step 3 indefinitely. Each call mints another 1e24 tokens.

5. Attacker deposits inflated tokens into an Aurora DeFi protocol and withdraws real assets.
```

### Citations

**File:** engine/src/contract_methods/connector.rs (L81-90)
```rust
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
```

**File:** engine/src/contract_methods/connector.rs (L112-130)
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

        match args {
            DeployErc20TokenArgs::Legacy(nep141) => {
                let address = engine::deploy_erc20_token(nep141, None, io, env, handler)?;

                io.return_output(
                    &borsh::to_vec(address.as_bytes()).map_err(|_| errors::ERR_SERIALIZE)?,
                );
                Ok(PromiseOrValue::Value(address))
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

**File:** engine/src/engine.rs (L1306-1314)
```rust
pub fn setup_receive_erc20_tokens_input(recipient: &Address, amount: u128) -> Vec<u8> {
    let selector = ERC20_MINT_SELECTOR;
    let tail = ethabi::encode(&[
        ethabi::Token::Address(recipient.raw().0.into()),
        ethabi::Token::Uint(amount.into()),
    ]);

    [selector, tail.as_slice()].concat()
}
```

**File:** engine/src/engine.rs (L1370-1374)
```rust
    engine
        .register_token(address, nep141)
        .map_err(DeployErc20Error::Register)?;

    Ok(address)
```

**File:** etc/eth-contracts/contracts/EvmErc20V2.sol (L49-51)
```text
    function mint(address account, uint256 amount) public onlyAdmin {
        _mint(account, amount);
    }
```

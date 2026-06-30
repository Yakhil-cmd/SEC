### Title
Missing Access Control on `deploy_erc20_token` Enables Unbacked ERC-20 Minting via Fake NEP-141 Registration - (File: `engine/src/contract_methods/connector.rs`)

---

### Summary

`deploy_erc20_token` has no caller authorization check. Any NEAR account can register an arbitrary NEP-141 account ID as an ERC-20 token on Aurora, then call `ft_on_transfer` directly from that account to mint unbacked ERC-20 tokens, breaking the bridge accounting invariant (ERC-20 supply = NEP-141 holdings held by Aurora).

---

### Finding Description

`deploy_erc20_token` in `engine/src/contract_methods/connector.rs` performs only a liveness check (`require_running`) and no ownership or caller authorization check: [1](#0-0) 

The `WithMetadata` branch even carries a comment that contradicts the actual code:

```
// Safe because these promises are read-only calls to the main engine contract
// and this transaction could be executed by the owner of the contract only.
```

No such owner check exists. Compare with other privileged functions that correctly call `require_owner_only`: [2](#0-1) 

The public WASM entry point exposes this without any guard: [3](#0-2) 

Once an attacker registers their own account (`attacker.near`) as a NEP-141 token, `ft_on_transfer` accepts any call from that predecessor and routes it to `receive_erc20_tokens`: [4](#0-3) 

`receive_erc20_tokens` looks up the ERC-20 address for the predecessor and mints tokens with no verification that real NEP-141 tokens were actually deposited: [5](#0-4) 

---

### Impact Explanation

**Insolvency / bridge accounting break (Critical).** The bridge invariant — that every ERC-20 token on Aurora is backed 1:1 by a NEP-141 token held by the Aurora contract — is violated. The attacker mints ERC-20 tokens for a fake NEP-141 account without depositing any real tokens. These unbacked ERC-20 tokens can be supplied as collateral to any DeFi protocol deployed on Aurora to borrow legitimate assets (ETH, USDC, etc.), draining real user funds from those protocols. Additionally, an attacker can front-run the registration of any not-yet-registered legitimate NEP-141 token (e.g., `usdc.near`), permanently preventing that token from ever being bridged to Aurora.

---

### Likelihood Explanation

**High.** The function is a public WASM export with no access control beyond `require_running`. Any NEAR account can call it permissionlessly. The attacker needs only to control a NEAR account (trivially achievable) and make two sequential calls. No special privileges, leaked keys, or governance capture are required.

---

### Recommendation

Add `require_owner_only` (or a dedicated allowlist check) to `deploy_erc20_token`, consistent with the comment already present in the code and consistent with how other privileged connector functions are guarded:

```rust
pub fn deploy_erc20_token<I: IO + Copy, E: Env, H: PromiseHandler>(
    io: I,
    env: &E,
    handler: &mut H,
) -> Result<PromiseOrValue<Address>, ContractError> {
    with_hashchain(io, env, function_name!(), |mut io| {
        let state = state::get_state(&io)?;
        require_running(&state)?;
+       require_owner_only(&state, &env.predecessor_account_id())?;
        ...
    })
}
```

---

### Proof of Concept

1. **Register fake token.** Attacker calls `deploy_erc20_token` on Aurora with `nep141 = "attacker.near"`. No access control blocks this. Aurora deploys an ERC-20 contract and records the `attacker.near ↔ ERC-20` mapping via `register_token`. [6](#0-5) 

2. **Mint unbacked tokens.** Attacker calls `ft_on_transfer` on Aurora directly from `attacker.near` (which they control) with `sender_id = "attacker.near"`, `amount = 1_000_000_000_000`, `msg = <attacker_evm_address>`. The predecessor check passes (`attacker.near ≠ eth_connector`), `get_erc20_from_nep141` succeeds (registered in step 1), and `ERC20_MINT_SELECTOR` is called on the ERC-20 contract, crediting the attacker with 1 trillion tokens backed by zero real NEP-141 deposits. [7](#0-6) 

3. **Exploit DeFi.** Attacker supplies the unbacked ERC-20 tokens as collateral in any Aurora-deployed lending protocol and borrows legitimate assets (ETH, bridged stablecoins), draining real user funds.

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

**File:** engine/src/contract_methods/connector.rs (L112-125)
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
```

**File:** engine/src/contract_methods/mod.rs (L79-87)
```rust
pub fn require_owner_only(
    state: &state::EngineState,
    predecessor_account_id: &AccountId,
) -> Result<(), ContractError> {
    if &state.owner_id != predecessor_account_id {
        return Err(errors::ERR_NOT_ALLOWED.into());
    }
    Ok(())
}
```

**File:** engine/src/lib.rs (L613-621)
```rust
    #[unsafe(no_mangle)]
    pub extern "C" fn deploy_erc20_token() {
        let io = Runtime;
        let env = Runtime;
        let mut handler = Runtime;
        contract_methods::connector::deploy_erc20_token(io, &env, &mut handler)
            .map_err(ContractError::msg)
            .sdk_unwrap();
    }
```

**File:** engine/src/engine.rs (L722-741)
```rust
    pub fn register_token(
        &mut self,
        erc20_token: Address,
        nep141_token: AccountId,
    ) -> Result<(), RegisterTokenError> {
        match get_erc20_from_nep141(&self.io, &nep141_token) {
            Err(GetErc20FromNep141Error::Nep141NotFound) => (),
            Err(GetErc20FromNep141Error::InvalidNep141AccountId) => {
                return Err(RegisterTokenError::InvalidNep141AccountId);
            }
            Err(GetErc20FromNep141Error::InvalidAddress) => {
                return Err(RegisterTokenError::InvalidAddress);
            }
            Ok(_) => return Err(RegisterTokenError::TokenAlreadyRegistered),
        }

        let erc20_token = ERC20Address(erc20_token);
        let nep141_token = NEP141Account(nep141_token);
        nep141_erc20_map(self.io).insert(&nep141_token, &erc20_token);
        Ok(())
```

**File:** engine/src/engine.rs (L796-839)
```rust
    pub fn receive_erc20_tokens<P: PromiseHandler>(
        &mut self,
        token: &AccountId,
        args: &FtOnTransferArgs,
        current_account_id: &AccountId,
        handler: &mut P,
    ) -> Result<Option<SubmitResult>, ContractError> {
        let amount = args.amount.as_u128();
        // Parse message to determine recipient
        let mut recipient = {
            // The message should contain the recipient EOA address.
            let message = args.msg.strip_prefix("0x").unwrap_or(&args.msg);
            // Recipient - 40 characters (Address in hex without '0x' prefix)
            if message.len() < 40 {
                return Err(ParseOnTransferMessageError::WrongMessageFormat.into());
            }
            let mut address_bytes = [0; 20];
            hex::decode_to_slice(&message[..40], &mut address_bytes)
                .map_err(|_| ParseOnTransferMessageError::WrongMessageFormat)?;
            Address::from_array(address_bytes)
        };

        if let Some(fallback_address) = silo::get_erc20_fallback_address(&self.io)
            && !silo::is_allow_receive_erc20_tokens(&self.io, &recipient)
        {
            recipient = fallback_address;
        }

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

        sdk::log!("Mint {amount} ERC-20 tokens for: {}", recipient.encode());
```
